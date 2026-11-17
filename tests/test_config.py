"""Local configuration: defaults, layering, clamping, and who may write."""
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dlconfig, brain, server


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._paths = (dlconfig.CONFIG_PATH, dlconfig.LOCAL_PATH)
        dlconfig.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        dlconfig.LOCAL_PATH = os.path.join(self.tmp, "config.local.json")

    def tearDown(self):
        dlconfig.CONFIG_PATH, dlconfig.LOCAL_PATH = self._paths

    def write(self, name, obj):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))


class TestDefaults(Tmp):
    def test_no_files_means_todays_behaviour(self):
        c = dlconfig.load()
        self.assertEqual(c["traffic"], {"minDelay": 10, "heavyDelay": 20, "monthlyCap": 4500,
                                        "dailyCap": 200, "rush": [[7, 9], [16, 19]]})
        self.assertEqual([m[0] for m in c["markets"]], [s for s, _ in server.MARKET_SYMBOLS])
        self.assertTrue(all(v is None for v in c["prefs"].values()))
        self.assertEqual(c["channels"]["custom"], [])
        self.assertEqual(c["display"]["fontScale"], 1.0)

    def test_broken_file_is_ignored_not_fatal(self):
        self.write("config.local.json", "{ this is not json")
        self.assertEqual(dlconfig.load()["traffic"]["minDelay"], 10)

    def test_local_overrides_shared(self):
        self.write("config.json", {"traffic": {"minDelay": 12, "heavyDelay": 30}})
        self.write("config.local.json", {"traffic": {"minDelay": 15}})
        t = dlconfig.load()["traffic"]
        self.assertEqual((t["minDelay"], t["heavyDelay"], t["monthlyCap"]), (15, 30, 4500))


class TestClamping(Tmp):
    def test_budget_can_never_pass_the_free_tier(self):
        self.write("config.local.json", {"traffic": {"monthlyCap": 999999, "dailyCap": 99999}})
        t = dlconfig.load()["traffic"]
        self.assertLessEqual(t["monthlyCap"], dlconfig.HARD_MONTHLY_CEILING)
        self.assertLess(dlconfig.HARD_MONTHLY_CEILING, dlconfig.FREE_TIER)
        self.assertLessEqual(t["dailyCap"], 400)

    def test_nonsense_values_fall_back(self):
        self.write("config.local.json", {"traffic": {"minDelay": "abc", "heavyDelay": 3, "rush": [[25, 3], "x"]},
                                         "markets": [["bad symbol!", "x"], ["", ""]],
                                         "display": {"fontScale": 40}})
        c = dlconfig.load()
        self.assertEqual(c["traffic"]["minDelay"], 10)
        self.assertGreater(c["traffic"]["heavyDelay"], c["traffic"]["minDelay"])
        self.assertEqual(c["traffic"]["rush"], [[7, 9], [16, 19]])
        self.assertEqual(len(c["markets"]), 6)
        self.assertEqual(c["display"]["fontScale"], 1.5)

    def test_custom_channels_and_feeds_must_be_http_urls(self):
        self.write("config.local.json", {
            "channels": {"custom": [{"name": "A", "src": "https://x.test/a.m3u8"},
                                    {"name": "B", "src": "javascript:alert(1)"},
                                    {"name": "", "src": "https://x.test"}]},
            "news": {"custom": [{"name": "F", "url": "https://x.test/rss", "region": "world"},
                                {"name": "G", "url": "file:///etc/passwd"}]}})
        c = dlconfig.load()
        self.assertEqual([(x["name"], x["type"]) for x in c["channels"]["custom"]], [("A", "hls")])
        self.assertEqual([(x["name"], x["region"]) for x in c["news"]["custom"]], [("F", "world")])


class TestHostileConfig(Tmp):
    def test_list_prefs_must_be_lists_of_ids(self):
        # a wrong type here used to throw in the page and kill the whole deck
        for bad in ("ch11", "{}", 5, True, "[1,2]", '{"a":1}', "[[\"x\"]]", "not json"):
            self.write("config.local.json", {"prefs": {"dl-tv": bad, "dl-news-off": bad}})
            p = dlconfig.load()["prefs"]
            self.assertIsNone(p["dl-tv"], bad); self.assertIsNone(p["dl-news-off"], bad)
        self.write("config.local.json", {"prefs": {"dl-tv": '["ch14","kan11"]'}})
        self.assertEqual(json.loads(dlconfig.load()["prefs"]["dl-tv"]), ["ch14", "kan11"])

    def test_scalar_prefs_are_typed(self):
        self.write("config.local.json", {"prefs": {"dl-lat": "abc", "dl-lng": "34.7", "dl-news-region": "mars",
                                                   "dl-news-relevant": "maybe", "dl-shelter-sec": "-5",
                                                   "dl-featured": "<img src=x onerror=1>", "dl-sound": "off"}})
        p = dlconfig.load()["prefs"]
        self.assertTrue(all(p[k] is None for k in ("dl-lat", "dl-lng", "dl-news-region", "dl-news-relevant",
                                                   "dl-shelter-sec", "dl-featured")))
        self.assertNotIn("dl-sound", p)             # device-local: never shared between screens

    def test_unmergeable_or_unencodable_files_fall_back_to_defaults(self):
        deep = "{" + '"a":{' * 3000 + "}" * 3000 + "}"
        self.write("config.local.json", deep)
        self.assertEqual(dlconfig.load()["traffic"]["minDelay"], 10)
        self.write("config.local.json", '{"prefs":{"dl-city":"\\ud800x"},"markets":[["^GSPC","\\ud800"]]}')
        json.dumps(dlconfig.load(), ensure_ascii=False).encode("utf-8")       # must not raise

    def test_custom_ids_do_not_shift_when_an_entry_is_deleted(self):
        two = {"channels": {"custom": [{"name": "A", "src": "https://x.test/a.m3u8"},
                                       {"name": "B", "src": "https://x.test/b.m3u8"}]}}
        self.write("config.local.json", two)
        b_id = dlconfig.load()["channels"]["custom"][1]["id"]
        two["channels"]["custom"].pop(0)
        self.write("config.local.json", two)
        self.assertEqual(dlconfig.load()["channels"]["custom"][0]["id"], b_id)

    def test_simultaneous_patches_both_land(self):
        import threading
        for i in range(25):
            dlconfig.save_local({})
            ts = [threading.Thread(target=dlconfig.patch_local, args=({"prefs": {"dl-city": "A"}},)),
                  threading.Thread(target=dlconfig.patch_local, args=({"traffic": {"minDelay": 14}},))]
            for t in ts: t.start()
            for t in ts: t.join()
            c = dlconfig.load()
            self.assertEqual((c["prefs"]["dl-city"], c["traffic"]["minDelay"]), ("A", 14))


