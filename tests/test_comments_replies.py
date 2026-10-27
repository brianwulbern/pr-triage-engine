import unittest
from unittest import mock

from prtriage import comments as PS


def _c(i, login, at, body, prtriage=False):
    return {"id": i, "login": login, "body": body, "url": f"u{i}", "created_at": at, "prtriage": prtriage}


THREAD = [
    _c(1, "ci-bot", "2026-10-27T01:35:01Z", "Jenkins Build"),
    _c(2, "bob", "2026-10-27T13:00:37Z", "<!-- prtriage-verdict -->", prtriage=True),
    _c(3, "alice", "2026-10-27T14:38:02Z", "None of these come from this change."),
    _c(4, "alice", "2026-10-27T16:48:06Z", "cc: @bob"),
    _c(5, "someone", "2026-10-27T17:00:00Z", "ci:test:relevant"),
    _c(6, "someone", "2026-10-27T16:50:00Z", "more"),
]


class ThreadReplies(unittest.TestCase):
    def test_human_replies_after_trt_comment(self):
        r = PS.thread_replies(THREAD, 2, "bob")
        self.assertEqual([x["url"] for x in r], ["u3", "u4", "u6"])
        self.assertEqual([x["mentions_me"] for x in r], [False, True, False])

    def test_answered_replies_drop(self):
        t = THREAD + [_c(7, "bob", "2026-10-27T16:49:00Z", "Thanks, ..."),
                      _c(8, "bob", "2026-10-27T18:00:00Z", "ci:test:relevant")]
        self.assertEqual([x["url"] for x in PS.thread_replies(t, 2, "bob")], ["u6"])

    def test_nothing_before_or_unknown_comment(self):
        self.assertEqual(PS.thread_replies(THREAD, 6, "bob"), [])
        self.assertEqual(PS.thread_replies(THREAD, 99, "bob"), [])

    def test_preview_carries_replies(self):
        prev = (2, "<!-- prtriage-sig: ⚠️:x -->", True)

        def find(*a, thread_out=None, **k):
            thread_out.extend(THREAD)
            return prev
        with mock.patch.object(PS, "_find_latest_verdict_comment", side_effect=find), \
             mock.patch.object(PS, "_thread_offered_fixes", return_value=[]), \
             mock.patch.object(PS, "_viewer_login", return_value="bob"):
            pv = PS.preview_pr_verdict_action(
                "https://github.com/example-org/product/pull/105",
                "<!-- prtriage-sig: ⚠️:x -->", token="t")
        self.assertEqual(len(pv["replies"]), 3)




if __name__ == "__main__":
    unittest.main()
