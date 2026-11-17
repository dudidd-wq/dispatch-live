#!/usr/bin/env python3
# ============================================================
#   Dispatch LIVE - local configuration
#   ----------------------------------------------------------
#   Three layers, later ones win:
#     1. DEFAULTS below        - identical to the behaviour before config
#                                existed, so upgrading changes nothing
#     2. config.json           - optional, shared (may be committed)
#     3. config.local.json     - this machine only, written by the settings
#                                screen, never committed (.gitignore)
#   Used by server.py (deck) and brain.py (traffic). Standard library only.
# ============================================================
import copy
import json
import os
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
LOCAL_PATH = os.path.join(ROOT, "config.local.json")
MAX_BYTES = 200 * 1024

# Google gives 5,000 free Compute Routes Pro events a month. No setting may
# push the brain past this, whatever the file says.
FREE_TIER = 5000
HARD_MONTHLY_CEILING = 4900

DEFAULTS = {
    "version": 1,
    # Browser preferences mirrored to this file so every browser and device
    # that opens the deck sees the same setup. Keys are the deck's own
    # localStorage names; None means "not set, use the deck's default".
    "prefs": {
        "dl-city": None, "dl-lat": None, "dl-lng": None,
        "dl-shelter-sec": None,
        "dl-tv": None, "dl-featured": None, "dl-featured2": None,
        "dl-news-off": None, "dl-news-region": None, "dl-news-relevant": None,
        # dl-sound is deliberately NOT here: muting one screen must not mute the others
    },
    "channels": {"custom": []},      # [{id, name, type: hls|iframe, src}]
    "news": {"custom": []},          # [{id, name, url, region: il|world}]
    "markets": [
        ["^TA125.TA", "ת״א 125"], ["^GSPC", "S&P 500"], ["^IXIC", "נאסד״ק"],
        ["USDILS=X", "דולר"], ["EURILS=X", "אירו"], ["BZ=F", "נפט ברנט"],
    ],
    "traffic": {
        "minDelay": 10,              # minutes; below this a road is "flowing"
        "heavyDelay": 20,
        "monthlyCap": 4500,
        "dailyCap": 200,
        "rush": [[7, 9], [16, 19]],  # inclusive hour ranges, Israel time
    },
    "display": {"fontScale": 1.0},
}

PREF_KEYS = tuple(DEFAULTS["prefs"].keys())
_lock = threading.Lock()


def deep_merge(base, over):
    """Return base overlaid with over. Dicts merge; everything else replaces."""
    if not isinstance(base, dict) or not isinstance(over, dict):
        return copy.deepcopy(over)
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) \
            else copy.deepcopy(v)
    return out


