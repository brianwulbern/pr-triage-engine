"""3.0.37 (TICKET-1): an `ai-resolved-conflict` pull is held until a person other than the
forwarder signs off with an Approve review or an LGTM comment made after the head commit."""
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from prtriage import ai_review as A, config

HEAD = "2026-10-14T10:00:00Z"
BEFORE, AFTER, LATER = "2026-10-14T09:00:00Z", "2026-10-14T11:00:00Z", "2026-10-14T12:00:00Z"
MEMBERS = {"alice", "bob", "me"}


def _rev(login, state, at, utype="User"):
    return {"user": {"login": login, "type": utype}, "state": state, "submitted_at": at}


def _com(login, body, at, utype="User"):
    return {"user": {"login": login, "type": utype}, "body": body, "created_at": at}


def _lab(login, at, name=None):
    return {"event": "labeled", "actor": {"login": login, "type": "User"}, "created_at": at,
            "label": {"name": name or config.AI_CONFLICT_SIGNOFF_LABEL}}


def _sign(reviews=(), comments=(), events=(), head=HEAD, excluded=("lcihu", "me")):
    return A.find_signer(list(reviews), list(comments), list(events), head, excluded,
                         lambda l: l.lower() in MEMBERS)


class Lgtm(unittest.TestCase):
    def test_counts(self):
        for b in ("LGTM", "lgtm!", " LGTM, thanks ", "Looks good to me.", "LGTM 👍", "lgtm ty"):
            self.assertTrue(A.is_lgtm(b), b)

    def test_does_not_count(self):
        for b in ("LGTM once the test passes", "LGTM?", "not LGTM", "please LGTM this",
                  "<!-- prtriage-ai-review-hold -->\nLGTM", "", None):
            self.assertFalse(A.is_lgtm(b), b)


class Signer(unittest.TestCase):
    def test_approve_after_the_head(self):
        self.assertEqual(_sign([_rev("alice", "APPROVED", AFTER)]),
                         ("alice", "Approve review", AFTER))

    def test_lgtm_comment(self):
        self.assertEqual(_sign(comments=[_com("bob", "LGTM", AFTER)])[0], "bob")

    def test_label_added_by_a_writer(self):
        self.assertEqual(_sign(events=[_lab("alice", AFTER)])[0], "alice")

    def test_before_the_head_is_stale(self):
        self.assertIsNone(_sign([_rev("alice", "APPROVED", BEFORE)]))
        self.assertEqual(_sign([_rev("alice", "APPROVED", BEFORE)], head="")[0], "alice")

    def test_forwarder_and_bot_and_author_cannot_sign(self):
        self.assertIsNone(_sign([_rev("me", "APPROVED", AFTER)]))
        self.assertIsNone(_sign(comments=[_com("lcihu", "LGTM", AFTER)]))
        self.assertIsNone(_sign(comments=[_com("ci[bot]", "LGTM", AFTER, "Bot")]))
        self.assertIsNone(_sign(events=[_lab("me", AFTER)]))

    def test_needs_org_membership(self):
        self.assertIsNone(_sign([_rev("visitor", "APPROVED", AFTER)]))

    def test_later_request_changes_undoes_it(self):
        self.assertIsNone(_sign([_rev("alice", "APPROVED", AFTER),
                                 _rev("bob", "CHANGES_REQUESTED", LATER)]))
        self.assertEqual(_sign([_rev("bob", "CHANGES_REQUESTED", AFTER),
                                _rev("alice", "APPROVED", LATER)])[0], "alice")

    def test_approve_on_an_earlier_head_is_stale(self):
        r = dict(_rev("alice", "APPROVED", AFTER), commit_id="old")
        self.assertIsNone(A.find_signer([r], [], [], HEAD, ["me"], lambda l: True, "new"))
        r["commit_id"] = "new"
        self.assertEqual(A.find_signer([r], [], [], HEAD, ["me"], lambda l: True, "new")[0],
                         "alice")

    def test_force_push_after_signoff_is_stale(self):
        push = {"event": "head_ref_force_pushed", "created_at": LATER}
        self.assertIsNone(_sign(comments=[_com("bob", "LGTM", AFTER)], events=[push]))

    def test_unreadable_head_holds(self):
        self.assertIsNone(_sign([_rev("alice", "APPROVED", AFTER)], head=None))

    def test_other_labels_do_not_count(self):
        self.assertIsNone(_sign(events=[_lab("alice", AFTER, name="ai-resolved-conflict")]))


def _pull(labels, author="lcihu"):
    return {"labels": [{"name": l} for l in labels], "user": {"login": author},
            "head": {"sha": "abc"}, "base": {"ref": "release-1.x"}}


