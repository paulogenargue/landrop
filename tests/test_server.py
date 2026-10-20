import io
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4  # not valid UTF-8


@pytest.fixture
def app(tmp_path):
    return server.create_app(tmp_path, max_upload_mb=1)


@pytest.fixture
def token(tmp_path, app):
    return (tmp_path / "landrop.token").read_text().strip()


@pytest.fixture
def client(app, token):
    c = app.test_client()
    c.environ_base["HTTP_X_LANDROP_TOKEN"] = token
    return c


@pytest.fixture
def anon(app):
    return app.test_client()


def incoming(tmp_path):
    return sorted((tmp_path / "incoming").iterdir())


def test_token_generated_once_and_private(tmp_path, app):
    tok = tmp_path / "landrop.token"
    first = tok.read_text()
    server.create_app(tmp_path)
    assert tok.read_text() == first
    assert len(first.strip()) == 64
    if os.name == "posix":
        assert stat.S_IMODE(tok.stat().st_mode) == 0o600


@pytest.mark.parametrize("path", ["/list", "/files/x", "/clipboard", "/api/incoming"])
def test_requires_token(anon, path):
    assert anon.get(path).status_code == 401
    assert anon.get(path, headers={"X-Landrop-Token": "wrong"}).status_code == 401


def test_ui_redirects_to_login(anon):
    r = anon.get("/ui")
    assert r.status_code == 302 and r.headers["Location"].endswith("/ui/login")
    assert anon.get("/ui/login").status_code == 200


def test_raw_image_upload_is_byte_exact(client, tmp_path):
    # The iOS Shortcut's "Request Body: File" mode: raw bytes, real Content-Type.
    r = client.post("/upload", data=JPEG, content_type="image/jpeg")
    assert r.status_code == 200
    [saved] = incoming(tmp_path)
    assert saved.suffix == ".jpg" and saved.read_bytes() == JPEG


def test_raw_text_upload(client, tmp_path):
    r = client.post("/upload", data="héllo ✓".encode(), content_type="text/plain; charset=utf-8")
    assert r.status_code == 200
    [saved] = incoming(tmp_path)
    assert saved.read_text(encoding="utf-8") == "héllo ✓"


def test_non_utf8_text_kept_byte_exact(client, tmp_path):
    # Never decode lossily (that once destroyed a real photo); keep the bytes.
    for body in (JPEG, "café".encode("latin-1")):
        assert client.post("/upload", data=body, content_type="text/plain").status_code == 200
    assert sorted(p.read_bytes() for p in incoming(tmp_path)) == sorted([JPEG, "café".encode("latin-1")])


def test_multipart_upload_sanitizes_name(client, tmp_path):
    r = client.post("/upload", data={"file": (io.BytesIO(b"x"), "../../evil.txt")})
    assert r.status_code == 200
    [saved] = incoming(tmp_path)
    assert saved.name.endswith("-evil.txt")


def test_multipart_text_field(client, tmp_path):
    assert client.post("/upload", data={"text": "note"}).status_code == 200
    assert incoming(tmp_path)[0].read_text(encoding="utf-8") == "note"


def test_uploads_in_same_second_dont_collide(client, tmp_path):
    for _ in range(5):
        client.post("/upload", data=JPEG, content_type="image/jpeg")
    assert len(incoming(tmp_path)) == 5


def test_upload_size_limit(client, tmp_path):
    r = client.post("/upload", data=b"\0" * (1024 * 1024 + 1), content_type="application/octet-stream")
    assert r.status_code == 413
    assert incoming(tmp_path) == []


def test_empty_upload_rejected(client, tmp_path):
    assert client.post("/upload", data=b"", content_type="image/jpeg").status_code == 400
    assert client.post("/upload").status_code == 400
    assert incoming(tmp_path) == []


def test_list_and_download_outgoing(client, tmp_path):
    (tmp_path / "outgoing" / "a.txt").write_bytes(b"hi")
    [entry] = client.get("/list").get_json()
    assert entry["name"] == "a.txt" and entry["size"] == 2
    assert client.get("/files/a.txt").data == b"hi"


