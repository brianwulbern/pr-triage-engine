"""2.35.0 (#2 phase 2) - the governed() sleep-and-retry wrapper.

`governed(do_request)` is the phase-2 primitive rolled across the GitHub call sites: it
paces, calls the thunk, observes the response, and - the new part - RETRIES once on a
rate-limit response (the next pace() sleeps the cooldown, honoring Retry-After). When the
governor is disabled (tests / PRTRIAGE_NO_GH_GOVERNOR) it collapses to exactly one call so
call-counts and mocks are unaffected.
"""
import unittest

from prtriage import governor as gg


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


def _enabled_gov(fc):
    g = gg.GitHubGovernor(clock=fc.now, sleep=fc.sleep, baseline=0.15, enabled=True)
    return g


class GovernedDisabled(unittest.TestCase):
    def test_disabled_is_exactly_one_call_no_retry(self):
        g = gg.GitHubGovernor(enabled=False)
        gg._singleton = g
        try:
            calls = []
            rl = _Resp(403, {"Retry-After": "5", "X-RateLimit-Remaining": "0"})
            out = gg.governed(lambda: (calls.append(1), rl)[1])
            self.assertEqual(len(calls), 1)   # no retry even on a rate-limit, when disabled
            self.assertIs(out, rl)
        finally:
            gg._singleton = None


class GovernedEnabled(unittest.TestCase):
    def setUp(self):
        self.fc = FakeClock()
        self.g = _enabled_gov(self.fc)
        gg._singleton = self.g

    def tearDown(self):
        gg._singleton = None

    def test_success_is_one_call(self):
        calls = []
        out = gg.governed(lambda: (calls.append(1), _Resp(200))[1])
        self.assertEqual(len(calls), 1)
        self.assertEqual(out.status_code, 200)
        self.assertEqual(self.g.snapshot()["clean_streak"], 1)

    def test_persistent_rate_limit_retries_once_then_returns(self):
        calls = []
        rl = _Resp(429, {"Retry-After": "9", "X-RateLimit-Remaining": "0"})
        out = gg.governed(lambda: (calls.append(1), rl)[1])
        self.assertEqual(len(calls), 2)                 # initial + one retry (retries=1)
        self.assertIs(out, rl)                          # still returns the last response
        self.assertGreaterEqual(self.fc.t, 9.0)         # slept the Retry-After before retry
        self.assertEqual(self.g.snapshot()["rate_limit_hits"], 2)

    def test_rate_limit_then_success_returns_success(self):
        seq = [_Resp(403, {"Retry-After": "3", "X-RateLimit-Remaining": "0"}), _Resp(200)]
        out = gg.governed(lambda: seq.pop(0))
        self.assertEqual(out.status_code, 200)          # the retry succeeded
        self.assertEqual(self.g.snapshot()["rate_limit_hits"], 1)

    def test_permission_403_is_not_retried(self):
        calls = []
        out = gg.governed(lambda: (calls.append(1), _Resp(403))[1])   # no rate headers
        self.assertEqual(len(calls), 1)                 # a scope 403 is terminal, not retried
        self.assertFalse(self.g.snapshot()["throttled"])

    def test_more_retries_configurable(self):
        calls = []
        rl = _Resp(429, {"Retry-After": "1", "X-RateLimit-Remaining": "0"})
        gg.governed(lambda: (calls.append(1), rl)[1], retries=3)
        self.assertEqual(len(calls), 4)                 # initial + 3 retries


if __name__ == "__main__":
    unittest.main()
