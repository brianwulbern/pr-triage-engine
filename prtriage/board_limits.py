"""Rate-limit awareness for the concurrent board: when to bail early, and a budget preflight.

The board triages many PRs at once. GitHub's limits are the constraint, and they are not all
visible: the core hourly budget shows up on the free /rate_limit endpoint, but the secondary
(burst) limit does not. These helpers probe what can be probed up front, and decide when a
throttled run should stop with a partial board instead of grinding. They never stop a short
tail: finishing a few PRs at high spacing costs seconds.

Extracted from the production tool; the functions below are the real implementation.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


# 2.92.0: when a manually-run board is "dancing with a rate limit" - GitHub's SECONDARY
# limit has tripped enough that the governor backed its inter-request spacing off past
# this many seconds (baseline 0.15s; it quadruples per hit: 0.15→0.6→2.4→9.6) - every
# remaining PR is dozens of calls × multi-second waits, i.e. a crawl (the 40-min run that
# motivated this). Better to bail with a partial board than grind. 2.237.0: raised the trip
# from 2.0 to 4.0 - a transient 2-hit secondary-limit burst only backs spacing to 2.4s, and
# bailing a nearly-done board on that (with primary budget untouched) throws away completed
# work; only a sustained 3rd hit (9.6s) now trips. Override with PRTRIAGE_PULLS_ABORT_SPACING;
# disable the abort with --no-rate-abort (a background run may legitimately want to grind).
def _abort_spacing() -> float:
    try:
        return float(os.environ.get("PRTRIAGE_PULLS_ABORT_SPACING", "4.0"))
    except ValueError:
        return 4.0


# 2.237.0: the crawl-abort exists to avoid a long grind through MANY remaining PRs. It is
# pointless on a short tail - finishing a few more PRs at high spacing costs seconds, not the
# 40-min grind the bail was built for - yet the old check fired identically whether 2 or 200
# PRs remained (this bit a 26/28 board that bailed the last 2 with 3000+ requests of budget
# left). So don't abort while fewer than this many PRs remain; let the governor's backoff
# pace them out and finish the board. A genuinely tripped circuit breaker still fails fast
# via GovernorTripped on the next call regardless. Tune with PRTRIAGE_PULLS_ABORT_MIN_REMAINING.
def _abort_min_remaining() -> int:
    try:
        return max(1, int(os.environ.get("PRTRIAGE_PULLS_ABORT_MIN_REMAINING", "5")))
    except (TypeError, ValueError):
        return 5


def _should_crawl_abort(remaining: int) -> Optional[str]:
    """The crawl-pressure string when the board SHOULD bail now, else None. Bails only when
    the governor is crawling (spacing past the trip / breaker tripped) AND enough PRs remain
    that grinding them is worse than a partial board. A short tail (remaining <
    _abort_min_remaining()) is always finished. 2.237.0."""
    if remaining < _abort_min_remaining():
        return None
    return _rate_limit_crawl()


def _rate_limit_crawl() -> Optional[str]:
    """If the GitHub governor has backed off into a crawl (spacing ≥ the abort trip) OR its
    circuit breaker has tripped (persistently hot - 2.99.0), return a one-line pressure
    description; else None."""
    from . import governor
    gov = governor.governor()
    snap = gov.snapshot()
    if snap.get("tripped") or float(snap.get("spacing", 0.0)) >= _abort_spacing():
        return gov.describe() or (
            f"spacing {snap.get('spacing')}s, {snap.get('rate_limit_hits')} hit(s)")
    return None


# 2.100.0: board budget preflight. The GitHub token is often SHARED (e.g. with a sibling tool) and a
# wide board makes ~30-40 calls per PR, so a partially-spent hour can't afford a big scan -
# the run then trips the breaker partway (it HOLDS what completed, but you waited for it). A
# FREE /rate_limit probe (exempt from the limit) up front warns when the budget can't cover
# the scan, so you can scope down or wait instead of finding out mid-run. Advisory only -
# never blocks (the breaker handles the actual trip). Tune the per-PR estimate with
# PRTRIAGE_PULLS_CALLS_PER_PR.
def _est_calls_per_pr() -> int:
    try:
        return max(1, int(os.environ.get("PRTRIAGE_PULLS_CALLS_PER_PR", "35")))
    except (TypeError, ValueError):
        return 35


def _core_budget(token: Optional[str]) -> tuple:
    """(remaining, reset_epoch) for the token's CORE (primary hourly) budget via the FREE
    /rate_limit endpoint (exempt from the limit itself). (None, None) on any failure - the
    preflight is best-effort and never blocks the board."""
    from . import http_cache
    try:
        resp = http_cache.gh_get(
            "https://api.github.com/rate_limit",
            headers={"Authorization": f"token {token}",
                     "Accept": "application/vnd.github+json"}, timeout=15)
        if getattr(resp, "status_code", None) != 200:
            return None, None
        core = ((resp.json() or {}).get("resources") or {}).get("core") or {}
        return core.get("remaining"), core.get("reset")
    except Exception:  # noqa: BLE001 - preflight is advisory
        return None, None


def _probe_secondary_limit(token: Optional[str]) -> bool:
    """Is the token under GitHub's SECONDARY (abuse) rate limit RIGHT NOW?

    The core budget from `/rate_limit` is BLIND to this: when a burst trips the secondary
    limit, GitHub 403s EVERY authed endpoint globally (even `/user` and public repos) with
    `x-ratelimit-remaining: 0` - while `/rate_limit` (an exempt endpoint) keeps reporting the
    untouched primary core budget (measured: every call 403s while /rate_limit reads
    4995/5000). So a green `_core_budget` is a FALSE "go" during exactly the limit type a
    board keeps hitting. One live authed probe (`GET /user`, free/cheap, doesn't spend core)
    settles it: a 403/429 carrying a rate-limit signature means the board would trip almost
    immediately. Conservative - only a clear rate-limit 403 counts (a 401/other is an auth
    problem the board itself surfaces, not our business to cry wolf over). Best-effort:
    any transport error → False (never blocks the board). Skip with PRTRIAGE_NO_SECONDARY_PROBE=1."""
    if not token or os.environ.get("PRTRIAGE_NO_SECONDARY_PROBE", "").strip():
        return False
    from . import http_cache
    try:
        resp = http_cache.gh_get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {token}",
                     "Accept": "application/vnd.github+json"}, timeout=15)
    except Exception:  # noqa: BLE001 - advisory probe, never load-bearing
        return False
    status = getattr(resp, "status_code", None)
    if status not in (403, 429):
        return False
    # A rate-limit 403 carries either remaining:0 or a "rate limit" message; a plain
    # forbidden (scope/SSO) does not - don't mislabel those as a secondary limit.
    try:
        remaining = (resp.headers or {}).get("x-ratelimit-remaining")
    except Exception:  # noqa: BLE001
        remaining = None
    body = ""
    try:
        body = (resp.text or "")[:400].lower()
    except Exception:  # noqa: BLE001
        body = ""
    return remaining == "0" or "rate limit" in body


def _budget_preflight(token: Optional[str], n_targets: int) -> None:
    """Warn once, up front, when the token can't cover the scan - either because a SECONDARY
    (abuse) limit is already active (a live probe; /rate_limit can't see it) or the primary
    core budget is too low. Advisory only - never blocks; the 2.99 breaker handles a real trip."""
    # Secondary limit first: it's the failure mode /rate_limit is blind to, and if it's
    # active the primary-budget math below is moot (every call 403s regardless of core).
    if _probe_secondary_limit(token):
        log.warning(
            "⛔ GitHub is rate-limiting this token RIGHT NOW (secondary/abuse limit - often "
            "from a recent burst, and SHARED with a sibling tool). Every authed call is 403ing, so the "
            "board would trip almost immediately (the breaker HOLDS what completes, but "
            "that's ~nothing from a cold start). Wait a few minutes for it to clear, then "
            "re-run. (/rate_limit still reads healthy - it's exempt and can't see this.)")
        return
    remaining, reset = _core_budget(token)
    if remaining is None:
        return
    per_pr = _est_calls_per_pr()
    if remaining >= n_targets * per_pr:
        return
    affordable = max(1, remaining // per_pr)
    when = ""
    if reset:
        try:
            when = f", resets ~{time.strftime('%H:%M', time.localtime(reset))}"
        except (ValueError, OSError, OverflowError):
            when = ""
    log.warning(
        "⚠️  GitHub budget low: %d core API call(s) left%s - this board costs ~%d/PR, so "
        "only ~%d of %d PR(s) will likely triage before the circuit breaker bails (it HOLDS "
        "what completes). Consider --open %d, or wait for the reset.",
        remaining, when, per_pr, affordable, n_targets, affordable)
