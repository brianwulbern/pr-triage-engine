"""2.171.0 - the --gh-post DRY-RUN says, per PR, whether the post would CREATE a new
comment, EDIT the existing one in place (same conclusion), or REPOST (a new comment
because the conclusion changed).

The apply path already reports created / updated / reposted (comments.upsert_pr_verdict_
comment, decided by comparing the embedded `<!-- prtriage-sig -->` of the latest prtriage comment
against the new one). Before this, the dry-run just said "WOULD POST" with no hint which
of the three it would be. `preview_pr_verdict_action` is the read-only twin, and the
board dry-run annotates each red PR with its tag plus a tally.
"""
import unittest
from unittest import mock

from prtriage import comments

URL = "https://github.com/example-org/product/pull/1234"
SIG_A = "<!-- prtriage-sig: 🟠:diff_source_edited -->\nHi @dev, read A"
SIG_B = "<!-- prtriage-sig: 🟡:suppressed_diff_base_standing -->\nHi @dev, read B"
NOSIG = "<!-- prtriage-verdict -->\nan older prtriage comment with no signature"


class PreviewAction(unittest.TestCase):
    def _find(self, ret=None, exc=None):
        if exc is not None:
            return mock.patch.object(comments, "_find_latest_verdict_comment",
                                     side_effect=exc)
        return mock.patch.object(comments, "_find_latest_verdict_comment",
                                 return_value=ret)

    def test_no_token_is_unknown(self):
        self.assertEqual(
            comments.preview_pr_verdict_action(URL, SIG_A, token=None)["action"],
            "unknown")

    def test_bad_url_is_unknown(self):
        self.assertEqual(
            comments.preview_pr_verdict_action("nope", SIG_A, token="x")["action"],
            "unknown")

    def test_no_prior_comment_creates(self):
        with self._find(ret=None):
            self.assertEqual(
                comments.preview_pr_verdict_action(URL, SIG_A, token="x")["action"],
                "create")

    def test_same_signature_updates(self):
        with self._find(ret=(111, SIG_A, False)):
            pv = comments.preview_pr_verdict_action(URL, SIG_A, token="x")
        self.assertEqual(pv["action"], "update")
        self.assertIn("111", pv["url"])          # points at the existing comment

    def test_changed_signature_reposts(self):
        with self._find(ret=(111, SIG_A, False)):
            self.assertEqual(
                comments.preview_pr_verdict_action(URL, SIG_B, token="x")["action"],
                "repost")

    def test_missing_prev_signature_updates_not_reposts(self):
        # an older comment with no sig → cannot CONFIRM the verdict moved → edit in place
        # (never spawn a duplicate on uncertainty), mirroring the apply.
        with self._find(ret=(111, NOSIG, False)):
            self.assertEqual(
                comments.preview_pr_verdict_action(URL, SIG_A, token="x")["action"],
                "update")

    def test_lookup_failure_is_unknown(self):
        with self._find(exc=comments._CommentLookupError("rate limit")):
            pv = comments.preview_pr_verdict_action(URL, SIG_A, token="x")
        self.assertEqual(pv["action"], "unknown")
        self.assertIn("skip", pv["reason"])      # a real post would skip, not create

    def test_matches_apply_decision(self):
        # the preview action is the present-tense of what upsert would report.
        with self._find(ret=(111, SIG_A, False)):
            self.assertEqual(
                comments.preview_pr_verdict_action(URL, SIG_B, token="x")["action"],
                "repost")   # upsert would return "reposted" for the same inputs








if __name__ == "__main__":
    unittest.main()