def _fake(pull, reviews=(), comments=(), events=(), commits=None):
    data = {
        "/pulls/1": pull,
        "/commits/abc": {"commit": {"committer": {"date": HEAD}}},
        "/pulls/1/reviews": list(reviews), "/issues/1/comments": list(comments),
        "/issues/1/events": list(events),
        "/pulls/1/commits": commits if commits is not None else [
            {"author": {"login": "alice", "type": "User"}},
            {"author": {"login": "lcihu", "type": "User"}}],
    }

    def get(url, token):
        path = url.split("product/x", 1)[1].split("?", 1)[0]
        return data.get(path)
    return get


class PullState(unittest.TestCase):
    def setUp(self):
        A._PERM.clear()
        A._STATES.clear()
        p = mock.patch("prtriage.comments._viewer_login", return_value="me")
        p.start()
        self.addCleanup(p.stop)
        b = mock.patch("prtriage.comments._ASSIGN_BOT_LOGINS", frozenset({"lcihu"}))
        b.start()
        self.addCleanup(b.stop)

    def _state(self, *a, **kw):
        with mock.patch.object(A, "_get", side_effect=_fake(*a, **kw)), \
             mock.patch.object(A, "_org_member", side_effect=lambda o, l, t: l in MEMBERS):
            return A.pull_state("product", "x", "1", "tok")

    def test_not_ai_is_not_held(self):
        st = self._state(_pull(["other"]))
        self.assertFalse(A.held(st))

    def test_ai_without_signoff_is_held_and_asks_the_human_author(self):
        st = self._state(_pull([config.AI_CONFLICT_LABEL]))
        self.assertTrue(A.held(st))
        self.assertEqual(st["authors"], ["alice"])
        self.assertIn("@alice", A.ask_text(st))
        self.assertNotIn(chr(0x2014), A.ask_text(st))

    def test_signed_off_is_released(self):
        st = self._state(_pull([config.AI_CONFLICT_LABEL]),
                         reviews=[_rev("bob", "APPROVED", AFTER)])
        self.assertFalse(A.held(st))

    def test_unreadable_pull_is_held(self):
        with mock.patch.object(A, "_get", return_value=None):
            self.assertTrue(A.held(A.pull_state("product", "x", "1", "tok")))

    def test_unreadable_reviews_hold(self):
        get = _fake(_pull([config.AI_CONFLICT_LABEL]), reviews=[_rev("bob", "APPROVED", AFTER)])
        with mock.patch.object(A, "_get",
                               side_effect=lambda u, t: None if "/reviews" in u else get(u, t)):
            self.assertTrue(A.held(A.pull_state("product", "x", "1", "tok")))

    def test_no_token_reads_nothing(self):
        self.assertFalse(A.held(A.pull_state("product", "x", "1", None)))


def _rec(n):
    return {"_audit": {"target": f"https://github.com/product/x/pull/{n}"}}


class BoardStep(unittest.TestCase):
    def _run(self, states, apply=True):
        A._STATES.clear()
        for n, st in states.items():
            A._STATES[f"product/x#{n}"] = st
        out = io.StringIO()
        with mock.patch("prtriage.github_writes._comment", return_value=True) as com, \
             mock.patch("prtriage.comments.add_pr_labels",
                        return_value={"ok": True}) as add, \
             mock.patch("prtriage.comments.remove_pr_label",
                        return_value={"ok": True}) as rm, redirect_stdout(out):
            A.board_step([_rec(n) for n in states], "tok", apply, "product/x", False, input)
        return out.getvalue(), com, add, rm

    def _st(self, **kw):
        base = {"ai": True, "readable": True, "signer": None, "stale": None, "authors": ["alice"],
                "noticed": False, "base": "release-1.x", "signed_label": False}
        base.update(kw)
        return base

    def test_asks_once(self):
        out, com, add, rm = self._run({"1": self._st(), "2": self._st(noticed=True)})
        self.assertEqual(com.call_count, 1)
        self.assertTrue(com.call_args[0][3].startswith(A._MARKER))
        self.assertIn("#2", out)

    def test_releases_with_the_signer_named(self):
        out, com, add, rm = self._run(
            {"1": self._st(signer=("bob", "Approve review", AFTER), noticed=True)})
        add.assert_called_once()
        self.assertIn("`bob`", com.call_args[0][3])

    def test_push_after_signoff_re_holds(self):
        out, com, add, rm = self._run({"1": self._st(
            signed_label=True, stale=("bob", "LGTM comment", BEFORE), noticed=True)})
        rm.assert_called_once()
        self.assertIn("pushed after `bob`", com.call_args[0][3])

    def test_dry_run_writes_nothing(self):
        out, com, add, rm = self._run({"1": self._st()}, apply=False)
        com.assert_not_called()
        self.assertIn("dry-run", out)

    def test_quiet_without_ai_pulls(self):
        out, com, _a, _r = self._run({"1": {"ai": False}})
        self.assertEqual(out, "")




if __name__ == "__main__":
    unittest.main()
