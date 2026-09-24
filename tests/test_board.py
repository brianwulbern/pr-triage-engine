"""The concurrent board: returns records in input order, finishes a short tail even when the
governor is crawling, and bails a long queue only on real pressure."""
import unittest
from unittest import mock

from prtriage import board, governor
from prtriage.verdict import Run, decide


def _triage(n):
    return {"pr": n, "verdict": decide(Run(n, []))}


class RunBoard(unittest.TestCase):
    def test_records_in_input_order(self):
        recs, aborted = board.run_board([3, 1, 2], _triage, concurrency=3)
        self.assertFalse(aborted)
        self.assertEqual([r["pr"] for r in recs], [3, 1, 2])

    def test_short_tail_finishes_despite_crawl(self):
        with mock.patch("prtriage.board_limits._rate_limit_crawl", return_value="crawl"):
            recs, aborted = board.run_board(list(range(4)), _triage, concurrency=1)
        self.assertFalse(aborted)
        self.assertEqual(len(recs), 4)

    def test_long_queue_bails_on_crawl(self):
        with mock.patch("prtriage.board_limits._rate_limit_crawl", return_value="crawl"):
            recs, aborted = board.run_board(list(range(30)), _triage, concurrency=1)
        self.assertTrue(aborted)
        self.assertLess(len(recs), 30)

    def test_tripped_breaker_aborts(self):
        def boom(n):
            if n == 2:
                raise governor.GovernorTripped("hot")
            return _triage(n)
        recs, aborted = board.run_board([1, 2, 3], boom, concurrency=1)
        self.assertTrue(aborted)

    def test_render_groups_by_verdict(self):
        out = board.render_board([_triage(1), _triage(2)])
        self.assertIn("FORWARD-SAFE (2)", out)


if __name__ == "__main__":
    unittest.main()
