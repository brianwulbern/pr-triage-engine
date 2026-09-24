"""2.199.0 (#1234) - don't post a verdict on a PR that was closed since the sweep triaged it.

A board triages while a PR is open, but the human can close it (superseded / ci:close) before
the post lands minutes later. Asking a dev to look at failures on a PR they just closed is
noise (a dev closed it as superseded, then prtriage pinged them on it). upsert_pr_verdict_comment
re-checks the state FRESH at post time (bypassing the get_pull_json triage-time memo) and skips
a closed PR. Fail-open: only a CONFIRMED 'closed' skips; an unreadable state still posts.
"""
import unittest
from unittest import mock

from prtriage import comments

PR_URL = "https://github.com/example-org/product/pull/1234"


def _resp(status, body=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    return r


class ClosedSkip(unittest.TestCase):
    def test_closed_pr_is_skipped_without_posting(self):
        with mock.patch.object(comments, "_pr_current_state", return_value="closed"), \
             mock.patch.object(comments.requests, "get") as g, \
             mock.patch.object(comments.requests, "post") as post, \
             mock.patch.object(comments.requests, "patch") as patch:
            res = comments.upsert_pr_verdict_comment(PR_URL, "🟠 INVESTIGATE", "tok")
        self.assertFalse(res.get("ok"))
        self.assertTrue(res.get("skipped"))
        self.assertIn("closed", res.get("error", "").lower())
        post.assert_not_called()
        patch.assert_not_called()
        g.assert_not_called()          # never even looks for the prior comment

    def test_open_pr_posts(self):
        with mock.patch.object(comments, "_pr_current_state", return_value="open"), \
             mock.patch.object(comments, "_find_latest_verdict_comment",
                               return_value=None), \
             mock.patch.object(comments.http_cache, "gh_post",
                               return_value=_resp(201, {"html_url": "u/1"})) as post:
            res = comments.upsert_pr_verdict_comment(PR_URL, "🟠 INVESTIGATE", "tok")
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("action"), "created")
        post.assert_called_once()

    def test_unknown_state_fails_open_and_posts(self):
        # a transient/unreadable state must NOT block a legit post
        with mock.patch.object(comments, "_pr_current_state", return_value=None), \
             mock.patch.object(comments, "_find_latest_verdict_comment",
                               return_value=None), \
             mock.patch.object(comments.http_cache, "gh_post",
                               return_value=_resp(201, {"html_url": "u/2"})) as post:
            res = comments.upsert_pr_verdict_comment(PR_URL, "🟠 INVESTIGATE", "tok")
        self.assertTrue(res.get("ok"))
        post.assert_called_once()


class FreshState(unittest.TestCase):
    def test_reads_state_via_conditional_get(self):
        with mock.patch.object(comments.http_cache, "conditional_get_json",
                               return_value={"state": "CLOSED"}):
            self.assertEqual(
                comments._pr_current_state("product", "product", "1234", "tok"),
                "closed")


    def test_none_on_no_token_or_error(self):
        self.assertIsNone(comments._pr_current_state("o", "r", "1", None))
        with mock.patch.object(comments.http_cache, "conditional_get_json",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(comments._pr_current_state("o", "r", "1", "tok"))


if __name__ == "__main__":
    unittest.main()
