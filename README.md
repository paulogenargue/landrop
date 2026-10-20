# landrop

A tiny self-hosted drop box for sending photos, files and text between your iPhone and your computer over the local network. It's on demand ("send this photo now"), not continuous sync like Syncthing.

There's no phone app. The iPhone side is two Shortcuts you build by hand in the built-in Shortcuts app. The server is one Python file (Flask + waitress), plus a small browser UI.

I wanted something small with no third-party app to trust, and something I can drive from a terminal.

## Features

- Share Sheet upload from the iPhone into `incoming/`
- Pick a file from `outgoing/` on the phone
- Clipboard read and write endpoints
- Browser UI at `/ui`: drag and drop, Ctrl+V paste into outgoing, keyboard-driven, bulk delete
- Shared-secret auth on everything

## Requirements

- Python 3.10 or newer
- An iPhone with the Shortcuts app, on the same network as the computer
- Clipboard on Linux needs `wl-clipboard` (Wayland) or `xclip`/`xsel` (X11). macOS and Windows have what they need built in. The clipboard only works while you're logged in to the desktop. On GNOME it also refuses while the screen is locked, and landrop answers 503.

## Install

```
git clone https://github.com/paulogenargue/landrop.git
cd landrop
```

Linux and macOS:

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
```

On Ubuntu/Debian you may need `sudo apt install python3-venv` (or `python3.X-venv`) first.

Windows (PowerShell):

```
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python server.py
```

The first run creates `landrop.token` (the shared secret) and prints the UI URLs, like `http://yourcomputer.local:8787/ui` and one with your LAN IP.

Read the token with:

```
cat landrop.token             # Linux / macOS
Get-Content landrop.token     # Windows
```

## Options

| Flag | Env var | Default | What it does |
| --- | --- | --- | --- |
| `--host` | `LANDROP_HOST` | `0.0.0.0` | Address to bind |
| `--port` | `LANDROP_PORT` | `8787` | Port to listen on |
| `--data-dir` | `LANDROP_DATA_DIR` | next to `server.py` | Where `incoming/`, `outgoing/` and `landrop.token` live |
| `--max-upload-mb` | | `2048` | Reject uploads bigger than this |
| `--no-clipboard` | | off | Disable the `/clipboard` endpoints |
| `--log-file` | | stderr | Append logs to a file (handy on Windows) |

## Reaching it from the phone

The phone talks to `http://yourcomputer.local:8787`, so the computer needs to answer `.local` names and let the port through its firewall.

**macOS** advertises `.local` out of the box. It may ask whether to accept incoming connections the first time; say yes.

**Linux** needs `avahi-daemon`, which most desktops already have. If you have Docker or a VPN installed, Avahi may advertise the wrong interface's IP. To fix it, add `allow-interfaces=<your wifi interface>` under `[server]` in `/etc/avahi/avahi-daemon.conf`, then restart `avahi-daemon`. If you use ufw, open the port for your LAN (adjust the subnet):

```
sudo ufw allow from 192.168.0.0/16 to any port 8787 proto tcp
```

**Windows** 10/11 usually answers `.local` too. If it doesn't, use the PC's IP address and give the PC a DHCP reservation in your router so the IP doesn't change. On first run Windows shows an "allow access" prompt. Allow it on Private networks only, and make sure your Wi-Fi profile is set to Private.

Sanity check from the computer. It should return `[]`:

```
curl -H "X-Landrop-Token: $(cat landrop.token)" http://yourcomputer.local:8787/list
```

## Start at login

All of these start landrop at login, not at boot. Run the commands from the repo root.

**Linux (systemd user unit)**

```
mkdir -p ~/.config/systemd/user
sed "s|@LANDROP_DIR@|$PWD|g" autostart/landrop.service > ~/.config/systemd/user/landrop.service
systemctl --user daemon-reload && systemctl --user enable --now landrop.service
```

Check on it with `systemctl --user status landrop` and `journalctl --user -u landrop -f`. The unit starts with your desktop session so the clipboard can reach the display. This needs a desktop with systemd session integration (GNOME, KDE and similar). If `systemctl --user is-active graphical-session.target` says `inactive` (i3, plain sway, headless), follow the comment at the top of `autostart/landrop.service` to switch it to `default.target`. Otherwise it won't come back after a reboot.

**macOS (launchd agent)**

```
sed "s|@LANDROP_DIR@|$PWD|g" autostart/local.landrop.plist > ~/Library/LaunchAgents/local.landrop.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.landrop.plist
```

Logs go to `landrop.log` in the repo. Stop it with `launchctl bootout gui/$(id -u)/local.landrop`.

Don't keep the repo in `~/Documents`, `~/Desktop` or `~/Downloads`. macOS privacy protection blocks background agents from reading those folders.

**Windows (Startup-folder shortcut)**

```
powershell -ExecutionPolicy Bypass -File autostart\install-windows.ps1
```

