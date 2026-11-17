#!/usr/bin/env python3
# ============================================================
#   Dispatch LIVE - local archive  (data/archive.db, SQLite)
#   ----------------------------------------------------------
#   The deck used to remember nothing: alert history came from Oref's own
#   archive (which Oref removed in 2026), the tension score lived for 24h in
#   one browser, and traffic was never kept at all.
#   This file keeps all three locally so the deck can show a week, a month,
#   and "what happened since last night" without depending on anyone.
#   Standard library only. Every public function is safe to call from any
#   thread and never raises: the archive is best-effort, never load-bearing.
# ============================================================
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "data", "archive.db")
KEEP_DAYS = 400
_lock = threading.Lock()
_ready = {"path": None}

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    alert_date TEXT NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS', Israel local time
    data       TEXT NOT NULL,          -- locality
    category   INTEGER,
    title      TEXT,
    rid        INTEGER,
    PRIMARY KEY (alert_date, data, category)
);
CREATE INDEX IF NOT EXISTS alerts_date ON alerts(alert_date);
CREATE TABLE IF NOT EXISTS tension (
    bucket INTEGER PRIMARY KEY,        -- epoch // 300: one reading per 5 minutes
    t      INTEGER NOT NULL,
    score  INTEGER NOT NULL,
    alerts INTEGER, idf INTEGER, news INTEGER
);
CREATE TABLE IF NOT EXISTS traffic (
    bucket INTEGER PRIMARY KEY,        -- epoch // 900: one reading per 15 minutes
    t      INTEGER NOT NULL,
    heavy INTEGER, slow INTEGER, free INTEGER, measured INTEGER,
    worst_road TEXT, worst_delay INTEGER,
    hotspots TEXT                      -- JSON [[road, leg, delayMin], ...]
);
"""


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    # Short timeout: if something else holds the file, give up quickly. The
    # archive is best-effort and must never stall the deck.
    c = sqlite3.connect(DB_PATH, timeout=1.5)
    if _ready["path"] != DB_PATH:
        c.executescript(SCHEMA)
        _ready["path"] = DB_PATH
    return c


def _run(fn, default):
    try:
        with _lock:
            c = _conn()
            try:
                out = fn(c)
                c.commit()
                return out
            finally:
                c.close()
    except Exception:
        _ready["path"] = None       # e.g. the file was deleted while running: rebuild the schema next time
        return default


# ---------------------------------------------------------------- alerts
def add_alerts(rows):
    """Store deck-format alert rows. Returns how many were new."""
    clean = []
    for r in rows or []:
        try:
            d, city = str(r.get("alertDate") or "")[:19], str(r.get("data") or "").strip()[:120]
            if len(d) == 16:
                d += ":00"
            datetime.strptime(d, "%Y-%m-%d %H:%M:%S")          # a real date, or skip the row
            if city:
                cat = r.get("category")
                # -1, not NULL: SQLite treats every NULL as distinct in a key,
                # so NULL-category rows used to be stored again on every poll
                clean.append((d, city, int(cat) if cat is not None else -1,
                              str(r.get("title") or "")[:120], r.get("rid")))
        except Exception:
            continue
    if not clean:
        return 0

    def go(c):
        before = c.total_changes
        c.executemany("INSERT OR IGNORE INTO alerts(alert_date,data,category,title,rid) "
                      "VALUES(?,?,?,?,?)", clean)
        return c.total_changes - before
    return _run(go, 0)


def il_now(now=None):
    """Israel local time. alert_date is Israel-time text, so every window over
    it must be computed in Israel time whatever the machine's zone is."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Jerusalem")
        dt = datetime.fromtimestamp(now, tz) if now else datetime.now(tz)
        return dt.replace(tzinfo=None)
    except Exception:
        return datetime.fromtimestamp(now) if now else datetime.now()


