"""2.34.0 (#2): a shared, adaptive client-side rate governor for GitHub HTTP.

GitHub enforces a SECONDARY (burst/concurrency) limit that is INVISIBLE to `/rate_limit`
- it only reveals itself as a 403/429 with a `Retry-After`. Several concurrent prtriage sweeps
+ polls + `--gh-post` writes trip it (observed repeatedly: core stayed 4998/5000 while
requests 403'd). We can't READ that ceiling, so we RIDE it: one process-wide governor that
every GitHub caller paces through, so their combined traffic self-tunes to just under the
invisible limit instead of each burst discovering it independently.

Mechanism (AIMD - additive-increase / multiplicative-decrease of *throughput*):
  • proactive baseline spacing between requests (globally, across threads),
  • a clean streak narrows spacing back toward baseline (additive),
  • a rate-limit response widens spacing hard and opens a cooldown (multiplicative),
  • `Retry-After` on a rate-limit response is honored as a floor on the cooldown,
  • the inferred pressure is queryable for `-v` / the banner.

Design notes:
  • The actual `requests.*` call stays at each call site UNCHANGED - callers wrap it as
    `pace(); resp = requests.get(...); observe(resp)`. That keeps every existing
    `mock.patch.object(module.requests, "get")` test working, and lets the retry loop stay
    opt-in per call site.
  • `PRTRIAGE_NO_GH_GOVERNOR=1` disables pacing entirely (set by the test conftest) so the suite
    never sleeps on real time.
  • `clock`/`sleep` are injectable so the AIMD behavior is unit-testable without wall-clock.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

# --- tunables (seconds) ---------------------------------------------------
# Baseline spacing is deliberately small: the secondary limit trips on BURST/concurrency,
# not on a single triage's serial trickle, so at rest we barely pace (a lone PR triage
# adds ~nothing) while a concurrent sweep still serializes through the shared timestamp.
BASELINE_SPACING = 0.15
MAX_SPACING = 8.0                 # ceiling so a pathological streak of 403s can't wedge us
BACKOFF_FACTOR = 4.0              # multiplicative widen on a rate-limit hit
RECOVER_STEP = 0.10              # additive narrow per clean streak milestone
RECOVER_EVERY = 8               # a clean streak this long triggers one narrow step
DEFAULT_COOLDOWN = 2.0           # cooldown when a rate-limit gives no Retry-After
MAX_COOLDOWN = 60.0              # never sit out longer than this on one hit

# 2.99.0 - circuit breaker. The AIMD backoff paces each request, but when the SECONDARY
# limit is PERSISTENTLY hot (every request 403s despite honored Retry-After backoffs), the
# spacing pins at MAX and the run just grinds: hundreds of calls × 8s each. Observed live -
# `prtriage pulls --me` ran 833s / 752 hits before a Ctrl+C, and the between-PR abort never fired
# because the FIRST PR (dozens of throttled calls) never completed. So after this many
# CONSECUTIVE hits (reset by any clean response), pace() trips: it stops pacing and RAISES
# GovernorTripped so the caller fails FAST instead of grinding - firing mid-PR / mid-
# enumeration, wherever the next GitHub call is. The trip auto-clears after a cooldown so a
# fresh attempt can probe whether the limit lifted. Tune with PRTRIAGE_GOVERNOR_TRIP_HITS.
CIRCUIT_TRIP_HITS = 8
CIRCUIT_COOLDOWN = 45.0

# 2.238.0 - isolated buckets. GitHub meters some endpoints in their OWN rate buckets, separate
# from (and far smaller than) the core REST budget: code search is 10 requests/minute. Running
# one dry says nothing about core or secondary pressure, so it must NOT widen the shared spacing
# or feed the circuit breaker. Field: every throttle captured 2026-09-23/24 was a
# `/search/code` 403 with X-RateLimit-Resource=code_search and remaining=0; each one backed ALL
# GitHub calls off 0.15 → 0.6 → 2.4s and bailed `prtriage pulls` with 3000+ core requests unspent
# (the immediate governed() retry is what made it two hits, 2s apart, since the bucket only
# resets per minute). Instead the bucket gets its own cooldown until its X-RateLimit-Reset,
# and callers skip it while it cools.
ISOLATED_RESOURCES = frozenset({"search", "code_search"})
RESOURCE_DEFAULT_COOLDOWN = 60.0   # when the response carries no usable reset time
RESOURCE_MAX_COOLDOWN = 65.0       # search buckets reset per minute; never sit out longer


class GovernorTripped(Exception):
    """Raised by pace() when the secondary rate limit has been persistently hot (>=
    CIRCUIT_TRIP_HITS consecutive throttled responses). The signal to FAIL FAST - bail the
    board with a partial result, or let a best-effort enrichment skip - rather than grind
    hundreds of maxed-spacing requests. Auto-clears after CIRCUIT_COOLDOWN."""


def _is_rate_limited(status: int, headers: Optional[Dict[str, Any]]) -> bool:
    """A response that signals GitHub throttling (primary OR secondary). 403/429 with a
    zero remaining budget or a Retry-After. A plain 403 (permission/scope) is NOT throttling
    and must not back us off - GitHub reuses 403 for both."""
    if status not in (403, 429):
        return False
    h = headers or {}
    if h.get("Retry-After") is not None:
        return True
    remaining = h.get("X-RateLimit-Remaining")
    if remaining is not None:
        try:
            return int(remaining) <= 0
        except (TypeError, ValueError):
            return False
    return False


def _retry_after_seconds(headers: Optional[Dict[str, Any]]) -> Optional[float]:
    """The Retry-After header as seconds, if present and numeric (GitHub sends a
    delta-seconds integer). None when absent/unparseable."""
    if not headers:
        return None
    ra = headers.get("Retry-After")
    if ra is None:
        return None
    try:
        return max(0.0, float(ra))
    except (TypeError, ValueError):
        return None


def _isolated_resource(headers: Optional[Dict[str, Any]]) -> Optional[str]:
    """The response's rate bucket when it is one of ISOLATED_RESOURCES, else None. Only a
    real string header counts (a mocked/absent header never matches)."""
    if not headers:
        return None
    try:
        res = headers.get("X-RateLimit-Resource")
    except Exception:
        return None
    return res if isinstance(res, str) and res in ISOLATED_RESOURCES else None


def _reset_in_seconds(headers: Optional[Dict[str, Any]]) -> Optional[float]:
    """Seconds until X-RateLimit-Reset (an epoch), or None when absent/unparseable."""
    if not headers:
        return None
    try:
        reset = headers.get("X-RateLimit-Reset")
        return None if reset is None else float(reset) - time.time()
    except (TypeError, ValueError, AttributeError):
        return None


class GitHubGovernor:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        baseline: float = BASELINE_SPACING,
        enabled: bool = True,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._baseline = baseline
        self._enabled = enabled
        self._lock = threading.Lock()
        self._spacing = baseline
        self._next_allowed = 0.0     # monotonic ts the next request may proceed
        self._cooldown_until = 0.0   # monotonic ts a rate-limit cooldown lifts
        self._clean_streak = 0
        self._rate_limit_hits = 0
        self._last_rate_limit_at: Optional[float] = None  # wall-ish (via clock) ts
        # 2.99.0 circuit breaker: consecutive throttled responses (reset on any clean one),
        # and the monotonic ts the trip auto-clears at (0.0 = not tripped).
        self._consecutive_hits = 0
        self._tripped_until = 0.0
        self._trip_env = os.environ.get("PRTRIAGE_GOVERNOR_TRIP_HITS")
        # 2.238.0: per-bucket cooldowns for ISOLATED_RESOURCES (monotonic ts it lifts) + hits.
        self._resource_until: Dict[str, float] = {}
        self._resource_hits: Dict[str, int] = {}

    # -- pacing -------------------------------------------------------------
    def pace(self) -> float:
        """Block until this request is allowed to proceed under the current spacing +
        any active cooldown, then reserve the next slot. Returns the seconds slept (0 when
        disabled or already due). The sleep happens OUTSIDE the lock so concurrent callers
        serialize only on the (cheap) slot arithmetic, not on each other's waits.

        2.38.0 - bound the reservation pile-up under concurrency. The cooldown floor
        (a real post-403 backoff, ≤ MAX_COOLDOWN) is honored in full, but the per-request
        SPACING reservation is clamped so N concurrent callers can't push `_next_allowed`
        arbitrarily far into the future. Without the clamp, an 8-worker sweep whose spacing
        had backed off to MAX_SPACING piled up hundreds of `+= spacing` reservations, so
        every `pace()` slept on a runaway wait (hours out) while GitHub sat idle at
        5000/5000 - and since nothing fired, no clean response ever arrived to narrow the
        spacing back down: a self-sustaining stall. Clamping caps a single wait at roughly
        cooldown + MAX_SPACING and lets traffic flow again so the AIMD recovery can run."""
        if not self._enabled:
            return 0.0
        with self._lock:
            now = self._clock()
            # 2.99.0 circuit breaker: while tripped, FAIL FAST (no request, no sleep) so the
            # caller bails instead of grinding maxed-spacing calls. Auto-clear once the
            # cooldown elapses, letting the next call probe whether the limit lifted.
            if self._tripped_until:
                if now < self._tripped_until:
                    raise GovernorTripped(
                        f"GitHub secondary rate limit persistently hot "
                        f"({self._rate_limit_hits} hit(s)); circuit breaker open")
                self._tripped_until = 0.0
                self._consecutive_hits = 0
            # Honor an active cooldown in full; that's a legitimate backoff.
            floor = max(now, self._cooldown_until)
            # Queue behind prior reservations, but never let the spacing queue run more
            # than MAX_SPACING beyond the floor - this is what bounds the pile-up.
            start = min(max(floor, self._next_allowed), floor + MAX_SPACING)
            # reserve the slot for THIS request; the next caller waits one spacing beyond.
            self._next_allowed = start + self._spacing
            wait = start - now
        if wait > 0:
            self._sleep(wait)
        return max(0.0, wait)

    # -- feedback -----------------------------------------------------------
    def observe(self, resp: Any) -> None:
        """Feed a response back to the governor. A throttling response widens spacing and
        opens a cooldown; any other response counts as a clean tick that, in streaks,
        narrows spacing back toward baseline. Tolerant of a bare status int or a duck-typed
        response with `.status_code`/`.headers`."""
        status, headers = _resp_parts(resp)
        if status is None:
            return
        if _is_rate_limited(status, headers):
            # 2.238.0: an isolated bucket (code search) cools ONLY itself - no global backoff.
            res = _isolated_resource(headers)
            if res:
                self.note_resource_limit(res, headers)
            else:
                self.note_rate_limit(_retry_after_seconds(headers))
        elif 200 <= status < 400:
            self._note_clean()
        # 4xx/5xx that aren't throttling: neither reward nor punish (don't narrow on a
        # 404, don't back off on a 500 - those aren't rate signals).

    def _trip_threshold(self) -> int:
        try:
            return max(1, int(self._trip_env)) if self._trip_env else CIRCUIT_TRIP_HITS
        except (TypeError, ValueError):
            return CIRCUIT_TRIP_HITS

    def note_rate_limit(self, retry_after: Optional[float] = None) -> None:
        with self._lock:
            self._rate_limit_hits += 1
            self._consecutive_hits += 1
            self._clean_streak = 0
            self._last_rate_limit_at = self._clock()
            self._spacing = min(MAX_SPACING, max(self._spacing, self._baseline) * BACKOFF_FACTOR)
            cooldown = retry_after if retry_after is not None else DEFAULT_COOLDOWN
            cooldown = min(MAX_COOLDOWN, max(0.0, cooldown))
            self._cooldown_until = max(self._cooldown_until, self._clock() + cooldown)
            # 2.99.0: persistently hot → open the breaker (pace() will fail fast).
            if self._consecutive_hits >= self._trip_threshold():
                self._tripped_until = self._clock() + CIRCUIT_COOLDOWN

    def _note_clean(self) -> None:
        with self._lock:
            self._clean_streak += 1
            self._consecutive_hits = 0     # a clean response breaks the trip streak
            self._tripped_until = 0.0      # the limit lifted - close the breaker
            if self._spacing > self._baseline and self._clean_streak % RECOVER_EVERY == 0:
                self._spacing = max(self._baseline, self._spacing - RECOVER_STEP)

    def note_resource_limit(self, resource: str,
                            headers: Optional[Dict[str, Any]] = None) -> None:
        """2.238.0: an ISOLATED bucket ran dry. Cool that bucket alone until its
        X-RateLimit-Reset (falling back to Retry-After, then RESOURCE_DEFAULT_COOLDOWN),
        bounded to [1s, RESOURCE_MAX_COOLDOWN]. Leaves the shared spacing, the global cooldown
        and the circuit-breaker streak untouched."""
        wait = _reset_in_seconds(headers)
        if wait is None:
            ra = _retry_after_seconds(headers)
            wait = ra if ra is not None else RESOURCE_DEFAULT_COOLDOWN
        wait = min(RESOURCE_MAX_COOLDOWN, max(1.0, wait))
        with self._lock:
            self._resource_hits[resource] = self._resource_hits.get(resource, 0) + 1
            self._resource_until[resource] = max(self._resource_until.get(resource, 0.0),
                                                 self._clock() + wait)

    def resource_cooling(self, resource: str) -> float:
        """Seconds left on an isolated bucket's cooldown (0.0 when usable)."""
        with self._lock:
            return max(0.0, self._resource_until.get(resource, 0.0) - self._clock())

    # -- introspection ------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = self._clock()
            cooling = max(0.0, self._cooldown_until - now)
            return {
                "spacing": round(self._spacing, 3),
                "baseline": self._baseline,
                "throttled": self._spacing > self._baseline or cooling > 0,
                "cooldown_remaining": round(cooling, 2),
                "clean_streak": self._clean_streak,
                "rate_limit_hits": self._rate_limit_hits,
                "consecutive_hits": self._consecutive_hits,
                "tripped": bool(self._tripped_until) and now < self._tripped_until,
                # 2.238.0: isolated buckets (code search) - cooling seconds + lifetime hits.
                "resource_cooling": {r: round(u - now, 1)
                                     for r, u in self._resource_until.items() if u > now},
                "resource_hits": dict(self._resource_hits),
            }

    def describe(self) -> str:
        """A one-line inferred-pressure summary for -v / the banner. Empty when at rest
        (nothing to report - the common case)."""
        s = self.snapshot()
        if not s["throttled"]:
            return ""
        bits = [f"spacing {s['spacing']}s (baseline {s['baseline']}s)"]
        if s["cooldown_remaining"] > 0:
            bits.append(f"cooling {s['cooldown_remaining']}s")
        bits.append(f"{s['rate_limit_hits']} rate-limit hit(s)")
        if s.get("tripped"):
            bits.append("⛔ circuit breaker OPEN (failing fast)")
        return "🐢 GitHub governor throttling - " + ", ".join(bits)

    def reset(self) -> None:
        """Restore rest state - for tests and a fresh process boundary."""
        with self._lock:
            self._spacing = self._baseline
            self._next_allowed = 0.0
            self._cooldown_until = 0.0
            self._clean_streak = 0
            self._rate_limit_hits = 0
            self._last_rate_limit_at = None
            self._consecutive_hits = 0
            self._tripped_until = 0.0
            self._resource_until.clear()
            self._resource_hits.clear()


