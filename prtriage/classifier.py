"""Pure-function classification of test failures as unique vs known-flake.

This module has zero HTTP / IO dependencies, so its logic can be unit-tested
with static fixtures.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import config

log = logging.getLogger("prtriage.classifier")


# Recognizes build-step pseudo-test names like `modules-compile[modules/apps/layout]`
# or `service-builder[core-impl]`. Kept local to the classifier so this module
# remains free of imports from diff_analyzer.
_BUILD_STEP_RE = re.compile(r"^(service-builder|modules-compile(?:-jdk\d+)?)\[")


# Recognizes the "this :test task was skipped because something upstream
# failed" message that Gradle emits on derivative failures. Format:
# `:apps:search:search-web:test was not executed. Please
# check the logs for details.`
#
# We require the colon-separated Gradle task path AND the literal `:test`
# at the end so the regex doesn't match unrelated uses of "was not
# executed" (e.g. in an assertion message about user actions).
_DERIVATIVE_SKIP_PATTERN = re.compile(
    r":[a-z][a-z0-9\-_:]*:test was not executed",
    re.IGNORECASE,
)

# 2.27.0 (perf): the BROADER Gradle task-cascade signature - ANY downstream task
# (`:test`, `:testIntegration`, `:buildService`, …) reporting "was not executed"
# because an upstream task (usually a `modules-compile[…]` failure) failed. This is a
# superset of _DERIVATIVE_SKIP_PATTERN (the `:test` subset). A blocked task never ran,
# so its OWN history (from when it did run elsewhere) is irrelevant to this build - so
# _triage_one uses this to SKIP both the case-id and history fetches, classifying the
# row from the error text alone. That's the request-count cut a rate-limit-bound tool
# needs: a compile-cascade meltdown (#1234: ~136 blocked rows) paid two round-trips
# per blocked row for history it then ignored. Same colon-task guard as the derivative
# pattern so it never false-matches prose like "the test was not executed because …".
_BLOCKED_CASCADE_PATTERN = re.compile(
    r":[a-z][a-z0-9\-_:]*:[a-zA-Z]\w* was not executed",
    re.IGNORECASE,
)


def is_blocked_cascade(error_str: Optional[str]) -> bool:
    """True when the error is a Gradle task-cascade 'was not executed' - the row is
    fallout from an upstream failure and never ran (see _BLOCKED_CASCADE_PATTERN)."""
    return bool(_BLOCKED_CASCADE_PATTERN.search(error_str or ""))


def is_derivative_skip(error_str: Optional[str]) -> bool:
    """True for the `:test`-task subset of a blocked cascade - kept as its own
    (uncounted) derivative_test_skip category, distinct from the counted cascade."""
    return bool(_DERIVATIVE_SKIP_PATTERN.search(error_str or ""))


def _is_build_step_name(test_name: str) -> bool:
    return bool(_BUILD_STEP_RE.match(test_name))


def detect_infrastructure_failure(error_text: str) -> Optional[str]:
    """Recognize failures whose root cause is the test environment, not
    the code under test.

    Returns the diagnostic label from the matching INFRASTRUCTURE_PATTERNS
    entry, or None for code-level errors that should be treated normally.

    Why this lives in the classifier and not in diff_analyzer or in the
    reporter: the same axis as classify_failure (does this failure carry
    code-level signal?) but a different question (is the *signal itself*
    trustworthy, or is the build environment broken?). A true_unique
    failure caused by an infrastructure issue is still a real failure;
    it just shouldn't be diff-blamed because the diff didn't cause it.
    The reporter uses this as a render hint (🌐 vs 🚨) and a banner
    aggregation point - the classification itself is unchanged.
    """
    if not error_text:
        return None
    for pattern, diagnostic in config.INFRASTRUCTURE_PATTERNS:
        if pattern.search(error_text):
            return diagnostic
    return None


def is_critical_test(test_name: Optional[str]) -> bool:
    """0.15.0: True if this test is a canary whose failure warrants
    investigation regardless of diff attribution.

    Lives in the classifier alongside detect_infrastructure_failure
    because it answers a parallel question - the same "should the
    reporter signal-boost this failure?" axis, just keyed off the test
    name rather than the error text. Patterns come from
    `config.CRITICAL_TEST_PATTERNS` and are matched with `re.search`
    against the full test name, so a single pattern like `\\bSmokeTest\\b`
    matches every environment-prefixed variant (`LocalFile.SmokeTest#Smoke`,
    `Tomcat.SmokeTest#Smoke`, etc.).

    Empty/None input returns False - defensive for callers that hand us
    a missing reportCaseName.
    """
    if not test_name:
        return False
    for pattern in config.CRITICAL_TEST_PATTERNS:
        if pattern.search(test_name):
            return True
    return False


def normalize_error(err_str: Any) -> str:
    """Collapse an error message to lowercase-alphabetic letters for fingerprinting.

    This strips line numbers, hashes, timestamps and whitespace so that "the
    same error" at different lines still compares equal.
    """
    if not err_str:
        return ""
    return re.sub(r"[^a-zA-Z]", "", str(err_str).lower())


def is_same_error(err1: str, err2: str) -> bool:
    """Decide whether two normalized error fingerprints represent the same failure."""
    if not err1 and not err2:
        return True
    if not err1 or not err2:
        return False

    # Prefix-substring match - but only when the prefix is long enough to be
    # meaningful. Short fingerprints like "timeout" or "npe" would otherwise
    # match huge portions of the history.
    min_len = config.MIN_SUBSTRING_MATCH_LEN
    p1, p2 = err1[:150], err2[:150]
    if len(p1) >= min_len and p1 in err2:
        return True
    if len(p2) >= min_len and p2 in err1:
        return True

    # Fall back to a ratio-based fuzzy match for rearranged stacks.
    return difflib.SequenceMatcher(None, err1, err2).ratio() >= config.SIMILARITY_THRESHOLD


def _run_status(run: Dict[str, Any]) -> str:
    return str(
        run.get("reportStatusItem", {}).get("name") or run.get("status", "")
    ).upper()


def _run_error_text(run: Dict[str, Any]) -> str:
    return str(
        run.get("error")
        or run.get("errors")
        or run.get("errorMessage")
        or run.get("reportStatusItem", {}).get("error")
        or ""
    )


def _run_build_name(run: Dict[str, Any]) -> str:
    return str(
        run.get("buildName")
        or run.get("reportBuildName")
        or run.get("build", {}).get("name", "")
    ).upper()


def _consecutive_failures_count(
    history_data: Dict[str, Any],
    current_case_result_id: Any,
    pr_string: str,
) -> int:
    """Count how many consecutive runs (including the current one) this
    test has been failing, walking back through history until we hit a
    non-FAILED run or run out of history.

    Skips the current run itself (we already know it failed) and same-PR
    re-triggers (not independent signal). Returns 1 for a brand-new
    failure (first independent run failed), 2 if the prior independent
    run also failed, etc.

    Designed for the reporter's "Failed in past N runs" bucketing in
    strict mode - the count tells the user how snowballed the regression
    is.
    """
    count = 1  # the current run is failing (we wouldn't be here otherwise)
    for run in history_data.get("items", []):
        if str(run.get("reportCaseResultId")) == str(current_case_result_id):
            continue
        if pr_string and pr_string in _run_build_name(run):
            continue
        if _run_status(run) == "FAILED":
            count += 1
        else:
            break
    return count


@dataclass
class Classification:
    is_unique: bool
    reason: str
    # Structured category for bucketing suppressed failures in summaries.
    # One of: "consistent_failure", "known_flake", "statistical_flake",
    # "volatile_log_flake", "recurring_silent_failure",
    # "infrastructure_flake", "build_step_flake", "critical_error",
    # "unique", or None if not classified.
    category: Optional[str] = None
    # Overall pass rate across non-PR history, 0-100. None if not computed.
    pass_rate: Optional[int] = None
    # 0.19.8: Pass rate computed over only the most recent N runs
    # (N = config.RECENT_PASS_RATE_WINDOW, default 30) instead of
    # the full history page. Same scale (0-100); None when fewer
    # than _MIN_RECENT_RUNS samples are available.
    #
    # Why this exists separately from pass_rate: the overall
    # pass_rate is averaged across every routine and branch this
    # test ever ran in. A test that passes on master but fails on
    # release-1.x ends up with pass_rate=80% (cross-branch
    # average) even though it always fails on the user's branch.
    # The stratification heuristic ("≥80% = rarely fails = likely
    # regression") then over-promotes these into the HIGH bucket
    # with misleading framing.
    #
    # Recent-window pass rate naturally biases toward whatever
    # branch CI was most active on lately - which on an active
    # release branch is the branch the user cares about. A test
    # currently failing on this branch gets a low recent_pass_rate
    # even if its overall pass_rate is high, and the stratification
    # demotes it out of HIGH appropriately.
    #
    # Tradeoffs noted in CHANGELOG: doesn't perfectly handle
    # branch-aware semantics, just approximates via recency. Real
    # branch filtering would need a per-history-item branch field
    # we'd have to plumb through. Recent-window is the cheaper
    # 80%-accurate fix.
    recent_pass_rate: Optional[int] = None
    # True if the test was also failing in the immediately-prior independent
    # run. Used by the reporter (in strict mode) to separate new regressions
    # from ongoing ones.
    previously_failed: bool = False
    # Number of consecutive runs (including the current) this test has been
    # failing. 1 means "first run failing", 2+ means "ongoing." Used by the
    # reporter to bucket strict-mode failures into "Failed in past N runs"
    # sections so the user can prioritize new regressions over snowballed ones.
    consecutive_failures: int = 1
    # When a failure is rescued from a flake bucket by the post-pass (e.g. a
    # build-step suppressed as a flake but actually caused by the diff),
    # the original classification reason is preserved here for transparency.
    original_reason: Optional[str] = None


def classify_failure(
    history_data: Dict[str, Any],
    current_error: Any,
    current_case_result_id: Any,
    test_name: str,
    pr_string: str,
    strict_mode: bool = False,
) -> Classification:
    """Decide whether a failing test is a genuine unique failure worth flagging.

    When `strict_mode` is True (used for routines that are expected to be
    100% green, like ci:test:stable), the normal "history says this is a
    known flake, suppress it" paths are skipped. As of 0.16.7 strict mode
    also surfaces failures that step 0 (Top Level Build echo), step 0b
    (derivative test skip), and step 1 (infrastructure noise) would
    otherwise suppress - so EVERY red signal makes it into the report's
    streak buckets and nothing gets buried in the "Suppressed as flakes"
    section. The reasons are kept (so the reader can still see "this is
    a build echo" / "this is infra noise") - only the routing changes.
    """
    current_error_str = str(current_error) if current_error else ""

    # 0.16.7: pre-compute the consecutive-failures streak once for the
    # strict-mode path so every early-return below can reference the
    # same value without re-walking history. The actual fetch already
    # happened in the caller; this is a single pass over the result.
    streak_for_strict: int = 1
    if strict_mode:
        streak_for_strict = _consecutive_failures_count(
            history_data, current_case_result_id, pr_string,
        )

    # 0. "Top Level Build" is an echo pseudo-test that fails whenever any
    # other test in the build run fails. It's not an independent signal -
    # in non-strict mode we suppress it unconditionally. In strict mode
    # (0.16.7+) we surface it anyway, with the same descriptive label,
    # because the user explicitly opted into "show me everything red."
    if test_name.strip() == "Top Level Build":
        return Classification(
            is_unique=strict_mode,
            reason=(
                "Top Level Build (echo: fails whenever any other test in "
                "the run fails)"
            ),
            category="build_echo",
            consecutive_failures=streak_for_strict,
        )

    # 0b. Derivative test-skip: when an upstream Gradle task fails (typically
    # a `modules-compile[<module>]` failure), the dependent `:test` tasks
    # for downstream modules report "was not executed. Please check the
    # logs for details." These aren't independent test failures - they're
    # symptoms of the upstream compile breakage that we'd already flag
    # separately. Suppressing them deduplicates the noise, especially on
    # PRs that touch shared dependencies (one compile failure can cause
    # 50+ downstream :test tasks to skip).
    #
    # Same strict-mode override (0.16.7): in strict mode these surface
    # alongside their upstream cause. Slight redundancy is acceptable when
    # the user has explicitly asked to see every signal.
    #
    # Pattern: matches the Gradle colon-separated task syntax, e.g.
    # ":apps:search:search-web:test was not executed".
    # Requires the trailing ":test" task name to avoid false-matching
    # cases like "the test was not executed because the user clicked X"
    # in an assertion failure.
    if _DERIVATIVE_SKIP_PATTERN.search(current_error_str):
        return Classification(
            is_unique=strict_mode,
            reason=(
                "Derivative test skip (a downstream :test task didn't run "
                "because an upstream Gradle task failed - fix the upstream "
                "failure and these will go away)"
            ),
            category="derivative_test_skip",
            consecutive_failures=streak_for_strict,
        )

    # 1. Known infrastructure noise. In non-strict mode, always suppressed
    # (infrastructure flakes are definitionally not test regressions). In
    # strict mode (0.16.7+), surfaced so the user can see them - though
    # they're still labeled as "Ignored Infrastructure/Session Flake" so
    # the read order is "yes this is real, AND it's environmental, AND
    # you asked to see everything."
    for pattern in config.IGNORED_ERROR_PATTERNS:
        if re.search(pattern, current_error_str, re.IGNORECASE | re.DOTALL):
            return Classification(
                is_unique=strict_mode,
                reason="Ignored Infrastructure/Session Flake Detected",
                category="infrastructure_flake",
                consecutive_failures=streak_for_strict,
            )

    # 2. Always-critical phrases → always flag.
    for phrase in config.CRITICAL_ERROR_PHRASES:
        if phrase.lower() in current_error_str.lower():
            return Classification(True, f"Critical Error Detected ('{phrase}')",
                                  category="critical_error")

    # 2a. Always-critical test names → always flag. Some tests (notably
    # `App Server Bundle Builder`) are infrastructure-level checks whose
    # failure ALWAYS indicates a real problem - by design they should
    # never be red. The test name itself is the signal, independent of
    # error content. This catches cases where the error preview gets
    # truncated before the relevant phrase appears, or where a downstream
    # classification (Consistent failure on a low pass-rate test) would
    # otherwise wrongly suppress it.
    test_name_lower = test_name.lower() if test_name else ""
    for pattern in config.CRITICAL_TEST_NAME_PATTERNS:
        if pattern.lower() in test_name_lower:
            return Classification(
                True,
                f"Critical Test Failure ('{pattern}' - infrastructure-level "
                f"failure, never a flake)",
                category="critical_error",
            )

    # 2b. Strict mode short-circuit - skip the history-based suppression
    # paths. Any failure that made it past steps 0-2 is something to flag
    # on a routine where we expect 100% green. Streak was pre-computed
    # at the top of the function (0.16.7) so we just reuse it here
    # instead of walking history a second time.
    if strict_mode:
        return Classification(
            True,
            "Flagged by strict mode (stable routine expects 100% green - "
            "flake-suppression disabled)",
            category="unique",
            previously_failed=streak_for_strict >= 2,
            consecutive_failures=streak_for_strict,
        )

    # 3. Filter history to runs that are independent signal.
    past_runs: List[Dict[str, Any]] = []
    for run in history_data.get("items", []):
        if str(run.get("reportCaseResultId")) == str(current_case_result_id):
            continue
        if pr_string and pr_string in _run_build_name(run):
            # Re-triggers from the same PR aren't independent - they're all
            # measuring the same (buggy or not) code.
            continue
        past_runs.append(run)

    if not past_runs:
        return Classification(True, "No history outside of this PR", category="unique")

    total = len(past_runs)
    pass_count = sum(1 for r in past_runs if _run_status(r) == "PASSED")
    pass_rate = int((pass_count / total) * 100) if total else 0

    # 0.19.8: Compute pass rate over only the most recent N runs.
    # Reuses the same `_run_timestamp` helper that the build-step
    # rescue uses below - defined inline here so the recent-window
    # logic doesn't depend on it being declared in the build-step
    # branch. Identical key-fallback list.
    #
    # We sort past_runs by timestamp descending and take the
    # first N. Defensive against the API returning items in any
    # order - if no items expose a usable timestamp, fall back
    # to using past_runs in API order (newest-first is the
    # documented ReportService contract for this endpoint).
    def _ts(r: Dict[str, Any]) -> str:
        for key in ("dateCreated", "createDate",
                    "modifiedDate", "dateModified"):
            v = r.get(key)
            if v:
                return str(v)
        return ""

    if any(_ts(r) for r in past_runs):
        recent_window = sorted(past_runs, key=_ts, reverse=True)
        recent_window = recent_window[:config.RECENT_PASS_RATE_WINDOW]
    else:
        # No usable timestamps - trust API order, newest-first.
        recent_window = past_runs[:config.RECENT_PASS_RATE_WINDOW]

    recent_total = len(recent_window)
    if recent_total >= config.RECENT_PASS_RATE_MIN_RUNS:
        recent_pass_count = sum(
            1 for r in recent_window if _run_status(r) == "PASSED"
        )
        recent_pass_rate: Optional[int] = int(
            (recent_pass_count / recent_total) * 100
        )
    else:
        # Below floor - recent_pass_rate is None, stratification
        # falls back to overall pass_rate (or no-history bucket
        # if pass_rate is also None).
        recent_pass_rate = None

    # 3b. Build-step pseudo-tests (modules-compile[...], service-builder[...]):
    # these don't have meaningful error fingerprints, but they do have pass/fail
    # history. If the step failed within the recent-runs window, treat it as
    # known-broken and suppress. Otherwise it's a genuinely new breakage.
    if _is_build_step_name(test_name):
        window = config.BUILD_STEP_RECENT_RUNS_WINDOW
        # Try to sort by a plausible timestamp so "recent" actually means recent.
        # ReportService exposes timestamps under a few different keys depending on the
        # endpoint; try them in order and fall back to the API's own ordering.
        def _run_timestamp(r: Dict[str, Any]) -> str:
            for key in ("dateCreated", "createDate", "modifiedDate", "dateModified"):
                v = r.get(key)
                if v:
                    return str(v)
            return ""
        if any(_run_timestamp(r) for r in past_runs):
            sorted_runs = sorted(past_runs, key=_run_timestamp, reverse=True)
        else:
            sorted_runs = past_runs  # API-returned order (hopefully newest-first)
        recent = sorted_runs[:window]
        recent_failures = sum(1 for r in recent if _run_status(r) == "FAILED")
        if recent_failures > 0:
            return Classification(
                False,
                f"Known Build-Step Failure "
                f"(failed {recent_failures}/{len(recent)} of last {window} runs)",
                category="build_step_flake",
                pass_rate=pass_rate,
                recent_pass_rate=recent_pass_rate,
            )
        return Classification(
            True,
            f"New Build-Step Failure (no failures in last {len(recent)} run(s))",
            category="unique",
            pass_rate=pass_rate,
            recent_pass_rate=recent_pass_rate,
        )

    # 4. Hard-coded override for a known-noisy suite.
    if "LogAssertorTest" in test_name:
        if pass_rate < config.LOG_ASSERTOR_FLAKE_THRESHOLD:
            return Classification(
                False, f"Volatile Log Flake ({pass_rate}% overall pass rate)",
                category="volatile_log_flake",
                pass_rate=pass_rate,
                recent_pass_rate=recent_pass_rate,
            )

    # 5. Compare this failure's error to past failures.
    current_fingerprint = normalize_error(current_error_str)
    failed_past_runs = [r for r in past_runs if _run_status(r) == "FAILED"]
    history_has_error_text = False

    for run in failed_past_runs:
        run_fingerprint = normalize_error(_run_error_text(run))
        if run_fingerprint:
            history_has_error_text = True
            if is_same_error(current_fingerprint, run_fingerprint):
                if pass_rate < config.CONSISTENT_FAILURE_PASS_RATE_MAX:
                    return Classification(
                        False,
                        f"Consistent Failure (Fails {100 - pass_rate}% of the time with this error)",
                        category="consistent_failure",
                        pass_rate=pass_rate,
                        recent_pass_rate=recent_pass_rate,
                    )
                return Classification(
                    False,
                    f"Known Flake (Seen this exact error, {pass_rate}% overall pass rate)",
                    category="known_flake",
                    pass_rate=pass_rate,
                    recent_pass_rate=recent_pass_rate,
                )
        elif not current_fingerprint:
            # Both this run and the current run have no error text → recurring silent failure.
            return Classification(False, "Recurring Silent Failure",
                                  category="recurring_silent_failure",
                                  pass_rate=pass_rate,
                                  recent_pass_rate=recent_pass_rate)

    if failed_past_runs and not history_has_error_text:
        if pass_rate < config.STATISTICAL_FLAKE_PASS_RATE_MAX:
            return Classification(
                False, f"Statistical Flake ({pass_rate}% overall pass rate)",
                category="statistical_flake",
                pass_rate=pass_rate,
                recent_pass_rate=recent_pass_rate,
            )

    # 0.16.5: compute consecutive_failures on the non-strict True
    # Unique Failure path too, so streak-bucketing in the reporter
    # works correctly when --bucketed is set without --strict. The
    # strict-mode branch above already does this; we mirror it here
    # so callers downstream (especially html_reporter's
    # _bucket_failures_for_display) get accurate streak data
    # regardless of flake-suppression mode.
    streak = _consecutive_failures_count(
        history_data, current_case_result_id, pr_string,
    )
    return Classification(True, "True Unique Failure",
                          category="unique", pass_rate=pass_rate,
                          recent_pass_rate=recent_pass_rate,
                          consecutive_failures=streak)
