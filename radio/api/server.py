#!/usr/bin/env python3
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


API_UUID = os.environ.get("RADIO_API_UUID", "")
QUEUE_DIR = Path(os.environ.get("RADIO_QUEUE_DIR", "/queue/new"))
STATE_DIR = Path(os.environ.get("RADIO_STATE_DIR", "/state/jobs"))
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
MAX_BODY = 16 * 1024
LIQUIDSOAP_CONTROL_HOST = os.environ.get("LIQUIDSOAP_CONTROL_HOST", "liquidsoap")
LIQUIDSOAP_CONTROL_PORT = int(os.environ.get("LIQUIDSOAP_CONTROL_PORT", "1234"))
MUSIC_DIR = Path(os.environ.get("RADIO_MUSIC_DIR", "/music"))
TRACK_FILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}\.m4a$")
MAX_TRACKS = 500
MAX_CONTROL_RESPONSE = 64 * 1024
CONTROL_LOCK = threading.Lock()
PLAYBACK_COMMAND_LOCK = threading.Lock()
PLAYBACK_COMMAND_FILE = STATE_DIR / ".playback-command.json"
PLAYBACK_COMMAND = {"revision": 0, "action": None, "target": None}
TRACK_CACHE_LOCK = threading.Lock()
TRACK_CACHE = {}
TRACK_SCAN_RUNNING = False


def valid_uuid4(value):
    return isinstance(value, str) and bool(UUID_RE.fullmatch(value.lower()))


def valid_api_uuid(value):
    return valid_uuid4(value) and value.lower() == API_UUID.lower()


def valid_youtube_url(value):
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


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def infer_mode(value, requested):
    if requested in {"single", "playlist"}:
        return requested
    query = parse_qs(urlsplit(value).query)
    return "playlist" if query.get("list") else "single"


def liquidsoap_command(command):
    with socket.create_connection(
        (LIQUIDSOAP_CONTROL_HOST, LIQUIDSOAP_CONTROL_PORT),
        timeout=5,
    ) as connection:
        connection.settimeout(5)
        connection.sendall((command + "\n").encode("utf-8"))
        response = bytearray()
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_CONTROL_RESPONSE:
                raise ValueError("Liquidsoap response is too large")

            normalized = bytes(response).replace(b"\r\n", b"\n")
            if normalized == b"END\n" or normalized.endswith(b"\nEND\n"):
                break

    output = bytes(response).replace(b"\r\n", b"\n").decode(
        "utf-8",
        errors="replace",
    ).strip()
    if not output.endswith("END"):
        raise ValueError("Incomplete Liquidsoap response")
    if "ERROR" in output.upper():
        raise ValueError("Liquidsoap rejected the command")
    return output


def play_next_track(path):
    """Queue a local track, wait until Liquidsoap can play it, then skip to it."""
    response = liquidsoap_command(f"next_track.push {path}")
    first_line = response.splitlines()[0].strip()
    if not first_line.isdecimal():
        raise OSError("Liquidsoap did not return a request id")

    request_id = int(first_line)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = liquidsoap_command(f"request.status {request_id}")
        status_lines = status.splitlines()
        request_status = status_lines[0].strip().lower() if status_lines else ""

        if request_status == "ready":
            liquidsoap_command("radio.skip")
            return request_id
        if request_status == "playing":
            return request_id
        if request_status in {"destroyed", ""}:
            raise OSError(f"Liquidsoap request {request_id} is {request_status or 'unknown'}")

        time.sleep(0.2)

    try:
        liquidsoap_command(f"next_track.remove_request_id {request_id}")
    except OSError:
        pass
    raise OSError(f"Liquidsoap request {request_id} did not become ready")


def track_metadata(path):
    fallback = {
        "filename": path.name,
        "artist": "",
        "title": "",
        "display": path.stem,
    }

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format_tags=title,artist",
                "-of", "json",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return fallback

    if result.returncode != 0:
        return fallback

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return fallback

    tags = payload.get("format", {}).get("tags", {})
    title = str(tags.get("title", "")).strip()
    artist = str(tags.get("artist", "")).strip()
    display = f"{artist} — {title}" if artist and title else title or artist or path.stem
    return {
        "filename": path.name,
        "artist": artist,
        "title": title,
        "display": display,
    }


def track_inventory():
    files = []
    for path in sorted(MUSIC_DIR.glob("*.m4a"), key=lambda item: item.name):
        if len(files) >= MAX_TRACKS:
            break
        try:
            if path.is_symlink() or not TRACK_FILE_RE.fullmatch(path.name):
                continue
            info = path.stat()
        except FileNotFoundError:
            continue
        if not path.is_file():
            continue
        files.append((path, info.st_size, info.st_mtime_ns))
    return files


def save_track_cache():
    with TRACK_CACHE_LOCK:
        snapshot = dict(TRACK_CACHE)
    try:
        atomic_json(STATE_DIR / ".library-cache.json", snapshot)
    except OSError:
        # The in-memory cache remains useful if persistent cache writes fail.
        pass


