"""A pull waiting on a person stays out of the forward set."""
import unittest
from datetime import datetime

from prtriage import waiting as W

ME = "me"
HEAD = "2026-11-16T10:00:00Z"
BEFORE, AFTER, LATER = "2026-11-16T09:00:00Z", "2026-11-16T12:00:00Z", "2026-11-16T14:00:00Z"


def _t(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _com(login, body, at, utype="User"):
    return {"user": {"login": login, "type": utype}, "body": body, "created_at": at}


def _rev(login, at):
    return {"user": {"login": login, "type": "User"}, "state": "COMMENTED", "submitted_at": at}


def _lab(login, at, utype="User", name=W.LABEL):
    return {"event": "labeled", "actor": {"login": login, "type": utype}, "created_at": at,
            "label": {"name": name}}


class LabelHold(unittest.TestCase):
    def hold(self, events, comments=(), labels=(W.LABEL,)):
        return W.label_hold(list(events), list(comments), list(labels), ME, _t(HEAD))

    def test_teammate_label_after_push_holds(self):
        why = self.hold([_lab("dev-b", AFTER)])
        self.assertIn("@dev-b", why)
        self.assertIn("after the last push", why)

    def test_own_label_does_not_hold(self):
        self.assertEqual(self.hold([_lab(ME, AFTER)]), "")

    def test_label_before_push_does_not_hold(self):
        self.assertEqual(self.hold([_lab("dev-b", BEFORE)]), "")

    def test_label_gone_does_not_hold(self):
        self.assertEqual(self.hold([_lab("dev-b", AFTER)], labels=()), "")

    def test_bot_label_does_not_hold(self):
        self.assertEqual(self.hold([_lab("ci-bot", AFTER, "Bot")]), "")

    def test_teammates_own_handback_does_not_hold(self):
        post = _com("dev-b", "Hi @dev, can you look?\n<!-- prtriage-sig: x -->", "2026-11-16T12:01:00Z")
        self.assertEqual(self.hold([_lab("dev-b", AFTER)], [post]), "")

    def test_latest_label_counts(self):
        self.assertIn("@bob", self.hold([_lab(ME, AFTER), _lab("bob", LATER)]))


class QuestionHold(unittest.TestCase):
    def hold(self, comments, reviews=(), head=HEAD):
        return W.question_hold(list(comments), list(reviews), ME, _t(head))

    ASK = "Hi @dev-c, this needs your sign-off. Is this OK to land as is? cc @dev-a"

    def test_unanswered_question_holds(self):
        why = self.hold([_com(ME, self.ASK, AFTER)])
        self.assertIn("@dev-c", why)
        self.assertIn("no reply", why)

    def test_reply_from_mentioned_clears(self):
        self.assertEqual(self.hold([_com(ME, self.ASK, AFTER), _com("dev-c", "Fine.", LATER)]), "")

    def test_review_from_mentioned_clears(self):
        self.assertEqual(self.hold([_com(ME, self.ASK, AFTER)], [_rev("dev-c", LATER)]), "")

    def test_push_after_question_clears(self):
        self.assertEqual(self.hold([_com(ME, self.ASK, AFTER)], head=LATER), "")

    def test_reply_from_someone_else_holds(self):
        self.assertNotEqual(self.hold([_com(ME, self.ASK, AFTER), _com("bob", "+1", LATER)]), "")

    def test_tool_post_is_not_a_question(self):
        self.assertEqual(self.hold([_com(ME, "Hi @dev, can you look?\n<!-- prtriage-sig: x -->", AFTER)]), "")

    def test_no_question_mark_does_not_hold(self):
        self.assertEqual(self.hold([_com(ME, "Pushed to the upstream fork. cc @dev", AFTER)]), "")

    def test_no_mention_does_not_hold(self):
        self.assertEqual(self.hold([_com(ME, "Is this still needed?", AFTER)]), "")

    def test_question_in_code_or_quote_does_not_hold(self):
        body = "> @dev can you look?\n\n`@x?` done @dev"
        self.assertEqual(self.hold([_com(ME, body, AFTER)]), "")

    def test_others_questions_do_not_hold(self):
        self.assertEqual(self.hold([_com("bob", self.ASK, AFTER)]), "")


class Gate(unittest.TestCase):
    def test_gate_memoizes_and_reads_target(self):
        calls = []
        orig = W.pull_wait
        W.pull_wait = lambda o, r, n, t: calls.append(n) or ("waiting" if n == "1" else "")
        W._STATES.clear()
        try:
            g = W.forward_gate("tok")
            rec = {"_audit": {"target": "https://github.com/example-org/product/pull/1"}}
            self.assertEqual(g(rec), "waiting")
            self.assertEqual(g(rec), "waiting")
            self.assertEqual(g({"_audit": {"target": "nope"}}), "")
        finally:
            W.pull_wait = orig
            W._STATES.clear()
        self.assertEqual(calls, ["1"])


if __name__ == "__main__":
    unittest.main()
