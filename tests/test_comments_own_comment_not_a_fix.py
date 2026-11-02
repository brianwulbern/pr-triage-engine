import unittest
from unittest import mock

from prtriage import comments as P

PR = "https://github.com/example-org/product/pull/106"
CHECK = ("I checked the model's conflict resolution here against the master commit 1285f9ba. "
         "The compile failure comes from the cherry-pick of master's c3bee7d into "
         "`PermissionsUtil` (9021028 here), which doesn't fit release-1.x.")


def _c(login, body, ts="2026-11-02T16:21:00Z"):
    return {"id": 1, "login": login, "body": body, "created_at": ts, "prtriage": False,
            "url": f"{PR}#issuecomment-1"}


class OwnCommentIsNotAFix(unittest.TestCase):
    def _run(self, thread, viewer):
        with mock.patch.object(P, "_pr_head", return_value=("abc1234", "2026-10-29T00:00:00Z")):
            return P._thread_offered_fixes("product", "product", "106", {}, 5,
                                           thread, viewer)

    def test_forwarders_resolution_check_is_skipped(self):
        self.assertEqual(self._run([_c("bob", CHECK)], "bob"), [])
        self.assertEqual(self._run([_c("Bob", CHECK)], "bob"), [])

    def test_a_developer_offer_still_counts(self):
        got = self._run([_c("alice", "please cherry-pick c3bee7d1 onto this")],
                        "bob")
        self.assertEqual([f["sha"] for f in got], ["c3bee7d1"])

    def test_unknown_viewer_keeps_the_old_behavior(self):
        self.assertEqual(len(self._run([_c("bob", CHECK)], "")), 1)

    def test_preview_passes_the_viewer(self):
        thread = [_c("bob", CHECK)]

        def find(owner, repo, number, headers, timeout, thread_out=None):
            thread_out.extend(thread)
            return (1, "<!-- prtriage-sig: 🛑:ci_compile_break -->", False)

        with mock.patch.object(P, "_find_latest_verdict_comment", side_effect=find), \
                mock.patch.object(P, "_viewer_login", return_value="bob"), \
                mock.patch.object(P, "_pr_head",
                                  return_value=("abc1234", "2026-10-29T00:00:00Z")), \
                mock.patch.object(P, "_compose_post_body",
                                  side_effect=lambda v, fixes: (self.assertEqual(fixes, []), v)[1]):
            P.preview_pr_verdict_action(PR, "Hi @alice - x\n<!-- prtriage-sig: 🛑:x -->",
                                         "tok")


if __name__ == "__main__":
    unittest.main()
