#!/usr/bin/env python3
# ============================================================
#   Dispatch IL  -  local news server (Python edition)
#   ----------------------------------------------------------
#   Zero dependencies. Requires Python 3.7+ (standard library only).
#   - Serves index.html at  http://localhost:3100
#   - Proxies feeds at       http://localhost:3100/fetch?url=...
#     (server-side fetch = no CORS, no rate-limited public proxies)
# ============================================================

import sys
import os
import re
import gzip
import zlib
import ssl
import socket
import json
import threading
import atexit
import subprocess
import webbrowser
from urllib.parse import urlparse, parse_qs, unquote
from urllib.request import Request, urlopen
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "3100"))

# ---- single-app activity watchdog ----
# Once the deck has connected, the server exits if NO request arrives for
# HB_TIMEOUT seconds (window closed / browser gone). The page polls several
# endpoints every few seconds, and even a heavily throttled background tab
# still fires at least once a minute — so 150s never false-triggers.
import time as _time
import time
import dlconfig
import dlarchive
LAST_REQ = {"t": None}   # armed on the first request

def _touch_activity():
    LAST_REQ["t"] = _time.time()

def _activity_watchdog():
    if os.environ.get("NOAUTOCLOSE", "") == "1":
        return
    timeout = float(os.environ.get("HB_TIMEOUT", "120"))
    while True:
        _time.sleep(min(10, timeout / 3))
        t = LAST_REQ["t"]
        if t is not None and (_time.time() - t) > timeout:
            print("\n  No activity from the deck window - stopping server.")
            os._exit(0)
ROOT = os.path.dirname(os.path.abspath(__file__))

# In-memory view-state shared between the controlling screen and any receivers.
SHARED_STATE = {"json": "{}"}
TIMEOUT = 20  # seconds per upstream fetch


def _find_browser():
    """Return (path, kind) for Edge or Chrome if installed, else (None, None).
    Works on Windows, macOS and Linux."""
    candidates = []
    if sys.platform.startswith("win"):
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LocalAppData", "")
        candidates = [
            (os.path.join(pfx86, r"Microsoft\Edge\Application\msedge.exe"), "edge"),
            (os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"), "edge"),
            (os.path.join(pf, r"Google\Chrome\Application\chrome.exe"), "chrome"),
            (os.path.join(pfx86, r"Google\Chrome\Application\chrome.exe"), "chrome"),
        ]
        if local:
            candidates.append((os.path.join(local, r"Microsoft\Edge\Application\msedge.exe"), "edge"))
            candidates.append((os.path.join(local, r"Google\Chrome\Application\chrome.exe"), "chrome"))
    elif sys.platform == "darwin":
        candidates = [
            ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "edge"),
            ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "chrome"),
            ("/Applications/Chromium.app/Contents/MacOS/Chromium", "chrome"),
            ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "chrome"),
        ]
    else:  # linux and others
        import shutil
        for exe, kind in [("microsoft-edge", "edge"), ("microsoft-edge-stable", "edge"),
                          ("google-chrome", "chrome"), ("google-chrome-stable", "chrome"),
                          ("chromium", "chrome"), ("chromium-browser", "chrome"),
                          ("brave-browser", "chrome")]:
            p = shutil.which(exe)
            if p:
                candidates.append((p, kind))
    for path, kind in candidates:
        if os.path.isfile(path):
            return path, kind
    return None, None


def _preseed_profile(profile, port):
    """Create the dedicated browser profile with geolocation PRE-GRANTED for
    the deck's origin — precise location with zero permission prompts —
    and a clean exit state (no 'restore pages?' bubble)."""
    try:
        import json as _json
        default_dir = os.path.join(profile, "Default")
        os.makedirs(default_dir, exist_ok=True)
        prefs_path = os.path.join(default_dir, "Preferences")
        prefs = {}
        if os.path.exists(prefs_path):
            try:
                with open(prefs_path, "r", encoding="utf-8") as f:
                    prefs = _json.load(f)
            except Exception:
                prefs = {}
        origin = "http://localhost:%d,*" % port
        prof = prefs.setdefault("profile", {})
        exc = prof.setdefault("content_settings", {}).setdefault("exceptions", {})
        exc.setdefault("geolocation", {})[origin] = {"setting": 1}
        prof["exit_type"] = "Normal"
        prof["exited_cleanly"] = True
        with open(prefs_path, "w", encoding="utf-8") as f:
            _json.dump(prefs, f)
    except Exception:
        pass


def _profile_proc_count(profile):
    """Windows: how many browser processes belong to OUR dedicated profile."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | "
             "Where-Object { $_.CommandLine -match 'DispatchDeckProfile' } | Measure-Object).Count"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        return int(out or "0")
    except Exception:
        return -1   # unknown — treat as "still alive" (never false-kill)


def _watch_profile_then_exit(profile, count_fn=_profile_proc_count):
    """Poll the profile's browser processes; once seen alive, exit when gone.
    This is the reliable signal on Windows, where the launched msedge.exe can
    hand off to another process and return immediately (breaking proc.wait)."""
    import time as _t
    seen = False
    start = _t.time()
    while True:
        _t.sleep(3)
        n = count_fn(profile)
        if n > 0:
            seen = True
        elif n == 0:
            if seen or (_t.time() - start) > 45:
                print("\n  Deck window closed - stopping server.")
                os._exit(0)


def open_browser_when_ready():
    """Wait until the port is accepting connections, then open the deck
    fullscreen in Edge/Chrome app mode (or the default browser as a fallback).
    Runs in a background thread. When the deck window is closed, the\n    server exits too (single-application behavior)."""
    url = "http://localhost:%d" % PORT
    # Wait for the listener to actually accept (max ~15s).
    for _ in range(150):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                break
        except OSError:
            import time
            time.sleep(0.1)
    path, kind = _find_browser()
    import tempfile
    profile = os.path.join(tempfile.gettempdir(), "DispatchDeckProfile")
    _preseed_profile(profile, PORT)
    if path:
        try:
            proc = subprocess.Popen([
                path,
                "--app=%s" % url,
                "--start-fullscreen",
                "--user-data-dir=%s" % profile,
                "--no-first-run",
                "--no-default-browser-check",
                # single-app behavior: no background processes lingering after
                # the window closes (Edge "startup boost" breaks proc.wait())
                "--disable-background-mode",
                # A covered or minimised window must keep polling every 5 s and
                # keep its countdown on time: without these, Chromium throttles
                # background timers to about once a minute after 5 minutes.
                "--disable-features=StartupBoost,msEdgeStartupBoost,Translate,IntensiveWakeUpThrottling,CalculateNativeWinOcclusion",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                # no questions: auto-deny permission prompts, allow autoplay
                "--deny-permission-prompts",
                "--autoplay-policy=no-user-gesture-required",
                "--hide-crash-restore-bubble",
                "--disable-session-crashed-bubble",
            ])
            # Because we use a DEDICATED profile dir, this process is the
            # window's own browser instance — it exits when the window closes.
            # Wait for it, then stop the server so the launcher behaves like
            # a single application. Set NOAUTOCLOSE=1 for always-on behavior.
            if os.environ.get("NOAUTOCLOSE", "") != "1":
                if sys.platform.startswith("win"):
                    # msedge.exe may hand off and return immediately on
                    # Windows — watch the profile's processes instead.
                    _watch_profile_then_exit(profile)
                else:
                    proc.wait()
                    print("\n  Deck window closed - stopping server.")
                    os._exit(0)
            return
        except Exception:
            pass
    # Fallback: default browser, normal window.
    try:
        webbrowser.open(url)
    except Exception:
        pass



# Browser-like headers — some feeds (Walla, Israel Hayom) reject bare bots.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
}

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js":   "text/javascript; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png":  "image/png",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
}

# Lenient SSL context — some feed hosts have imperfect cert chains.
# This is a read-only news fetch from public feeds, so this is safe here.
# NOTE: deliberately NOT socket.setdefaulttimeout(). A process-wide default
# also lands on the listening socket and on every accepted client connection,
# so a browser holding a keep-alive open longer than the timeout gets torn
# down mid-session. Outbound calls carry their own explicit timeouts instead.

# Certificates are verified. Until v80 verification was off for every source,
# so any device on the path (a compromised router, a hostile hotspot) could
# have answered "no alert" in Oref's name.
# If a site's certificate cannot be verified on this PC (on Windows, Python
# reads the Windows certificate store, which can lack a root the browser
# would fetch on demand), the request is retried once WITHOUT verification so
# the feed keeps working - and that is recorded and shown in the health panel
# as "certificate not verified", never hidden.
_SSL_CTX = ssl.create_default_context()
_SSL_LOOSE = ssl.create_default_context()
_SSL_LOOSE.check_hostname = False
_SSL_LOOSE.verify_mode = ssl.CERT_NONE
_tls_unverified = {}          # host -> (when, reason) of the last unverified fallback


def _is_cert_error(e):
    r = getattr(e, "reason", None)
    return (isinstance(e, ssl.SSLCertVerificationError) or isinstance(r, ssl.SSLCertVerificationError)
            or "CERTIFICATE_VERIFY_FAILED" in str(e))


def _note_unverified(host, e):
    first = host not in _tls_unverified
    if len(_tls_unverified) < 50 or not first:
        _tls_unverified[host] = (time.time(), str(e)[:120])
    if first:
        try:
            print("  [!] TLS certificate of %s could not be verified - using it unverified (see /health)" % host)
        except Exception:
            pass


def open_url(req, timeout, strict=False):
    """urlopen with a verified certificate, or - when only the certificate
    check fails and strict is off - once more without it, recorded in
    _tls_unverified. strict is on for Oref: an alert feed whose certificate
    cannot be verified may be someone else answering "no alert", so it is a
    failure (shown in red), never a quiet fallback."""
    try:
        return urlopen(req, timeout=timeout, context=_SSL_CTX)
    except Exception as e:
        if strict or not _is_cert_error(e):
            raise
        url = req.full_url if hasattr(req, "full_url") else str(req)
        _note_unverified(urlparse(url).hostname or "?", e)
        return urlopen(req, timeout=timeout, context=_SSL_LOOSE)


def _decompress(raw, encoding):
    """Decompress gzip/deflate bodies (urllib does not do this automatically)."""
    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass
    return raw


def _to_utf8_text(raw, content_type):
    """Decode bytes to a unicode string, then normalize so the browser reads it as UTF-8.
    Tries the header charset, then UTF-8, then Hebrew (windows-1255), then latin-1."""
    charset = None
    if content_type:
        m = re.search(r"charset=([\w-]+)", content_type, re.I)
        if m:
            charset = m.group(1).lower()
    for enc in [charset, "utf-8", "windows-1255", "latin-1"]:
        if not enc:
            continue
        try:
            text = raw.decode(enc)
            # Rewrite the XML prolog encoding so DOMParser trusts UTF-8 after re-encode.
            text = re.sub(r'(<\?xml[^>]*encoding=")[\w-]+(")',
                          r"\1utf-8\2", text, count=1, flags=re.I)
            return text
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_upstream(target):
    """Fetch a URL server-side and return (ok, status, text)."""
    req = Request(target, headers=BROWSER_HEADERS)
    try:
        with open_url(req, TIMEOUT) as resp:
            raw = resp.read()
            raw = _decompress(raw, (resp.headers.get("Content-Encoding") or "").lower())
            text = _to_utf8_text(raw, resp.headers.get("Content-Type") or "")
            return True, resp.status, text
    except Exception as e:
        return False, 502, "Upstream fetch failed: %s" % e


# Pikud HaOref (Home Front Command) red-alert endpoints. These require
# specific headers and are geo-blocked outside Israel — they work from
# Dudi's Israeli IP. We proxy them so the browser can read them (CORS).
OREF_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
    "Referer": "https://www.oref.org.il/",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "keep-alive",
}
OREF_ALERTS = "https://www.oref.org.il/WarningMessages/alert/alerts.json"
# The history endpoint has moved over the years. Try several known-good URLs
# in order; the first that returns a non-empty JSON array wins. The Ajax
# handler (GetAlarmsHistory.aspx) is the current one; the others are fallbacks.
OREF_HISTORY_URLS = [
    "https://www.oref.org.il/warningMessages/alert/History/AlertsHistory.json",
]
# Oref replaced its site in 2026. The old archive handler
# (alerts-history.oref.org.il/.../GetAlarmsHistory.aspx) now redirects to an
# HTML page, and the short AlertsHistory.json above only covers about a day,
# so on a quiet day the deck had no history at all - no list and no maps.
# This is the API the new site itself calls. Periods: lastDay, lastWeek,
# lastMonth. Note the language code is "heb"; "he" answers 400.
OREF_HISTORY_API = "https://www.oref.org.il/api/v1/AlertHistory/heb?period=%s&cities="

# Home Front Command defense policy (מדיניות התגוננות): education, gatherings
# and workplace status per district. The endpoint has moved between versions,
# so several known shapes are tried; if none respond we return a clear
# "unavailable" marker rather than inventing guidance (this is safety data).
OREF_POLICY_PROBE = "https://www.oref.org.il/api/v1/restrictionsPolicy/data/heb/%D7%91%D7%AA%20%D7%99%D7%9D"


def fetch_oref_policy():
    """Defense-policy strip. Disabled since Oref replaced its site (2026):
    the old endpoints are gone and the new per-area one
    (api/v1/restrictionsPolicy/data/heb/<area>) answers HTTP 500 for every
    area. The general page (restrictionsPolicy/heb) carries no per-area
    status, so showing it would be presenting unverified safety guidance.
    The daily self-test probes OREF_POLICY_PROBE and reports the day it
    starts answering JSON again."""
    return False, 502, '{"error":"policy unavailable","source":"oref","why":"upstream endpoint returns 500"}'


# One outstanding request per upstream URL. The deck polls on a timer, so a
# feed that takes longer than its interval used to start a second, third and
# fourth fetch of the same thing — each holding a socket and a thread.
_inflight = {}
_inflight_lock = threading.Lock()


def _begin_fetch(key):
    with _inflight_lock:
        if _inflight.get(key):
            return False
        _inflight[key] = time.time()
        return True


def _end_fetch(key):
    with _inflight_lock:
        _inflight.pop(key, None)


_oref_shared = {}        # url -> (finished_at, result) of the most recent fetch


def _decode_feed(raw):
    """Bytes -> text for the Oref feeds. They are UTF-8 with a BOM today, were
    UTF-16 in the past, and the quiet answer has at times been NUL padding;
    none of that may turn 'quiet' into 'no connection' or the reverse."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    return text.replace("\x00", "").replace("\ufeff", "").strip()


