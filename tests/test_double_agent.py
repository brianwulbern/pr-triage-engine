"""3.1.0: two models review each held AI resolution once per head, to help the person who
signs it off. The hold doesn't change."""
import json
import os
import tempfile
import unittest
from unittest import mock

from prtriage import ai_review as A
from prtriage import double_agent as D

M = "a" * 40
BODY = (f"Resolved while cherry-picking https://github.com/example-org/product/commit/{M}\n\n"
        "Resolved files:\n* `a/B.java`\n* `a/C.java`\n\nModel: m\n\n"
        "Risks reported by the model:\n* kept the branch's helper\n\nmore")
PASS = {"verdict": "pass", "hunks": [], "reason": "Applies master's intent."}
CONCERN = {"verdict": "concern", "reason": "The helper is missing on the branch.",
           "hunks": [{"file": "a/B.java", "issue": "calls postX, absent on the branch"}]}


def _rec(*answers):
    return {"pr": "107", "head": "c" * 40, "base": "release-1.x", "at": "t",
            "reviews": [{"name": n, "model": m, "answer": a} for (n, m), a
                        in zip(D.config.DOUBLE_AGENT_MODELS, answers)]}


class Parse(unittest.TestCase):
    def test_sections_read_the_master_commit_files_and_risks(self):
        s = D.sections(BODY)
        self.assertEqual(s[0]["master"], M)
        self.assertEqual(s[0]["files"], ["a/B.java", "a/C.java"])
        self.assertIn("kept the branch's helper", s[0]["risks"])

    def test_an_answer_cut_before_its_closing_brace_is_recovered(self):
        raw = 'checked it. {"verdict": "concern", "hunks": [], "reason": "missing helper."'
        self.assertEqual(D.last_json(raw)["verdict"], "concern")

    def test_no_verdict_is_none(self):
        self.assertIsNone(D.last_json("I could not decide."))


class Later(unittest.TestCase):
    """3.1.1 (an older pull): a fix pushed after the backport bot's pick is part of the evidence."""

    def _commits(self):
        return [{"sha": "0" * 40, "commit": {"message": "older"}},
                {"sha": "1" * 40, "commit": {"message": "the backport bot pick"}},
                {"sha": "2" * 40, "commit": {"message": "Use the branch API\n\nbody"}},
                {"sha": "3" * 40, "commit": {"message": "unrelated"}}]

    def test_a_later_commit_on_a_resolved_file_is_listed(self):
        secs = [{"pick": "1" * 40, "files": ["a/B.java"]}]
        touched = {"2" * 40: "a/B.java\nx/Y.java\n", "3" * 40: "x/Y.java\n"}
        with mock.patch.object(D, "_git", side_effect=lambda root, *a, **k: touched.get(a[-1])):
            got = D.later_commits(secs, self._commits(), "/r")
        self.assertEqual(got, [("2" * 40, "Use the branch API", ["a/B.java"])])

    def test_commits_before_the_pick_and_without_a_pick_are_ignored(self):
        with mock.patch.object(D, "_git", return_value="a/B.java\n"):
            self.assertEqual(D.later_commits([{"pick": None, "files": ["a/B.java"]}],
                                             self._commits(), "/r"), [])
            got = D.later_commits([{"pick": "1" * 40, "files": ["a/B.java"]}],
                                  self._commits(), "/r")
        self.assertEqual([g[0] for g in got], ["2" * 40, "3" * 40])


