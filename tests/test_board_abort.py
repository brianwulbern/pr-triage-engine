"""2.92.0 - prtriage pulls bails early when GitHub's secondary rate limit trips a crawl.

Field report: `prtriage pulls --me` ran ~40 min and hit 28 rate-limit hits (governor spacing
7.8s vs 0.15s baseline) before the user Ctrl+C'd. When the governor has backed off into a
crawl, every remaining PR is dozens of calls × multi-second waits - better to bail with a
partial board than grind. _run_targets now checks the governor after each PR and, with
abort_on_throttle, returns (partial_records, aborted=True). --no-rate-abort opts out.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from prtriage import board_limits as pulls


def _rec(t):
    return {"_audit": {"target": t}, "result": {"verdict": {"orb": "🟢", "action": "forward"}}}


class AbortSpacing(unittest.TestCase):
    def test_env_override(self):
        with mock.patch.dict("os.environ", {"PRTRIAGE_PULLS_ABORT_SPACING": "5.5"}):
            self.assertEqual(pulls._abort_spacing(), 5.5)

    def test_bad_env_falls_back(self):
        with mock.patch.dict("os.environ", {"PRTRIAGE_PULLS_ABORT_SPACING": "nonsense"}):
            self.assertEqual(pulls._abort_spacing(), 4.0)


class RateLimitCrawl(unittest.TestCase):
    def _gov(self, spacing, hits=0):
        g = mock.Mock()
        g.snapshot.return_value = {"spacing": spacing, "rate_limit_hits": hits}
        g.describe.return_value = f"🐢 spacing {spacing}s"
        return g

    def test_none_below_threshold(self):
        with mock.patch("prtriage.governor.governor",
                        return_value=self._gov(0.6)):
            self.assertIsNone(pulls._rate_limit_crawl())

    def test_string_at_or_above_threshold(self):
        # 2.237.0: threshold is now 4.0, so a 3rd-hit spacing (9.6s) trips it.
        with mock.patch("prtriage.governor.governor",
                        return_value=self._gov(9.6, hits=7)):
            out = pulls._rate_limit_crawl()
            self.assertIsNotNone(out)
            self.assertIn("spacing", out)






if __name__ == "__main__":
    unittest.main()
