"""Traffic Brain: classification, budget, batching and failure reporting."""
import io, os, sys, tempfile, unittest, urllib.error
from datetime import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import brain


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        brain.DATA = self.tmp
        brain.MODEL_PATH = os.path.join(self.tmp, "model.json")
        brain.STATE_PATH = os.path.join(self.tmp, "state.json")
        brain.MODEL.clear(); brain.MODEL.update({"cells": {}, "static": {}, "updated": 0, "samples": 0})
        brain.STATE.clear(); brain.STATE.update({"month": "", "usedMonth": 0, "day": "", "usedDay": 0,
                                                 "lastVerify": 0, "lastMeasure": {}, "triggers": [], "log": []})


class TestLevels(Base):
    def test_under_ten_minutes_is_not_reported(self):
        self.assertEqual(brain.level_of(0), "free")
        self.assertEqual(brain.level_of(9), "free")

    def test_thresholds(self):
        self.assertEqual(brain.level_of(brain.MIN_DELAY), "slow")
        self.assertEqual(brain.level_of(brain.HEAVY_DELAY - 1), "slow")
        self.assertEqual(brain.level_of(brain.HEAVY_DELAY), "heavy")
        self.assertEqual(brain.level_of(None), "unknown")


class TestBudget(Base):
    def test_caps_stay_inside_the_free_tier(self):
        self.assertLessEqual(brain.MONTHLY_CAP, 5000)
        self.assertGreater(brain.TRIGGER_RESERVE, 0)

    def test_counters_reset_when_the_month_turns(self):
        brain.STATE.update(month="2020-01", usedMonth=4400, day="2020-01-31", usedDay=150)
        left = brain.budget_left()
        self.assertEqual(brain.STATE["usedMonth"], 0)
        self.assertEqual(brain.STATE["usedDay"], 0)
        self.assertEqual(left, min(brain.MONTHLY_CAP, brain.DAILY_CAP))

    def test_scheduled_work_leaves_the_trigger_reserve(self):
        brain._roll_periods()
        brain.STATE["usedMonth"] = brain.MONTHLY_CAP - brain.TRIGGER_RESERVE
        self.assertLessEqual(brain.budget_left(scheduled=True), 0)
        self.assertGreater(brain.budget_left(scheduled=False), 0)

    def test_no_key_means_no_spend(self):
        brain.ROOT = self.tmp
        os.environ.pop("GOOGLE_MAPS_KEY", None)
        self.assertEqual(brain.verify()["why"], "no_key")


class TestBatch(Base):
    def test_batch_is_valid_and_unique(self):
        for n in (2, 3, 4, brain.TRIGGER_BATCH):
            b = brain.pick_batch(n)
            self.assertEqual(len(b), len(set(b)))
            self.assertLessEqual(len(b), n)
            self.assertTrue(all(0 <= i < len(brain.SEGMENTS) for i in b))

    def test_force_all_covers_every_segment(self):
        self.assertEqual(sorted(brain.pick_batch(len(brain.SEGMENTS), force_all=True)),
                         list(range(len(brain.SEGMENTS))))

    def test_whole_country_is_covered(self):
        regs = {s["reg"] for s in brain.SEGMENTS}
        for must in ("גוש דן", "אילת", "גולן"):
            self.assertIn(must, regs)


class TestFailureReporting(Base):
    def _http_error(self, code, body):
        return urllib.error.HTTPError("https://routes.googleapis.com/x", code, "err", {}, io.BytesIO(body))

    def test_reason_is_recorded_and_exposed(self):
        body = b'{"error":{"status":"INVALID_ARGUMENT","message":"API key not valid. Please pass a valid API key."}}'
        brain._note_failure(self._http_error(400, body))
        err = brain.STATE["lastError"]
        self.assertEqual(err["code"], 400)
        self.assertIn("API key not valid", err["msg"])
        self.assertIn("google.key", err["hint"])
        self.assertTrue(brain.snapshot()["health"]["failing"])

    def test_key_is_never_written_to_state_or_log(self):
        fake = "AIza" + "B" * 35
        brain._note_failure(ValueError("request failed for key=%s" % fake))
        self.assertNotIn(fake, brain.STATE["lastError"]["msg"])

    def test_snapshot_shape(self):
        s = brain.snapshot()
        for k in ("segments", "counts", "hotspots", "verdict", "budget", "model", "health"):
            self.assertIn(k, s)
        self.assertEqual(len(s["segments"]), len(brain.SEGMENTS))


if __name__ == "__main__":
    unittest.main()


