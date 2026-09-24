"""2.34.0 (#2) - the adaptive GitHub rate governor.

GitHub's SECONDARY (burst) limit is invisible to /rate_limit and only shows as a 403/429
+ Retry-After. The governor RIDES it: proactive spacing between requests, multiplicative
back-off on a throttling response (honoring Retry-After as a cooldown floor), additive
recovery on clean streaks, and a queryable inferred-pressure summary. A plain permission
403 must NOT back us off (GitHub reuses 403). Clock+sleep are injected so AIMD is tested
without wall-clock.
"""
import unittest

from prtriage import governor as gg


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept += s
        self.t += s


def _gov(fc, baseline=0.15):
    return gg.GitHubGovernor(clock=fc.now, sleep=fc.sleep, baseline=baseline, enabled=True)


class RateLimitDetection(unittest.TestCase):
    def test_403_with_retry_after_is_throttling(self):
        self.assertTrue(gg._is_rate_limited(403, {"Retry-After": "30"}))

    def test_429_with_zero_remaining_is_throttling(self):
        self.assertTrue(gg._is_rate_limited(429, {"X-RateLimit-Remaining": "0"}))

    def test_plain_permission_403_is_not_throttling(self):
        # the crucial false-positive guard: a scope/permission 403 must not back us off.
        self.assertFalse(gg._is_rate_limited(403, {}))
        self.assertFalse(gg._is_rate_limited(403, {"X-RateLimit-Remaining": "4998"}))

    def test_2xx_is_never_throttling(self):
        self.assertFalse(gg._is_rate_limited(200, {"Retry-After": "5"}))

    def test_retry_after_parsing(self):
        self.assertEqual(gg._retry_after_seconds({"Retry-After": "12"}), 12.0)
        self.assertIsNone(gg._retry_after_seconds({}))
        self.assertIsNone(gg._retry_after_seconds({"Retry-After": "soon"}))


class Pacing(unittest.TestCase):
    def test_first_request_does_not_wait(self):
        fc = FakeClock()
        self.assertEqual(_gov(fc).pace(), 0.0)
        self.assertEqual(fc.slept, 0.0)

    def test_back_to_back_requests_are_spaced(self):
        fc = FakeClock()
        g = _gov(fc, baseline=0.15)
        g.pace()                    # reserves t=0..0.15
        waited = g.pace()           # must wait until 0.15
        self.assertAlmostEqual(waited, 0.15, places=6)
        self.assertAlmostEqual(fc.slept, 0.15, places=6)

    def test_disabled_governor_never_sleeps(self):
        fc = FakeClock()
        g = gg.GitHubGovernor(clock=fc.now, sleep=fc.sleep, enabled=False)
        g.pace(); g.pace(); g.pace()
        self.assertEqual(fc.slept, 0.0)


class BackoffAndRecovery(unittest.TestCase):
    def test_rate_limit_widens_spacing_and_opens_cooldown(self):
        fc = FakeClock()
        g = _gov(fc, baseline=0.15)
        g.note_rate_limit(retry_after=5.0)
        snap = g.snapshot()
        self.assertAlmostEqual(snap["spacing"], 0.15 * gg.BACKOFF_FACTOR, places=6)
        self.assertTrue(snap["throttled"])
        self.assertEqual(snap["rate_limit_hits"], 1)
        # the next request must sit out the cooldown
        waited = g.pace()
        self.assertAlmostEqual(waited, 5.0, places=6)

    def test_retry_after_is_a_cooldown_floor(self):
        fc = FakeClock()
        g = _gov(fc)
        g.note_rate_limit(retry_after=None)  # no header → DEFAULT_COOLDOWN
        self.assertAlmostEqual(g.snapshot()["cooldown_remaining"], gg.DEFAULT_COOLDOWN, places=6)

    def test_spacing_is_capped(self):
        fc = FakeClock()
        g = _gov(fc, baseline=1.0)
        for _ in range(20):
            g.note_rate_limit(retry_after=0.0)
        self.assertLessEqual(g.snapshot()["spacing"], gg.MAX_SPACING)

    def test_clean_streak_recovers_spacing(self):
        fc = FakeClock()
        g = _gov(fc, baseline=0.15)
        g.note_rate_limit(retry_after=0.0)       # spacing → 0.6
        widened = g.snapshot()["spacing"]
        for _ in range(gg.RECOVER_EVERY):        # one recovery milestone
            g._note_clean()
        self.assertAlmostEqual(g.snapshot()["spacing"], widened - gg.RECOVER_STEP, places=6)

    def test_recovery_never_undershoots_baseline(self):
        fc = FakeClock()
        g = _gov(fc, baseline=0.15)
        g.note_rate_limit(retry_after=0.0)
        for _ in range(gg.RECOVER_EVERY * 20):
            g._note_clean()
        self.assertGreaterEqual(g.snapshot()["spacing"], 0.15)


