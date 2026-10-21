#!/usr/bin/env python3
"""SaveSteam: local web app to browse Steam/Proton save prefixes across
drives and back up chosen folders into SaveSteam/<game>/<timestamp>/.

Standard-library only. Binds to 127.0.0.1 so it's only reachable locally.
"""
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
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
    xdg-open. Fire-and-forget - doesn't wait for the file manager to exit."""
    try:
        subprocess.Popen(['xdg-open', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
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
                (e for e in it if e.is_dir() and e.name.endswith(AUTO_SNAPSHOT_SUFFIX)),
                key=lambda e: e.name,
                reverse=True,
            )
    except OSError:
        return
    for stale in auto_snaps[keep:]:
        shutil.rmtree(stale.path, ignore_errors=True)
        try:
            os.remove(_meta_path(stale.path))
        except OSError:
            pass


_backup_pass_lock = threading.Lock()


def run_auto_backup_cycle():
    if not _backup_pass_lock.acquire(blocking=False):
        return []  # a pass (scheduled or manual) is already running - don't double up
    try:
        sync_auto_targets()
        cfg = load_config()
        keep = cfg.get('keep_snapshots', AUTO_BACKUP_KEEP_DEFAULT)
        changed = False
        results = []
        error_games = []
        for target in cfg.get('auto_backup_targets', []):
            if not target.get('enabled', True):
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
    """Force an immediate, permanent snapshot of one auto-backup target,
    regardless of whether its signature has changed - shared by "back up all
    now" and the per-game "Sauvegarder maintenant" button."""
    if not target.get('enabled', True):
        return {'game': target['game_name'], 'status': 'paused'}
    src = target['source_path']
    if not os.path.exists(src) or not scanner.is_browse_allowed(src):
        return {'game': target['game_name'], 'status': 'missing'}
    try:
        dest = make_backup_copy(src, target['game_name'], auto=False)
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
    return {'game': target['game_name'], 'status': 'backed_up', 'dest': dest}


def register_auto_target(src, game_name):
    def mutator(cfg):
        for t in cfg['auto_backup_targets']:
            if t['source_path'] == src:
                t['game_name'] = game_name
                t['enabled'] = True
                return
        cfg['auto_backup_targets'].append(_new_auto_target(src, game_name))
    update_config(mutator)


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

    def mutator(cfg):
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
        existing_paths = {t['source_path'] for t in cfg['auto_backup_targets']}

        for cs in sources['custom_sources']:
            if not cs['exists'] or cs['path'] in existing_paths:
                continue
            cfg['auto_backup_targets'].append(_new_auto_target(cs['path'], sanitize_game_name(cs['label'])))
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
                try:
                    candidates = scanner.auto_trackable_save_dirs(entry['path'], exe_path=entry.get('exe_path'))
                except OSError:
                    continue
                for c in candidates:
                    game_name = base_name if len(candidates) == 1 else sanitize_game_name(f'{base_name} ({c["name"]})')
                    cfg['auto_backup_targets'].append(_new_auto_target(c['path'], game_name))
                    existing_paths.add(c['path'])
                    existing_names.add(game_name)

    return update_config(mutator)


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
                                  'targets': cfg['auto_backup_targets']})
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
            elif path == '/api/backup-all':
                self._handle_backup_all()
            elif path == '/api/backup-game':
                self._handle_backup_game()
            elif path == '/api/open-folder':
                self._handle_open_folder()
            elif path == '/api/custom-source':
                self._handle_add_custom_source()
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
                is_auto = se.name.endswith(AUTO_SNAPSHOT_SUFFIX)
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
            dest = make_backup_copy(src, game_name, auto=False)
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
        if not real_src.endswith(AUTO_SNAPSHOT_SUFFIX):
            self._send_error_json(400, 'this snapshot is already kept')
            return
        dest = real_src[: -len(AUTO_SNAPSHOT_SUFFIX)]
        base_dest = dest
        suffix = 1
        while os.path.exists(dest):
            suffix += 1
            dest = f'{base_dest}-kept{suffix}'
        os.rename(real_src, dest)
        try:
            os.rename(_meta_path(real_src), _meta_path(dest))
        except OSError:
            pass
        self._send_json({'ok': True, 'dest': dest})

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
        if real_src.endswith(AUTO_SNAPSHOT_SUFFIX):
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
            cfg['auto_backup_targets'] = [t for t in cfg['auto_backup_targets'] if t['id'] != target_id]

        cfg = update_config(mutator)
        self._send_json({'targets': cfg['auto_backup_targets']})


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
