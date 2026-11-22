#!/usr/bin/env python3
"""SaveSteam: local web app to browse Steam/Proton save prefixes across
drives and back up chosen folders into SaveSteam/<game>/<timestamp>/.

Standard-library only. Binds to 127.0.0.1 so it's only reachable locally.
"""
import glob
import json
import mimetypes
import os
import re
import shutil
import stat as stat_mode
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, 'static')
# Normally config.json lives next to server.py (the systemd install). When
# running from a read-only package (e.g. the AppImage build, mounted/extracted
# read-only), SAVETEAM_CONFIG_DIR points it at a writable directory outside
# the package instead, so settings persist across runs without needing write
# access to the app's own install location.
CONFIG_PATH = os.path.join(os.environ.get('SAVETEAM_CONFIG_DIR', APP_DIR), 'config.json')
AUTO_BACKUP_INTERVAL_DEFAULT = 20 * 60 + 5  # 20 min 5 sec, offset from the 20-min UI refresh
AUTO_BACKUP_KEEP_DEFAULT = 5
SCAN_INTERVAL_DEFAULT = 20 * 60  # how often the UI re-scans/re-polls, independent of the backup cycle
MIN_BACKUP_INTERVAL = 60  # floor, so a fat-fingered setting can't hammer the disk every few seconds
MIN_SCAN_INTERVAL = 30


def _default_compat_groups():
    return [dict(g) for g in scanner.DEFAULT_COMPAT_GROUPS]


SYSTEMD_UNIT_PATH = os.path.expanduser('~/.config/systemd/user/saveteam.service')


def _systemd_available():
    return shutil.which('systemctl') is not None


def _autostart_exec_start():
    """Where the systemd unit should point: the AppImage itself (headless,
    no GUI window - see AppRun) if we're running from one (AppImage runtimes
    set $APPIMAGE to its own path), otherwise this server.py, matching what
    install.sh already sets up for a normal install."""
    appimage = os.environ.get('APPIMAGE')
    if appimage:
        return f'{appimage} --headless'
    return f'{sys.executable} {os.path.abspath(__file__)}'


def _systemd_unit_content():
    return (
        '[Unit]\n'
        'Description=SaveSteam - Steam/Proton save browser and backup tool\n'
        'After=graphical-session.target\n\n'
        '[Service]\n'
        f'ExecStart={_autostart_exec_start()}\n'
        'Restart=on-failure\n'
        'RestartSec=2\n\n'
        '[Install]\n'
        'WantedBy=default.target\n'
    )


def get_autostart_status():
    if not _systemd_available():
        return {'supported': False, 'enabled': False}
    try:
        result = subprocess.run(
            ['systemctl', '--user', 'is-enabled', 'saveteam.service'],
            capture_output=True, text=True, timeout=5,
        )
        enabled = result.stdout.strip() == 'enabled'
    except (OSError, subprocess.SubprocessError):
        enabled = False
    return {'supported': True, 'enabled': enabled}


def set_autostart(enabled):
    """Enable or disable the systemd --user unit that starts SaveSteam at
    login. Only touches whether it starts NEXT login - never starts or
    stops the currently-running instance, so there's no port conflict with
    whatever's running right now (window, AppImage, or the service itself)."""
    if not _systemd_available():
        raise OSError('systemd is not available on this system')
    if enabled:
        os.makedirs(os.path.dirname(SYSTEMD_UNIT_PATH), exist_ok=True)
        with open(SYSTEMD_UNIT_PATH, 'w', encoding='utf-8') as f:
            f.write(_systemd_unit_content())
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, timeout=10)
        subprocess.run(['systemctl', '--user', 'enable', 'saveteam.service'], check=True, timeout=10)
    else:
        subprocess.run(['systemctl', '--user', 'disable', 'saveteam.service'], check=False, timeout=10)
    return get_autostart_status()


DEFAULT_CONFIG = {
    'port': 8765,
    'custom_sources': [],
    'auto_backup_targets': [],
    'keep_snapshots': AUTO_BACKUP_KEEP_DEFAULT,
    'backup_interval_seconds': AUTO_BACKUP_INTERVAL_DEFAULT,
    'scan_interval_seconds': SCAN_INTERVAL_DEFAULT,
    'compat_groups': None,  # filled in lazily via _default_compat_groups() - see _load_config_nolock
    'backup_root': None,  # None means "use the default <DRIVE_ROOT>/SaveSteam" - see default_backup_root()
    'run_in_background': True,  # if False, closing the native app window shuts the whole app down instead of leaving auto-backup running
}


def default_backup_root():
    return os.path.join(scanner.DRIVE_ROOT, 'SaveSteam')

AUTO_SNAPSHOT_SUFFIX = '-auto'

_config_lock = threading.Lock()
_scan_lock = threading.Lock()
_scan_cache = {'data': None, 'at': 0}
# how long a cached scan is reused before a page load/poll triggers a real
# disk scan again - tied to the user's own scan_interval_seconds setting
# (Settings tab) rather than a separate hardcoded value, so "Refresh" is the
# only thing that forces a scan sooner than that.

_cover_cache_lock = threading.Lock()
_cover_cache = {}  # appid (str) -> header_image URL or None, cached for the life of the process

_disk_usage_cache = {'data': None, 'at': 0}
DISK_USAGE_CACHE_TTL = 60  # walking every backup dir is a bit heavy to do on every page load/poll

LOW_SPACE_THRESHOLD_BYTES = 5 * 1024 ** 3  # 5 GB
LOW_SPACE_RENOTIFY_SECONDS = 6 * 3600  # don't spam a desktop notification every single cycle while still low
_low_space_state = {'notified_at': 0, 'was_low': False}


