"""2.33.0 - --gh-post: don't duplicate the verdict comment on a flaky read; always
report the outcome on stdout.

Two bugs surfaced by the first live gh-post test (own PR #1234):

(1) DUPLICATE COMMENTS. `_find_trt_verdict_comment_id` returned None on ANY non-200
    (incl. a 403 rate limit), and `upsert_pr_verdict_comment` treated None as "no prior
    comment yet" → CREATE. So a rate-limited comment-list read spawned a fresh duplicate
    instead of editing the existing one. Two duplicates landed on #1234 during a
    rate-limited window. Fix: a read failure now raises `_CommentLookupError`; upsert
    ABORTS the post ({"ok": False, "skipped": True}) rather than creating blind.

(2) SILENT MUTATION. The success confirmation went through `log.info`, which deep
    analysis' logging reconfiguration swallowed - so a run that mutated the PR printed no
    confirmation. Fix: `_print_post_outcome` prints to stdout, independent of logging.
"""
import io
import unittest
from contextlib import redirect_stdout
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


class LookupClassifiesReadFailure(unittest.TestCase):
    def _find(self):
        # 2.156.0: renamed → returns (id, body) of the LATEST prtriage comment, or None.
        return comments._find_latest_verdict_comment(
            "product", "product", "1234", {}, timeout=5)

    def test_clean_empty_read_is_confidently_none(self):
        with mock.patch.object(comments.requests, "get", return_value=_Resp(200, [])):
            self.assertIsNone(self._find())

    def test_clean_read_finds_existing(self):
        page = [{"id": 111, "body": f"{MARKER}\n**PR#1234** …"}]
        with mock.patch.object(comments.requests, "get", return_value=_Resp(200, page)):
            self.assertEqual(self._find()[0], 111)

    def test_403_raises_lookup_error_not_none(self):
        # THE fix: a rate-limited read is UNKNOWN, must NOT read as 'none'.
        with mock.patch.object(comments.requests, "get", return_value=_Resp(403)):
            with self.assertRaises(comments._CommentLookupError):
                self._find()

    def test_network_error_raises_lookup_error(self):
        with mock.patch.object(comments.requests, "get",
                               side_effect=comments.requests.RequestException("boom")):
            with self.assertRaises(comments._CommentLookupError):
                self._find()


class UpsertNeverDuplicatesOnReadFailure(unittest.TestCase):
    def test_read_failure_skips_the_post_no_create(self):
        # A 403 on the list read must NOT fall through to a POST (create).
        get = mock.Mock(return_value=_Resp(403))
        post = mock.Mock()
        patch = mock.Mock()
        with mock.patch.object(comments.requests, "get", get), \
             mock.patch.object(comments.requests, "post", post), \
             mock.patch.object(comments.requests, "patch", patch):
            res = comments.upsert_pr_verdict_comment(PR, "body", token="tok")
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("skipped"))
        self.assertIn("duplicate", res["error"])
        post.assert_not_called()   # the whole point: no blind create
        patch.assert_not_called()

    def test_clean_none_still_creates(self):
        get = mock.Mock(return_value=_Resp(200, []))
        post = mock.Mock(return_value=_Resp(201, {"html_url": f"{PR}#issuecomment-1"}))
        with mock.patch.object(comments.requests, "get", get), \
             mock.patch.object(comments.requests, "post", post):
            res = comments.upsert_pr_verdict_comment(PR, "body", token="tok")
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "created")
        post.assert_called_once()

    def test_existing_still_edits_in_place(self):
        page = [{"id": 222, "body": f"{MARKER}\nold"}]
        get = mock.Mock(return_value=_Resp(200, page))
        patch = mock.Mock(return_value=_Resp(200, {"html_url": f"{PR}#issuecomment-222"}))
        with mock.patch.object(comments.requests, "get", get), \
             mock.patch.object(comments.requests, "patch", patch):
            res = comments.upsert_pr_verdict_comment(PR, "body", token="tok")
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "updated")
        patch.assert_called_once()




if __name__ == "__main__":
    unittest.main()
