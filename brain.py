#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRAFFIC BRAIN — מערכת תנועה עצמאית לגוש דן
=============================================

הרעיון: תנועה היא כמעט לגמרי צפויה. במקום לשאול את גוגל "מה קורה עכשיו"
כל חמש דקות (ומשלם מאות שקלים בחודש), המערכת לומדת את הדופק השבועי של
הכבישים מדגימות מעטות וזולות, חוזה ממנו בחינם ברזולוציה כלשהי, וקונה
מדידה אמיתית רק כשיש סיבה להאמין שהמודל טועה.

עצמאי לחלוטין: רץ ברקע, שומר את המודל על הדיסק, ומגיש אותו ב-/traffic.
חדר המצב (או כל כלי אחר) רק צורך — לא מחשב, ולא מחזיק מפתח.

הרצה:  python brain.py        (פורט 3200)
מפתח:  קובץ google.key ליד הקובץ הזה, או משתנה סביבה GOOGLE_MAPS_KEY
"""

import http.server
import json
import math
import os
import random
import socketserver
import ssl
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import dlconfig
from datetime import datetime, timedelta

PORT = int(os.environ.get("BRAIN_PORT", "3200"))
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
MODEL_PATH = os.path.join(DATA, "model.json")
STATE_PATH = os.path.join(DATA, "state.json")

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---------------------------------------------------------------- segments
# מקטעים מכוונים בעורקי גוש דן. כל אחד הוא קטע שנהג באמת חושב במונחיו.
SEGMENTS = [
    # --- גוש דן: הליבה, נמדדת הכי הרבה ---
    {"road": "איילון דרום",  "leg": "רוקח ← השלום",      "reg": "גוש דן",  "o": (32.0975, 34.8012), "d": (32.0703, 34.7936)},
    {"road": "איילון צפון",  "leg": "השלום ← רוקח",      "reg": "גוש דן",  "o": (32.0703, 34.7936), "d": (32.0975, 34.8012)},
    {"road": "איילון דרום",  "leg": "השלום ← וולפסון",   "reg": "גוש דן",  "o": (32.0703, 34.7936), "d": (32.0480, 34.7830)},
    {"road": "כביש 4 צפון",  "leg": "חולון ← בני ברק",   "reg": "גוש דן",  "o": (32.0180, 34.7780), "d": (32.0870, 34.8330)},
    {"road": "כביש 4 דרום",  "leg": "בני ברק ← חולון",   "reg": "גוש דן",  "o": (32.0870, 34.8330), "d": (32.0180, 34.7780)},
    # --- מסדרון ירושלים ---
    {"road": "כביש 1 מזרחה", "leg": "שורש ← ירושלים",    "reg": "ירושלים", "o": (31.8100, 35.0200), "d": (31.7960, 35.1600)},
    {"road": "כביש 1 מערבה", "leg": "ירושלים ← שורש",    "reg": "ירושלים", "o": (31.7960, 35.1600), "d": (31.8100, 35.0200)},
    {"road": "כביש 1 מערבה", "leg": "גנות ← תל אביב",    "reg": "גוש דן",  "o": (32.0000, 34.8480), "d": (32.0560, 34.8100)},
    # --- צפון ---
    {"road": "כביש 2 צפון",  "leg": "תל אביב ← נתניה",   "reg": "שרון",    "o": (32.1050, 34.7830), "d": (32.3200, 34.8550)},
    {"road": "כביש 2 דרום",  "leg": "נתניה ← תל אביב",   "reg": "שרון",    "o": (32.3200, 34.8550), "d": (32.1050, 34.7830)},
    {"road": "כביש 2 חיפה",  "leg": "חדרה ← חיפה",       "reg": "חיפה",    "o": (32.4400, 34.9050), "d": (32.7900, 35.0000)},
    {"road": "כביש 6 צפון",  "leg": "קסם ← עירון",       "reg": "מרכז",    "o": (32.1450, 35.0100), "d": (32.4600, 35.0500)},
    # --- דרום ---
    {"road": "כביש 4 דרום",  "leg": "אשדוד ← אשקלון",    "reg": "דרום",    "o": (31.8000, 34.6800), "d": (31.6700, 34.5900)},
    {"road": "כביש 40 דרום", "leg": "קרית גת ← ב״ש",     "reg": "דרום",    "o": (31.6100, 34.7700), "d": (31.2500, 34.7900)},
    # --- הקצוות: לא נתקעים שם הרבה, אבל בלעדיהם זו לא מפה של המדינה ---
    {"road": "כביש 40 הנגב",  "leg": "ב״ש ← מצפה רמון",   "reg": "נגב",     "o": (31.2500, 34.7900), "d": (30.6100, 34.8000)},
    {"road": "כביש 90 הערבה", "leg": "יטבתה ← אילת",      "reg": "אילת",    "o": (29.8800, 35.0700), "d": (29.5580, 34.9500)},
    {"road": "כביש 85 גליל",  "leg": "עכו ← צפת",         "reg": "גליל",    "o": (32.9200, 35.1000), "d": (32.9650, 35.4960)},
    {"road": "כביש 90 בקעה",  "leg": "בית שאן ← טבריה",   "reg": "צפון",    "o": (32.5000, 35.5000), "d": (32.7900, 35.5300)},
    {"road": "כביש 98 גולן",  "leg": "קצרין ← החרמון",    "reg": "גולן",    "o": (32.9930, 35.6900), "d": (33.2400, 35.7800)},
]

# מקטעי ליבה שנמדדים בעדיפות בשעות שיא
CORE = {"גוש דן"}

# ---------------------------------------------------------------- budget
# Google gives 5,000 free Compute Routes Pro events a month (TRAFFIC_AWARE is
# a Pro feature). Everything below is sized to stay under that with room to
# spare, and the ceilings make going over impossible rather than unlikely.
MONTHLY_CAP = 4500          # hard stop, well inside the free 5,000
DAILY_CAP = 200             # stops a bad day from eating the month
TRIGGER_BATCH = 6           # an incident measures the core, not the country
TRIGGER_RESERVE = 900       # kept aside so incidents never starve the schedule
FRESH_MIN = 25              # מדידה נחשבת "עכשיו" עד גיל זה

# ---------------------------------------------------------------- model
# הזמן מקובץ ל-30 דקות, והיום לסוג-יום: בישראל ראשון-חמישי דומים,
# שישי שונה, שבת שונה לגמרי. כך כל תא מקבל פי חמישה דגימות.
BUCKETS_PER_DAY = 48


def day_type(dt):
    wd = dt.weekday()           # Mon=0 .. Sun=6
    if wd == 4:
        return "fri"
    if wd == 5:
        return "sat"
    return "week"


def bucket_of(dt):
    return dt.hour * 2 + (1 if dt.minute >= 30 else 0)


def cell_key(seg_i, dt):
    return "%d|%s|%d" % (seg_i, day_type(dt), bucket_of(dt))


# ---------------------------------------------------------------- storage
_lock = threading.RLock()
MODEL = {"cells": {}, "static": {}, "updated": 0, "samples": 0}
STATE = {"month": "", "usedMonth": 0, "day": "", "usedDay": 0,
         "lastVerify": 0, "lastMeasure": {}, "triggers": [], "log": []}


def _load():
    global MODEL, STATE
    os.makedirs(DATA, exist_ok=True)
    for path, target, default in ((MODEL_PATH, "MODEL", MODEL), (STATE_PATH, "STATE", STATE)):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            default.update(loaded)
        except Exception:
            pass


def _save():
    os.makedirs(DATA, exist_ok=True)
    for path, obj in ((MODEL_PATH, MODEL), (STATE_PATH, STATE)):
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            pass


def _roll_periods():
    """Reset the counters when the month or the day turns over."""
    now = datetime.now()
    m, d = now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
    if STATE.get("month") != m:
        STATE.update(month=m, usedMonth=0)
    if STATE.get("day") != d:
        STATE.update(day=d, usedDay=0)


def budget_left(scheduled=False):
    """Scheduled sampling stops early so that incidents late in the month can
    still be answered. Triggers may use the full ceiling."""
    _roll_periods()
    cap = MONTHLY_CAP - (TRIGGER_RESERVE if scheduled else 0)
    return min(cap - STATE["usedMonth"], DAILY_CAP - STATE["usedDay"])


# ---------------------------------------------------------------- learning
def observe(seg_i, dt, ratio, static_s):
    """Welford online mean/variance — no sample list to grow forever."""
    key = cell_key(seg_i, dt)
    c = MODEL["cells"].get(key) or {"n": 0, "mean": 0.0, "m2": 0.0}
    n = c["n"] + 1
    delta = ratio - c["mean"]
    mean = c["mean"] + delta / n
    m2 = c["m2"] + delta * (ratio - mean)
    MODEL["cells"][key] = {"n": n, "mean": round(mean, 4), "m2": round(m2, 6)}
    # free-flow duration is a property of the road, so a running median-ish
    # value is enough and keeps delays in real minutes
    s = MODEL["static"].get(str(seg_i))
    MODEL["static"][str(seg_i)] = static_s if s is None else round(s * 0.8 + static_s * 0.2, 1)
    MODEL["samples"] = MODEL.get("samples", 0) + 1
    MODEL["updated"] = time.time()


def _cell(seg_i, dtype, bucket):
    return MODEL["cells"].get("%d|%s|%d" % (seg_i, dtype, bucket % BUCKETS_PER_DAY))


def predict(seg_i, dt):
    """Weighted blend of the matching cell and its immediate neighbours.
    Traffic at 08:00 informs 08:30, so a thin model still says something."""
    dtype, b = day_type(dt), bucket_of(dt)
    parts = []
    for offset, w in ((0, 1.0), (-1, 0.45), (1, 0.45), (-2, 0.15), (2, 0.15)):
        c = _cell(seg_i, dtype, b + offset)
        if c and c["n"] > 0:
            parts.append((c, w))
    if not parts:
        return None
    wn = sum(c["n"] * w for c, w in parts)
    if wn <= 0:
        return None
    mean = sum(c["mean"] * c["n"] * w for c, w in parts) / wn
    # confidence: enough samples AND consistent ones
    centre = _cell(seg_i, dtype, b)
    n_eff = wn
    var = 0.0
    if centre and centre["n"] > 1:
        var = max(0.0, centre["m2"] / (centre["n"] - 1))
    cv = (math.sqrt(var) / mean) if mean > 0 else 0
    conf = min(1.0, n_eff / 10.0) / (1.0 + 3.0 * cv)
    return {"ratio": mean, "confidence": round(max(0.05, min(0.99, conf)), 2),
            "n": int(round(n_eff))}


ACTIVE_FROM, ACTIVE_TO = 10, 47      # 05:00 .. 23:59 in 30-minute buckets


def coverage():
    """How much of the picture that matters is filled in. Deep-night buckets
    are excluded on purpose: nobody is stuck at 03:00, and counting them
    would keep the sampler in learning mode forever."""
    span = ACTIVE_TO - ACTIVE_FROM + 1
    want = len(SEGMENTS) * 3 * span
    have = 0
    for k, c in MODEL["cells"].items():
        try:
            b = int(k.split("|")[2])
        except Exception:
            continue
        if ACTIVE_FROM <= b <= ACTIVE_TO and c["n"] >= 2:
            have += 1
    return have / want if want else 0.0


# ---------------------------------------------------------------- measuring
def api_key():
    p = os.path.join(ROOT, "google.key")
    try:
        if os.path.isfile(p):
            k = open(p, encoding="utf-8").read().strip()
            if k:
                return k
    except Exception:
        pass
    return os.environ.get("GOOGLE_MAPS_KEY") or None


def _call_routes(key, seg):
    """One Routes API query. Isolated so tests can replace it."""
    body = json.dumps({
        "origin": {"location": {"latLng": {"latitude": seg["o"][0], "longitude": seg["o"][1]}}},
        "destination": {"location": {"latLng": {"latitude": seg["d"][0], "longitude": seg["d"][1]}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                 "User-Agent": UA,
                 "X-Goog-FieldMask": "routes.duration,routes.staticDuration"})
    with urllib.request.urlopen(req, timeout=15, context=_SSL) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    route = (d.get("routes") or [{}])[0]
    dur = float(str(route.get("duration", "0s")).rstrip("s") or 0)
    stat = float(str(route.get("staticDuration", "0s")).rstrip("s") or 0)
    if not dur or not stat:
        raise ValueError("empty route")
    return dur, stat


RUSH = [[7, 9], [16, 19]]
_cfg_seen = {"mtime": None}


def is_rush(hour):
    return any(a <= hour <= b for a, b in RUSH)


def apply_config(force=False):
    """Read the traffic settings from config.json / config.local.json.
    Called at start and on every scheduler tick; re-reads only when a file
    changed, so the settings screen takes effect without a restart.
    dlconfig clamps every value - the monthly cap can never pass the free tier."""
    global MIN_DELAY, HEAVY_DELAY, MONTHLY_CAP, DAILY_CAP, TRIGGER_RESERVE, RUSH
    try:
        m = dlconfig.mtime()
        if not force and m == _cfg_seen["mtime"]:
            return False
        t = dlconfig.load()["traffic"]
        with _lock:
            MIN_DELAY, HEAVY_DELAY = t["minDelay"], t["heavyDelay"]
            MONTHLY_CAP, DAILY_CAP = t["monthlyCap"], t["dailyCap"]
            TRIGGER_RESERVE = min(900, MONTHLY_CAP // 5)   # 900 at the default cap
            RUSH = t["rush"]
        _cfg_seen["mtime"] = m
        return True
    except Exception:
        return False


def il_now():
    """Israel local time regardless of where the machine thinks it is."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Jerusalem")).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def batch_size():
    """How many segments to measure in one cycle."""
    h = il_now().hour
    if is_rush(h):
        return 4           # rush: a fresh slice of the roads that matter
    if 6 <= h <= 22:
        return 3
    return 2