def _resp_parts(resp: Any):
    """(status_int, headers_dict) from a response, a bare int, or None."""
    if resp is None:
        return None, None
    if isinstance(resp, int):
        return resp, None
    status = getattr(resp, "status_code", None)
    headers = getattr(resp, "headers", None)
    return status, headers


# -- process-wide singleton -------------------------------------------------
_singleton: Optional[GitHubGovernor] = None
_singleton_lock = threading.Lock()


def governor() -> GitHubGovernor:
    """The shared process-wide governor every GitHub caller paces through. Disabled (a
    no-op pacer) when PRTRIAGE_NO_GH_GOVERNOR is set, so the test suite never sleeps."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                enabled = os.environ.get("PRTRIAGE_NO_GH_GOVERNOR") not in ("1", "true", "TRUE")
                _singleton = GitHubGovernor(enabled=enabled)
    return _singleton


def pace() -> float:
    """Module-level convenience: pace the shared governor."""
    return governor().pace()


def observe(resp: Any) -> None:
    """Module-level convenience: feed a response to the shared governor."""
    governor().observe(resp)


def resource_cooling(resource: str) -> float:
    """2.238.0: seconds left on the shared governor's cooldown for an isolated bucket."""
    return governor().resource_cooling(resource)


def isolated_limit(resp: Any) -> Optional[str]:
    """2.238.0: the isolated bucket name when `resp` is a rate-limit on one (code search ran
    dry), else None. A transient bucket exhaustion, so callers must not cache it as a miss."""
    status, headers = _resp_parts(resp)
    if status is None or not _is_rate_limited(status, headers):
        return None
    return _isolated_resource(headers)


