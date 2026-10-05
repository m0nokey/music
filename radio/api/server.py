#!/usr/bin/env python3
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psycopg2
from psycopg2.pool import ThreadedConnectionPool


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
TRACK_CACHE_LOCK = threading.Lock()
TRACK_CACHE = {}
TRACK_SCAN_RUNNING = False
DB_POOL = None
ADMIN_PASSWORD_FILE = Path(os.environ.get("RADIO_ADMIN_PASSWORD_FILE", "/run/secrets/music_admin_password"))
ADMIN_SESSION_SECONDS = 12 * 60 * 60
ADMIN_LOGIN_LIMIT = 3
ADMIN_LOCK_SECONDS = 15 * 60


def valid_uuid4(value):
    return isinstance(value, str) and bool(UUID_RE.fullmatch(value.lower()))


def valid_api_uuid(value):
    return valid_uuid4(value) and value.lower() == API_UUID.lower()


def client_key(handler):
    candidate = handler.headers.get("X-Real-IP", "").strip()
    try:
        address = ipaddress.ip_address(candidate or handler.client_address[0])
    except ValueError:
        address = ipaddress.ip_address(handler.client_address[0])
    return hashlib.sha256(address.compressed.encode("ascii")).hexdigest()


def session_token(handler):
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie", ""))
    except (TypeError, ValueError):
        return ""
    morsel = cookie.get("music_admin_session")
    return morsel.value if morsel else ""


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


@contextmanager
def database_connection():
    connection = DB_POOL.getconn()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        DB_POOL.putconn(connection)


def initialize_database():
    global DB_POOL
    password_file = Path(os.environ.get("RADIO_DB_PASSWORD_FILE", "/run/secrets/postgres_password"))
    password = password_file.read_text(encoding="utf-8").strip()
    DB_POOL = ThreadedConnectionPool(
        1,
        8,
        host=os.environ.get("RADIO_DB_HOST", "postgres"),
        port=int(os.environ.get("RADIO_DB_PORT", "5432")),
        dbname=os.environ.get("RADIO_DB_NAME", "music"),
        user=os.environ.get("RADIO_DB_USER", "music"),
        password=password,
        connect_timeout=5,
        application_name="music-radio-api",
    )

    migrations = Path("/app/migrations")
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )

    for migration in sorted(migrations.glob("*.sql")):
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (migration.name,))
                if cursor.fetchone():
                    continue
                cursor.execute(migration.read_text(encoding="utf-8"))
                cursor.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (migration.name,))
    print("PostgreSQL schema is ready", flush=True)