class TestSave(Tmp):
    def test_patch_merges_and_survives_reload(self):
        self.assertTrue(dlconfig.save_local({"traffic": {"minDelay": 14}})[0])
        self.assertTrue(dlconfig.patch_local({"prefs": {"dl-city": "חולון"}})[0])
        c = dlconfig.load()
        self.assertEqual(c["traffic"]["minDelay"], 14)
        self.assertEqual(c["prefs"]["dl-city"], "חולון")

    def test_rejects_non_objects_and_huge_files(self):
        self.assertFalse(dlconfig.save_local([1, 2])[0])
        self.assertFalse(dlconfig.save_local({"x": "y" * (dlconfig.MAX_BYTES + 10)})[0])


class TestBrainReadsConfig(Tmp):
    def tearDown(self):
        Tmp.tearDown(self)
        brain.apply_config(force=True)          # back to the defaults

    def test_thresholds_follow_the_file(self):
        self.write("config.local.json", {"traffic": {"minDelay": 15, "heavyDelay": 40, "monthlyCap": 3000,
                                                     "rush": [[6, 10]]}})
        self.assertTrue(brain.apply_config(force=True))
        self.assertEqual(brain.level_of(12), "free")
        self.assertEqual(brain.level_of(15), "slow")
        self.assertEqual(brain.level_of(40), "heavy")
        self.assertEqual(brain.MONTHLY_CAP, 3000)
        self.assertLess(brain.TRIGGER_RESERVE, brain.MONTHLY_CAP)
        self.assertTrue(brain.is_rush(6)); self.assertFalse(brain.is_rush(17))

    def test_unchanged_file_is_not_reread(self):
        brain.apply_config(force=True)
        self.assertFalse(brain.apply_config())


class FakeHandler:
    def __init__(self, ip, host, origin=None):
        self.client_address = (ip, 5555)
        self.headers = {"Host": host}
        if origin:
            self.headers["Origin"] = origin


class TestWhoMayWrite(unittest.TestCase):
    def test_this_computer_only(self):
        ok = server._is_local_request
        self.assertTrue(ok(FakeHandler("127.0.0.1", "localhost:3100")))
        self.assertTrue(ok(FakeHandler("127.0.0.1", "127.0.0.1:3100", "http://localhost:3100")))
        self.assertFalse(ok(FakeHandler("192.168.1.20", "192.168.1.5:3100")))          # phone on the Wi-Fi
        self.assertFalse(ok(FakeHandler("127.0.0.1", "evil.example:3100")))            # DNS rebinding
        self.assertFalse(ok(FakeHandler("127.0.0.1", "localhost:3100", "https://evil.example")))  # cross-site post


if __name__ == "__main__":
    unittest.main()


class TestIdsFromOlderFiles(Tmp):
    def test_positional_ids_written_by_v76_are_carried_over(self):
        self.write("config.local.json", {
            "channels": {"custom": [{"name": "A", "src": "https://x.test/a.m3u8"}, {"name": "B", "src": "https://x.test/b.m3u8"}]},
            "news": {"custom": [{"name": "F", "url": "https://x.test/rss"}]},
            "prefs": {"dl-tv": '["ch14","custom2"]', "dl-featured": "custom1", "dl-news-off": '["cfeed1","tg_x"]'}})
        c = dlconfig.load()
        a, b = [x["id"] for x in c["channels"]["custom"]]
        self.assertEqual(json.loads(c["prefs"]["dl-tv"]), ["ch14", b])
        self.assertEqual(c["prefs"]["dl-featured"], a)
        self.assertEqual(json.loads(c["prefs"]["dl-news-off"]), [c["news"]["custom"][0]["id"], "tg_x"])

    def test_the_same_address_twice_is_one_channel(self):
        self.write("config.local.json", {"channels": {"custom": [{"name": "A", "src": "https://x.test/a.m3u8"},
                                                                  {"name": "A again", "src": "https://x.test/a.m3u8"}]}})
        self.assertEqual(len(dlconfig.load()["channels"]["custom"]), 1)