def fetch_oref(url):
    """Fetch an Oref endpoint with the headers it requires. Returns (ok, status, text).
    The realtime endpoint returns an empty body when there's no active alert —
    we normalize that to '[]' so the browser always gets valid JSON.

    Only one request per URL goes upstream at a time. A second caller WAITS for
    that answer and shares it. Until v76 the second caller got an instant 429
    error instead - so two screens polling together (PC + phone, or the deck
    and the self-test) made each other report "no connection to Oref"."""
    started = time.time()
    if not _begin_fetch(url):
        deadline = started + 13
        while time.time() < deadline:
            time.sleep(0.05)
            hit = _oref_shared.get(url)
            if hit and hit[0] >= started:
                return hit[1]                     # the answer that was in flight
            if not _inflight.get(url):
                break                             # it ended before we looked: fetch ourselves
        else:
            return False, 504, '{"error":"oref fetch timed out waiting for an earlier request"}'
        if not _begin_fetch(url):                 # lost the race again: share once more
            time.sleep(0.2)
            hit = _oref_shared.get(url)
            return hit[1] if hit else (False, 429, '{"error":"fetch already in flight"}')
    result = (False, 502, '{"error":"oref fetch failed"}')
    req = Request(url, headers=OREF_HEADERS)
    try:
        # Alerts are never read unverified. The other sources that share this
        # function (markets, quakes, air, flights) keep the recorded fallback:
        # in v81 they were strict too, and on a PC whose certificate store
        # lacks a root, markets went "unavailable".
        strict = (urlparse(url).hostname or "").endswith("oref.org.il")
        with open_url(req, 12, strict=strict) as resp:
            raw = resp.read()
            raw = _decompress(raw, (resp.headers.get("Content-Encoding") or "").lower())
            text = _decode_feed(raw)
            if not text:
                text = "[]"   # quiet = no active alert
            result = (True, resp.status, text)
    except Exception as e:
        why = str(e).replace('"', "'")
        if _is_cert_error(e):
            why = ("the certificate of oref.org.il could not be verified on this PC - "
                   "open https://www.oref.org.il once in Edge, then restart the deck (%s)" % why[:80])
        result = (False, 502, '{"error":"oref fetch failed: %s"}' % why)
    finally:
        now = time.time()
        if len(_oref_shared) > 40:                  # one entry per URL ever fetched otherwise
            for k in [k for k, v in list(_oref_shared.items()) if now - v[0] > 30]:
                _oref_shared.pop(k, None)
        _oref_shared[url] = (now, result)           # publish before releasing the slot
        _end_fetch(url)
    return result


def _normalize_oref_api(text):
    """New API shape -> the flat list the deck has always consumed.
    In : {"alarmsGroupedByDate":[{"key":"13.11.2026","value":[
            {"data":"...","date":"13.11.2026","time":"03:00:07",
             "category":1,"category_Desc":"...","rid":706621}, ...]}]}
    Out: [{"alertDate":"2026-11-13 03:00:07","title":"...","data":"...",
           "category":1,"rid":706621}, ...]  newest first.
    Returns None when the payload is not that shape (an HTML error page)."""
    import json as _json
    try:
        d = _json.loads(text)
    except Exception:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("alarmsGroupedByDate"), list):
        return None
    out = []
    for grp in d["alarmsGroupedByDate"]:
        for it in (grp or {}).get("value") or []:
            try:
                dd, mm, yy = str(it.get("date", "")).split(".")
                tm = str(it.get("time", "")).strip() or "00:00:00"
                if len(tm) == 5:
                    tm += ":00"
                stamp = "%04d-%02d-%02d %s" % (int(yy), int(mm), int(dd), tm)
            except Exception:
                stamp = str(it.get("dateTime", "")).replace("T", " ")
            if not stamp or not it.get("data"):
                continue
            out.append({"alertDate": stamp,
                        "title": it.get("category_Desc") or "",
                        "data": it.get("data"),
                        "category": it.get("category"),
                        "rid": it.get("rid")})
    out.sort(key=lambda r: r["alertDate"], reverse=True)
    return out


_hist_week = {"t": 0.0, "rows": None}
_backfill = {"t": 0.0}


import queue as _queue
_archive_q = _queue.Queue(maxsize=8)


def _archive_writer():
    while True:
        rows = _archive_q.get()
        try:
            dlarchive.add_alerts(rows)
        except Exception:
            pass


def _archive_async(rows):
    """Archive writes never ride on a request thread, and never pile up: one
    writer, a short queue, and a poll that finds the queue full is dropped
    (the next poll carries the same rows)."""
    if not getattr(_archive_async, "started", False):
        _archive_async.started = True
        threading.Thread(target=_archive_writer, daemon=True).start()
    try:
        _archive_q.put_nowait(list(rows))
    except _queue.Full:
        pass


def _backfill_archive(now):
    """Twice a day, pull Oref's last month into the local archive. Without it
    the archive only knows what was polled while this PC was on, and a week
    summary would silently show part of the picture."""
    if now - _backfill["t"] < 12 * 3600:
        return
    _backfill["t"] = now

    def run():
        ok, _s, text = fetch_oref(OREF_HISTORY_API % "lastMonth")
        rows = _normalize_oref_api(text) if ok else None
        if rows:
            dlarchive.add_alerts(rows)
        elif rows is None:
            _backfill["t"] = now - 12 * 3600 + 900       # failed: try again in 15 minutes
    threading.Thread(target=run, daemon=True).start()
_hist_status = {"source": "", "at": 0.0, "rows": 0, "error": ""}


def fetch_oref_history():
    """History for the alerts panel. Returns (ok, status, text).

    1. The new API, last day. Rows there -> done.
    2. A quiet day: the last week from the same API (cached 10 minutes), so
       the panel still shows the most recent event and the maps stay up.
    3. The legacy short file, as a fallback if the API changes again.
    An empty list is a valid answer; an error is reported as an error."""
    import json as _json
    now = time.time()
    api_seen = False
    ok, _st, text = fetch_oref(OREF_HISTORY_API % "lastDay")
    rows = _normalize_oref_api(text) if ok else None
    if rows is not None:
        api_seen = True
        _backfill_archive(now)
        if rows:
            _archive_async(rows)
            _hist_status.update(source="api:lastDay", at=now, rows=len(rows), error="")
            return True, 200, _json.dumps(rows, ensure_ascii=False)
        if ((_hist_week["rows"] is None or now - _hist_week["t"] > 600)
                and now - _hist_week.get("tried", 0) > 60):
            _hist_week["tried"] = now
            ok2, _s2, text2 = fetch_oref(OREF_HISTORY_API % "lastWeek")
            wk = _normalize_oref_api(text2) if ok2 else None
            if wk is not None:
                _hist_week.update(t=now, rows=wk)
                _archive_async(wk)
            elif now - _hist_week["t"] > 1800:
                _hist_week.update(rows=None)        # a week list we cannot refresh is not "the last week"
        if _hist_week["rows"]:
            _hist_status.update(source="api:lastWeek", at=now,
                                rows=len(_hist_week["rows"]), error="")
            return True, 200, _json.dumps(_hist_week["rows"], ensure_ascii=False)
    last = (False, 502, '{"error":"all history endpoints failed"}')
    for url in OREF_HISTORY_URLS:
        ok, status, text = fetch_oref(url)
        if not ok:
            last = (ok, status, text)
            continue
        try:
            data = _json.loads(text)
        except Exception:
            last = (False, 502, '{"error":"history endpoint returned non-JSON"}')
            continue            # an HTML error page, not data
        if isinstance(data, list):
            if data:
                _hist_status.update(source="legacy", at=now, rows=len(data), error="")
                return True, 200, text
            last = (True, 200, "[]")
    if api_seen and not last[0]:
        last = (True, 200, "[]")            # the API answered: genuinely quiet
    if not api_seen and last == (True, 200, "[]"):
        # The API did not answer and the legacy file is empty. An empty legacy
        # file proves nothing (it is empty most of the time): this is an
        # outage, and reporting it as a quiet day would be a lie.
        last = (False, 502, '{"error":"oref history API unavailable"}')
    if api_seen and last == (True, 200, "[]"):
        # Oref only goes back a week. Our own archive goes back further, so the
        # last event stays on the map through a quiet stretch.
        old = dlarchive.recent_alerts(days=30, limit=250)
        if old:
            _hist_status.update(source="archive", at=now, rows=len(old), error="")
            return True, 200, _json.dumps(old, ensure_ascii=False)
    _hist_status.update(source="api" if api_seen else "none", at=now, rows=0,
                        error="" if last[0] else "history unavailable")
    return last


def debug_oref_history():
    """Return a diagnostic JSON showing what each history endpoint returns,
    so the user can see exactly why history may be empty."""
    import json as _json
    report = []
    for url in [OREF_HISTORY_API % "lastDay", OREF_HISTORY_API % "lastWeek"] + OREF_HISTORY_URLS:
        ok, status, text = fetch_oref(url)
        snippet = text[:200] if text else ""
        kind = "unknown"
        try:
            d = _json.loads(text)
            kind = ("array[%d]" % len(d)) if isinstance(d, list) else "object"
        except Exception:
            kind = "non-json (html/error)"
        report.append({"url": url, "ok": ok, "status": status, "kind": kind, "preview": snippet})
    return _json.dumps(report, ensure_ascii=False)


# ---- IDF Spokesperson (official) via Telegram public web preview ----
# The official Hebrew IDF channel mirrors the @idfonline X posts. Telegram's
# public preview (t.me/s/<channel>) is plain HTML with no login or rate limit,
# so it's a reliable source for the real, raw spokesperson statements.
IDF_TG_URL = "https://t.me/s/idf_telegram"


# Earthquakes near Israel. EMSC has far better regional coverage than USGS
# for the Dead Sea Transform; USGS is kept as a fallback. Both are free and
# need no API key.
# Strategic envelope: Yemen in the south up to Turkey/Greece in the north,
# Eastern Mediterranean to eastern Iran. Wide on purpose — shallow weak events
# in this box can be explosion signatures (strikes), not just tectonics.
QUAKE_BBOX = {"minlat": 12.0, "maxlat": 42.0, "minlon": 20.0, "maxlon": 64.0}


def fetch_quakes():
    import json as _json
    b = QUAKE_BBOX
    emsc = ("https://www.seismicportal.eu/fdsnws/event/1/query?format=json&limit=80"
            "&minlat=%(minlat)s&maxlat=%(maxlat)s&minlon=%(minlon)s&maxlon=%(maxlon)s"
            "&minmag=2.5" % b)
    usgs = ("https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&limit=80"
            "&minlatitude=%(minlat)s&maxlatitude=%(maxlat)s"
            "&minlongitude=%(minlon)s&maxlongitude=%(maxlon)s&minmagnitude=2.5" % b)
    for url in (emsc, usgs):
        ok, _s, text = fetch_oref(url)
        if not ok or not text:
            continue
        try:
            d = _json.loads(text)
        except Exception:
            continue
        feats = d.get("features") or []
        out = []
        for f in feats:
            p = f.get("properties") or {}
            g = (f.get("geometry") or {}).get("coordinates") or []
            if len(g) < 2:
                continue
            out.append({
                "mag": p.get("mag") or p.get("magnitude"),
                "place": p.get("flynn_region") or p.get("place") or p.get("region") or "",
                "time": p.get("time") or p.get("origin_time") or p.get("datetime"),
                "lng": g[0], "lat": g[1],
                # EMSC encodes depth as NEGATIVE km (elevation convention);
                # USGS as positive. Normalize to positive km-below-surface.
                "depth": abs(g[2]) if len(g) > 2 and g[2] is not None else None,
            })
        if out:
            return _json.dumps(out, ensure_ascii=False)
    return '{"error":"quakes unavailable"}'


# ---- life indicators: markets, shekel, TLV flight delays ----
# Yahoo chart API (no key, browser UA) for indices+FX; data.gov.il CKAN for
# the Ben-Gurion flight board. Both cached briefly to stay polite.
MARKET_SYMBOLS = [
    ("^TA125.TA", "ת\u05f4א 125"),
    ("^GSPC", "S&P 500"),
    ("^IXIC", "נאסד\u05f4ק"),
    ("USDILS=X", "דולר"),
    ("EURILS=X", "אירו"),
    ("BZ=F", "נפט ברנט"),
]
_markets_cache = {"t": 0, "data": None, "key": None}


def _market_symbols():
    """Symbols come from the config file; the list above is the default."""
    try:
        rows = [(r[0], r[1]) for r in dlconfig.load()["markets"]]
    except Exception:
        rows = list(MARKET_SYMBOLS)
    key = repr(rows)
    if _markets_cache.get("key") != key:        # list edited: drop the cache
        _markets_cache.update(t=0, data=None, key=key)
    return rows


_flights_cache = {"t": 0, "data": None}


def fetch_markets():
    import json as _json
    import time as _time
    import urllib.parse as _up
    now = _time.time()
    if _markets_cache["data"] and now - _markets_cache["t"] < 240:
        return _markets_cache["data"]
    out = []
    for sym, name in _market_symbols():
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=15m"
               % _up.quote(sym))
        ok, _s, text = fetch_oref(url)
        if not ok or not text:
            continue
        try:
            m = _json.loads(text)["chart"]["result"][0]["meta"]
            price = m.get("regularMarketPrice")
            prev = m.get("chartPreviousClose") or m.get("previousClose")
            if price is None or not prev:
                continue
            qt = m.get("regularMarketTime")
            out.append({"sym": sym, "name": name, "price": price,
                        "pct": round((price / prev - 1) * 100, 2),
                        # the exchange's own timestamp for this quote: TASE
                        # runs ~15 min behind, FX and US indices are live
                        "asOf": qt,
                        "tz": m.get("exchangeTimezoneName")})
        except Exception:
            continue
    if out:
        data = _json.dumps(out, ensure_ascii=False)
        _markets_cache.update(t=now, data=data)
        return data
    return _fresh_or_error(_markets_cache, 900, "markets unavailable")


# Waze live-map public feed (no key). Blocked for datacenter IPs but serves
# Israeli residential IPs — same situation as the Oref endpoints, which is
# exactly why this runs through the local server on the user's network.
# Bbox: Gush Dan / center region (Ayalon, Route 1/2/4/20).
# Waze runs Israel as its OWN environment ("il"), separate from the global
# "row" — an Israeli bbox must be asked from the il server or it 403s/empties.
WAZE_URLS = [
    ("https://www.waze.com/live-map/api/georss"
     "?top=32.20&bottom=31.85&left=34.70&right=35.05"
     "&env=il&types=traffic,alerts"),
    ("https://www.waze.com/live-map/api/georss"
     "?top=32.20&bottom=31.85&left=34.70&right=35.05"
     "&env=row&types=traffic,alerts"),
]
# ---- Waze Live Map mirror: a REAL browser window, OBSERVED not spoofed ----
# The dead end was synthetic requests: waze.com gates its feed with reCAPTCHA
# Enterprise, so any hand-made call is 403 forever — and forging that token is
# both impossible and off-limits.
#
# This does the opposite: it opens an ordinary, visible Waze Live Map window
# (its own throwaway profile — never the user's personal one, never the deck's
# browser). reCAPTCHA runs for real; if it ever challenges, the window is on
# screen and a human answers it, which is exactly what the test is for. The
# page then fetches its traffic feed by itself, as it does for any visitor,
# and we merely READ the responses it already received over the DevTools
# protocol. We never issue a request to waze.com, never replay a token, and
# never disable a security mechanism. It is the software equivalent of
# reading a map that is already open on your desk.
MIRROR_PORT = 9334
MIRROR_LAT, MIRROR_LON, MIRROR_ZOOM = 32.017, 34.745, 13   # Bat Yam
MIRROR_STALE_S = 420          # no fresh feed for this long -> reload the page
# Waze routes lat/lon query params to its DIRECTIONS planner (they mean
# "navigate to here"), and the planner never polls the traffic feed. So the
# plain map URL comes first; the rest are rotated through until one of them
# actually produces a feed. Evidence decides, not a guess.
MIRROR_URLS = [
    "https://www.waze.com/live-map",
    "https://www.waze.com/livemap?lon=%s&lat=%s&zoom=%s" % (MIRROR_LON, MIRROR_LAT, MIRROR_ZOOM),
    "https://www.waze.com/en/live-map",
    "https://www.waze.com/live-map?lat=%s&lon=%s&zoom=%s" % (MIRROR_LAT, MIRROR_LON, MIRROR_ZOOM),
]
_MIRROR = {"proc": None, "thread": None, "json": None, "t": 0,
           "state": "off", "note": "", "seen": [], "url_idx": 0, "page_text": "",
           "working_url": None}


