"""1.38.0 - cross-run reliability reconciliation for PR-level triage.

A PR trigger (e.g. `ci:test:relevant`) is often built more than once - a
reschedule/re-run on the same commit, or a fresh run after a fix is pushed.
Triaging only the LATEST build (comments.latest_per_trigger) throws that
history away, but comparing a test's outcome ACROSS re-runs is the strongest
reliability signal there is - stronger than historical pass-rate, because it's
literally the same code (when the commit matches).

The user's rule (authoritative):
  • a test that FAILS in an earlier run but PASSES in the latest → GREEN LIGHT
    (resolved - a settled flake at the same commit, or a fix at a newer one).
  • a test that PASSES in an earlier run but FAILS in the latest, AT THE SAME
    COMMIT → flaky/unrelated (same code passed then failed → non-deterministic),
    de-weighted from diff-attribution.
  • the LATEST run is authoritative for the current pass/fail state; the history
    only classifies reliability.

Same-commit identity is `BuildMetadata.git_hash` (a real per-build SHA - verified
present on product PR builds); `branch_ahead_by/behind_by` is triage-time-global
and useless per-build. When a hash is missing we withhold the same-commit
conclusions rather than guess (the strict degrade the user chose).

Pure math (`reconcile_runs`) + a thin best-effort network boundary
(`reconcile_trigger`), mirroring build_transition.py. Reads raw statuses only -
no history, no classification, no diff scoring for the prior runs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .build_transition import read_build_statuses

# ── per-test cross-run verdicts (for a test FAILING in the latest run) ────────
RECON_REPRODUCED = "reproduced"          # failed latest AND failed the comparable earlier run → real
RECON_FLAKED_SAME_COMMIT = "flaked_same_commit"      # failed latest, PASSED earlier at SAME commit → flaky
RECON_NEWLY_FAILING_CROSS_COMMIT = "newly_failing_cross_commit"  # passed earlier at a DIFFERENT commit
RECON_NO_DATA = "no_data"                # no comparable earlier outcome (untested/absent/unknown commit)
# 3.0.48 (#1234): passed earlier at the same commit, but a compile gate red in that run is
# green now, so the earlier run didn't build the code that fails now
RECON_NEWLY_FAILING_REBUILT = "newly_failing_rebuilt"

# ── green-light verdicts (for a test that FAILED earlier but PASSES latest) ───
GREEN_SETTLED_SAME_COMMIT = "green_settled_same_commit"  # fail→pass, same commit → settled flake
GREEN_FIX_CONFIRMED = "green_fix_confirmed"              # fail→pass, newer commit → fix landed
GREEN_UNKNOWN_COMMIT = "green_unknown_commit"            # fail→pass, commit unknown

_DEWEIGHTED = {RECON_FLAKED_SAME_COMMIT}


_COMPILE_GATE_RE = re.compile(r"^[\w-]*compile[\w-]*\[")


def _gate_fixed_since(prev: "RunStatuses", latest: "RunStatuses") -> bool:
    return any(s == "F" and _COMPILE_GATE_RE.match(n) and latest.by_name.get(n) != "F"
               for n, s in prev.by_name.items())


def _same_commit(a: Optional[str], b: Optional[str]) -> Optional[bool]:
    """True/False when both hashes are present; None when either is unknown
    (strict: never guess commit identity)."""
    if a and b:
        return a == b
    return None


@dataclass(frozen=True)
class RunStatuses:
    """One build's per-test outcome, collapsed across components to a single
    status per test name: 'P' (passed, never failed), 'F' (failed in ≥1
    component), or absent (not run / untested)."""
    build_id: str
    git_hash: Optional[str]
    by_name: Dict[str, str]


@dataclass
class TriggerReconciliation:
    """The cross-run reconciliation for ONE trigger's builds."""
    trigger_key: str
    run_ids: List[str]                       # ascending (oldest → newest)
    same_commit: Optional[bool]              # latest vs the most-recent earlier run
    latest_failures: Dict[str, str] = field(default_factory=dict)  # name → RECON_*
    green_lights: List[Tuple[str, str]] = field(default_factory=list)  # (name, GREEN_*)

    @property
    def n_runs(self) -> int:
        return len(self.run_ids)


