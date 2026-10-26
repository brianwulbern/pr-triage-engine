import unittest

from prtriage import ai_review as A
from prtriage import comments


class LgtmWording(unittest.TestCase):
    def test_counts(self):
        for b in ("@release-mgr, tested this locally and all looks good. Thanks! ",
                  "@release-mgr, tested it locally and it looks good ",
                  "Looks good", "looks good!", "LGTM", "@a @b LGTM",
                  "I checked the resolution and it looks good to me.",
                  "Thanks, reviewed it, looks good"):
            self.assertTrue(A.is_lgtm(b), b)

    def test_does_not_count(self):
        for b in ("looks good except the test", "looks good?", "tested it locally and it fails",
                  "tested it locally and it doesn't look good", "not looks good",
                  "It looks good once CI passes", "I'll take a look on that ASAP.",
                  "@release-mgr Thanks, I checked it too and the resolution matches master.",
                  "LGTM once the test passes", "<!-- prtriage-ai-review-hold -->\nlooks good", None):
            self.assertFalse(A.is_lgtm(b), b)

    def test_finds_signer_from_wording(self):
        c = [{"user": {"login": "dev-r"}, "created_at": "2026-10-21T10:54:00Z",
              "body": "@release-mgr, tested it locally and it looks good "}]
        got = A.find_signer([], c, [], "2026-10-20T00:00:00Z", ("me",), lambda l: True)
        self.assertEqual(got[0], "dev-r")



if __name__ == "__main__":
    unittest.main()
