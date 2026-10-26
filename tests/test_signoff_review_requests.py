"""3.0.42: the backport bot asks the original authors with a GitHub review request. prtriage reads it, doesn't post
its own ask on top, and shows a removed request or a comment-only review on the board. Anyone in
the org can still sign off, and only a sign-off releases the hold."""
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from prtriage import ai_review as A, config

from tests.test_signoff_hold import (
    AFTER, BEFORE, MEMBERS, _fake, _pull, _rec, _rev)

REQ_AT = "2026-10-14T10:30:00Z"


def _req(who, at=REQ_AT, actor="lcihu", removed=False):
    return {"event": "review_request_removed" if removed else "review_requested",
            "created_at": at, "actor": {"login": actor, "type": "User"},
            "requested_reviewer": {"login": who, "type": "User"}}


def _requested(pull, *who):
    pull = dict(pull)
    pull["requested_reviewers"] = [{"login": w} for w in who]
    return pull


class Requests(unittest.TestCase):
    def test_bot_request_to_the_author(self):
        rq = A.review_requests(_requested({}, "alice"), [_req("alice")], [], ["alice"])
        self.assertEqual(rq["asked"], {"alice": REQ_AT})
        self.assertEqual(rq["removed"], [])

    def test_request_to_someone_else_is_not_an_ask(self):
        rq = A.review_requests(_requested({}, "carol"), [_req("carol")], [], ["alice"])
        self.assertEqual(rq["asked"], {})

    def test_removed_request_is_reported(self):
        rq = A.review_requests({}, [_req("alice"), _req("alice", AFTER, "bob", True)], [],
                               ["alice"])
        self.assertEqual(rq["removed"], [("alice", "bob")])

    def test_comment_only_review_is_reported(self):
        rq = A.review_requests({}, [_req("alice")], [_rev("alice", "COMMENTED", AFTER)],
                               ["alice"])
        self.assertEqual(rq["commented"], ["alice"])

    def test_approval_is_not_comment_only(self):
        rq = A.review_requests({}, [_req("alice")], [_rev("alice", "COMMENTED", AFTER),
                                                     _rev("alice", "APPROVED", AFTER)],
                               ["alice"])
        self.assertEqual(rq["commented"], [])


class PullState(unittest.TestCase):
    def setUp(self):
        A._PERM.clear()
        A._STATES.clear()
        for target, value in (("prtriage.comments._viewer_login", "me"),
                              ("prtriage.comments._ASSIGN_BOT_LOGINS",
                               frozenset({"lcihu"}))):
            p = mock.patch(target, value) if isinstance(value, frozenset) else \
                mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _state(self, *a, **kw):
        with mock.patch.object(A, "_get", side_effect=_fake(*a, **kw)), \
             mock.patch.object(A, "_org_member", side_effect=lambda o, l, t: l in MEMBERS):
            return A.pull_state("product", "x", "1", "tok")

    def test_removed_request_still_holds(self):
        st = self._state(_pull([config.AI_CONFLICT_LABEL]),
                         events=[_req("alice"), _req("alice", AFTER, "bob", True)])
        self.assertTrue(A.held(st))
        self.assertEqual(st["requests"]["removed"], [("alice", "bob")])

    def test_comment_only_review_still_holds(self):
        st = self._state(_pull([config.AI_CONFLICT_LABEL]),
                         reviews=[_rev("alice", "COMMENTED", AFTER)], events=[_req("alice")])
        self.assertTrue(A.held(st))

    def test_anyone_in_the_org_can_still_sign_off(self):
        st = self._state(_requested(_pull([config.AI_CONFLICT_LABEL]), "alice"),
                         reviews=[_rev("bob", "APPROVED", AFTER)], events=[_req("alice")])
        self.assertFalse(A.held(st))


class BoardStep(unittest.TestCase):
    def _run(self, st):
        A._STATES.clear()
        A._STATES["product/x#1"] = st
        out = io.StringIO()
        with mock.patch("prtriage.github_writes._comment", return_value=True) as com, \
             mock.patch("prtriage.comments.add_pr_labels", return_value={"ok": True}), \
             mock.patch("prtriage.comments.remove_pr_label", return_value={"ok": True}), \
             redirect_stdout(out):
            A.board_step([_rec("1")], "tok", True, "product/x", False, input)
        return out.getvalue(), com

    def _st(self, requests):
        return {"ai": True, "readable": True, "signer": None, "stale": None,
                "authors": ["alice"], "noticed": False, "base": "release-1.x",
                "signed_label": False, "requests": requests}

    def test_no_comment_when_bot_requested_the_author(self):
        out, com = self._run(self._st({"asked": {"alice": REQ_AT}, "removed": [],
                                       "commented": []}))
        com.assert_not_called()
        self.assertIn("review requested from @alice since 2026-10-14", out)

    def test_comment_when_nobody_was_requested(self):
        out, com = self._run(self._st({"asked": {}, "removed": [], "commented": []}))
        com.assert_called_once()

    def test_board_names_the_removal_and_the_comment(self):
        out, _com = self._run(self._st({"asked": {"alice": REQ_AT},
                                        "removed": [("alice", "bob")],
                                        "commented": []}))
        self.assertIn("@bob removed the request to @alice, still held", out)
        out, _com = self._run(self._st({"asked": {"alice": REQ_AT}, "removed": [],
                                        "commented": ["alice"]}))
        self.assertIn("@alice commented without approving", out)


if __name__ == "__main__":
    unittest.main()
