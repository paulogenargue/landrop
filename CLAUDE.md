# landrop

A small self-hosted drop box for moving photos, files and text between an
iPhone (via the built-in Shortcuts app) and a computer on the same network.
On demand, not continuous sync. See `README.md` for setup and usage.

## Layout

- `server.py` is the whole server: a Flask app built by `create_app(data_dir)`,
  served by waitress from `main()`. No module-level side effects, so tests
  build a fresh app per temp dir.
- `static/` has the browser UI (vanilla JS, no build step).
- `autostart/` has the per-OS login-autostart files (systemd user unit,
  launchd agent, Windows Startup-folder script).
- `tests/` is pytest. Run `.venv/bin/python -m pytest tests`. CI runs it on
  Linux, macOS and Windows.

## Things that look odd but are deliberate

- **`/upload` branches on Content-Type, not on what the body looks like.** The
  iOS Shortcut's "Request Body: File" mode posts raw bytes with the item's
  real type (e.g. `image/jpeg`), not multipart. Non-text types are streamed
  to disk byte-for-byte. Text that fails to decode as UTF-8 is saved as its
  raw bytes, never lossily decoded (that once destroyed a real photo).
- **`request.get_data(cache=True)` before touching `request.form`.** Reading
  the form first consumes the stream and the raw-body fallback silently
  returns empty.
- **Shortcut routes (`/upload`, `/list`, `/files/<name>`, `/clipboard`) are a
  stable API.** People's phones have them baked into shortcuts they built by
  hand. The browser UI uses separate `/api/<box>` routes so it can change
  freely.
- **Two auth paths.** Shortcuts send `X-Landrop-Token`. Browsers can't attach
  headers to `<img>`/links, so `/ui/login` sets an `HttpOnly`,
  `SameSite=Strict` cookie. Cookie-authenticated writes must also carry a
  same-origin `Origin` header, because SameSite treats other ports on the same
  host as same-site.
- **File responses get `Content-Security-Policy: sandbox`, and only raster
  images are ever served inline.** Uploaded HTML/SVG would otherwise run
  script on the UI's origin.
- **Uploads never overwrite.** Names get a microsecond timestamp, but that
  isn't unique on its own (Windows clocks can be ~16 ms coarse), so
  `new_file()` creates each file with `O_EXCL` and adds a `-N` suffix on a
  clash.
- **Clipboard tools are per OS** (`pbcopy`/`pbpaste`, PowerShell, then
  `wl-copy`, `xclip`, `xsel`). Each has an encoding or blocking quirk,
  documented inline in `_clipboard_cmds` and `set_clipboard`.

## Conventions

- Keep it dependency-light: Flask and waitress only, no new runtime deps
  without a strong reason.
- Plain HTTP on the LAN is a known trade-off, documented in the README's
  Security section. Don't add TLS handling to the server itself.
- Private, machine-specific notes go in `CLAUDE.local.md` (gitignored).