def recent_alerts(days=30, limit=400):
    """Newest first, in the shape the deck consumes."""
    since = (il_now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    def go(c):
        cur = c.execute("SELECT alert_date,title,data,category,rid FROM alerts "
                        "WHERE alert_date >= ? ORDER BY alert_date DESC LIMIT ?", (since, int(limit)))
        return [{"alertDate": a, "title": t, "data": d, "category": None if cat == -1 else cat, "rid": rid}
                for a, t, d, cat, rid in cur.fetchall()]
    return _run(go, [])


# ---------------------------------------------------------------- tension
def add_tension(score, alerts=None, idf=None, news=None, now=None):
    now = int(now or time.time())
    try:
        score = max(0, min(100, int(score)))
    except Exception:
        return False

    def to_int(v):
        try:
            return int(v)
        except Exception:
            return None

    def go(c):
        c.execute("INSERT OR REPLACE INTO tension(bucket,t,score,alerts,idf,news) VALUES(?,?,?,?,?,?)",
                  (now // 300, now, score, to_int(alerts), to_int(idf), to_int(news)))
        return True
    return _run(go, False)


# ---------------------------------------------------------------- traffic
def add_traffic(snap, now=None):
    """Store one brain snapshot (the /traffic payload)."""
    now = int(now or time.time())
    try:
        counts = snap.get("counts") or {}
        hot = [[h.get("road"), h.get("leg"), h.get("delayMin")] for h in (snap.get("hotspots") or [])[:8]]
        worst = snap.get("worst") or {}
        if not snap.get("segments"):
            return False
        row = (now // 900, now, counts.get("heavy", 0), counts.get("slow", 0), counts.get("free", 0),
               snap.get("measured", 0), worst.get("road"), worst.get("delayMin"),
               json.dumps(hot, ensure_ascii=False))
    except Exception:
        return False

    def go(c):
        c.execute("INSERT OR REPLACE INTO traffic(bucket,t,heavy,slow,free,measured,worst_road,"
                  "worst_delay,hotspots) VALUES(?,?,?,?,?,?,?,?,?)", row)
        return True
    return _run(go, False)


# ---------------------------------------------------------------- reading
def _events(rows):
    """Same grouping the deck uses: one event = same minute + same type."""
    seen, out = {}, []
    for a, title, city, cat in rows:
        key = (a[:16], cat, title)
        ev = seen.get(key)
        if ev is None:
            ev = seen[key] = {"at": a, "title": title, "category": cat, "cities": []}
            out.append(ev)
        if city not in ev["cities"]:
            ev["cities"].append(city)
    return out


def summary(hours=24, now=None):
    """What happened in the last N hours, from local data only."""
    now = now or time.time()
    hours = max(1, min(24 * 31, int(hours)))
    since_ts = int(now - hours * 3600)
    since_s = il_now(since_ts).strftime("%Y-%m-%d %H:%M:%S")

    def go(c):
        a = c.execute("SELECT alert_date,title,data,category FROM alerts WHERE alert_date >= ? "
                      "ORDER BY alert_date DESC", (since_s,)).fetchall()
        # "event ended" / "you may leave the shelter" rows are not attacks
        real = [r for r in a if r[3] != 13]
        events = _events(real)
        by_type, by_city = {}, {}
        for ev in events:
            by_type[ev["title"] or "התרעה"] = by_type.get(ev["title"] or "התרעה", 0) + 1
        for _a, _t, city, _c in real:
            by_city[city] = by_city.get(city, 0) + 1
        t = c.execute("SELECT t,score FROM tension WHERE t >= ? ORDER BY t", (since_ts,)).fetchall()
        tr = c.execute("SELECT t,heavy,slow,worst_road,worst_delay,hotspots FROM traffic "
                       "WHERE t >= ? ORDER BY t", (since_ts,)).fetchall()
        roads = {}
        for _t, _h, _s, _wr, _wd, hot in tr:
            try:
                for road, leg, delay in json.loads(hot or "[]"):
                    r = roads.setdefault(road, {"road": road, "buckets": 0, "max": 0})
                    r["buckets"] += 1
                    r["max"] = max(r["max"], int(delay or 0))
            except Exception:
                pass
        peak = max(tr, key=lambda r: r[4] or 0) if tr else None
        scores = [s for _t, s in t]
        return {
            "hours": hours, "from": since_ts, "to": int(now),
            "alerts": {
                "events": len(events), "localities": len(by_city), "rows": len(real),
                "byType": sorted(by_type.items(), key=lambda kv: -kv[1])[:6],
                "topLocalities": sorted(by_city.items(), key=lambda kv: -kv[1])[:8],
                "last": ({"at": events[0]["at"], "title": events[0]["title"],
                          "cities": events[0]["cities"][:6], "n": len(events[0]["cities"])}
                         if events else None),
            },
            "tension": ({"samples": len(scores), "min": min(scores), "max": max(scores),
                         "avg": round(sum(scores) / len(scores)), "first": scores[0], "last": scores[-1],
                         "peakAt": max(t, key=lambda r: r[1])[0]} if scores else None),
            "traffic": ({"samples": len(tr),
                         "heavyMinutes": 15 * sum(1 for r in tr if (r[1] or 0) > 0),
                         "slowMinutes": 15 * sum(1 for r in tr if (r[1] or 0) == 0 and (r[2] or 0) > 0),
                         "peak": ({"at": peak[0], "road": peak[3], "delayMin": peak[4]}
                                  if peak and peak[4] else None),
                         "roads": sorted(roads.values(), key=lambda r: (-r["buckets"], -r["max"]))[:6]}
                        if tr else None),
        }
    return _run(go, {"hours": hours, "alerts": None, "tension": None, "traffic": None, "error": "archive unavailable"})


def series(days=7, now=None):
    """Chart data for the last N days."""
    now = now or time.time()
    days = max(1, min(KEEP_DAYS, int(days)))
    since_ts = int(now - days * 86400)
    since_s = il_now(since_ts).strftime("%Y-%m-%d %H:%M:%S")

    def go(c):
        t = c.execute("SELECT t,score FROM tension WHERE t >= ? ORDER BY t", (since_ts,)).fetchall()
        step = max(1, len(t) // 480)          # at most ~480 points on the chart
        tension = []
        for i in range(0, len(t), step):
            chunk = t[i:i + step]
            tension.append([chunk[len(chunk) // 2][0], max(s for _t, s in chunk)])
        a = c.execute("SELECT alert_date,title,data,category FROM alerts WHERE alert_date >= ? "
                      "AND (category IS NULL OR category != 13) ORDER BY alert_date DESC", (since_s,)).fetchall()
        per_day = {}
        for ev in _events(a):
            per_day[ev["at"][:10]] = per_day.get(ev["at"][:10], 0) + 1
        tr = c.execute("SELECT t,worst_delay,heavy FROM traffic WHERE t >= ? ORDER BY t", (since_ts,)).fetchall()
        tday = {}
        for ts, wd, heavy in tr:
            d = il_now(ts).strftime("%Y-%m-%d")
            cur = tday.setdefault(d, {"max": 0, "heavyMinutes": 0})
            cur["max"] = max(cur["max"], wd or 0)
            cur["heavyMinutes"] += 15 if (heavy or 0) > 0 else 0
        day_list = [(il_now(now) - timedelta(days=i)).strftime("%Y-%m-%d")
                    for i in range(days - 1, -1, -1)]
        return {"days": days, "dayList": day_list, "tension": tension,
                "alertsPerDay": [per_day.get(d, 0) for d in day_list],
                "trafficMaxDelay": [tday.get(d, {}).get("max", 0) for d in day_list],
                "trafficHeavyMinutes": [tday.get(d, {}).get("heavyMinutes", 0) for d in day_list]}
    return _run(go, {"days": days, "dayList": [], "tension": [], "alertsPerDay": [],
                     "trafficMaxDelay": [], "trafficHeavyMinutes": [], "error": "archive unavailable"})


def stats():
    def go(c):
        out = {}
        for tbl, col in (("alerts", "alert_date"), ("tension", "t"), ("traffic", "t")):
            n, lo = c.execute("SELECT COUNT(*), MIN(%s) FROM %s" % (col, tbl)).fetchone()
            out[tbl] = {"rows": n, "since": lo}
        try:
            out["bytes"] = os.path.getsize(DB_PATH)
        except OSError:
            out["bytes"] = 0
        return out
    return _run(go, {})


def prune(now=None):
    """Drop anything older than KEEP_DAYS. Called once a day."""
    now = now or time.time()
    cut_ts = int(now - KEEP_DAYS * 86400)
    cut_s = il_now(cut_ts).strftime("%Y-%m-%d %H:%M:%S")

    def go(c):
        c.execute("DELETE FROM alerts WHERE alert_date < ?", (cut_s,))
        c.execute("DELETE FROM tension WHERE t < ?", (cut_ts,))
        c.execute("DELETE FROM traffic WHERE t < ?", (cut_ts,))
        return True
    return _run(go, False)
