"""Local archive (SQLite) and the last-good-answer cache."""
import json, os, sys, tempfile, time, unittest
from datetime import datetime, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dlarchive, server


def stamp(hours_ago):
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


class Base(unittest.TestCase):
    def setUp(self):
        self._db = dlarchive.DB_PATH
        dlarchive.DB_PATH = os.path.join(tempfile.mkdtemp(), "data", "archive.db")

    def tearDown(self):
        dlarchive.DB_PATH = self._db


class TestAlerts(Base):
    def test_duplicates_are_stored_once(self):
        rows = [{"alertDate": stamp(2), "title": "ירי רקטות וטילים", "data": "שדרות", "category": 1, "rid": 1},
                {"alertDate": stamp(2), "title": "ירי רקטות וטילים", "data": "שדרות", "category": 1, "rid": 1}]
        self.assertEqual(dlarchive.add_alerts(rows), 1)
        self.assertEqual(dlarchive.add_alerts(rows), 0)            # every poll repeats the same rows
        self.assertEqual(len(dlarchive.recent_alerts()), 1)

    def test_bad_rows_are_skipped_not_fatal(self):
        self.assertEqual(dlarchive.add_alerts([{"alertDate": "", "data": "x"}, {"foo": 1}, None and {}]), 0)
        self.assertEqual(dlarchive.add_alerts(None), 0)

    def test_newest_first_and_window(self):
        dlarchive.add_alerts([{"alertDate": stamp(1), "title": "a", "data": "A", "category": 1},
                              {"alertDate": stamp(30), "title": "b", "data": "B", "category": 2},
                              {"alertDate": stamp(24 * 45), "title": "old", "data": "C", "category": 1}])
        self.assertEqual([r["data"] for r in dlarchive.recent_alerts(days=30)], ["A", "B"])


class TestSummary(Base):
    def test_period_summary(self):
        t = stamp(3)
        dlarchive.add_alerts([
            {"alertDate": t, "title": "ירי רקטות וטילים", "data": "שדרות", "category": 1},
            {"alertDate": t, "title": "ירי רקטות וטילים", "data": "נתיבות", "category": 1},
            {"alertDate": stamp(2), "title": "האירוע הסתיים", "data": "שדרות", "category": 13},
            {"alertDate": stamp(40), "title": "חדירת כלי טיס עוין", "data": "קריית שמונה", "category": 2}])
        now = time.time()
        for i, score in enumerate([20, 35, 60, 40]):
            dlarchive.add_tension(score, 1, 2, 3, now=now - (4 - i) * 3600)
        dlarchive.add_traffic({"segments": [1], "counts": {"heavy": 1, "slow": 2, "free": 5}, "measured": 4,
                               "worst": {"road": "איילון דרום", "delayMin": 27},
                               "hotspots": [{"road": "איילון דרום", "leg": "x", "delayMin": 27}]}, now=now - 3600)
        s = dlarchive.summary(24)
        self.assertEqual(s["alerts"]["events"], 1)                 # one barrage, two localities
        self.assertEqual(s["alerts"]["localities"], 2)             # "event ended" rows are not attacks
        self.assertEqual(s["alerts"]["byType"][0], ["ירי רקטות וטילים", 1] if isinstance(s["alerts"]["byType"][0], list)
                         else ("ירי רקטות וטילים", 1))
        self.assertEqual((s["tension"]["min"], s["tension"]["max"], s["tension"]["last"]), (20, 60, 40))
        self.assertEqual(s["traffic"]["peak"]["road"], "איילון דרום")
        self.assertEqual(s["traffic"]["heavyMinutes"], 15)
        self.assertEqual(dlarchive.summary(72)["alerts"]["events"], 2)

    def test_empty_archive_is_not_an_error(self):
        s = dlarchive.summary(24)
        self.assertEqual(s["alerts"]["events"], 0)
        self.assertIsNone(s["tension"]); self.assertIsNone(s["traffic"])
        self.assertEqual(len(dlarchive.series(7)["dayList"]), 7)

    def test_tension_is_one_reading_per_five_minutes(self):
        now = 1_800_000_000
        dlarchive.add_tension(10, now=now); dlarchive.add_tension(30, now=now + 60)
        dlarchive.add_tension(50, now=now + 400)
        self.assertEqual(dlarchive.stats()["tension"]["rows"], 2)
        self.assertFalse(dlarchive.add_tension("abc"))

    def test_series_and_prune(self):
        now = time.time()
        dlarchive.add_tension(42, now=now - 3600)
        dlarchive.add_tension(10, now=now - (dlarchive.KEEP_DAYS + 5) * 86400)
        dlarchive.add_alerts([{"alertDate": stamp(5), "title": "a", "data": "A", "category": 1}])
        s = dlarchive.series(7)
        self.assertEqual(sum(s["alertsPerDay"]), 1)
        self.assertEqual(s["tension"][-1][1], 42)
        dlarchive.prune()
        self.assertEqual(dlarchive.stats()["tension"]["rows"], 1)

    def test_unusable_database_never_raises(self):
        dlarchive.DB_PATH = os.path.join(tempfile.mkdtemp(), "data")     # a directory, not a file
        os.makedirs(dlarchive.DB_PATH)
        self.assertEqual(dlarchive.add_alerts([{"alertDate": stamp(1), "data": "A"}]), 0)
        self.assertEqual(dlarchive.recent_alerts(), [])
        self.assertIn("error", dlarchive.summary(24))


