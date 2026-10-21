#!/bin/bash
# Installs SaveSteam: copies the app into ~/.local/share/saveteam, registers a
# systemd --user service (starts at login) and an app-menu launcher.
#
# Your game backups are never touched - they live wherever backup_root points
# to (an external drive, typically), independently of this install.
#
# Usage: bash install.sh
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/saveteam"

echo "Installing SaveSteam into $APP_DIR ..."
mkdir -p "$APP_DIR"
for item in server.py scanner.py vdf.py native_app.py open_app_window.sh static; do
    cp -r "$SRC_DIR/$item" "$APP_DIR/"
done
chmod +x "$APP_DIR/open_app_window.sh" "$APP_DIR/native_app.py"

echo "Setting up the systemd user service ..."
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/saveteam.service" <<'EOF'
[Unit]
Description=SaveSteam - Steam/Proton save browser and backup tool
After=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 %h/.local/share/saveteam/server.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

echo "Setting up the app-menu launcher ..."
mkdir -p "$HOME/.local/share/applications"
# Note: this heredoc is intentionally UNQUOTED (<<EOF, not <<'EOF') so
# $APP_DIR expands to this machine's real absolute path below - Icon= in a
# .desktop file is a plain string with no shell/env-var expansion, so it
# must be baked in here rather than left as a literal $HOME reference.
cat > "$HOME/.local/share/applications/saveteam.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SaveSteam
Comment=Browse Steam/Proton save prefixes and back up game saves
Exec=sh -c 'systemctl --user start saveteam.service; sleep 1; python3 "$APP_DIR/native_app.py"'
Icon=$APP_DIR/static/icon.png
Terminal=false
Categories=Game;Utility;
EOF
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$HOME/.local/share/applications" || true

echo "Starting the service ..."
systemctl --user daemon-reload
systemctl --user enable --now saveteam.service

echo
echo "Done. SaveSteam is running at http://127.0.0.1:8765/"
