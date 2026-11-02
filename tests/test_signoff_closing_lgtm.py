import unittest

from prtriage import ai_review as A

REVIEW = (
    "I have reviewed the model's conflict resolution as an original author.\n\n"
    "The only conflicted file was `bnd.bnd`, and the resolution is correct.\n\n"
    "* The helper file is byte identical to `master`.\n"
    "* This branch still passes `_settingsHelper`, branch drift unrelated to the change.\n\n"
    "The chain stays aligned, so there is no risk when a database moves between lines.\n\n"
    "LGTM.\n\n"
    "Thanks @dev-c for the database team sign-off and @bob for the independent read.\n\n")


class ClosingLgtm(unittest.TestCase):
    def test_counts(self):
        for b in (REVIEW, "The resolution matches master.\n\nLGTM",
                  "Checked the registrator.\n\nLooks good to me!\n\nThanks!"):
            self.assertTrue(A.is_lgtm(b), b)

    def test_does_not_count(self):
        for b in ("Please fix the typo first.\n\nLGTM",
                  "Can you confirm the version mapping?\n\nLGTM",
                  "The test should pass after the re-run.\n\nLGTM.",
                  "This needs to move to 6.1.9 before merging.\n\nLGTM",
                  "The resolution is fine.\n\nLGTM once CI passes",
                  "LGTM for the registrator.\n\nThe bnd.bnd change is wrong.",
                  "Thanks for the ping.\n\nI'll look tomorrow.",
                  "LGTM?"):
            self.assertFalse(A.is_lgtm(b), b)

    def test_finds_signer(self):
        c = [{"user": {"login": "alice"}, "created_at": "2026-11-02T00:31:40Z",
              "body": REVIEW}]
        got = A.find_signer([], c, [], "2026-10-26T00:00:00Z", ("bob",), lambda l: True)
        self.assertEqual(got[0], "alice")


if __name__ == "__main__":
    unittest.main()