def _collapse(statuses: "BuildStatuses") -> Dict[str, str]:  # noqa: F821
    """Collapse a BuildStatuses (keyed on (name, component)) to name → 'P'/'F'.
    F wins over P (a name that failed in any component is failing); a name only
    in `passed` is 'P'; untested-only names are omitted (no signal)."""
    by_name: Dict[str, str] = {}
    for name, _comp in statuses.passed:
        by_name.setdefault(name, "P")
    for name, _comp in statuses.failed:
        by_name[name] = "F"   # override: failed-anywhere wins
    return by_name


def reconcile_runs(runs: List[RunStatuses]) -> TriggerReconciliation:
    """Pure reconciliation over temporally-ordered runs (ascending; the last is
    the authoritative latest). Classifies every test failing in the latest run,
    and collects green-lights (failed earlier, passes latest)."""
    latest = runs[-1]
    earlier = runs[:-1]                       # ascending; most-recent-earlier = earlier[-1]
    recon = TriggerReconciliation(
        trigger_key="",
        run_ids=[r.build_id for r in runs],
        same_commit=_same_commit(latest.git_hash, earlier[-1].git_hash) if earlier else None,
    )

    # Tests FAILING in the latest run: classify against the most-recent earlier
    # run that has a definitive (P/F) outcome for the test.
    for name, st in latest.by_name.items():
        if st != "F":
            continue
        verdict = RECON_NO_DATA
        for prev in reversed(earlier):
            pst = prev.by_name.get(name)
            if pst not in ("P", "F"):
                continue                       # untested/absent earlier → keep looking back
            if pst == "F":
                verdict = RECON_REPRODUCED
            else:                              # PASSED earlier, FAILS now
                same = _same_commit(latest.git_hash, prev.git_hash)
                if same is True and _gate_fixed_since(prev, latest):
                    verdict = RECON_NEWLY_FAILING_REBUILT
                elif same is True:
                    verdict = RECON_FLAKED_SAME_COMMIT
                elif same is False:
                    verdict = RECON_NEWLY_FAILING_CROSS_COMMIT
                else:
                    verdict = RECON_NO_DATA    # unknown commit → strict, no de-weight
            break
        recon.latest_failures[name] = verdict

    # Green-lights: tests that FAILED in some earlier run but PASS in the latest.
    earlier_failed = {n for r in earlier for n, s in r.by_name.items() if s == "F"}
    for name in earlier_failed:
        if latest.by_name.get(name) != "P":
            continue
        gv = GREEN_UNKNOWN_COMMIT
        for prev in reversed(earlier):
            if prev.by_name.get(name) == "F":
                same = _same_commit(latest.git_hash, prev.git_hash)
                gv = (GREEN_SETTLED_SAME_COMMIT if same is True
                      else GREEN_FIX_CONFIRMED if same is False
                      else GREEN_UNKNOWN_COMMIT)
                break
        recon.green_lights.append((name, gv))

    return recon


def reconcile_trigger(client: Any, tb: Any) -> Optional[TriggerReconciliation]:
    """Network boundary: read statuses for a trigger's latest build + its
    most-recent prior runs (capped by RECONCILE_MAX_RUNS_PER_TRIGGER), then
    reconcile. Returns None when the trigger has no prior runs (the common case
    → zero added calls) or on any error (best-effort - never worse than a
    single-build read).

    `tb` is a pr_triage.TriggerBuild: build_id, git_hash, and prior_runs =
    ((build_id, git_hash), …) ascending."""
    prior = list(getattr(tb, "prior_runs", ()) or ())
    if not prior:
        return None
    # Keep the most-recent (N-1) priors; append the latest as the final run.
    keep = prior[-(config.RECONCILE_MAX_RUNS_PER_TRIGGER - 1):]
    ordered = list(keep) + [(tb.build_id, getattr(tb, "git_hash", None))]
    try:
        runs: List[RunStatuses] = []
        for bid, ghash in ordered:
            statuses = read_build_statuses(client, str(bid), include_batches=True)
            runs.append(RunStatuses(build_id=str(bid), git_hash=ghash,
                                    by_name=_collapse(statuses)))
    except Exception:  # noqa: BLE001 - reconciliation is best-effort
        return None
    recon = reconcile_runs(runs)
    recon.trigger_key = getattr(tb, "trigger_key", "")
    return recon
