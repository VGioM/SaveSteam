"""Minimal parsers for Valve's text and binary VDF formats (stdlib only)."""
import re
import struct

_KV_RE = re.compile(r'"((?:[^"\\]|\\.)*)"\s*"((?:[^"\\]|\\.)*)"')


def parse_text_kv_flat(text):
    """Return a flat dict of key -> value for the first-level key/value pairs
    found anywhere in a text VDF blob. Good enough for appmanifest .acf files
    where we only care about top-level scalar fields like "appid" / "name"."""
    return {k: v for k, v in _KV_RE.findall(text)}


def iter_library_paths(libraryfolders_text):
    """Yield each Steam library root path referenced in libraryfolders.vdf."""
    for m in re.finditer(r'"path"\s*"((?:[^"\\]|\\.)*)"', libraryfolders_text):
        yield m.group(1).replace('\\\\', '\\').replace('\\"', '"')


def parse_shortcuts_vdf(path):
    """Parse a binary shortcuts.vdf file, returning a list of dicts with at
    least 'appid' (unsigned 32-bit, matching compatdata folder names) and
    'AppName' when present."""
    with open(path, 'rb') as f:
        data = f.read()

    def read_cstr(i):
        j = data.index(b'\x00', i)
        return data[i:j].decode('utf-8', 'replace'), j + 1

    if not data or data[0:1] != b'\x00':
        return []
    i = 1
    _, i = read_cstr(i)  # "shortcuts"
    results = []
    n = len(data)
    while i < n:
        t = data[i]
        i += 1
        if t == 0x08:
            break
        _key, i = read_cstr(i)  # entry index, e.g. "0"
        if t != 0x00:
            break
        entry = {}
        while i < n:
            t2 = data[i]
            i += 1
            if t2 == 0x08:
                break
            k2, i = read_cstr(i)
            if t2 == 0x01:  # string
                v2, i = read_cstr(i)
                entry[k2] = v2
            elif t2 == 0x02:  # int32
                v2 = struct.unpack_from('<i', data, i)[0]
                i += 4
                entry[k2] = v2
            elif t2 == 0x00:  # nested object (e.g. "tags") - skip generically
                depth = 1
                while depth > 0 and i < n:
                    t3 = data[i]
                    i += 1
                    if t3 == 0x08:
                        depth -= 1
                        continue
                    _k3, i = read_cstr(i)
                    if t3 == 0x01:
                        _v3, i = read_cstr(i)
                    elif t3 == 0x02:
                        i += 4
                    elif t3 == 0x00:
                        depth += 1
            else:
                break
        if 'appid' in entry:
            entry['appid'] = entry['appid'] & 0xFFFFFFFF
        results.append(entry)
    return results
