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


class _InternetStandIn(unittest.TestCase):
    """setUp/tearDown and a fake resolver shared by the /fetch tests."""

    def setUp(self):
        self._gai, self._conn = server.socket.getaddrinfo, server._pinned_connection
        self._getproxies = server.urllib.request.getproxies
        server.urllib.request.getproxies = lambda: {}          # these tests exercise the direct path
        self.resolutions = []

    def tearDown(self):
        server.socket.getaddrinfo, server._pinned_connection = self._gai, self._conn
        server.urllib.request.getproxies = self._getproxies

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
        server._pinned_connection = lambda scheme, host, p, ip, timeout, ctx=None: real_conn(
            scheme, host, p, "127.0.0.1" if ip == "93.184.216.34" else ip, timeout)


class TestProxyHops(_InternetStandIn):
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

    def test_behind_a_proxy_every_hop_is_still_checked(self):
        server.urllib.request.getproxies = self._getproxies
        self.assertFalse(server._hop_allowed("http://127.0.0.1:3100/config"))
        self.assertFalse(server._hop_allowed("http://192.168.1.1/"))
        self.assertFalse(server._hop_allowed("file:///etc/passwd"))
        server.urllib.request.getproxies = lambda: {"http": "http://127.0.0.1:9", "https": "http://127.0.0.1:9"}
        self.assertEqual(server.fetch_public("http://127.0.0.1:3100/config")[1], 400)

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
        server._pinned_connection = lambda scheme, host, p, ip, timeout, ctx=None: real_conn(
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


class TestHealthTableIsOurs(unittest.TestCase):
    def setUp(self):
        server._health.clear()

    def tearDown(self):
        server._health.clear()

    def test_only_hosts_the_deck_uses_get_a_row(self):
        from urllib.parse import quote
        k = server._health_key("/fetch", "url=" + quote("https://www.ynet.co.il/Integration/StoryRss2.xml"))
        self.assertEqual(k[0], "feed:ynet.co.il")
        for junk in ("ftp://x", "http://h1.example/", "https://attacker.test/a", "http://no-such-host.invalid/"):
            self.assertIsNone(server._health_key("/fetch", "url=" + quote(junk)), junk)

    def test_a_full_table_makes_room_but_never_drops_a_critical_source(self):
        server.health_note("oref_alerts", "A", True, 1, critical=True)
        for i in range(200):
            server.health_note("feed:h%d" % i, "x", False, 1, "e")
        self.assertIn("oref_alerts", server._health)
        self.assertLessEqual(len(server._health), 80)
        server.health_note("oref_history", "H", True, 1, critical=True)     # a critical newcomer still gets in
        self.assertIn("oref_history", server._health)


class TestFetchLimits(_InternetStandIn):
    """Time, size and shape limits on /fetch, against a hostile local server."""

    @classmethod
    def setUpClass(cls):
        import http.server, threading, gzip, time as _t

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *a): pass
            def _send(self, body, extra=()):
                self.send_response(200)
                for k, v in extra: self.send_header(k, v)
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                if self.path == "/bomb":
                    self._send(gzip.compress(b"\0" * (60 * 1024 * 1024)), [("Content-Encoding", "gzip")])
                elif self.path == "/gz":
                    self._send(gzip.compress("<rss>שלום</rss>".encode()), [("Content-Encoding", "gzip"), ("Content-Type", "text/xml; charset=utf-8")])
                elif self.path == "/trickle":
                    self.send_response(200); self.send_header("Content-Length", "100"); self.end_headers()
                    for _ in range(100):
                        try:
                            self.wfile.write(b"x"); self.wfile.flush(); _t.sleep(0.5)
                        except Exception:
                            return
                elif self.path == "/slowheaders":
                    # the status line, then one header byte every half second
                    self.wfile.write(b"HTTP/1.1 200 OK\r\n"); self.wfile.flush()
                    for _ in range(40):
                        try:
                            self.wfile.write(b"X"); self.wfile.flush(); _t.sleep(0.5)
                        except Exception:
                            return
                elif self.path == "/rawdeflate":
                    import zlib
                    c = zlib.compressobj(wbits=-15); body = c.compress("<rss>raw</rss>".encode()) + c.flush()
                    self._send(body, [("Content-Encoding", "deflate")])
                elif self.path == "/zlibdeflate":
                    import zlib
                    self._send(zlib.compress(b"<rss>zlib</rss>"), [("Content-Encoding", "deflate")])
                elif self.path == "/notgz":
                    self._send(b"<rss>labelled gzip, sent plain</rss>", [("Content-Encoding", "gzip")])
                elif self.path == "/br":
                    self._send(b"\x1b\x03\x00\xf8\xa5\x40\xc2\xe3", [("Content-Encoding", "br")])
                elif self.path == "/feed;jsessionid=abc?x=1":
                    self._send(b"<rss>params kept</rss>")
                elif self.path == "/challenge":
                    self._send(b"<html><head><title>Just a moment...</title></head><body>challenge-platform</body></html>")
                else:
                    self._send(("<rss>%s</rss>" % self.path).encode())
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    def _url(self, path):
        self._internet(lambda h, n: "93.184.216.34")
        return "http://public.test:%d%s" % (self.port, path)

    def test_a_decompression_bomb_is_refused_without_inflating_it(self):
        ok, st, txt = server.fetch_public(self._url("/bomb"))
        self.assertEqual((ok, st), (False, 502)); self.assertIn("expands", txt)

    def test_gzip_feeds_still_work(self):
        self.assertEqual(server.fetch_public(self._url("/gz"))[2], "<rss>שלום</rss>")

    def test_a_trickling_server_cannot_hold_a_thread_past_the_deadline(self):
        import time
        old = server.FETCH_DEADLINE
        try:
            server.FETCH_DEADLINE = 3
            t0 = time.time()
            ok, st, txt = server.fetch_public(self._url("/trickle"))
            self.assertLess(time.time() - t0, 6)
            self.assertFalse(ok); self.assertIn("longer", txt)
        finally:
            server.FETCH_DEADLINE = old

    def test_a_server_trickling_its_headers_cannot_hold_a_thread(self):
        import time
        old = server.FETCH_DEADLINE
        try:
            server.FETCH_DEADLINE = 3
            t0 = time.time()
            ok, st, txt = server.fetch_public(self._url("/slowheaders"))
            self.assertLess(time.time() - t0, 6)
            self.assertFalse(ok)
        finally:
            server.FETCH_DEADLINE = old

    def test_every_deflate_flavour_and_a_mislabelled_body(self):
        self.assertEqual(server.fetch_public(self._url("/rawdeflate"))[2], "<rss>raw</rss>")
        self.assertEqual(server.fetch_public(self._url("/zlibdeflate"))[2], "<rss>zlib</rss>")
        self.assertEqual(server.fetch_public(self._url("/notgz"))[2], "<rss>labelled gzip, sent plain</rss>")

    def test_an_encoding_we_cannot_read_is_a_clear_failure(self):
        ok, st, txt = server.fetch_public(self._url("/br"))
        self.assertFalse(ok); self.assertIn("br", txt)

    def test_we_only_ask_for_encodings_we_can_read(self):
        self.assertEqual(server._FETCH_HEADERS["Accept-Encoding"], "gzip, deflate")

    def test_path_parameters_are_sent_as_written(self):
        self.assertEqual(server.fetch_public(self._url("/feed;jsessionid=abc?x=1"))[2], "<rss>params kept</rss>")

    def test_a_bot_protection_page_is_a_failure_not_the_feed(self):
        ok, st, txt = server.fetch_public(self._url("/challenge"))
        self.assertEqual((ok, st), (False, 502)); self.assertIn("bot-protection", txt)