# -- concurrency cap --------------------------------------------------------
# The governor paces TIMING (spacing between requests). GitHub's secondary limit, though,
# trips on SIMULTANEITY - too many in-flight requests at once. So a process-wide semaphore
# bounds concurrent GitHub requests independently of the timing pacer, and stays active even
# when PRTRIAGE_NO_GH_GOVERNOR disables pacing (the governor-off sweep is the burst-prone mode).
# Default 4; PRTRIAGE_GH_MAX_CONCURRENCY overrides (<= 0 disables the cap).
DEFAULT_MAX_CONCURRENCY = 4
_gh_semaphore: Any = None            # None=uninitialized, False=disabled, else a Semaphore
_gh_sem_lock = threading.Lock()


def _concurrency_limit() -> int:
    raw = os.environ.get("PRTRIAGE_GH_MAX_CONCURRENCY")
    if not raw:
        return DEFAULT_MAX_CONCURRENCY
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_CONCURRENCY


def _gh_slot():
    """A context manager bounding CONCURRENT in-flight GitHub requests, process-wide. A plain
    Semaphore is itself a context manager (acquire on enter, release on exit); when the cap is
    disabled we hand back a nullcontext so the call path is identical."""
    global _gh_semaphore
    if _gh_semaphore is None:
        with _gh_sem_lock:
            if _gh_semaphore is None:
                n = _concurrency_limit()
                _gh_semaphore = threading.Semaphore(n) if n > 0 else False
    return _gh_semaphore if _gh_semaphore else contextlib.nullcontext()