def scan_track_inventory():
    global TRACK_SCAN_RUNNING
    try:
        while True:
            inventory = track_inventory()
            with TRACK_CACHE_LOCK:
                cached = dict(TRACK_CACHE)
            pending = [
                item for item in inventory
                if item[0].name not in cached
                or cached[item[0].name].get("size") != item[1]
                or cached[item[0].name].get("mtime_ns") != item[2]
            ]

            if pending:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = pool.map(lambda item: track_metadata(item[0]), pending)
                    updates = {}
                    for item, metadata in zip(pending, results):
                        updates[item[0].name] = {
                            "size": item[1],
                            "mtime_ns": item[2],
                            "metadata": metadata,
                        }
                with TRACK_CACHE_LOCK:
                    TRACK_CACHE.update(updates)

            current_names = {item[0].name for item in inventory}
            with TRACK_CACHE_LOCK:
                TRACK_CACHE_KEYS = set(TRACK_CACHE)
                for name in TRACK_CACHE_KEYS - current_names:
                    TRACK_CACHE.pop(name, None)
            save_track_cache()

            latest = track_inventory()
            if [(p.name, size, mtime) for p, size, mtime in latest] == [
                (p.name, size, mtime) for p, size, mtime in inventory
            ]:
                break
    finally:
        with TRACK_CACHE_LOCK:
            TRACK_SCAN_RUNNING = False


def list_tracks():
    global TRACK_SCAN_RUNNING
    inventory = track_inventory()
    with TRACK_CACHE_LOCK:
        cached = dict(TRACK_CACHE)
        if not TRACK_SCAN_RUNNING:
            needs_scan = any(
                path.name not in cached
                or cached[path.name].get("size") != size
                or cached[path.name].get("mtime_ns") != mtime_ns
                for path, size, mtime_ns in inventory
            )
            if needs_scan:
                TRACK_SCAN_RUNNING = True
                threading.Thread(target=scan_track_inventory, daemon=True).start()
        loading = TRACK_SCAN_RUNNING

    tracks = []
    for path, size, mtime_ns in inventory:
        entry = cached.get(path.name)
        if entry and entry.get("size") == size and entry.get("mtime_ns") == mtime_ns:
            tracks.append(entry["metadata"])
        else:
            tracks.append({
                "filename": path.name,
                "artist": "",
                "title": "",
                "display": path.stem,
            })
    tracks.sort(key=lambda item: item["display"].casefold())
    return tracks, loading


