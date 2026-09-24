"""Tunables for the classifier, the verdict ladder and the comment engine.

The production deployment tunes these against its own test suite and CI. The values here are
generic defaults so the showcase runs; the shapes (types, meaning) match the real ones.
"""
from __future__ import annotations

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