def open_in_file_manager(path):
    """Best-effort: open a folder in the user's default file manager via
    xdg-open. The service can start before the graphical session exists, so
    the display variables are read fresh from the systemd user manager. An
    early non-zero exit is reported as a failure; a process still running
    after a moment is the file manager starting normally."""
    try:
        proc = subprocess.Popen(
            ['xdg-open', path], env=_graphical_session_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        return proc.wait(timeout=1.5) == 0
    except subprocess.TimeoutExpired:
        return True


GRAPHICAL_ENV_KEYS = (
    'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'XDG_CURRENT_DESKTOP',
    'XDG_SESSION_TYPE', 'KDE_FULL_SESSION', 'KDE_SESSION_VERSION',
)


def _graphical_session_env():
    env = os.environ.copy()
    try:
        out = subprocess.run(
            ['systemctl', '--user', 'show-environment'],
            capture_output=True, text=True, timeout=3, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return env
    for line in out.splitlines():
        key, sep, value = line.partition('=')
        if sep and key in GRAPHICAL_ENV_KEYS:
            env[key] = value
    return env


REMOVABLE_ROOTS = ('/run/media/', '/media/', '/mnt/')


def _source_confirmed_gone(path):
    """True only if path is missing AND the disk that should hold it is
    available - an unplugged drive must never cost a game its tracking."""
    if os.path.lexists(path) and os.path.exists(path):
        return False
    real = os.path.realpath(path)
    root = next((r for r in REMOVABLE_ROOTS if real.startswith(r)), None)
    if root is None:
        return True
    current = os.path.dirname(real)
    while len(current) >= len(root):
        if os.path.ismount(current):
            return True
        current = os.path.dirname(current)
    return False


def notify_desktop(summary, body, urgency='normal'):
    """Best-effort desktop notification via notify-send. Silently does
    nothing if unavailable (e.g. no session bus) - this is a convenience,
    never something the backup cycle should fail over."""
    try:
        subprocess.run(
            ['notify-send', '--app-name=SaveSteam', f'--urgency={urgency}', summary, body],
            timeout=5, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def get_disk_usage(force=False):
    now = time.time()
    if not force and _disk_usage_cache['data'] is not None and now - _disk_usage_cache['at'] < DISK_USAGE_CACHE_TTL:
        return _disk_usage_cache['data']
    root = savesteam_root()
    du = shutil.disk_usage(root)
    data = {'backups_bytes': dir_size(root), 'free_bytes': du.free, 'total_bytes': du.total}
    _disk_usage_cache['data'] = data
    _disk_usage_cache['at'] = now
    return data


def check_low_space_and_notify():
    du = shutil.disk_usage(savesteam_root())
    now = time.time()
    if du.free < LOW_SPACE_THRESHOLD_BYTES:
        if not _low_space_state['was_low'] or now - _low_space_state['notified_at'] > LOW_SPACE_RENOTIFY_SECONDS:
            notify_desktop(
                'SaveSteam - espace disque faible',
                f'Seulement {du.free / 1e9:.1f} Go libres sur le disque de sauvegarde.',
                urgency='critical',
            )
            _low_space_state['notified_at'] = now
        _low_space_state['was_low'] = True
    else:
        _low_space_state['was_low'] = False


def fetch_steam_header_image(appid):
    """Look up the current header image URL for a real Steam appid via
    Steam's public store API. Needed because newer app listings use a
    content-hashed CDN path that can't be guessed from the appid alone
    (the old cdn.akamai.steamstatic.com/steam/apps/<id>/header.jpg pattern
    only works for older listings). Cached in memory - this is a cosmetic
    lookup, not something that needs to hit Steam on every page load."""
    with _cover_cache_lock:
        if appid in _cover_cache:
            return _cover_cache[appid]
    url = f'https://store.steampowered.com/api/appdetails?appids={appid}&l=english'
    result = None
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.load(resp)
        entry = data.get(str(appid), {})
        if entry.get('success'):
            result = entry.get('data', {}).get('header_image')
    except Exception:  # noqa: BLE001 - best-effort cosmetic lookup, never fatal
        result = None
    with _cover_cache_lock:
        _cover_cache[appid] = result
    return result


# ---- game covers (local Steam artwork + SteamGridDB picker) ----
# Kept outside the app folder (a git repo) and outside config.json (which gets
# copied into the disaster-recovery backup), so the API key never travels.
SGDB_API = 'https://www.steamgriddb.com/api/v2'
SGDB_KEY_PATH = os.path.expanduser('~/.config/saveteam/steamgriddb.key')
COVERS_DIR = os.path.expanduser('~/.local/share/saveteam-covers')
COVER_MAX_BYTES = 15 * 1024 * 1024
COVER_TYPES = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}
COVER_ID_RE = re.compile(r'^\d{1,20}$')  # custom sources use millisecond timestamps


class SgdbError(Exception):
    """A SteamGridDB failure with a message fit to show the user."""


def sgdb_key():
    try:
        with open(SGDB_KEY_PATH, 'r', encoding='utf-8') as f:
            return f.read().strip() or None
    except OSError:
        return None


def save_sgdb_key(key):
    os.makedirs(os.path.dirname(SGDB_KEY_PATH), mode=0o700, exist_ok=True)
    fd = os.open(SGDB_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(key)


def _sgdb_get(api_path, key):
    req = urllib.request.Request(
        SGDB_API + api_path,
        headers={'Authorization': f'Bearer {key}', 'User-Agent': 'SaveSteam'},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp).get('data') or []
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SgdbError('clé SteamGridDB refusée') from e
        if e.code == 404:
            return []
        raise SgdbError(f'SteamGridDB a répondu {e.code}') from e
    except (OSError, ValueError) as e:
        raise SgdbError('SteamGridDB est injoignable') from e


def sgdb_find_grids(app_id, name, kind, query, key):
    """Portrait covers for a game. A real Steam appid is an exact lookup; a
    non-Steam shortcut, or a manual search, goes through a name search."""
    params = '?dimensions=600x900,660x930&types=static&nsfw=false'
    if kind == 'steam' and not query:
        grids = _sgdb_get(f'/grids/steam/{app_id}{params}', key)
        matched = name
    else:
        term = re.sub(r'[™®©]', '', query or name or '').strip()
        if not term:
            return {'game': None, 'grids': []}
        games = _sgdb_get('/search/autocomplete/' + urllib.parse.quote(term, safe=''), key)
        if not games:
            return {'game': None, 'grids': []}
        matched = games[0].get('name')
        grids = _sgdb_get(f"/grids/game/{int(games[0]['id'])}{params}", key)
    return {
        'game': matched,
        'grids': [{'url': g['url'], 'thumb': g.get('thumb') or g['url']} for g in grids if g.get('url')],
    }


def _is_sgdb_image_url(url):
    parsed = urlparse(url)
    host = parsed.hostname or ''
    return parsed.scheme == 'https' and (host == 'steamgriddb.com' or host.endswith('.steamgriddb.com'))


def remove_cover(app_id):
    for existing in glob.glob(os.path.join(COVERS_DIR, app_id + '.*')):
        os.remove(existing)


def download_cover(app_id, url):
    if not _is_sgdb_image_url(url):
        raise SgdbError('adresse d\'image non autorisée')
    req = urllib.request.Request(url, headers={'User-Agent': 'SaveSteam'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if not _is_sgdb_image_url(resp.geturl()):
                raise SgdbError('adresse d\'image non autorisée')
            ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            ext = COVER_TYPES.get(ctype)
            if not ext:
                raise SgdbError('format d\'image non pris en charge')
            data = resp.read(COVER_MAX_BYTES + 1)
    except OSError as e:
        raise SgdbError('téléchargement de l\'image impossible') from e
    if len(data) > COVER_MAX_BYTES:
        raise SgdbError('image trop lourde')
    os.makedirs(COVERS_DIR, exist_ok=True)
    tmp = os.path.join(COVERS_DIR, f'.{app_id}.tmp')
    with open(tmp, 'wb') as f:
        f.write(data)
    remove_cover(app_id)
    os.replace(tmp, os.path.join(COVERS_DIR, app_id + ext))


def find_cover_file(app_id):
    """The user's pick first, then the portrait Steam already has locally."""
    chosen = glob.glob(os.path.join(COVERS_DIR, app_id + '.*'))
    if chosen:
        return chosen[0]
    pattern = os.path.join(scanner.STEAM_ROOT, 'userdata', '*', 'config', 'grid', app_id + 'p.*')
    local = sorted(glob.glob(pattern))
    return local[0] if local else None


def _load_config_nolock():
    if not os.path.isfile(CONFIG_PATH):
        cfg = dict(DEFAULT_CONFIG)
        cfg['compat_groups'] = _default_compat_groups()
        _save_config_nolock(cfg)
        return cfg
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        cfg = dict(DEFAULT_CONFIG)
    cfg.setdefault('port', DEFAULT_CONFIG['port'])
    cfg.setdefault('custom_sources', [])
    cfg.setdefault('auto_backup_targets', [])
    cfg.setdefault('keep_snapshots', AUTO_BACKUP_KEEP_DEFAULT)
    cfg.setdefault('backup_interval_seconds', AUTO_BACKUP_INTERVAL_DEFAULT)
    cfg.setdefault('scan_interval_seconds', SCAN_INTERVAL_DEFAULT)
    cfg.setdefault('backup_root', None)
    cfg.setdefault('run_in_background', True)
    if not cfg.get('compat_groups'):
        cfg['compat_groups'] = _default_compat_groups()
    return cfg


def _save_config_nolock(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def load_config():
    with _config_lock:
        return _load_config_nolock()


def save_config(cfg):
    with _config_lock:
        _save_config_nolock(cfg)


def update_config(mutator):
    """Atomically load, mutate in place, and persist the config."""
    with _config_lock:
        cfg = _load_config_nolock()
        mutator(cfg)
        _save_config_nolock(cfg)
        return cfg


def get_sources(force=False):
    now = time.time()
    with _scan_lock:
        cfg = load_config()
        ttl = cfg.get('scan_interval_seconds', SCAN_INTERVAL_DEFAULT)
        if not force and _scan_cache['data'] is not None and now - _scan_cache['at'] < ttl:
            return _scan_cache['data']
        data = scanner.list_sources(cfg)
        _scan_cache['data'] = data
        _scan_cache['at'] = now
        return data


_SAFE_NAME_RE = re.compile(r'[\x00-\x1f/\\]')


def sanitize_game_name(name):
    name = (name or '').strip()
    name = _SAFE_NAME_RE.sub('_', name)
    name = name.strip(' .')
    return name or 'Unknown game'


def savesteam_root():
    root = (load_config().get('backup_root') or '').strip() or default_backup_root()
    os.makedirs(root, exist_ok=True)
    return root


def dir_size(path):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                total += os.lstat(fp).st_size
            except OSError:
                pass
    return total


def compute_signature(path):
    """Cheap change-detection signature: (file count, total size, newest mtime).
    Not a content hash - good enough to skip a redundant copy of unchanged saves
    without walking/hashing potentially large prefixes every cycle."""
    if os.path.isfile(path):
        st = os.stat(path)
        return [1, st.st_size, st.st_mtime]
    count = 0
    total = 0
    newest = 0.0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            count += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return [count, total, newest]


def _meta_path(snapshot_dir):
    return snapshot_dir.rstrip('/') + '.meta.json'


def read_snapshot_meta(snapshot_dir):
    try:
        with open(_meta_path(snapshot_dir), 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _latest_snapshot_dir(game_name):
    """Most recent existing snapshot dir for this game (any kind: auto, kept,
    manual, -prerestore) - used as a hardlink reference so a new snapshot
    doesn't re-copy bytes for files that haven't changed since then."""
    dest_dir = os.path.join(savesteam_root(), game_name)
    try:
        with os.scandir(dest_dir) as it:
            snaps = sorted((e for e in it if e.is_dir()), key=lambda e: e.name, reverse=True)
    except OSError:
        return None
    return snaps[0].path if snaps else None


def _copy_file_dedup(src_file, dest_file, ref_file):
    """Copy src_file to dest_file, but hardlink to ref_file instead of
    duplicating bytes if it looks like the same content (same size + mtime).
    Falls back to a real copy whenever that's not possible (no reference,
    mismatch, or the link fails e.g. across filesystems)."""
    if ref_file is not None:
        try:
            src_st = os.stat(src_file)
            ref_st = os.stat(ref_file)
            if not os.path.islink(ref_file) and src_st.st_size == ref_st.st_size and src_st.st_mtime == ref_st.st_mtime:
                os.link(ref_file, dest_file)
                return
        except OSError:
            pass
    shutil.copy2(src_file, dest_file)


def _copy_tree_dedup(src, dest, ref):
    """Recursively copy src into dest, hardlinking any file that's unchanged
    from the matching file under ref (the previous snapshot) instead of
    re-copying it. Symlinks are preserved as symlinks, never followed -
    Proton prefixes contain pfx/dosdevices/z: -> "/" and similar drive-letter
    symlinks that would otherwise pull in far more than the prefix itself."""
    os.makedirs(dest, exist_ok=True)
    with os.scandir(src) as it:
        entries = list(it)
    for entry in entries:
        d = os.path.join(dest, entry.name)
        r = os.path.join(ref, entry.name) if ref else None
        if entry.is_symlink():
            os.symlink(os.readlink(entry.path), d)
        elif entry.is_dir():
            _copy_tree_dedup(entry.path, d, r if r and os.path.isdir(r) else None)
        else:
            _copy_file_dedup(entry.path, d, r if r and os.path.isfile(r) else None)


def make_backup_copy(src, game_name, auto=False, tag=''):
    """Copy src into SaveSteam/<game_name>/<timestamp>[-auto][tag]/, avoiding
    collisions. Files unchanged since the game's previous snapshot are
    hardlinked instead of duplicated, so keeping several snapshots costs
    disk space roughly proportional to what actually changed, not to the
    snapshot count. Writes a sidecar <dest>.meta.json recording where src
    was, so a snapshot can later be restored even if it's no longer being
    tracked. Returns the destination path."""
    if os.path.isdir(src):
        src_bytes = compute_signature(src)[1]
        free_bytes = shutil.disk_usage(savesteam_root()).free
        if src_bytes > free_bytes:
            raise OSError(
                f'not enough free space: source is {src_bytes / 1e9:.1f} GB, '
                f'only {free_bytes / 1e9:.1f} GB free on the backup drive'
            )
    ref = _latest_snapshot_dir(game_name)
    timestamp = time.strftime('%Y%m%d-%H%M%S') + (AUTO_SNAPSHOT_SUFFIX if auto else '') + tag
    dest_dir = os.path.join(savesteam_root(), game_name)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, timestamp)
    base_dest = dest
    suffix = 1
    while os.path.exists(dest):
        suffix += 1
        dest = f'{base_dest}-{suffix}'
    if os.path.isdir(src):
        _copy_tree_dedup(src, dest, ref)
    else:
        os.makedirs(dest, exist_ok=True)
        ref_file = os.path.join(ref, os.path.basename(src)) if ref else None
        _copy_file_dedup(src, os.path.join(dest, os.path.basename(src)), ref_file if ref_file and os.path.isfile(ref_file) else None)
    with open(_meta_path(dest), 'w', encoding='utf-8') as f:
        json.dump({'source_path': src, 'game_name': game_name, 'created_at': time.time(), 'auto': auto}, f)
    return dest


def prune_auto_snapshots(game_name, keep=AUTO_BACKUP_KEEP_DEFAULT):
    dest_dir = os.path.join(savesteam_root(), game_name)
    try:
        with os.scandir(dest_dir) as it:
            auto_snaps = sorted(
                (e for e in it if e.is_dir() and _is_auto_snapshot(e.name)),
                key=lambda e: e.name,
                reverse=True,
            )
    except OSError:
        return
    # Rotate per original folder: a game saving in two places must not have
    # one folder's new snapshot push out the other's only copy.
    by_source = {}
    for snap in auto_snaps:
        by_source.setdefault(read_snapshot_meta(snap.path).get('source_path'), []).append(snap)
    for snaps in by_source.values():
        stale_snaps = snaps[keep:]
        if not stale_snaps:
            continue
        newest_size = scanner._dir_size(snaps[0].path)
        for stale in stale_snaps:
            # The folder shrank a lot (typically a reinstall with a blank
            # save): the older, fuller snapshot is the one worth keeping.
            if scanner._dir_size(stale.path) > newest_size * 2:
                continue
            shutil.rmtree(stale.path, ignore_errors=True)
            try:
                os.remove(_meta_path(stale.path))
            except OSError:
                pass


def _base_game_name(name):
    return re.sub(r' \([^()]*\)$', '', name)


_SNAPSHOT_NAME_RE = re.compile(r'^\d{8}-\d{6}')


def _is_auto_snapshot(name):
    # Two folders saved in the same second produce "-auto-2", "-auto-3"...
    return re.search(r'-auto(-\d+)?$', name) is not None


def _promote_snapshot(path):
    """Make an auto snapshot permanent by removing its "-auto" suffix."""
    base_dest = re.sub(r'-auto(-\d+)?$', '', path)
    dest = base_dest
    suffix = 1
    while os.path.exists(dest):
        suffix += 1
        dest = f'{base_dest}-kept{suffix}'
    os.rename(path, dest)
    try:
        os.rename(_meta_path(path), _meta_path(dest))
    except OSError:
        pass
    return dest


def _game_snapshots(base):
    """(folder name, snapshot DirEntry) for every backup folder of a game."""
    found = []
    try:
        folders = [e for e in os.scandir(savesteam_root())
                   if e.is_dir() and (e.name == base or e.name.startswith(base + ' ('))]
    except OSError:
        return found
    for folder in folders:
        try:
            found.extend((folder.name, s) for s in os.scandir(folder.path)
                         if s.is_dir() and 'prerestore' not in s.name)
        except OSError:
            continue
    return found


def protect_latest_snapshots(base, source_path=None):
    """Make the newest snapshot of each saved location permanent, so nothing
    a reinstalled game produces can rotate it away."""
    newest = {}
    for _folder, snap in _game_snapshots(base):
        source = read_snapshot_meta(snap.path).get('source_path')
        if source_path is not None and source != source_path:
            continue
        if source not in newest or snap.name[:15] > newest[source].name[:15]:
            newest[source] = snap
    for snap in newest.values():
        if _is_auto_snapshot(snap.name):
            _promote_snapshot(snap.path)


def _tree_entries(path):
    """Relative path -> (full path, lstat) of everything under path, without
    following symlinks. A single file is keyed by its name, as in a snapshot."""
    if os.path.isfile(path):
        return {os.path.basename(path): (path, os.lstat(path))}

    def fail(error):
        raise error

    found = {}
    for dirpath, dirnames, filenames in os.walk(path, onerror=fail):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            found[os.path.relpath(full, path)] = (full, os.lstat(full))
    return found


def _same_bytes(a, b):
    with open(a, 'rb') as fa, open(b, 'rb') as fb:
        while True:
            chunk = fa.read(1024 * 1024)
            if chunk != fb.read(1024 * 1024):
                return False
            if not chunk:
                return True


def same_content(a, b):
    """True only if both trees hold the same names, modification dates and
    bytes. Any doubt or read error counts as different: nothing gets deleted."""
    try:
        left, right = _tree_entries(a), _tree_entries(b)
        if left.keys() != right.keys():
            return False
        for rel, (path_a, st_a) in left.items():
            path_b, st_b = right[rel]
            if stat_mode.S_IFMT(st_a.st_mode) != stat_mode.S_IFMT(st_b.st_mode):
                return False
            if stat_mode.S_ISDIR(st_a.st_mode):
                continue
            if stat_mode.S_ISLNK(st_a.st_mode):
                if os.readlink(path_a) != os.readlink(path_b):
                    return False
            elif not stat_mode.S_ISREG(st_a.st_mode):
                return False
            elif st_a.st_size != st_b.st_size or st_a.st_mtime_ns != st_b.st_mtime_ns:
                return False
            elif (st_a.st_dev, st_a.st_ino) != (st_b.st_dev, st_b.st_ino) and not _same_bytes(path_a, path_b):
                return False
        return True
    except OSError:
        return False


def _regular_snapshots(game_name):
    """Snapshot paths of one backup folder, newest first. Safety copies made
    before a restore are left out: they are never merged or deleted."""
    try:
        with os.scandir(os.path.join(savesteam_root(), game_name)) as it:
            names = [e.name for e in it if e.is_dir() and _SNAPSHOT_NAME_RE.match(e.name) and 'prerestore' not in e.name]
    except OSError:
        return []
    names.sort(key=lambda n: (n[:15], n), reverse=True)
    return [os.path.join(savesteam_root(), game_name, n) for n in names]


def is_same_as_latest(src, game_name):
    snaps = _regular_snapshots(game_name)
    return snaps[0] if snaps and same_content(src, snaps[0]) else None


def backup_permanent(src, game_name):
    """Permanent snapshot of src. If the newest one is already identical, it
    is kept (made permanent if needed) instead of adding a copy."""
    same = is_same_as_latest(src, game_name)
    if same:
        return (_promote_snapshot(same) if _is_auto_snapshot(os.path.basename(same)) else same), 'identical'
    return make_backup_copy(src, game_name, auto=False), 'backed_up'


def merge_duplicate_snapshots(game_name):
    """Keep one snapshot per identical group: the newest, so "latest" stays
    right, and permanent if any copy of the group was. Returns how many went."""
    groups = []
    for snap in _regular_snapshots(game_name):
        permanent = not _is_auto_snapshot(os.path.basename(snap))
        for group in groups:
            if same_content(group['keep'], snap):
                group['extra'].append(snap)
                group['permanent'] = group['permanent'] or permanent
                break
        else:
            groups.append({'keep': snap, 'extra': [], 'permanent': permanent})
    removed = 0
    for group in groups:
        if not group['extra']:
            continue
        # Promoted first: an interruption can't leave the content in rotation only.
        if group['permanent'] and _is_auto_snapshot(os.path.basename(group['keep'])):
            _promote_snapshot(group['keep'])
        for snap in group['extra']:
            try:
                shutil.rmtree(snap)
            except OSError:
                continue
            removed += 1
            try:
                os.remove(_meta_path(snap))
            except OSError:
                pass
    return removed


def merge_all_duplicate_snapshots():
    try:
        with os.scandir(savesteam_root()) as it:
            folders = [e.name for e in it if e.is_dir() and not e.name.startswith('_')]
    except OSError:
        return
    for folder in folders:
        merge_duplicate_snapshots(folder)


def _source_inode(path):
    # Inode only: a device number can change across remounts, an inode
    # changes when the folder was really deleted and recreated.
    try:
        return os.stat(path).st_ino
    except OSError:
        return None


def _backed_up_games():
    """Base names of games with at least one real snapshot. Must be computed
    before update_config(): savesteam_root() reads the config, and its lock
    isn't reentrant, so calling it from a mutator would hang the server."""
    games = set()
    try:
        folders = [e for e in os.scandir(savesteam_root()) if e.is_dir()]
    except OSError:
        return games
    for folder in folders:
        try:
            if any(s.is_dir() and 'prerestore' not in s.name for s in os.scandir(folder.path)):
                games.add(_base_game_name(folder.name))
        except OSError:
            continue
    return games


def _guarded_new_target(src, game_name, protect, backed_up):
    """A new target. If this game already has backups it's a reinstall (or a
    reformatted PC): wait for the user instead of backing up a blank save."""
    target = _new_auto_target(src, game_name)
    target['source_inode'] = _source_inode(src)
    base = _base_game_name(game_name)
    if base in backed_up:
        target['awaiting_restore'] = True
        protect.append((base, None))
    return target


def _folder_label(path):
    if '/steamuser/' in path:
        parts = path.split('/steamuser/', 1)[1].split('/')
        return f'AppData {parts[1]}' if parts[0] == 'AppData' and len(parts) > 1 else parts[0]
    return os.path.basename(path.rstrip('/'))


def split_shared_backup_folders():
    """Give each tracked folder its own backup folder and name when several
    folders of a game were sharing one (the same name twice in the list)."""
    cfg = load_config()
    by_name = {}
    for t in cfg['auto_backup_targets']:
        by_name.setdefault(t['game_name'], []).append(t)
    renames = {}
    for name, targets in by_name.items():
        if len({t['source_path'] for t in targets}) < 2:
            continue
        taken = set(by_name) - {name}
        base = _base_game_name(name)
        for t in targets:
            wanted = sanitize_game_name(f'{base} ({_folder_label(t["source_path"])})')
            new_name, n = wanted, 1
            while new_name in taken:
                n += 1
                new_name = f'{wanted} {n}'
            taken.add(new_name)
            renames[t['id']] = new_name
        old_dir = os.path.join(savesteam_root(), name)
        new_by_source = {t['source_path']: renames[t['id']] for t in targets}
        try:
            snaps = [s for s in os.scandir(old_dir) if s.is_dir()]
        except OSError:
            snaps = []
        for snap in snaps:
            meta = read_snapshot_meta(snap.path)
            new_name = new_by_source.get(meta.get('source_path'))
            if not new_name:
                continue
            new_dir = os.path.join(savesteam_root(), new_name)
            os.makedirs(new_dir, exist_ok=True)
            dest = os.path.join(new_dir, snap.name)
            if os.path.exists(dest):
                continue
            old_meta = _meta_path(snap.path)
            os.rename(snap.path, dest)
            with open(_meta_path(dest), 'w', encoding='utf-8') as f:
                json.dump({**meta, 'game_name': new_name}, f)
            try:
                os.remove(old_meta)
            except OSError:
                pass
        try:
            os.rmdir(old_dir)
        except OSError:
            pass
    if renames:
        def mutator(cfg2):
            for t in cfg2['auto_backup_targets']:
                if t['id'] in renames:
                    t['game_name'] = renames[t['id']]
        update_config(mutator)


def list_untracked_folders():
    """Folders that can be brought back: those the user stopped tracking,
    plus save folders detected for a non-Steam game but not tracked (which
    covers anything stopped before stops were remembered)."""
    cfg = load_config()
    tracked = {t['source_path'] for t in cfg['auto_backup_targets']}
    found = {}
    for s in cfg.get('stopped_targets', []):
        if s['source_path'] not in tracked:
            found[s['source_path']] = {
                'source_path': s['source_path'], 'game_name': s['game_name'],
                'stopped': True, 'exists': os.path.isdir(s['source_path']),
            }
    for group in get_sources(force=True)['groups']:
        if not group['mounted']:
            continue
        for entry in group['entries']:
            if entry['kind'] != 'nonsteam' or not entry.get('path') or not entry.get('name'):
                continue
            try:
                candidates = scanner.auto_trackable_save_dirs(entry['path'], exe_path=entry.get('exe_path'))
            except OSError:
                continue
            base = sanitize_game_name(entry['name'])
            for c in candidates:
                if c['path'] not in tracked and c['path'] not in found:
                    found[c['path']] = {
                        'source_path': c['path'],
                        'game_name': sanitize_game_name(f'{base} ({_folder_label(c["path"])})'),
                        'stopped': False, 'exists': True,
                    }
    return sorted(found.values(), key=lambda f: f['game_name'].lower())


def track_untracked_folder(source_path):
    folder = next((f for f in list_untracked_folders() if f['source_path'] == source_path), None)
    if folder is None:
        raise ValueError('dossier inconnu')
    if not folder['exists']:
        raise ValueError('ce dossier n\'existe plus sur le disque')
    protect_latest_snapshots(_base_game_name(folder['game_name']), source_path)

    def mutator(cfg):
        cfg['stopped_targets'] = [s for s in cfg.get('stopped_targets', []) if s['source_path'] != source_path]
        if not any(t['source_path'] == source_path for t in cfg['auto_backup_targets']):
            target = _new_auto_target(source_path, folder['game_name'])
            target['source_inode'] = _source_inode(source_path)
            cfg['auto_backup_targets'].append(target)
    update_config(mutator)


def resume_tracking(game):
    """"Repartir de zéro": old backups stay permanently, new ones may start."""
    base = _base_game_name(game)
    protect_latest_snapshots(base)

    def mutator(cfg):
        for t in cfg['auto_backup_targets']:
            if _base_game_name(t['game_name']) == base:
                t.pop('awaiting_restore', None)
                t['source_inode'] = _source_inode(t['source_path'])
    update_config(mutator)


def _current_game_entry(game_name, sources):
    names = {game_name, _base_game_name(game_name)}
    matches = [
        entry for group in sources['groups'] if group['mounted'] for entry in group['entries']
        if entry['kind'] in ('steam', 'nonsteam') and entry.get('name')
        and sanitize_game_name(entry['name']) in names
    ]
    matches.sort(key=lambda entry: not entry.get('path'))
    return matches[0] if matches else None


def resolve_restore_dest(source_path, game_name, sources):
    """Where a snapshot belongs today. A reinstalled non-Steam game can get a
    new prefix id, so a save inside a prefix follows the game by name and
    keeps its relative location. Returns (dest, status), status being
    'ready', 'launch_first' (prefix doesn't exist yet) or 'not_found'."""
    entry = _current_game_entry(game_name, sources)
    if '/pfx/' in source_path:
        old_prefix, rel = source_path.split('/pfx/', 1)
        if entry is None:
            if os.path.isdir(os.path.join(old_prefix, 'pfx', 'drive_c')):
                return source_path, 'ready'
            return None, 'not_found'
        if not entry.get('path') or not os.path.isdir(
                os.path.join(entry['path'], 'pfx', 'drive_c', 'users', 'steamuser')):
            return None, 'launch_first'
        return os.path.join(entry['path'], 'pfx', rel), 'ready'
    if os.path.isdir(source_path) or os.path.isdir(os.path.dirname(source_path)):
        return source_path, 'ready'
    exe = entry.get('exe_path') if entry else None
    if exe and os.path.isdir(os.path.dirname(exe)):
        return os.path.join(os.path.dirname(exe), os.path.basename(source_path)), 'ready'
    return None, 'launch_first' if entry else 'not_found'


def build_restore_plan(game):
    """Every saved location of a game (all its backup folders), each with its
    available versions, newest first, and the destination found for it."""
    base = _base_game_name(game)
    root = savesteam_root()
    tracked = {}
    for t in load_config().get('auto_backup_targets', []):
        tracked.setdefault(t['game_name'], set()).add(t['source_path'])
    sources = get_sources(force=True)
    locations = {}
    try:
        folders = [e for e in os.scandir(root) if e.is_dir() and (e.name == base or e.name.startswith(base + ' ('))]
    except OSError:
        folders = []
    for folder in folders:
        for snap in os.scandir(folder.path):
            if not snap.is_dir() or 'prerestore' in snap.name:
                continue
            source = read_snapshot_meta(snap.path).get('source_path')
            if not source and len(tracked.get(folder.name, ())) == 1:
                source = next(iter(tracked[folder.name]))
            dest, status = resolve_restore_dest(source, folder.name, sources) if source else (None, 'not_found')
            if dest and not scanner.is_browse_allowed(dest):
                dest, status = None, 'not_found'
            key = dest or source or snap.path
            label = (source or folder.name).split('/steamuser/')[-1]
            if '/' in label and label.startswith('/'):
                label = '/'.join(label.split('/')[-2:])
            loc = locations.setdefault(key, {
                'label': label, 'dest': dest, 'status': status, 'source_path': source,
                'game_folder': folder.name, 'snapshots': [],
            })
            loc['snapshots'].append({
                'path': snap.path, 'name': snap.name, 'game_folder': folder.name,
                'mtime': snap.stat().st_mtime, 'size': scanner._dir_size(snap.path),
            })
    for loc in locations.values():
        loc['snapshots'].sort(key=lambda s: s['mtime'], reverse=True)
    return {'game': base, 'locations': sorted(locations.values(), key=lambda l: l['label'].lower())}


def restore_game(game, snapshot_paths):
    """Put the chosen snapshots back. Destinations come from the plan, never
    from the request, and anything already there is snapshotted first."""
    plan = build_restore_plan(game)
    by_path = {
        s['path']: (loc, s) for loc in plan['locations'] for s in loc['snapshots']
        if loc['status'] == 'ready'
    }
    if not snapshot_paths or any(p not in by_path for p in snapshot_paths):
        raise ValueError('sauvegarde inconnue ou dossier du jeu introuvable')
    if not _backup_pass_lock.acquire(blocking=False):
        raise BlockingIOError('une sauvegarde est en cours, réessaie dans un instant')
    results = []
    try:
        for path in snapshot_paths:
            loc, snap = by_path[path]
            dest = loc['dest']
            safety = None
            if os.path.isdir(dest) and os.listdir(dest):
                safety = make_backup_copy(dest, snap['game_folder'], auto=False, tag='-prerestore')
            os.makedirs(dest, exist_ok=True)
            shutil.copytree(path, dest, symlinks=True, dirs_exist_ok=True)
            results.append({'label': loc['label'], 'dest': dest, 'safety_snapshot': safety})

            def mutator(cfg, dest=dest, folder=snap['game_folder']):
                if not any(t['source_path'] == dest for t in cfg['auto_backup_targets']):
                    cfg['auto_backup_targets'].append(_new_auto_target(dest, folder))

            update_config(mutator)
        base = _base_game_name(game)

        def clear_waiting(cfg):
            for t in cfg['auto_backup_targets']:
                if _base_game_name(t['game_name']) == base:
                    t.pop('awaiting_restore', None)
                    t['source_inode'] = _source_inode(t['source_path'])

        update_config(clear_waiting)
    finally:
        _backup_pass_lock.release()
    return results


_backup_pass_lock = threading.Lock()


def run_auto_backup_cycle():
    if not _backup_pass_lock.acquire(blocking=False):
        return []  # a pass (scheduled or manual) is already running - don't double up
    try:
        sync_auto_targets()
        split_shared_backup_folders()
        cfg = load_config()
        keep = cfg.get('keep_snapshots', AUTO_BACKUP_KEEP_DEFAULT)
        changed = False
        results = []
        error_games = []
        for target in cfg.get('auto_backup_targets', []):
            if not target.get('enabled', True) or target.get('awaiting_restore'):
                continue
            src = target['source_path']
            if not os.path.exists(src) or not scanner.is_browse_allowed(src):
                results.append((target['id'], 'missing', None))
                continue
            sig = compute_signature(src)
            if sig == target.get('last_signature'):
                target['last_checked_at'] = time.time()
                changed = True
                results.append((target['id'], 'unchanged', None))
                continue
            # Dates changed on a folder but same files as the last backup.
            if is_same_as_latest(src, target['game_name']):
                target['last_signature'] = sig
                target['last_checked_at'] = time.time()
                changed = True
                results.append((target['id'], 'unchanged', None))
                continue
            try:
                dest = make_backup_copy(src, target['game_name'], auto=True)
            except OSError as e:
                results.append((target['id'], 'error', str(e)))
                error_games.append(target['game_name'])
                continue
            prune_auto_snapshots(target['game_name'], keep=keep)
            target['last_signature'] = sig
            target['last_backup_at'] = time.time()
            target['last_checked_at'] = target['last_backup_at']
            changed = True
            results.append((target['id'], 'backed_up', dest))
        if changed:
            save_config(cfg)
        merge_all_duplicate_snapshots()
        if error_games:
            notify_desktop(
                'SaveSteam - échec de sauvegarde',
                'Échec pour : ' + ', '.join(error_games),
                urgency='critical',
            )
        check_low_space_and_notify()
        return results
    finally:
        _backup_pass_lock.release()


def auto_backup_loop():
    while True:
        try:
            run_auto_backup_cycle()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f'auto-backup cycle failed: {e}\n')
        interval = load_config().get('backup_interval_seconds', AUTO_BACKUP_INTERVAL_DEFAULT)
        time.sleep(max(interval, MIN_BACKUP_INTERVAL))


def _new_auto_target(src, game_name):
    return {
        'id': uuid.uuid4().hex,
        'source_path': src,
        'game_name': game_name,
        'enabled': True,
        'last_signature': None,
        'last_backup_at': None,
        'last_checked_at': None,
    }


def _backup_target_now(target):
    """Immediate permanent snapshot of one auto-backup target, shared by "back
    up all now" and the per-game button. An identical latest snapshot is kept
    instead of duplicated, and identical older ones are merged."""
    if not target.get('enabled', True) or target.get('awaiting_restore'):
        return {'game': target['game_name'], 'status': 'paused'}
    src = target['source_path']
    if not os.path.exists(src) or not scanner.is_browse_allowed(src):
        return {'game': target['game_name'], 'status': 'missing'}
    try:
        dest, status = backup_permanent(src, target['game_name'])
        merge_duplicate_snapshots(target['game_name'])
    except OSError as e:
        return {'game': target['game_name'], 'status': 'error', 'error': str(e)}
    sig = compute_signature(src)
    target_id = target['id']

    def mutator(cfg2, target_id=target_id, sig=sig):
        for t in cfg2['auto_backup_targets']:
            if t['id'] == target_id:
                t['last_signature'] = sig
                t['last_backup_at'] = time.time()
                t['last_checked_at'] = t['last_backup_at']

    update_config(mutator)
    return {'game': target['game_name'], 'status': status, 'dest': dest}


def register_auto_target(src, game_name):
    def mutator(cfg):
        for t in cfg['auto_backup_targets']:
            if t['source_path'] == src:
                t['game_name'] = game_name
                t['enabled'] = True
                return
        cfg['auto_backup_targets'].append(_new_auto_target(src, game_name))
    update_config(mutator)


def _targets_with_steam_status(cfg):
    """Copies of the targets with 'in_steam': False when neither the target's
    game name nor its compatdata id matches anything Steam still knows.
    Custom sources aren't Steam games, so they always count as present."""
    name_map = scanner.build_name_map()
    known = {sanitize_game_name(v['name']) for v in name_map.values() if v.get('name')}
    custom_paths = {cs['path'] for cs in cfg.get('custom_sources', [])}
    result = []
    for t in cfg['auto_backup_targets']:
        name = t['game_name']
        prefix_id = re.search(r'/compatdata[^/]*/(\d+)(?:/|$)', t['source_path'])
        in_steam = (
            t['source_path'] in custom_paths
            or name in known
            or any(name.startswith(n + ' (') for n in known)
            or bool(prefix_id and prefix_id.group(1) in name_map)
        )
        result.append({**t, 'in_steam': in_steam})
    return result


def sync_auto_targets():
    """Keeps auto-backup targets in sync, with zero required setup:
      - real Steam games (kind 'steam') are never auto-tracked, and never
        will be - Steam Cloud already covers them.
      - non-Steam games (kind 'nonsteam') get their actual save folder(s)
        found and tracked automatically: scanner.auto_trackable_save_dirs()
        scans the conventional per-game data locations inside the Proton
        prefix and returns whichever look like real, non-empty, non-clutter
        save data. No user action needed - a game with no target yet just
        gets re-scanned on every sync until it has something worth tracking
        (e.g. the user hasn't launched it yet). The manual "Choisir le
        dossier de sauvegarde..." picker in the UI is only a fallback for the
        rare game whose save location this heuristic doesn't recognize.
      - custom sources (folders the user explicitly added) keep being
        auto-tracked as before - those are already precise, hand-picked
        locations.

    This also drops any stale *whole-prefix* targets left over from the old
    behavior (before this heuristic existed, every compatdata entry - Steam
    and non-Steam alike - was tracked by its whole prefix path, which is what
    made backups balloon in size: a Proton prefix carries a full Wine
    "Windows install", not just the save data). Existing backup files aren't
    touched, only the tracking entry.
    """
    sources = get_sources(force=True)
    mounted_groups = [g for g in sources['groups'] if g['mounted']]
    whole_prefix_paths = {entry['path'] for group in mounted_groups for entry in group['entries']}
    protect = []  # (game, source or None): snapshots to make permanent
    backed_up = _backed_up_games()

    def mutator(cfg):
        protect.clear()
        before = len(cfg['auto_backup_targets'])
        cfg['auto_backup_targets'] = [
            t for t in cfg['auto_backup_targets'] if t['source_path'] not in whole_prefix_paths
        ]
        dropped = before - len(cfg['auto_backup_targets'])
        if dropped:
            sys.stderr.write(
                f'sync_auto_targets: stopped auto-tracking {dropped} whole-prefix target(s) '
                '(existing backups kept; now tracking precise save folders instead)\n'
            )

        # Forget targets of games removed from Steam. Only the tracking entry
        # goes away, snapshots stay on disk and remain restorable.
        custom_paths = {cs['path'] for cs in sources['custom_sources']}
        for t in cfg['auto_backup_targets']:
            if _source_confirmed_gone(t['source_path']):
                protect.append((_base_game_name(t['game_name']), t['source_path']))
        kept = [
            t for t in cfg['auto_backup_targets']
            if t['source_path'] in custom_paths or not _source_confirmed_gone(t['source_path'])
        ]
        gone = len(cfg['auto_backup_targets']) - len(kept)

        # A renamed shortcut leaves its old-name target behind on the same
        # folder. Drop the old name, carrying over "enabled" so a folder that
        # was actively backed up through it doesn't silently stop being so.
        current_names = {
            sanitize_game_name(entry['name'])
            for group in mounted_groups for entry in group['entries']
            if entry['kind'] == 'nonsteam' and entry['name']
        } | {sanitize_game_name(cs['label']) for cs in sources['custom_sources']}

        def is_current(name):
            return name in current_names or any(name.startswith(n + ' (') for n in current_names)

        keepers = {}
        for t in kept:
            if is_current(t['game_name']):
                keepers.setdefault(t['source_path'], []).append(t)
        deduped = []
        for t in kept:
            owners = keepers.get(t['source_path'])
            if owners and not is_current(t['game_name']):
                if t.get('enabled', True):
                    owners[0]['enabled'] = True
                continue
            deduped.append(t)
        renamed = len(kept) - len(deduped)
        cfg['auto_backup_targets'] = deduped

        # Same path but a different folder: it was deleted and recreated
        # between two checks, i.e. a quick reinstall.
        for t in deduped:
            inode = _source_inode(t['source_path'])
            if inode is None:
                continue
            known = t.get('source_inode')
            t['source_inode'] = inode
            if known is not None and known != inode:
                t['awaiting_restore'] = True
                protect.append((_base_game_name(t['game_name']), t['source_path']))
        if gone or renamed:
            sys.stderr.write(
                f'sync_auto_targets: forgot {gone} target(s) with a deleted folder and '
                f'{renamed} left over from a renamed game (existing backups kept)\n'
            )
        existing_paths = {t['source_path'] for t in cfg['auto_backup_targets']}
        stopped_paths = {s['source_path'] for s in cfg.get('stopped_targets', [])}
        stopped_games = {_base_game_name(s['game_name']) for s in cfg.get('stopped_targets', [])}

        for cs in sources['custom_sources']:
            if not cs['exists'] or cs['path'] in existing_paths or cs['path'] in stopped_paths:
                continue
            cfg['auto_backup_targets'].append(
                _guarded_new_target(cs['path'], sanitize_game_name(cs['label']), protect, backed_up))
            existing_paths.add(cs['path'])

        existing_names = {t['game_name'] for t in cfg['auto_backup_targets']}
        for group in mounted_groups:
            for entry in group['entries']:
                if entry['kind'] != 'nonsteam':
                    continue
                base_name = sanitize_game_name(entry['name'] or f"Unknown (id {entry['id']})")
                # Match by name, not by path prefix: a candidate's actual save
                # folder can live entirely outside this entry's compatdata
                # prefix (e.g. a shortcut whose .exe - and its saves right
                # next to it - live on an external drive; see
                # [[battlenet_ubisoft_game_drive]]), so a path-prefix check
                # would never recognize it as already covered and would keep
                # re-adding duplicate targets on every sync.
                if base_name in existing_names or any(n.startswith(base_name + ' (') for n in existing_names):
                    continue  # already has at least one tracked folder (auto or manual)
                if base_name in stopped_games:
                    continue  # the user stopped this game: its other folders stay off too
                try:
                    candidates = [
                        c for c in scanner.auto_trackable_save_dirs(entry['path'], exe_path=entry.get('exe_path'))
                        if c['path'] not in stopped_paths
                    ]
                except OSError:
                    continue
                same_names = len({c['name'] for c in candidates}) < len(candidates)
                for c in candidates:
                    label = _folder_label(c['path']) if same_names else c['name']
                    game_name = base_name if len(candidates) == 1 else sanitize_game_name(f'{base_name} ({label})')
                    cfg['auto_backup_targets'].append(_guarded_new_target(c['path'], game_name, protect, backed_up))
                    existing_paths.add(c['path'])
                    existing_names.add(game_name)

    result = update_config(mutator)
    for base, source in protect:
        try:
            protect_latest_snapshots(base, source)
        except OSError as e:
            sys.stderr.write(f'could not protect snapshots of {base}: {e}\n')
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = 'SaveSteam/1.0'

    def log_message(self, fmt, *args):
        sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))

    # ---- helpers ----
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, message):
        self._send_json({'error': message}, status=status)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return None

    def _serve_static(self, rel_path):
        if not rel_path or rel_path == '/':
            rel_path = 'index.html'
        rel_path = rel_path.lstrip('/')
        full = os.path.realpath(os.path.join(STATIC_DIR, rel_path))
        if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) and full != os.path.realpath(STATIC_DIR):
            self.send_error(403)
            return
        if not os.path.isfile(full):
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        with open(full, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- routing ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == '/api/sources':
                force = qs.get('force', ['0'])[0] == '1'
                self._send_json(get_sources(force=force))
            elif path == '/api/browse':
                self._handle_browse(qs)
            elif path == '/api/size':
                self._handle_size(qs)
            elif path == '/api/backups':
                self._handle_list_backups()
            elif path == '/api/auto-targets':
                # only re-scan+sync when explicitly asked (the "Refresh" button) -
                # a plain page load/poll just reports current state, no disk work.
                do_sync = qs.get('sync', ['0'])[0] == '1'
                cfg = sync_auto_targets() if do_sync else load_config()
                self._send_json({'interval_seconds': cfg.get('backup_interval_seconds', AUTO_BACKUP_INTERVAL_DEFAULT),
                                  'keep': cfg.get('keep_snapshots', AUTO_BACKUP_KEEP_DEFAULT),
                                  'targets': _targets_with_steam_status(cfg)})
            elif path == '/api/settings':
                cfg = load_config()
                self._send_json({
                    'keep_snapshots': cfg.get('keep_snapshots', AUTO_BACKUP_KEEP_DEFAULT),
                    'backup_interval_seconds': cfg.get('backup_interval_seconds', AUTO_BACKUP_INTERVAL_DEFAULT),
                    'scan_interval_seconds': cfg.get('scan_interval_seconds', SCAN_INTERVAL_DEFAULT),
                    'backup_root': (cfg.get('backup_root') or '').strip() or default_backup_root(),
                    'backup_root_is_default': not (cfg.get('backup_root') or '').strip(),
                    'run_in_background': cfg.get('run_in_background', True),
                    'min_backup_interval_seconds': MIN_BACKUP_INTERVAL,
                    'min_scan_interval_seconds': MIN_SCAN_INTERVAL,
                    'defaults': {
                        'keep_snapshots': AUTO_BACKUP_KEEP_DEFAULT,
                        'backup_interval_seconds': AUTO_BACKUP_INTERVAL_DEFAULT,
                        'scan_interval_seconds': SCAN_INTERVAL_DEFAULT,
                        'backup_root': default_backup_root(),
                        'run_in_background': True,
                    },
                })
            elif path == '/api/compat-groups':
                self._send_json({'groups': load_config().get('compat_groups', scanner.DEFAULT_COMPAT_GROUPS)})
            elif path == '/api/cover':
                appid = qs.get('appid', [None])[0]
                if not appid or not appid.isdigit():
                    self._send_error_json(400, 'appid is required')
                else:
                    self._send_json({'header_image': fetch_steam_header_image(appid)})
            elif path == '/api/untracked-folders':
                self._send_json({'folders': list_untracked_folders()})
            elif path == '/api/restore-plan':
                game = qs.get('game', [''])[0]
                if not game or '/' in game or game.startswith('.'):
                    self._send_error_json(400, 'jeu invalide')
                else:
                    self._send_json(build_restore_plan(game))
            elif path == '/api/cover-image':
                self._handle_cover_image(qs)
            elif path == '/api/sgdb/status':
                self._send_json({'configured': bool(sgdb_key())})
            elif path == '/api/sgdb/grids':
                self._handle_sgdb_grids(qs)
            elif path == '/api/discover-compatdata':
                self._handle_discover_compatdata()
            elif path == '/api/suggest-save-dirs':
                self._handle_suggest_save_dirs(qs)
            elif path == '/api/disk-usage':
                force = qs.get('force', ['0'])[0] == '1'
                self._send_json(get_disk_usage(force=force))
            elif path == '/api/autostart':
                self._send_json(get_autostart_status())
            elif path.startswith('/api/'):
                self._send_error_json(404, 'unknown endpoint')
            elif path.startswith('/static/'):
                self._serve_static(path[len('/static/'):])
            elif path == '/':
                self._serve_static('index.html')
            else:
                self.send_error(404)
        except Exception as e:  # noqa: BLE001
            self._send_error_json(500, str(e))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == '/api/backup':
                self._handle_backup()
            elif path == '/api/settings':
                self._handle_update_settings()
            elif path == '/api/restore-game':
                self._handle_restore_game()
            elif path == '/api/resume-tracking':
                game = str((self._read_json_body() or {}).get('game', ''))
                if not game or '/' in game or game.startswith('.'):
                    self._send_error_json(400, 'jeu invalide')
                else:
                    resume_tracking(game)
                    self._send_json({'ok': True})
            elif path == '/api/sgdb/key':
                self._handle_sgdb_key()
            elif path == '/api/cover-choice':
                self._handle_cover_choice()
            elif path == '/api/backup-all':
                self._handle_backup_all()
            elif path == '/api/backup-game':
                self._handle_backup_game()
            elif path == '/api/open-folder':
                self._handle_open_folder()
            elif path == '/api/custom-source':
                self._handle_add_custom_source()
            elif path == '/api/auto-targets/track':
                self._handle_track_folder()
            elif path == '/api/auto-targets/toggle':
                self._handle_toggle_auto_target()
            elif path == '/api/backups/keep':
                self._handle_keep_backup()
            elif path == '/api/backups/unkeep':
                self._handle_unkeep_backup()
            elif path == '/api/restore':
                self._handle_restore()
            elif path == '/api/compat-groups':
                self._handle_upsert_compat_group()
            elif path == '/api/window-closed':
                self._handle_window_closed()
            elif path == '/api/autostart':
                self._handle_set_autostart()
            else:
                self._send_error_json(404, 'unknown endpoint')
        except Exception as e:  # noqa: BLE001
            self._send_error_json(500, str(e))

    def do_DELETE(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            if parsed.path == '/api/custom-source':
                self._handle_delete_custom_source(qs)
            elif parsed.path == '/api/cover-choice':
                app_id = qs.get('id', [''])[0]
                if not COVER_ID_RE.match(app_id):
                    self._send_error_json(400, 'id invalide')
                else:
                    remove_cover(app_id)
                    self._send_json({'ok': True})
            elif parsed.path == '/api/auto-targets':
                self._handle_delete_auto_target(qs)
            elif parsed.path == '/api/backups':
                self._handle_delete_backup(qs)
            elif parsed.path == '/api/compat-groups':
                self._handle_delete_compat_group(qs)
            else:
                self._send_error_json(404, 'unknown endpoint')
        except Exception as e:  # noqa: BLE001
            self._send_error_json(500, str(e))

    # ---- handlers ----
    def _handle_browse(self, qs):
        target = qs.get('path', [None])[0]
        if not target:
            roots = [{'name': os.path.basename(r) or r, 'path': r, 'is_dir': True}
                      for r in scanner.BROWSE_ROOTS if os.path.isdir(r)]
            self._send_json({'path': None, 'parent': None, 'entries': roots})
            return
        if not scanner.is_browse_allowed(target) or not os.path.isdir(target):
            self._send_error_json(403, 'path not allowed or not a directory')
            return
        entries = []
        try:
            with os.scandir(target) as it:
                for e in it:
                    try:
                        is_dir = e.is_dir(follow_symlinks=True)
                    except OSError:
                        is_dir = False
                    entries.append({'name': e.name, 'path': os.path.join(target, e.name), 'is_dir': is_dir})
        except OSError as e:
            self._send_error_json(500, str(e))
            return
        entries.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        parent = os.path.dirname(target.rstrip('/')) or None
        if parent and not scanner.is_browse_allowed(parent):
            parent = None
        self._send_json({'path': target, 'parent': parent, 'entries': entries})

    def _handle_size(self, qs):
        target = qs.get('path', [None])[0]
        if not target or not scanner.is_browse_allowed(target) or not os.path.exists(target):
            self._send_error_json(403, 'path not allowed')
            return
        size = dir_size(target) if os.path.isdir(target) else os.path.getsize(target)
        self._send_json({'path': target, 'bytes': size})

    def _handle_discover_compatdata(self):
        cfg = load_config()
        known_reals = {os.path.realpath(g['path']) for g in cfg.get('compat_groups', scanner.DEFAULT_COMPAT_GROUPS)}
        results = []
        for entry in scanner.discover_compatdata_dirs():
            results.append({**entry, 'known': entry['real_path'] in known_reals})
        self._send_json({'results': results})

    def _handle_suggest_save_dirs(self, qs):
        target = qs.get('path', [None])[0]
        if not target or not scanner.is_browse_allowed(target) or not os.path.isdir(target):
            self._send_error_json(403, 'path not allowed or not a directory')
            return
        exe = qs.get('exe', [None])[0]
        if exe and not scanner.is_browse_allowed(exe):
            exe = None
        self._send_json(scanner.suggest_save_dirs(target, exe_path=exe))

    def _handle_list_backups(self):
        root = savesteam_root()
        games = []
        # fallback for snapshots made before the metadata sidecar existed
        tracked_paths = {t['game_name']: t['source_path'] for t in load_config().get('auto_backup_targets', [])}
        try:
            with os.scandir(root) as it:
                game_dirs = sorted((e for e in it if e.is_dir()), key=lambda e: e.name.lower())
        except OSError:
            game_dirs = []
        for gd in game_dirs:
            snapshots = []
            try:
                with os.scandir(gd.path) as it2:
                    snap_entries = sorted(it2, key=lambda e: e.name, reverse=True)
            except OSError:
                snap_entries = []
            for se in snap_entries:
                if not se.is_dir():
                    continue  # a snapshot is always a timestamped folder, not a loose file
                try:
                    st = se.stat()
                except OSError:
                    continue
                meta = read_snapshot_meta(se.path)
                is_auto = _is_auto_snapshot(se.name)
                snapshots.append({
                    'name': se.name,
                    'path': se.path,
                    'mtime': st.st_mtime,
                    'is_auto': is_auto,
                    # was this originally an auto snapshot that got "Kept" (suffix stripped)?
                    'kept': (not is_auto) and bool(meta.get('auto')),
                    'source_path': meta.get('source_path') or tracked_paths.get(gd.name),
                })
            if snapshots:
                games.append({'game': gd.name, 'path': gd.path, 'snapshots': snapshots, 'has_target': gd.name in tracked_paths})
        self._send_json({'root': root, 'games': games})

    def _handle_update_settings(self):
        body = self._read_json_body()
        if body is None:
            self._send_error_json(400, 'invalid JSON body')
            return
        try:
            keep = int(body.get('keep_snapshots'))
            interval = int(body.get('backup_interval_seconds'))
            scan_interval = int(body.get('scan_interval_seconds'))
        except (TypeError, ValueError):
            self._send_error_json(400, 'keep_snapshots, backup_interval_seconds and scan_interval_seconds must be integers')
            return
        if keep < 1:
            self._send_error_json(400, 'keep_snapshots must be at least 1')
            return
        if interval < MIN_BACKUP_INTERVAL:
            self._send_error_json(400, f'backup_interval_seconds must be at least {MIN_BACKUP_INTERVAL}')
            return
        if scan_interval < MIN_SCAN_INTERVAL:
            self._send_error_json(400, f'scan_interval_seconds must be at least {MIN_SCAN_INTERVAL}')
            return

        backup_root = (body.get('backup_root') or '').strip()
        if backup_root:
            if not scanner.is_browse_allowed(backup_root):
                self._send_error_json(403, 'backup destination must be under a mounted drive, /mnt, /media, Steam, or your home folder')
                return
            try:
                os.makedirs(backup_root, exist_ok=True)
            except OSError as e:
                self._send_error_json(400, f'backup destination is not usable: {e}')
                return

        run_in_background = bool(body.get('run_in_background', True))

        def mutator(cfg):
            cfg['keep_snapshots'] = keep
            cfg['backup_interval_seconds'] = interval
            cfg['scan_interval_seconds'] = scan_interval
            cfg['backup_root'] = backup_root or None
            cfg['run_in_background'] = run_in_background

        cfg = update_config(mutator)
        # apply the new limit right away instead of waiting for the next cycle
        game_names = {t['game_name'] for t in cfg['auto_backup_targets']}
        for game_name in game_names:
            prune_auto_snapshots(game_name, keep=keep)
        self._send_json({
            'keep_snapshots': keep,
            'backup_interval_seconds': interval,
            'scan_interval_seconds': scan_interval,
            'backup_root': backup_root or default_backup_root(),
            'run_in_background': run_in_background,
        })

    def _handle_backup(self):
        body = self._read_json_body()
        if body is None:
            self._send_error_json(400, 'invalid JSON body')
            return
        src = body.get('path')
        game_name = sanitize_game_name(body.get('game_name'))
        if not src or not scanner.is_browse_allowed(src) or not os.path.exists(src):
            self._send_error_json(403, 'source path not allowed or missing')
            return
        try:
            dest, _status = backup_permanent(src, game_name)
        except OSError as e:
            self._send_error_json(500, f'backup failed: {e}')
            return
        register_auto_target(src, game_name)
        self._send_json({'ok': True, 'dest': dest})

    def _handle_delete_backup(self, qs):
        path = qs.get('path', [None])[0]
        if not path:
            self._send_error_json(400, 'path is required')
            return
        real = os.path.realpath(path)
        root = os.path.realpath(savesteam_root())
        if not real.startswith(root + os.sep) or not os.path.isdir(real):
            self._send_error_json(403, 'not a valid backup path')
            return
        shutil.rmtree(real, ignore_errors=True)
        try:
            os.remove(_meta_path(real))
        except OSError:
            pass
        self._send_json({'ok': True})

    def _handle_keep_backup(self):
        """Strip the '-auto' suffix from a snapshot so it's exempt from
        auto-pruning forever - i.e. promote an auto snapshot to "kept"."""
        body = self._read_json_body()
        if body is None or not body.get('path'):
            self._send_error_json(400, 'path is required')
            return
        src = body['path']
        root = os.path.realpath(savesteam_root())
        real_src = os.path.realpath(src)
        if not real_src.startswith(root + os.sep) or not os.path.isdir(real_src):
            self._send_error_json(403, 'not a valid backup path')
            return
        if not _is_auto_snapshot(os.path.basename(real_src)):
            self._send_error_json(400, 'this snapshot is already kept')
            return
        self._send_json({'ok': True, 'dest': _promote_snapshot(real_src)})

    def _handle_unkeep_backup(self):
        """Undo 'Keep': put a previously-kept snapshot back into the normal
        auto-pruned rotation by re-adding the '-auto' suffix."""
        body = self._read_json_body()
        if body is None or not body.get('path'):
            self._send_error_json(400, 'path is required')
            return
        src = body['path']
        root = os.path.realpath(savesteam_root())
        real_src = os.path.realpath(src)
        if not real_src.startswith(root + os.sep) or not os.path.isdir(real_src):
            self._send_error_json(403, 'not a valid backup path')
            return
        if _is_auto_snapshot(os.path.basename(real_src)):
            self._send_error_json(400, 'this snapshot is already in the automatic rotation')
            return
        dest = real_src + AUTO_SNAPSHOT_SUFFIX
        base_dest = dest
        suffix = 1
        while os.path.exists(dest):
            suffix += 1
            dest = f'{base_dest}-{suffix}'
        os.rename(real_src, dest)
        try:
            os.rename(_meta_path(real_src), _meta_path(dest))
        except OSError:
            pass
        self._send_json({'ok': True, 'dest': dest})

    def _handle_restore(self):
        """Copy a snapshot's contents back onto a destination folder (e.g. a
        freshly reinstalled game's compatdata folder), merging/overwriting.
        Takes a safety snapshot of whatever's currently at the destination
        first, if there's anything there, so a bad restore can be undone."""
        body = self._read_json_body()
        if body is None or not body.get('snapshot_path') or not body.get('dest_path'):
            self._send_error_json(400, 'snapshot_path and dest_path are required')
            return
        snapshot_path = os.path.realpath(body['snapshot_path'])
        dest_path = body['dest_path']
        root = os.path.realpath(savesteam_root())
        if not snapshot_path.startswith(root + os.sep) or not os.path.isdir(snapshot_path):
            self._send_error_json(403, 'not a valid backup snapshot')
            return
        if not scanner.is_browse_allowed(dest_path):
            self._send_error_json(403, 'destination path not allowed')
            return
        meta = read_snapshot_meta(snapshot_path)
        game_name = meta.get('game_name') or os.path.basename(os.path.dirname(snapshot_path))

        safety_dest = None
        if os.path.isdir(dest_path) and os.listdir(dest_path):
            try:
                safety_dest = make_backup_copy(dest_path, game_name, auto=False, tag='-prerestore')
            except OSError as e:
                self._send_error_json(500, f'could not snapshot current state before restoring: {e}')
                return
        try:
            os.makedirs(dest_path, exist_ok=True)
            shutil.copytree(snapshot_path, dest_path, symlinks=True, dirs_exist_ok=True)
        except OSError as e:
            self._send_error_json(500, f'restore failed: {e}')
            return
        self._send_json({'ok': True, 'restored_to': dest_path, 'safety_snapshot': safety_dest})

    def _handle_backup_all(self):
        """Manually snapshot every currently tracked target right now, as a
        permanent (never auto-pruned) copy - regardless of the skip-if-unchanged
        check, since this is an explicit on-demand request."""
        if not _backup_pass_lock.acquire(blocking=False):
            self._send_error_json(409, 'a backup pass is already running - try again shortly')
            return
        try:
            sync_auto_targets()
            cfg = load_config()
            results = [_backup_target_now(t) for t in cfg.get('auto_backup_targets', [])]
        finally:
            _backup_pass_lock.release()
        self._send_json({'results': results})

    def _handle_backup_game(self):
        """Manually snapshot every currently tracked target for ONE game right
        now (the "Sauvegarder maintenant" button on a game's row in the
        Backups tab) - same semantics as "back up all" (permanent, ignores
        the skip-if-unchanged check) but scoped to a single game_name."""
        body = self._read_json_body()
        if body is None or not body.get('game_name'):
            self._send_error_json(400, 'game_name is required')
            return
        game_name = body['game_name']
        cfg = load_config()
        targets = [t for t in cfg.get('auto_backup_targets', []) if t['game_name'] == game_name]
        if not targets:
            self._send_error_json(404, 'no tracked source folder for this game')
            return
        if not _backup_pass_lock.acquire(blocking=False):
            self._send_error_json(409, 'a backup pass is already running - try again shortly')
            return
        try:
            results = [_backup_target_now(t) for t in targets]
        finally:
            _backup_pass_lock.release()
        self._send_json({'results': results})

    def _handle_restore_game(self):
        body = self._read_json_body() or {}
        game = str(body.get('game', ''))
        snapshots = body.get('snapshots')
        if not game or '/' in game or game.startswith('.') or not isinstance(snapshots, list):
            self._send_error_json(400, 'requête invalide')
            return
        try:
            self._send_json({'results': restore_game(game, snapshots)})
        except ValueError as e:
            self._send_error_json(400, str(e))
        except BlockingIOError as e:
            self._send_error_json(409, str(e))
        except OSError as e:
            self._send_error_json(500, f'restauration impossible : {e}')

    def _handle_cover_image(self, qs):
        app_id = qs.get('id', [''])[0]
        path = find_cover_file(app_id) if COVER_ID_RE.match(app_id) else None
        if not path:
            self.send_error(404)
            return
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(path)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    def _handle_sgdb_grids(self, qs):
        app_id = qs.get('id', [''])[0]
        key = sgdb_key()
        if not COVER_ID_RE.match(app_id):
            self._send_error_json(400, 'id invalide')
        elif not key:
            self._send_error_json(400, 'clé SteamGridDB manquante')
        else:
            try:
                self._send_json(sgdb_find_grids(
                    app_id, qs.get('name', [''])[0], qs.get('kind', [''])[0], qs.get('q', [''])[0].strip(), key,
                ))
            except SgdbError as e:
                self._send_error_json(502, str(e))

    def _handle_sgdb_key(self):
        body = self._read_json_body()
        key = (body or {}).get('key', '').strip()
        if not re.fullmatch(r'[A-Za-z0-9]{16,128}', key):
            self._send_error_json(400, 'cette clé ne semble pas valide')
            return
        try:
            _sgdb_get('/search/autocomplete/doom', key)
        except SgdbError as e:
            self._send_error_json(400, str(e))
            return
        save_sgdb_key(key)
        self._send_json({'configured': True})

    def _handle_cover_choice(self):
        body = self._read_json_body() or {}
        app_id, url = str(body.get('id', '')), str(body.get('url', ''))
        if not COVER_ID_RE.match(app_id):
            self._send_error_json(400, 'id invalide')
            return
        try:
            download_cover(app_id, url)
        except SgdbError as e:
            self._send_error_json(502, str(e))
            return
        self._send_json({'ok': True})

    def _handle_open_folder(self):
        """Open a folder in the desktop's file manager (the "Ouvrir le
        dossier" buttons in the Backups tab, and per-target "Ouvrir" in the
        Sauvegarde auto tab) - restricted to either the backup destination
        tree or a path the browse UI would already allow (same allowlist as
        /api/browse), this isn't a general-purpose file-manager launcher."""
        body = self._read_json_body()
        if body is None or not body.get('path'):
            self._send_error_json(400, 'path is required')
            return
        path = body['path']
        root = os.path.realpath(savesteam_root())
        real_path = os.path.realpath(path)
        under_backup_root = real_path == root or real_path.startswith(root + os.sep)
        if not (under_backup_root or scanner.is_browse_allowed(real_path)) or not os.path.isdir(real_path):
            self._send_error_json(403, 'path not allowed or not a directory')
            return
        if not open_in_file_manager(real_path):
            self._send_error_json(500, "impossible d'ouvrir le gestionnaire de fichiers")
            return
        self._send_json({'ok': True})

    def _handle_upsert_compat_group(self):
        """Add a new compatdata group, or edit an existing one (if 'id' is
        given and matches). This is what lets the user repoint the app at a
        drive's compatdata folder themselves if it ever gets renamed/remounted
        differently, without needing a code change."""
        body = self._read_json_body()
        if body is None:
            self._send_error_json(400, 'invalid JSON body')
            return
        label = (body.get('label') or '').strip()
        path = (body.get('path') or '').strip()
        group_id = (body.get('id') or '').strip() or None
        if not label or not path:
            self._send_error_json(400, 'label and path are required')
            return

        result = {'ok': False}

        def mutator(cfg):
            groups = cfg.setdefault('compat_groups', scanner.DEFAULT_COMPAT_GROUPS)
            if group_id:
                for g in groups:
                    if g['id'] == group_id:
                        g['label'] = label
                        g['path'] = path
                        result['ok'] = True
                        result['id'] = group_id
                        return
            new_id = uuid.uuid4().hex
            groups.append({'id': new_id, 'label': label, 'path': path})
            result['ok'] = True
            result['id'] = new_id

        update_config(mutator)
        self._send_json(get_sources(force=True))

    def _handle_delete_compat_group(self, qs):
        group_id = qs.get('id', [None])[0]
        if not group_id:
            self._send_error_json(400, 'id is required')
            return
        cfg = load_config()
        if len(cfg.get('compat_groups', [])) <= 1:
            self._send_error_json(400, 'at least one compatdata group must remain')
            return

        def mutator(cfg2):
            cfg2['compat_groups'] = [g for g in cfg2['compat_groups'] if g['id'] != group_id]

        update_config(mutator)
        self._send_json(get_sources(force=True))

    def _handle_window_closed(self):
        """Called by the native app window when it's closed, so the user's
        'run in background' preference can take effect. If disabled, this
        shuts the whole server down (native_app.py started it and nothing
        else keeps it alive, so the app fully quits); if enabled, it's a
        no-op and auto-backup keeps running as normal."""
        cfg = load_config()
        run_in_background = cfg.get('run_in_background', True)
        self._send_json({'ok': True, 'shutting_down': not run_in_background})
        if run_in_background:
            return
        server_ref = self.server

        def shutdown_when_idle():
            # don't kill the process mid-copy - wait briefly for any in-flight
            # backup pass (scheduled or manual) to finish first
            deadline = time.time() + 30
            while _backup_pass_lock.locked() and time.time() < deadline:
                time.sleep(0.5)
            server_ref.shutdown()

        threading.Thread(target=shutdown_when_idle, daemon=True).start()

    def _handle_set_autostart(self):
        """Enable/disable the systemd --user unit that starts SaveSteam at
        login (separate from 'run in background', which only governs the
        currently-running session). Never starts/stops anything right now -
        only takes effect at the next login, so it can't conflict with
        whatever instance (window, AppImage, service) is running this call."""
        body = self._read_json_body()
        if body is None or 'enabled' not in body:
            self._send_error_json(400, 'enabled is required')
            return
        try:
            status = set_autostart(bool(body['enabled']))
        except (OSError, subprocess.SubprocessError) as e:
            self._send_error_json(500, f'could not update autostart: {e}')
            return
        self._send_json(status)

    def _handle_add_custom_source(self):
        body = self._read_json_body()
        if body is None:
            self._send_error_json(400, 'invalid JSON body')
            return
        label = (body.get('label') or '').strip()
        path = (body.get('path') or '').strip()
        if not label or not path:
            self._send_error_json(400, 'label and path are required')
            return
        if not scanner.is_browse_allowed(path) or not os.path.exists(path):
            self._send_error_json(403, 'path not allowed or missing')
            return
        cfg = load_config()
        new_id = str(int(time.time() * 1000))
        cfg['custom_sources'].append({'id': new_id, 'label': label, 'path': path})
        save_config(cfg)
        register_auto_target(path, sanitize_game_name(label))
        self._send_json(get_sources(force=True))

    def _handle_delete_custom_source(self, qs):
        source_id = qs.get('id', [None])[0]
        if not source_id:
            self._send_error_json(400, 'id is required')
            return
        cfg = load_config()
        cfg['custom_sources'] = [c for c in cfg['custom_sources'] if c['id'] != source_id]
        save_config(cfg)
        self._send_json(get_sources(force=True))

    def _handle_toggle_auto_target(self):
        body = self._read_json_body()
        if body is None or 'id' not in body or 'enabled' not in body:
            self._send_error_json(400, 'id and enabled are required')
            return
        found = {'ok': False}

        def mutator(cfg):
            for t in cfg['auto_backup_targets']:
                if t['id'] == body['id']:
                    t['enabled'] = bool(body['enabled'])
                    found['ok'] = True

        cfg = update_config(mutator)
        if not found['ok']:
            self._send_error_json(404, 'target not found')
            return
        self._send_json({'targets': cfg['auto_backup_targets']})

    def _handle_delete_auto_target(self, qs):
        target_id = qs.get('id', [None])[0]
        if not target_id:
            self._send_error_json(400, 'id is required')
            return

        def mutator(cfg):
            # Remembered, so the next sync doesn't re-add it on its own and
            # the user can bring it back from "Non suivis".
            stopped = cfg.setdefault('stopped_targets', [])
            for t in cfg['auto_backup_targets']:
                if t['id'] == target_id and not any(s['source_path'] == t['source_path'] for s in stopped):
                    stopped.append({'source_path': t['source_path'], 'game_name': t['game_name'],
                                    'stopped_at': time.time()})
            cfg['auto_backup_targets'] = [t for t in cfg['auto_backup_targets'] if t['id'] != target_id]

        cfg = update_config(mutator)
        self._send_json({'targets': cfg['auto_backup_targets']})

    def _handle_track_folder(self):
        body = self._read_json_body() or {}
        try:
            track_untracked_folder(str(body.get('source_path', '')))
        except ValueError as e:
            self._send_error_json(400, str(e))
            return
        self._send_json({'ok': True})


def main():
    cfg = load_config()
    port = int(os.environ.get('SAVETEAM_PORT', cfg.get('port', 8765)))
    threading.Thread(target=auto_backup_loop, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'SaveSteam listening on http://127.0.0.1:{port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print('SaveSteam stopped.')


if __name__ == '__main__':
    main()