@pytest.mark.parametrize("path", ["/files/../landrop.token", "/files/..%2flandrop.token",
                                  "/api/outgoing/../landrop.token", "/api/incoming/..%2f..%2fserver.py",
                                  "/api/nope/x"])
def test_no_path_traversal(client, path):
    r = client.get(path)
    assert r.status_code == 404
    assert b"landrop" not in r.data or b"error" in r.data


def test_inline_only_for_raster_images(client, tmp_path):
    (tmp_path / "incoming" / "p.jpg").write_bytes(JPEG)
    (tmp_path / "incoming" / "x.html").write_text("<script>alert(1)</script>")
    (tmp_path / "incoming" / "x.svg").write_text("<svg onload=alert(1)>")
    r = client.get("/api/incoming/p.jpg?inline=1")
    assert "attachment" not in r.headers.get("Content-Disposition", "")
    for name in ("x.html", "x.svg"):
        r = client.get(f"/api/incoming/{name}?inline=1")
        assert r.headers["Content-Disposition"].startswith("attachment")
        assert "sandbox" in r.headers["Content-Security-Policy"]
        assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_cookie_login_and_csrf_guard(anon, token, tmp_path):
    assert anon.post("/ui/login", data={"token": "nope"}).headers["Location"].endswith("error=1")
    r = anon.post("/ui/login", data={"token": token})
    assert "HttpOnly" in r.headers["Set-Cookie"] and "SameSite=Strict" in r.headers["Set-Cookie"]
    assert anon.get("/api/outgoing").status_code == 200
    (tmp_path / "outgoing" / "f").write_bytes(b"x")
    # A cookie-authenticated write from another origin (e.g. another port) is refused...
    assert anon.delete("/api/outgoing/f", headers={"Origin": "http://localhost:3000"}).status_code == 403
    assert anon.delete("/api/outgoing/f").status_code == 403
    # ...but from the UI's own origin it goes through.
    assert anon.delete("/api/outgoing/f", headers={"Origin": "http://localhost"}).status_code == 200


def test_logout_clears_cookie(anon, token):
    anon.post("/ui/login", data={"token": token})
    anon.post("/ui/logout", headers={"Origin": "http://localhost"})
    assert anon.get("/api/outgoing").status_code == 401


def test_api_upload_and_delete(client, tmp_path):
    r = client.post("/api/outgoing", data={"file": [(io.BytesIO(b"1"), "a.txt"), (io.BytesIO(b"2"), "a.txt")]})
    assert len(set(r.get_json()["saved"])) == 2
    for name in r.get_json()["saved"]:
        assert client.delete(f"/api/outgoing/{name}").status_code == 200
    assert client.get("/api/outgoing").get_json() == []


def test_clipboard_disabled(tmp_path, token):
    c = server.create_app(tmp_path, clipboard=False).test_client()
    c.environ_base["HTTP_X_LANDROP_TOKEN"] = token
    assert c.get("/clipboard").status_code == 501
    assert c.post("/clipboard", data="x", content_type="text/plain").status_code == 501


def test_clipboard_roundtrip_with_fake_tool(client, monkeypatch, tmp_path):
    store = tmp_path / "clip"
    py = sys.executable
    copy = [py, "-c", f"import sys; sys.stdin.reconfigure(encoding='utf-8'); "
                      f"open({str(store)!r}, 'w', encoding='utf-8').write(sys.stdin.read())"]
    paste = [py, "-c", f"import sys; sys.stdout.reconfigure(encoding='utf-8'); "
                       f"sys.stdout.write(open({str(store)!r}, encoding='utf-8').read())"]
    monkeypatch.setattr(server, "_clipboard_cmds", lambda: (copy, paste, None))
    monkeypatch.setattr(server.sys, "platform", "linux")  # skip the PowerShell newline strip
    assert client.post("/clipboard", data="clip ✓".encode(), content_type="text/plain").status_code == 200
    assert client.get("/clipboard").get_json() == {"text": "clip ✓"}