def _read(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _num(v, lo, hi, default):
    try:
        v = float(v)
    except Exception:
        return default
    if v != v:                      # NaN
        return default
    return max(lo, min(hi, v))


def _text(v, limit):
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        return ""
    # drop lone surrogates and control characters: they cannot be sent as UTF-8
    s = str(v).encode("utf-8", "ignore").decode("utf-8", "ignore")
    return "".join(ch for ch in s if ch >= " " or ch in "\t")[:limit]


def _stable_id(prefix, src):
    """An id derived from the address, not from the position in the list -
    deleting one custom channel must not hand its on/off state to the next."""
    import hashlib
    return prefix + hashlib.sha1(src.encode("utf-8", "replace")).hexdigest()[:8]


def _id_list(v):
    """A pref that the deck JSON.parses into a list of ids. Returns the JSON
    text, or None when it is anything else - the deck builds a Set from it and
    a wrong type used to kill the whole page."""
    try:
        d = json.loads(v) if isinstance(v, str) else v
        if isinstance(d, list) and all(isinstance(x, str) and 0 < len(x) <= 60 for x in d) and len(d) <= 200:
            return json.dumps(d, ensure_ascii=False)
    except Exception:
        pass
    return None


def _pref(key, v):
    """Type-check one preference; None means 'not set'."""
    import re
    if v is None:
        return None
    if key in ("dl-tv", "dl-news-off"):
        return _id_list(v)
    v = _text(v, 80).strip()
    if not v:
        return None
    if key in ("dl-lat", "dl-lng"):
        try:
            f = float(v)
            return v if f == f and -180 <= f <= 180 else None
        except Exception:
            return None
    if key == "dl-shelter-sec":
        return v if v.isdigit() and 0 < int(v) <= 3600 else None
    if key == "dl-news-region":
        return v if v in ("all", "il", "world") else None
    if key == "dl-news-relevant":
        return v if v in ("0", "1") else None
    if key in ("dl-featured", "dl-featured2"):
        return v if re.match(r"^[A-Za-z0-9_\-]{1,40}$", v) else None
    return v[:60]                                   # dl-city


def sanitize(cfg):
    """Clamp and repair a merged config so no file content can break the deck
    or the budget. Unknown keys are dropped; bad values fall back to defaults."""
    d = DEFAULTS
    out = copy.deepcopy(d)

    prefs = cfg.get("prefs") if isinstance(cfg.get("prefs"), dict) else {}
    for k in PREF_KEYS:
        out["prefs"][k] = _pref(k, prefs.get(k))
    if (out["prefs"]["dl-lat"] is None) != (out["prefs"]["dl-lng"] is None):
        out["prefs"]["dl-lat"] = out["prefs"]["dl-lng"] = None      # half a coordinate is no coordinate

    t = cfg.get("traffic") if isinstance(cfg.get("traffic"), dict) else {}
    dt = d["traffic"]
    mn = int(_num(t.get("minDelay"), 1, 120, dt["minDelay"]))
    hv = int(_num(t.get("heavyDelay"), 2, 240, dt["heavyDelay"]))
    if hv <= mn:
        hv = mn + 1
    rush = []
    for pair in (t.get("rush") if isinstance(t.get("rush"), list) else dt["rush"])[:4]:
        try:
            a, b = int(pair[0]), int(pair[1])
            if 0 <= a <= b <= 23:
                rush.append([a, b])
        except Exception:
            pass
    out["traffic"] = {
        "minDelay": mn, "heavyDelay": hv,
        "monthlyCap": int(_num(t.get("monthlyCap"), 0, HARD_MONTHLY_CEILING, dt["monthlyCap"])),
        "dailyCap": int(_num(t.get("dailyCap"), 0, 400, dt["dailyCap"])),
        "rush": rush if rush else copy.deepcopy(dt["rush"]),
    }

    markets = []
    for row in (cfg.get("markets") if isinstance(cfg.get("markets"), list) else [])[:12]:
        try:
            sym, name = _text(row[0], 24).strip(), _text(row[1], 40).strip()
            if sym and name and all(c.isalnum() or c in "^=.-" for c in sym):
                markets.append([sym, name])
        except Exception:
            pass
    out["markets"] = markets if markets else copy.deepcopy(d["markets"])

    chans = []
    src_list = ((cfg.get("channels") or {}).get("custom")
                if isinstance(cfg.get("channels"), dict) else None) or []
    for i, c in enumerate(src_list[:12] if isinstance(src_list, list) else []):
        if not isinstance(c, dict):
            continue
        src = _text(c.get("src"), 600).strip()
        name = _text(c.get("name"), 40).strip()
        if name and src.lower().startswith(("http://", "https://")):
            chans.append({"id": _stable_id("c", src), "name": name,
                          "type": "iframe" if c.get("type") == "iframe" else "hls", "src": src})
    seen, uniq = set(), []
    for c in chans:                       # the same address twice is one channel
        if c["id"] not in seen:
            seen.add(c["id"]); uniq.append(c)
    old_ch = {"custom%d" % (i + 1): c["id"] for i, c in enumerate(chans)}     # ids written by v76
    chans = uniq
    out["channels"] = {"custom": chans}

    feeds = []
    src_list = ((cfg.get("news") or {}).get("custom")
                if isinstance(cfg.get("news"), dict) else None) or []
    for i, c in enumerate(src_list[:20] if isinstance(src_list, list) else []):
        if not isinstance(c, dict):
            continue
        url = _text(c.get("url"), 600).strip()
        name = _text(c.get("name"), 40).strip()
        if name and url.lower().startswith(("http://", "https://")):
            feeds.append({"id": _stable_id("f", url), "name": name, "url": url,
                          "region": "world" if c.get("region") == "world" else "il"})
    seen, uniq = set(), []
    for f in feeds:
        if f["id"] not in seen:
            seen.add(f["id"]); uniq.append(f)
    old_feed = {"cfeed%d" % (i + 1): f["id"] for i, f in enumerate(feeds)}
    out["news"] = {"custom": uniq}

    # v76 numbered custom entries by position (custom1, cfeed1). Carry any such
    # id still stored in the preferences over to the address-based id.
    def remap(key, table):
        try:
            ids = json.loads(out["prefs"][key]) if out["prefs"].get(key) else None
        except Exception:
            ids = None
        if ids and any(i in table for i in ids):
            out["prefs"][key] = json.dumps([table.get(i, i) for i in ids], ensure_ascii=False)
    remap("dl-tv", old_ch)
    remap("dl-news-off", old_feed)
    for k in ("dl-featured", "dl-featured2"):
        if out["prefs"].get(k) in old_ch:
            out["prefs"][k] = old_ch[out["prefs"][k]]

    disp = cfg.get("display") if isinstance(cfg.get("display"), dict) else {}
    out["display"] = {"fontScale": round(_num(disp.get("fontScale"), 0.8, 1.5, 1.0), 2)}
    return out


def load_local():
    return _read(LOCAL_PATH)


def load():
    """The effective configuration: defaults < config.json < config.local.json.
    Never raises: a file that cannot be merged or cleaned (absurd nesting, a
    type nobody expected) yields the defaults, flagged, instead of taking the
    deck down."""
    try:
        merged = deep_merge(deep_merge(DEFAULTS, _read(CONFIG_PATH)), load_local())
        return sanitize(merged)
    except Exception as e:
        out = copy.deepcopy(DEFAULTS)
        out["configError"] = "%s: %s" % (type(e).__name__, str(e)[:80])
        return out


def save_local(obj):
    """Replace config.local.json. Returns (ok, message)."""
    if not isinstance(obj, dict):
        return False, "config must be a JSON object"
    try:
        raw = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception as e:
        return False, "config is not valid JSON data: %s" % str(e)[:80]
    if len(raw.encode("utf-8", "replace")) > MAX_BYTES:
        return False, "config too large"
    with _lock:
        return _write_local(raw)


def _write_local(raw):
    import time as _t
    tmp = LOCAL_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(raw)
        for attempt in range(5):
            try:
                os.replace(tmp, LOCAL_PATH)
                return True, "saved"
            except PermissionError:          # Windows: another process has the file open for a moment
                _t.sleep(0.05 * (attempt + 1))
        return False, "config.local.json is in use by another program"
    except Exception as e:
        return False, "could not write config.local.json: %s" % e


def patch_local(patch):
    """Deep-merge a partial object into config.local.json. The read, the merge
    and the write happen under one lock, so two saves at the same moment both
    land instead of one silently replacing the other."""
    if not isinstance(patch, dict):
        return False, "patch must be a JSON object"
    with _lock:
        try:
            merged = deep_merge(load_local(), patch)
            raw = json.dumps(merged, ensure_ascii=False, indent=2)
        except Exception as e:
            return False, "config could not be merged: %s" % str(e)[:80]
        if len(raw.encode("utf-8", "replace")) > MAX_BYTES:
            return False, "config too large"
        return _write_local(raw)


def mtime():
    """Changes whenever either file changes; used by brain.py to hot-reload."""
    out = 0.0
    for p in (CONFIG_PATH, LOCAL_PATH):
        try:
            out = max(out, os.path.getmtime(p))
        except OSError:
            pass
    return out


class _RotatingLog:
    """A stdout/stderr replacement that writes to logs/<name>.log and rolls
    the file at ~1 MB, keeping three old copies. Used when the deck runs in
    the background with no console window (pythonw has no stdout at all)."""

    def __init__(self, name, limit=1024 * 1024, keep=3):
        self.dir = os.path.join(ROOT, "logs")
        self.path = os.path.join(self.dir, name + ".log")
        self.limit, self.keep = limit, keep
        self._lock = threading.Lock()
        os.makedirs(self.dir, exist_ok=True)

    def write(self, text):
        if not text:
            return 0
        import time as _t
        with self._lock:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.limit:
                    for i in range(self.keep - 1, 0, -1):
                        a, b = "%s.%d" % (self.path, i), "%s.%d" % (self.path, i + 1)
                        if os.path.exists(a):
                            os.replace(a, b)
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8") as f:
                    for line in str(text).splitlines(True):
                        f.write((_t.strftime("%Y-%m-%d %H:%M:%S ") if line.strip() else "") + line)
            except Exception:
                pass
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


def setup_logging(name):
    """Send prints and tracebacks to logs/<name>.log when there is no console
    (pythonw) or when DL_LOG=1. Returns True when file logging is on."""
    import sys
    if os.environ.get("DL_LOG", "") == "1" or sys.stdout is None or sys.stderr is None:
        log = _RotatingLog(name)
        sys.stdout = sys.stderr = log
        return True
    return False
