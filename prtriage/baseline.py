"""Rolling failure-baseline computation for routine health
indication (0.19.6).

Driven by a user observation: history-based suppression
("known_flake") is sampled against a rolling window of recent
test runs. When that window is bumpier than usual, the
classifier suppresses MORE failures - meaning a PR triaged
during a high-flake-noise period gets more permissive triage
than the same diff would during a calm period. Trust in the
verdict is implicitly correlated with environmental health,
but pre-0.19.6 there was no way for the user to see that
context.

0.19.6 adds a one-line indicator at the top of the suppressed
breakdown:

  📈 Rolling baseline: 47 failures/build over last 14 days
     (median; ⬆️ up from 22 over the prior 14 days)

Calibrates the user's trust in the suppression depth. When the
trend is up sharply, they know to look at the unique failures
more carefully - flake-shaped regressions are more likely to
slip through. When stable or down, they can trust the verdict
more.

**Performance.** The build-metrics endpoint exists and is one
HTTP call per routine; caching with a 6-hour TTL means most
triage runs are cache hits and pay zero latency. The fetch is
also fire-and-forget - if it fails or times out, the indicator
silently skips and the rest of the report renders normally.

**Approximation.** "Failures/build" here is total failures per
build, not strictly "flake-shaped failures per build" - getting
the latter would require re-classifying every historical build's
failures, which is too expensive for an inline signal. Total
failures is a strong proxy: when the total goes up, almost all
the increase shows up as flake-shaped suppressions (consistent
failures are stable baseline). Document this trade-off in the
indicator text so users don't over-read the number.
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# Cache TTL - six hours strikes a balance between "fresh enough
# to reflect today's CI noise" and "stale enough that repeated
# triages don't all hit ReportService." The metric we're computing
# (rolling 14-day median) doesn't move dramatically over a day,
# let alone six hours, so the freshness loss is small.
_CACHE_TTL_SECONDS = 6 * 60 * 60

# Window sizes for the comparison. 14 days each side gives us
# enough builds (typically 20-40 per period for a reasonable
# routine) for the median to be stable. Shorter windows make
# the signal noisy; longer ones lag too much behind real shifts.
_RECENT_WINDOW_DAYS = 14
_PRIOR_WINDOW_DAYS = 14

# Minimum builds needed in each window for the signal to fire.
# Below 5, the median is meaningless. Routines that run rarely
# (< 5 builds/2 weeks) just don't get a baseline indicator.
_MIN_BUILDS_PER_WINDOW = 5

# Number of builds to fetch from the metrics endpoint. 80 covers
# 28 days for routines running ~2x/day; uncommon routines that
# run more frequently might lose data past the limit but the
# windows we care about are recent-most so the most-recent
# builds are always covered.
_BUILDS_TO_FETCH = 80

# Trend thresholds. The 1.5x / 0.67x ratio is the threshold
# beyond which we call the change "up" or "down" - anything
# less is "stable." Heuristic; tunable.
_TREND_UP_RATIO = 1.5
_TREND_DOWN_RATIO = 1 / 1.5


def _cache_dir() -> str:
    """Return the user's cache directory for prtriage baselines.
    Standard XDG path on Linux/Mac; falls back gracefully if
    the env var isn't set or the dir can't be created.
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = xdg if xdg else os.path.expanduser("~/.cache")
    cache_dir = os.path.join(base, "prtriage")
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        # If we can't create the dir, just disable caching by
        # returning a path that won't exist. The cache-read path
        # handles this gracefully.
        return ""
    return cache_dir


def _cache_path(routine_id: str) -> str:
    """Return the cache filename for a given routine, or ""
    when caching is disabled."""
    base = _cache_dir()
    if not base:
        return ""
    # Routine IDs are numeric strings in ReportService; safe as filenames.
    # Pattern-match defensively in case future routine IDs include
    # other characters that would be unsafe in a path.
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", routine_id)
    return os.path.join(base, f"baseline-{safe_id}.json")


def _read_cache(routine_id: str) -> Optional[Dict[str, Any]]:
    """Read a cached baseline for this routine. Returns None if
    the cache is missing, stale (>TTL), corrupt, or disabled.

    Cache hits are transparent - caller never sees the difference
    from a fresh fetch. Cache misses fall through to the live
    compute path.
    """
    path = _cache_path(routine_id)
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    if (time.time() - st.st_mtime) > _CACHE_TTL_SECONDS:
        log.debug(
            "baseline cache for routine %s is stale (>%ds), "
            "re-fetching", routine_id, _CACHE_TTL_SECONDS,
        )
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("baseline cache read failed: %s", exc)
        return None
    return data


