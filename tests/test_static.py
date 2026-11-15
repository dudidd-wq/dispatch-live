"""Things that have each broken a release at least once."""
import glob, os, re, subprocess, unittest
ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(name, mode="r"):
    with open(os.path.join(ROOT, name), mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


class TestStatic(unittest.TestCase):
    def test_batch_files_are_ascii_with_crlf(self):
        bats = glob.glob(os.path.join(ROOT, "*.bat"))
        self.assertTrue(bats)
        for p in bats:
            with open(p, "rb") as f:
                raw = f.read()
            self.assertTrue(all(b < 128 for b in raw), "%s has non-ASCII bytes (cmd.exe breaks)" % p)
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), "%s has bare LF line endings" % p)

    def test_build_tag_is_the_same_in_both_places(self):
        html = read("index.html")
        a = re.findall(r"const DL_BUILD = '([^']+)'", html)
        b = re.findall(r'<b class="build-tag"[^>]*>v([^<]+)<', html)
        self.assertEqual(len(a), 1); self.assertEqual(len(b), 1)
        self.assertEqual(a[0], b[0])

    def test_no_api_key_is_tracked(self):
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        self.assertNotIn("google.key", files)
        for f in files:
            try:
                txt = read(f)
            except Exception:
                continue
            self.assertIsNone(re.search(r"AIza[0-9A-Za-z_\-]{35}", txt), "possible Google API key in " + f)

    def test_deck_never_calls_google_directly(self):
        # the brain is the only component allowed to spend the Routes quota
        self.assertNotIn("routes.googleapis.com", read("server.py"))
        self.assertNotIn("routes.googleapis.com", read("index.html"))

    def test_maps_do_not_depend_on_alert_history(self):
        html = read("index.html")
        self.assertEqual(html.count("function alertsSplitHtml"), 1)
        self.assertGreaterEqual(html.count("alertsSplitHtml("), 4)    # definition + null, empty, rows


if __name__ == "__main__":
    unittest.main()
