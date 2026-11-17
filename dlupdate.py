"""One-click update from the GitHub releases of dispatch-live.

check()    - is there a newer release than the build that is running?
apply()    - download it, check it, keep a backup of the running files,
             put the new files in place.
rollback() - put the newest backup back.
restart()  - start the updated program in place of this one.

What is never touched: settings (config.local.json), keys (google.key),
the data and log folders, and Hayom Yom (hayomyom.json): only the program
files listed in PROGRAM_FILES are written.

Launcher files (.bat, .sh, .command) are NOT replaced by an update. Windows
reads a running batch file line by line from disk, so replacing the one
that started this server would make it run half of each version. When a
release changes them, apply() says so and they are installed from the zip.
"""
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from urllib.parse import urlparse

REPO = os.environ.get("DL_UPDATE_REPO", "dudidd-wq/dispatch-live")
API_LATEST = "https://api.github.com/repos/%s/releases/latest"
PROGRAM_FILES = ("index.html", "server.py", "brain.py", "dlconfig.py", "dlarchive.py", "dlupdate.py",
                 "README.md", "CHANGELOG.md")
LAUNCHER_RE = re.compile(r"^[A-Za-z0-9_-]+\.(bat|sh|command)$")
DOC_RE = re.compile(r"^docs/[A-Za-z0-9_-]+\.md$")
ALLOWED_HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com", "codeload.github.com")
MAX_ZIP = 15 * 1024 * 1024
KEEP_BACKUPS = 3
_SSL = ssl.create_default_context()          # always verified: this downloads code
_lock = threading.Lock()
_cache = {"t": 0, "data": None}


class UpdateError(Exception):
    """Carries a Hebrew message for the settings screen."""


def build_number(tag):
    """'v17.11-84' or '17.11-84' -> 84. Builds count up; the date part does not order them."""
    m = re.search(r"-(\d+)\s*$", str(tag or ""))
    return int(m.group(1)) if m else -1


def current_build(root):
    try:
        with open(os.path.join(root, "index.html"), encoding="utf-8") as f:
            m = re.search(r"const DL_BUILD = '([^']+)'", f.read())
        return m.group(1) if m else ""
    except OSError:
        return ""


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _host_ok(newurl):
            raise UpdateError("ההורדה הופנתה לכתובת לא מוכרת: %s" % (urlparse(newurl).hostname or "?"))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _host_ok(url):
    u = urlparse(url)
    return u.scheme == "https" and (u.hostname or "").lower() in ALLOWED_HOSTS


def _get(url, limit, accept="application/json"):
    if not _host_ok(url):
        raise UpdateError("כתובת לא מוכרת: %s" % url)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_SSL), _CheckedRedirects())
    req = urllib.request.Request(url, headers={"User-Agent": "dispatch-live-updater", "Accept": accept})
    try:
        with opener.open(req, timeout=30) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("לא נמצאה גרסה ב-GitHub. אם הריפו עדיין פרטי, המחשב לא יכול לקרוא אותו.")
        if e.code in (403, 429):
            raise UpdateError("GitHub הגביל את מספר הבדיקות. נסו שוב בעוד שעה.")
        raise UpdateError("GitHub ענה %d" % e.code)
    except UpdateError:
        raise
    except Exception as e:
        raise UpdateError("אין חיבור ל-GitHub (%s)" % e)
    if len(data) > limit:
        raise UpdateError("הקובץ גדול מהמותר")
    return data


def check(root, force=False):
    """{current, latest, newer, notes, published, asset} - cached 30 minutes."""
    cur = current_build(root)
    with _lock:
        hit = _cache["data"]
        if hit and not force and time.time() - _cache["t"] < 1800:
            return dict(hit, current=cur, newer=build_number(hit["latest"]) > build_number(cur))
    rel = json.loads(_get(API_LATEST % REPO, 2 * 1024 * 1024).decode("utf-8"))
    tag = rel.get("tag_name") or ""
    asset = next((a for a in rel.get("assets") or [] if str(a.get("name", "")).endswith(".zip")), None)
    data = {"latest": tag.lstrip("v"), "notes": (rel.get("body") or "")[:4000],
            "published": rel.get("published_at"), "asset": asset and asset.get("browser_download_url")}
    with _lock:
        _cache.update(t=time.time(), data=data)
    return dict(data, current=cur, newer=build_number(tag) > build_number(cur))


def read_release(zip_bytes, expect_build):
    """The release's files, checked. Returns ({name: bytes}, [launcher names])."""
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise UpdateError("קובץ העדכון פגום")
    files, launchers = {}, {}
    total = 0
    for info in z.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if not name.startswith("dispatch-live-app/"):
            continue
        rel = name[len("dispatch-live-app/"):]
        if ".." in rel.split("/") or rel.startswith("/"):
            raise UpdateError("קובץ העדכון מכיל נתיב לא חוקי")
        total += info.file_size
        if total > 40 * 1024 * 1024:
            raise UpdateError("קובץ העדכון גדול מהמותר")
        if rel in PROGRAM_FILES or DOC_RE.match(rel):
            files[rel] = z.read(info)
        elif LAUNCHER_RE.match(rel):
            launchers[rel] = z.read(info)
    for need in ("index.html", "server.py"):
        if need not in files:
            raise UpdateError("בקובץ העדכון חסר %s" % need)
    m = re.search(rb"const DL_BUILD = '([^']+)'", files["index.html"])
    got = m.group(1).decode() if m else ""
    if expect_build and got != expect_build:
        raise UpdateError("הגרסה בקובץ (%s) אינה הגרסה שפורסמה (%s)" % (got or "?", expect_build))
    for n, b in files.items():
        if n.endswith(".py"):
            try:
                compile(b, n, "exec")
            except SyntaxError as e:
                raise UpdateError("שגיאה בקוד של %s (%s)" % (n, e))
    return files, launchers


