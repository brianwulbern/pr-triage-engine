"""The readiness board: triage many open PRs concurrently and group them by verdict.

The runner is the same shape as production's: a thread pool of triage workers, while the
GitHub governor serializes GitHub traffic across them (so the concurrency buys overlap on the
other, slower service, not a GitHub burst). After each PR completes it checks rate-limit
pressure and bails with a partial board only when the governor is genuinely crawling AND enough
PRs remain to make grinding worse than stopping (board_limits._should_crawl_abort). In-flight
PRs always finish; only not-yet-started ones are cancelled. A tripped circuit breaker fails
fast wherever the next GitHub call happens.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Sequence, Tuple

from . import board_limits, governor
from .verdict import GREEN, INVESTIGATE, LABELS, LEAN_OK, RED, REVIEW

log = logging.getLogger(__name__)

ORDER = (RED, INVESTIGATE, REVIEW, LEAN_OK, GREEN)


def run_board(targets: Sequence[Any], triage_one: Callable[[Any], Dict[str, Any]], *,
              concurrency: int = 5, abort_on_throttle: bool = True
              ) -> Tuple[List[Dict[str, Any]], bool]:
    """Triage every target with `triage_one`, up to `concurrency` at once.
    Returns (records in input order, aborted)."""
    by_target: Dict[int, Dict[str, Any]] = {}
    aborted = False
    pool = ThreadPoolExecutor(max_workers=max(1, concurrency))
    try:
        futs = {pool.submit(triage_one, t): i for i, t in enumerate(targets)}
        done = 0
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                by_target[i] = fut.result()
            except governor.GovernorTripped as exc:
                log.warning("⛔ circuit breaker tripped (%s); aborting queued PRs", exc)
                aborted = True
                break
            done += 1
            if abort_on_throttle:
                crawl = board_limits._should_crawl_abort(len(targets) - done)
                if crawl:
                    log.warning("⛔ rate-limit crawl after %d/%d done: %s; aborting %d queued, "
                                "letting in-flight finish", done, len(targets), crawl,
                                len(targets) - done)
                    aborted = True
                    break
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return [by_target[i] for i in sorted(by_target)], aborted


def render_board(records: List[Dict[str, Any]], aborted: bool = False) -> str:
    """Group records (each with a `verdict` and `pr`) into the readiness board."""
    buckets: Dict[str, List[Dict[str, Any]]] = {o: [] for o in ORDER}
    for r in records:
        buckets[r["verdict"].orb].append(r)
    out = ["=" * 72, f"OPEN-PR READINESS BOARD: {len(records)} PR(s)", "=" * 72]
    for orb in ORDER:
        rows = buckets[orb]
        if not rows:
            continue
        out.append(f"\n{orb} {LABELS[orb]} ({len(rows)})")
        for r in sorted(rows, key=lambda x: -x["pr"]):
            head = r["verdict"].headline
            out.append(f"    #{r['pr']}  {head[:96]}{'...' if len(head) > 96 else ''}")
    out.append("\n" + "  ".join(f"{o} {len(buckets[o])}" for o in ORDER))
    if aborted:
        out.append("⛔ aborted early on a rate limit; the rows above are complete, the rest were "
                   "not triaged.")
    return "\n".join(out)
