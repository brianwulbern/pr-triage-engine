"""1.38.0 - cross-run reliability reconciliation (PR-level).

A PR trigger built more than once at the SAME commit that produces DISJOINT
failure sets across runs is flaky, not diff-caused. #1234 replay: build A had 7
"unique" failures + deep escalate·high ("LayoutTestUtil broke 4+ tests"); build B
(same commit) had 2 completely different failures. None reproduced → the whole
PR de-escalates and the false-high deep escalate is suppressed.

User's rule: fail→pass = green light (latest wins); pass→fail same-commit = flaky
(de-weighted). Latest run is authoritative for current state; history classifies.
"""
import unittest

from types import SimpleNamespace

from prtriage import config
from prtriage import run_reconciliation as rr


def _rs(build_id, git_hash, **statuses):
    """RunStatuses from name=status kwargs (status one of 'P'/'F')."""
    return rr.RunStatuses(build_id=build_id, git_hash=git_hash, by_name=dict(statuses))


# ── pure reconciliation math ──────────────────────────────────────────────
class ReconcileRuns(unittest.TestCase):
    def test_fail_then_pass_same_commit_is_green_settled(self):
        runs = [_rs("1", "aa", T="F"), _rs("2", "aa", T="P")]
        r = rr.reconcile_runs(runs)
        self.assertNotIn("T", r.latest_failures)      # not failing in latest
        self.assertIn(("T", rr.GREEN_SETTLED_SAME_COMMIT), r.green_lights)

    def test_fail_then_pass_different_commit_is_fix_confirmed(self):
        runs = [_rs("1", "aa", T="F"), _rs("2", "bb", T="P")]
        r = rr.reconcile_runs(runs)
        self.assertIn(("T", rr.GREEN_FIX_CONFIRMED), r.green_lights)

    def test_pass_then_fail_same_commit_is_flaked(self):
        runs = [_rs("1", "aa", T="P"), _rs("2", "aa", T="F")]
        r = rr.reconcile_runs(runs)
        self.assertEqual(r.latest_failures["T"], rr.RECON_FLAKED_SAME_COMMIT)

    def test_pass_then_fail_different_commit_is_cross_commit(self):
        runs = [_rs("1", "aa", T="P"), _rs("2", "bb", T="F")]
        r = rr.reconcile_runs(runs)
        self.assertEqual(r.latest_failures["T"], rr.RECON_NEWLY_FAILING_CROSS_COMMIT)

    def test_fail_then_fail_is_reproduced(self):
        runs = [_rs("1", "aa", T="F"), _rs("2", "aa", T="F")]
        r = rr.reconcile_runs(runs)
        self.assertEqual(r.latest_failures["T"], rr.RECON_REPRODUCED)

    def test_untested_earlier_is_no_data(self):
        runs = [_rs("1", "aa"), _rs("2", "aa", T="F")]     # T absent (untested) in run 1
        r = rr.reconcile_runs(runs)
        self.assertEqual(r.latest_failures["T"], rr.RECON_NO_DATA)

    def test_unknown_commit_pass_then_fail_is_no_data(self):
        # strict: never de-weight on unknown commit identity.
        runs = [_rs("1", None, T="P"), _rs("2", "aa", T="F")]
        r = rr.reconcile_runs(runs)
        self.assertEqual(r.latest_failures["T"], rr.RECON_NO_DATA)

    def test_latest_run_is_authoritative(self):
        # F, P, F → still failing now; the middle pass doesn't clear it.
        runs = [_rs("1", "aa", T="F"), _rs("2", "aa", T="P"), _rs("3", "aa", T="F")]
        r = rr.reconcile_runs(runs)
        self.assertEqual(r.latest_failures["T"], rr.RECON_FLAKED_SAME_COMMIT)


class Collapse(unittest.TestCase):
    def test_failed_component_wins_over_passed(self):
        from prtriage.build_transition import BuildStatuses
        bs = BuildStatuses(build_id="1",
                           passed=frozenset({("T", "compA")}),
                           failed=frozenset({("T", "compB")}),
                           untested=frozenset())
        self.assertEqual(rr._collapse(bs)["T"], "F")


# ── network boundary ──────────────────────────────────────────────────────
class _Client:
    """iter_build_cases returns pre-seeded records per build."""
    def __init__(self, per_build):
        self._per_build = per_build       # build_id -> [(name, status), ...]
        self.calls = []

    def iter_build_cases(self, build_id):
        self.calls.append(build_id)
        for name, status in self._per_build.get(str(build_id), []):
            yield {"reportCaseName": name, "reportComponentName": "", "status": status}


def _tb(build_id, git_hash, prior_runs=()):
    return SimpleNamespace(build_id=build_id, trigger_key="ci:test:relevant",
                                  build_name="", git_hash=git_hash, prior_runs=prior_runs)


class ReconcileTrigger(unittest.TestCase):
    def test_single_run_returns_none_no_calls(self):
        c = _Client({})
        self.assertIsNone(rr.reconcile_trigger(c, _tb("2", "aa")))
        self.assertEqual(c.calls, [])

    def test_two_runs_same_commit_flake(self):
        c = _Client({"1": [("T", "PASSED")], "2": [("T", "FAILED")]})
        recon = rr.reconcile_trigger(c, _tb("2", "aa", prior_runs=(("1", "aa"),)))
        self.assertIsNotNone(recon)
        self.assertEqual(recon.latest_failures["T"], rr.RECON_FLAKED_SAME_COMMIT)
        self.assertTrue(recon.same_commit)

    def test_cap_limits_prior_fetches(self):
        # 5 priors, cap=3 → only latest + 2 priors fetched.
        c = _Client({str(i): [] for i in range(1, 7)})
        priors = tuple((str(i), "aa") for i in range(1, 6))
        rr.reconcile_trigger(c, _tb("6", "aa", prior_runs=priors))
        self.assertEqual(len(c.calls), config.RECONCILE_MAX_RUNS_PER_TRIGGER)

    def test_error_is_best_effort_none(self):
        class Boom:
            def iter_build_cases(self, _):
                raise RuntimeError("429")
        self.assertIsNone(rr.reconcile_trigger(Boom(), _tb("2", "aa", prior_runs=(("1", "aa"),))))


















if __name__ == "__main__":
    unittest.main()