class TestLastGoodAnswer(unittest.TestCase):
    def setUp(self):
        server._last_good.clear()

    def test_remember_and_recall(self):
        server._remember_good("/quakes", 200, b"[1]", "application/json")
        self.assertEqual(server._recall_good("/quakes")[1], b"[1]")
        self.assertIsNone(server._recall_good("/markets"))

    def test_too_old_is_not_served(self):
        server._last_good["/quakes"] = (time.time() - server.STALE_MAX_SEC - 5, b"[1]", "application/json")
        self.assertIsNone(server._recall_good("/quakes"))

    def test_live_alert_feed_is_never_served_stale(self):
        # an old "no alert" is worse than an honest "no connection"
        self.assertIn("oref_alerts", server.STALE_NEVER)

    def _handler(self, sid, cache_id):
        import io

        class H(server.Handler):
            def __init__(self):
                self.out, self.status, self.wfile = {}, None, io.BytesIO()
            def send_response(self, status):
                self.status = status
            def send_header(self, k, v):
                self.out[k] = v
            def end_headers(self):
                pass
        h = H()
        h._hkey, h._cache_id = (sid, "label", False), cache_id
        return h

    def test_failure_is_covered_by_the_last_good_answer_and_says_so(self):
        h = self._handler("quakes", "/quakes")
        h._send(200, '[{"mag":3.1}]', "application/json")
        self.assertNotIn("X-DL-Stale", h.out)
        h = self._handler("quakes", "/quakes")
        h._send(200, '{"error":"quakes unavailable"}', "application/json")
        self.assertEqual(h.status, 200)
        self.assertEqual(h.wfile.getvalue(), b'[{"mag":3.1}]')
        self.assertIn("X-DL-Stale", h.out)
        self.assertIn("quakes unavailable", h._resp[1])        # health still sees the real failure
        self.assertIsNotNone(h._stale)

    def test_live_alerts_fail_honestly(self):
        h = self._handler("oref_alerts", "/oref/alerts")
        h._send(200, "[]", "application/json")
        h = self._handler("oref_alerts", "/oref/alerts")
        h._send(502, '{"error":"oref fetch failed"}', "application/json")
        self.assertEqual(h.status, 502)
        self.assertNotIn("X-DL-Stale", h.out)

    def test_cache_is_bounded(self):
        for i in range(140):
            server._remember_good("/fetch?url=%d" % i, 200, b"x", "text/plain")
        self.assertLessEqual(len(server._last_good), 91)


if __name__ == "__main__":
    unittest.main()
