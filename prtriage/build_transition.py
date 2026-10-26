"""A build's tests partitioned by outcome, read once from the report service.

The re-run reconciliation compares a pull's latest run with its earlier runs. This is the read
it needs: every case of a build, keyed on (name, component), split into passed, failed and
not-run, with shard and batch rows left out.

Extracted from the production tool; the functions below are the real implementation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Tuple

TestKey = Tuple[str, str]

_BATCH_COMPONENT = "batch"
_SHARD_NAME_RE = re.compile(r"/\d+/\d+$")


def case_key(rec: Dict[str, Any]) -> TestKey:
    name = rec.get("reportCaseName") or rec.get("caseName") or rec.get("name") or ""
    comp = rec.get("reportComponentName") or rec.get("componentName") or ""
    return (name, comp)


def _is_batch_case(name: str, component: str) -> bool:
    if component and component.strip().lower() == _BATCH_COMPONENT:
        return True
    return bool(_SHARD_NAME_RE.search(name))


def _status(rec: Dict[str, Any]) -> str:
    for k in ("status", "reportCaseResultStatus", "caseResultStatus"):
        v = rec.get(k)
        if v:
            return str(v).upper()
    return ""


@dataclass(frozen=True)
class BuildStatuses:
    """A build's tests partitioned by outcome, keyed on (name, component).

    `untested` holds the not-run bucket (ReportService UNTESTED + defensive
    BLOCKED/INCOMPLETE). Batch rows are filtered the same way the routine
    profile filters them (unless include_batches)."""
    build_id: str
    passed: FrozenSet[TestKey]
    failed: FrozenSet[TestKey]
    untested: FrozenSet[TestKey]

    @property
    def all_tests(self) -> FrozenSet[TestKey]:
        return self.passed | self.failed | self.untested

    @property
    def ran(self) -> int:
        return len(self.passed) + len(self.failed)


# --------------------------------------------------------------------------
# Network boundary - read a build's per-status test sets (kept thin, out of the
# pure math above so the transition logic tests without a ReportService connection).
# --------------------------------------------------------------------------
def read_build_statuses(
    client: Any, build_id: str, *, include_batches: bool = False,
) -> BuildStatuses:
    """Read a build's cases once and partition them by outcome, batch-filtered
    like the routine profile. PASSED→passed, FAILED→failed, everything else
    (UNTESTED/BLOCKED/INCOMPLETE/blank) → the not-run `untested` bucket."""
    passed: set = set()
    failed: set = set()
    untested: set = set()
    for rec in client.iter_build_cases(str(build_id)):
        key = case_key(rec)
        if not include_batches and _is_batch_case(key[0], key[1]):
            continue
        st = _status(rec)
        if st == "FAILED":
            failed.add(key)
        elif st == "PASSED":
            passed.add(key)
        else:
            untested.add(key)
    return BuildStatuses(
        build_id=str(build_id),
        passed=frozenset(passed),
        failed=frozenset(failed),
        untested=frozenset(untested),
    )