def reset_concurrency() -> None:
    """Drop the cached semaphore so the next call re-reads PRTRIAGE_GH_MAX_CONCURRENCY (tests)."""
    global _gh_semaphore
    _gh_semaphore = None


def governed(do_request: Callable[[], Any], *, retries: int = 1) -> Any:
    """2.35.0 (#2 phase 2): run a request THUNK under the shared governor, return its response.

    `do_request` must perform the actual `requests.*` call and return the response - kept in
    the CALLER's module so `mock.patch.object(module.requests, "get")` still intercepts it,
    and so each site keeps its own error handling / fallback around this call.

    ENABLED: pace() → do_request() → observe(resp); if the response is a rate-limit (403/429
    with a rate signal) and attempts remain, loop - observe() has opened a cooldown, so the
    next pace() SLEEPS it (honoring Retry-After) before retrying the call in place. This is
    the sleep-and-retry phase 1 lacked; the caller's existing fallback (e.g. bail to cookies)
    still runs afterward if every attempt is throttled.

    DISABLED (PRTRIAGE_NO_GH_GOVERNOR / tests): exactly ONE call, no pacing, no retry - behavior
    and call-count identical to a bare `requests.*`. A transport error from `do_request`
    propagates unchanged (the caller already handles it); only rate-limit RESPONSES retry."""
    g = governor()
    if not g._enabled:
        with _gh_slot():            # cap concurrency even when pacing is off
            resp = do_request()
        capture_throttle(resp)      # still record throttles on governor-off sweeps
        return resp
    resp = None
    for attempt in range(retries + 1):
        g.pace()
        with _gh_slot():
            resp = do_request()
        g.observe(resp)
        capture_throttle(resp)
        status, headers = _resp_parts(resp)
        # 2.238.0: never retry an isolated-bucket exhaustion in place - the bucket resets per
        # minute, so a retry after the 2s default cooldown is a guaranteed second 403.
        if (attempt < retries and status is not None and _is_rate_limited(status, headers)
                and not _isolated_resource(headers)):
            continue
        return resp
    return resp