class Text(unittest.TestCase):
    """3.1.2: the verdict first, both reviews folded below."""

    def test_both_pass(self):
        t = D.comment_text(_rec(PASS, PASS))
        self.assertTrue(t.startswith(D.MARKER.format(sha="c" * 40)))
        self.assertIn("**Double-agent review: both models pass.**", t.split("\n")[1])
        self.assertIn("<details><summary>Both reviews</summary>", t)
        self.assertIn("not a sign-off", t)
        self.assertNotIn("\u2014", t)
        self.assertNotIn("@", t)

    def test_a_concern_leads_with_the_file_and_the_issue(self):
        t = D.comment_text(_rec(CONCERN, PASS))
        lines = t.split("\n")
        self.assertIn("a concern to check before signing off", lines[1])
        self.assertEqual(lines[2], "- `B.java`: calls postX, absent on the branch (Fable)")
        self.assertIn("**Opus** (`claude-opus-5-5`): pass.", t)
        self.assertEqual(D.summary(_rec(CONCERN, PASS)), "concern (Fable concern, Opus pass)")

    def test_both_models_on_one_file_are_one_line_with_the_shorter_sentence(self):
        long = {"verdict": "concern", "reason": "r", "hunks": [
            {"file": "a/B.java", "issue": "The new test calls postX, which the branch's "
                                          "helper does not define at this head. More."}]}
        rows = D.concerns(_rec(long, CONCERN))
        self.assertEqual(rows, [("a/B.java", "calls postX, absent on the branch",
                                 ["Fable", "Opus"])])

    def test_a_concern_without_hunks_uses_the_reason(self):
        bare = {"verdict": "concern", "hunks": [], "reason": "Check the upgrade step. Rest."}
        self.assertEqual(D.concerns(_rec(bare, PASS)), [("", "Check the upgrade step.",
                                                         ["Fable"])])

    def test_a_concern_mentions_and_a_pass_never_does(self):
        self.assertIn("@dev 🕵", D.comment_text(_rec(CONCERN, PASS), ["dev"]))
        self.assertNotIn("@dev", D.comment_text(_rec(PASS, PASS), ["dev"]))

    def test_the_ask_line(self):
        self.assertIn("both pass", D.ask_line(_rec(PASS, PASS)))
        self.assertIn("raised a concern", D.ask_line(_rec(CONCERN, PASS)))
        self.assertEqual(D.ask_line(None), "")

    def test_posted_comments_read_the_layout(self):
        comments = [{"id": 1, "user": {"login": "me"},
                     "body": "<!-- prtriage-double-agent: " + "c" * 40 + " -->"},
                    {"id": 2, "user": {"login": "me"}, "body": D.MARKER.format(sha="e" * 40)},
                    {"id": 3, "user": {"login": "other"},
                     "body": D.MARKER.format(sha="d" * 40)}]
        self.assertEqual(D.posted_comments(comments, "me"),
                         {"c" * 40: (1, 1, None), "e" * 40: (2, D.LAYOUT, None)})
        self.assertEqual(D.posted_heads(comments, "me"), {"c" * 40, "e" * 40})


