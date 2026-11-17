"""Source health records and the daily self-test."""
import json, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server


class TestHealthRecords(unittest.TestCase):
    def setUp(self):
        server._health.clear()
        self._brain = server.brain_get
        server.brain_get = lambda path: None

    def tearDown(self):
        server.brain_get = self._brain

    def test_source_keys(self):
        self.assertEqual(server._health_key("/oref/history", "")[0], "oref_history")
        self.assertTrue(server._health_key("/oref/alerts", "")[2])                 # critical
        self.assertEqual(server._health_key("/fetch", "url=https%3A%2F%2Fwww.ynet.co.il%2Frss.xml")[0], "feed:ynet.co.il")
        self.assertEqual(server._health_key("/telegram", "ch=idf_telegram")[0], "tg:idf_telegram")
        self.assertIsNone(server._health_key("/index.html", ""))
        self.assertIsNone(server._health_key("/health", ""))

    def test_what_counts_as_a_failure(self):
        self.assertEqual(server._error_of(200, '[{"a":1}]'), "")
        self.assertEqual(server._error_of(200, '{"error":"brain_down"}'), "brain_down")
        self.assertIn("HTTP 502", server._error_of(502, "Upstream fetch failed: timed out"))
        self.assertEqual(server._error_of(200, b"<rss></rss>"), "")

    def test_down_then_recovered(self):
        server.health_note("quakes", "רעידות אדמה", False, 900, "quakes unavailable")
        server.health_note("quakes", "רעידות אדמה", False, 900, "quakes unavailable")
        h = [x for x in server.health_snapshot()["sources"] if x["id"] == "quakes"][0]
        self.assertEqual((h["state"], h["streak"], h["error"]), ("down", 2, "quakes unavailable"))
        self.assertIn("רעידות אדמה", server.health_snapshot()["down"])
        server.health_note("quakes", "רעידות אדמה", True, 300)
        h = [x for x in server.health_snapshot()["sources"] if x["id"] == "quakes"][0]
        self.assertEqual((h["state"], h["streak"], h["okCount"], h["failCount"]), ("ok", 0, 1, 2))

    def test_snapshot_reports_a_missing_brain(self):
        self.assertFalse(server.health_snapshot()["brain"]["up"])


class TestSelfTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(server, k) for k in
                       ("fetch_oref", "brain_get", "fetch_markets", "fetch_quakes", "fetch_flights",
                        "fetch_roads", "fetch_air", "fetch_idf_spokesperson")}
        server._selftest.update(at=0.0, running=False, results=[])

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(server, k, v)

    def _all_good(self):
        api = '{"alarmsGroupedByDate":[]}'
        server.fetch_oref = lambda url: ((True, 200, api) if "AlertHistory" in url else
                                         (False, 502, '{"error":"500"}') if "restrictionsPolicy" in url else
                                         (True, 200, "[]"))
        server.brain_get = lambda p: {"segments": [1, 2], "checkedAt": "10:00", "health": {"failing": False},
                                      "budget": {"state": "healthy"}}
        server.fetch_markets = lambda: '[{"name":"x"}]'
        server.fetch_quakes = lambda: '[]'
        server.fetch_flights = lambda: '{"dep":{},"arr":{}}'
        server.fetch_roads = lambda: '{"items":[]}'
        server.fetch_air = lambda lat, lng: '{"aqi":41}'
        server.fetch_idf_spokesperson = lambda: '[{"text":"x"}]'

    def _result(self, sid):
        return [r for r in server._selftest["results"] if r["id"] == sid][0]

    def test_everything_healthy(self):
        self._all_good()
        server.run_selftest()
        self.assertEqual([r["id"] for r in server._selftest["results"] if r["ok"] is False], [])
        self.assertIsNone(self._result("oref_policy")["ok"])        # off on purpose, not a failure
        self.assertEqual(len(server._selftest["results"]), len(server.SELFTEST_PROBES))

    def test_oref_changing_its_api_shape_is_caught(self):
        # this is the failure that went unnoticed for weeks before v73
        self._all_good()
        server.fetch_oref = lambda url: (True, 200, "<!DOCTYPE html><title>Page Error</title>")
        server.run_selftest()
        self.assertFalse(self._result("oref_history")["ok"])
        self.assertFalse(self._result("oref_alerts")["ok"])

    def test_google_refusing_the_key_is_caught(self):
        self._all_good()
        server.brain_get = lambda p: {"segments": [], "health": {"failing": True,
                                      "error": {"code": 403, "hint": "key rejected"}}, "budget": {}}
        server.run_selftest()
        r = self._result("traffic")
        self.assertFalse(r["ok"]); self.assertIn("key rejected", r["detail"])

    def test_brain_not_running_is_caught_only_when_traffic_is_set_up(self):
        import tempfile
        self._all_good()
        server.brain_get = lambda p: None
        root = server.ROOT
        try:
            server.ROOT = tempfile.mkdtemp()
            server.run_selftest()
            self.assertIsNone(self._result("traffic")["ok"])         # no key: traffic is simply not in use
            open(os.path.join(server.ROOT, "google.key"), "w").write("k")
            server.run_selftest()
            self.assertFalse(self._result("traffic")["ok"])           # key present, service down: a real fault
        finally:
            server.ROOT = root

    def test_a_crashing_probe_does_not_stop_the_rest(self):
        self._all_good()
        def boom():
            raise RuntimeError("upstream exploded")
        server.fetch_markets = boom
        server.run_selftest()
        self.assertFalse(self._result("markets")["ok"])
        self.assertTrue(self._result("air")["ok"])

    def test_policy_stays_off(self):
        self.assertFalse(server.fetch_oref_policy()[0])


