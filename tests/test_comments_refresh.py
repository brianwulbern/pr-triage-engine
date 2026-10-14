"""2.189.0 - --gh-post posts a FRESH comment when the thread moved on, not an invisible
in-place edit near the top.

prtriage's verdict comment can sit far up a long thread. 2.156 only reposted when the VERDICT
changed, so an unchanged verdict was edited in place and stayed buried above new replies /
new ci:test:relevant results. Now, when a NON-prtriage comment (a human reply, or a CI build
result) is newer than prtriage's latest verdict comment, an unchanged verdict RE-SURFACES as a
fresh bottom-of-thread comment ("refreshed"). Self-limiting: the fresh comment becomes the
newest prtriage comment, so it won't repost again until there's new activity after it. Opt out
with PRTRIAGE_NO_GH_REFRESH=1.
"""
import os
import unittest
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


def _tc(cid, sig, ts):   # prtriage verdict comment with a created_at
    return {"id": cid, "created_at": ts, "body": f"{MARKER}\nbody\n<!-- prtriage-sig: {sig} -->"}


def _nc(ts, body="looking into it"):   # a non-prtriage comment (human reply / CI result)
    return {"id": 777, "created_at": ts, "body": body}


T0, T1, T2 = ("2026-09-15T09:00:00Z", "2026-09-15T10:00:00Z", "2026-09-15T11:00:00Z")


class LaterActivity(unittest.TestCase):
    def _find(self, page):
        with mock.patch.object(comments.requests, "get", return_value=_Resp(200, page)):
            return comments._find_latest_verdict_comment(
                "product", "product", "1234", {}, timeout=5)

    def test_reply_after_trt_sets_later_activity(self):
        got = self._find([_tc(20, "🛑:a", T1), _nc(T2)])
        self.assertEqual(got[0], 20)
        self.assertTrue(got[2])

    def test_reply_before_trt_is_not_later_activity(self):
        got = self._find([_nc(T0), _tc(20, "🛑:a", T1)])
        self.assertFalse(got[2])

    def test_no_nontrt_comment_is_not_later_activity(self):
        got = self._find([_tc(20, "🛑:a", T1)])
        self.assertFalse(got[2])


class UpsertRefresh(unittest.TestCase):
    def _run(self, page, new_body, env=None):
        get = mock.Mock(return_value=_Resp(200, page))
        post = mock.Mock(return_value=_Resp(201, {"html_url": f"{PR}#issuecomment-new"}))
        patch = mock.Mock(return_value=_Resp(200, {"html_url": f"{PR}#issuecomment-edit"}))
        with mock.patch.object(comments.requests, "get", get), \
             mock.patch.object(comments.requests, "post", post), \
             mock.patch.object(comments.requests, "patch", patch), \
             mock.patch.dict(os.environ, env or {}, clear=False):
            res = comments.upsert_pr_verdict_comment(PR, new_body, token="tok")
        return res, post, patch

    def test_unchanged_verdict_with_activity_refreshes(self):
        res, post, patch = self._run(
            [_tc(20, "🛑:x", T1), _nc(T2)], f"same\n<!-- prtriage-sig: 🛑:x -->")
        self.assertEqual(res["action"], "refreshed")
        post.assert_called_once()
        patch.assert_not_called()

    def test_unchanged_verdict_quiet_thread_edits_in_place(self):
        res, post, patch = self._run(
            [_tc(20, "🛑:x", T1)], f"same\n<!-- prtriage-sig: 🛑:x -->")
        self.assertEqual(res["action"], "updated")
        patch.assert_called_once()
        post.assert_not_called()

    def test_changed_verdict_still_reposts(self):
        res, post, patch = self._run(
            [_tc(20, "🛑:x", T1), _nc(T2)], f"clean\n<!-- prtriage-sig: 🟢:y -->")
        self.assertEqual(res["action"], "reposted")
        post.assert_called_once()

    def test_env_opt_out_keeps_in_place_edit(self):
        res, post, patch = self._run(
            [_tc(20, "🛑:x", T1), _nc(T2)], f"same\n<!-- prtriage-sig: 🛑:x -->",
            env={"PRTRIAGE_NO_GH_REFRESH": "1"})
        self.assertEqual(res["action"], "updated")
        patch.assert_called_once()
        post.assert_not_called()


class PreviewRefresh(unittest.TestCase):
    def _preview(self, page, new_body):
        with mock.patch.object(comments.requests, "get", return_value=_Resp(200, page)):
            return comments.preview_pr_verdict_action(PR, new_body, token="tok")

    def test_preview_refresh(self):
        pv = self._preview([_tc(20, "🛑:x", T1), _nc(T2)], f"same\n<!-- prtriage-sig: 🛑:x -->")
        self.assertEqual(pv["action"], "refresh")

    def test_preview_update_when_quiet(self):
        pv = self._preview([_tc(20, "🛑:x", T1)], f"same\n<!-- prtriage-sig: 🛑:x -->")
        self.assertEqual(pv["action"], "update")

    def test_preview_repost_when_changed(self):
        pv = self._preview([_tc(20, "🛑:x", T1), _nc(T2)], f"clean\n<!-- prtriage-sig: 🟢:y -->")
        self.assertEqual(pv["action"], "repost")



# 2.254.0: the refresh is opt-in (PRTRIAGE_GH_REFRESH=1); these tests exercise the opt-in path.
_saved_refresh = None


def setUpModule():
    global _saved_refresh
    _saved_refresh = os.environ.get("PRTRIAGE_GH_REFRESH")
    os.environ["PRTRIAGE_GH_REFRESH"] = "1"


def tearDownModule():
    if _saved_refresh is None:
        os.environ.pop("PRTRIAGE_GH_REFRESH", None)
    else:
        os.environ["PRTRIAGE_GH_REFRESH"] = _saved_refresh


if __name__ == "__main__":
    unittest.main()
