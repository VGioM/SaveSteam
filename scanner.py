"""Builds the list of game/save sources (Steam compatdata + custom folders)
and resolves opaque compatdata IDs to human-readable game names."""
import glob
import os
import re
import struct
import time

import vdf

STEAM_ROOT = os.path.expanduser('~/.local/share/Steam')
DRIVE_ROOT = '/run/media/system/Samsung_870'

# Compat groups are normally stored in config.json (key 'compat_groups') so
# the user can add/rename/repoint them from the app (Settings tab) without
# needing a code change - e.g. if a drive gets remounted under a new label.
# This is only the fallback used the very first time config.json is created.
DEFAULT_COMPAT_GROUPS = [
    {'id': 'default-870', 'label': 'Samsung_870', 'path': os.path.join(DRIVE_ROOT, 'compatdata 870')},
    {'id': 'default-860', 'label': 'Samsung_860', 'path': os.path.join(DRIVE_ROOT, 'compatdata 860')},
    {'id': 'default-home', 'label': 'Home (internal disk)', 'path': os.path.join(DRIVE_ROOT, 'compatdata home')},
]

BROWSE_ROOTS = [os.path.realpath(p) for p in (
    '/run/media',
    '/media',
    '/mnt',
    STEAM_ROOT,
    os.path.expanduser('~'),
)]  # realpath is required: /home is a symlink to /var/home on this system,
    # and is_browse_allowed() compares against a realpath'd input path.
    # Broadened to cover any mounted drive, not just this PC's current layout,
    # so a renamed/relabeled/newly-added drive still works once its compatdata
    # group is (re)configured in Settings.

# Where to look for *other* Steam libraries when the user manually asks for a
# disk scan (Settings tab) - e.g. after a drive got relabeled or a new one was
# plugged in. Purely a discovery aid; never auto-applied.
MOUNT_SEARCH_ROOTS = ['/run/media', '/media', '/mnt']
DISCOVER_MAX_DEPTH = 6
DISCOVER_MAX_DIRS_VISITED = 50000
_PRUNE_DIRNAMES = {'common', 'workshop', 'shadercache', 'downloading', 'temp'}


def discover_compatdata_dirs():
    """Manually-triggered, read-only search across mounted drives for any
    steamapps/compatdata folder - including ones not currently referenced by
    COMPAT_GROUPS or libraryfolders.vdf. Prunes known-huge/irrelevant Steam
    subtrees (game install folders, workshop content, shader cache) so it
    stays fast instead of crawling entire game libraries."""
    found = []
    seen_real = set()
    visited = 0

    def record(path):
        real = os.path.realpath(path)
        if real in seen_real or not os.path.isdir(path):
            return
        seen_real.add(real)
        try:
            entry_count = sum(1 for _ in os.scandir(path))
        except OSError:
            entry_count = None
        found.append({'path': path, 'real_path': real, 'entry_count': entry_count})

    record(os.path.join(STEAM_ROOT, 'steamapps', 'compatdata'))
    record(os.path.expanduser('~/.var/app/com.valvesoftware.Steam/data/Steam/steamapps/compatdata'))

    for root in MOUNT_SEARCH_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, _filenames in os.walk(root, topdown=True, onerror=lambda e: None):
            visited += 1
            if visited > DISCOVER_MAX_DIRS_VISITED:
                dirnames[:] = []
                continue
            depth = dirpath[len(root):].count(os.sep)
            if depth >= DISCOVER_MAX_DEPTH:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRNAMES and not d.startswith('.')]
            if os.path.basename(dirpath) == 'compatdata':
                record(dirpath)
                dirnames[:] = []  # nothing useful nested inside a compatdata folder itself
    return found

_TOOL_NAME_RE = re.compile(
    r'^(Proton\b|Steam Linux Runtime|SteamVR|Steamworks Common Redistributables)',
    re.IGNORECASE,
)
_SORT_HACK_RE = re.compile(r'^z\s+')


def _clean_display_name(name):
    return _SORT_HACK_RE.sub('', name).strip()