class ObserveRouting(unittest.TestCase):
    class _Resp:
        def __init__(self, status, headers=None):
            self.status_code = status
            self.headers = headers or {}

    def test_throttling_response_backs_off(self):
        fc = FakeClock()
        g = _gov(fc)
        g.observe(self._Resp(403, {"Retry-After": "9"}))
        self.assertEqual(g.snapshot()["rate_limit_hits"], 1)

    def test_permission_403_does_not_back_off(self):
        fc = FakeClock()
        g = _gov(fc)
        g.observe(self._Resp(403, {}))
        self.assertEqual(g.snapshot()["rate_limit_hits"], 0)
        self.assertFalse(g.snapshot()["throttled"])

    def test_success_counts_clean(self):
        fc = FakeClock()
        g = _gov(fc)
        g.observe(self._Resp(200))
        self.assertEqual(g.snapshot()["clean_streak"], 1)

    def test_non_rate_4xx_5xx_is_neutral(self):
        fc = FakeClock()
        g = _gov(fc)
        g.observe(self._Resp(404))
        g.observe(self._Resp(500))
        s = g.snapshot()
        self.assertEqual(s["clean_streak"], 0)
        self.assertEqual(s["rate_limit_hits"], 0)

    def test_bare_int_and_none_tolerated(self):
        fc = FakeClock()
        g = _gov(fc)
        g.observe(200)          # bare status int
        g.observe(None)         # no response (e.g. transport error)
        self.assertEqual(g.snapshot()["clean_streak"], 1)


class Introspection(unittest.TestCase):
    def test_describe_empty_at_rest(self):
        self.assertEqual(_gov(FakeClock()).describe(), "")

    def test_describe_populated_when_throttled(self):
        g = _gov(FakeClock())
        g.note_rate_limit(retry_after=5.0)
        d = g.describe()
        self.assertIn("throttling", d)
        self.assertIn("spacing", d)

    def test_reset_restores_rest(self):
        g = _gov(FakeClock())
        g.note_rate_limit(retry_after=5.0)
        g.reset()
        self.assertFalse(g.snapshot()["throttled"])
        self.assertEqual(g.describe(), "")


class Singleton(unittest.TestCase):
    def test_governor_is_shared(self):
        self.assertIs(gg.governor(), gg.governor())

    def test_kill_switch_disables(self):
        import os
        old = os.environ.get("PRTRIAGE_NO_GH_GOVERNOR")
        gg._singleton = None
        os.environ["PRTRIAGE_NO_GH_GOVERNOR"] = "1"
        try:
            self.assertFalse(gg.governor()._enabled)
        finally:
            gg._singleton = None
            if old is None:
                os.environ.pop("PRTRIAGE_NO_GH_GOVERNOR", None)
            else:
                os.environ["PRTRIAGE_NO_GH_GOVERNOR"] = old


if __name__ == "__main__":
    unittest.main()