def pick_batch(n, force_all=False):
    """Measuring all 14 every cycle would burn the month in days. Instead each
    cycle buys the few readings that change the picture the most:
      * how stale the current reading is
      * whether the model expects trouble there (a road the model says is
        empty needs no proof; one near the 10-minute line does)
      * how unsure the model is
      * core Gush Dan arteries during rush hour
    Everything else is filled in by prediction, which is free."""
    if force_all:
        return list(range(len(SEGMENTS)))
    now = il_now()
    rush = is_rush(now.hour)
    scored = []
    for i, seg in enumerate(SEGMENTS):
        m = STATE["lastMeasure"].get(str(i))
        age = (time.time() - m["t"]) / 60.0 if m else 9999
        s = min(age, 300) / 60.0                      # staleness, capped
        p = predict(i, now)
        static_s = MODEL["static"].get(str(i))
        if p and static_s:
            exp = max(0, static_s * (p["ratio"] - 1) / 60.0)
            # right around the reporting threshold is where a wrong guess
            # actually misleads someone
            s += 2.5 if exp >= MIN_DELAY else (1.2 if exp >= MIN_DELAY * 0.6 else 0.0)
            s += (1.0 - p["confidence"]) * 2.0
        else:
            s += 3.0                                   # never seen: learn it
        # the Gush Dan core is where most people are stuck most of the time,
        # so it outranks the periphery even outside rush hour
        if seg.get("reg") in CORE:
            s += 1.0 + (1.5 if rush else 0.0)
        scored.append((s, i))
    # highest score first; on a tie prefer the earlier (more central) segment.
    # Sorting on (score, index) descending quietly handed every tie to the
    # last roads in the list, so the first cycles measured the periphery.
    scored.sort(key=lambda si: (-si[0], si[1]))
    # One slot every cycle goes to whichever REGION has waited longest. Without
    # it the rush-hour filter kept the budget in Gush Dan and the north and
    # south were never sampled during the hours that matter — the deck claimed
    # a national picture it had not actually looked at.
    regional = None
    if n >= 2:
        oldest, best = -1, None
        for i, seg in enumerate(SEGMENTS):
            m = STATE["lastMeasure"].get(str(i))
            age = (time.time() - m["t"]) / 60.0 if m else 1e9
            reg = seg.get("reg")
            if reg in CORE:
                continue                      # the core is covered anyway
            if age > oldest:
                oldest, best = age, i
        if best is not None and oldest > 45:
            regional = best
    if rush:
        # During rush the budget goes to the core arteries plus anything the
        # model expects near or over the 10-minute line. A road the model is
        # confident is empty gets no call at all — that is exactly the saving
        # that buys the tighter cycle elsewhere.
        def worth(i):
            if SEGMENTS[i].get("reg") in CORE:
                return True
            p = predict(i, now)
            st = MODEL["static"].get(str(i))
            if not p or not st:
                return True                      # unknown: learn it
            exp = max(0, st * (p["ratio"] - 1) / 60.0)
            return exp >= MIN_DELAY * 0.6 or p["confidence"] < 0.5
        hot = [i for _, i in scored if worth(i)]
        if len(hot) >= n:
            picked = hot[:n]
            if regional is not None and regional not in picked:
                picked = picked[:n - 1] + [regional]
            return picked
    picked = [i for _, i in scored[:n]]
    if regional is not None and regional not in picked:
        picked = picked[:n - 1] + [regional]
    return picked


