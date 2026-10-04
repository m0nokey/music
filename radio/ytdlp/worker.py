#!/usr/bin/env python3
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit


QUEUE_NEW = Path(os.environ.get("RADIO_QUEUE_NEW", "/queue/new"))
QUEUE_PROCESSING = Path(os.environ.get("RADIO_QUEUE_PROCESSING", "/queue/processing"))
QUEUE_FAILED = Path(os.environ.get("RADIO_QUEUE_FAILED", "/queue/failed"))
INCOMING = Path(os.environ.get("RADIO_INCOMING_DIR", "/incoming"))
MUSIC = Path(os.environ.get("RADIO_MUSIC_DIR", "/music"))
STATE = Path(os.environ.get("RADIO_STATE_DIR", "/state"))
MAX_ITEMS = int(os.environ.get("RADIO_MAX_PLAYLIST_ITEMS", "100"))
MAX_SECONDS = int(os.environ.get("RADIO_MAX_VIDEO_SECONDS", "7200"))
JOB_TIMEOUT = int(os.environ.get("RADIO_JOB_TIMEOUT_SECONDS", "3600"))
POLL_SECONDS = float(os.environ.get("RADIO_POLL_SECONDS", "2"))
UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


def valid_job_id(value):
    return isinstance(value, str) and bool(UUID4_RE.fullmatch(value.lower()))


def valid_source_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and hostname in ALLOWED_HOSTS
        and not parsed.username
        and not parsed.password
        and port is None
        and bool(parsed.path)
        and not parsed.fragment
    )


def write_state(job_id, status, message, **extra):
    payload = {"job_id": job_id, "status": status, "message": message, **extra}
    target = STATE / "jobs" / f"{job_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)


def move_to_failed(job_file):
    try:
        os.replace(job_file, QUEUE_FAILED / job_file.name)
    except FileNotFoundError:
        pass


def run_command(arguments):
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=JOB_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise RuntimeError("download timed out")


def validate_audio(path):
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == b"audio"


def publish_audio(source):
    if source.suffix.lower() != ".m4a" or source.is_symlink() or not source.is_file():
        return False
    destination = MUSIC / source.name
    if destination.exists():
        return False
    temporary = MUSIC / f".{source.name}.part"
    with source.open("rb") as input_file, temporary.open("wb") as output_file:
        shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
        output_file.flush()
        os.fsync(output_file.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, destination)
    return True


def process(job_file):
    try:
        job = json.loads(job_file.read_text(encoding="utf-8"))
        job_id = job["job_id"]
        url = job["url"]
        mode = job["mode"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        move_to_failed(job_file)
        return

    if not valid_job_id(job_id) or not valid_source_url(url) or mode not in {"single", "playlist"}:
        move_to_failed(job_file)
        return

    write_state(job_id, "processing", "downloading")
    staging = INCOMING / job_id
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=False)
    try:
        arguments = [
            "yt-dlp",
            "--ignore-errors",
            "--no-abort-on-error",
            "--extract-audio",
            "--audio-format", "m4a",
            "--audio-quality", "0",
            "--embed-metadata",
            "--no-write-playlist-metafiles",
            "--windows-filenames",
            "--download-archive", str(STATE / "download-archive.txt"),
            "--playlist-end", str(MAX_ITEMS),
            "--match-filter", f"duration <= {MAX_SECONDS}",
            "--paths", f"home:{staging}",
            "--paths", f"temp:{staging / '.tmp'}",
            "--output", "%(id)s.%(ext)s",
            "--yes-playlist" if mode == "playlist" else "--no-playlist",
            url,
        ]
        if run_command(arguments) != 0:
            raise RuntimeError("downloader returned an error")

        added = 0
        for audio in sorted(staging.glob("*.m4a")):
            if validate_audio(audio) and publish_audio(audio):
                added += 1
        if added == 0:
            raise RuntimeError("no valid audio tracks were downloaded")

        write_state(job_id, "completed", f"added {added} track(s)", added=added)
        try:
            job_file.unlink()
        except FileNotFoundError:
            pass
    except Exception as error:
        write_state(job_id, "failed", str(error)[:160])
        move_to_failed(job_file)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main():
    for directory in (QUEUE_NEW, QUEUE_PROCESSING, QUEUE_FAILED, INCOMING, MUSIC, STATE / "jobs"):
        directory.mkdir(parents=True, exist_ok=True)
    while True:
        candidates = sorted(QUEUE_NEW.glob("*.json"))
        if not candidates:
            time.sleep(POLL_SECONDS)
            continue
        source = candidates[0]
        claimed = QUEUE_PROCESSING / source.name
        try:
            os.replace(source, claimed)
        except FileNotFoundError:
            continue
        process(claimed)


if __name__ == "__main__":
    main()