def _looks_like_feed(body):
    """The traffic feed identifies itself by its shape, whatever it is called."""
    import json as _json
    if not body or not body.strip().startswith("{"):
        return False
    try:
        d = _json.loads(body)
    except Exception:
        return False
    return isinstance(d, dict) and (isinstance(d.get("jams"), list)
                                    or isinstance(d.get("alerts"), list))


def _mirror_tile_zoom():
    """Which zoom the map is really drawing — read straight off the tile URLs."""
    import re as _re
    zs = []
    for u in (_MIRROR.get("seen") or []):
        m = _re.search(r"/il-tiles/[a-z]+/[a-z]+/(\d+)/", u)
        if m:
            zs.append(int(m.group(1)))
    return sorted(set(zs)) or None


def _mirror_profile():
    import tempfile
    return os.path.join(tempfile.gettempdir(), "dl-waze-live")


def _mirror_spawn():
    """Launch a normal (NOT headless) Waze window in its own profile."""
    exe = _browser_exe()
    if not exe:
        _MIRROR["note"] = "לא נמצא דפדפן (Edge/Chrome)"
        return False
    url = MIRROR_URLS[_MIRROR["url_idx"] % len(MIRROR_URLS)]
    try:
        _MIRROR["proc"] = subprocess.Popen(
            [exe,
             "--remote-debugging-port=%d" % MIRROR_PORT,
             "--remote-allow-origins=*",
             "--user-data-dir=" + _mirror_profile(),
             "--no-first-run", "--no-default-browser-check",
             # keep the feed alive while the window sits minimised behind the deck
             "--disable-background-timer-throttling",
             "--disable-backgrounding-occluded-windows",
             "--disable-renderer-backgrounding",
             "--window-size=560,430", "--window-position=40,40",
             url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        _MIRROR["note"] = str(e)[:90]
        return False


def _mirror_page_ws():
    import json as _json
    for _ in range(60):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % MIRROR_PORT, timeout=2) as r:
                targets = _json.loads(r.read().decode("utf-8", "replace"))
            pages = [t for t in targets if t.get("type") == "page"
                     and "waze" in (t.get("url") or "")]
            if not pages:
                pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                return pages[0].get("webSocketDebuggerUrl")
        except Exception:
            pass
        time.sleep(0.5)
    return None


def _mirror_page_info():
    """What the window is actually showing (catches a reCAPTCHA challenge)."""
    import json as _json
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json/list" % MIRROR_PORT, timeout=2) as r:
            targets = _json.loads(r.read().decode("utf-8", "replace"))
        return [{"title": t.get("title"), "url": (t.get("url") or "")[:120]}
                for t in targets if t.get("type") == "page"]
    except Exception as e:
        return {"error": str(e)[:80]}


def _cdp_roundtrip(sock, msg_id, method, params, deferred, timeout=20):
    """Send a CDP command and wait for its reply, parking any protocol events
    that arrive meanwhile so the listener never loses one."""
    import json as _json
    _ws_send(sock, _json.dumps({"id": msg_id, "method": method,
                                "params": params or {}}))
    sock.settimeout(timeout)
    while True:
        m = _json.loads(_ws_recv_message(sock))
        if m.get("id") == msg_id:
            return m
        if m.get("method"):
            deferred.append(m)


def _mirror_nudge(sock, seq, deferred):
    """A small drag on the map. The live map loads its data on viewport
    change, so this is simply the interaction a person would make."""
    cx, cy = 280, 215
    for ev, x, y in (("mousePressed", cx, cy), ("mouseMoved", cx + 26, cy + 16),
                     ("mouseReleased", cx + 26, cy + 16)):
        seq["n"] += 1
        _cdp_roundtrip(sock, seq["n"], "Input.dispatchMouseEvent",
                       {"type": ev, "x": x, "y": y, "button": "left",
                        "clickCount": 1}, deferred, timeout=8)


def _mirror_zoom_out(sock, seq, deferred, steps=5):
    """Scroll-wheel out, the way a person would, so the map covers the region
    instead of a couple of streets."""
    for _ in range(steps):
        seq["n"] += 1
        try:
            _cdp_roundtrip(sock, seq["n"], "Input.dispatchMouseEvent",
                           {"type": "mouseWheel", "x": 280, "y": 215,
                            "deltaX": 0, "deltaY": 220}, deferred, timeout=8)
        except Exception:
            return
        time.sleep(0.45)


def _mirror_geo_state(sock, seq, deferred):
    """Answer the permission question with evidence, not assumption."""
    seq["n"] += 1
    try:
        m = _cdp_roundtrip(
            sock, seq["n"], "Runtime.evaluate",
            {"expression": "navigator.permissions.query({name:'geolocation'})"
                           ".then(p=>p.state).catch(e=>'err:'+e)",
             "awaitPromise": True, "returnByValue": True}, deferred, timeout=10)
        return (((m.get("result") or {}).get("result") or {}).get("value")) or "?"
    except Exception as e:
        return "err:" + str(e)[:40]


def _mirror_href(sock, seq, deferred):
    seq["n"] += 1
    m = _cdp_roundtrip(sock, seq["n"], "Runtime.evaluate",
                       {"expression": "location.href", "returnByValue": True},
                       deferred, timeout=8)
    return (((m.get("result") or {}).get("result") or {}).get("value")) or ""


def _mirror_page_text(sock, seq, deferred):
    """Whatever the window is displaying, in words — the fastest way to see a
    consent dialog, a challenge or an error instead of guessing."""
    seq["n"] += 1
    try:
        m = _cdp_roundtrip(
            sock, seq["n"], "Runtime.evaluate",
            {"expression": "(document.body&&document.body.innerText||'')"
                           ".replace(/\\s+/g,' ').trim().slice(0,400)",
             "returnByValue": True}, deferred, timeout=8)
        return (((m.get("result") or {}).get("result") or {}).get("value")) or ""
    except Exception:
        return ""


def _mirror_loop():
    """Watch the real window and keep the newest feed it received."""
    import json as _json
    seq = {"n": 1000}
    while True:
        sock = None
        try:
            if _MIRROR["proc"] is None or _MIRROR["proc"].poll() is not None:
                _MIRROR["state"] = "פותח חלון"
                if not _mirror_spawn():
                    time.sleep(30)
                    continue
                time.sleep(3)
            ws = _mirror_page_ws()
            if not ws:
                _MIRROR["state"] = "אין חיבור לחלון"
                time.sleep(15)
                continue
            sock = _ws_connect(ws, timeout=15)
            deferred = []
            seq["n"] += 1
            _cdp_roundtrip(sock, seq["n"], "Network.enable", {}, deferred)
            # A fresh profile makes the map wait on a location prompt, which is
            # very likely why the window "does nothing". We pre-grant it for
            # this throwaway browser only and hand it an approximate position
            # (the same area the deck is already configured for) — no real GPS
            # is exposed, and the user's own browser is untouched.
            for meth, params in (
                    ("Browser.grantPermissions",
                     {"origin": "https://www.waze.com",
                      "permissions": ["geolocation"]}),
                    ("Emulation.setGeolocationOverride",
                     {"latitude": MIRROR_LAT, "longitude": MIRROR_LON,
                      "accuracy": 80})):
                seq["n"] += 1
                try:
                    _cdp_roundtrip(sock, seq["n"], meth, params, deferred, timeout=8)
                except Exception:
                    pass
            _MIRROR["state"] = "מאזין"
            watching = {}
            last_data = time.time()
            next_nudge = time.time() + 14
            zoomed = False
            while True:
                batch = deferred
                deferred = []
                # events parked during a command roundtrip must be handled at
                # once, not after the next blocking read — otherwise a feed can
                # sit unnoticed for up to half a minute
                sock.settimeout(0.2 if batch else 20)
                try:
                    batch = batch + [_json.loads(_ws_recv_message(sock))]
                except (socket.timeout, TimeoutError):
                    pass
                for mm in batch:
                    meth = mm.get("method")
                    p = mm.get("params") or {}
                    if meth == "Network.responseReceived":
                        r = p.get("response") or {}
                        u = r.get("url") or ""
                        mime = (r.get("mimeType") or "").lower()
                        if "waze" in u and r.get("status") == 200:
                            _MIRROR["seen"].append(u.split("?")[0] + "  [" + mime + "]")
                            del _MIRROR["seen"][:-25]
                            # identify the feed by CONTENT, not by URL name:
                            # any JSON the map fetches is a candidate
                            if "json" in mime or "georss" in u.lower():
                                watching[p.get("requestId")] = True
                    elif meth == "Network.loadingFinished":
                        rid = p.get("requestId")
                        if watching.pop(rid, None):
                            seq["n"] += 1
                            try:
                                res = _cdp_roundtrip(
                                    sock, seq["n"], "Network.getResponseBody",
                                    {"requestId": rid}, deferred)
                                body = ((res.get("result") or {}).get("body")) or ""
                                if _looks_like_feed(body):
                                    _MIRROR.update(
                                        json=body, t=time.time(), state="חי",
                                        note="",
                                        working_url=MIRROR_URLS[_MIRROR["url_idx"]
                                                                % len(MIRROR_URLS)])
                                    last_data = time.time()
                            except Exception:
                                pass
                # a person would drag the map; the app fetches on viewport change
                if time.time() > next_nudge:
                    try:
                        href = _mirror_href(sock, seq, deferred)
                        if "/directions" in href:
                            # wrong screen entirely — try the next candidate now
                            _MIRROR["url_idx"] += 1
                            _MIRROR["state"] = "מחליף כתובת"
                            seq["n"] += 1
                            _cdp_roundtrip(sock, seq["n"], "Page.navigate",
                                           {"url": MIRROR_URLS[_MIRROR["url_idx"]
                                                               % len(MIRROR_URLS)]},
                                           deferred, timeout=12)
                            last_data = time.time()
                        else:
                            _MIRROR["page_text"] = _mirror_page_text(
                                sock, seq, deferred)
                            if not zoomed:
                                _MIRROR["geo"] = _mirror_geo_state(
                                    sock, seq, deferred)
                                _mirror_zoom_out(sock, seq, deferred)
                                zoomed = True
                            _mirror_nudge(sock, seq, deferred)
                    except Exception:
                        pass
                    next_nudge = time.time() + (90 if _MIRROR.get("json") else 40)
                # still nothing after a while -> rotate to the next candidate
                wait = MIRROR_STALE_S if _MIRROR.get("json") else 130
                if time.time() - last_data > wait:
                    if not _MIRROR.get("json"):
                        _MIRROR["url_idx"] += 1
                    seq["n"] += 1
                    _cdp_roundtrip(sock, seq["n"], "Page.navigate",
                                   {"url": MIRROR_URLS[_MIRROR["url_idx"]
                                                       % len(MIRROR_URLS)]},
                                   deferred, timeout=12)
                    last_data = time.time()
        except Exception as e:
            _MIRROR["note"] = str(e)[:90]
            _MIRROR["state"] = "מתחבר מחדש"
            time.sleep(8)
        finally:
            try:
                if sock:
                    sock.close()
            except Exception:
                pass


def _mirror_enabled():
    """Kept, but off unless explicitly switched on with a `waze-mirror.on`
    file. Waze renders its traffic layer as server-side raster tiles
    (il-tiles/live/base/*.png) — verified from the live window — so there is
    no data feed to read. If that ever changes, the plumbing is still here."""
    # Superseded by Waze's official iframe embed, which the deck's own browser
    # renders directly — no hidden window, no CDP, no scraping. Kept only as an
    # opt-in experiment via a `waze-mirror.on` file.
    return os.path.isfile(os.path.join(ROOT, "waze-mirror.on"))


def _mirror_start():
    if not _mirror_enabled():
        _MIRROR["state"] = "כבוי"
        return
    th = _MIRROR.get("thread")
    if th is None or not th.is_alive():
        th = threading.Thread(target=_mirror_loop, daemon=True)
        _MIRROR["thread"] = th
        th.start()


def _mirror_stop():
    p = _MIRROR.get("proc")
    try:
        if p and p.poll() is None:
            p.terminate()
    except Exception:
        pass


def _shape_waze(d):
    """Waze feed -> the deck's traffic summary."""
    jams = d.get("jams") or []
    alerts = d.get("alerts") or []
    heavy = [j for j in jams if (j.get("level") or 0) >= 4]
    total_km = sum((j.get("length") or 0) for j in jams) / 1000.0
    worst = None
    for j in jams:
        if j.get("street") and (worst is None
                                or (j.get("delay") or 0) > (worst.get("delay") or 0)):
            worst = j
    accidents = sum(1 for a in alerts if str(a.get("type")) == "ACCIDENT")
    return {
        "jams": len(jams), "heavy": len(heavy), "km": round(total_km, 1),
        "accidents": accidents,
        "src": "waze",
        "ageMin": None,
        "worst": ({"street": worst.get("street"),
                   "delayMin": round((worst.get("delay") or 0) / 60.0)}
                  if worst else None),
    }


def _traffic_from_mirror():
    import json as _json
    raw = _MIRROR.get("json")
    if not raw:
        return None
    try:
        out = _shape_waze(_json.loads(raw))
    except Exception:
        return None
    out["ageMin"] = round(max(0, time.time() - (_MIRROR.get("t") or 0)) / 60.0)
    if out["ageMin"] > 25:        # stale beyond usefulness — say nothing
        return None
    return out


_traffic_cache = {"t": 0, "data": None}


# ---- browser-grade fetch chain ----
# Google/Waze filter by TLS fingerprint, not IP: the user's browser passes
# while python-urllib is rejected. So we escalate through progressively more
# browser-like fetchers, ending with Edge itself — the literal browser the
# user navigates Waze with, indistinguishable by definition.
def _fetch_via_curl(url):
    """Windows ships curl.exe (Schannel TLS = OS fingerprint)."""
    import subprocess
    import shutil as _sh
    curl = _sh.which("curl") or r"C:\Windows\System32\curl.exe"
    if not curl or not os.path.isfile(curl):
        return None
    try:
        out = subprocess.run(
            [curl, "-s", "--max-time", "12", "--compressed",
             "-A", OREF_HEADERS["User-Agent"],
             "-H", "Referer: https://www.waze.com/live-map/",
             "-H", "Accept: application/json",
             "-H", "Accept-Language: he-IL,he;q=0.9,en;q=0.8",
             url],
            capture_output=True, timeout=16)
        t = out.stdout.decode("utf-8", "replace").strip()
        if t.startswith("{"):
            return t
    except Exception:
        pass
    return None


# ---- hidden mini-browser with a REAL Waze session (CDP over stdlib) ----
# A dedicated headless Edge/Chrome lives in its own throwaway profile, opens
# waze.com/live-map once (establishing genuine cookies/session), and every
# poll we execute fetch() INSIDE that page via the DevTools protocol —
# same-origin, indistinguishable from the site's own traffic. The deck's
# main browser and the user's personal profile are never touched, and no
# security flag is disabled (same-origin needs none).
import base64 as _b64
import struct as _struct

_WAZE_BROWSER = {"proc": None, "port": 9333, "ready": False}


def _browser_exe():
    for exe in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if os.path.isfile(exe):
            return exe
    import shutil as _sh
    return _sh.which("msedge") or _sh.which("chrome") or _sh.which("chromium") \
        or _sh.which("chromium-browser") or _sh.which("google-chrome")


def _ws_connect(ws_url, timeout=10):
    """Minimal RFC6455 client over stdlib socket (text frames only)."""
    from urllib.parse import urlparse as _up
    u = _up(ws_url)
    s = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
    key = _b64.b64encode(os.urandom(16)).decode()
    path = u.path + (("?" + u.query) if u.query else "")
    s.sendall((
        "GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n" % (path, u.hostname, u.port, key)
    ).encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            raise OSError("ws handshake failed")
        resp += chunk
    if b" 101 " not in resp.split(b"\r\n", 1)[0]:
        raise OSError("ws upgrade rejected")
    return s


def _ws_send(s, text):
    payload = text.encode("utf-8")
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    if n < 126:
        head = _struct.pack("!BB", 0x81, 0x80 | n)
    elif n < 65536:
        head = _struct.pack("!BBH", 0x81, 0x80 | 126, n)
    else:
        head = _struct.pack("!BBQ", 0x81, 0x80 | 127, n)
    s.sendall(head + mask + masked)


def _ws_recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise OSError("ws closed")
        buf += chunk
    return buf


def _ws_recv_message(s):
    """Receive one complete (possibly fragmented) message; answer pings."""
    parts = []
    while True:
        b1, b2 = _ws_recv_exact(s, 2)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        ln = b2 & 0x7F
        if ln == 126:
            ln = _struct.unpack("!H", _ws_recv_exact(s, 2))[0]
        elif ln == 127:
            ln = _struct.unpack("!Q", _ws_recv_exact(s, 8))[0]
        data = _ws_recv_exact(s, ln) if ln else b""
        if opcode == 0x9:                     # ping -> pong
            mask = os.urandom(4)
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            s.sendall(_struct.pack("!BB", 0x8A, 0x80 | len(data)) + mask + masked)
            continue
        if opcode == 0x8:
            raise OSError("ws closed by peer")
        parts.append(data)
        if fin:
            return b"".join(parts).decode("utf-8", "replace")


def _cdp_call(s, msg_id, method, params=None, timeout=25):
    import json as _json
    _ws_send(s, _json.dumps({"id": msg_id, "method": method,
                             "params": params or {}}))
    s.settimeout(timeout)
    while True:
        m = _json.loads(_ws_recv_message(s))
        if m.get("id") == msg_id:
            return m


def _waze_browser_ensure():
    """Spawn (or reuse) the hidden browser; return a page websocket url."""
    import subprocess
    import tempfile
    import json as _json
    import urllib.request
    port = _WAZE_BROWSER["port"]
    proc = _WAZE_BROWSER["proc"]
    if proc is None or proc.poll() is not None:
        exe = _browser_exe()
        if not exe:
            return None
        prof = os.path.join(tempfile.gettempdir(), "dl-waze-session")
        proc = subprocess.Popen(
            [exe, "--headless=new", "--disable-gpu", "--no-first-run",
             "--disable-extensions", "--mute-audio",
             "--remote-debugging-port=%d" % port,
             "--remote-allow-origins=*",
             "--user-data-dir=" + prof, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _WAZE_BROWSER.update(proc=proc, ready=False)
    # wait for the debug endpoint
    for _ in range(40):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % port, timeout=2) as r:
                targets = _json.loads(r.read().decode())
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.4)
    return None


def _fetch_via_session(georss_url):
    """fetch() executed inside a live waze.com page — a real session."""
    import json as _json
    ws_url = _waze_browser_ensure()
    if not ws_url:
        return None
    try:
        s = _ws_connect(ws_url)
    except Exception:
        return None
    try:
        if not _WAZE_BROWSER["ready"]:
            _cdp_call(s, 1, "Page.navigate",
                      {"url": "https://www.waze.com/live-map/"})
            time.sleep(7)                     # let the app boot + set cookies
            _WAZE_BROWSER["ready"] = True
        expr = ("fetch(%s, {headers:{'Accept':'application/json'}})"
                ".then(async r => r.status + '|' + await r.text())"
                ".catch(e => 'ERR|' + e)"
                % _json.dumps(georss_url))
        m = _cdp_call(s, 2, "Runtime.evaluate",
                      {"expression": expr, "awaitPromise": True,
                       "returnByValue": True}, timeout=30)
        val = (((m.get("result") or {}).get("result") or {}).get("value")) or ""
        _WAZE_BROWSER["last_val"] = val[:200]     # for diagnostics
        status, _, body = val.partition("|")
        if status == "200" and body.strip().startswith("{"):
            return body
        # session may have gone stale — renavigate once next time
        _WAZE_BROWSER["ready"] = False
    except Exception:
        _WAZE_BROWSER["ready"] = False
    finally:
        try:
            s.close()
        except Exception:
            pass
    return None


WAZE_PROBE_HTML = """<!doctype html><meta charset="utf-8"><body><script>
(async () => {
  const urls = %s;
  for (const u of urls) {
    try {
      const r = await fetch(u, { headers: { 'Accept': 'application/json' } });
      if (!r.ok) continue;
      const t = await r.text();
      if (t && t.trim().startsWith('{')) { document.body.textContent = t; document.title = 'OK'; return; }
    } catch (e) {}
  }
  document.title = 'FAIL';
})();
</script></body>"""


def _fetch_via_edge(url):
    """Headless Edge --dump-dom: a genuine Chromium request, byte-identical
    fingerprint to the user's own browsing. The heavy artillery."""
    import subprocess
    import tempfile
    import re as _re
    for exe in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.isfile(exe):
            break
    else:
        return None
    prof = os.path.join(tempfile.gettempdir(), "dl-fetch-profile")
    # A raw JSON URL makes headless Chromium DOWNLOAD instead of render, so
    # we point it at our local probe page: the fetch() then runs inside the
    # browser itself — genuine Chromium network stack end to end.
    # --disable-web-security lifts CORS only inside this throwaway headless
    # instance that visits nothing but localhost.
    probe_url = "http://127.0.0.1:%d/waze-probe" % PORT
    try:
        out = subprocess.run(
            [exe, "--headless=new", "--disable-gpu", "--no-first-run",
             "--disable-extensions", "--disable-web-security",
             "--user-data-dir=" + prof,
             "--virtual-time-budget=9000",
             "--dump-dom", probe_url],
            capture_output=True, timeout=35)
        html = out.stdout.decode("utf-8", "replace")
        m = _re.search(r"\{.*\}", html, _re.DOTALL)
        if m:
            return m.group(0)
    except Exception:
        pass
    return None


# ---- Google Routes API (official, uses the user's own Cloud key) ----
# Activated by dropping the key string into a "google.key" file next to this
# server. Cost-guarded by design: 4 arteries, 40-min cache by day / 60-min at
# night ==> ~3.8K calls/month, inside the free tier.
# ---- live road incidents (free, no key, no account) ----
# Israeli news desks publish road closures / crashes as plain server-rendered
# HTML. Only items from the last 24h are kept — see the ObjectId trick below.
ROADS_SOURCES = [
    ("https://hamal.co.il/%D7%A2%D7%95%D7%9E%D7%A1%D7%99_%D7%AA%D7%A0%D7%95%D7%A2%D7%94-26", "עומס"),
    ("https://hamal.co.il/%D7%97%D7%A1%D7%99%D7%9E%D7%AA_%D7%9B%D7%91%D7%99%D7%A9%D7%99%D7%9D-81", "חסימה"),
]
ROADS_LOCAL = re.compile(
    r"איילון|השלום|רוקח|נמיר|גהה|כביש 1\b|כביש 2\b|כביש 4\b|כביש 5\b|כביש 20\b|"
    r"תל אביב|רמת גן|בני ברק|חולון|בת ים|פתח תקו|ראשון לציון|הרצליה|גבעתיים|"
    r"מחלף|גשר|קריה")
_roads_cache = {"t": 0, "data": None}


# ---- air quality (free, no key, no account) ----
# A household deck should say when the air itself is the hazard: dust storms,
# fires, an industrial incident, smoke after a strike. open-meteo serves this
# without a key, so the paid Google quota stays reserved for traffic.
_air_cache = {"t": 0, "data": None}


# ---- source logos ----
# Fetched once through the deck and cached on disk, so the news list never
# waits on the network to paint and works offline afterwards. Logos are used
# only to attribute the outlet that published a headline.
LOGO_DOMAINS = {
    "n12": "www.n12.co.il", "ynet": "www.ynet.co.il", "walla": "www.walla.co.il",
    "kan": "www.kan.org.il", "maariv": "www.maariv.co.il",
    "hayom": "www.israelhayom.co.il", "arutz7": "www.inn.co.il",
    "toi": "www.timesofisrael.com", "jpost": "www.jpost.com",
    "bbc": "www.bbc.com", "nyt": "www.nytimes.com",
    "guardian": "www.theguardian.com", "cnn": "www.cnn.com",
    "reuters": "www.reuters.com", "apnews": "apnews.com", "afp": "www.afp.com",
    "wsj": "www.wsj.com", "almonitor": "www.al-monitor.com", "dw": "www.dw.com",
}
LOGO_DIR = os.path.join(ROOT, "logos")
_logo_mem = {}


def fetch_logo(sid):
    """Return PNG bytes for a source id, or None. Cached on disk forever."""
    import urllib.request
    if sid in _logo_mem:
        return _logo_mem[sid]
    dom = LOGO_DOMAINS.get(sid)
    if not dom:
        return None
    path = os.path.join(LOGO_DIR, sid + ".png")
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 40:
            data = open(path, "rb").read()
            _logo_mem[sid] = data
            return data
    except Exception:
        pass
    url = "https://www.google.com/s2/favicons?domain=%s&sz=64" % dom
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_HEADERS["User-Agent"]})
        with open_url(req, 10) as r:
            data = r.read()
        if len(data) < 40:
            return None
        os.makedirs(LOGO_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        _logo_mem[sid] = data
        return data
    except Exception:
        return None


def fetch_air(lat, lng):
    import json as _json
    import time as _t
    now = _t.time()
    key = "%.3f,%.3f" % (lat, lng)
    if _air_cache["data"] and _air_cache.get("key") == key and now - _air_cache["t"] < 900:
        return _air_cache["data"]
    url = ("https://air-quality-api.open-meteo.com/v1/air-quality"
           "?latitude=%s&longitude=%s"
           "&current=european_aqi,pm10,pm2_5,dust,ozone,nitrogen_dioxide"
           "&timezone=Asia%%2FJerusalem" % (lat, lng))
    ok, _s, text = fetch_oref(url)
    if not ok or not text:
        return (_fresh_or_error(_air_cache, 900, "air unavailable") if _air_cache.get("key") == key
                else '{"error":"air unavailable"}')
    try:
        c = _json.loads(text)["current"]
    except Exception:
        return (_fresh_or_error(_air_cache, 900, "air unavailable") if _air_cache.get("key") == key
                else '{"error":"air unavailable"}')
    aqi = c.get("european_aqi")
    dust = c.get("dust") or 0
    pm25 = c.get("pm2_5") or 0
    # European AQI bands, plus a dust override: a sand storm can be breathable
    # by AQI yet still be the thing you close the windows for.
    if aqi is None:
        level, label = "unknown", "לא ידוע"
    elif aqi <= 20:
        level, label = "good", "טובה"
    elif aqi <= 40:
        level, label = "fair", "סבירה"
    elif aqi <= 60:
        level, label = "moderate", "בינונית"
    elif aqi <= 80:
        level, label = "poor", "ירודה"
    elif aqi <= 100:
        level, label = "bad", "גרועה"
    else:
        level, label = "severe", "מסוכנת"
    note = None
    if dust >= 100:
        level, label, note = "severe", "סופת אבק", "אבק חורג — מומלץ לסגור חלונות"
    elif dust >= 40:
        if level in ("good", "fair"):
            level, label = "moderate", "אובך"
        note = "ריכוז אבק גבוה"
    elif pm25 >= 55 and level in ("good", "fair", "moderate"):
        level, label, note = "poor", "עשן/חלקיקים", "ריכוז חלקיקים גבוה"
    data = _json.dumps({"aqi": aqi, "level": level, "label": label, "note": note,
                        "pm25": round(pm25, 1), "pm10": round(c.get("pm10") or 0, 1),
                        "dust": round(dust, 1), "at": c.get("time"),
                        "src": "open-meteo"}, ensure_ascii=False)
    _air_cache.update(t=now, data=data, key=key)
    return data


def fetch_roads():
    """Road INCIDENTS (closures, crashes) published in the last 24 hours.
    Normally silent — it is an incident channel, not a congestion meter."""
    import html as _html
    import json as _json
    import time as _t
    now = _t.time()
    if _roads_cache["data"] and now - _roads_cache["t"] < 300:
        return _roads_cache["data"]
    items, seen = [], set()
    answered = 0
    for url, tag in ROADS_SOURCES:
        ok, _s, text = fetch_oref(url)
        if not ok or not text or "<article" not in text:
            continue
        answered += 1
        for block in re.findall(r"<article.*?</article>", text, re.S):
            m = re.search(r"<h\d[^>]*>(.*?)</h\d>", block, re.S)
            if not m:
                continue
            # The visible date is filled in by client JS, so the server HTML
            # carries none. The article id is a Mongo ObjectId whose first
            # four bytes ARE the publish timestamp — decode it and drop
            # anything old. Without this the page reads as "live" while
            # serving items from 2024.
            oid = re.search(r'id="([0-9a-f]{24})"', block)
            if not oid:
                continue
            age_h = (_t.time() - int(oid.group(1)[:8], 16)) / 3600.0
            if age_h > 24:
                continue
            title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
            title = re.sub(r"\s+", " ", title)
            if len(title) < 12 or title in seen:
                continue
            # section headers and nav crumbs are not reports
            if title.startswith("#") or title in ("עומסי תנועה", "חסימת כבישים"):
                continue
            seen.add(title)
            link = re.search(r'href="([^"]+)"', block)
            href = link.group(1) if link else ""
            if href.startswith("/"):
                href = "https://hamal.co.il" + href
            items.append({"title": title, "link": href, "tag": tag,
                          "agoH": round(age_h, 1),
                          "local": bool(ROADS_LOCAL.search(title))})
    if not answered:
        return '{"error":"roads unavailable"}'
    if not items:
        # nothing published in the last 24h: that is a real answer, not a fault
        data = _json.dumps({"items": [], "local": 0,
                            "checkedAt": _t.strftime("%H:%M")}, ensure_ascii=False)
        _roads_cache.update(t=now, data=data)
        return data
    # anything naming a Gush Dan artery comes first
    items.sort(key=lambda i: (not i["local"],))
    local_n = sum(1 for i in items if i["local"])
    if local_n and not _roads_cache.get("notified"):
        # a fresh local closure/crash: ask the brain to verify right now
        try:
            brain_trigger("road-incident")
        except Exception:
            pass
        _roads_cache["notified"] = True
    elif not local_n:
        _roads_cache["notified"] = False
    data = _json.dumps({"items": items[:14], "local": local_n,
                        "checkedAt": _t.strftime("%H:%M")}, ensure_ascii=False)
    _roads_cache.update(t=now, data=data)
    return data


# ---- Traffic Brain (שירות עצמאי, אותה תיקייה, פורט 3200) ----
# The brain owns the model, the API key and the budget. The deck only relays,
# so there is exactly one place that can ever spend a Google call.
BRAIN_URL = os.environ.get("BRAIN_URL", "http://127.0.0.1:3200")
_brain_state = {"up": False, "checked": 0}


def brain_get(path, timeout=4):
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(BRAIN_URL + path, timeout=timeout) as r:
            return _json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def brain_trigger(reason):
    """Tell the brain that reality just diverged from routine."""
    import urllib.parse as _up
    brain_get("/trigger?reason=" + _up.quote(str(reason)[:40]), timeout=2)


def fetch_traffic():
    """Relay only. The Traffic Brain (brain.py, port 3200) owns the model, the
    API key and the budget — having a second spender here could quietly double
    the monthly usage, so the deck never calls Google itself."""
    import json as _json
    import time as _t
    snap = brain_get("/traffic")
    if snap and snap.get("segments"):
        _brain_state.update(up=True, checked=_t.time())
        _traffic_cache.update(t=_t.time(), data=_json.dumps(snap, ensure_ascii=False))
        return _traffic_cache["data"]
    _brain_state.update(up=False, checked=_t.time())
    if _traffic_cache["data"] and _t.time() - _traffic_cache["t"] < 900:
        return _traffic_cache["data"]          # brain restarting: show last known
    return _json.dumps({"error": "brain_down"}, ensure_ascii=False)


atexit.register(_mirror_stop)


FLIGHTS_RES = "e83f763b-b7d7-479e-b172-ae981ddc6de5"


def fetch_flights():
    """Summarize TLV board: average delay + heavily-delayed counts, per
    direction, over the recent window."""
    import json as _json
    import time as _time
    from datetime import datetime, timedelta
    now = _time.time()
    if _flights_cache["data"] and now - _flights_cache["t"] < 480:
        return _flights_cache["data"]
    url = ("https://data.gov.il/api/3/action/datastore_search?resource_id=%s&limit=1500"
           % FLIGHTS_RES)
    ok, _s, text = fetch_oref(url)
    if not ok or not text:
        return _fresh_or_error(_flights_cache, 900, "flights unavailable")
    try:
        recs = _json.loads(text)["result"]["records"]
    except Exception:
        return _fresh_or_error(_flights_cache, 900, "flights unavailable")
    # The board publishes Israel local time. Deriving "now" from the machine
    # clock silently breaks anywhere that is not on Israel time, so ask for
    # the zone explicitly and only fall back to local time if unavailable.
    try:
        from zoneinfo import ZoneInfo
        t_now = datetime.now(ZoneInfo("Asia/Jerusalem")).replace(tzinfo=None)
    except Exception:
        t_now = datetime.now()
    lo, hi = t_now - timedelta(hours=5), t_now + timedelta(hours=3)
    # The board also publishes UPCOMING flights with an expected time. Those
    # are a forecast, not a measurement, so they are counted separately —
    # averaging them together quietly diluted the real figure.
    # EXACT match, never substring: the board also uses "לא סופי" (= not
    # final), and a substring test counted 942 pending flights as completed,
    # inventing zero delays and future timestamps.
    DONE = ("נחתה", "המריאה", "סופי")
    agg = {"D": {"n": 0, "sum": 0, "late": 0, "cancel": 0, "soon": 0, "soonSum": 0},
           "A": {"n": 0, "sum": 0, "late": 0, "cancel": 0, "soon": 0, "soonSum": 0}}
    feed_at = None
    for r in recs:
        try:
            sched = datetime.fromisoformat(str(r.get("CHSTOL"))[:19])
        except Exception:
            continue
        if not (lo <= sched <= hi):
            continue
        d = agg.get(str(r.get("CHAORD") or "").strip())
        if d is None:
            continue
        status = str(r.get("CHRMINH") or "")
        if "בוטל" in status or "מבוטל" in status:
            d["cancel"] += 1
            continue
        try:
            actual = datetime.fromisoformat(str(r.get("CHPTOL"))[:19])
        except Exception:
            continue
        delay = max(0, (actual - sched).total_seconds() / 60.0)
        # exact status AND the event must already be in the past
        if status.strip() in DONE and actual <= t_now:
            d["n"] += 1
            d["sum"] += delay
            if delay >= 20:
                d["late"] += 1
            if feed_at is None or actual > feed_at:
                feed_at = actual        # how current the board really is
        else:
            d["soon"] += 1
            d["soonSum"] += delay
    def pack(d):
        return {"n": d["n"], "avg": round(d["sum"] / d["n"], 1) if d["n"] else 0,
                "late": d["late"], "cancel": d["cancel"],
                "soon": d["soon"],
                "soonAvg": round(d["soonSum"] / d["soon"], 1) if d["soon"] else 0}
    data = _json.dumps({"dep": pack(agg["D"]), "arr": pack(agg["A"]),
                        "feedAt": feed_at.isoformat(timespec="minutes") if feed_at else None,
                        "checkedAt": t_now.strftime("%H:%M")},
                       ensure_ascii=False)
    _flights_cache.update(t=now, data=data)
    return data


def _scrape_tg(url, label):
    """Shared Telegram public-preview parser (per-message blocks so text,
    time and link never desync)."""
    import json as _json
    import html as _html
    ok, _status, raw = fetch_oref(url)
    if not ok or not raw:
        return '{"error":"telegram fetch failed: %s"}' % label

    # Telegram now serves parts of the preview JSON-escaped (\" instead of ").
    # Normalize first so one parser handles both the plain and escaped shapes.
    if '\\"tgme_widget_message' in raw:
        raw = raw.replace('\\/', '/').replace('\\"', '"').replace('\\n', '\n')

    def clean(t):
        t = re.sub(r'<br\s*/?>', '\n', t)
        t = re.sub(r'<a [^>]*>', '', t)
        t = re.sub(r'</a>', '', t)
        t = re.sub(r'<[^>]+>', '', t)
        return _html.unescape(t).strip()

    # Anchor on the message TEXT div (stable across markup revisions) and take
    # the time/link from the footer that follows it, rather than depending on
    # the wrapper element's exact class string.
    out = []
    for m in re.finditer(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', raw, re.DOTALL):
        txt = clean(m.group(1))
        if not txt:
            continue
        tail = raw[m.end():m.end() + 4000]
        tm = re.search(r'<time[^>]*datetime="([^"]+)"', tail)
        ln = re.search(r'<a class="tgme_widget_message_date"[^>]*href="([^"]+)"', tail)
        out.append({"text": txt, "time": tm.group(1) if tm else None,
                    "link": ln.group(1) if ln else None})
    if not out:
        return '{"error":"telegram parse failed: %s"}' % label
    return _json.dumps(out, ensure_ascii=False)


# Channels the deck may read. Kept as an explicit allow-list so the endpoint
# can never be turned into an open proxy. "official" marks government /
# emergency-service sources; the rest are press or OSINT and are labelled as
# unverified in the UI.
TG_CHANNELS = {
    "idf_telegram": {"name": "דובר צה\u05f4ל", "official": True},
    "MDAISRAEL":    {"name": "מגן דוד אדום", "official": True},
    "kann_news":    {"name": "כאן חדשות", "official": False},
    "newssil":      {"name": "חדשות ישראל", "official": False},
    "abualiexpress": {"name": "אבו עלי אקספרס", "official": False},
    # foreign OSINT — events in Israel that local outlets are barred from
    # reporting (censorship / gag orders) surface here first
    "osintdefender": {"name": "OSINTdefender", "official": False},
    "clashreport": {"name": "Clash Report", "official": False},
    "Middle_East_Spectator": {"name": "Middle East Spectator", "official": False},
}


def fetch_telegram(channel):
    """Scrape a public Telegram channel preview (t.me/s/<channel>).
    Returns JSON: [{text, time, link}, ...] newest-last."""
    if channel not in TG_CHANNELS:
        return '{"error":"channel not allowed"}'
    return _scrape_tg("https://t.me/s/%s" % channel, channel)


def fetch_idf_spokesperson():
    """Scrape the latest IDF Spokesperson posts from the official Telegram
    public preview. Returns JSON: [{text, time, link}, ...] newest-last.

    We split the page into per-message blocks and extract text+time+link from
    each block together, so they always stay aligned (some posts contain extra
    text nodes that would desync a naive index-by-index pairing)."""
    import json as _json
    import html as _html
    ok, status, raw = fetch_upstream(IDF_TG_URL)
    if not ok or not raw:
        return '{"error":"idf telegram fetch failed"}'

    def clean(t):
        t = re.sub(r'<br\s*/?>', '\n', t)
        t = re.sub(r'<a [^>]*>', '', t)
        t = re.sub(r'</a>', '', t)
        t = re.sub(r'<[^>]+>', '', t)
        return _html.unescape(t).strip()

    # Each post is wrapped in a div whose class starts with
    # "tgme_widget_message ". Split on that boundary and parse each chunk.
    chunks = raw.split('class="tgme_widget_message ')
    out = []
    for ch in chunks[1:]:
        mt = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', ch, re.DOTALL)
        if not mt:
            continue
        txt = clean(mt.group(1))
        if not txt:
            continue
        tm = re.search(r'<time[^>]*datetime="([^"]+)"', ch)
        ln = re.search(r'<a class="tgme_widget_message_date" href="([^"]+)"', ch)
        out.append({
            "text": txt,
            "time": tm.group(1) if tm else None,
            "link": ln.group(1) if ln else None,
        })
    if not out:
        return '{"error":"idf telegram parse failed"}'
    return _json.dumps(out, ensure_ascii=False)




# The only files the deck ever needs from disk. Everything else the page uses
# comes from an endpoint. Nothing in this folder besides these is servable.
STATIC_FILES = {"index.html"}
_SEGMENT_OK = re.compile(r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)*$")


def static_path(url_path):
    """Absolute path of a servable file, or None. Pure function so the tests
    can replay it with Windows path rules."""
    name = "index.html" if url_path in ("/", "") else url_path
    if not name.startswith("/") and name != "index.html":
        return None
    name = name.lstrip("/")
    # one plain segment only: no separators, drive letters, streams, short
    # names, traversal, trailing dots or spaces, control characters
    if not _SEGMENT_OK.match(name) or name.lower() not in STATIC_FILES:
        return None
    full = os.path.join(ROOT, name)
    try:
        if os.path.commonpath([os.path.realpath(full), os.path.realpath(ROOT)]) != os.path.realpath(ROOT):
            return None
    except Exception:
        return None
    return full if os.path.isfile(full) else None


class _NotPublic(Exception):
    """The target is this machine or the home network."""


class _Unresolvable(Exception):
    """The name could not be resolved - an ordinary outage, not an attack."""


def _public_addresses(host, port):
    """Resolve host and return its addresses, all of them public. Raises
    _NotPublic if ANY address is loopback, private, link-local, carrier-grade
    NAT (100.64/10) or otherwise not a public web address, and _Unresolvable
    when the name does not resolve (the internet is down, DNS is broken)."""
    import ipaddress
    host = (host or "").strip().lower().strip("[]")
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise _NotPublic(host)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as e:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise _Unresolvable(str(e))
        raise _NotPublic(host)
    out = []
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped                     # ::ffff:127.0.0.1 is 127.0.0.1
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified or (ip.version == 4 and ip in cgnat)):
            raise _NotPublic(str(ip))
        out.append(str(ip))
    if not out:
        raise _Unresolvable("no address")
    return out


def _public_target(url):
    """True when url is an ordinary public web address (see fetch_public)."""
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        _public_addresses(u.hostname, u.port or (443 if u.scheme == "https" else 80))
        return True
    except Exception:
        return False


def _pinned_connection(scheme, host, port, ip, timeout, ctx=None):
    """A connection that goes to the address we validated - not to whatever
    the name resolves to a moment later (DNS rebinding)."""
    import http.client
    ctx = ctx or _SSL_CTX

    if scheme == "https":
        class Conn(http.client.HTTPSConnection):
            def connect(self):
                sock = socket.create_connection((ip, self.port), self.timeout)
                try:
                    self.sock = ctx.wrap_socket(sock, server_hostname=self.host)
                except Exception:
                    sock.close()
                    raise
        return Conn(host, port, timeout=timeout, context=ctx)

    class Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((ip, self.port), self.timeout)
    return Conn(host, port, timeout=timeout)


FETCH_DEADLINE = 25              # seconds for the whole fetch, redirects and body included
_FETCH_HEADERS = dict(BROWSER_HEADERS, **{"Accept-Encoding": "gzip, deflate"})
FETCH_MAX_WIRE = 6 * 1024 * 1024     # bytes as sent
FETCH_MAX_BODY = 10 * 1024 * 1024    # bytes after decompression

# A 200 "checking your browser" page is not the feed. Treating it as the feed
# would also store it as the source's last good answer.
_BLOCK_PAGE = re.compile(r"<title>\s*(Just a moment|Attention Required|Access denied|Please Wait\b)"
                         r"|challenge-platform|cf-browser-verification|captcha-delivery", re.I)


class _FetchLimit(Exception):
    pass


class _Deadline:
    """Closes every socket of a fetch when its time is up. Socket timeouts
    restart with every byte, so a server that drips its HEADERS one byte at a
    time used to hold a thread for as long as it liked."""

    def __init__(self, seconds):
        self.socks, self.fired = [], False
        self.timer = threading.Timer(max(0.1, seconds), self.fire)
        self.timer.daemon = True
        self.timer.start()

    def add(self, sock):
        if sock is not None:
            self.socks.append(sock)
            if self.fired:
                self._close(sock)

    @staticmethod
    def _close(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass

    def fire(self):
        self.fired = True
        for s in list(self.socks):
            self._close(s)

    def cancel(self):
        self.timer.cancel()


def _read_limited(resp, deadline, set_timeout=None):
    """Read a response body within the deadline and the size limits, and
    decompress it as it streams so a small gzip cannot expand without bound."""
    enc = (resp.getheader("Content-Encoding") or "").lower().strip()
    if enc not in ("", "identity", "gzip", "x-gzip", "deflate"):
        # br, zstd...: we asked for gzip/deflate only; binary we cannot read
        # must not be passed off (or remembered) as the feed
        raise _FetchLimit("unsupported content encoding %r" % enc[:20])
    # 32 + MAX_WBITS: accept a gzip or a zlib header, whichever the server sent.
    # Some servers label raw deflate, or even a plain body, as compressed: the
    # first chunk decides, as the pre-v80 decoder did.
    dec = zlib.decompressobj(32 + zlib.MAX_WBITS) if enc in ("gzip", "x-gzip", "deflate") else None
    first = dec is not None
    wire, out, size = 0, [], 0
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise _FetchLimit("took longer than %d s" % FETCH_DEADLINE)
        if set_timeout:
            set_timeout(max(0.5, min(left, TIMEOUT)))
        try:
            chunk = resp.read1(65536)
        except (OSError, ValueError, AttributeError):
            if time.monotonic() >= deadline - 0.2:
                raise _FetchLimit("took longer than %d s" % FETCH_DEADLINE)
            raise
        if not chunk:
            # the deadline guard closes the socket, which reads as end of
            # file: a body cut off that way is not a complete answer
            if time.monotonic() >= deadline - 0.2:
                raise _FetchLimit("took longer than %d s" % FETCH_DEADLINE)
            break
        wire += len(chunk)
        if wire > FETCH_MAX_WIRE:
            raise _FetchLimit("response too large")
        if dec is not None:
            try:
                chunk = dec.decompress(chunk, FETCH_MAX_BODY - size + 1)
            except zlib.error:
                if not first:
                    raise _FetchLimit("corrupt %s body" % enc)
                raw_dec = zlib.decompressobj(-zlib.MAX_WBITS)
                try:
                    chunk = raw_dec.decompress(chunk, FETCH_MAX_BODY - size + 1)
                    dec = raw_dec                               # raw deflate
                except zlib.error:
                    dec = None                                  # labelled compressed, sent plain
            first = False
            if dec is not None and dec.unconsumed_tail:
                raise _FetchLimit("response expands too much")
        size += len(chunk)
        if size > FETCH_MAX_BODY:
            raise _FetchLimit("response expands too much")
        out.append(chunk)
    if dec is not None:
        try:
            tail = dec.flush()
        except zlib.error:
            tail = b""
        if size + len(tail) > FETCH_MAX_BODY:
            raise _FetchLimit("response expands too much")
        out.append(tail)
    return b"".join(out)


def _body_text(raw, resp):
    text = _to_utf8_text(raw, resp.getheader("Content-Type") or "")
    if _BLOCK_PAGE.search(text[:20000]):
        raise _FetchLimit("the site answered with a bot-protection page, not the feed")
    return text


def _hop_allowed(url):
    """Hop check when a proxy does the connecting: refuse anything that names
    this machine or a private network. A name we cannot resolve locally is
    left to the proxy (which cannot reach this machine's loopback)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    try:
        _public_addresses(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except _NotPublic:
        return False
    except _Unresolvable:
        pass
    return True


def _fetch_via_proxy(target, max_hops, max_bytes):
    class Hops(urllib.request.HTTPRedirectHandler):
        max_redirections = max_hops

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not _hop_allowed(newurl):
                raise _NotPublic(newurl)
            return urllib.request.HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)

    if not _hop_allowed(target):
        return False, 400, "Only public web addresses can be fetched"
    deadline = time.monotonic() + FETCH_DEADLINE

    import http.client
    guard = _Deadline(deadline - time.monotonic())

    class GConn(http.client.HTTPConnection):
        def connect(self):
            http.client.HTTPConnection.connect(self)
            guard.add(self.sock)

    class GSConn(http.client.HTTPSConnection):
        def connect(self):
            http.client.HTTPSConnection.connect(self)
            guard.add(self.sock)

    class GH(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(GConn, req)

    def attempt(ctx):
        class GHS(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(GSConn, req, context=ctx)
        opener = urllib.request.build_opener(Hops, GH, GHS)
        with opener.open(Request(target, headers=_FETCH_HEADERS), timeout=TIMEOUT) as resp:
            def set_timeout(t):
                try:
                    resp.fp.raw._sock.settimeout(t)
                except Exception:
                    pass
            raw = _read_limited(resp, deadline, set_timeout)
            return True, resp.status, _body_text(raw, resp)
    try:
        try:
            return attempt(_SSL_CTX)
        except Exception as e:
            if guard.fired or not _is_cert_error(e):
                raise
            _note_unverified(urlparse(target).hostname or "?", e)
            return attempt(_SSL_LOOSE)
    except _NotPublic:
        return False, 400, "Only public web addresses can be fetched"
    except Exception as e:
        if guard.fired:
            return False, 502, "Upstream fetch failed: took longer than %d s" % FETCH_DEADLINE
        return False, 502, "Upstream fetch failed: %s" % e
    finally:
        guard.cancel()


def fetch_public(target, max_hops=5, max_bytes=6 * 1024 * 1024):
    """The /fetch proxy. Returns (ok, status, text) like fetch_upstream, plus:
    every hop (the URL and each redirect) is resolved once, checked to be a
    public address, and connected to at exactly that address. A redirect to
    this machine or the home network is refused. status 400 = refused target,
    502 = ordinary upstream failure (including "the name does not resolve")."""
    from urllib.parse import urljoin, urlsplit
    url = target
    deadline = time.monotonic() + FETCH_DEADLINE
    guard = None
    try:
        px = urllib.request.getproxies()
        if px.get("http") or px.get("https"):
            # This machine reaches the web through a proxy (for one scheme or
            # both). A direct pinned connection would bypass it, and a redirect
            # could switch scheme, so the whole fetch goes the proxy way, with
            # every hop's address still checked.
            return _fetch_via_proxy(target, max_hops, max_bytes)
        guard = _Deadline(FETCH_DEADLINE)
        for _hop in range(max_hops + 1):
            if time.monotonic() > deadline:
                return False, 502, "Upstream fetch failed: took longer than %d s" % FETCH_DEADLINE
            u = urlparse(url)
            if u.scheme not in ("http", "https") or not u.hostname:
                return False, 400, "Only public web addresses can be fetched"
            port = u.port or (443 if u.scheme == "https" else 80)
            try:
                ips = _public_addresses(u.hostname, port)
            except _NotPublic:
                return False, 400, "Only public web addresses can be fetched"
            except _Unresolvable as e:
                return False, 502, "Upstream fetch failed: could not resolve %s (%s)" % (u.hostname, str(e)[:60])
            # A PC with no IPv6 route, or a host with one dead address, must
            # fall through to the next address - as urlopen always did.
            conn, last_err = None, None
            for ip in ips[:6]:
                for ctx in (_SSL_CTX, _SSL_LOOSE):
                    if time.monotonic() > deadline:
                        break
                    c = _pinned_connection(u.scheme, u.hostname, port, ip,
                                           max(1.0, min(TIMEOUT, deadline - time.monotonic())), ctx)
                    try:
                        c.connect()
                        guard.add(c.sock)
                        conn = c
                        if ctx is _SSL_LOOSE:
                            _note_unverified(u.hostname, last_err)
                        break
                    except Exception as e:
                        last_err = e
                        c.close()
                        if not (u.scheme == "https" and _is_cert_error(e)):
                            break              # only a certificate failure earns the unverified retry
                if conn is not None:
                    break
            if conn is None:
                return False, 502, "Upstream fetch failed: %s" % last_err
            try:
                sp = urlsplit(url)                 # keeps ";params" in the path
                path = (sp.path or "/") + (("?" + sp.query) if sp.query else "")
                conn.request("GET", path, headers=_FETCH_HEADERS)
                resp = conn.getresponse()
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.getheader("Location")
                    if not loc:
                        return False, 502, "Upstream fetch failed: redirect without a location"
                    url = urljoin(url, loc)
                    continue
                if resp.status >= 400:
                    return False, 502, "Upstream fetch failed: HTTP Error %d: %s" % (resp.status, resp.reason)
                raw = _read_limited(resp, deadline, lambda t: conn.sock and conn.sock.settimeout(t))
                return True, resp.status, _body_text(raw, resp)
            finally:
                conn.close()
        return False, 502, "Upstream fetch failed: too many redirects"
    except Exception as e:
        if guard is not None and guard.fired:
            return False, 502, "Upstream fetch failed: took longer than %d s" % FETCH_DEADLINE
        return False, 502, "Upstream fetch failed: %s" % e
    finally:
        if guard is not None:
            guard.cancel()


# The deck's alertKind(), in Python, for merging a list of live messages.
# Both are checked against Oref's own message table (tests/fixtures).
OREF_SHELTER_RE = re.compile("היכנסו מי?יד|להיכנס מי?יד|היכנסו למרחב|היכנסו למבנה|הסתגרו|הסתגרות|אין לצאת|להמשיך לשהות במרחב המוגן|המשך שהייה במרחב המוגן|להישאר במרחב המוגן|הישארו במרחב המוגן|יש לשהות במרחב המוגן|יש לשהות במרחב מוגן|שהו במרחב המוגן|המשך שהייה במבנה|לשהות במבנה|הישארו בשטח פתוח|לשהות בשטח פתוח")
OREF_ENDED_RE = re.compile("הסתיים|ניתן לצאת|יכולים לצאת|החשש הוסר|הוסר החשש|נשלל|חלפה|ניתן לחזור|לחזור לשגרה|סיום|שלילת|הסרת|התרעת שווא|חזרו לפעול|חזר לפעול|אין הנחיות לציבור|חזרה למבנים|יציאה ממרחב מוגן")


def _oref_kind(x):
    title = " ".join(str(x.get("title") or "").split())
    desc = " ".join(str(x.get("desc") or "").split())
    try:
        cat = int(str(x.get("cat")).strip())
    except (TypeError, ValueError):
        cat = None
    if cat is not None and cat != 10:
        return "attack"
    if OREF_SHELTER_RE.search(title):
        return "shelter"
    if OREF_ENDED_RE.search(title):
        return "ended"
    if OREF_SHELTER_RE.search(desc):
        return "shelter"
    return "early" if cat == 10 else "attack"


def _alert_payload(text):
    """The live alert feed has exactly two legitimate shapes: an empty list
    (quiet) or an object carrying a list of localities. Returns the text to
    hand to the deck, or None when the body is anything else - an HTML "page
    moved", null, an error object. None is NOT quiet, it is a failure; this is
    how Oref's history URL died silently in 2026.
    A non-empty list of alert objects is merged into the one-object shape the
    deck reads, so it can never be mistaken for the empty "quiet" list."""
    try:
        d = json.loads(text)
    except Exception:
        return None
    if isinstance(d, list):
        if not d:
            return "[]"
        if not all(isinstance(x, dict) and isinstance(x.get("data"), list) for x in d):
            return None
        # Several messages at once. The ones that sound the siren (attack, and
        # cat-10 "go in / stay in") are merged; an all-clear or heads-up for
        # one town must never relabel another town's siren. The classifier is
        # the deck's own (alertKind), checked against Oref's message table.
        kinds = [_oref_kind(x) for x in d]
        siren = [x for x, k in zip(d, kinds) if k in ("attack", "shelter")]
        chosen = siren or [x for x, k in zip(d, kinds) if k == "early"] or d
        cities = []
        for x in chosen:
            cities += [c for c in x["data"] if isinstance(c, str) and c not in cities]
        if not cities:
            return None
        # the base message sets title and category: "stay until further
        # notice" is the stricter instruction, so it wins over a timed stay
        base = next((x for x, k in zip(d, kinds) if k == "shelter"), None) if siren else None
        merged = dict(base or chosen[0])
        merged["data"] = cities
        # the deck decides "new alert" by id: a message added to the list must
        # change it, or a siren for a newly included town would never sound
        merged["id"] = "+".join(sorted(str(x.get("id") or "") for x in chosen))
        if any(k != "ended" for x, k in zip(d, kinds) if x in chosen):
            # an all-clear that came in the same list still ends the stay of
            # the towns it names (they are not in the merged list)
            ended = []
            for x, k in zip(d, kinds):
                if k == "ended":
                    ended += [c for c in x["data"] if isinstance(c, str) and c not in cities and c not in ended]
            if ended:
                merged["ended"] = ended
        return json.dumps(merged, ensure_ascii=False)
    if isinstance(d, dict):
        data, cities = d.get("data"), d.get("cities")
        if not (isinstance(data, list) or isinstance(cities, list)):
            return None
        src = data if isinstance(data, list) and data else (cities or [])
        names = [c for c in src if isinstance(c, str)]
        if src and not names:
            return None                  # localities in a form we cannot read: a failure, never "quiet"
        if not names:
            return "[]"                  # an alert object naming no locality alerts nobody
        out = dict(d)
        out["data"] = names
        return json.dumps(out, ensure_ascii=False)
    return None


def _valid_alert_payload(text):
    return _alert_payload(text) is not None


def _fresh_or_error(cache, max_age, message):
    """A fetcher's own cache may cover a failure only briefly. Older than
    max_age it must report the failure, so the last-good-answer layer can
    serve it MARKED as old instead of passing it off as current."""
    if cache.get("data") and time.time() - cache.get("t", 0) <= max_age:
        return cache["data"]
    return '{"error":"%s"}' % message


# ============================================================
#   LAST GOOD ANSWER
#   ----------------------------------------------------------
#   When a source fails, the deck keeps showing its last good answer (up to
#   STALE_MAX_SEC old) instead of an empty panel. The response carries
#   X-DL-Stale: <age in seconds>, and /health still records the failure, so
#   nothing pretends to be fresh.
#   Never applied to the live alert feed: an old "no alert" is worse than an
#   honest "no connection".
# ============================================================
STALE_MAX_SEC = 6 * 3600
STALE_NEVER = {"oref_alerts", "traffic"}        # traffic: the brain has its own cache
_last_good = {}
_last_good_lock = threading.Lock()


def _remember_good(cache_id, status, body, ctype):
    if len(body) > 700000:               # 60 entries x 700 KB bounds this at ~40 MB
        return
    with _last_good_lock:
        if len(_last_good) >= 60 and cache_id not in _last_good:
            oldest = min(_last_good, key=lambda k: _last_good[k][0])
            _last_good.pop(oldest, None)
        _last_good[cache_id] = (time.time(), body, ctype)


def _recall_good(cache_id):
    with _last_good_lock:
        hit = _last_good.get(cache_id)
    if hit and time.time() - hit[0] <= STALE_MAX_SEC:
        return hit
    return None


def _archive_loop():
    """Sample the traffic picture every 15 minutes; prune once a day."""
    last_prune = 0
    time.sleep(45)
    while True:
        try:
            snap = brain_get("/traffic")
            if snap:
                dlarchive.add_traffic(snap)
            if time.time() - last_prune > 86400:
                dlarchive.prune()
                last_prune = time.time()
        except Exception:
            pass
        time.sleep(900)


# ============================================================
#   SOURCE HEALTH  +  DAILY SELF-TEST
#   ----------------------------------------------------------
#   Until v74 a source that broke simply vanished from the deck. Now every
#   upstream request is recorded (last answer, latency, last error) and a
#   self-test checks once a day that each source still returns the SHAPE the
#   deck parses - which is exactly what changed when Oref replaced its site.
# ============================================================
HEALTH_SOURCES = {
    "/oref/alerts":  ("oref_alerts",  "פיקוד העורף · התרעה פעילה", True),
    "/oref/history": ("oref_history", "פיקוד העורף · היסטוריה", True),
    "/idf":          ("idf",          "דובר צה״ל", False),
    "/quakes":       ("quakes",       "רעידות אדמה", False),
    "/markets":      ("markets",      "מדדים ומטבע", False),
    "/flights":      ("flights",      "נתב״ג", False),
    "/roads":        ("roads",        "דיווחי כבישים", False),
    "/air":          ("air",          "איכות אוויר", False),
    "/traffic":      ("traffic",      "שירות התנועה", False),
}
_health = {}
_health_lock = threading.Lock()
_selftest = {"at": 0.0, "running": False, "results": []}
BOOT_AT = time.time()


def _health_key(path, query):
    """(id, label, critical) for a request path, or None if it is not a source."""
    if path in HEALTH_SOURCES:
        return HEALTH_SOURCES[path]
    try:
        if path == "/telegram":
            ch = (parse_qs(query or "").get("ch") or [""])[0][:40]
            return ("tg:" + ch, "טלגרם · " + ch, False) if ch in TG_CHANNELS else None
        if path == "/fetch":
            host = (urlparse((parse_qs(query or "").get("url") or [""])[0]).hostname or "").lower()[:60]
            if host.startswith("www."):
                host = host[4:]
            # only hosts the deck itself uses (or a feed the owner added):
            # anyone can send /fetch?url=..., and must not be able to fill
            # this table or push real sources out of it
            return ("feed:" + host, "הזנה · " + host, False) if host and host in _known_hosts() else None
    except Exception:
        return None
    return None


_known = {"key": None, "hosts": frozenset()}


def _known_hosts():
    """Hosts the page itself references, plus feeds added in the settings."""
    try:
        idx = os.path.join(ROOT, "index.html")
        key = (os.path.getmtime(idx), dlconfig.mtime())
        if _known["key"] != key:
            hosts = set()
            with open(idx, encoding="utf-8", errors="replace") as f:
                for h in re.findall(r"https?://([A-Za-z0-9.\-]+)", f.read()):
                    hosts.add(h.lower()[4:] if h.lower().startswith("www.") else h.lower())
            for feed in dlconfig.load()["news"]["custom"]:
                h = (urlparse(feed["url"]).hostname or "").lower()
                hosts.add(h[4:] if h.startswith("www.") else h)
            _known.update(key=key, hosts=frozenset(hosts))
    except Exception:
        pass
    return _known["hosts"]


def health_note(sid, label, ok, ms, err="", critical=False, stale=None):
    now = time.time()
    with _health_lock:
        if sid not in _health and len(_health) >= 80:
            # full: the source unused for longest makes room - never a critical one
            old = [h for h in _health.values() if not h["critical"]]
            if not old:
                return
            _health.pop(min(old, key=lambda h: h["lastTry"])["id"], None)
        h = _health.setdefault(sid, {"id": sid, "label": label, "critical": critical,
                                     "ok": None, "lastOk": 0, "lastFail": 0, "lastTry": 0,
                                     "ms": 0, "error": "", "okCount": 0, "failCount": 0,
                                     "streak": 0})
        h["label"], h["lastTry"], h["ms"] = label, now, int(ms)
        h["servedStale"] = stale          # seconds old, when a failure was covered by the last good answer
        if ok:
            h.update(ok=True, lastOk=now, streak=0)
            h["okCount"] += 1
        else:
            h.update(ok=False, lastFail=now, error=str(err)[:200])
            h["failCount"] += 1
            h["streak"] += 1


def _error_of(status, body):
    """Why a response counts as a failure, or '' when it is fine."""
    head = body[:240] if isinstance(body, str) else bytes(body[:240]).decode("utf-8", "replace")
    if head.lstrip().startswith('{"error"'):
        try:
            return str(json.loads(head + ("" if head.rstrip().endswith("}") else '"}')).get("error"))[:160]
        except Exception:
            return head[:160]
    if status >= 400:
        return ("HTTP %d: " % status) + head[:120]
    return ""


def _fmt_age(ts, now):
    if not ts:
        return None
    return int(now - ts)


def health_snapshot():
    now = time.time()
    with _health_lock:
        rows = [dict(h) for h in _health.values() if h["critical"] or now - h["lastTry"] < 24 * 3600]
    for h in rows:
        # down = the last attempt failed; stale = nobody has asked for 10 min
        h["state"] = ("unknown" if h["ok"] is None else
                      "down" if not h["ok"] else
                      "stale" if now - h["lastTry"] > 600 else "ok")
        h["okAgo"], h["failAgo"], h["tryAgo"] = (_fmt_age(h["lastOk"], now),
                                                 _fmt_age(h["lastFail"], now),
                                                 _fmt_age(h["lastTry"], now))
    rows.sort(key=lambda h: (0 if h["state"] == "down" else 1, not h["critical"], h["label"]))
    brain = brain_get("/traffic") or {}
    st = dict(_selftest)
    # A probe that failed at 03:00 is not news at 15:00 if the live source has
    # answered since. Without this one blip kept the light red for a day.
    live_ok = {h["id"]: h["lastOk"] for h in rows}
    still_failed = [r for r in st["results"]
                    if r["ok"] is False and not live_ok.get(r["id"], 0) > st["at"]]
    return {
        "build": _build_on_disk(),
        "uptimeSec": int(now - BOOT_AT),
        "threads": threading.active_count(),
        "sources": rows,
        "down": [h["label"] for h in rows if h["state"] == "down"],
        "tlsUnverified": sorted(h for h, (t, _r) in list(_tls_unverified.items()) if now - t < 7 * 86400),
        "brain": {"up": bool(brain), "health": brain.get("health"),
                  "budget": brain.get("budget"), "checkedAt": brain.get("checkedAt")},
        "selftest": {"at": st["at"], "ago": _fmt_age(st["at"], now), "running": st["running"],
                     "results": st["results"],
                     "failed": [r["label"] for r in still_failed],
                     "criticalFailed": [r["label"] for r in still_failed if r.get("critical")]},
    }


def _build_on_disk():
    try:
        with open(os.path.join(ROOT, "index.html"), encoding="utf-8", errors="replace") as f:
            m = re.search(r"const DL_BUILD = '([^']+)'", f.read())
        return m.group(1) if m else ""
    except Exception:
        return ""


def _j(text):
    return json.loads(text)


def _why(text):
    """The reason inside an {"error": ...} body, or the body's first line."""
    try:
        return str(json.loads(text).get("error"))[:120]
    except Exception:
        return str(text)[:120]


def _probe_oref_alerts():
    ok, st, text = fetch_oref(OREF_ALERTS)
    if not ok:
        return False, _why(text)
    if not _valid_alert_payload(text):
        return False, "מבנה לא צפוי: " + text[:80]
    return True, "אין התרעה פעילה" if _j(text) == [] else "התרעה פעילה"


def _probe_oref_history():
    ok, st, text = fetch_oref(OREF_HISTORY_API % "lastWeek")
    if not ok:
        return False, _why(text)
    rows = _normalize_oref_api(text)
    if rows is None:
        return False, "ה-API של ההיסטוריה שינה מבנה: " + text[:80]
    return True, "%d אירועים בשבוע האחרון" % len(rows)


def _probe_oref_policy():
    ok, st, text = fetch_oref(OREF_POLICY_PROBE)
    try:
        if ok and isinstance(_j(text), (dict, list)):
            return None, "הכתובת של פיקוד העורף חזרה לענות - אפשר להחזיר את פס המדיניות"
    except Exception:
        pass
    return None, "כבוי: הכתובת האזורית של פיקוד העורף עדיין מחזירה שגיאה"


def _probe_json(fn, check, describe):
    def run():
        d = _j(fn())
        if isinstance(d, dict) and d.get("error"):
            return False, str(d["error"])[:120]
        if not check(d):
            return False, "מבנה לא צפוי"
        return True, describe(d)
    return run


def _probe_traffic():
    b = brain_get("/traffic")
    if not b:
        if not os.path.isfile(os.path.join(ROOT, "google.key")):
            return None, "התנועה לא מוגדרת (אין google.key)"
        return False, "שירות התנועה (brain.py) לא רץ"
    h = b.get("health") or {}
    if h.get("failing"):
        e = h.get("error") or {}
        return False, "Google דוחה: %s (%s)" % (e.get("hintHe") or e.get("hint", ""), e.get("code", ""))
    if (b.get("budget") or {}).get("state") == "no_key":
        return None, "אין google.key - התנועה כבויה"
    return True, "%d מקטעים, מדידה אחרונה %s" % (len(b.get("segments") or []), b.get("checkedAt") or "-")


SELFTEST_PROBES = [
    ("oref_alerts", "פיקוד העורף · התרעה פעילה", _probe_oref_alerts),
    ("oref_history", "פיקוד העורף · היסטוריה", _probe_oref_history),
    ("oref_policy", "פיקוד העורף · מדיניות התגוננות", _probe_oref_policy),
    ("idf", "דובר צה״ל", _probe_json(lambda: fetch_idf_spokesperson(),
        lambda d: isinstance(d, list) and len(d) > 0 and isinstance(d[0], dict),
        lambda d: "%d הודעות" % len(d))),
    ("quakes", "רעידות אדמה", _probe_json(lambda: fetch_quakes(),
        lambda d: isinstance(d, (list, dict)), lambda d: "תקין")),
    ("markets", "מדדים ומטבע", _probe_json(lambda: fetch_markets(),
        lambda d: isinstance(d, list) and len(d) > 0, lambda d: "%d מדדים" % len(d))),
    ("flights", "נתב״ג", _probe_json(lambda: fetch_flights(),
        lambda d: isinstance(d, dict) and "dep" in d and "arr" in d, lambda d: "תקין")),
    ("roads", "דיווחי כבישים", _probe_json(lambda: fetch_roads(),
        lambda d: isinstance(d, dict) and isinstance(d.get("items"), list),
        lambda d: "%d דיווחים" % len(d["items"]))),
    ("air", "איכות אוויר", _probe_json(lambda: fetch_air(32.017, 34.745),
        lambda d: isinstance(d, dict) and "aqi" in d, lambda d: "AQI %s" % d.get("aqi"))),
    ("traffic", "שירות התנועה", _probe_traffic),
]


def run_selftest():
    """Check every source against the shape the deck expects. ok is True,
    False, or None for 'informational' (a source that is off on purpose)."""
    if _selftest["running"]:
        return
    _selftest["running"] = True
    results = []
    try:
        for sid, label, probe in SELFTEST_PROBES:
            t0 = time.time()
            try:
                ok, detail = probe()
            except Exception as e:
                ok, detail = False, "%s: %s" % (type(e).__name__, str(e)[:120])
            results.append({"id": sid, "label": label, "ok": ok, "detail": detail,
                            "critical": sid in ("oref_alerts", "oref_history"),
                            "ms": int((time.time() - t0) * 1000)})
        _selftest.update(at=time.time(), results=results)
        bad = [r for r in results if r["ok"] is False]
        if bad:        # ASCII only: the Windows console mangles Hebrew
            print("  [self-test] %d of %d sources FAILED: %s  (details: /health)"
                  % (len(bad), len(results), ", ".join(r["id"] for r in bad)))
    finally:
        _selftest["running"] = False


def _selftest_loop():
    time.sleep(90)                      # let the first real requests go first
    streak, last_failed = 0, None
    while True:
        try:
            run_selftest()
        except Exception:
            pass
        # a failure is re-checked in ten minutes, not tomorrow - but something
        # that keeps failing is checked hourly, not 144 times a day
        failed = sorted(r["id"] for r in _selftest["results"] if r["ok"] is False)
        streak = streak + 1 if failed and failed == last_failed else 0
        last_failed = failed
        time.sleep(24 * 3600 if not failed else 600 if streak < 3 else 3600)


def _same_origin(handler):
    """A browser request from a page of ours: no Origin header (same-origin
    GET/POST from old browsers, curl) or an Origin whose host is the Host we
    were addressed as."""
    try:
        origin = handler.headers.get("Origin")
        if not origin:
            return True
        o = urlparse(origin)
        mine = urlparse("http://" + (handler.headers.get("Host") or ""))
        return ((o.hostname or "").lower() == (mine.hostname or "").lower()
                and (o.port or 80) == (mine.port or 80))
    except Exception:
        return False


def _is_local_request(handler):
    """Settings may only be changed from this machine. The peer address stops
    other devices on the LAN; the Host and Origin checks stop a web page on
    this machine (or a DNS-rebinding page) from posting on the user's behalf."""
    try:
        if handler.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return False
        host = (handler.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("localhost", "127.0.0.1", "[::1]"):
            return False
        origin = handler.headers.get("Origin")
        if origin:
            oh = urlparse(origin).hostname or ""
            if oh.lower() not in ("localhost", "127.0.0.1", "::1"):
                return False
        return True
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    # Per-connection only: an idle keep-alive releases its thread instead of
    # holding it. This is the safe half of what a global default would do.
    timeout = 65

    def setup(self):
        BaseHTTPRequestHandler.setup(self)
        try:
            self.connection.settimeout(self.timeout)
        except Exception:
            pass

    # http.server prints a full traceback for any unhandled error in a
    # handler, including plain client disconnects. That is what filled the
    # console with hundreds of WinError 10053 dumps and hid anything real.
    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                TimeoutError, socket.timeout) as e:
            self.close_connection = True

    def log_error(self, fmt, *args):
        msg = fmt % args if args else fmt
        if any(s in str(msg) for s in ("10053", "10054", "Broken pipe",
                                       "aborted", "reset")):
            return                      # client went away; nothing to report
        BaseHTTPRequestHandler.log_error(self, fmt, *args)


    # Quieter console — don't log every request line.
    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type, extra=None):
        self._resp = (status, body[:240])      # what the health wrapper inspects
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        hk, cid = getattr(self, "_hkey", None), getattr(self, "_cache_id", None)
        if hk and cid and hk[0] not in STALE_NEVER:
            if _error_of(status, body):
                hit = _recall_good(cid)
                if hit:
                    age = int(time.time() - hit[0])
                    self._stale = age
                    status, body, content_type = 200, hit[1], hit[2]
                    extra = dict(extra or {})
                    extra["X-DL-Stale"] = str(age)
                    extra["Access-Control-Expose-Headers"] = "X-DL-Stale"
            else:
                _remember_good(cid, status, body, content_type)
        # A client that gave up mid-response is routine here: the deck aborts
        # its own requests after 12s, tabs get closed, laptops sleep. Windows
        # reports that as ConnectionAbortedError (WinError 10053), which was
        # NOT in the caught list — so every abort killed its handler thread
        # with a traceback. The headers can fail for the same reason, so the
        # whole exchange is guarded, not just the body.
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                TimeoutError, socket.timeout, OSError):
            self.close_connection = True

    def do_POST(self):
        _touch_activity()
        parsed = urlparse(self.path)
        if parsed.path == "/state":
            # The view-state relay is used by other screens on the LAN, so it is
            # not localhost-only - but a foreign web page has no business here,
            # and the body is a few hundred bytes.
            if not _same_origin(self):
                self._send(403, '{"ok":false}', "application/json; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length < 0 or length > 65536:
                    self._send(413, '{"ok":false}', "application/json; charset=utf-8")
                    return
                body = self.rfile.read(length).decode("utf-8", "replace") if length else "{}"
                json.loads(body)
                SHARED_STATE["json"] = body
            except Exception:
                pass
            self._send(200, '{"ok":true}', "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/archive/tension":
            ok = False
            if _is_local_request(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    d = json.loads(self.rfile.read(length).decode("utf-8", "replace")) if 0 < length < 2000 else {}
                    ok = dlarchive.add_tension(d.get("score"), d.get("alerts"), d.get("idf"), d.get("news"))
                except Exception:
                    ok = False
            self._send(200 if ok else 400, json.dumps({"ok": bool(ok)}), "application/json; charset=utf-8")
            return
        if parsed.path == "/config":
            if not _is_local_request(self):
                self._send(403, '{"ok":false,"error":"settings can only be changed on this computer"}',
                           "application/json; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > dlconfig.MAX_BYTES:
                    raise ValueError("bad length")
                req = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
                if not isinstance(req, dict):
                    raise ValueError("body must be an object")
                if isinstance(req.get("local"), dict):          # replace the whole file
                    ok, msg = dlconfig.save_local(req["local"])
                elif isinstance(req.get("patch"), dict):        # merge into it
                    ok, msg = dlconfig.patch_local(req["patch"])
                else:
                    ok, msg = False, "expected {local:{...}} or {patch:{...}}"
            except Exception as e:
                ok, msg = False, "bad request: %s" % str(e)[:80]
            self._send(200 if ok else 400,
                       json.dumps({"ok": ok, "message": msg,
                                   "config": dlconfig.load() if ok else None}, ensure_ascii=False),
                       "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            return
        self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_GET(self):
        """Route the request, then record how the source behaved."""
        parsed = urlparse(self.path)
        key = _health_key(parsed.path, parsed.query)
        if not key:
            return self._route_get()
        self._resp, self._stale = None, None
        self._hkey, self._cache_id = key, self.path
        t0 = time.time()
        try:
            self._route_get()
        finally:
            status, body = self._resp or (599, "no response")
            err = _error_of(status, body)
            if self._hkey is not None:       # the route may decide this was not a source request
                health_note(key[0], key[1], not err, (time.time() - t0) * 1000, err, key[2],
                            stale=self._stale)
            self._hkey = self._cache_id = None

    def _route_get(self):
        _touch_activity()
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self._send(200, json.dumps(health_snapshot(), ensure_ascii=False, indent=2),
                       "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/selftest":
            if ("run" in (parsed.query or "") and not _selftest["running"]
                    and time.time() - _selftest["at"] > 45 and _same_origin(self)):
                threading.Thread(target=run_selftest, daemon=True).start()
            self._send(200, json.dumps({"at": _selftest["at"], "running": True if "run" in (parsed.query or "") else _selftest["running"],
                                        "results": _selftest["results"]}, ensure_ascii=False),
                       "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/archive/summary" or parsed.path == "/archive/series":
            q = parse_qs(parsed.query or "")
            try:
                if parsed.path.endswith("summary"):
                    out = dlarchive.summary(int((q.get("hours") or ["24"])[0]))
                else:
                    out = dlarchive.series(int((q.get("days") or ["7"])[0]))
                    out["stats"] = dlarchive.stats()
            except Exception as e:
                out = {"error": "bad request: %s" % str(e)[:60]}
            self._send(200, json.dumps(out, ensure_ascii=False), "application/json; charset=utf-8",
                       {"Cache-Control": "no-store"})
            return
        if parsed.path == "/config":
            self._send(200, json.dumps({"config": dlconfig.load(), "local": dlconfig.load_local(),
                                        "defaults": dlconfig.DEFAULTS,
                                        "writable": _is_local_request(self),
                                        "freeTier": dlconfig.FREE_TIER,
                                        "ceiling": dlconfig.HARD_MONTHLY_CEILING},
                                       ensure_ascii=False),
                       "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            return

        # ---- view-state sync (controller pushes, receiver polls) ----
        if parsed.path == "/state":
            self._send(200, SHARED_STATE.get("json", "{}"),
                       "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return

        # ---- host LAN IP (so the Cast dialog can show a typable address) ----
        if parsed.path == "/lanip":
            import json as _json
            self._send(200, _json.dumps({"ip": _lan_ip(), "port": PORT}),
                       "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return

        # ---- Pikud HaOref red-alert endpoints ----
        if parsed.path == "/oref/history-debug":
            text = debug_oref_history()
            self._send(200, text, "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/traffic-debug":
            import json as _json
            b = brain_get("/model") or {}
            if b:
                self._send(200, _json.dumps(
                    {"brain": "up", "model": b, "traffic": brain_get("/traffic")},
                    ensure_ascii=False, indent=2),
                    "application/json; charset=utf-8",
                    {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
                return
            _mirror_start()
            age = (round((time.time() - _MIRROR["t"]) / 60.0, 1)
                   if _MIRROR.get("t") else None)
            report = {
                "mirror_state": _MIRROR.get("state"),
                "mirror_note": _MIRROR.get("note"),
                "browser_exe": _browser_exe() or "NOT FOUND",
                "window_alive": bool(_MIRROR.get("proc")
                                     and _MIRROR["proc"].poll() is None),
                "feed_age_min": age,
                "feed_sample": (_MIRROR.get("json") or "")[:200],
                "parsed": _traffic_from_mirror(),
                "window_says": _MIRROR.get("page_text") or "(עדיין לא נקרא)",
                "geolocation_permission": _MIRROR.get("geo") or "(לא נבדק)",
                "tile_zoom": _mirror_tile_zoom(),
                "active_url": MIRROR_URLS[_MIRROR["url_idx"] % len(MIRROR_URLS)],
                "working_url": _MIRROR.get("working_url"),
                "page": _mirror_page_info(),
                "seen_requests": list(_MIRROR.get("seen") or []),
            }
            self._send(200, _json.dumps(report, ensure_ascii=False, indent=2),
                       "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/waze-probe":
            import json as _json
            html = WAZE_PROBE_HTML % _json.dumps(WAZE_URLS)
            self._send(200, html, "text/html; charset=utf-8",
                       {"Cache-Control": "no-store"})
            return
        if parsed.path == "/version":
            # Settles "did the file actually update?" without guesswork: this
            # reads index.html FROM DISK right now and reports what is there,
            # so a stale browser cannot fake it.
            import json as _json
            import time as _t
            local = _is_local_request(self)
            info = {"serverFile": os.path.abspath(__file__) if local else "(shown on this computer only)"}
            try:
                p = os.path.join(ROOT, "index.html")
                raw = open(p, encoding="utf-8", errors="replace").read()
                m = re.search(r"DL_BUILD = '([^']+)'", raw)
                info.update({
                    "indexPath": p if local else "(shown on this computer only)",
                    "buildOnDisk": m.group(1) if m else "NOT FOUND",
                    "sizeBytes": os.path.getsize(p),
                    "modified": _t.strftime("%d/%m/%Y %H:%M:%S",
                                            _t.localtime(os.path.getmtime(p))),
                    "hasBrain": os.path.isfile(os.path.join(ROOT, "brain.py")),
                    "hasKey": os.path.isfile(os.path.join(ROOT, "google.key")),
                })
            except Exception as e:
                info["error"] = str(e)[:120]
            self._send(200, _json.dumps(info, ensure_ascii=False, indent=2),
                       "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/logo":
            q = parse_qs(parsed.query)
            sid = (q.get("s") or [""])[0]
            data = fetch_logo(sid)
            if not data:
                self._send(404, b"", "image/png",
                           {"Access-Control-Allow-Origin": "*"})
                return
            self._send(200, data, "image/png",
                       {"Access-Control-Allow-Origin": "*",
                        "Cache-Control": "public, max-age=604800"})
            return
        if parsed.path == "/air":
            q = parse_qs(parsed.query)
            try:
                lat = float((q.get("lat") or ["32.017"])[0])
                lng = float((q.get("lng") or ["34.745"])[0])
            except Exception:
                lat, lng = 32.017, 34.745
            text = fetch_air(lat, lng)
            self._send(200, text, "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/roads":
            text = fetch_roads()
            self._send(200, text, "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/traffic":
            text = fetch_traffic()
            self._send(200, text, "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/markets":
            text = fetch_markets()
            self._send(200, text, "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/flights":
            text = fetch_flights()
            self._send(200, text, "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/quakes":
            text = fetch_quakes()
            self._send(200, text, "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/telegram":
            qs = parse_qs(parsed.query or "")
            ch = (qs.get("ch") or [""])[0]
            text = fetch_telegram(ch)
            self._send(200, text, "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/oref/policy":
            ok, status, text = fetch_oref_policy()
            self._send(200 if ok else 502, text,
                       "application/json; charset=utf-8",
                       {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
            return
        if parsed.path == "/oref/alerts" or parsed.path == "/oref/history":
            if parsed.path == "/oref/alerts":
                ok, status, text = fetch_oref(OREF_ALERTS)
                if ok:
                    shaped = _alert_payload(text)
                    ok, text = ((True, shaped) if shaped is not None else
                                (False, '{"error":"oref alerts: unexpected response shape"}'))
            else:
                ok, status, text = fetch_oref_history()
            self._send(
                200 if ok else 502,
                text,
                "application/json; charset=utf-8",
                {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
            )
            return

        # ---- IDF Spokesperson (official Telegram) ----
        if parsed.path == "/idf":
            text = fetch_idf_spokesperson()
            ok = not text.startswith('{"error"')
            self._send(
                200 if ok else 502,
                text,
                "application/json; charset=utf-8",
                {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
            )
            return

        # ---- feed proxy endpoint ----
        if parsed.path == "/fetch":
            # parse_qs already percent-decodes the value ONCE, which fully
            # restores the original URL. A second unquote() here double-decodes
            # and corrupts any URL whose query text contains percent sequences
            # (Hebrew, quotes, "when:2d") — turning %D7..%22 into raw bytes and
            # breaking the request. So we rely on parse_qs alone.
            qs = parse_qs(parsed.query)
            target = qs.get("url", [None])[0]
            if not target or not re.match(r"^https?://", target, re.I):
                self._send(400, "Bad or missing url param", "text/plain; charset=utf-8")
                return
            ok, status, text = fetch_public(target)
            if status == 400:
                self._hkey = None            # refused, not a source: keep it out of the health table
                self._send(400, text, "text/plain; charset=utf-8")
                return
            self._send(
                200 if ok else 502,
                text,
                "text/plain; charset=utf-8",
                {"Cache-Control": "no-store"},       # same-origin only: no CORS grant on the proxy
            )
            return

        # ---- static files ----
        # Allow-list, not block-list. The v74 block-list missed Windows path
        # semantics: "/google.key." (trailing dot), "::$DATA" streams, 8.3
        # short names and drive-relative paths all resolve to the real file.
        file_path = static_path(parsed.path)
        if not file_path:
            self._send(404, "Not found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(file_path)[1].lower()
        try:
            with open(file_path, "rb") as f:
                data = f.read()
            # no-store on everything: a LAN deck must ALWAYS serve the file
            # that is on disk — a cached stale index.html cost us a debugging
            # round when updates silently didn't appear
            self._send(200, data, MIME.get(ext, "application/octet-stream"),
                       {"Cache-Control": "no-store, must-revalidate"})
        except Exception:
            self._send(500, "Read error", "text/plain; charset=utf-8")


def _lan_ip():
    """Best-effort: find this machine's LAN IP (so phones can reach the deck)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no traffic actually sent
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    dlconfig.setup_logging("server")       # logs/server.log when there is no console
    line = "=" * 48
    # NETWORK mode: set NETWORK=1 to let other devices on your Wi-Fi (phone,
    # tablet) reach the deck. Default is localhost-only (this machine).
    network = os.environ.get("NETWORK", "") == "1"
    host = "0.0.0.0" if network else "127.0.0.1"
    print()
    print(line)
    print("  Dispatch LIVE  -  TV + News + Red Alerts (Python)")
    print(line)
    print("  Running at:  http://localhost:%d" % PORT)
    if network:
        ip = _lan_ip()
        print("  On your phone (same Wi-Fi):  http://%s:%d" % (ip, PORT))
    print("  Stop:        press Ctrl+C in this window")
    print(line)
    print()
    try:
        # Without these the server accumulates one stuck thread per hung
        # upstream request until it can no longer accept connections — which
        # is why a refresh did nothing, closing the window helped for a while
        # and only a reboot cleared it.
        ThreadingHTTPServer.daemon_threads = True
        ThreadingHTTPServer.request_queue_size = 128
        httpd = ThreadingHTTPServer((host, PORT), Handler)
    except OSError as e:
        print("  [!] Could not start on port %d." % PORT)
        print("      Another program may be using it.")
        print("      Tip: set a different port, e.g.  set PORT=3200  then re-run.")
        print("      (%s)" % e)
        input("\n  Press Enter to close...")
        return
    # Open the deck in the browser once we're listening (unless disabled).
    if os.environ.get("NOOPEN", "") != "1":
        threading.Thread(target=open_browser_when_ready, daemon=True).start()
        print("  Opening the deck in your browser...")
        print()
    threading.Thread(target=_activity_watchdog, daemon=True).start()
    threading.Thread(target=_selftest_loop, daemon=True).start()
    threading.Thread(target=_archive_loop, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