Stop any landrop you started by hand first (Ctrl+C), because both can't use the port. This drops a shortcut running `pythonw` into your Startup folder and starts it right away. No admin needed. Windows Firewall asks once for `pythonw.exe` at that point. It's a different program from the `python.exe` you ran by hand, so you get asked again. Allow Private networks only. Logs go to `landrop.log`. To remove it, delete `landrop.lnk` from the Startup folder.

## iPhone Shortcuts

Both Shortcuts need the token as a header (`X-Landrop-Token`) on every request. There's no way to reference the file from the phone, so you paste it in once when building each Shortcut. Replace `yourcomputer.local` with your computer's name or IP in the URLs below.

### Send to landrop

Share Sheet target, for sending photos and files to `incoming/`.

1. Create a new Shortcut and add **Receive** input **from Share Sheet**. Pick the types you want to share (Images, Files, Text, ...).
2. Add **Get Contents of URL**:
   - URL: `http://yourcomputer.local:8787/upload`
   - Method: `POST`
   - Headers: `X-Landrop-Token` = your token
   - Request Body: **File**
   - File: **Shortcut Input**

"File" body mode matters. It sends the raw bytes with the item's real content type (like `image/jpeg`) instead of wrapping it in multipart, and the server relies on that to tell a file from a text share.

### Get from landrop

Picks a file from `outgoing/` and opens it on the phone.

1. **Get Contents of URL**: `GET http://yourcomputer.local:8787/list`, header `X-Landrop-Token`. Shortcuts parses the JSON for you.
2. **Repeat with Each** on the result. Inside it, add **Get Dictionary Value** for key `name` from **Repeat Item**.
3. **Choose from List** with **Repeat Results** as the list.
4. **URL Encode** the **Chosen Item**, so names with spaces or `#` still work.
5. **Get Contents of URL**: `GET http://yourcomputer.local:8787/files/` followed by **URL Encoded Text**, header `X-Landrop-Token`.
6. **Quick Look** on the previous result.

Quick Look only previews the file. To keep it, tap the share button in the preview and choose Save Image or Save to Files.

### Clipboard (optional)

- Computer to phone: `GET /clipboard`, then **Get Dictionary Value** for `text` and **Copy to Clipboard**.
- Phone to computer: **Get Clipboard**, then `POST /clipboard` with **Request Body: Form** and a field `text` set to the clipboard. JSON `{"text": ...}` and a raw text body work too.

## Browser UI

Open `http://yourcomputer.local:8787/ui` and paste the token once. The login cookie lasts a year, and there's a Log out button.

| Key | Action |
| --- | --- |
| `r` | Refresh |
| `u` | Upload |
| `Ctrl+V` | Paste a file, image or text into outgoing |
| `Up` / `Down` (or `k` / `j`) | Move between rows |
| `Space` | Select |
| `a` | Select all |
| `Esc` | Clear selection |
| `Enter` | Download |
| `Del` | Delete the selection, or the focused row |

## HTTP API

Every route needs the `X-Landrop-Token` header.

| Route | What it does |
| --- | --- |
| `POST /upload` | Save a file or text into `incoming/` |
| `GET /list` | JSON list of `outgoing/` |
| `GET /files/<name>` | Download a file from `outgoing/` |
| `GET /clipboard` | Read the computer's clipboard as `{"text": ...}` |
| `POST /clipboard` | Set the computer's clipboard from the request body |

These are meant to stay stable. The `/api/*` routes are for the browser UI and may change.

## Security

- The token is 256 bits of randomness, compared in constant time, and required on every route. `landrop.token` is created owner-only (on Windows it relies on your user-profile permissions).
- It's **plain HTTP**. The token and your files travel unencrypted on your LAN, so only use it on networks you trust, like home Wi-Fi and not a café or office. If you want encryption, reach it over a VPN like Tailscale or WireGuard. A TLS reverse proxy works for the Shortcuts, but the browser UI's same-origin check breaks (uploads and deletes get refused). A VPN is simpler.
- Anyone with the token can read your computer's clipboard and write files into `incoming/`. Treat it like a password.
- To rotate it, delete `landrop.token` and restart, then update your Shortcuts.
- Use `--no-clipboard` if you don't want the clipboard exposed at all.
- Uploaded files are served as downloads with a sandbox CSP, never rendered as pages. Filenames are sanitized (no paths, no Windows-reserved names), and non-ASCII letters are kept.
- Each upload is capped by `--max-upload-mb` and streamed to disk. Nothing caps the total, so a token holder could fill the disk.
- It's not meant to be exposed to the internet.

## Development

```
.venv/bin/pip install pytest            # Windows: .venv\Scripts\pip install pytest
.venv/bin/python -m pytest tests        # Windows: .venv\Scripts\python -m pytest tests
```

CI runs the tests on Linux, macOS and Windows.

## License

MIT