# -- secondary-limit telemetry ----------------------------------------------
# GitHub hides the secondary/burst limit from /rate_limit, so the only way to UNDERSTAND it
# (when it trips, the real Retry-After cooldown, which endpoints, the inferred spacing +
# concurrency at that instant) is to record every throttle ourselves. governed() is the seam
# every prtriage GitHub call routes through in BOTH modes (paced and PRTRIAGE_NO_GH_GOVERNOR), so a
# capture here sees governor-off sweeps too. Append-only JSONL; best-effort; no extra GitHub
# calls (it records the 403s we already receive). Analyse later to tune D1 / the concurrency
# cap instead of guessing the invisible ceiling.
_SECONDARY_LOG_ENV = "PRTRIAGE_SECONDARY_LIMIT_LOG"       # override the log path
_SECONDARY_LOG_OFF = "PRTRIAGE_NO_SECONDARY_LIMIT_LOG"    # kill-switch (set by the test conftest)


def secondary_log_path() -> Optional[str]:
    """The durable secondary-limit log path, or None when capture is disabled. Default
    ~/.cache/prtriage/secondary-limit.jsonl; overridable via PRTRIAGE_SECONDARY_LIMIT_LOG; off under
    PRTRIAGE_NO_SECONDARY_LIMIT_LOG."""
    if os.environ.get(_SECONDARY_LOG_OFF) in ("1", "true", "TRUE"):
        return None
    override = os.environ.get(_SECONDARY_LOG_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".cache", "prtriage", "secondary-limit.jsonl")


