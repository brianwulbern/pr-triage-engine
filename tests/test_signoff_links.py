import unittest

from prtriage import ai_review as A

SELF_SIGNOFF = (
    "@bob LGTM. I resolved the conflict with [TICKET-148](https://tracker.example.com/"
    "browse/TICKET-148) in this pull myself. The three commits are the ones that landed on "
    "`release-1.x` from https://github.com/example-org/product/pull/301.\n\n"
    "`TagServiceTest` passed in this pull's relevant run (ReportService build 1001).\n\n\n"
    "[TICKET-148]: https://tracker.example.com/browse/TICKET-148?origin=abc\n")


class LinksAreNotConditions(unittest.TestCase):
    def test_self_signoff_with_jira_link(self):
        self.assertTrue(A.is_lgtm(SELF_SIGNOFF))

    def test_closing_lgtm_with_a_query_link(self):
        self.assertTrue(A.is_lgtm("Matches master, see <https://x.test/a?b=1>.\n\nLGTM"))

    def test_a_real_question_still_holds(self):
        self.assertFalse(A.is_lgtm("@bob LGTM. Can you re-run CI first? "
                                   "https://x.test/a?b=1"))
        self.assertFalse(A.is_lgtm("LGTM. Please wait for https://x.test/run?id=1 to pass."))

    def test_resolution_sentence_with_link(self):
        self.assertTrue(A.signs_off_resolution(
            "Checked https://x.test/a?b=1 today. LGTM for the resolution."))


if __name__ == "__main__":
    unittest.main()
