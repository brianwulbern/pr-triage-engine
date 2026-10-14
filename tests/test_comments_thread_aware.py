"""2.239.0 - --gh-post reads the thread before posting (#1234).

On product#1234 a dev replied "you guys need to cherry pick this commit <url>" with
the correct fix. prtriage's next post ignored it, again told the authors to fix the PR themselves,
and was a near-copy of its prior comment (the only change: "27 commit(s) behind" -> "82"),
posted fresh because the reply counted as the thread moving on.

Now at post time prtriage (a) leads with a fix offered in a human comment the PR hasn't picked up
(newer than the head commit, not the head itself), and (b) treats a refresh whose body is
unchanged except for volatile numbers as an in-place edit, not a duplicate. The behind-count
only counts as a change when it crosses config.STALE_REBASE_BEHIND_THRESHOLD.
"""
import os
import unittest
from unittest import mock

from prtriage import config, comments as P

PR = "https://github.com/example-org/product/pull/1234"
MARKER = P._VERDICT_MARKER
FIX_URL = ("https://github.com/dev-a/product/commit/"
           "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d")
FIX_TEXT = f"@release-mgr @dev-b you guys need to cherry pick this commit here {FIX_URL}"
T_HEAD, T_TRT, T_FIX, T_TRT2 = ("2026-09-22T20:22:57Z", "2026-09-23T14:50:14Z",
                                "2026-09-23T18:08:22Z", "2026-09-24T03:19:33Z")


def _verdict(behind=27, sig="🛑:ci_compile_break"):
    return ("Hi @dev-a @dev-c - can you take a look at prtriage's verdict below? it needs "
            "a change to the PR itself before landing (see the headline).\n\n"
            "**PR#1234** - 🛑 DO NOT LAND - compile failure\n"
            f"ℹ️ Branch is {behind} commit(s) behind base (and 11 ahead).\n"
            f"<!-- prtriage-sig: {sig} -->")


def _human(ts, body, login="dev-a", cid=1001):
    return {"id": cid, "login": login, "body": body, "created_at": ts, "prtriage": False,
            "url": f"{PR}#issuecomment-{cid}"}


class CommentFixOffers(unittest.TestCase):
    def test_cherry_pick_commit_link(self):
        got = P._comment_fix_offers(_human(T_FIX, FIX_TEXT))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["sha"][:7], "1a2b3c4")
        self.assertEqual(got[0]["commit_url"], FIX_URL)
        self.assertEqual(got[0]["login"], "dev-a")

    def test_bare_sha_with_cherry_pick(self):
        got = P._comment_fix_offers(_human(T_FIX, "please cherry-pick 1a2b3c4d5e6 onto this"))
        self.assertEqual([(g["sha"], g["commit_url"]) for g in got], [("1a2b3c4d5e6", None)])

    def test_commit_link_with_fix_wording(self):
        got = P._comment_fix_offers(_human(T_FIX, f"this commit fixes it: {FIX_URL}"))
        self.assertEqual(len(got), 1)

    def test_commit_link_without_fix_wording_is_not_an_offer(self):
        self.assertEqual(P._comment_fix_offers(_human(T_FIX, f"this commit broke it {FIX_URL}")),
                         [])

    def test_all_letter_hex_word_ignored(self):
        self.assertEqual(P._comment_fix_offers(_human(T_FIX, "cherry pick the defaced one")), [])