class TestCertificates(unittest.TestCase):
    def test_certificates_are_verified_by_default(self):
        import ssl
        self.assertEqual(server._SSL_CTX.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(server._SSL_CTX.check_hostname)
        import brain
        self.assertEqual(brain._SSL.verify_mode, ssl.CERT_REQUIRED)       # the Google key is never sent unverified

    def test_an_unverifiable_certificate_is_retried_once_and_recorded(self):
        import ssl, urllib.error
        calls = []
        real = server.urlopen
        def fake(req, timeout=None, context=None):
            calls.append(context)
            if context is server._SSL_CTX:
                raise urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
            class R:
                status = 200
                def __enter__(s): return s
                def __exit__(s, *a): return False
            return R()
        server.urlopen = fake
        server._tls_unverified.clear()
        try:
            server.open_url(server.Request("https://feed.example/rss"), 5)
        finally:
            server.urlopen = real
        self.assertEqual(calls, [server._SSL_CTX, server._SSL_LOOSE])
        self.assertIn("feed.example", server._tls_unverified)
        self.assertIn("feed.example", server.health_snapshot()["tlsUnverified"])
        server._tls_unverified.clear()

    def test_other_errors_are_not_retried_unverified(self):
        import urllib.error
        calls = []
        real = server.urlopen
        def fake(req, timeout=None, context=None):
            calls.append(context); raise urllib.error.URLError("connection refused")
        server.urlopen = fake
        try:
            with self.assertRaises(urllib.error.URLError):
                server.open_url(server.Request("https://feed.example/rss"), 5)
        finally:
            server.urlopen = real
        self.assertEqual(calls, [server._SSL_CTX])


class TestHayomYomFile(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._root = server.ROOT
        server.ROOT = tempfile.mkdtemp()
        server._hayom_cache.update(key=None, data={})

    def tearDown(self):
        server.ROOT = self._root

    def test_no_file_means_no_entry(self):
        self.assertIsNone(server.hayomyom_entry("11 Kislev 5787"))

    def test_lookup_by_hebcal_date_and_bad_files(self):
        import json as _j, os as _o
        p = _o.path.join(server.ROOT, "hayomyom.json")
        with open(p, "w", encoding="utf-8") as f:
            _j.dump({"11 Kislev": "sample", "1  Adar II": "x"}, f)
        self.assertEqual(server.hayomyom_entry("11 Kislev 5787")["text"], "sample")
        self.assertEqual(server.hayomyom_entry("1 Adar II 5787")["text"], "x")
        self.assertIsNone(server.hayomyom_entry("12 Kislev 5787"))
        with open(p, "w", encoding="utf-8") as f:
            f.write("not json")
        _o.utime(p, (1, 1))
        self.assertIsNone(server.hayomyom_entry("11 Kislev 5787"))       # a broken file is ignored
        self.assertIsNone(server.static_path("/hayomyom.json"))          # and never served as a file
