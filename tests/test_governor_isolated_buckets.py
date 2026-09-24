"""2.238.0 - a code-search bucket exhaustion cools ONLY code search, not all of GitHub.

Field (the secondary-limit JSONL, 2026-09-23/24): every throttle behind two `prtriage pulls` bails
was a `/search/code` 403 with X-RateLimit-Resource=code_search and remaining=0. That bucket is
separate from core and tiny (10/min). The governor treated it as global pressure, backing
EVERY GitHub call off 0.15 → 0.6 → 2.4s (the immediate governed() retry made it two hits, 2s
apart), which tripped the board abort with 3000+ core requests unspent. It also cached the
rate-limited miss, so one exhaustion became a permanent "not found" for that test.

Now: an isolated-bucket hit gets its own cooldown until X-RateLimit-Reset, leaves spacing and
the circuit breaker alone, is not retried in place, and code search skips (uncached) while
it cools.
"""
import time
import unittest
from unittest import mock

from prtriage import governor
from prtriage.governor import GitHubGovernor


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _gov():
    clock = _Clock()
    return GitHubGovernor(clock=clock, sleep=lambda s: None, enabled=True), clock


def _search_403(reset_in=30.0):
    return mock.Mock(status_code=403, text="API rate limit exceeded", url=None,
                     headers={"X-RateLimit-Resource": "code_search",
                              "X-RateLimit-Remaining": "0",
                              "X-RateLimit-Reset": str(int(time.time() + reset_in))})


def _core_403():
    return mock.Mock(status_code=403, text="rate limited", url=None,
                     headers={"X-RateLimit-Resource": "core",
                              "X-RateLimit-Remaining": "0"})


class IsolatedBucketGovernor(unittest.TestCase):
    def test_search_hit_does_not_widen_global_spacing(self):
        g, _ = _gov()
        base = g.snapshot()["spacing"]
        g.observe(_search_403())
        s = g.snapshot()
        self.assertEqual(s["spacing"], base)          # no global backoff
        self.assertEqual(s["rate_limit_hits"], 0)      # not counted as global pressure
        self.assertEqual(s["consecutive_hits"], 0)
        self.assertFalse(s["throttled"])
        self.assertEqual(s["resource_hits"], {"code_search": 1})

    def test_search_hit_cools_only_its_bucket_until_reset(self):
        g, _ = _gov()
        g.observe(_search_403(reset_in=30))
        self.assertGreater(g.resource_cooling("code_search"), 25)
        self.assertLessEqual(g.resource_cooling("code_search"), 31)
        self.assertEqual(g.resource_cooling("core"), 0.0)

    def test_cooldown_lifts_when_clock_passes_reset(self):
        g, clock = _gov()
        g.observe(_search_403(reset_in=30))
        clock.t += 31
        self.assertEqual(g.resource_cooling("code_search"), 0.0)

    def test_cooldown_bounds(self):
        g, _ = _gov()
        g.observe(_search_403(reset_in=-100))            # reset already past -> floor 1s
        self.assertAlmostEqual(g.resource_cooling("code_search"), 1.0, delta=0.01)
        g2, _ = _gov()
        g2.observe(_search_403(reset_in=1000))           # absurd reset -> capped 65s
        self.assertAlmostEqual(g2.resource_cooling("code_search"), 65.0, delta=0.01)

    def test_no_reset_header_uses_default(self):
        g, _ = _gov()
        r = mock.Mock(status_code=403, text="", url=None,
                      headers={"X-RateLimit-Resource": "code_search",
                               "X-RateLimit-Remaining": "0"})
        g.observe(r)
        self.assertAlmostEqual(g.resource_cooling("code_search"),
                               governor.RESOURCE_DEFAULT_COOLDOWN, delta=0.01)

    def test_many_search_hits_never_trip_the_breaker(self):
        g, _ = _gov()
        for _ in range(20):
            g.observe(_search_403())
        self.assertFalse(g.snapshot()["tripped"])
        g.pace()                                          # must not raise GovernorTripped

    def test_core_exhaustion_still_backs_off_globally(self):
        g, _ = _gov()
        base = g.snapshot()["spacing"]
        g.observe(_core_403())
        s = g.snapshot()
        self.assertGreater(s["spacing"], base)
        self.assertEqual(s["rate_limit_hits"], 1)

    def test_reset_clears_bucket_state(self):
        g, _ = _gov()
        g.observe(_search_403())
        g.reset()
        self.assertEqual(g.resource_cooling("code_search"), 0.0)
        self.assertEqual(g.snapshot()["resource_hits"], {})


class GovernedNoInPlaceRetry(unittest.TestCase):
    def setUp(self):
        self.g, _ = _gov()
        governor._singleton = self.g            # conftest resets it after the test

    def test_search_exhaustion_not_retried(self):
        calls = []
        def _do():
            calls.append(1)
            return _search_403()
        governor.governed(_do)
        self.assertEqual(len(calls), 1)                 # the old path made a 2nd, doomed call

    def test_core_rate_limit_still_retried(self):
        calls = []
        def _do():
            calls.append(1)
            return _core_403()
        governor.governed(_do)
        self.assertEqual(len(calls), 2)                 # retries=1, unchanged


class IsolatedLimitHelper(unittest.TestCase):
    def test_detects_search_exhaustion(self):
        self.assertEqual(governor.isolated_limit(_search_403()), "code_search")

    def test_ignores_a_clean_search_response(self):
        ok = mock.Mock(status_code=200, headers={"X-RateLimit-Resource": "code_search",
                                                 "X-RateLimit-Remaining": "9"})
        self.assertIsNone(governor.isolated_limit(ok))

    def test_ignores_core_and_mock_headers(self):
        self.assertIsNone(governor.isolated_limit(_core_403()))
        self.assertIsNone(governor.isolated_limit(
            mock.Mock(status_code=403, headers=mock.Mock())))




if __name__ == "__main__":
    unittest.main()
