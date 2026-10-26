import datetime
import unittest
from unittest import mock

from prtriage import ai_review as A

MON = datetime.date(2026, 10, 26)


def _rec(num, landed=False):
    v = {"landing": {"state": "landed"}} if landed else {}
    return {"_audit": {"target": f"https://github.com/example-org/product/pull/{num}"},
            "result": {"verdict": v}}


def _st(authors=("alice",), noticed=True, asked="", reminded=""):
    return {"ai": True, "readable": True, "signer": None, "signed_label": False,
            "authors": list(authors), "noticed": noticed, "asked_at": asked,
            "reminded_at": reminded, "requests": {}}


class Workdays(unittest.TestCase):
    def test_skips_weekends(self):
        self.assertEqual(A.workdays_between("2026-10-23T14:25:00Z", MON), 1)
        self.assertEqual(A.workdays_between("2026-10-20T10:00:00Z", MON), 4)
        self.assertEqual(A.workdays_between("", MON), 0)


class Classify(unittest.TestCase):
    def _run(self, states, records):
        A._STATES.clear()
        with mock.patch.object(A, "_state", side_effect=lambda o, r, n, t: states[n]):
            return A.classify(records, "tok", "example-org/product", now=MON)

    def test_buckets(self):
        states = {"1": _st(noticed=False), "2": _st(asked="2026-10-20T10:00:00Z"),
                  "3": _st(asked="2026-10-23T10:00:00Z"),
                  "4": _st(asked="2026-10-15T10:00:00Z", reminded="2026-10-20T10:00:00Z"),
                  "5": _st(noticed=False)}
        c = self._run(states, [_rec(1), _rec(2), _rec(3), _rec(4), _rec(5, landed=True)])
        got = {k: [row[2] for row in v] for k, v in c.items() if v}
        self.assertEqual(got, {"ask": ["1"], "remind": ["2"], "waiting": ["3"],
                               "overdue": ["4"], "landed": ["5"]})


class Grouping(unittest.TestCase):
    def test_one_mention_per_author_set(self):
        rows = [("o", "r", n, _st(authors=a)) for n, a in
                (("103", ("dev-q",)), ("101", ("dev-q",)), ("102", ("Dev-Q",)),
                 ("104", ("dev-x",)))]
        groups = A.by_authors(rows)
        self.assertEqual(sorted([r[2] for r in g] for g in groups),
                         [["101", "102", "103"], ["104"]])

    def test_board_posts_full_ask_once_and_pointers(self):
        states = {n: _st(noticed=False, authors=("dev-q",)) for n in ("101", "102")}
        posted = []
        A._STATES.clear()
        with mock.patch.object(A, "_state", side_effect=lambda o, r, n, t: states[n]), \
                mock.patch("prtriage.github_writes._comment",
                           side_effect=lambda t, repo, n, body: posted.append((n, body)) or True):
            A.board_step([_rec(101), _rec(102)], "tok", True,
                         "example-org/product", False, lambda q: "")
        self.assertEqual([n for n, _ in posted], ["101", "102"])
        self.assertIn("@dev-q", posted[0][1])
        self.assertIn("#102", posted[0][1])
        self.assertNotIn("@dev-q", posted[1][1])
        self.assertTrue(posted[1][1].startswith(A._MARKER))




if __name__ == "__main__":
    unittest.main()
