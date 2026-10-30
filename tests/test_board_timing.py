import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from prtriage import board_timing as B


class BoardTiming(unittest.TestCase):
    def setUp(self):
        B._SPANS.clear()
        B._SLOW.clear()

    def test_off_without_the_variable(self):
        with mock.patch.dict(os.environ, {"PRTRIAGE_BOARD_TIMINGS": ""}):
            self.assertFalse(B.install())

    def test_rebind_times_every_importer(self):
        def work(target):
            return target.upper()
        home = types.ModuleType("prtriage._bt_home")
        user = types.ModuleType("prtriage._bt_user")
        home.work = user.work = work
        sys.modules[home.__name__], sys.modules[user.__name__] = home, user
        try:
            B._rebind(home, "work", "call:work", True)
            self.assertIs(home.work, user.work)
            self.assertEqual(user.work("pull/1234"), "PULL/1234")
            B._rebind(home, "work", "call:work", True)
            home.work("pull/1")
        finally:
            del sys.modules[home.__name__], sys.modules[user.__name__]
        self.assertEqual(B._SPANS["call:work"]["calls"], 2)
        self.assertEqual([lbl for _s, lbl in B._SLOW["call:work"]].count("pull/1234"), 1)


    def test_a_failing_call_is_still_timed(self):
        def boom():
            raise ValueError("x")
        with self.assertRaises(ValueError):
            B._wrap(boom, "call:boom", False)()
        self.assertEqual(B._SPANS["call:boom"]["calls"], 1)

    def test_hosts_and_commands(self):
        self.assertEqual(B._host("https://api.github.com/repos/x"), "github")
        self.assertEqual(B._host("https://reports.example.com/o/c/x"), "report")
        self.assertEqual(B._host("https://googleapis.example.com/b/o"), "gcs")
        self.assertEqual(B._host("https://jenkins.example.com/job/x"), "jenkins")
        self.assertEqual(B._cmd(["git", "-C", "/repo", "-c", "a=b", "ls-tree", "-r"]),
                         "git ls-tree")
        self.assertEqual(B._cmd(["/usr/bin/git", "ls-tree", "-r"]), "git ls-tree")
        self.assertEqual(B._cmd(["/home/me/.local/bin/claude", "-p"]), "claude")

    def test_write_reports_spans(self):
        B._add("phase:run_targets", 12.5)
        B._add("http:github", 0.4)
        B._add("http:github", 0.6)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.json")
            with mock.patch("sys.stderr"):
                B.write(p)
            rep = json.load(open(p))
        self.assertEqual(rep["spans"]["http:github"], {"calls": 2, "seconds": 1.0, "max": 0.6})
        self.assertEqual(rep["spans"]["phase:run_targets"]["seconds"], 12.5)


class GitHubEndpoints(unittest.TestCase):
    def setUp(self):
        B._SPANS.clear()

    def test_endpoints_group_numbers_shas_and_tails(self):
        E = B._gh_endpoint
        self.assertEqual(E("https://api.github.com/repos/example-org/product/pulls/41276"),
                         "/repos/example-org/product/pulls/{n}")
        self.assertEqual(E("https://api.github.com/repos/o/r/issues/41276/comments?per_page=100"),
                         "/repos/o/r/issues/{n}/comments")
        self.assertEqual(E("https://api.github.com/repos/o/r/commits/b6e7db2f1e2b/pulls"),
                         "/repos/o/r/commits/{sha}/pulls")
        self.assertEqual(E("https://api.github.com/repos/o/r/contents/modules/a/b.java?ref=x"),
                         "/repos/o/r/contents/{path}")
        self.assertEqual(E("https://api.github.com/repos/o/r/compare/release-1.x...abc1234"),
                         "/repos/o/r/compare/{range}")
        self.assertEqual(E("https://api.github.com/graphql"), "/graphql")

    def test_github_calls_split_by_endpoint_and_304s_are_counted(self):
        url = "https://api.github.com/repos/o/r/pulls/1"
        B._http("get", url, 0.2, 200)
        B._http("GET", url, 0.1, 304)
        B._http("GET", "https://reports.example.com/o/c/x", 0.3, 200)
        self.assertEqual(B._SPANS["gh:GET /repos/o/r/pulls/{n}"]["calls"], 2)
        self.assertEqual(B._SPANS["gh:304 not modified (free)"]["calls"], 1)
        self.assertEqual(B._SPANS["http:github"]["calls"], 2)
        self.assertNotIn("gh:GET /o/c/x", B._SPANS)

    def test_contents_reads_are_tagged_by_ref_kind_and_caller(self):
        base = "https://api.github.com/repos/o/r/contents/a/packageinfo"
        B._http("GET", base + "?ref=" + "a" * 40, 0.1, 200)
        B._http("GET", base + "?ref=release-1.x", 0.1, 404)
        B._http("GET", base, 0.1, 200)
        kinds = sorted(k.split()[0] for k in B._SPANS if k.startswith("ghc:"))
        self.assertEqual(kinds, ["ghc:branch", "ghc:default", "ghc:sha"])
        self.assertTrue(all("test_board_timing" not in k for k in B._SPANS))

    def test_ref_kind_reads_the_request_params(self):
        url = "https://api.github.com/repos/o/r/contents/a/b.java"
        self.assertEqual(B._ref_kind(url, {"ref": "c" * 40}), "sha")
        self.assertEqual(B._ref_kind(url, {"ref": "master"}), "master")
        self.assertEqual(B._ref_kind(url, {"ref": "release-1.x"}), "branch")
        self.assertEqual(B._ref_kind(url, None), "default")
        B._http("GET", url, 0.1, 200, {"ref": "master"})
        self.assertTrue(any(k.startswith("ghc:master ") for k in B._SPANS))

    def test_caller_skips_fetch_source_and_lambdas(self):
        mod = types.ModuleType("prtriage._bt_skip")
        exec("from prtriage import board_timing as B\n"
             "def fetch_source():\n    return (lambda: B._caller())()\n"
             "def asker():\n    return fetch_source()\n", mod.__dict__)
        self.assertTrue(mod.asker().startswith("_bt_skip.asker:"))

    def test_caller_names_the_nearest_trt_frames(self):
        mod = types.ModuleType("prtriage._bt_probe")
        exec("from prtriage import board_timing as B\n"
             "def probe():\n    return B._caller()\n", mod.__dict__)
        mod.__dict__["__name__"] = "prtriage._bt_probe"
        self.assertTrue(mod.probe().startswith("_bt_probe.probe:"))

    def test_status_from_response_or_error(self):
        self.assertEqual(B._status(types.SimpleNamespace(status_code=304), None), 304)
        self.assertEqual(B._status(None, types.SimpleNamespace(code=404)), 404)
        self.assertIsNone(B._status(None, None))
        self.assertEqual(B._cmd(["gh", "api", "repos/o/r"]), "gh api")




if __name__ == "__main__":
    unittest.main()
