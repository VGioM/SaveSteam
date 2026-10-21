# SaveSteam

Local web app to browse Steam/Proton save prefixes across drives and back up
chosen folders into `SaveSteam/<game>/<timestamp>/`.

Built for Bazzite/SteamOS-style setups, where game saves end up buried in
`steamapps/compatdata/<appid>/pfx/drive_c/users/steamuser/...` and the numeric
appid tells you nothing about which game it belongs to. SaveSteam resolves
those ids to real game names (Steam's own libraries + the Steam store API for
cover art), lists them in one place, and snapshots the folders you pick onto
an external drive.

## Features

- Scans every Steam library / drive for Proton prefixes and non-Steam game
  folders, and resolves appids to game names and cover images.
- Manual and automatic backups (`SaveSteam/<game>/<timestamp>/`), with a
  rolling number of snapshots kept per game.
- Restore a snapshot back into the live prefix.
- Custom sources, for games whose saves live outside compatdata (Battle.net,
  Ubisoft Connect, standalone launchers).
- Desktop notification when the backup drive is running low on space.
- Native app window (GTK4/WebKitGTK 6, falling back to GTK3/WebKit2GTK 4.1,
  then to a Chromium app window) over the same local server, so auto-backup
  keeps running in the background whether the window is open or not.

Python standard library only - no pip dependencies. The server binds to
`127.0.0.1`, so it is never reachable from the network.

## Install

```bash
bash install.sh
```

This copies the app into `~/.local/share/saveteam`, enables a
`systemd --user` service (so it starts at login) and adds a "SaveSteam" entry
to the app menu. Then open <http://127.0.0.1:8765/> or launch SaveSteam from
the app menu.

Backups are not touched by installing or reinstalling: they live under
`backup_root` (set in the Settings tab, typically an external drive), so they
survive an OS reinstall on their own.

## Layout

| File | Role |
| --- | --- |
| `server.py` | HTTP server, REST API, backup/restore, auto-backup loop |
| `scanner.py` | Drive/library/prefix discovery, appid → game name resolution |
| `vdf.py` | Minimal Valve VDF parser (reads Steam's own library files) |
| `native_app.py` | Native GTK+WebKit window around the local UI |
| `open_app_window.sh` | Browser-based fallback window |
| `static/` | Front-end (`index.html`, `app.js`, `app.css`, icon) |
| `install.sh` | Installer: app dir + systemd user service + app-menu entry |

`config.json` holds machine-specific settings (paths, backup root, intervals)
and is intentionally not tracked; `server.py` writes one with sane defaults on
first run. `SAVETEAM_CONFIG_DIR` can point it elsewhere, for read-only installs.
