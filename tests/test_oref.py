"""Red-alert history: the 2026 Oref API, its normalisation and the fallbacks."""
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server, dlarchive

API_WEEK = json.dumps({
    "lastRetrievedAlarm": {"data": "x"},
    "alarmsGroupedByDate": [
        {"key": "11.11.2026", "value": [
            {"data": "אילת", "date": "11.11.2026", "time": "12:14:38", "dateTime": "2026-11-11T12:14:00",
             "category": 13, "category_Desc": "רעידת אדמה - הנחיות חזרה למבנים", "rid": 3}]},
        {"key": "13.11.2026", "value": [
            {"data": "נחל עוז", "date": "13.11.2026", "time": "02:56:06", "dateTime": "2026-11-13T02:56:00",
             "category": 1, "category_Desc": "ירי רקטות וטילים", "rid": 1},
            {"data": "נחל עוז", "date": "13.11.2026", "time": "03:00:07", "dateTime": "2026-11-13T03:00:00",
             "category": 13, "category_Desc": "האירוע הסתיים", "rid": 2}]}]}, ensure_ascii=False)
API_EMPTY = '{"alarmsGroupedByDate":[]}'
HTML = "<!DOCTYPE html><html><title>Page Error</title></html>"


class TestNormalise(unittest.TestCase):
    def test_flat_rows_newest_first(self):
        rows = server._normalize_oref_api(API_WEEK)
        self.assertEqual([r["alertDate"] for r in rows],
                         ["2026-11-13 03:00:07", "2026-11-13 02:56:06", "2026-11-11 12:14:38"])
        self.assertEqual(rows[1], {"alertDate": "2026-11-13 02:56:06", "title": "ירי רקטות וטילים",
                                   "data": "נחל עוז", "category": 1, "rid": 1})

    def test_error_pages_are_not_data(self):
        for bad in (HTML, "[]", "", "null", '{"error":"x"}'):
            self.assertIsNone(server._normalize_oref_api(bad))

    def test_quiet_api_answer_is_an_empty_list(self):
        self.assertEqual(server._normalize_oref_api(API_EMPTY), [])


