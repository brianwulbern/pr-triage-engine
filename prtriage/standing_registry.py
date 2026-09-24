"""Standing-failure registry (#6) - de-escalate the review residue that recurs.

A1 (`_cross_pr_standing_clear`) clears a failure red across ≥4 builds *in a single
triage's live history window*. But the audit corpus shows failures that recur across
many DISTINCT PRs spread over time - the same test failing under unrelated diffs - that
a single window misses. A test failing under K unrelated diffs can't be caused by any
one of them: it's standing on the base branch, not introduced by the PR under triage.

This module distills that recurrence from the on-disk audit corpus into a registry
(`{test_name: distinct_pr_count}` for names failing in ≥ MIN_PRS distinct PRs) that the
verdict ladder consults as a last-chance de-escalator before the characterized hedge.

Design (this de-escalator can SUPPRESS a failure, so it's conservative):
  * Corpus-backed, not self-referential - evidence is "failed under N unrelated diffs",
    the same soundness A1 rests on, just durable across time instead of one window.
  * OFF when the registry file is absent (built explicitly by `prtriage standing-registry`).
  * High bar (MIN_PRS, default 5 - above A1's 4) so a 2–3 PR coincidence never clears.
  * The verdict is a soft 🟡 lean with a "confirm still-standing" caveat - the signal is
    historical, so it points the human at the disambiguator rather than asserting green.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
from collections import defaultdict
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# Default: a failure must have failed in this many DISTINCT PRs across the corpus to
# be registered as standing. Above A1's CROSS_PR_STANDING_MIN_BUILDS (4) on purpose.
MIN_PRS = 5

# Names that are never a single "test" to register - build/compile steps, the
# smoke/canary aggregates, and gradle task paths (`:apps:…:packageRunTest`, which are
# infra/cascade failures with their own upstream handling, not the test residue this
# de-escalator targets). Registrable = a real test identity: a `.spec.ts` spec, or a
# Java-style FQN whose last segment is a Capitalized class name.
_NON_TEST = re.compile(r"^(modules-compile|service-builder|semantic-versioning|"
                       r"modules-compile-jdk8)\[")
_SPEC = re.compile(r"\.spec\.ts")


def _is_registrable(name: str) -> bool:
    if not name:
        return False
    if _NON_TEST.match(name):
        return False
    if "SmokeTest" in name or name.startswith("LocalFile."):
        return False
    if _SPEC.search(name):
        return True
    # Java-style FQN with a Capitalized class segment - excludes gradle `:task`
    # paths (no dots) and bare tokens.
    segs = name.split("#", 1)[0].split(".")
    return len(segs) >= 2 and segs[-1][:1].isupper()


def build_registry(paths: List[str], min_prs: int = MIN_PRS) -> Dict[str, int]:
    """Scan the audit JSONL corpus → {test_name: distinct_pr_count} for names failing
    in ≥ min_prs distinct PRs. A record's PR is its result.pr_url / _audit.target."""
    pr_of_name: Dict[str, set] = defaultdict(set)
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    res = rec.get("result") or {}
                    pr = res.get("pr_url") or (rec.get("_audit") or {}).get("target")
                    if not pr:
                        continue
                    # 2.41.0: a --pr-level audit row carries `unioned_failures`
                    # (to_json) rather than the per-build `unique_failures`; read
                    # either so a PR-level open sweep also feeds the registry.
                    fails = (res.get("unique_failures")
                             or res.get("unioned_failures") or [])
                    for uf in fails:
                        name = (uf.get("name") or uf.get("reportCaseName") or "").strip()
                        if _is_registrable(name):
                            pr_of_name[name].add(pr)
        except OSError as exc:
            log.warning("standing-registry: can't read %s: %s", path, exc)
    return {n: len(prs) for n, prs in pr_of_name.items() if len(prs) >= min_prs}


def _default_registry_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "prtriage", "standing-registry.json")


# process-local memo so a batch/PR-level run loads the file once.
_LOADED: Optional[Dict[str, int]] = None
_LOADED_PATH: Optional[str] = None


def _reset_cache() -> None:  # test hook
    global _LOADED, _LOADED_PATH
    _LOADED, _LOADED_PATH = None, None


def load_registry(path: Optional[str] = None) -> Dict[str, int]:
    """Load the registry file (memoized), or {} when absent/unreadable - the feature
    is simply off until `prtriage standing-registry` has written it."""
    global _LOADED, _LOADED_PATH
    path = path or _default_registry_path()
    if _LOADED is not None and _LOADED_PATH == path:
        return _LOADED
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = {str(k): int(v) for k, v in json.load(fh).items()}
    except (OSError, ValueError, TypeError):
        data = {}
    _LOADED, _LOADED_PATH = data, path
    return data


def standing_pr_count(name: Optional[str], registry: Optional[Dict[str, int]] = None,
                      min_prs: int = MIN_PRS) -> int:
    """The distinct-PR count for this test in the registry (0 if not standing).
    Matches the failure name as stored in the corpus."""
    if not name:
        return 0
    reg = registry if registry is not None else load_registry()
    n = reg.get(name.strip(), 0)
    return n if n >= min_prs else 0


def run_build_registry(argv: List[str]) -> int:
    """`prtriage standing-registry` - (re)build the registry from the audit corpus."""
    import argparse
    p = argparse.ArgumentParser(
        prog="prtriage standing-registry",
        description="Distill recurring test failures from the audit corpus into the "
                    "standing-failure registry the verdict ladder consults.")
    p.add_argument("--dir", default=".", help="Directory of audit JSONL files.")
    p.add_argument("--glob", default="prtriage-audit_*.jsonl", help="Audit file glob.")
    p.add_argument("--min-prs", type=int, default=MIN_PRS,
                   help=f"Min distinct PRs to register a failure (default {MIN_PRS}).")
    p.add_argument("--out", default=None, help="Registry path (default: XDG cache).")
    args = p.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(args.dir, args.glob)))
    if not paths:
        log.error("standing-registry: no files match %s in %s", args.glob, args.dir)
        return 1
    reg = build_registry(paths, args.min_prs)
    out = args.out or _default_registry_path()
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(reg, fh, indent=0, sort_keys=True)
    except OSError as exc:
        log.error("standing-registry: can't write %s: %s", out, exc)
        return 1
    log.info("📕 standing-registry: %d recurring failure(s) (≥%d PRs) across %d file(s) "
             "→ %s", len(reg), args.min_prs, len(paths), out)
    top = sorted(reg.items(), key=lambda kv: kv[1], reverse=True)[:10]
    for name, cnt in top:
        log.info("   %3d PRs  %s", cnt, name[:80])
    return 0
