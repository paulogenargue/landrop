#!/usr/bin/env python3
"""landrop server: on-demand file + clipboard drop box between a phone and this machine."""

import argparse
import hmac
import itertools
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.security import safe_join

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_PORT = 8787
DEFAULT_MAX_UPLOAD_MB = 2048
COOKIE_NAME = "landrop_token"
TEXT_PREVIEW_BYTES = 200
MAX_TEXT_BYTES = 10 * 1024 * 1024  # text bodies are read into memory, files are streamed
# PowerShell alone can take several seconds to cold-start on Windows.
CLIPBOARD_TIMEOUT = 15 if sys.platform == "win32" else 5
# Only these are ever rendered inline (thumbnails). Everything else downloads,
# so an uploaded .html/.svg can't run script on this origin.
INLINE_MIMETYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/avif"}

# mimetypes reads the OS's own database (Windows registry, macOS mime.types),
# which disagrees across platforms (text/vcard is ".vcard" on macOS and missing
# on Windows), so pin the types a phone actually shares.
UPLOAD_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "image/heic": ".heic", "image/heif": ".heif", "video/quicktime": ".mov", "video/mp4": ".mp4",
    "audio/mp4": ".m4a", "audio/mpeg": ".mp3", "application/pdf": ".pdf", "application/zip": ".zip",
    "application/json": ".json", "text/plain": ".txt", "text/csv": ".csv", "text/vcard": ".vcf",
    "text/x-vcard": ".vcf", "text/calendar": ".ics", "text/html": ".html",
}

log = logging.getLogger("landrop")

# Characters that break a path, a Windows filename, or a URL the iOS Shortcut
# builds by appending the name unencoded.
_UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*#%&+]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"{dev}{n}" for dev in ("COM", "LPT") for n in [*"0123456789", "\u00b9", "\u00b2", "\u00b3"]),
}


def safe_filename(name):
    """Like werkzeug's secure_filename, but keeps non-ASCII letters (so a photo
    named in Japanese stays recognizable) while still blocking traversal,
    Windows-reserved names and URL-hostile characters."""
    name = unicodedata.normalize("NFC", name or "").replace("\\", "/").rsplit("/", 1)[-1]
    # Cf covers invisible tricks like U+202E (right-to-left override) spoofing an extension.
    name = "".join(c for c in name if unicodedata.category(c) not in ("Cc", "Cf"))
    name = re.sub(r"\s+", "_", _UNSAFE_NAME.sub("_", name)).strip("._")
    stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
    if len(ext.encode("utf-8")) > 20:  # not a real extension, don't protect it
        stem, dot, ext = name, "", ""
    # Filesystems cap names at 255 bytes; trim the stem so the extension survives.
    stem = stem.encode("utf-8")[: 200 - len(ext.encode("utf-8"))].decode("utf-8", "ignore")
    name = stem + dot + ext if stem else ext
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return name or "upload"


