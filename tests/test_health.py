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

    def test_brain_not_running_is_caught(self):
        self._all_good()
        server.brain_get = lambda p: None
        server.run_selftest()
        self.assertFalse(self._result("traffic")["ok"])

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