class TestHistoryChain(unittest.TestCase):
    def setUp(self):
        self.real = server.fetch_oref
        server._hist_week.update(t=0.0, rows=None, tried=0)
        self.calls = []
        self._async = server._archive_async
        server._archive_async = lambda rows: dlarchive.add_alerts(rows)     # synchronous in tests
        server._backfill["t"] = __import__("time").time()                   # no background back-fill
        self._db = dlarchive.DB_PATH          # never touch the real archive
        dlarchive.DB_PATH = os.path.join(tempfile.mkdtemp(), "archive.db")

    def tearDown(self):
        server.fetch_oref = self.real
        server._archive_async = self._async
        dlarchive.DB_PATH = self._db

    def _serve(self, table):
        def fake(url):
            self.calls.append(url)
            for needle, answer in table:
                if needle in url:
                    return answer
            return (False, 502, '{"error":"unreachable"}')
        server.fetch_oref = fake

    def test_last_day_rows_win(self):
        self._serve([("period=lastDay", (True, 200, API_WEEK))])
        ok, st, txt = server.fetch_oref_history()
        self.assertTrue(ok); self.assertEqual(len(json.loads(txt)), 3)
        self.assertEqual(len(self.calls), 1)

    def test_quiet_day_falls_back_to_last_week_and_caches_it(self):
        self._serve([("period=lastDay", (True, 200, API_EMPTY)), ("period=lastWeek", (True, 200, API_WEEK))])
        ok, _, txt = server.fetch_oref_history()
        self.assertTrue(ok); self.assertEqual(len(json.loads(txt)), 3)
        server.fetch_oref_history()
        self.assertEqual(sum("lastWeek" in u for u in self.calls), 1)   # second poll used the cache

    def test_a_failing_week_fetch_is_retried_at_most_once_a_minute(self):
        self._serve([("period=lastDay", (True, 200, API_EMPTY)), ("period=lastWeek", (False, 502, "{}")),
                     ("AlertsHistory.json", (True, 200, "[]"))])
        for _ in range(5):
            server.fetch_oref_history()
        self.assertEqual(sum("lastWeek" in u for u in self.calls), 1)

    def test_truly_quiet_week_is_a_valid_empty_answer(self):
        self._serve([("period=", (True, 200, API_EMPTY)), ("AlertsHistory.json", (True, 200, "[]"))])
        self.assertEqual(server.fetch_oref_history(), (True, 200, "[]"))

    def test_quiet_week_shows_the_last_event_from_our_own_archive(self):
        # Oref only goes back a week; the deck remembers further
        self._serve([("period=lastDay", (True, 200, API_WEEK))])
        server.fetch_oref_history()                      # this poll is archived
        server._hist_week.update(t=0.0, rows=None, tried=0)
        self._serve([("period=", (True, 200, API_EMPTY)), ("AlertsHistory.json", (True, 200, "[]"))])
        ok, _, txt = server.fetch_oref_history()
        rows = json.loads(txt)
        self.assertTrue(ok); self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["alertDate"], "2026-11-13 03:00:07")
        self.assertEqual(server._hist_status["source"], "archive")

    def test_api_down_and_empty_legacy_file_is_an_outage_not_a_quiet_day(self):
        # the legacy file is empty most of the time; it proves nothing
        self._serve([("period=", (True, 200, HTML)), ("AlertsHistory.json", (True, 200, "[]"))])
        ok, st, _ = server.fetch_oref_history()
        self.assertFalse(ok); self.assertEqual(st, 502)

    def test_week_list_that_cannot_be_refreshed_expires(self):
        import time
        server._hist_week.update(t=time.time() - 3 * 86400, rows=[{"alertDate": "2026-11-01 10:00:00", "data": "x"}])
        self._serve([("period=lastDay", (True, 200, API_EMPTY)), ("period=lastWeek", (False, 502, "{}")),
                     ("AlertsHistory.json", (True, 200, "[]"))])
        ok, _, txt = server.fetch_oref_history()
        self.assertEqual(json.loads(txt), [])            # three-day-old "last week" is not served
        self.assertIsNone(server._hist_week["rows"])

    def test_archive_is_backfilled_from_the_last_month(self):
        import time
        server._backfill["t"] = 0
        self._serve([("period=lastDay", (True, 200, API_EMPTY)), ("period=lastWeek", (True, 200, API_EMPTY)),
                     ("period=lastMonth", (True, 200, API_WEEK)), ("AlertsHistory.json", (True, 200, "[]"))])
        server.fetch_oref_history()
        for _ in range(40):
            if len(dlarchive.recent_alerts(days=400)) == 3: break
            time.sleep(0.05)
        self.assertEqual(len(dlarchive.recent_alerts(days=400)), 3)
        self.assertEqual(sum("lastMonth" in u for u in self.calls), 1)

    def test_api_gone_uses_legacy_file(self):
        legacy = json.dumps([{"alertDate": "2026-11-13 02:56:06", "title": "t", "data": "c", "category": 1}])
        self._serve([("period=", (True, 200, HTML)), ("AlertsHistory.json", (True, 200, legacy))])
        ok, _, txt = server.fetch_oref_history()
        self.assertTrue(ok); self.assertEqual(json.loads(txt)[0]["data"], "c")

    def test_everything_down_is_an_error_not_a_quiet_day(self):
        self._serve([])
        ok, st, _ = server.fetch_oref_history()
        self.assertFalse(ok); self.assertEqual(st, 502)

    def test_html_everywhere_is_an_error(self):
        self._serve([("oref", (True, 200, HTML))])
        self.assertFalse(server.fetch_oref_history()[0])


if __name__ == "__main__":
    unittest.main()