if __name__ == "__main__":
    unittest.main()


class TestStaticAllowList(unittest.TestCase):
    """Replays the static-file rule the way Windows would resolve the names."""

    TRICKS = ["/google.key", "/google.key.", "/google.key ", "/GOOGLE.KEY", "/google.key::$DATA", "/GOOGLE~1.KEY",
              "/CONFIG~1.JSO", "/config.local.json", "/config.local.json.", "/data/archive.db", "/data./archive.db",
              "/logs./server.log", "/C:data/archive.db", "/C:/Users/x/app/data/state.json", "/..%2fgoogle.key",
              "/../google.key", "/./google.key", "//google.key", "/index.html/../google.key", "/index.html.",
              "/index.html::$DATA", "/index.html%00.key", "/.gitignore", "/server.py", "/dlconfig.py", "/brain.py",
              "/tests/test_oref.py", "/backup/x", "\\google.key", "/index.html\\..\\google.key", "/INDEX~1.HTM"]

    def test_only_the_page_is_servable(self):
        for t in self.TRICKS:
            self.assertIsNone(server.static_path(t), t)
        for ok in ("/", "/index.html"):
            self.assertTrue(server.static_path(ok).endswith("index.html"), ok)

    def test_no_trick_survives_windows_name_resolution(self):
        # Windows drops trailing dots and spaces and resolves streams and
        # drive-relative names; whatever passes the rule must be index.html
        # For every trick, work out which file Windows would actually open
        # (trailing dots/spaces dropped, ":stream" ignored, drive-relative and
        # backslash forms resolved). If that file is anything but the page, the
        # rule must refuse it - and it must refuse index.html by any name but
        # its plain one, since a variant spelling is how the old rule was beaten.
        import ntpath
        for t in self.TRICKS:
            name = ntpath.normpath("C:\\app\\" + t.replace("/", "\\").lstrip("\\"))
            opened = ntpath.basename(name).split(":")[0].rstrip(". ").lower()
            self.assertIsNone(server.static_path(t), "%s -> Windows opens %r" % (t, opened))
        self.assertGreaterEqual(sum(1 for t in self.TRICKS if "key" in t.lower()), 8)


