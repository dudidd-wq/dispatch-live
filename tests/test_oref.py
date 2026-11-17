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
        server._hist_week.update(t=0.0, rows=None)
        self.calls = []
        self._db = dlarchive.DB_PATH          # never touch the real archive
        dlarchive.DB_PATH = os.path.join(tempfile.mkdtemp(), "archive.db")

    def tearDown(self):
        server.fetch_oref = self.real
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

    def test_truly_quiet_week_is_a_valid_empty_answer(self):
        self._serve([("period=", (True, 200, API_EMPTY)), ("AlertsHistory.json", (True, 200, "[]"))])
        self.assertEqual(server.fetch_oref_history(), (True, 200, "[]"))

    def test_quiet_week_shows_the_last_event_from_our_own_archive(self):
        # Oref only goes back a week; the deck remembers further
        self._serve([("period=lastDay", (True, 200, API_WEEK))])
        server.fetch_oref_history()                      # this poll is archived
        server._hist_week.update(t=0.0, rows=None)
        self._serve([("period=", (True, 200, API_EMPTY)), ("AlertsHistory.json", (True, 200, "[]"))])
        ok, _, txt = server.fetch_oref_history()
        rows = json.loads(txt)
        self.assertTrue(ok); self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["alertDate"], "2026-11-13 03:00:07")
        self.assertEqual(server._hist_status["source"], "archive")

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
