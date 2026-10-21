#!/bin/bash
# Opens $1 as a chrome-less "app" window (no tabs/address bar) using
# whichever Chromium/Chrome variant is installed - native package or
# flatpak - so SaveSteam feels like a standalone app instead of a browser
# tab. Runs it in the FOREGROUND (blocks until the window is closed) so a
# caller (native_app.py's fallback path) can tell when the window closed
# and act on it - e.g. the "run in background" setting. Falls back to
# xdg-open (a normal tab in the default browser, backgrounded - there's no
# reliable "closed" signal from that path) if no such browser is found, so
# this never breaks on a machine without one.
#
# Usage: open_app_window.sh URL [PROFILE_DIR]
# PROFILE_DIR (optional) keeps SaveSteam's app window in its own browser
# profile, separate from the user's regular browsing profile/session.
set -u

url="${1:?url required}"
profile_dir="${2:-}"

extra_args=()
if [ -n "$profile_dir" ]; then
    mkdir -p "$profile_dir"
    extra_args+=("--user-data-dir=$profile_dir")
fi

try_native() {
    command -v "$1" >/dev/null 2>&1 || return 1
    exec "$1" "${extra_args[@]}" "--app=$url" --window-size=1400,900 --class=SaveSteam >/dev/null 2>&1
}

try_flatpak() {
    command -v flatpak >/dev/null 2>&1 || return 1
    flatpak info "$1" >/dev/null 2>&1 || return 1
    exec flatpak run "$1" "${extra_args[@]}" "--app=$url" --window-size=1400,900 --class=SaveSteam >/dev/null 2>&1
}

for bin in google-chrome google-chrome-stable chromium chromium-browser; do
    try_native "$bin"
done
for app_id in org.chromium.Chromium com.google.Chrome; do
    try_flatpak "$app_id"
done

# no Chromium/Chrome found anywhere - fall back to a normal browser tab
xdg-open "$url" >/dev/null 2>&1 &
disown
exit 0