class TestProxyTargets(unittest.TestCase):
    def test_the_proxy_does_not_reach_this_machine_or_the_home_network(self):
        for bad in ("http://127.0.0.1:3100/config", "http://localhost:3200/trigger", "http://[::1]:3100/",
                    "http://192.168.1.1/", "http://10.0.0.5/x", "http://169.254.169.254/latest",
                    "http://0.0.0.0:3100/", "file:///etc/passwd", "ftp://example.com/", "http://x.localhost/"):
            self.assertFalse(server._public_target(bad), bad)

    def test_same_origin_rule_for_the_state_relay(self):
        class H:
            def __init__(self, host, origin=None):
                self.headers = {"Host": host}
                if origin: self.headers["Origin"] = origin
        self.assertTrue(server._same_origin(H("192.168.1.5:3100")))                                 # no Origin
        self.assertTrue(server._same_origin(H("192.168.1.5:3100", "http://192.168.1.5:3100")))     # another screen of ours
        self.assertFalse(server._same_origin(H("localhost:3100", "https://evil.example")))


class TestFetchersDoNotPassOffOldData(unittest.TestCase):
    def test_own_cache_covers_a_failure_only_briefly(self):
        import time
        fresh = {"t": time.time() - 60, "data": "[1]"}
        old = {"t": time.time() - 3 * 86400, "data": "[1]"}
        self.assertEqual(server._fresh_or_error(fresh, 900, "x unavailable"), "[1]")
        self.assertIn("error", server._fresh_or_error(old, 900, "x unavailable"))
        self.assertIn("error", server._fresh_or_error({"t": 0, "data": None}, 900, "x unavailable"))

    def test_health_table_is_bounded(self):
        server._health.clear()
        for i in range(500):
            server.health_note("feed:h%d" % i, "x", False, 1, "e")
        self.assertLessEqual(len(server._health), 120)
        self.assertIsNone(server._health_key("/telegram", "ch=not_a_real_channel_%d" % 7))
        server._health.clear()


class TestProxyHops(unittest.TestCase):
    """The /fetch proxy against a local web server standing in for the internet."""

    @classmethod
    def setUpClass(cls):
        import http.server, threading

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                if self.path == "/secret":
                    body = b"LOOPBACK SECRET"
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/to-loopback":
                    self.send_response(302); self.send_header("Location", "http://127.0.0.1:%d/secret" % cls.port)
                    self.send_header("Content-Length", "0"); self.end_headers()
                elif self.path == "/hop":
                    self.send_response(302); self.send_header("Location", "/page")
                    self.send_header("Content-Length", "0"); self.end_headers()
                else:
                    body = b"<rss>ok</rss>"
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                    self.wfile.write(body)
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        self._gai, self._conn = server.socket.getaddrinfo, server._pinned_connection
        self._getproxies = server.urllib.request.getproxies
        server.urllib.request.getproxies = lambda: {}          # these tests exercise the direct path
        self.resolutions = []

    def tearDown(self):
        server.socket.getaddrinfo, server._pinned_connection = self._gai, self._conn
        server.urllib.request.getproxies = self._getproxies

    def test_behind_a_proxy_every_hop_is_still_checked(self):
        server.urllib.request.getproxies = self._getproxies
        self.assertFalse(server._hop_allowed("http://127.0.0.1:3100/config"))
        self.assertFalse(server._hop_allowed("http://192.168.1.1/"))
        self.assertFalse(server._hop_allowed("file:///etc/passwd"))
        server.urllib.request.getproxies = lambda: {"http": "http://127.0.0.1:9", "https": "http://127.0.0.1:9"}
        self.assertEqual(server.fetch_public("http://127.0.0.1:3100/config")[1], 400)

    def _internet(self, answers):
        """public.test 'resolves' to a public address; connections to that
        address are delivered to the local stand-in server."""
        import socket
        real_conn, port, test = self._conn, self.port, self

        real_gai = self._gai

        def gai(host, p, *a, **k):
            try:
                __import__("ipaddress").ip_address(str(host).strip("[]"))
                return real_gai(host, p, *a, **k)          # a literal address: nothing to resolve
            except ValueError:
                pass
            test.resolutions.append(host)
            ip = answers(host, len(test.resolutions))
            if ip is None:
                raise socket.gaierror("name not known")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, p))]
        server.socket.getaddrinfo = gai
        server._pinned_connection = lambda scheme, host, p, ip, timeout: real_conn(
            scheme, host, p, "127.0.0.1" if ip == "93.184.216.34" else ip, timeout)

    def test_a_public_page_and_a_same_site_redirect_work(self):
        self._internet(lambda h, n: "93.184.216.34")
        self.assertEqual(server.fetch_public("http://public.test:%d/page" % self.port), (True, 200, "<rss>ok</rss>"))
        self.assertEqual(server.fetch_public("http://public.test:%d/hop" % self.port)[2], "<rss>ok</rss>")

    def test_a_redirect_to_this_machine_is_refused(self):
        self._internet(lambda h, n: "93.184.216.34")
        ok, status, text = server.fetch_public("http://public.test:%d/to-loopback" % self.port)
        self.assertEqual((ok, status), (False, 400)); self.assertNotIn("SECRET", text)

    def test_a_name_that_changes_its_answer_cannot_reach_this_machine(self):
        # public on the first lookup, loopback on every later one (DNS rebinding)
        self._internet(lambda h, n: "93.184.216.34" if n == 1 else "127.0.0.1")
        ok, status, text = server.fetch_public("http://public.test:%d/secret" % self.port)
        self.assertEqual(len(self.resolutions), 1)             # resolved once; the checked address is the one used
        self.assertNotEqual(server.fetch_public("http://public.test:%d/secret" % self.port)[1], 200)

    def test_a_dead_first_address_falls_back_to_the_next(self):
        import socket
        real_conn, port = self._conn, self.port
        real_gai = self._gai
        def gai(host, p, *a, **k):
            if host == "public.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.99", p)),
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))]
            return real_gai(host, p, *a, **k)
        server.socket.getaddrinfo = gai
        # .99 is dead (nothing listens on the port it is mapped to); .34 is the stand-in server
        server._pinned_connection = lambda scheme, host, p, ip, timeout: real_conn(
            scheme, host, p if ip == "93.184.216.34" else 9, "127.0.0.1", timeout)
        self.assertEqual(server.fetch_public("http://public.test:%d/page" % port), (True, 200, "<rss>ok</rss>"))

    def test_addresses_that_are_not_the_public_internet(self):
        for bad in ("http://100.64.1.1/", "http://[::ffff:127.0.0.1]/", "http://2130706433/", "http://0x7f.1/",
                    "http://public@127.0.0.1/", "http://127.1/", "http://[fe80::1]/", "http://[fc00::1]/"):
            self.assertEqual(server.fetch_public(bad)[1], 400, bad)

    def test_no_internet_is_an_ordinary_failure_not_a_refusal(self):
        # must go down the normal failure path so the last good answer is served, marked as old
        self._internet(lambda h, n: None)
        ok, status, text = server.fetch_public("http://www.ynet.co.il/rss.xml")
        self.assertEqual((ok, status), (False, 502)); self.assertIn("could not resolve", text)


