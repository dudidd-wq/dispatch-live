"""One-click update: what is accepted, what is written, what is kept."""
import io, json, os, sys, tempfile, unittest, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dlupdate


def make_zip(build="17.11-90", extra=None, drop=(), py_ok=True):
    files = {
        "index.html": ("<script>const DL_BUILD = '%s';</script>" % build).encode(),
        "server.py": b"print('new server')\n" if py_ok else b"def broken(:\n",
        "brain.py": b"x = 1\n",
        "dlupdate.py": b"y = 2\n",
        "README.md": b"readme",
        "Start-Dispatch-LIVE.bat": b"@echo off\r\nnew launcher\r\n",
        "docs/ROADMAP.md": b"roadmap",
    }
    files.update(extra or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in files.items():
            if n not in drop:
                z.writestr("dispatch-live-app/" + n, b)
    return buf.getvalue()


class TestVersions(unittest.TestCase):
    def test_build_numbers_order_by_the_counter(self):
        self.assertEqual(dlupdate.build_number("v17.11-84"), 84)
        self.assertGreater(dlupdate.build_number("v01.12-90"), dlupdate.build_number("17.11-84"))
        self.assertEqual(dlupdate.build_number("nonsense"), -1)

    def test_only_github_hosts_over_https(self):
        self.assertTrue(dlupdate._host_ok("https://api.github.com/repos/x/y/releases/latest"))
        self.assertTrue(dlupdate._host_ok("https://release-assets.githubusercontent.com/a"))
        for bad in ("http://api.github.com/x", "https://evil.com/x", "https://github.com.evil.com/x", "file:///etc/passwd"):
            self.assertFalse(dlupdate._host_ok(bad), bad)
        with self.assertRaises(dlupdate.UpdateError):
            dlupdate._get("https://evil.com/x", 10)


class TestReadRelease(unittest.TestCase):
    def test_a_good_release(self):
        files, launchers = dlupdate.read_release(make_zip(), "17.11-90")
        self.assertIn("server.py", files); self.assertIn("docs/ROADMAP.md", files)
        self.assertEqual(list(launchers), ["Start-Dispatch-LIVE.bat"])

    def test_refused_releases(self):
        cases = [(b"not a zip", "17.11-90"), (make_zip(drop=("server.py",)), "17.11-90"),
                 (make_zip(build="17.11-89"), "17.11-90"), (make_zip(py_ok=False), "17.11-90")]
        for blob, want in cases:
            with self.assertRaises(dlupdate.UpdateError):
                dlupdate.read_release(blob, want)

    def test_paths_outside_the_folder_and_private_files_are_never_taken(self):
        blob = make_zip(extra={"../evil.py": b"x", "google.key": b"stolen", "config.local.json": b"{}",
                               "hayomyom.json": b"{}", "sub/server.py": b"x"})
        try:
            files, launchers = dlupdate.read_release(blob, "17.11-90")
        except dlupdate.UpdateError:
            return                                   # refusing the whole zip is fine too
        for n in ("google.key", "config.local.json", "hayomyom.json", "sub/server.py", "../evil.py"):
            self.assertNotIn(n, files); self.assertNotIn(n, launchers)


class TestInstallAndRollback(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.w("index.html", "<script>const DL_BUILD = '17.11-84';</script>")
        self.w("server.py", "print('old server')\n")
        self.w("google.key", "secret"); self.w("config.local.json", '{"a":1}')
        self.w("Start-Dispatch-LIVE.bat", "@echo off\r\nold launcher\r\n")

    def w(self, n, t):
        with open(os.path.join(self.root, n), "w", encoding="utf-8", newline="") as f:
            f.write(t)

    def r(self, n):
        with open(os.path.join(self.root, n), encoding="utf-8", newline="") as f:
            return f.read()

    def test_install_backs_up_replaces_program_files_and_leaves_the_rest(self):
        files, launchers = dlupdate.read_release(make_zip(), "17.11-90")
        res = dlupdate.install(self.root, files, launchers)
        self.assertEqual(res["from"], "17.11-84")
        self.assertEqual(dlupdate.current_build(self.root), "17.11-90")
        self.assertEqual(self.r("server.py"), "print('new server')\n")
        self.assertEqual(self.r("google.key"), "secret")
        self.assertEqual(self.r("config.local.json"), '{"a":1}')
        self.assertEqual(self.r("Start-Dispatch-LIVE.bat"), "@echo off\r\nold launcher\r\n")   # never replaced live
        self.assertEqual(res["launchers"], ["Start-Dispatch-LIVE.bat"])                       # but reported
        self.assertEqual(dlupdate.backups(self.root), ["17.11-84"])
        # and back again
        back = dlupdate.rollback(self.root)
        self.assertEqual(back["to"], "17.11-84")
        self.assertEqual(dlupdate.current_build(self.root), "17.11-84")
        self.assertEqual(self.r("server.py"), "print('old server')\n")
        self.assertIn("17.11-90", dlupdate.backups(self.root))                               # the rollback can be undone

    def test_only_the_last_backups_are_kept(self):
        import time
        for n in range(85, 91):
            files, _ = dlupdate.read_release(make_zip(build="17.11-%d" % n), "17.11-%d" % n)
            dlupdate.install(self.root, files)
            time.sleep(0.01)
        self.assertEqual(len(dlupdate.backups(self.root)), dlupdate.KEEP_BACKUPS)

    def test_apply_end_to_end_with_github_stood_in(self):
        rel = {"tag_name": "v17.11-90", "body": "notes", "published_at": "x",
               "assets": [{"name": "dispatch-live-v17.11-90.zip", "browser_download_url": "https://github.com/a.zip"}]}
        blob = make_zip()
        real = dlupdate._get
        dlupdate._get = lambda url, limit, accept=None: json.dumps(rel).encode() if "api.github.com" in url else blob
        dlupdate._cache.update(t=0, data=None)
        try:
            info = dlupdate.check(self.root, force=True)
            self.assertTrue(info["newer"]); self.assertEqual(info["latest"], "17.11-90")
            res = dlupdate.apply(self.root)
            self.assertEqual(res["to"], "17.11-90")
            with self.assertRaises(dlupdate.UpdateError):          # nothing newer now
                dlupdate.apply(self.root)
        finally:
            dlupdate._get = real
            dlupdate._cache.update(t=0, data=None)