class ThreadOfferedFixes(unittest.TestCase):
    def _run(self, thread, head=("9f8e7d6c5bbbb", T_HEAD)):
        with mock.patch.object(P, "_pr_head", return_value=head) as ph:
            return P._thread_offered_fixes("product", "product", "1234", {}, 5,
                                           thread), ph

    def test_offer_newer_than_head_counts(self):
        got, _ = self._run([_human(T_FIX, FIX_TEXT)])
        self.assertEqual(len(got), 1)

    def test_offer_older_than_head_is_assumed_handled(self):
        got, _ = self._run([_human(T_FIX, FIX_TEXT)], head=("9f8e7d6c5bbbb",
                                                           "2026-09-23T20:00:00Z"))
        self.assertEqual(got, [])

    def test_offered_commit_is_the_head(self):
        got, _ = self._run([_human(T_FIX, FIX_TEXT)], head=("1a2b3c4d5e6f7081", T_HEAD))
        self.assertEqual(got, [])

    def test_bot_and_trt_comments_ignored(self):
        got, ph = self._run([
            _human(T_FIX, FIX_TEXT, login="ci-bot-hu"),
            dict(_human(T_FIX, FIX_TEXT), prtriage=True)])
        self.assertEqual(got, [])
        ph.assert_not_called()                 # no offer -> the head is never fetched

    def test_no_offer_never_fetches_the_head(self):
        _, ph = self._run([_human(T_FIX, "looking into it")])
        ph.assert_not_called()

    def test_head_unreadable_falls_back_to_since_last_verdict(self):
        prtriage = {"id": 1, "login": "release-mgr", "body": MARKER, "created_at": T_TRT,
               "prtriage": True, "url": ""}
        got, _ = self._run([prtriage, _human(T_FIX, FIX_TEXT)], head=(None, None))
        self.assertEqual(len(got), 1)                        # offer is after prtriage's verdict
        early = _human("2026-09-23T10:00:00Z", FIX_TEXT)
        got, _ = self._run([early, prtriage], head=(None, None))
        self.assertEqual(got, [])                            # offer predates prtriage's verdict


class ComposePostBody(unittest.TestCase):
    FIX = {"login": "dev-a", "sha": "1a2b3c4d5e6f7081", "commit_url": FIX_URL,
           "comment_url": f"{PR}#issuecomment-1001"}

    def test_greeting_replaced_mentions_kept(self):
        out = P._compose_post_body(_verdict(), [self.FIX])
        first = out.split("\n", 1)[0]
        self.assertTrue(first.startswith("Hi @dev-a @dev-c, dev-a offered a fix"))
        self.assertIn(f"[`1a2b3c4`]({FIX_URL})", first)
        self.assertIn("issuecomment-1001", first)
        self.assertNotIn("it needs a change to the PR itself", first)
        self.assertIn("🛑 DO NOT LAND", out)                 # the verdict itself stays

    def test_no_greeting_prepends_lead(self):
        body = "**PR#1** - ⚠️ REVIEW\n<!-- prtriage-sig: ⚠️:x -->"
        out = P._compose_post_body(body, [self.FIX])
        self.assertTrue(out.startswith("💬 dev-a offered a fix"))
        self.assertTrue(out.endswith(body))

    def test_green_verdict_untouched(self):
        body = "🟢 LIKELY SAFE\n<!-- prtriage-sig: 🟢:clean -->"
        self.assertEqual(P._compose_post_body(body, [self.FIX]), body)

    def test_no_fixes_untouched(self):
        self.assertEqual(P._compose_post_body(_verdict(), []), _verdict())

    def test_lead_has_no_em_dash(self):
        # the whole greeting line is replaced, so the composed first line has none at all
        first = P._compose_post_body(_verdict(), [self.FIX]).split("\n", 1)[0]
        self.assertNotIn(chr(0x2014), first)
        lead = P._compose_post_body("**PR#1** ⚠️\n<!-- prtriage-sig: ⚠️:x -->", [self.FIX])
        self.assertNotIn(chr(0x2014), lead.split("\n", 1)[0])


class PostSubstance(unittest.TestCase):
    def test_behind_drift_within_range_is_no_change(self):
        self.assertEqual(P._post_substance(MARKER + "\n" + _verdict(27)),
                         P._post_substance(_verdict(82)))

    def test_crossing_rebase_threshold_is_a_change(self):
        t = config.STALE_REBASE_BEHIND_THRESHOLD
        self.assertNotEqual(P._post_substance(_verdict(t - 10)),
                            P._post_substance(_verdict(t + 10)))

    def test_real_change_is_a_change(self):
        self.assertNotEqual(P._post_substance(_verdict()),
                            P._post_substance(_verdict().replace("compile failure", "x")))


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


def _page_trt(ts, body):
    return {"id": 20, "created_at": ts, "body": f"{MARKER}\n{body}",
            "user": {"login": "release-mgr"}, "html_url": f"{PR}#issuecomment-20"}


def _page_human(ts, body):
    return {"id": 1001, "created_at": ts, "body": body,
            "user": {"login": "dev-a"}, "html_url": f"{PR}#issuecomment-1001"}