def load_track_cache():
    try:
        payload = json.loads((STATE_DIR / ".library-cache.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    with TRACK_CACHE_LOCK:
        for name, entry in payload.items():
            if (
                isinstance(name, str)
                and TRACK_FILE_RE.fullmatch(name)
                and isinstance(entry, dict)
                and isinstance(entry.get("metadata"), dict)
            ):
                TRACK_CACHE[name] = entry


def playback_target(path):
    """Return cached ID3-equivalent tags without delaying the queue command."""
    try:
        stat = path.stat()
    except OSError:
        return None
    with TRACK_CACHE_LOCK:
        entry = TRACK_CACHE.get(path.name)
        if (
            not entry
            or entry.get("size") != stat.st_size
            or entry.get("mtime_ns") != stat.st_mtime_ns
        ):
            return None
        metadata = entry.get("metadata", {})
        artist = str(metadata.get("artist", "")).strip()
        title = str(metadata.get("title", "")).strip()
    if not artist and not title:
        return None
    return {"artist": artist, "title": title}


def publish_playback_command(action, target=None, issued_at_ms=None):
    global PLAYBACK_COMMAND
    with PLAYBACK_COMMAND_LOCK:
        event = {
            "revision": PLAYBACK_COMMAND["revision"] + 1,
            "action": action,
            "target": target,
            "issued_at_ms": issued_at_ms or int(time.time() * 1000),
        }
        PLAYBACK_COMMAND = event
        try:
            atomic_json(PLAYBACK_COMMAND_FILE, event)
        except OSError as error:
            # Keep the live in-memory signal usable if persistence is unavailable.
            print(f"Could not persist playback command: {error}", flush=True)
        return event


def load_playback_command():
    global PLAYBACK_COMMAND
    try:
        payload = json.loads(PLAYBACK_COMMAND_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("revision"), int)
        and payload["revision"] >= 0
    ):
        PLAYBACK_COMMAND = payload


def validated_track_path(filename):
    if not isinstance(filename, str) or not TRACK_FILE_RE.fullmatch(filename):
        raise ValueError("Invalid track filename")
    path = MUSIC_DIR / filename
    if path.is_symlink() or not path.is_file():
        raise ValueError("Track not found")
    return path


class Handler(BaseHTTPRequestHandler):
    server_version = "RadioAPI/1.0"

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status, message):
        self.send_json(status, {"error": message})

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            raise ValueError("invalid content length")
        if length < 0 or length > MAX_BODY:
            raise ValueError("request is too large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("incomplete request")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("JSON object is required")
        return payload

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/healthz":
            self.send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if path == "/bootstrap":
            if not valid_api_uuid(API_UUID):
                self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, "API is not configured")
                return
            self.send_json(HTTPStatus.OK, {"api_base": f"/v1/{API_UUID}"})
            return

        tracks_path = f"/v1/{API_UUID}/tracks"
        if path == tracks_path:
            try:
                tracks, loading = list_tracks()
            except (OSError, subprocess.SubprocessError):
                self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "Track library is unavailable")
                return
            self.send_json(HTTPStatus.OK, {"tracks": tracks, "loading": loading})
            return

        command_path = f"/v1/{API_UUID}/playback-command"
        if path == command_path:
            with PLAYBACK_COMMAND_LOCK:
                command = {**PLAYBACK_COMMAND, "server_time_ms": int(time.time() * 1000)}
            self.send_json(HTTPStatus.OK, command)
            return

        prefix = f"/v1/{API_UUID}/jobs/"
        if not path.startswith(prefix):
            self.fail(HTTPStatus.NOT_FOUND, "not found")
            return
        job_id = path[len(prefix):]
        if "/" in job_id or not valid_uuid4(job_id):
            self.fail(HTTPStatus.NOT_FOUND, "not found")
            return
        state_file = STATE_DIR / f"{job_id}.json"
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.fail(HTTPStatus.NOT_FOUND, "job not found")
            return
        except (OSError, json.JSONDecodeError):
            self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "job state is unavailable")
            return
        self.send_json(HTTPStatus.OK, payload)

    def do_POST(self):
        path = urlsplit(self.path).path
        skip_path = f"/v1/{API_UUID}/skip"
        if path == skip_path:
            try:
                with CONTROL_LOCK:
                    issued_at_ms = int(time.time() * 1000)
                    liquidsoap_command("radio.skip")
                    publish_playback_command("skip", issued_at_ms=issued_at_ms)
            except (OSError, ValueError):
                self.fail(HTTPStatus.BAD_GATEWAY, "Liquidsoap control is unavailable")
                return
            self.send_json(HTTPStatus.OK, {"status": "ok", "message": "Current track skipped"})
            return

        play_next_path = f"/v1/{API_UUID}/play-next"
        if path == play_next_path:
            try:
                payload = self.read_json()
                track = validated_track_path(payload.get("filename"))
                with CONTROL_LOCK:
                    request_id = play_next_track(track)
                    issued_at_ms = int(time.time() * 1000)
                    publish_playback_command(
                        "play-next",
                        playback_target(track),
                        issued_at_ms,
                    )
            except (ValueError, json.JSONDecodeError) as error:
                self.fail(HTTPStatus.BAD_REQUEST, str(error))
                return
            except OSError:
                self.fail(HTTPStatus.BAD_GATEWAY, "Liquidsoap control is unavailable")
                return
            self.send_json(
                HTTPStatus.ACCEPTED,
                {
                    "status": "queued",
                    "message": "Selected track is starting",
                    "filename": track.name,
                    "request_id": request_id,
                },
            )
            return

        prefix = f"/v1/{API_UUID}/jobs"
        if path != prefix:
            self.fail(HTTPStatus.NOT_FOUND, "not found")
            return
        try:
            payload = self.read_json()
            raw_url = payload.get("url", "")
            if not isinstance(raw_url, str):
                raise ValueError("url must be a string")
            value = raw_url.strip()
            requested_mode = payload.get("mode", "auto")
            if not valid_youtube_url(value):
                raise ValueError("only HTTPS YouTube URLs are accepted")
            if requested_mode not in {"auto", "single", "playlist"}:
                raise ValueError("invalid mode")
            mode = infer_mode(value, requested_mode)
            job_id = str(uuid.uuid4())
            if not valid_uuid4(job_id):
                raise ValueError("could not create job id")
            state = {
                "job_id": job_id,
                "status": "queued",
                "mode": mode,
                "message": "queued",
            }
            queue = {
                "job_id": job_id,
                "url": value,
                "mode": mode,
                "url_sha256": hashlib.sha256(value.encode()).hexdigest(),
            }
            atomic_json(STATE_DIR / f"{job_id}.json", state)
            atomic_json(QUEUE_DIR / f"{job_id}.json", queue)
        except (ValueError, json.JSONDecodeError) as error:
            self.fail(HTTPStatus.BAD_REQUEST, str(error))
            return
        except OSError:
            self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "queue is unavailable")
            return
        self.send_json(HTTPStatus.ACCEPTED, {"job_id": job_id, "status": "queued"})

    def log_message(self, format_string, *args):
        # Do not log request bodies or URLs. The path contains only API/job UUIDs.
        super().log_message(format_string, *args)


def main():
    if not valid_api_uuid(API_UUID):
        raise SystemExit("RADIO_API_UUID must be a UUID v4")
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    load_track_cache()
    load_playback_command()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