def build_name_map():
    """Returns dict: id (str) -> {"name", "kind", "path"} where kind is one
    of steam / tool / nonsteam."""
    name_map = {}

    lf_path = os.path.join(STEAM_ROOT, 'steamapps', 'libraryfolders.vdf')
    library_paths = {STEAM_ROOT}
    if os.path.isfile(lf_path):
        try:
            with open(lf_path, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
            for p in vdf.iter_library_paths(text):
                library_paths.add(p)
        except OSError:
            pass

    for lib in library_paths:
        for acf in glob.glob(os.path.join(lib, 'steamapps', 'appmanifest_*.acf')):
            try:
                with open(acf, 'r', encoding='utf-8', errors='replace') as f:
                    kv = vdf.parse_text_kv_flat(f.read())
            except OSError:
                continue
            appid = kv.get('appid')
            name = kv.get('name')
            if not appid or not name:
                continue
            kind = 'tool' if _TOOL_NAME_RE.match(name) else 'steam'
            name_map[appid] = {
                'name': _clean_display_name(name),
                'kind': kind,
                'path': acf,
                # Where this app's compatdata prefix *would* live once it's
                # launched for the first time - lets list_sources() show
                # installed-but-never-run games right away, matched to the
                # right compat group by comparing real paths.
                'library_compatdata': os.path.join(lib, 'steamapps', 'compatdata'),
            }

    userdata_glob = os.path.join(STEAM_ROOT, 'userdata', '*', 'config', 'shortcuts.vdf')
    for sc_path in glob.glob(userdata_glob):
        try:
            shortcuts = vdf.parse_shortcuts_vdf(sc_path)
        except (OSError, ValueError, struct.error):
            continue
        for sc in shortcuts:
            appid = sc.get('appid')
            appname = sc.get('AppName')
            if appid is None or not appname:
                continue
            key = str(appid)
            if key not in name_map:
                name_map[key] = {
                    'name': _clean_display_name(appname),
                    'kind': 'nonsteam',
                    'path': sc_path,
                    'exe': (sc.get('Exe') or '').strip('"') or None,
                }
    return name_map


def resolve_id(id_str, name_map):
    entry = name_map.get(id_str)
    if entry:
        return entry['name'], entry['kind']
    if id_str == '0':
        return '(empty / unassigned prefix)', 'special'
    return None, 'unknown'


def _exe_outside_prefix(exe_path, prefix_path):
    """Whether a non-Steam shortcut's target executable lives outside its own
    compatdata prefix - if so, the app (and anything it installs next to
    itself, e.g. a launcher's own downloaded games) survives a prefix
    wipe/reinstall untouched; if not, it's fragile and would be lost."""
    if not exe_path:
        return None
    real_exe = os.path.realpath(exe_path)
    real_prefix = os.path.realpath(prefix_path)
    return not (real_exe == real_prefix or real_exe.startswith(real_prefix + os.sep))


def _dir_mtime(path):
    try:
        return os.stat(path, follow_symlinks=True).st_mtime
    except OSError:
        return None


def _tracked_count(full_path, name, target_paths, target_names):
    # Path-prefix match covers a target living inside this entry's own
    # compatdata folder (the common case); name match covers a target whose
    # actual save folder lives entirely outside it (e.g. a shortcut whose
    # .exe - and saves - live on an external drive, matched to this entry by
    # game name instead - see [[battlenet_ubisoft_game_drive]]).
    prefix = full_path + os.sep
    by_path = sum(1 for p in target_paths if p == full_path or p.startswith(prefix))
    if not name:
        return by_path
    by_name = sum(1 for n in target_names if n == name or n.startswith(name + ' ('))
    return max(by_path, by_name)


def _not_yet_launched_entries(group_path, existing_ids, name_map):
    """Steam games that are installed (have an appmanifest) but have never
    been launched, so no compatdata prefix has been created for them yet in
    this group - there's no save data to back up, but the game should still
    show up as soon as it's installed instead of only after its first run."""
    real_group = os.path.realpath(group_path)
    entries = []
    for appid, info in name_map.items():
        if info['kind'] != 'steam' or appid in existing_ids:
            continue
        lib_compatdata = info.get('library_compatdata')
        if not lib_compatdata or os.path.realpath(lib_compatdata) != real_group:
            continue
        entries.append({
            'id': appid,
            'path': None,
            'real_path': None,
            'is_symlink': False,
            'name': info['name'],
            'kind': 'steam',
            'mtime': None,
            'tracked_count': 0,
            'exe_path': None,
            'exe_outside_prefix': None,
            'not_launched': True,
        })
    entries.sort(key=lambda e: (e['name'] or '').lower())
    return entries


def list_sources(config):
    name_map = build_name_map()
    targets = config.get('auto_backup_targets', [])
    target_paths = [t['source_path'] for t in targets]
    target_names = [t['game_name'] for t in targets]
    groups = []

    for group_cfg in config.get('compat_groups', DEFAULT_COMPAT_GROUPS):
        group_id, label, group_path = group_cfg['id'], group_cfg['label'], group_cfg['path']
        mounted = os.path.isdir(group_path)
        entries = []
        if mounted:
            try:
                ids = sorted(os.listdir(group_path))
            except OSError:
                ids = []
            for id_str in ids:
                full_path = os.path.join(group_path, id_str)
                name, kind = resolve_id(id_str, name_map)
                is_symlink = os.path.islink(full_path)
                exe_path = name_map.get(id_str, {}).get('exe') if kind == 'nonsteam' else None
                entries.append({
                    'id': id_str,
                    'path': full_path,
                    'real_path': os.path.realpath(full_path) if is_symlink else full_path,
                    'is_symlink': is_symlink,
                    'name': name,
                    'kind': kind,
                    'mtime': _dir_mtime(full_path),
                    'tracked_count': _tracked_count(full_path, name, target_paths, target_names),
                    'exe_path': exe_path,
                    'exe_outside_prefix': _exe_outside_prefix(exe_path, full_path),
                })
            entries.extend(_not_yet_launched_entries(group_path, set(ids), name_map))
        groups.append({
            'id': group_id,
            'label': label,
            'path': group_path,
            'mounted': mounted,
            'entries': entries,
        })

    custom = []
    for cs in config.get('custom_sources', []):
        exists = os.path.exists(cs['path'])
        custom.append({
            'id': cs['id'],
            'label': cs['label'],
            'path': cs['path'],
            'exists': exists,
            'mtime': _dir_mtime(cs['path']) if exists else None,
        })

    return {'groups': groups, 'custom_sources': custom, 'scanned_at': time.time()}


def is_browse_allowed(path):
    real = os.path.realpath(path)
    return any(real == root or real.startswith(root + os.sep) for root in BROWSE_ROOTS)


# Where non-Steam (Proton) games conventionally keep per-game writable data,
# relative to a compatdata/<id> root. Used to suggest a narrow save folder to
# back up instead of the whole multi-GB prefix (which also holds a full Wine
# Windows install, DirectX caches, etc. - none of that is a save file).
_SAVE_DIR_PARENTS = [
    ('AppData/Roaming', ('pfx', 'drive_c', 'users', 'steamuser', 'AppData', 'Roaming')),
    ('AppData/Local', ('pfx', 'drive_c', 'users', 'steamuser', 'AppData', 'Local')),
    ('AppData/LocalLow', ('pfx', 'drive_c', 'users', 'steamuser', 'AppData', 'LocalLow')),
    ('Documents', ('pfx', 'drive_c', 'users', 'steamuser', 'Documents')),
    ('Documents/My Games', ('pfx', 'drive_c', 'users', 'steamuser', 'Documents', 'My Games')),
    ('Saved Games', ('pfx', 'drive_c', 'users', 'steamuser', 'Saved Games')),
]

# Vendor/OS clutter that shows up in those same folders but is never a game
# save - excluded from auto-tracking (still shown, de-emphasized, in the
# manual picker - a game could theoretically be named this).
_LIKELY_JUNK_NAMES = {
    'microsoft', 'nvidia corporation', 'temp', 'packages', 'adobe',
    'virtualstore', 'connectivity', 'crashdumps', 'd3dscache', 'amd',
    # standard Windows user-shell stub folders Wine creates in every prefix -
    # basically never where a game actually writes its saves.
    'downloads', 'music', 'pictures', 'videos', 'templates', 'public',
    'desktop', 'favorites', 'links', 'contacts', 'searches',
    # shader/API caches, not save data.
    'dxvk', 'dxvk-cache', 'vkd3d-proton', 'mesa_shader_cache', 'gpucache',
    # 'My Games' is scanned as its own parent below (its actual per-game
    # subfolders are the real candidates) - listing it here too, as a bare
    # child of Documents, would double-track it alongside those subfolders.
    'my games',
}

# Some non-Steam shortcuts (private-server game launchers especially, per
# real examples found on this machine: Warmane, Tony Hawk's Pro Skater
# reTHAWed) point their Exe at a .exe living entirely outside the Proton
# prefix (see [[battlenet_ubisoft_game_drive]] memory) and keep their actual
# save/config data right next to that .exe instead of under the prefix's
# AppData/Documents - classic WoW-client folders (WTF holds account/character
# settings, Interface holds AddOns/SavedVariables) are the most common case.
_EXTERNAL_SAVE_DIR_NAMES = {
    'wtf', 'interface', 'save', 'saves', 'savegames', 'savedgames',
    'profiles', 'userdata', 'settings',
}


def _dir_size(path):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for fn in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, fn)).st_size
            except OSError:
                pass
    return total


