"""2.156.0 - --gh-post keeps a PAPER TRAIL: edit in place while the verdict holds,
post a NEW comment when the conclusion changes.

Before, prtriage kept exactly one verdict comment and EDITED it in place on every re-run
(anti-spam). But when a failing PR's conclusion changes - the dev fixes it (🛑→🟢), or
a prtriage upgrade re-judges the same run - overwriting erased the prior verdict, losing the
history a reviewer (and prtriage's own before/after) wants to see.

Now each rendered body carries a hidden `<!-- prtriage-sig: <orb>:<code> -->` (the stable
conclusion identity). upsert compares the new body's sig against the LATEST prtriage comment's:
  • same sig  → edit that comment in place (unchanged verdict, no thread spam).
  • diff sig  → POST A NEW comment ("reposted"), leaving the prior as a paper trail.
  • sig missing on either side → edit in place (conservative: never spam on doubt).
The finder returns the NEWEST prtriage comment (a chain now exists), so a re-run updates the
current-conclusion comment, not the oldest.
"""
import unittest
from unittest import mock

from prtriage import comments

PR = "https://github.com/example-org/product/pull/1234"
MARKER = comments._VERDICT_MARKER


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


def _c(cid, sig=None, extra="old"):
    sigln = f"\n<!-- prtriage-sig: {sig} -->" if sig else ""
    return {"id": cid, "body": f"{MARKER}\n{extra}{sigln}"}




class LatestWins(unittest.TestCase):
    def test_returns_newest_trt_comment(self):
        page = [_c(10, "🛑:a"), {"id": 15, "body": "someone else"}, _c(20, "🟢:b")]
        with mock.patch.object(comments.requests, "get", return_value=_Resp(200, page)):
            got = comments._find_latest_verdict_comment(
                "product", "product", "1234", {}, timeout=5)
        self.assertEqual(got[0], 20)                       # the newest match
        self.assertEqual(comments._verdict_sig(got[1]), "🟢:b")


class UpsertBehavior(unittest.TestCase):
    def _run(self, page, new_body):
        get = mock.Mock(return_value=_Resp(200, page))
        post = mock.Mock(return_value=_Resp(201, {"html_url": f"{PR}#issuecomment-new"}))
        patch = mock.Mock(return_value=_Resp(200, {"html_url": f"{PR}#issuecomment-edit"}))
        with mock.patch.object(comments.requests, "get", get), \
             mock.patch.object(comments.requests, "post", post), \
             mock.patch.object(comments.requests, "patch", patch):
            res = comments.upsert_pr_verdict_comment(PR, new_body, token="tok")
        return res, post, patch

    def test_same_sig_edits_in_place(self):
        res, post, patch = self._run(
            [_c(222, "🛑:suppressed_diff_reproduced")],
            "new body\n<!-- prtriage-sig: 🛑:suppressed_diff_reproduced -->")
        self.assertEqual(res["action"], "updated")
        patch.assert_called_once()
        post.assert_not_called()

    def test_changed_sig_posts_new_comment(self):
        # the dev fixed it: 🛑 → 🟢 → a NEW comment, the old one stays as history
        res, post, patch = self._run(
            [_c(222, "🛑:suppressed_diff_reproduced")],
            "clean now\n<!-- prtriage-sig: 🟢:clean_forward -->")
        self.assertEqual(res["action"], "reposted")
        post.assert_called_once()
        patch.assert_not_called()

    def test_no_prior_creates(self):
        res, post, patch = self._run([], "body\n<!-- prtriage-sig: 🛑:x -->")
        self.assertEqual(res["action"], "created")
        post.assert_called_once()

    def test_missing_sig_both_sides_edits_in_place(self):
        # an older comment with no embedded sig + a body with none → don't spam, edit it
        res, post, patch = self._run([_c(222)], "plain body no sig")
        self.assertEqual(res["action"], "updated")
        patch.assert_called_once()
        post.assert_not_called()

    def test_prior_sig_but_new_body_missing_edits_in_place(self):
        # can't confirm the verdict moved → conservative edit-in-place
        res, post, patch = self._run([_c(222, "🛑:x")], "plain body no sig")
        self.assertEqual(res["action"], "updated")
        patch.assert_called_once()

    def test_repost_targets_the_newest_and_reflips(self):
        # a chain exists (🛑 then 🟢); a new 🛑 (regressed again) differs from the newest
        # (🟢) → repost, not an edit of the 🟢
        res, post, patch = self._run(
            [_c(10, "🛑:x"), _c(20, "🟢:clean_forward")],
            "regressed\n<!-- prtriage-sig: 🛑:x -->")
        self.assertEqual(res["action"], "reposted")
        post.assert_called_once()
        patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