def _write_cache(routine_id: str, baseline: Dict[str, Any]) -> None:
    """Write a baseline to cache. Failures are silent - caching
    is a perf optimization, not load-bearing."""
    path = _cache_path(routine_id)
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(baseline, f)
    except OSError as exc:
        log.debug("baseline cache write failed: %s", exc)


# Field names the ReportService metrics endpoint may use for the
# failure count. Defensive - different routine types may shape
# the response slightly differently, and we'd rather skip the
# indicator than crash if a new shape appears. First match wins.
#
# Order matters: most-specific ReportService naming first, then common
# fallback variants. `caseResultFailed` is the canonical ReportService
# REST convention I've seen in other endpoints.
_FAILURE_COUNT_FIELDS = (
    "caseResultFailed",
    "failed",
    "numFailed",
    "failedCount",
    "failedTestCount",
    "totalFailed",
)

# Blocked tests are failure-equivalent (they didn't run, often
# due to an upstream Gradle task failure or environment issue).
# When the endpoint exposes both `failed` and `blocked`, we sum
# them for the baseline count.
_BLOCKED_COUNT_FIELDS = (
    "caseResultBlocked",
    "blocked",
    "numBlocked",
    "blockedCount",
)

# Execution-date fields. ReportService endpoints have varied between
# ISO timestamps and millisecond epoch - try both shapes. The routine
# builds-metrics endpoint (used by --profile-days) names it
# `reportBuildDueDate`, e.g. "2026-07-01 17:03:55.463" (space separator,
# millisecond fraction) - confirmed via diag_profile_dates.py.
_EXECUTION_DATE_FIELDS = (
    "reportBuildDueDate",
    "dueDate",
    "executionDate",
    "dateExecuted",
    "createDate",
    "dateCreated",
)


def _extract_count(record: Dict[str, Any],
                   field_names: Tuple[str, ...]) -> Optional[int]:
    """Pull the first numeric value matching one of the candidate
    field names from a build-metrics record. Returns None when
    none of the names yield a usable int.
    """
    for name in field_names:
        v = record.get(name)
        if v is None:
            continue
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                continue
    return None


def _extract_date(record: Dict[str, Any]) -> Optional[datetime]:
    """Parse the build's execution date from one of several
    candidate field names / formats. Returns timezone-aware UTC
    datetime, or None if no parsable value found.

    Handles:
      - ISO 8601 strings (with or without 'Z' suffix)
      - millisecond epoch integers
      - second epoch integers (best-effort detection by magnitude)
    """
    for name in _EXECUTION_DATE_FIELDS:
        v = record.get(name)
        if v is None:
            continue
        # ISO string.
        if isinstance(v, str):
            s = v.strip()
            if not s:
                continue
            # Replace trailing Z with +00:00 for fromisoformat,
            # which doesn't accept Z directly until 3.11.
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except ValueError:
                continue
        # Epoch number - detect ms vs s by magnitude. Values past
        # year 2286 in seconds = 10^10; values within plausible
        # recent timestamps in milliseconds = 10^12. The threshold
        # at 10^11 splits cleanly.
        if isinstance(v, (int, float)):
            if v > 1e11:
                # Milliseconds.
                return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)
            # Seconds.
            return datetime.fromtimestamp(v, tz=timezone.utc)
    return None


def _extract_build_data(builds_raw: List[Dict[str, Any]]) -> List[Tuple[datetime, int]]:
    """Convert raw build-metric records into (date, failure_count)
    tuples. Records that lack either field are dropped silently -
    we'd rather lose a few sample points than crash on a malformed
    record. Failure count = failed + blocked (blocked is treated
    as failure-equivalent because it means the test couldn't run).
    """
    out: List[Tuple[datetime, int]] = []
    for record in builds_raw:
        date = _extract_date(record)
        if date is None:
            continue
        failed = _extract_count(record, _FAILURE_COUNT_FIELDS)
        blocked = _extract_count(record, _BLOCKED_COUNT_FIELDS) or 0
        if failed is None:
            continue
        out.append((date, failed + blocked))
    return out


