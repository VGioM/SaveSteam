#!/usr/bin/env python3
"""Native app window for SaveSteam - embeds the local web UI in a real
GTK+WebKit window (system libraries, no separate browser process, no
Chromium/Firefox dependency). Connects to the already-running server; does
not start one itself, so background auto-backup keeps running independently
of whether this window is open.

Tries the modern GTK4 + WebKitGTK 6.0 stack first, falls back to the older
GTK3 + WebKit2GTK 4.1 stack, and if neither is installed on this system,
falls back to open_app_window.sh (a Chromium/Chrome app-window, or a plain
browser tab as a last resort) so this never leaves the user stranded."""
import os
import subprocess
import sys
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.environ.get('SAVETEAM_PORT', '8765')
URL = f'http://127.0.0.1:{PORT}/'
ICON_PATH = os.path.join(HERE, 'static', 'icon.png')


def _send_window_closed():
    """Tells the server the app window closed, so it can honor the user's
    'run in background' Settings toggle - shut itself down if it's off, or
    do nothing (stay running for auto-backup) if it's on. Best-effort: if
    the server's unreachable there's nothing to shut down anyway."""
    try:
        req = urllib.request.Request(URL + 'api/window-closed', data=b'{}', method='POST',
                                      headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=3)
    except OSError:
        pass


def notify_window_closed():
    """Fire-and-forget variant for GTK close/destroy signal handlers - runs
    in a background thread so the notification's network call never makes
    the window feel like it's hanging on close."""
    threading.Thread(target=_send_window_closed, daemon=True).start()


def try_gtk4_webkit6():
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('WebKit', '6.0')
    from gi.repository import Gtk, WebKit

    app = Gtk.Application(application_id='io.saveteam.SaveSteam')

    def on_activate(a):
        win = Gtk.ApplicationWindow(application=a, title='SaveSteam')
        win.set_default_size(1400, 900)
        webview = WebKit.WebView()
        webview.load_uri(URL)
        win.set_child(webview)

        def on_close_request(_win):
            notify_window_closed()
            return False  # allow the window to close normally either way

        win.connect('close-request', on_close_request)
        win.present()

    app.connect('activate', on_activate)
    return app.run([sys.argv[0]]) == 0


def try_gtk3_webkit2():
    import gi
    gi.require_version('Gtk', '3.0')
    gi.require_version('WebKit2', '4.1')
    from gi.repository import Gtk, WebKit2

    win = Gtk.Window(title='SaveSteam')
    win.set_default_size(1400, 900)
    if os.path.isfile(ICON_PATH):
        try:
            win.set_icon_from_file(ICON_PATH)
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
    def on_destroy(_win):
        notify_window_closed()
        Gtk.main_quit()

    win.connect('destroy', on_destroy)
    webview = WebKit2.WebView()
    webview.load_uri(URL)
    win.add(webview)
    win.show_all()
    Gtk.main()
    return True


def fallback_browser():
    """Used only when neither GTK+WebKit stack is available. Runs the
    browser app-window helper in the FOREGROUND - it blocks until the
    Chromium/Chrome app-mode window is closed - so the 'run in background'
    setting still takes effect here, not just in the native-window path.
    (The very last resort inside that script, a plain xdg-open tab, can't
    signal 'closed' reliably, so the setting has no effect in that case.)"""
    profile_dir = os.path.join(os.environ.get('SAVETEAM_CONFIG_DIR', HERE), 'chromium-profile')
    subprocess.run([os.path.join(HERE, 'open_app_window.sh'), URL, profile_dir])
    _send_window_closed()


def main():
    for attempt in (try_gtk4_webkit6, try_gtk3_webkit2):
        try:
            attempt()
            return
        except Exception as e:  # noqa: BLE001 - any missing lib/binding falls through
            sys.stderr.write(f'{attempt.__name__} unavailable: {e}\n')
    fallback_browser()


if __name__ == '__main__':
    main()