class Review(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _review(self, answers):
        calls = []

        def ask(model, prompt, root):
            calls.append(model)
            return {"model": model, "answer": answers[len(calls) - 1], "cost": 0.1}

        def match(secs, commits, root):
            for s in secs:
                s["pick"] = "p" * 40

        with mock.patch.object(D, "_ask", side_effect=ask), \
                mock.patch.object(D, "match_picks", side_effect=match), \
                mock.patch.object(D, "bundle", return_value="evidence"):
            rec = D.review("107", "c" * 40, "release-1.x", "t", BODY, [], "/r")
        return rec, calls

    def test_a_head_is_reviewed_once(self):
        rec, calls = self._review([PASS, PASS])
        self.assertEqual(len(calls), 2)
        self.assertTrue(D.both_pass(rec))
        again, calls = self._review([CONCERN, CONCERN])
        self.assertEqual(calls, [])
        self.assertTrue(D.both_pass(again))

    def test_a_missing_answer_is_not_cached(self):
        rec, _ = self._review([PASS, None])
        self.assertIsNone(rec)
        _rec2, calls = self._review([PASS, PASS])
        self.assertEqual(len(calls), 2)

    def test_a_pull_without_bot_sections_is_not_reviewed(self):
        with mock.patch.object(D, "_ask") as ask:
            self.assertIsNone(D.review("1", "c" * 40, "b", "t", "plain body", [], "/r"))
        ask.assert_not_called()

    def test_the_ledger_records_the_verdicts(self):
        D.ledger(_rec(CONCERN, PASS), True)
        with open(os.path.join(D._dir(), "ledger.jsonl")) as fh:
            row = json.loads(fh.readline())
        self.assertEqual(row["verdicts"], {"Fable": "concern", "Opus": "pass"})
        self.assertFalse(row["both_pass"])
        self.assertTrue(row["compile_label"])


def _st(head="c" * 40, posted=()):
    return {"ai": True, "readable": True, "head": head, "da_heads": set(posted),
            "base": "release-1.x", "authors": ["dev"], "open": True}


class Board(unittest.TestCase):
    def _classes(self, **rows):
        c = {k: [] for k in ("ask", "release", "refresh", "rehold", "waiting", "unreadable",
                             "remind", "overdue", "landed", "replaced", "replied", "sibling")}
        c.update(rows)
        return c

    def _step(self, c, rec=None):
        with mock.patch("prtriage.local_clone.portal_root", return_value="/r"), \
                mock.patch.object(D, "enabled", return_value=True), \
                mock.patch.object(D, "ensure_local", return_value=True), \
                mock.patch.object(D, "review", return_value=rec or _rec(PASS, PASS)) as rv:
            return A.double_agent_step(c, "t"), rv

    def test_held_pulls_are_reviewed(self):
        out, rv = self._step(self._classes(waiting=[("product", "r", "1", _st())],
                                           ask=[("product", "r", "2", _st())]))
        self.assertEqual([x[2] for x in out], ["1", "2"])
        self.assertEqual(rv.call_count, 2)

    def test_a_head_already_posted_is_skipped(self):
        out, rv = self._step(self._classes(waiting=[("product", "r", "1",
                                                     _st(posted=["c" * 40]))]))
        self.assertEqual(out, [])
        rv.assert_not_called()

    def test_a_signed_off_or_landed_pull_is_not_reviewed(self):
        out, rv = self._step(self._classes(release=[("product", "r", "1", _st())],
                                           landed=[("product", "r", "2", _st())]))
        self.assertEqual(out, [])
        rv.assert_not_called()

    def test_off_without_a_local_clone(self):
        with mock.patch("prtriage.local_clone.portal_root", return_value=None), \
                mock.patch.object(D, "review") as rv:
            self.assertEqual(A.double_agent_step(self._classes(
                waiting=[("product", "r", "1", _st())]), "t"), [])
        rv.assert_not_called()

    def _board(self, apply):
        from prtriage import github_writes
        st = _st()
        c = self._classes(waiting=[("product", "product", "107", st)])
        rec = _rec(CONCERN, PASS)
        with mock.patch.object(A, "classify", return_value=c), \
                mock.patch.object(A, "double_agent_step",
                                  return_value=[("product", "product", "107", st,
                                                 rec)]), \
                mock.patch.object(github_writes, "_comment", return_value=True) as cm, \
                mock.patch.object(D, "ledger") as lg:
            A.board_step([], "t", apply, "example-org/product", False, lambda q: "")
        return cm, lg, st

    def test_an_old_layout_on_the_current_head_is_relaid(self):
        st = _st(posted=["c" * 40])
        st["da_comments"] = {"c" * 40: (77, 1)}
        new = _st(head="e" * 40, posted=["e" * 40])
        new["da_comments"] = {"e" * 40: (78, D.LAYOUT)}
        c = self._classes(waiting=[("product", "r", "1", st), ("product", "r", "2", new)])
        with mock.patch.object(D, "cached", return_value=_rec(PASS, PASS)):
            got = A.double_agent_relayout(c)
        self.assertEqual([(x[2], x[3]) for x in got], [("1", 77)])

    def _relayout_board(self, apply):
        from prtriage import github_writes
        c = self._classes(waiting=[("product", "product", "1234", _st())])
        row = ("product", "product", "1234", 77, _rec(CONCERN, PASS))
        with mock.patch.object(A, "classify", return_value=c), \
                mock.patch.object(A, "double_agent_step", return_value=[]), \
                mock.patch.object(A, "double_agent_relayout", return_value=[row]), \
                mock.patch.object(github_writes, "_edit_comment", return_value=True) as ed:
            A.board_step([], "t", apply, "example-org/product", False, lambda q: "")
        return ed

    def test_a_relayout_edits_in_place_without_a_mention(self):
        ed = self._relayout_board(True)
        self.assertEqual(ed.call_args[0][2], 77)
        self.assertNotIn("@", ed.call_args[0][3])

    def test_a_dry_run_relays_nothing(self):
        self._relayout_board(False).assert_not_called()

    def test_the_ask_carries_the_review_line(self):
        from prtriage import github_writes
        st = _st()
        st["authors"] = ["dev"]
        c = self._classes(ask=[("product", "product", "107", st)])
        with mock.patch.object(A, "classify", return_value=c), \
                mock.patch.object(A, "double_agent_step",
                                  return_value=[("product", "product", "107", st,
                                                 _rec(CONCERN, PASS))]), \
                mock.patch.object(A, "double_agent_relayout", return_value=[]), \
                mock.patch.object(github_writes, "_comment", return_value=True) as cm, \
                mock.patch.object(D, "ledger"):
            A.board_step([], "t", True, "example-org/product", False, lambda q: "")
        ask, review = cm.call_args_list[0][0][3], cm.call_args_list[1][0][3]
        self.assertIn("raised a concern; see the double-agent review", ask)
        self.assertIn("@dev 🕵", review)

    def test_apply_posts_the_review_and_logs_it(self):
        cm, lg, st = self._board(True)
        body = cm.call_args[0][3]
        self.assertIn("prtriage-double-agent: " + "c" * 40, body)
        self.assertIn("@dev 🕵", body)
        lg.assert_called_once()
        self.assertIn("c" * 40, st["da_heads"])

    def test_a_dry_run_posts_nothing(self):
        cm, lg, _st2 = self._board(False)
        cm.assert_not_called()
        lg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