class UpsertThreadAware(unittest.TestCase):
    def _run(self, page, new_body, head=("9f8e7d6c5bbbb", T_HEAD)):
        get = mock.Mock(return_value=_Resp(200, page))
        post = mock.Mock(return_value=_Resp(201, {"html_url": f"{PR}#issuecomment-new"}))
        patch = mock.Mock(return_value=_Resp(200, {"html_url": f"{PR}#issuecomment-20"}))
        with mock.patch.object(P.requests, "get", get), \
             mock.patch.object(P.requests, "post", post), \
             mock.patch.object(P.requests, "patch", patch), \
             mock.patch.object(P, "_pr_head", return_value=head), \
             mock.patch.object(P, "_pr_current_state", return_value="open"), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRTRIAGE_NO_GH_REFRESH", None)
            res = P.upsert_pr_verdict_comment(PR, new_body, token="tok")
        return res, post, patch

    def test_40353_refresh_leads_with_the_fix(self):
        # thread moved on BECAUSE of the fix offer -> fresh comment, and it answers it.
        res, post, patch = self._run([_page_trt(T_TRT, _verdict(27)),
                                      _page_human(T_FIX, FIX_TEXT)], _verdict(82))
        self.assertEqual(res["action"], "refreshed")
        sent = post.call_args.kwargs["json"]["body"]
        self.assertIn("dev-a offered a fix: cherry-pick [`1a2b3c4`]", sent)
        patch.assert_not_called()

    def test_refresh_with_nothing_new_edits_in_place(self):
        # a chatty reply, same verdict, only the behind-count drifted -> no duplicate.
        res, post, patch = self._run([_page_trt(T_TRT, _verdict(27)),
                                      _page_human(T_FIX, "thanks, looking")], _verdict(82))
        self.assertEqual(res["action"], "updated")
        post.assert_not_called()
        patch.assert_called_once()

    def test_after_fix_lead_posted_a_rerun_keeps_it_in_place(self):
        # prtriage already answered with the lead; nothing new since -> edit in place, lead kept.
        led = P._compose_post_body(_verdict(82), [{
            "login": "dev-a", "sha": "1a2b3c4d5e6f7081", "commit_url": FIX_URL,
            "comment_url": f"{PR}#issuecomment-1001"}])
        res, post, patch = self._run([_page_human(T_FIX, FIX_TEXT),
                                      _page_trt(T_TRT2, led)], _verdict(90))
        self.assertEqual(res["action"], "updated")
        self.assertIn("dev-a offered a fix", patch.call_args.kwargs["json"]["body"])

    def test_behind_crossing_threshold_still_refreshes(self):
        t = config.STALE_REBASE_BEHIND_THRESHOLD
        res, post, _ = self._run([_page_trt(T_TRT, _verdict(t - 5)),
                                  _page_human(T_FIX, "thanks")], _verdict(t + 5))
        self.assertEqual(res["action"], "refreshed")
        post.assert_called_once()


class PreviewThreadAware(unittest.TestCase):
    def test_preview_flags_quiet_and_returns_composed_body(self):
        get = mock.Mock(return_value=_Resp(200, [_page_trt(T_TRT, _verdict(27)),
                                                 _page_human(T_FIX, "thanks")]))
        with mock.patch.object(P.requests, "get", get), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRTRIAGE_NO_GH_REFRESH", None)
            pv = P.preview_pr_verdict_action(PR, _verdict(82), token="tok")
        self.assertEqual(pv["action"], "update")
        self.assertTrue(pv["quiet"])
        self.assertEqual(pv["body"], _verdict(82))

    def test_preview_shows_fix_lead(self):
        get = mock.Mock(return_value=_Resp(200, [_page_trt(T_TRT, _verdict(27)),
                                                 _page_human(T_FIX, FIX_TEXT)]))
        with mock.patch.object(P.requests, "get", get), \
             mock.patch.object(P, "_pr_head", return_value=("9f8e7d6c5bbbb", T_HEAD)), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRTRIAGE_NO_GH_REFRESH", None)
            pv = P.preview_pr_verdict_action(PR, _verdict(82), token="tok")
        self.assertEqual(pv["action"], "refresh")
        self.assertFalse(pv["quiet"])
        self.assertTrue(pv["body"].startswith("Hi @dev-a @dev-c, dev-a offered"))



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