def test_clipboard_tool_failure_is_reported(client, monkeypatch):
    monkeypatch.setattr(server, "_clipboard_cmds", lambda: (["/nonexistent-tool"], ["/nonexistent-tool"], None))
    assert client.get("/clipboard").status_code == 500
    assert client.post("/clipboard", data="x", content_type="text/plain").status_code == 500


@pytest.mark.skipif(not os.environ.get("LANDROP_TEST_REAL_CLIPBOARD"), reason="touches the real clipboard")
def test_real_clipboard_roundtrip(client):
    assert client.post("/clipboard", data="landrop ✓ test".encode(), content_type="text/plain").status_code == 200
    assert client.get("/clipboard").get_json() == {"text": "landrop ✓ test"}


@pytest.mark.parametrize("raw, expected", [
    ("日本語.txt", "日本語.txt"),
    ("../../etc/passwd", "passwd"),
    ("..\\..\\boot.ini", "boot.ini"),
    ("CON.txt", "_CON.txt"),
    ("my photo #1.jpg", "my_photo__1.jpg"),
    ("a‮gpj.exe", "agpj.exe"),
    ("..", "upload"),
    ("", "upload"),
])
def test_safe_filename(raw, expected):
    assert server.safe_filename(raw) == expected


def test_safe_filename_keeps_extension_within_byte_limit():
    name = server.safe_filename("é" * 300 + ".jpg")
    assert name.endswith(".jpg") and len(name.encode()) <= 201


def test_unicode_filename_upload(client, tmp_path):
    r = client.post("/api/outgoing", data={"file": (io.BytesIO(b"x"), "写真.jpg")})
    assert r.get_json()["saved"] == ["写真.jpg"]
    assert [e["name"] for e in client.get("/list").get_json()] == ["写真.jpg"]
    assert client.get("/files/%E5%86%99%E7%9C%9F.jpg").data == b"x"


def test_clipboard_accepts_json_and_form(client, monkeypatch):
    got = []
    monkeypatch.setattr(server, "_clipboard_cmds", lambda: (["copy"], ["paste"], None))
    monkeypatch.setattr(server.subprocess, "run",
                        lambda cmd, input=None, **kw: got.append(input) or server.subprocess.CompletedProcess(cmd, 0))
    assert client.post("/clipboard", json={"text": "from json"}).status_code == 200
    assert client.post("/clipboard", data={"text": "from form"}).status_code == 200
    assert client.post("/clipboard", json={"nope": 1}).status_code == 400
    assert got == ["from json", "from form"]


def test_clipboard_text_size_cap(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_TEXT_BYTES", 10)
    monkeypatch.setattr(server, "_clipboard_cmds", lambda: (["copy"], ["paste"], None))
    assert client.post("/clipboard", data="x" * 11, content_type="text/plain").status_code == 413


def test_clipboard_timeout_is_503(client, monkeypatch):
    def hang(cmd, **kw):
        raise server.subprocess.TimeoutExpired(cmd, 5)
    monkeypatch.setattr(server, "_clipboard_cmds", lambda: (["copy"], ["paste"], None))
    monkeypatch.setattr(server.subprocess, "run", hang)
    r = client.get("/clipboard")
    assert r.status_code == 503 and "locked" in r.get_json()["error"]


def test_text_typed_files_kept_as_files(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAX_TEXT_BYTES", 10)
    client.post("/upload", data=b"BEGIN:VCARD", content_type="text/vcard")
    client.post("/upload", data=b"a,b\n" * 10, content_type="text/csv")
    client.post("/upload", data=b"y" * 20, content_type="text/plain")  # big text/plain: streamed, not 413
    names = sorted(p.name.rsplit("-", 1)[-1] for p in incoming(tmp_path))
    assert names == ["text.txt", "upload.csv", "upload.vcf"]


def test_long_fake_extension_does_not_500(client):
    r = client.post("/api/outgoing", data={"file": (io.BytesIO(b"x"), "a." + "e" * 300)})
    assert r.status_code == 200 and len(r.get_json()["saved"][0].encode()) <= 200


@pytest.mark.parametrize("raw", ["COM¹.txt", "LPT²", "CONIN$", "conout$.log"])
def test_more_windows_reserved_names(raw):
    assert server.safe_filename(raw).startswith("_")