class TestConcurrentFetch(unittest.TestCase):
    """Two screens (or the deck and the self-test) asking at the same moment."""

    def setUp(self):
        self._urlopen = server.urlopen
        server._inflight.clear(); server._oref_shared.clear()
        self.upstream_calls = 0

    def tearDown(self):
        server.urlopen = self._urlopen

    def _slow_upstream(self, body=b"[]", fail=False, delay=0.25):
        import io, time as _t
        test = self

        class Resp(io.BytesIO):
            status = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, timeout=None, context=None):
            test.upstream_calls += 1
            _t.sleep(delay)
            if fail:
                raise OSError("connection refused")
            return Resp(body)
        server.urlopen = fake

    def _race(self, n=4):
        import threading
        out = []
        ts = [threading.Thread(target=lambda: out.append(server.fetch_oref("https://oref.test/alerts"))) for _ in range(n)]
        for t in ts: t.start()
        for t in ts: t.join()
        return out

    def test_simultaneous_callers_share_one_upstream_request(self):
        self._slow_upstream()
        out = self._race(4)
        self.assertEqual([r[0] for r in out], [True] * 4)       # nobody is told "no connection"
        self.assertEqual(self.upstream_calls, 1)

    def test_a_real_failure_is_shared_too_and_the_slot_is_released(self):
        self._slow_upstream(fail=True)
        out = self._race(3)
        self.assertEqual([r[0] for r in out], [False] * 3)
        self.assertTrue(all("connection refused" in r[2] for r in out))
        self.assertEqual(server._inflight, {})
        self._slow_upstream(body=b'{"data":["x"]}', delay=0)
        self.assertTrue(server.fetch_oref("https://oref.test/alerts")[0])   # recovers on the next poll


class TestAlertShape(unittest.TestCase):
    """HTTP 200 is not an answer. Only two shapes are."""

    def test_legitimate_shapes(self):
        self.assertTrue(server._valid_alert_payload("[]"))
        self.assertTrue(server._valid_alert_payload('{"id":"1","cat":"1","title":"x","data":["a","b"],"desc":"y"}'))

    def test_a_list_of_alerts_is_merged_never_read_as_quiet(self):
        out = json.loads(server._alert_payload('[{"id":"1","title":"t","data":["a","b"]},{"id":"2","title":"t","data":["b","c"]}]'))
        self.assertEqual(out["data"], ["a", "b", "c"])
        self.assertIsNone(server._alert_payload('[{"id":"1","title":"no localities"}]'))
        # an alert that names no locality alerts nobody; "cities" is honoured when "data" is empty
        self.assertEqual(server._alert_payload('{"id":"1","title":"t","data":[]}'), "[]")
        # localities we cannot read are a failure, never quiet
        self.assertIsNone(server._alert_payload('{"title":"t","data":[{"name":"בת ים"}]}'))
        # an all-clear for one town must not relabel another town's siren
        both = json.loads(server._alert_payload('[{"title":"האירוע הסתיים","cat":"10","data":["חיפה"]},'
                                                 '{"title":"ירי רקטות וטילים","cat":"1","data":["בת ים"]}]'))
        self.assertEqual((both["title"], both["data"]), ("ירי רקטות וטילים", ["בת ים"]))
        self.assertEqual(json.loads(server._alert_payload('{"title":"t","data":[],"cities":["x"]}'))["data"], ["x"])

    def test_the_feed_in_every_encoding_it_has_used(self):
        # constructed from the feed's documented format, not a recording
        alert = '{"id":"134000000000000000","cat":"1","title":"ירי רקטות וטילים","data":["שדרות, איבים, ניר עם"],"desc":"היכנסו למרחב המוגן"}'
        for raw in (alert.encode("utf-8"), b"\xef\xbb\xbf" + alert.encode("utf-8"),
                    b"\xff\xfe" + alert.encode("utf-16-le"), b"\xfe\xff" + alert.encode("utf-16-be")):
            self.assertEqual(json.loads(server._alert_payload(server._decode_feed(raw)))["data"], ["שדרות, איבים, ניר עם"])
        for quiet in (b"", b"\xef\xbb\xbf", b"\r\n", b"\xef\xbb\xbf\r\n", b"\x00\x00\x00", b"\xff\xfe", b" \n "):
            self.assertEqual(server._decode_feed(quiet), "", quiet)          # the route turns "" into []
        self.assertEqual(server._alert_payload("[]"), "[]")

    def test_everything_else_is_a_failure_not_quiet(self):
        for bad in ("<!DOCTYPE html><title>Page moved</title>", "null", '{"message":"moved"}', '"ok"', "5",
                    "", '{"data":"not a list"}', "[1,2]", '{"error":"x"}'):
            self.assertFalse(server._valid_alert_payload(bad), bad)