class TestSelfTestDoesNotCryWolf(unittest.TestCase):
    def setUp(self):
        self._brain = server.brain_get
        server.brain_get = lambda p: None
        server._health.clear()

    def tearDown(self):
        server.brain_get = self._brain
        server._selftest.update(at=0.0, running=False, results=[])
        server._health.clear()

    def test_a_failed_probe_is_dropped_once_the_live_source_answers(self):
        import time
        server._selftest.update(at=time.time() - 100, running=False, results=[
            {"id": "oref_alerts", "label": "A", "ok": False, "detail": "x", "critical": True, "ms": 1},
            {"id": "quakes", "label": "Q", "ok": False, "detail": "x", "critical": False, "ms": 1}])
        self.assertEqual(server.health_snapshot()["selftest"]["criticalFailed"], ["A"])
        server.health_note("oref_alerts", "A", True, 100, critical=True)        # the live feed answers again
        st = server.health_snapshot()["selftest"]
        self.assertEqual(st["criticalFailed"], []); self.assertEqual(st["failed"], ["Q"])


class TestStateRelayGuards(unittest.TestCase):
    def test_port_is_part_of_the_origin(self):
        class H:
            def __init__(self, host, origin): self.headers = {"Host": host, "Origin": origin}
        self.assertTrue(server._same_origin(H("192.168.1.5:3100", "http://192.168.1.5:3100")))
        self.assertFalse(server._same_origin(H("192.168.1.5:3100", "http://192.168.1.5:8080")))
        self.assertFalse(server._same_origin(H("localhost:3100", "null")))
