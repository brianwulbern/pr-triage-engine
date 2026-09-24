"""2.237.0 - the board's rate-limit crawl-abort is remaining-aware and less trigger-happy.

The abort exists to avoid grinding through MANY remaining PRs at maxed spacing. It used to
fire identically whether 2 or 200 PRs remained, so a transient 2-hit secondary-limit burst
(spacing ~2.4s) bailed a nearly-complete board with primary budget untouched (a real 26/28
run bailed the last 2). Two changes: the trip spacing default goes 2.0 -> 4.0 (a 2-hit burst
no longer trips it), and the abort is skipped while fewer than _abort_min_remaining() PRs
remain (a short tail is always finished). A genuinely tripped circuit breaker still fails
fast via GovernorTripped regardless, so correctness on a real persistent throttle is intact.
"""
import unittest
from unittest import mock

from prtriage import board_limits as pulls


class AbortThresholds(unittest.TestCase):
    def test_default_abort_spacing_raised_to_4(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("PRTRIAGE_PULLS_ABORT_SPACING", None)
            self.assertEqual(pulls._abort_spacing(), 4.0)

    def test_abort_spacing_env_override(self):
        with mock.patch.dict("os.environ", {"PRTRIAGE_PULLS_ABORT_SPACING": "8"}):
            self.assertEqual(pulls._abort_spacing(), 8.0)

    def test_abort_spacing_bad_env_falls_back(self):
        with mock.patch.dict("os.environ", {"PRTRIAGE_PULLS_ABORT_SPACING": "nope"}):
            self.assertEqual(pulls._abort_spacing(), 4.0)

    def test_default_min_remaining_is_5(self):
        import os
        os.environ.pop("PRTRIAGE_PULLS_ABORT_MIN_REMAINING", None)
        self.assertEqual(pulls._abort_min_remaining(), 5)

    def test_min_remaining_env_override(self):
        with mock.patch.dict("os.environ", {"PRTRIAGE_PULLS_ABORT_MIN_REMAINING": "12"}):
            self.assertEqual(pulls._abort_min_remaining(), 12)

    def test_min_remaining_floor_is_1(self):
        with mock.patch.dict("os.environ", {"PRTRIAGE_PULLS_ABORT_MIN_REMAINING": "0"}):
            self.assertEqual(pulls._abort_min_remaining(), 1)


class ShouldCrawlAbort(unittest.TestCase):
    def test_short_tail_never_aborts_even_when_crawling(self):
        # governor IS crawling, but only 4 PRs remain (< default 5) -> finish them.
        with mock.patch.object(pulls, "_rate_limit_crawl", return_value="spacing 9.6s"):
            self.assertIsNone(pulls._should_crawl_abort(4))

    def test_long_tail_aborts_when_crawling(self):
        with mock.patch.object(pulls, "_rate_limit_crawl", return_value="spacing 9.6s"):
            self.assertEqual(pulls._should_crawl_abort(50), "spacing 9.6s")

    def test_at_threshold_aborts(self):
        with mock.patch.object(pulls, "_rate_limit_crawl", return_value="tripped"):
            self.assertEqual(pulls._should_crawl_abort(5), "tripped")   # remaining == min

    def test_no_pressure_never_aborts(self):
        # plenty remaining, but the governor is calm -> keep going.
        with mock.patch.object(pulls, "_rate_limit_crawl", return_value=None):
            self.assertIsNone(pulls._should_crawl_abort(100))

    def test_short_tail_does_not_even_probe_the_governor(self):
        # a short tail must skip the crawl check entirely (cheap + never bails).
        crawl = mock.Mock(return_value="spacing 9.6s")
        with mock.patch.object(pulls, "_rate_limit_crawl", crawl):
            self.assertIsNone(pulls._should_crawl_abort(2))
            crawl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
