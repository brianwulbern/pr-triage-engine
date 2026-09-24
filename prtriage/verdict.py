"""Verdict synthesis: from classified failures to one confident conclusion.

A condensed version of the production ladder. The real one has dozens of rungs, each added for
a specific false green or false alarm seen in the field. This keeps the rules that carry the
design:

* A compile break in a module the change edits blocks everything, so it is named first.
* A failure the change is responsible for is called that, not hedged.
* A failure in code the change touches that usually passes is not a verdict yet. One red run
  cannot tell a fresh break from a one-off, so the call is "re-run at this commit", stated as
  the missing evidence and how to get it, never as a maybe.
* Base noise (known flakes, infrastructure, failures that also fail on other commits) does not
  implicate the change, EXCEPT when it sits in a module the change edits. There it can hide a
  real break ("masking"), so masked coverage is never called green.
* A run that barely executed anything is not evidence of safety.

The asymmetry is deliberate: calling a real break green is the worst error; blaming the change
for base noise is the second worst. When the evidence is short, the verdict holds and says
exactly what would settle it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from . import config

# Classifier categories that mean "pre-existing / environmental", not caused by this change.
BASE_NOISE = frozenset({
    "consistent_failure", "known_flake", "statistical_flake", "volatile_log_flake",
    "recurring_silent_failure", "infrastructure_flake", "build_step_flake", "build_echo",
    "derivative_test_skip",
})

RED, INVESTIGATE, REVIEW, LEAN_OK, GREEN = "🛑", "🟠", "⚠️", "🟡", "🟢"
LABELS = {RED: "DO NOT LAND", INVESTIGATE: "INVESTIGATE", REVIEW: "REVIEW",
          LEAN_OK: "LIKELY NOT THIS PR", GREEN: "FORWARD-SAFE"}


@dataclass
class Failure:
    name: str
    error: str = ""
    category: Optional[str] = None       # from classifier.Classification.category
    is_unique: bool = True                # classifier: a genuine new failure, not suppressed
    pass_rate: Optional[int] = None       # recent pass rate, 0-100
    module: str = ""
    diff_touches_test: bool = False       # the change edits this test (or its failing method)
    diff_touches_module: bool = False     # the change edits the module the test lives in
    compile_break: bool = False           # the module failed to compile in this run
    blocked: bool = False                 # never ran: an upstream task failed first


@dataclass
class Run:
    pr: int
    failures: List[Failure] = field(default_factory=list)
    executed: int = 0
    expected: int = 0
    behind: int = 0
    authors: List[str] = field(default_factory=list)


@dataclass
class Verdict:
    orb: str
    code: str
    headline: str

    @property
    def sig(self) -> str:
        """The comparable conclusion. The comment engine edits in place while this is
        unchanged and starts a new comment (a paper trail) when it moves."""
        return f"{self.orb}:{self.code}"

    @property
    def implicates_pr(self) -> bool:
        return self.orb in (RED, INVESTIGATE)


def _names(fs: List[Failure], cap: int = 3) -> str:
    shown = ", ".join(f"`{f.name}`" for f in fs[:cap])
    return shown + (f" (+{len(fs) - cap} more)" if len(fs) > cap else "")


def decide(run: Run) -> Verdict:
    """First rung that matches wins. Ordered from most to least decisive."""
    fs = run.failures

    breaks = [f for f in fs if f.compile_break and f.diff_touches_module]
    if breaks:
        mod = breaks[0].module or breaks[0].name
        blocked = sum(1 for f in fs if f.blocked)
        tail = f"; {blocked} downstream test(s) never ran behind it" if blocked else ""
        return Verdict(RED, "ci_compile_break",
                       f"the build fails to compile `{mod}`, a module this change edits, so this "
                       f"is the change, not the base{tail}. Fix the compile error first.")

    attributed = [f for f in fs if f.is_unique and f.diff_touches_test]
    if attributed:
        return Verdict(RED, "diff_attributed",
                       f"{_names(attributed)} fail(s) in code this change edits and is not "
                       f"failing on other commits. Fix before landing.")

    in_module = [f for f in fs if f.is_unique and f.diff_touches_module]
    reliable = [f for f in in_module
                if f.pass_rate is not None and f.pass_rate >= config.RELIABLE_PASS_RATE]
    if reliable:
        return Verdict(REVIEW, "rerun_to_decide",
                       f"not ready to forward: {_names(reliable)} failed in a module this change "
                       f"edits and usually passes ({reliable[0].pass_rate}% recently). One run "
                       f"can't tell a fresh break from a one-off: re-run at this commit. A clean "
                       f"pass clears it, a repeat confirms the break.")
    if in_module:
        return Verdict(INVESTIGATE, "module_unattributed",
                       f"{_names(in_module)} failed in a module this change edits, with no "
                       f"history showing it fails elsewhere. Likely this change; verify before "
                       f"assuming otherwise.")

    masked = [f for f in fs if not f.is_unique and f.diff_touches_module
              and (f.blocked or (f.category in BASE_NOISE))]
    if masked:
        return Verdict(REVIEW, "masked_by_base",
                       f"not ready to forward: {len(masked)} base failure(s) sit in modules this "
                       f"change edits ({_names(masked)}), so they can hide a real break. Re-run "
                       f"once the base is green for those modules.")

    if run.expected and run.executed / run.expected < config.MIN_COVERAGE:
        return Verdict(REVIEW, "inconclusive_coverage",
                       f"not ready to forward: only {run.executed} of {run.expected} expected "
                       f"tests ran, so zero failures proves nothing yet. Re-run for full coverage.")

    unrelated = [f for f in fs if f.is_unique]
    if unrelated:
        return Verdict(LEAN_OK, "unrelated_unique",
                       f"{_names(unrelated)} failed outside anything this change touches. Very "
                       f"likely base drift; a re-run confirms.")

    if fs:
        return Verdict(GREEN, "base_noise",
                       f"every failure is known base noise ({len(fs)} suppressed: flaky, "
                       f"environmental, or failing on other commits too) outside the code this "
                       f"change touches.")
    return Verdict(GREEN, "clean", "no failures.")


def render_comment(run: Run, v: Verdict) -> str:
    """The PR comment body. Greets the authors only when the verdict asks something of them.
    Ends with the hidden signature the comment engine compares. No em-dashes: it is posted."""
    lines = []
    if v.implicates_pr and run.authors:
        mentions = " ".join(f"@{a}" for a in run.authors)
        lines.append(f"Hi {mentions}, can you take a look at the verdict below? It needs a "
                     f"change to the PR itself before landing.")
        lines.append("")
    lines.append(f"**PR#{run.pr}**: {v.orb} {LABELS[v.orb]}: {v.headline}")
    lines.append("")
    lines.append("<details>")
    lines.append("<summary>Evidence</summary>")
    lines.append("")
    for f in run.failures:
        tag = f.category or ("unique" if f.is_unique else "suppressed")
        rate = f" ({f.pass_rate}% recent pass)" if f.pass_rate is not None else ""
        lines.append(f"- `{f.name}`: {tag}{rate}")
    if run.expected:
        lines.append(f"- coverage: {run.executed} of {run.expected} expected tests ran")
    lines.append(f"ℹ️ Branch is {run.behind} commit(s) behind base.")
    if v.orb != GREEN and run.behind > config.STALE_REBASE_BEHIND_THRESHOLD:
        lines.append("⚠️ That is past the rebase-needed line: rebase before re-running.")
    lines.append("")
    lines.append("</details>")
    lines.append(f"<!-- prtriage-sig: {v.sig} -->")
    return "\n".join(lines)
