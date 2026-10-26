import unittest
from unittest import mock

from prtriage import ai_review as A

RULES = A.parse_codeowners(
    "modules/apps/portal-cache/    @platform-core\n"
    "modules/apps/portal-cache/portal-cache-test/    @product-database-infra\n"
    "modules/apps/commerce/    @product-commerce\n"
    "# a comment\n"
    "portal-kernel/src/com/product/portal/kernel/upgrade/    @product-database-infra\n")


class Owners(unittest.TestCase):
    def test_last_match_wins(self):
        self.assertEqual(A.owners_of("modules/apps/portal-cache/x/A.java", RULES),
                         ["platform-core"])
        self.assertEqual(A.owners_of("modules/apps/portal-cache/portal-cache-test/B.java",
                                     RULES), ["product-database-infra"])
        self.assertEqual(A.owners_of("modules/apps/portal-cache-x/C.java", RULES), [])

    def test_platform_team(self):
        self.assertTrue(A.platform_team(["Core-Dev-A"], None, None))
        self.assertTrue(A.platform_team(["alice"], ["modules/apps/commerce/a",
                                                 "modules/apps/portal-cache/b"], RULES))
        self.assertFalse(A.platform_team(["alice"], ["modules/apps/commerce/a"], RULES))
        self.assertFalse(A.platform_team(["alice"], None, RULES))


def _pull_state(authors, files, comments):
    pull = {"labels": [{"name": "ai-resolved-conflict"}], "base": {"ref": "release-1.x"},
            "head": {"sha": "abc"}, "user": {"login": "ci-bot-hu"}}

    def get(url, token):
        if url.endswith("/commits/abc"):
            return {"commit": {"committer": {"date": "2026-10-20T00:00:00Z"}}}
        if "CODEOWNERS" in url:
            import base64
            return {"content": base64.b64encode(
                b"modules/apps/portal-cache/    @platform-core\n").decode()}
        return pull

    def get_all(url, token, pages=10):
        if url.endswith("/files"):
            return None if files is None else [{"filename": f} for f in files]
        if url.endswith("/commits"):
            return [{"author": {"login": a, "type": "User"}} for a in authors]
        if url.endswith("/comments"):
            return comments
        return []

    A._CODEOWNERS.clear()
    with mock.patch.object(A, "_get", side_effect=get), \
            mock.patch.object(A, "_get_all", side_effect=get_all), \
            mock.patch("prtriage.comments._viewer_login", return_value="forwarder"), \
            mock.patch.object(A, "_members", return_value=lambda login: True):
        return A.pull_state("product", "product", "1", "tok")


class Redirect(unittest.TestCase):
    def test_platform_team_author_asks_only_the_reviewer(self):
        st = _pull_state(["core-dev-a"], ["portal-kernel/a.java"], [])
        self.assertEqual(st["authors"], ["core-reviewer"])
        self.assertEqual(st["team"], "Platform Core")
        self.assertFalse(st["noticed"])
        text = A.ask_text(st)
        self.assertTrue(text.startswith("@core-reviewer the backport bot's"))
        self.assertNotIn("core-dev-a", text)
        self.assertIn("If you're out", text)

    def test_platform_team_path_redirects(self):
        st = _pull_state(["alice"], ["modules/apps/portal-cache/a.java"], [])
        self.assertEqual(st["authors"], ["core-reviewer"])

    def test_other_code_keeps_authors(self):
        st = _pull_state(["alice"], ["modules/apps/commerce/a.java"], [])
        self.assertEqual(st["authors"], ["alice"])
        self.assertNotIn("team", st)

    def test_earlier_ask_to_the_authors_does_not_count(self):
        old = [{"body": f"{A._MARKER}\n@core-dev-a the backport bot's model ...",
                "created_at": "2026-10-21T00:00:00Z"}]
        self.assertFalse(_pull_state(["core-dev-a"], [], old)["noticed"])
        new = old + [{"body": f"{A._MARKER}\n@core-reviewer the backport bot's model ...",
                      "created_at": "2026-10-26T00:00:00Z"}]
        st = _pull_state(["core-dev-a"], [], new)
        self.assertTrue(st["noticed"])
        self.assertEqual(st["asked_at"], "2026-10-26T00:00:00Z")

    def test_pointer_names_the_reviewer_without_mention(self):
        text = A.pointer_text("1234", "asked", {"team": "Platform Core"})
        self.assertIn("Asked core-reviewer on #1234", text)
        self.assertNotIn("@", text)

    def test_unread_files_fall_back_to_the_authors(self):
        st = _pull_state(["alice"], None, [])
        self.assertTrue(st["readable"])
        self.assertEqual(st["authors"], ["alice"])
        self.assertEqual(_pull_state(["core-dev-c"], None, [])["authors"], ["core-reviewer"])


if __name__ == "__main__":
    unittest.main()