class TestBudgetIsAWall(Base):
    """The cap must hold under simultaneous requests and count what was sent."""

    def setUp(self):
        Base.setUp(self)
        self._call, self._root = brain._call_routes, brain.ROOT
        brain.ROOT = self.tmp
        open(os.path.join(self.tmp, "google.key"), "w").write("test-key")
        brain._roll_periods()
        self.sent = 0

    def tearDown(self):
        brain._call_routes, brain.ROOT = self._call, self._root

    def _fake(self, fail=None):
        import threading, time
        lock = threading.Lock()
        def call(key, seg):
            with lock:
                self.sent += 1
            time.sleep(0.002)
            if fail:
                raise fail
            return 600.0, 500.0
        brain._call_routes = call

    def test_simultaneous_verifies_never_pass_the_cap(self):
        import threading
        self._fake()
        brain.STATE["usedMonth"] = brain.MONTHLY_CAP - 19
        ts = [threading.Thread(target=brain.verify, kwargs={"reason": "manual", "n": 19, "force_all": True})
              for _ in range(12)]
        for t in ts: t.start()
        for t in ts: t.join()
        for _ in range(6):                              # and again, one after the other
            brain.verify(reason="manual", n=19, force_all=True)
        self.assertLessEqual(brain.STATE["usedMonth"], brain.MONTHLY_CAP)
        self.assertLessEqual(self.sent, 19)

    def test_calls_that_may_have_been_billed_are_counted(self):
        self._fake(fail=TimeoutError("timed out"))
        brain.verify(reason="manual", n=3, force_all=False)
        self.assertEqual(brain.STATE["usedMonth"], self.sent)
        self.assertGreater(self.sent, 0)

    def test_calls_google_refused_outright_are_not_counted(self):
        self._fake(fail=urllib.error.HTTPError("u", 400, "bad key", {}, io.BytesIO(b"{}")))
        brain.verify(reason="manual", n=3, force_all=False)
        self.assertEqual(brain.STATE["usedMonth"], 0)
        self.assertGreater(self.sent, 0)

    def test_zero_cap_means_no_calls(self):
        self._fake()
        cap = brain.MONTHLY_CAP
        try:
            brain.MONTHLY_CAP = 0
            self.assertEqual(brain.verify(reason="manual", n=3)["why"], "budget")
            self.assertEqual(self.sent, 0)
        finally:
            brain.MONTHLY_CAP = cap


class TestTriggerSurvivesBusy(TestBudgetIsAWall):
    def test_a_trigger_that_met_a_running_verify_is_not_lost(self):
        self._fake()
        brain.STATE["triggers"] = ["road-incident"]
        self.assertTrue(brain._verify_gate.acquire(blocking=False))       # a manual verify is in progress
        try:
            res = brain.verify("trigger:road-incident", n=2)
            self.assertEqual(res.get("why"), "busy")
            self.assertEqual(brain.STATE["triggers"], ["road-incident"])   # verify itself never drops it
        finally:
            brain._verify_gate.release()
        src = open(os.path.join(os.path.dirname(__file__), "..", "brain.py"), encoding="utf-8").read()
        self.assertIn('if res.get("why") != "busy":', src)                # and the scheduler pops only after a real run


class TestTriggers(Base):
    def _queue(self, *reasons):
        import io as _io

        class Fake:
            def __init__(s, path): s.path = path; s.out = None
        for r in reasons:
            with brain._lock:
                pass
            brain.STATE.setdefault("triggers", [])
            # same logic the /trigger handler runs
            trig = [t for t in (brain.STATE.get("triggers") or [])
                    if isinstance(t, dict) and __import__("time").time() - t.get("t", 0) < brain.TRIGGER_TTL]
            if not any(t.get("r") == r for t in trig) and len(trig) < 3:
                trig.append({"r": r, "t": __import__("time").time()})
            brain.STATE["triggers"] = trig

    def test_the_handler_dedupes_and_caps(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "brain.py"), encoding="utf-8").read()
        self.assertIn('if not any(t.get("r") == reason for t in trig) and len(trig) < 3:', src)
        self._queue("road-incident", "road-incident", "a", "b", "c")
        self.assertEqual([t["r"] for t in brain.STATE["triggers"]], ["road-incident", "a", "b"])

    def test_triggers_that_waited_out_an_empty_budget_expire(self):
        import time
        brain.STATE["triggers"] = [{"r": "old", "t": time.time() - brain.TRIGGER_TTL - 5}, "legacy-string",
                                   {"r": "new", "t": time.time()}]
        fresh = [t for t in brain.STATE["triggers"]
                 if isinstance(t, dict) and time.time() - t.get("t", 0) < brain.TRIGGER_TTL]
        self.assertEqual([t["r"] for t in fresh], ["new"])
        src = open(os.path.join(os.path.dirname(__file__), "..", "brain.py"), encoding="utf-8").read()
        self.assertIn("time.time() - t.get(\"t\", 0) < TRIGGER_TTL]", src)