def suggest_save_dirs(prefix_root, exe_path=None):
    """Best-effort scan of a Proton prefix's conventional per-game data
    locations, so the user can pick the actual save folder instead of the
    entire prefix. Heuristic, not exhaustive - games can save anywhere -
    this only narrows the common cases down for browsing.

    If exe_path is given (a non-Steam shortcut's target .exe) and it lives
    outside prefix_root, also checks common save-folder names right next to
    that .exe - some launchers (see _EXTERNAL_SAVE_DIR_NAMES) keep saves
    there instead of under the prefix's AppData/Documents."""
    candidates = []
    seen_real = set()

    def add_candidates(parent, label):
        try:
            children = list(os.scandir(parent))
        except OSError:
            return
        for entry in children:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            real = os.path.realpath(entry.path)
            if real in seen_real:
                continue
            seen_real.add(real)
            candidates.append({
                'label': f'{label} / {entry.name}',
                'path': entry.path,
                'name': entry.name,
                'size_bytes': _dir_size(entry.path),
                'likely_junk': entry.name.lower() in _LIKELY_JUNK_NAMES,
            })

    for label, rel_parts in _SAVE_DIR_PARENTS:
        add_candidates(os.path.join(prefix_root, *rel_parts), label)

    if exe_path and _exe_outside_prefix(exe_path, prefix_root):
        exe_dir = os.path.dirname(os.path.realpath(exe_path))
        try:
            children = list(os.scandir(exe_dir))
        except OSError:
            children = []
        for entry in children:
            if entry.name.lower() not in _EXTERNAL_SAVE_DIR_NAMES:
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            real = os.path.realpath(entry.path)
            if real in seen_real:
                continue
            seen_real.add(real)
            candidates.append({
                'label': f'Dossier du jeu / {entry.name}',
                'path': entry.path,
                'name': entry.name,
                'size_bytes': _dir_size(entry.path),
                'likely_junk': False,
            })

    candidates.sort(key=lambda c: (c['likely_junk'], -c['size_bytes']))
    steamuser_root = os.path.join(prefix_root, 'pfx', 'drive_c', 'users', 'steamuser')
    return {
        'candidates': candidates,
        'steamuser_root': steamuser_root if os.path.isdir(steamuser_root) else prefix_root,
    }


def auto_trackable_save_dirs(prefix_root, exe_path=None):
    """Candidates from suggest_save_dirs() confident enough to track with no
    user input: not vendor/OS clutter, and not empty (nothing to back up yet -
    re-checked on every sync, so it starts getting tracked the moment the game
    actually writes something there)."""
    return [
        c for c in suggest_save_dirs(prefix_root, exe_path=exe_path)['candidates']
        if not c['likely_junk'] and c['size_bytes'] > 0
    ]