def load_token(path):
    """Read the shared secret, generating it (owner-only perms) on first run."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32) + "\n")
        log.info("generated new shared secret in %s", path)
    token = Path(path).read_text().strip()
    if len(token) < 32:
        sys.exit(f"{path} holds a token shorter than 32 characters; delete it to regenerate")
    return token


def _clipboard_cmds():
    """(copy, paste, env) for this OS, or None if no clipboard tool is available."""
    if sys.platform == "darwin":
        # launchd agents start without LANG, and pbcopy/pbpaste then mangle non-ASCII.
        return ["pbcopy"], ["pbpaste"], {**os.environ, "LANG": "en_US.UTF-8"}
    if sys.platform == "win32":
        ps = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
        utf8 = "$e = New-Object Text.UTF8Encoding $false; "
        return (
            ps + [utf8 + "[Console]::InputEncoding = $e; Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
            ps + [utf8 + "[Console]::OutputEncoding = $e; Get-Clipboard -Raw"],
            None,
        )
    if shutil.which("wl-copy") and shutil.which("wl-paste"):
        return ["wl-copy"], ["wl-paste", "--no-newline"], None
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"], None
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"], None
    return None


def create_app(data_dir, max_upload_mb=DEFAULT_MAX_UPLOAD_MB, clipboard=True):
    data_dir = Path(data_dir).resolve()
    incoming_dir = data_dir / "incoming"
    outgoing_dir = data_dir / "outgoing"
    boxes = {"incoming": incoming_dir, "outgoing": outgoing_dir}
    for d in boxes.values():
        d.mkdir(parents=True, exist_ok=True)
    secret_token = load_token(data_dir / "landrop.token")

    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = max_upload_mb * 1024 * 1024

    def token_ok(supplied):
        return hmac.compare_digest((supplied or "").encode(), secret_token.encode())

    def new_file(dest_dir, name, stamp=True):
        """Create and return an empty file for `name` (timestamp-prefixed by
        default) that didn't exist before. The timestamp alone isn't unique:
        Windows clocks can tick as coarsely as ~16 ms, so bursts of shares would
        overwrite each other. O_EXCL plus a -N suffix makes that impossible."""
        base = f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{name}" if stamp else name
        stem, dot, ext = base.rpartition(".") if "." in base else (base, "", "")
        for n in itertools.count():
            dest = dest_dir / (base if n == 0 else f"{stem}-{n}{dot}{ext}")
            try:
                os.close(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
                return dest
            except FileExistsError:
                continue

    @app.before_request
    def require_shared_secret():
        # Shortcuts send the header; the browser UI can't attach headers to plain
        # links/<img> tags, so it authenticates with a cookie set by /ui/login.
        if request.path == "/ui/login":
            return None
        if token_ok(request.headers.get("X-Landrop-Token")):
            return None
        if token_ok(request.cookies.get(COOKIE_NAME)):
            # SameSite=Strict still lets other ports on this host ride the cookie,
            # so state-changing cookie requests must come from this exact origin.
            origin = request.headers.get("Origin")
            if request.method not in ("GET", "HEAD") and origin != request.host_url.rstrip("/"):
                return jsonify(error="cross-origin request refused"), 403
            return None
        if request.path == "/ui" or request.path.startswith("/ui/"):
            return redirect("/ui/login")
        return jsonify(error="unauthorized"), 401

    @app.after_request
    def harden_and_log(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"  # "no-referrer" makes form POSTs send Origin: null
        if request.path.startswith(("/files/", "/api/")):
            resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        else:
            resp.headers["X-Frame-Options"] = "DENY"
        log.info("%s %s %s %s", request.remote_addr, request.method, request.path, resp.status_code)
        return resp

    @app.errorhandler(HTTPException)
    def json_error(e):
        return jsonify(error=e.description), e.code

    def text_field():
        """Raw body if present, else a 'text' form or JSON field. Reads raw bytes
        first (cache=True) so the body stream is still available for form parsing
        afterward -- request.form consumes the stream, so checking it first would
        make the raw-body fallback silently return empty."""
        if (request.content_length or 0) > MAX_TEXT_BYTES:
            abort(413, f"text bodies are limited to {MAX_TEXT_BYTES // (1024 * 1024)} MB")
        raw = request.get_data(cache=True)
        if request.is_json:
            # Shortcuts' "Request Body: JSON" sends {"text": ...}; storing that
            # literally would put braces on someone's clipboard.
            body = request.get_json(silent=True)
            return body.get("text") if isinstance(body, dict) and isinstance(body.get("text"), str) else None
        text = request.form.get("text")
        if not text and raw:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None  # binary data with no recognized file part -- don't mangle it
        return text

    def save_stream(dest):
        try:
            with open(dest, "wb") as f:
                shutil.copyfileobj(request.stream, f, 1024 * 1024)
        except BaseException:
            dest.unlink(missing_ok=True)  # don't leave a truncated file behind
            raise

    @app.route("/upload", methods=["POST"])
    def upload():
        mimetype = request.mimetype

        if mimetype == "multipart/form-data":
            if "file" not in request.files:
                text = request.form.get("text")
                if not text:
                    return jsonify(error="no file or text in request"), 400
                dest = new_file(incoming_dir, "text.txt")
                dest.write_text(text, encoding="utf-8")
                return jsonify(saved=dest.name)
            upload_file = request.files["file"]
            name = safe_filename(upload_file.filename)
            dest = new_file(incoming_dir, name)
            upload_file.save(dest)
            return jsonify(saved=dest.name)

        # iOS Shortcuts' "Request Body: File" option posts the raw file bytes
        # with Content-Type set to the shared item's real type (e.g. image/jpeg)
        # rather than wrapping it in multipart -- so anything other than a small
        # text/plain or form body is a real file, kept byte-for-byte with its
        # type's extension (.csv, .vcf, ...). Streamed to disk so a large video
        # never sits in memory.
        small = (request.content_length or 0) <= MAX_TEXT_BYTES
        is_text_share = mimetype in ("text/plain", "application/x-www-form-urlencoded", "") and small
        if not is_text_share:
            ext = UPLOAD_EXTENSIONS.get(mimetype) or mimetypes.guess_extension(mimetype) or ""
            dest = new_file(incoming_dir, f"{'text' if mimetype == 'text/plain' else 'upload'}{ext}")
            save_stream(dest)
            if dest.stat().st_size == 0:
                dest.unlink()
                return jsonify(error="empty request body"), 400
            return jsonify(saved=dest.name)

        text = text_field()
        if text:
            dest = new_file(incoming_dir, "text.txt")
            dest.write_text(text, encoding="utf-8")
        elif request.get_data(cache=True):
            # Not UTF-8 (a Latin-1 .txt, or a mislabeled file): keep the exact
            # bytes rather than refuse it or decode it lossily.
            dest = new_file(incoming_dir, "text.txt")
            dest.write_bytes(request.get_data(cache=True))
        else:
            return jsonify(error="no file or text in request"), 400
        return jsonify(saved=dest.name)

    @app.route("/list", methods=["GET"])
    def list_outgoing():
        entries = []
        for path in sorted(outgoing_dir.iterdir()):
            if path.is_file():
                stat = path.stat()
                entries.append({"name": path.name, "size": stat.st_size, "mtime": stat.st_mtime})
        return jsonify(entries)

    @app.route("/files/<path:name>", methods=["GET"])
    def get_file(name):
        return send_from_directory(outgoing_dir, name)

    def run_clipboard(cmd, env, **kwargs):
        if sys.platform == "win32":
            # Under pythonw there's no console, so PowerShell would pop a window each call.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(cmd, env=env, timeout=CLIPBOARD_TIMEOUT, encoding="utf-8", **kwargs)
        except subprocess.TimeoutExpired:
            # Seen on GNOME/Wayland: wl-clipboard blocks while the screen is locked.
            log.warning("clipboard command %s timed out", cmd[0])
            abort(503, "clipboard tool timed out (is the screen locked?)")
        except OSError as e:
            log.warning("clipboard command %s failed: %s", cmd[0], e)
            return None
        return result

    @app.route("/clipboard", methods=["GET"])
    def get_clipboard():
        cmds = clipboard and _clipboard_cmds()
        if not cmds:
            return jsonify(error="clipboard unavailable (disabled, or no clipboard tool installed)"), 501
        _, paste, env = cmds
        result = run_clipboard(paste, env, capture_output=True)
        if result is None or result.returncode != 0:
            return jsonify(error="reading the clipboard failed"), 500
        text = result.stdout.lstrip("﻿")
        if sys.platform == "win32" and text.endswith("\n"):
            # PowerShell appends a newline to its output (text mode has already
            # turned its \r\n into \n).
            text = text[:-1]
        return jsonify(text=text)

    @app.route("/clipboard", methods=["POST"])
    def set_clipboard():
        cmds = clipboard and _clipboard_cmds()
        if not cmds:
            return jsonify(error="clipboard unavailable (disabled, or no clipboard tool installed)"), 501
        text = text_field()
        if text is None:
            return jsonify(error="no text in request"), 400
        copy, _, env = cmds
        # stdout/stderr to DEVNULL: xclip and wl-copy fork a child that keeps
        # serving the selection, and a captured pipe would block until it exits.
        result = run_clipboard(copy, env, input=text, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result is None or result.returncode != 0:
            return jsonify(error="writing the clipboard failed"), 500
        return jsonify(ok=True)

    @app.route("/ui/login", methods=["GET"])
    def login_page():
        return send_from_directory(STATIC_DIR, "login.html")

    @app.route("/ui/login", methods=["POST"])
    def login():
        if not token_ok(request.form.get("token", "").strip()):
            return redirect("/ui/login?error=1")
        resp = redirect("/ui")
        resp.set_cookie(COOKIE_NAME, secret_token, max_age=365 * 24 * 3600, httponly=True, samesite="Strict")
        return resp

    @app.route("/ui/logout", methods=["POST"])
    def logout():
        resp = redirect("/ui/login")
        resp.delete_cookie(COOKIE_NAME)
        return resp

    @app.route("/ui", methods=["GET"])
    def ui():
        return send_from_directory(STATIC_DIR, "index.html")

    def box_dir(box):
        if box not in boxes:
            abort(404)
        return boxes[box]

    def box_file(box, name):
        path = safe_join(str(box_dir(box)), name)
        if path is None or not Path(path).is_file():
            abort(404)
        return Path(path)

    def text_preview(path):
        if path.suffix.lower() != ".txt":
            return None
        with path.open("rb") as f:
            head = f.read(TEXT_PREVIEW_BYTES)
        return head.decode("utf-8", errors="replace").strip().split("\n", 1)[0]

    @app.route("/api/<box>", methods=["GET"])
    def api_list(box):
        entries = []
        for path in box_dir(box).iterdir():
            if path.is_file():
                stat = path.stat()
                entries.append(
                    {"name": path.name, "size": stat.st_size, "mtime": stat.st_mtime, "preview": text_preview(path)}
                )
        entries.sort(key=lambda e: e["mtime"], reverse=True)
        return jsonify(entries)

    @app.route("/api/<box>", methods=["POST"])
    def api_upload(box):
        dest_dir = box_dir(box)
        files = request.files.getlist("file")
        if not files:
            return jsonify(error="no file in request"), 400
        saved = []
        for upload_file in files:
            name = safe_filename(upload_file.filename)
            dest = new_file(dest_dir, name, stamp=False)  # a clash becomes name-1.ext
            upload_file.save(dest)
            saved.append(dest.name)
        return jsonify(saved=saved)

    @app.route("/api/<box>/<path:name>", methods=["GET"])
    def api_download(box, name):
        path = box_file(box, name)
        # ?inline=1 is for thumbnails, and only for raster images; everything
        # else (and plain links) downloads as an attachment.
        inline = request.args.get("inline") and mimetypes.guess_type(path.name)[0] in INLINE_MIMETYPES
        return send_from_directory(box_dir(box), name, as_attachment=not inline)

    @app.route("/api/<box>/<path:name>", methods=["DELETE"])
    def api_delete(box, name):
        box_file(box, name).unlink()
        return jsonify(deleted=name)

    return app


def _lan_addresses():
    """Best-effort guess at the addresses a phone on the LAN could use."""
    names = [f"{socket.gethostname().split('.')[0]}.local"]
    try:
        # No packet is sent; this just asks the OS which interface routes outward.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))
            names.append(s.getsockname()[0])
    except OSError:
        pass
    return names


def main():
    parser = argparse.ArgumentParser(description="landrop: a LAN drop box for files and clipboard text.")
    parser.add_argument("--host", default=os.environ.get("LANDROP_HOST", "0.0.0.0"),
                        help="address to bind (default: 0.0.0.0, all interfaces)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("LANDROP_PORT", DEFAULT_PORT)),
                        help=f"port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--data-dir", default=os.environ.get("LANDROP_DATA_DIR", str(BASE_DIR)),
                        help="where incoming/, outgoing/ and landrop.token live (default: next to server.py)")
    parser.add_argument("--max-upload-mb", type=int, default=DEFAULT_MAX_UPLOAD_MB,
                        help=f"reject uploads larger than this (default: {DEFAULT_MAX_UPLOAD_MB})")
    parser.add_argument("--no-clipboard", action="store_true",
                        help="disable the /clipboard endpoints")
    parser.add_argument("--log-file", help="append logs here instead of stderr (useful on Windows)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        filename=args.log_file,
    )

    from waitress import serve

    app = create_app(args.data_dir, args.max_upload_mb, clipboard=not args.no_clipboard)
    data_dir = Path(args.data_dir).resolve()
    log.info("landrop serving %s on %s:%d", data_dir, args.host, args.port)
    for addr in _lan_addresses():
        log.info("  web UI: http://%s:%d/ui", addr, args.port)
    log.info("  token:  %s", data_dir / "landrop.token")
    serve(
        app,
        host=args.host,
        port=args.port,
        max_request_body_size=args.max_upload_mb * 1024 * 1024,
        ident="landrop",
    )


if __name__ == "__main__":
    main()