def _backup_dir(root):
    return os.path.join(root, "backup")


def install(root, files, launchers=None):
    """Back up the running files, then put the new ones in place. Returns a summary."""
    cur = current_build(root) or "unknown"
    bdir = os.path.join(_backup_dir(root), "v" + re.sub(r"[^0-9A-Za-z.-]", "_", cur))
    os.makedirs(bdir, exist_ok=True)
    changed = []
    for name, data in files.items():
        dst = os.path.join(root, *name.split("/"))
        old = None
        if os.path.exists(dst):
            with open(dst, "rb") as f:
                old = f.read()
            bpath = os.path.join(bdir, *name.split("/"))
            os.makedirs(os.path.dirname(bpath), exist_ok=True)
            with open(bpath, "wb") as f:
                f.write(old)
        if old == data:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
        changed.append(name)
    with open(os.path.join(bdir, "files.json"), "w", encoding="utf-8") as f:
        json.dump(sorted(files), f)
    _prune_backups(root)
    stale = []
    for name, data in (launchers or {}).items():
        dst = os.path.join(root, name)
        try:
            with open(dst, "rb") as f:
                same = f.read() == data
        except OSError:
            same = False
        if not same:
            stale.append(name)
    return {"from": cur, "changed": changed, "launchers": sorted(stale)}


def _prune_backups(root):
    try:
        dirs = sorted((d for d in os.listdir(_backup_dir(root)) if d.startswith("v")),
                      key=lambda d: os.path.getmtime(os.path.join(_backup_dir(root), d)))
        for d in dirs[:-KEEP_BACKUPS]:
            shutil.rmtree(os.path.join(_backup_dir(root), d), ignore_errors=True)
    except OSError:
        pass


def backups(root):
    try:
        out = []
        for d in os.listdir(_backup_dir(root)):
            p = os.path.join(_backup_dir(root), d)
            if d.startswith("v") and os.path.exists(os.path.join(p, "files.json")):
                out.append((os.path.getmtime(p), d[1:]))
        return [b for _, b in sorted(out, reverse=True)]
    except OSError:
        return []


def rollback(root, build=None):
    """Put a backup back (the newest, or the build named). The files it
    replaces are backed up first, so a rollback can itself be undone."""
    have = backups(root)
    if not have:
        raise UpdateError("אין גרסה קודמת שמורה")
    build = build or have[0]
    if build not in have:
        raise UpdateError("הגרסה %s לא שמורה" % build)
    src = os.path.join(_backup_dir(root), "v" + build)
    with open(os.path.join(src, "files.json"), encoding="utf-8") as f:
        names = [n for n in json.load(f) if n in PROGRAM_FILES or DOC_RE.match(n)]
    files = {}
    for n in names:
        p = os.path.join(src, *n.split("/"))
        if os.path.exists(p):
            with open(p, "rb") as f:
                files[n] = f.read()
    if "index.html" not in files or "server.py" not in files:
        raise UpdateError("הגיבוי של %s חסר" % build)
    res = install(root, files)
    res["to"] = build
    return res


def apply(root):
    info = check(root, force=True)
    if not info["newer"]:
        raise UpdateError("אין גרסה חדשה יותר (מותקנת %s, אחרונה %s)" % (info["current"], info["latest"]))
    if not info.get("asset"):
        raise UpdateError("לגרסה %s אין קובץ התקנה" % info["latest"])
    blob = _get(info["asset"], MAX_ZIP, accept="application/octet-stream")
    files, launchers = read_release(blob, info["latest"])
    res = install(root, files, launchers)
    res["to"] = info["latest"]
    with _lock:
        _cache["data"] = None
    return res


def restart(argv, delay=1.0, pythonw_ok=True):
    """Replace this process with the updated program, after `delay` seconds
    (so the HTTP answer that asked for it is delivered first).
    - Linux/macOS: exec in place (same process, the launcher keeps waiting).
    - Windows, started by a launcher that loops (DL_RESTART=loop): exit 75,
      and the launcher runs the program again in the same window.
    - Windows otherwise (background mode): start a new copy, then exit."""
    def go():
        time.sleep(delay)
        env = dict(os.environ, NOOPEN="1", DL_RESTARTED="1")
        exe = sys.executable
        args = [exe] + list(argv)
        sys.stdout and sys.stdout.flush()
        if os.name != "nt":
            os.execve(exe, args, env)
        if os.environ.get("DL_RESTART") == "loop":
            os._exit(75)
        if pythonw_ok:
            w = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.exists(w):
                args[0] = w
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(args, cwd=os.path.dirname(os.path.abspath(argv[0])), env=env,
                         creationflags=flags, close_fds=True)
        os._exit(0)
    threading.Thread(target=go, daemon=True).start()


def bind_with_retry(make):
    """After a restart the old process may still hold the port for a moment."""
    tries = 40 if os.environ.get("DL_RESTARTED") == "1" else 1
    for i in range(tries):
        try:
            return make()
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(0.5)