def save_track_record(path, size, mtime_ns, metadata):
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO tracks (
                    filename, storage_key, artist, title, duration_seconds,
                    file_size_bytes, file_mtime_ns, is_available
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, true)
                ON CONFLICT (filename) DO UPDATE SET
                    storage_key = EXCLUDED.storage_key,
                    artist = CASE WHEN tracks.metadata_source = 'auto' THEN EXCLUDED.artist ELSE tracks.artist END,
                    title = CASE WHEN tracks.metadata_source = 'auto' THEN EXCLUDED.title ELSE tracks.title END,
                    duration_seconds = COALESCE(EXCLUDED.duration_seconds, tracks.duration_seconds),
                    file_size_bytes = EXCLUDED.file_size_bytes,
                    file_mtime_ns = EXCLUDED.file_mtime_ns,
                    is_available = true,
                    updated_at = now()
                """,
                (
                    path.name,
                    path.name,
                    metadata.get("artist", ""),
                    metadata.get("title", ""),
                    metadata.get("duration"),
                    size,
                    mtime_ns,
                ),
            )


def display_track(filename, artist, title, duration):
    display = f"{artist} — {title}" if artist and title else title or artist or Path(filename).stem
    return {
        "filename": filename,
        "artist": artist,
        "title": title,
        "display": display,
        "duration": duration,
    }


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


def track_metadata(path):
    fallback = {
        "filename": path.name,
        "artist": "",
        "title": "",
        "display": path.stem,
        "duration": None,
    }

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration:format_tags=title,artist",
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
    raw_duration = payload.get("format", {}).get("duration")
    try:
        duration = max(0.0, float(raw_duration)) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration = None
    title = str(tags.get("title", "")).strip()
    artist = str(tags.get("artist", "")).strip()
    display = f"{artist} — {title}" if artist and title else title or artist or path.stem
    return {
        "filename": path.name,
        "artist": artist,
        "title": title,
        "display": display,
        "duration": duration,
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
                        try:
                            save_track_record(item[0], item[1], item[2], metadata)
                        except psycopg2.Error as error:
                            print(f"Could not save track metadata for {item[0].name}: {error}", flush=True)
                with TRACK_CACHE_LOCK:
                    TRACK_CACHE.update(updates)

            current_names = {item[0].name for item in inventory}
            with TRACK_CACHE_LOCK:
                TRACK_CACHE_KEYS = set(TRACK_CACHE)
                for name in TRACK_CACHE_KEYS - current_names:
                    TRACK_CACHE.pop(name, None)
            with database_connection() as connection:
                with connection.cursor() as cursor:
                    if current_names:
                        cursor.execute(
                            "UPDATE tracks SET is_available = false, updated_at = now() "
                            "WHERE storage_backend = 'local' AND is_available "
                            "AND filename <> ALL(%s)",
                            (list(current_names),),
                        )
                    else:
                        cursor.execute(
                            "UPDATE tracks SET is_available = false, updated_at = now() "
                            "WHERE storage_backend = 'local' AND is_available"
                        )
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
    inventory_names = {path.name for path, _size, _mtime_ns in inventory}
    with TRACK_CACHE_LOCK:
        cached = dict(TRACK_CACHE)
        if not TRACK_SCAN_RUNNING:
            needs_scan = (
                any(
                    path.name not in cached
                    or cached[path.name].get("size") != size
                    or cached[path.name].get("mtime_ns") != mtime_ns
                    for path, size, mtime_ns in inventory
                )
                or any(name not in inventory_names for name in cached)
            )
            if needs_scan:
                TRACK_SCAN_RUNNING = True
                threading.Thread(target=scan_track_inventory, daemon=True).start()
        loading = TRACK_SCAN_RUNNING

    tracks_by_filename = {}
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT filename, artist, title, duration_seconds "
                "FROM tracks WHERE is_available ORDER BY lower(artist), lower(title), filename"
            )
            for filename, artist, title, duration in cursor.fetchall():
                tracks_by_filename[filename] = display_track(
                    filename,
                    artist or "",
                    title or "",
                    float(duration) if duration is not None else None,
                )

    for path, size, mtime_ns in inventory:
        if path.name not in tracks_by_filename:
            tracks_by_filename[path.name] = display_track(path.name, "", "", None)
    tracks = list(tracks_by_filename.values())
    tracks.sort(key=lambda item: item["display"].casefold())
    return tracks, loading


def load_track_cache():
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT filename, artist, title, duration_seconds, file_size_bytes, file_mtime_ns "
                "FROM tracks WHERE storage_backend = 'local'"
            )
            for filename, artist, title, duration, size, mtime_ns in cursor.fetchall():
                metadata = display_track(
                    filename,
                    artist or "",
                    title or "",
                    float(duration) if duration is not None else None,
                )
                TRACK_CACHE[filename] = {
                    "size": size,
                    "mtime_ns": mtime_ns,
                    "metadata": metadata,
                }

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
                path = MUSIC_DIR / name
                if path.is_file() and not path.is_symlink():
                    try:
                        save_track_record(path, int(entry.get("size", 0)), int(entry.get("mtime_ns", 0)), entry["metadata"])
                        TRACK_CACHE[name] = entry
                    except (OSError, TypeError, ValueError, psycopg2.Error) as error:
                        print(f"Could not import cached track {name}: {error}", flush=True)


def validated_track_path(filename):
    if not isinstance(filename, str) or not TRACK_FILE_RE.fullmatch(filename):
        raise ValueError("Invalid track filename")
    path = MUSIC_DIR / filename
    if path.is_symlink() or not path.is_file():
        raise ValueError("Track not found")
    return path


class Handler(BaseHTTPRequestHandler):
    server_version = "RadioAPI/1.0"

    def send_json(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status, message):
        self.send_json(status, {"error": message})

    def has_admin_session(self):
        token = session_token(self)
        if not token or len(token) > 128:
            return False
        digest = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM admin_sessions WHERE token_hash = %s AND expires_at > now()",
                    (digest,),
                )
                return cursor.fetchone() is not None

    def require_admin(self):
        try:
            authenticated = self.has_admin_session()
        except psycopg2.Error:
            self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "Authentication service is unavailable")
            return False
        if not authenticated:
            self.fail(HTTPStatus.UNAUTHORIZED, "Sign in to continue")
            return False
        return True

    def admin_login(self):
        try:
            payload = self.read_json()
            supplied = payload.get("password")
            if not isinstance(supplied, str) or not supplied or len(supplied) > 1024:
                raise ValueError("Enter the password")
            key = client_key(self)
            with database_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO admin_login_attempts (client_key) VALUES (%s) ON CONFLICT DO NOTHING",
                        (key,),
                    )
                    cursor.execute(
                        "SELECT failed_attempts, blocked_until, blocked_until > now() "
                        "FROM admin_login_attempts WHERE client_key = %s FOR UPDATE",
                        (key,),
                    )
                    failures, blocked_until, is_blocked = cursor.fetchone()
                    if is_blocked:
                        seconds = max(1, int((blocked_until.timestamp() - time.time()) + 0.999))
                        self.send_json(
                            HTTPStatus.TOO_MANY_REQUESTS,
                            {"error": "Too many attempts. Try again later.", "retry_after": seconds},
                            {"Retry-After": str(seconds)},
                        )
                        return
                    if blocked_until is not None:
                        failures = 0
                    try:
                        expected = ADMIN_PASSWORD_FILE.read_text(encoding="utf-8").rstrip("\r\n")
                    except OSError:
                        self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "Admin password is not configured")
                        return
                    if hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
                        cursor.execute("DELETE FROM admin_login_attempts WHERE client_key = %s", (key,))
                        cursor.execute("DELETE FROM admin_sessions WHERE expires_at <= now()")
                        token = secrets.token_urlsafe(32)
                        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
                        cursor.execute(
                            "INSERT INTO admin_sessions (token_hash, expires_at) "
                            "VALUES (%s, now() + (%s * interval '1 second'))",
                            (token_hash, ADMIN_SESSION_SECONDS),
                        )
                    else:
                        failures += 1
                        blocked = failures >= ADMIN_LOGIN_LIMIT
                        cursor.execute(
                            "UPDATE admin_login_attempts SET failed_attempts = %s, "
                            "blocked_until = CASE WHEN %s THEN now() + (%s * interval '1 second') ELSE NULL END, "
                            "updated_at = now() WHERE client_key = %s",
                            (failures, blocked, ADMIN_LOCK_SECONDS, key),
                        )
                        seconds = ADMIN_LOCK_SECONDS if blocked else 0
                        self.send_json(
                            HTTPStatus.TOO_MANY_REQUESTS if blocked else HTTPStatus.UNAUTHORIZED,
                            {
                                "error": "Too many attempts. Try again later." if blocked else "Incorrect password",
                                "attempts_remaining": max(0, ADMIN_LOGIN_LIMIT - failures),
                                "retry_after": seconds,
                            },
                            {"Retry-After": str(seconds)} if blocked else None,
                        )
                        return

            secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
            if os.environ.get("RADIO_ADMIN_COOKIE_SECURE", "") == "1":
                secure = True
            cookie = (
                f"music_admin_session={token}; Path=/; Max-Age={ADMIN_SESSION_SECONDS}; "
                "HttpOnly; SameSite=Strict"
            )
            if secure:
                cookie += "; Secure"
            self.send_json(HTTPStatus.OK, {"authenticated": True}, {"Set-Cookie": cookie})
        except (ValueError, json.JSONDecodeError) as error:
            self.fail(HTTPStatus.BAD_REQUEST, str(error))
        except psycopg2.Error:
            self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "Authentication service is unavailable")

    def admin_logout(self):
        token = session_token(self)
        if token:
            digest = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
            try:
                with database_connection() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("DELETE FROM admin_sessions WHERE token_hash = %s", (digest,))
            except psycopg2.Error:
                self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "Authentication service is unavailable")
                return
        self.send_json(
            HTTPStatus.OK,
            {"authenticated": False},
            {"Set-Cookie": "music_admin_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"},
        )

    def serve_track(self, filename):
        try:
            path = validated_track_path(filename)
            size = path.stat().st_size
        except (ValueError, OSError):
            self.fail(HTTPStatus.NOT_FOUND, "track not found")
            return

        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or (not match.group(1) and not match.group(2)):
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else size - 1
            else:
                suffix = int(match.group(2))
                start = max(0, size - suffix)
            if start >= size or end < start:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT

        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", "audio/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

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
        if path == "/auth/session":
            try:
                if self.has_admin_session():
                    self.send_json(HTTPStatus.OK, {"authenticated": True})
                else:
                    self.fail(HTTPStatus.UNAUTHORIZED, "Sign in to continue")
            except psycopg2.Error:
                self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "Authentication service is unavailable")
            return
        if path == "/bootstrap":
            if not valid_api_uuid(API_UUID):
                self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, "API is not configured")
                return
            self.send_json(HTTPStatus.OK, {"api_base": f"/v1/{API_UUID}"})
            return

        tracks_path = f"/v1/{API_UUID}/tracks"
        media_prefix = f"/v1/{API_UUID}/media/"
        if path.startswith(media_prefix):
            self.serve_track(path[len(media_prefix):])
            return
        if path == tracks_path:
            try:
                tracks, loading = list_tracks()
            except (OSError, subprocess.SubprocessError, psycopg2.Error):
                self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "Track library is unavailable")
                return
            self.send_json(HTTPStatus.OK, {"tracks": tracks, "loading": loading})
            return

        playlists_path = f"/v1/{API_UUID}/playlists"
        if path == playlists_path:
            try:
                with database_connection() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT p.slot, p.name, p.ascii_art,
                                   count(t.filename)::integer,
                                   COALESCE(sum(t.duration_seconds), 0)::double precision
                            FROM playlists p
                            LEFT JOIN playlist_tracks pt ON pt.playlist_slot = p.slot
                            LEFT JOIN tracks t ON t.filename = pt.track_filename AND t.is_available
                            GROUP BY p.slot, p.name, p.ascii_art
                            ORDER BY p.slot
                            """
                        )
                        rows = cursor.fetchall()
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "playlists": [
                            {
                                "slot": slot,
                                "name": name,
                                "ascii_art": ascii_art,
                                "track_count": count,
                                "duration": duration,
                            }
                            for slot, name, ascii_art, count, duration in rows
                        ]
                    },
                )
            except psycopg2.Error:
                self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "Playlist data is unavailable")
            return

        playlist_prefix = f"{playlists_path}/"
        if path.startswith(playlist_prefix):
            raw_slot = path[len(playlist_prefix):]
            if not raw_slot.isdigit() or not 1 <= int(raw_slot) <= 8:
                self.fail(HTTPStatus.NOT_FOUND, "playlist not found")
                return
            slot = int(raw_slot)
            try:
                with database_connection() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT slot, name, ascii_art FROM playlists WHERE slot = %s",
                            (slot,),
                        )
                        playlist = cursor.fetchone()
                        if playlist is None:
                            self.fail(HTTPStatus.NOT_FOUND, "playlist not found")
                            return
                        cursor.execute(
                            """
                            SELECT t.filename, t.artist, t.title, t.duration_seconds
                            FROM playlist_tracks pt
                            JOIN tracks t ON t.filename = pt.track_filename
                            WHERE pt.playlist_slot = %s AND t.is_available
                            ORDER BY pt.position
                            """,
                            (slot,),
                        )
                        rows = cursor.fetchall()
                playlist_tracks = [
                    display_track(
                        filename,
                        artist or "",
                        title or "",
                        float(duration) if duration is not None else None,
                    )
                    for filename, artist, title, duration in rows
                ]
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "playlist": {
                            "slot": playlist[0],
                            "name": playlist[1],
                            "ascii_art": playlist[2],
                            "tracks": playlist_tracks,
                            "track_count": len(playlist_tracks),
                            "duration": sum(track[3] or 0 for track in rows),
                        }
                    },
                )
            except psycopg2.Error:
                self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "Playlist data is unavailable")
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

    def do_HEAD(self):
        path = urlsplit(self.path).path
        media_prefix = f"/v1/{API_UUID}/media/"
        if path.startswith(media_prefix):
            self.serve_track(path[len(media_prefix):])
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/auth/login":
            self.admin_login()
            return
        if path == "/auth/logout":
            self.admin_logout()
            return
        if not self.require_admin():
            return

        skip_path = f"/v1/{API_UUID}/skip"
        if path == skip_path:
            try:
                liquidsoap_command("radio.skip")
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
                    response = liquidsoap_command(f"next_track.push {track}")
                    liquidsoap_command("radio.skip")
            except (ValueError, json.JSONDecodeError) as error:
                self.fail(HTTPStatus.BAD_REQUEST, str(error))
                return
            except OSError:
                self.fail(HTTPStatus.BAD_GATEWAY, "Liquidsoap control is unavailable")
                return
            request_id = response.splitlines()[0].strip()
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
    try:
        admin_password = ADMIN_PASSWORD_FILE.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as error:
        raise SystemExit(f"Admin password secret is unavailable: {error}") from error
    if len(admin_password) < 12:
        raise SystemExit("Admin password must contain at least 12 characters")
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    initialize_database()
    load_track_cache()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