# ---- road shapes, from the OTHER free tier ----
# Compute Routes Essentials is a separate SKU with its own 10,000 free events
# a month, and it returns the real geometry of a road. It cannot see traffic,
# so it is useless for measuring congestion — but a road's shape never
# changes, so 14 calls ONCE buy a map that follows the actual asphalt instead
# of drawing straight lines between two points.
def _fetch_shape(key, seg):
    import urllib.request
    body = json.dumps({
        "origin": {"location": {"latLng": {"latitude": seg["o"][0], "longitude": seg["o"][1]}}},
        "destination": {"location": {"latLng": {"latitude": seg["d"][0], "longitude": seg["d"][1]}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",     # keeps this on Essentials
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                 "User-Agent": UA,
                 "X-Goog-FieldMask": "routes.polyline.encodedPolyline,routes.distanceMeters"})
    with urllib.request.urlopen(req, timeout=15, context=_SSL) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    route = (d.get("routes") or [{}])[0]
    enc = ((route.get("polyline") or {}).get("encodedPolyline"))
    if not enc:
        raise ValueError("no polyline")
    return enc, route.get("distanceMeters")


def fetch_shapes():
    """Fill in any missing road shapes. Runs once per segment, ever."""
    key = api_key()
    if not key:
        return 0
    got = 0
    for i, seg in enumerate(SEGMENTS):
        if MODEL.get("shapes", {}).get(str(i)):
            continue
        try:
            enc, dist = _fetch_shape(key, seg)
        except Exception:
            continue
        with _lock:
            MODEL.setdefault("shapes", {})[str(i)] = enc
            MODEL.setdefault("dist", {})[str(i)] = dist
            STATE["essentialsUsed"] = STATE.get("essentialsUsed", 0) + 1
        got += 1
        time.sleep(0.3)
    if got:
        with _lock:
            _save()
    return got


def _note_failure(e):
    """Record why Google did not answer. Until v73 every failure was dropped
    silently: a revoked key, a disabled API or an exhausted quota all looked
    the same from the deck - the numbers simply stopped being fresh."""
    import re as _re
    code, msg = 0, str(e)[:160]
    try:
        if isinstance(e, urllib.error.HTTPError):
            code = e.code
            raw = e.read().decode("utf-8", "replace")
            try:
                err = (json.loads(raw) or {}).get("error") or {}
                msg = "%s: %s" % (err.get("status", ""), err.get("message", ""))
            except Exception:
                msg = raw[:160]
    except Exception:
        pass
    msg = _re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "<key>", msg)[:200]   # never log the key
    hint = {400: "bad request - API key format or request body",
            401: "key rejected",
            403: "key rejected, Routes API not enabled, or billing off",
            429: "Google daily quota reached"}.get(code, "network or Google error")
    if "API key not valid" in msg or "API_KEY_INVALID" in msg:
        hint = "google.key holds a key Google does not accept (deleted or regenerated?)"
    elif "expired" in msg.lower():
        hint = "the API key has expired"
    elif "SERVICE_DISABLED" in msg or "has not been used" in msg:
        hint = "Routes API is not enabled on the Google Cloud project"
    elif "billing" in msg.lower():
        hint = "billing is not active on the Google Cloud project"
    he = ("המפתח ב-google.key לא מתקבל ב-Google (נמחק או הוחלף?)" if "google.key holds" in hint else
          "תוקף המפתח פג" if "expired" in hint else
          "Routes API לא מופעל בפרויקט ב-Google Cloud" if "not enabled on" in hint else
          "החיוב לא פעיל בפרויקט ב-Google Cloud" if "billing is not" in hint else
          "המכסה היומית ב-Google מוצתה" if code == 429 else
          "המפתח נדחה, Routes API כבוי או שהחיוב לא פעיל" if code in (401, 403) else
          "בקשה לא תקינה" if code == 400 else "תקלת רשת או תקלה ב-Google")
    with _lock:
        prev = STATE.get("lastError") or {}
        STATE["lastError"] = {"t": time.time(), "code": code, "msg": msg, "hint": hint, "hintHe": he,
                              "count": (prev.get("count", 0) + 1) if prev.get("code") == code else 1}
        first = STATE["lastError"]["count"] == 1
    if first:      # once per distinct failure, ASCII only for the Windows console
        try:
            print(" [!] Google measurement FAILED  code=%s  %s" % (code or "-", hint))
            print("     %s" % msg.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass


def verify(reason="scheduled", n=None, force_all=False):
    """Buy the truth for a prioritised batch, learn from it, record the spend."""
    key = api_key()
    if not key:
        return {"ok": False, "why": "no_key"}
    batch = pick_batch(n or batch_size(), force_all)
    with _lock:
        if budget_left(scheduled=(reason == "scheduled")) < len(batch):
            return {"ok": False, "why": "budget"}
    now = datetime.now()
    got = 0
    for i in batch:
        seg = SEGMENTS[i]
        try:
            dur, stat = _call_routes(key, seg)
        except Exception as e:
            _note_failure(e)
            continue
        ratio = dur / stat
        with _lock:
            STATE["usedMonth"] += 1
            STATE["usedDay"] += 1
            observe(i, now, ratio, stat)
            STATE["lastMeasure"][str(i)] = {
                "t": time.time(), "ratio": round(ratio, 3),
                "delayMin": max(0, round((dur - stat) / 60.0)),
                "minutes": round(dur / 60.0)}
        got += 1
    with _lock:
        STATE["lastVerify"] = time.time()
        if got:
            STATE["lastOk"] = time.time()
            STATE["lastError"] = None
        STATE["log"] = ([{"t": time.time(), "reason": reason, "segments": got,
                          "roads": [SEGMENTS[i]["road"] for i in batch]}]
                        + STATE.get("log", []))[:60]
        _save()
    return {"ok": got > 0, "segments": got, "reason": reason,
            "measured": [SEGMENTS[i]["road"] for i in batch]}


# ---------------------------------------------------------------- scheduler
def verify_interval_min():
    """Rush hours get a tight cycle so the map feels live exactly when it
    matters; nights are nearly free to skip. With batching this lands around
    138 calls a day — roughly 4,100 a month, inside the free 5,000."""
    h = il_now().hour
    if h < 6 or h >= 23:
        return 180                        # night
    if is_rush(h):
        return 18                         # rush: a fresh slice every ~quarter hour
    return 60                             # rest of the day


def deck_watchdog():
    """Shut down when the deck goes away.

    Closing the deck window with the X never runs the launcher's cleanup
    lines, so the brain used to survive as an orphan window. Watching for the
    deck itself covers every way it can end: X, taskkill, crash, reboot.
    If the deck is never seen at all the brain keeps running, so it can still
    be used standalone."""
    import urllib.request
    seen = False
    missing_since = None
    deck = os.environ.get("DECK_URL", "http://127.0.0.1:3100")
    while True:
        time.sleep(20)
        try:
            with urllib.request.urlopen(deck + "/lanip", timeout=3):
                pass
            up = True
        except Exception:
            try:
                with urllib.request.urlopen(deck + "/", timeout=3):
                    pass
                up = True
            except Exception:
                up = False
        if up:
            seen, missing_since = True, None
            continue
        if not seen:
            continue                      # standalone use: stay alive
        if missing_since is None:
            missing_since = time.time()
        elif time.time() - missing_since > 150:
            print("Deck closed - shutting the Traffic Brain down.")
            _mirror_stop_noop()
            os._exit(0)


def _mirror_stop_noop():
    try:
        _save()
    except Exception:
        pass


def scheduler():
    first = True
    while True:
        try:
            # measure almost immediately on boot: a fresh install that shows
            # "no data" for half a minute looks broken even though it is not
            time.sleep(3 if first else 20)
            first = False
            apply_config()
            if not api_key():
                continue
            with _lock:
                pending = STATE.get("triggers") or []
                last = STATE.get("lastVerify", 0)
                enough = budget_left(scheduled=not pending) >= batch_size()
            if not enough:
                continue
            # a trigger means the model is probably wrong right now
            if pending:
                with _lock:
                    reason = pending.pop(0)
                    STATE["triggers"] = pending
                # A closure in Gush Dan is no reason to re-measure Beer
                # Sheva. Sweeping all 14 on every trigger was unbudgeted and
                # could push the month past the free 5,000 on its own.
                verify("trigger:" + str(reason)[:40], n=TRIGGER_BATCH)
                continue
            if (time.time() - last) / 60.0 >= verify_interval_min():
                verify("scheduled")
        except Exception:
            time.sleep(30)


# ---------------------------------------------------------------- output
# "עיכוב של עד 10 דק׳ לא שווה ייחוס" — so the classification is driven by
# real minutes lost, not by a percentage. 12% on a 60-minute road is 7 lost
# minutes (nobody cares); 30% on a 20-minute road is 6 (still nobody cares);
# 20 minutes lost anywhere is a jam.
MIN_DELAY = 10          # below this a road is simply "flowing"
HEAVY_DELAY = 20


def level_of(delay_min, ratio=None):
    if delay_min is None:
        return "unknown"
    if delay_min >= HEAVY_DELAY:
        return "heavy"
    if delay_min >= MIN_DELAY:
        return "slow"
    return "free"


def snapshot():
    now = datetime.now()
    out = []
    with _lock:
        for i, seg in enumerate(SEGMENTS):
            static_s = MODEL["static"].get(str(i))
            m = STATE["lastMeasure"].get(str(i))
            age = (time.time() - m["t"]) / 60.0 if m else None
            if m and age is not None and age <= FRESH_MIN:
                out.append({
                    "road": seg["road"], "leg": seg["leg"], "reg": seg.get("reg"),
                    "delayMin": m["delayMin"], "minutes": m["minutes"],
                    "ratio": m["ratio"], "level": level_of(m["delayMin"], m["ratio"]),
                    "source": "measured", "ageMin": round(age),
                    "confidence": 1.0, "o": seg["o"], "d": seg["d"],
                    "shape": MODEL.get("shapes", {}).get(str(i))})
                continue
            p = predict(i, now)
            if not p or not static_s:
                out.append({
                    "road": seg["road"], "leg": seg["leg"], "reg": seg.get("reg"),
                    "delayMin": None, "level": "unknown",
                    "source": "unknown", "ageMin": None, "confidence": 0.0,
                    "o": seg["o"], "d": seg["d"],
                    "shape": MODEL.get("shapes", {}).get(str(i))})
                continue
            out.append({
                "road": seg["road"], "leg": seg["leg"], "reg": seg.get("reg"),
                "delayMin": max(0, round(static_s * (p["ratio"] - 1) / 60.0)),
                "minutes": round(static_s * p["ratio"] / 60.0),
                "ratio": round(p["ratio"], 3),
                "level": level_of(max(0, round(static_s * (p["ratio"] - 1) / 60.0)), p["ratio"]),
                "source": "predicted", "ageMin": None,
                "confidence": p["confidence"], "basedOn": p["n"],
                "o": seg["o"], "d": seg["d"],
                "shape": MODEL.get("shapes", {}).get(str(i))})
        used_m, used_d = STATE["usedMonth"], STATE["usedDay"]
        last = STATE.get("lastVerify", 0)
        last_ok = STATE.get("lastOk", 0)
        last_err = STATE.get("lastError")
    known = [s for s in out if s["level"] != "unknown"]
    order = {"heavy": 0, "slow": 1, "free": 2}
    known.sort(key=lambda s: (order[s["level"]], -(s["delayMin"] or 0)))
    hot = [s for s in known if s["level"] != "free"]
    counts = {"heavy": 0, "slow": 0, "free": 0}
    for s in known:
        counts[s["level"]] += 1
    nxt = last + verify_interval_min() * 60 if last else time.time()
    return {
        "asOf": datetime.now().isoformat(timespec="seconds"),
        "src": "brain",
        "segments": known + [s for s in out if s["level"] == "unknown"],
        "counts": counts, "hotspots": hot,
        "worst": hot[0] if hot else None,
        "verdict": ("כבדה" if counts["heavy"] else "ערה" if counts["slow"]
                    else "זורמת" if known else "אין נתונים"),
        "checkedAt": (datetime.fromtimestamp(last).strftime("%H:%M") if last else None),
        "measured": sum(1 for s in known if s["source"] == "measured"),
        "predicted": sum(1 for s in known if s["source"] == "predicted"),
        "nextVerification": datetime.fromtimestamp(nxt).strftime("%H:%M"),
        "budget": {"usedMonth": used_m, "capMonth": MONTHLY_CAP,
                   "usedDay": used_d, "capDay": DAILY_CAP,
                   "state": ("no_key" if not api_key() else
                             "exhausted" if budget_left() < len(SEGMENTS) else "healthy")},
        "model": {"coverage": round(coverage(), 3), "samples": MODEL.get("samples", 0),
                  "phase": "learning" if coverage() < 0.55 else "steady"},
        "thresholds": {"minDelay": MIN_DELAY, "heavyDelay": HEAVY_DELAY, "rush": RUSH},
        # failing = Google is refusing us right now; what the deck shows is the
        # model's forecast, not a measurement
        "health": {"failing": bool(last_err),
                   "lastOk": (datetime.fromtimestamp(last_ok).strftime("%d.%m %H:%M")
                              if last_ok else None),
                   "error": last_err},
    }


# ---------------------------------------------------------------- http
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        query = self.path.split("?")[1] if "?" in self.path else ""
        if path in ("/traffic", "/"):
            self._send(snapshot())
        elif path == "/trigger":
            reason = "external"
            for part in query.split("&"):
                if part.startswith("reason="):
                    reason = urllib.parse.unquote(part[7:])
            with _lock:
                trig = STATE.get("triggers") or []
                if len(trig) < 5:
                    trig.append(reason)
                STATE["triggers"] = trig
                _save()
            self._send({"ok": True, "queued": reason})
        elif path == "/model":
            with _lock:
                cells = MODEL["cells"]
                filled = {"week": 0, "fri": 0, "sat": 0}
                for k, c in cells.items():
                    if c["n"] >= 2:
                        filled[k.split("|")[1]] += 1
                import calendar as _cal
                d = datetime.now()
                dim = _cal.monthrange(d.year, d.month)[1]
                pace = STATE["usedMonth"] / max(1, d.day)
                self._send({"coverage": round(coverage(), 3),
                            "samples": MODEL.get("samples", 0),
                            "shapes": len(MODEL.get("shapes", {})),
                            "budget": {
                                "usedMonth": STATE["usedMonth"],
                                "essentialsUsed": STATE.get("essentialsUsed", 0),
                                "perDay": round(pace, 1),
                                "projectedMonth": int(pace * dim),
                                "freeTier": 5000, "hardCap": MONTHLY_CAP,
                                "onTrack": pace * dim <= MONTHLY_CAP},
                            "cells": len(cells), "filled": filled,
                            "static": MODEL["static"],
                            "phase": "learning" if coverage() < 0.55 else "steady",
                            "recent": STATE.get("log", [])[:12]})
        elif path == "/verify":
            self._send(verify("manual", force_all=("all" in query)))
        else:
            self._send({"error": "not found"}, 404)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    dlconfig.setup_logging("brain")        # logs/brain.log when there is no console
    _load()
    apply_config(force=True)
    _roll_periods()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=deck_watchdog, daemon=True).start()
    threading.Thread(target=fetch_shapes, daemon=True).start()
    # ASCII only: the Windows console reverses Hebrew and turns the banner
    # into gibberish. The data is in Hebrew everywhere it is actually read.
    print("=" * 58)
    print(" TRAFFIC BRAIN   ->  http://localhost:%d/traffic" % PORT)
    print(" API key : %s" % ("FOUND" if api_key() else "MISSING (add google.key here)"))
    print(" Model   : %d samples, %.0f%% coverage [%s]"
          % (MODEL.get("samples", 0), coverage() * 100,
             "learning" if coverage() < 0.55 else "steady"))
    print(" Budget  : %d / %d this month" % (STATE["usedMonth"], MONTHLY_CAP))
    print(" Next    : first measurement within ~1 min")
    print("=" * 58)
    print(" Leave this window open. Minimising is fine.")
    print("")
    Server(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
