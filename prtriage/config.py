"""Tunables for the classifier, the verdict ladder and the comment engine.

The production deployment tunes these against its own test suite and CI. The values here are
generic defaults so the showcase runs; the shapes (types, meaning) match the real ones.
"""
from __future__ import annotations

import os
import re

# --- classifier: history-based flake detection -------------------------------------------
# A failure is "the same error" as a past one when a normalized prefix of at least this many
# characters matches, or the fuzzy ratio clears SIMILARITY_THRESHOLD.
MIN_SUBSTRING_MATCH_LEN = 40
SIMILARITY_THRESHOLD = 0.80

# Pass-rate bands (0-100) over the test's recent history.
CONSISTENT_FAILURE_PASS_RATE_MAX = 20    # below this it fails nearly always: standing on base
STATISTICAL_FLAKE_PASS_RATE_MAX = 80     # below this (and above the band above) it is flaky
LOG_ASSERTOR_FLAKE_THRESHOLD = 85        # log-scan suites are noisier, so a looser flake band
RECENT_PASS_RATE_WINDOW = 30             # how many recent runs the recent pass rate uses
RECENT_PASS_RATE_MIN_RUNS = 5            # fewer samples than this: no recent pass rate
BUILD_STEP_RECENT_RUNS_WINDOW = 5

# Errors that always matter, whatever the history says.
CRITICAL_ERROR_PHRASES = (
    "OutOfMemoryError",
    "error: cannot find symbol",
    "error: cannot find class",
    "error: package",
    "Compilation failed:",
    "error TS",
)
# Tests whose failure means nothing else in the run can be trusted (a startup canary).
CRITICAL_TEST_PATTERNS = (re.compile(r"\bSmokeTest\b"),)
CRITICAL_TEST_NAME_PATTERNS = ("Bundle Builder",)

# Known-noise errors to drop outright.
IGNORED_ERROR_PATTERNS = (
    r"(Cannot|Unable to) find session with id:",
)

# Infrastructure signatures: (pattern, what it means). A match is environmental, not the change.
INFRASTRUCTURE_PATTERNS = (
    (re.compile(r"(?:json\w*Exception|JSONDecodeError).*?<!doctype html>", re.I | re.S),
     "a service returned an HTML error page where JSON was expected"),
    (re.compile(r"Connection (?:reset|refused)|SocketTimeoutException|Read timed out", re.I),
     "a network flake between the test and a dependency"),
    (re.compile(r"No space left on device", re.I),
     "the CI node ran out of disk"),
    (re.compile(r"Could not resolve host|UnknownHostException", re.I),
     "a DNS failure on the CI node"),
)

# --- verdict ladder ------------------------------------------------------------------------
# A failure in code the change touches that passes at least this often is "usually reliable":
# one red run cannot tell a fresh break from a one-off, so the call is a re-run, not a verdict.
RELIABLE_PASS_RATE = 90
# A run that executed less than this fraction of its expected tests is not evidence of safety.
MIN_COVERAGE = 0.5

# --- branch staleness ------------------------------------------------------------------------
# How far behind base a branch can drift before a rebase is actually needed. The comment engine
# treats a behind-count change as news only when it crosses this line.
STALE_REBASE_BEHIND_THRESHOLD = 200

# --- AI-resolved merge conflicts -------------------------------------------------------------
# The backport bot labels a pull whose merge conflict a model resolved. It holds until a person
# other than the forwarder signs off (an Approve review or an LGTM comment from an org member),
# then the sign-off label is added. One reminder after this many working days.
AI_CONFLICT_LABEL = "ai-resolved-conflict"
AI_CONFLICT_SIGNOFF_LABEL = "ai-resolution-signed-off"
AI_REVIEW_REMIND_WORKDAYS = 2
# A team can route these reviews to one person: a pull that changes a path CODEOWNERS gives the
# team, or whose commit author is on it, asks only this reviewer.
PLATFORM_TEAM_CODEOWNER = "platform-core"
PLATFORM_TEAM_MEMBERS = ("core-dev-a", "core-dev-b", "core-dev-c")
PLATFORM_TEAM_AI_REVIEWER = "core-reviewer"
POSTABLE_REPO_PREFIX = "example-org/"

# --- re-run reconciliation -------------------------------------------------------------------
# How many earlier runs of the same trigger a pull's latest run is compared against.
RECONCILE_MAX_RUNS_PER_TRIGGER = 3

# --- board cache / deep-record cache -----------------------------------------------------------
# A red or review row ages out after this; the deep-record cache ages every row out after it.
PULLS_CACHE_TTL_SECONDS = int(os.environ.get("PULLS_CACHE_TTL_SECONDS") or 24 * 3600)
# Bumped by a release that changes what a verdict says, so cached verdicts made before it miss.
VERDICT_LOGIC = 23

# --- deep analysis model chain ---------------------------------------------------------------
DEEP_ANALYSIS_MODEL_ENV = "PRTRIAGE_DEEP_MODEL"
DEEP_ANALYSIS_MODEL_FALLBACKS = ("claude-opus-5-5", "claude-sonnet-5-5")


def cache_root() -> str:
    """The cache base every file lives under: $XDG_CACHE_HOME, else ~/.cache."""
    return os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
