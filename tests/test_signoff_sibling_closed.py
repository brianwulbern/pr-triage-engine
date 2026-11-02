import datetime
import unittest
from unittest import mock

from prtriage import ai_review as A

MON = datetime.date(2026, 11, 2)
POINTER = A._REMIND_MARKER + "\n" + A.pointer_text("201", "remind", {"team": "Platform Core"})


def _rec(num):
    return {"_audit": {"target": f"https://github.com/example-org/product/pull/{num}"},
            "result": {"verdict": {}}}


def _st(**kw):
    st = {"ai": True, "readable": True, "signer": None, "signed_label": False,
          "authors": ["core-reviewer"], "team": "Platform Core", "noticed": True,
          "asked_at": "2026-10-23T10:00:00Z", "reminded_at": "2026-10-28T10:00:00Z",
          "requests": {}, "open": True, "pointer": None, "base": "release-1.x"}
    st.update(kw)
    return st


def _classify(states):
    A._STATES.clear()
    with mock.patch.object(A, "_state", side_effect=lambda o, r, n, t: states[n]):
        return A.classify([_rec(n) for n in states if n != "201"], "tok",
                          "example-org/product", now=MON)


class SiblingClosed(unittest.TestCase):
    def test_closed_signed_sibling_asks_here(self):
        states = {"202": _st(pointer="201"),
                  "201": _st(open=False, signer=("core-reviewer", "LGTM comment", "x"))}
        c = _classify(states)
        self.assertEqual([r[2] for r in c["sibling"]], ["202"])
        self.assertFalse(c["overdue"])
        body = A.sibling_text(states["202"])
        self.assertIn("@core-reviewer #201", body)
        self.assertIn("closed", body)
        self.assertIn("`core-reviewer` approved", body)
        self.assertIn("likely holds up", body)
        self.assertNotIn("\u2014", body)

    def test_open_or_unsigned_sibling_stays_overdue(self):
        for sib in (_st(signer=("core-reviewer", "LGTM comment", "x")), _st(open=False)):
            c = _classify({"202": _st(pointer="201"), "201": sib})
            self.assertEqual([r[2] for r in c["overdue"]], ["202"])
            self.assertFalse(c["sibling"])

    def test_board_groups_the_siblings(self):
        states = {"202": _st(pointer="201"), "203": _st(pointer="201"),
                  "201": _st(open=False, signer=("core-reviewer", "LGTM comment", "x"))}
        posted = []
        A._STATES.clear()
        with mock.patch.object(A, "_state", side_effect=lambda o, r, n, t: states[n]), \
                mock.patch("prtriage.github_writes._comment",
                           side_effect=lambda t, repo, n, body: posted.append((n, body)) or True):
            A.board_step([_rec(202), _rec(203)], "tok", True,
                         "example-org/product", False, lambda q: "")
        self.assertEqual([n for n, _ in posted], ["202", "203"])
        self.assertIn("@core-reviewer #201", posted[0][1])
        self.assertIn("#203", posted[0][1])
        self.assertTrue(posted[1][1].startswith(A._REMIND_MARKER))
        self.assertIn("on #202 for this pull too", posted[1][1])


def _pointer_of(comments):
    pull = {"labels": [{"name": "ai-resolved-conflict"}], "base": {"ref": "release-1.x"},
            "head": {"sha": "abc"}, "user": {"login": "ci-bot-hu"},
            "state": "open"}

    def get(url, token):
        if url.endswith("/commits/abc"):
            return {"commit": {"committer": {"date": "2026-10-20T00:00:00Z"}}}
        return None if "CODEOWNERS" in url else pull

    def get_all(url, token, pages=10):
        if url.endswith("/commits"):
            return [{"author": {"login": "dave", "type": "User"}}]
        if url.endswith("/files"):
            return [{"filename": "modules/apps/a/a.java"}]
        if url.endswith("/comments"):
            return comments
        return []

    A._CODEOWNERS.clear()
    with mock.patch.object(A, "_get", side_effect=get), \
            mock.patch.object(A, "_get_all", side_effect=get_all), \
            mock.patch.object(A, "platform_team", return_value=True), \
            mock.patch("prtriage.comments._viewer_login", return_value="forwarder"), \
            mock.patch.object(A, "_members", return_value=lambda login: True):
        return A.pull_state("product", "product", "202", "tok")["pointer"]


def _c(login, at, body):
    return {"user": {"login": login, "type": "User"}, "created_at": at, "body": body}


class PointerFromComments(unittest.TestCase):
    def test_last_trt_comment_is_a_pointer(self):
        self.assertEqual(_pointer_of([_c("forwarder", "2026-10-28T10:00:00Z", POINTER)]), "201")

    def test_a_full_reminder_after_the_pointer_clears_it(self):
        full = A._REMIND_MARKER + "\n@core-reviewer a reminder: this pull still needs a sign-off"
        self.assertIsNone(_pointer_of([_c("forwarder", "2026-10-28T10:00:00Z", POINTER),
                                       _c("forwarder", "2026-10-30T10:00:00Z", full)]))

    def test_a_hand_written_mention_after_the_pointer_clears_it(self):
        hand = "@core-reviewer thanks for checking #201. Could you check these too?"
        self.assertIsNone(_pointer_of([_c("forwarder", "2026-10-28T10:00:00Z", POINTER),
                                       _c("forwarder", "2026-11-02T16:00:00Z", hand)]))


if __name__ == "__main__":
    unittest.main()
