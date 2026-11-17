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
        "dl-sound": None,
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
    return str(v)[:limit] if isinstance(v, (str, int, float)) else ""


def sanitize(cfg):
    """Clamp and repair a merged config so no file content can break the deck
    or the budget. Unknown keys are dropped; bad values fall back to defaults."""
    d = DEFAULTS
    out = copy.deepcopy(d)

    prefs = cfg.get("prefs") if isinstance(cfg.get("prefs"), dict) else {}
    for k in PREF_KEYS:
        v = prefs.get(k)
        out["prefs"][k] = None if v is None else _text(v, 4000)

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
            chans.append({"id": "custom%d" % (i + 1), "name": name,
                          "type": "iframe" if c.get("type") == "iframe" else "hls", "src": src})
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
            feeds.append({"id": "cfeed%d" % (i + 1), "name": name, "url": url,
                          "region": "world" if c.get("region") == "world" else "il"})
    out["news"] = {"custom": feeds}

    disp = cfg.get("display") if isinstance(cfg.get("display"), dict) else {}
    out["display"] = {"fontScale": round(_num(disp.get("fontScale"), 0.8, 1.5, 1.0), 2)}
    return out


def load_local():
    return _read(LOCAL_PATH)


def load():
    """The effective configuration: defaults < config.json < config.local.json."""
    merged = deep_merge(deep_merge(DEFAULTS, _read(CONFIG_PATH)), load_local())
    return sanitize(merged)


def save_local(obj):
    """Replace config.local.json. Returns (ok, message)."""
    if not isinstance(obj, dict):
        return False, "config must be a JSON object"
    raw = json.dumps(obj, ensure_ascii=False, indent=2)
    if len(raw.encode("utf-8")) > MAX_BYTES:
        return False, "config too large"
    with _lock:
        tmp = LOCAL_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(raw)
            os.replace(tmp, LOCAL_PATH)
        except Exception as e:
            return False, "could not write config.local.json: %s" % e
    return True, "saved"


def patch_local(patch):
    """Deep-merge a partial object into config.local.json."""
    if not isinstance(patch, dict):
        return False, "patch must be a JSON object"
    return save_local(deep_merge(load_local(), patch))


def mtime():
    """Changes whenever either file changes; used by brain.py to hot-reload."""
    out = 0.0
    for p in (CONFIG_PATH, LOCAL_PATH):
        try:
            out = max(out, os.path.getmtime(p))
        except OSError:
            pass
    return out
