"""2.99.0 - governor circuit breaker: fail fast under a persistently-hot secondary limit.

Field report: `prtriage pulls --me` ran 833s / 752 rate-limit hits before a Ctrl+C - the AIMD
backoff pinned spacing at MAX (8s) and the run ground through hundreds of calls, and the
2.92.0 between-PR abort never fired because the FIRST PR (dozens of throttled calls) never
completed. Now after CIRCUIT_TRIP_HITS CONSECUTIVE throttled responses (reset by any clean
one), pace() opens the breaker and RAISES GovernorTripped so the caller fails fast -
mid-PR / mid-enumeration, wherever the next GitHub call is. The board catches it (or the
between-PR crawl check sees `tripped`) and bails with a partial result. Auto-clears after a
cooldown so a fresh attempt can probe whether the limit lifted.
"""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from prtriage import governor as G
from prtriage import board_limits as pulls


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _gov():
    clk = _Clock()
    return G.GitHubGovernor(clock=clk, sleep=lambda s: None, enabled=True), clk


class Breaker(unittest.TestCase):
    def test_trips_after_consecutive_hits(self):
        g, _ = _gov()
        for _ in range(G.CIRCUIT_TRIP_HITS):
            g.note_rate_limit(0.0)
        self.assertTrue(g.snapshot()["tripped"])
        with self.assertRaises(G.GovernorTripped):
            g.pace()

    def test_below_threshold_does_not_trip(self):
        g, _ = _gov()
        for _ in range(G.CIRCUIT_TRIP_HITS - 1):
            g.note_rate_limit(0.0)
        self.assertFalse(g.snapshot()["tripped"])
        g.pace()   # must not raise

    def test_clean_response_resets_the_streak(self):
        g, _ = _gov()
        for _ in range(G.CIRCUIT_TRIP_HITS - 1):
            g.note_rate_limit(0.0)
        g._note_clean()               # a success breaks the consecutive streak
        g.note_rate_limit(0.0)        # +1 → nowhere near the threshold
        self.assertFalse(g.snapshot()["tripped"])
        g.pace()                      # no raise

    def test_auto_recovers_after_cooldown(self):
        g, clk = _gov()
        for _ in range(G.CIRCUIT_TRIP_HITS):
            g.note_rate_limit(0.0)
        with self.assertRaises(G.GovernorTripped):
            g.pace()
        clk.advance(G.CIRCUIT_COOLDOWN + 1)
        g.pace()                      # cooldown elapsed → probe allowed, no raise
        self.assertFalse(g.snapshot()["tripped"])

    def test_disabled_governor_never_trips(self):
        g = G.GitHubGovernor(clock=_Clock(), sleep=lambda s: None, enabled=False)
        for _ in range(50):
            g.note_rate_limit(0.0)
        self.assertEqual(g.pace(), 0.0)   # disabled → no pacing, no trip

    def test_env_override_threshold(self):
        with mock.patch.dict(os.environ, {"PRTRIAGE_GOVERNOR_TRIP_HITS": "3"}):
            g = G.GitHubGovernor(clock=_Clock(), sleep=lambda s: None, enabled=True)
            for _ in range(3):
                g.note_rate_limit(0.0)
            with self.assertRaises(G.GovernorTripped):
                g.pace()

    def test_describe_names_the_open_breaker(self):
        g, _ = _gov()
        for _ in range(G.CIRCUIT_TRIP_HITS):
            g.note_rate_limit(0.0)
        self.assertIn("circuit breaker OPEN", g.describe())




if __name__ == "__main__":
    unittest.main()