def _split_windows(
    build_data: List[Tuple[datetime, int]],
    now: Optional[datetime] = None,
) -> Tuple[List[int], List[int]]:
    """Split build data into (recent_window_counts, prior_window_counts)
    based on `now`. Each result is a list of failure counts for
    builds that fell within the corresponding window.

    `now` is overridable so tests can pin time. Production always
    passes None to use the current UTC time.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=_RECENT_WINDOW_DAYS)
    prior_cutoff = recent_cutoff - timedelta(days=_PRIOR_WINDOW_DAYS)

    recent: List[int] = []
    prior: List[int] = []
    for (date, count) in build_data:
        if date > recent_cutoff:
            recent.append(count)
        elif date > prior_cutoff:
            prior.append(count)
        # Older than the prior window - drop, we don't need it.
    return recent, prior


def _trend(recent_median: float, prior_median: float) -> str:
    """Classify the median delta as up/down/stable.

    Stable when the change is within ±50% (1.5x in either
    direction). Heuristic chosen to suppress noise - small
    fluctuations don't constitute "the routine got bumpier."
    """
    if prior_median == 0:
        # Edge case: the prior window had perfect builds. Any
        # increase in recent is technically "up" but the ratio
        # math breaks. Treat as stable unless recent is also 0.
        return "up" if recent_median > 0 else "stable"
    ratio = recent_median / prior_median
    if ratio >= _TREND_UP_RATIO:
        return "up"
    if ratio <= _TREND_DOWN_RATIO:
        return "down"
    return "stable"


def compute_routine_baseline(
    client: Any,
    routine_id: str,
    use_cache: bool = True,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Compute the routine's failure-count baseline over the last
    two 14-day windows. Returns a dict suitable for direct rendering
    via `format_baseline_line`, or None when the data is
    insufficient.

    Returns None when:
      - The ReportService fetch fails (network, auth, etc.)
      - Either window has fewer than 5 builds (median unreliable)
      - The metrics endpoint doesn't expose recognizable failure-
        count fields (defensive against schema drift)

    Output shape:
      {
        "recent_median": int,
        "recent_count": int,    # builds in last 14d
        "prior_median": int,
        "prior_count": int,     # builds in prior 14d
        "trend": "up" | "down" | "stable",
        "computed_at": float,   # epoch seconds; for cache
                                # freshness debugging
      }

    `use_cache=False` forces a live fetch (used in tests).
    """
    if use_cache:
        cached = _read_cache(routine_id)
        if cached is not None:
            return cached

    try:
        builds_raw = client.fetch_routine_recent_builds(
            routine_id, limit=_BUILDS_TO_FETCH,
        )
    except Exception as exc:  # noqa: BLE001
        # Network blip, auth, etc. The indicator is enrichment,
        # not load-bearing - log at debug and skip.
        log.debug("baseline fetch failed: %s", exc)
        return None
    if not builds_raw:
        return None

    build_data = _extract_build_data(builds_raw)
    if not build_data:
        # All records lacked recognizable failure-count fields.
        # Either the endpoint shape changed or we're querying a
        # routine type with a different schema. Log the keys we
        # saw at debug so a future iteration can extend the
        # candidate-field lists.
        log.debug(
            "baseline: no parsable build data from %d raw records. "
            "First record keys: %s",
            len(builds_raw),
            sorted(builds_raw[0].keys()) if builds_raw else [],
        )
        return None

    recent, prior = _split_windows(build_data, now=now)
    if len(recent) < _MIN_BUILDS_PER_WINDOW or len(prior) < _MIN_BUILDS_PER_WINDOW:
        log.debug(
            "baseline: insufficient builds in window - recent=%d, "
            "prior=%d (need >=%d each)",
            len(recent), len(prior), _MIN_BUILDS_PER_WINDOW,
        )
        return None

    recent_median = statistics.median(recent)
    prior_median = statistics.median(prior)
    baseline = {
        "recent_median": int(recent_median),
        "recent_count": len(recent),
        "prior_median": int(prior_median),
        "prior_count": len(prior),
        "trend": _trend(recent_median, prior_median),
        "computed_at": time.time(),
    }

    if use_cache:
        _write_cache(routine_id, baseline)

    return baseline


def format_baseline_line(baseline: Dict[str, Any]) -> str:
    """Format the baseline dict as a single user-facing line.

    Example outputs:
      📈 Rolling baseline: 47 failures/build over last 14 days (median; ⬆ up from 22 over the prior 14 days)
      📊 Rolling baseline: 22 failures/build over last 14 days (median; stable from 19)
      📉 Rolling baseline: 12 failures/build over last 14 days (median; ⬇ down from 31)

    The leading emoji shifts to match the trend - gives the eye
    a glanceable cue without forcing the user to parse the
    numbers. ⬆/⬇/➡ arrows reinforce the direction inside the
    parenthetical so the trend reads clearly in monospace logs
    that may not render the leading emoji well.
    """
    recent = baseline["recent_median"]
    prior = baseline["prior_median"]
    trend = baseline["trend"]

    if trend == "up":
        leader = "📈"
        arrow = "⬆ up"
    elif trend == "down":
        leader = "📉"
        arrow = "⬇ down"
    else:
        leader = "📊"
        arrow = "stable"

    if trend == "stable":
        return (
            f"{leader} Rolling baseline: {recent} failures/build over "
            f"last 14 days (median; {arrow} from {prior})"
        )
    return (
        f"{leader} Rolling baseline: {recent} failures/build over "
        f"last 14 days (median; {arrow} from {prior} over the "
        f"prior 14 days)"
    )