def _throttle_record(resp: Any, status: Any, headers: Any) -> Dict[str, Any]:
    h = headers or {}
    def _hget(k: str) -> Any:
        try:
            return h.get(k)
        except Exception:
            return None
    retry_after = _retry_after_seconds(headers)
    remaining = _hget("X-RateLimit-Remaining")
    body_lc = ""
    try:
        body_lc = (getattr(resp, "text", "") or "")[:400].lower()
    except Exception:
        body_lc = ""
    # secondary/burst = a Retry-After (or an explicit "secondary" body) while the core budget
    # is NOT zero; core exhaustion = X-RateLimit-Remaining 0 with no Retry-After.
    secondary = ("secondary" in body_lc
                 or (retry_after is not None and remaining not in (None, "", "0")))
    url = None
    try:
        url = getattr(resp, "url", None) or getattr(getattr(resp, "request", None), "url", None)
        if url:
            url = str(url).split("?", 1)[0]
    except Exception:
        url = None
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "status": status,
        "kind": "secondary" if secondary else "core",
        "retry_after": retry_after,
        "remaining": remaining,
        "limit": _hget("X-RateLimit-Limit"),
        "resource": _hget("X-RateLimit-Resource"),
        "url": url,
        "spacing": governor().snapshot().get("spacing"),
    }


def capture_throttle(resp: Any) -> None:
    """Record a GitHub THROTTLE response (secondary/burst OR core exhaustion) as one JSONL
    line in the durable secondary-limit log. Best-effort: never raises, never blocks the
    request path, and returns immediately for a non-throttle response (the common case)."""
    try:
        status, headers = _resp_parts(resp)
        if status is None or not _is_rate_limited(status, headers):
            return
        path = secondary_log_path()
        if not path:
            return
        rec = _throttle_record(resp, status, headers)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def summarize_secondary_log(path: Optional[str] = None) -> Dict[str, Any]:
    """Read the secondary-limit JSONL and summarize it: total throttles, split by
    secondary vs core, by X-RateLimit-Resource, Retry-After min/median/max, distinct PIDs,
    and the first/last timestamps. {'events': 0} when the log is absent/empty."""
    path = path or secondary_log_path()
    recs: List[Dict[str, Any]] = []
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            recs.append(json.loads(line))
                        except Exception:
                            continue
        except Exception:
            pass
    if not recs:
        return {"events": 0, "path": path}
    kinds: Dict[str, int] = {}
    resources: Dict[str, int] = {}
    ras: List[float] = []
    pids = set()
    for r in recs:
        kinds[r.get("kind", "?")] = kinds.get(r.get("kind", "?"), 0) + 1
        res = r.get("resource") or "?"
        resources[res] = resources.get(res, 0) + 1
        if isinstance(r.get("retry_after"), (int, float)):
            ras.append(float(r["retry_after"]))
        if r.get("pid") is not None:
            pids.add(r["pid"])
    ra_stats = None
    if ras:
        ras.sort()
        ra_stats = {"min": ras[0], "median": ras[len(ras) // 2], "max": ras[-1]}
    return {
        "events": len(recs), "path": path,
        "by_kind": kinds, "by_resource": resources,
        "retry_after": ra_stats, "distinct_pids": len(pids),
        "first": recs[0].get("ts"), "last": recs[-1].get("ts"),
    }


def format_secondary_summary(path: Optional[str] = None) -> str:
    """A human-readable one-block summary of the secondary-limit log for the CLI."""
    s = summarize_secondary_log(path)
    if not s.get("events"):
        return f"No secondary-limit events recorded yet ({s.get('path')})."
    lines = [f"📉 GitHub throttle history - {s['events']} event(s)  [{s['path']}]",
             f"   window: {s['first']} → {s['last']}",
             f"   by kind: " + ", ".join(f"{k}={v}" for k, v in sorted(s['by_kind'].items())),
             f"   by resource: " + ", ".join(f"{k}={v}" for k, v in sorted(s['by_resource'].items())),
             f"   distinct processes: {s['distinct_pids']}"]
    if s.get("retry_after"):
        ra = s["retry_after"]
        lines.append(f"   Retry-After secs: min {ra['min']} · median {ra['median']} · max {ra['max']}")
    return "\n".join(lines)
