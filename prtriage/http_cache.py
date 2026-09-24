"""ETag + disk cache for GitHub GETs (#1).

A GitHub conditional request (`If-None-Match: <etag>`) that comes back **304 Not
Modified does NOT count against the REST rate limit** - so a resource re-read while
unchanged is free. prtriage re-reads the same PR constantly: a dev re-run, a `--batch`
sweep's working set, and now the lpr forward interlock re-triaging the very PR the dev
just triaged. This stores each GET's ETag + body under XDG and revalidates on the next
read: 304 → serve the cached body for free; 200 → GitHub changed, refresh. Correctness
is GitHub's to keep - we only reuse a body when the server confirms it's unchanged.

Scope (the deliberate slice): a body-or-None conditional GET, wired into
`get_pull_json` (the most-reused read). Richer call sites that branch on status codes
keep their direct requests. `PRTRIAGE_NO_GH_CACHE=1` disables it (plain uncached GET).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter

from . import governor

log = logging.getLogger(__name__)

_DISABLE_ENV = "PRTRIAGE_NO_GH_CACHE"

# 2.86.0 (perf P1, Fable-proposed / Opus-verified): ONE process-wide pooled Session for
# every GitHub GET. Bare `requests.get(...)` builds a throwaway connection each call - and
# under a proxied HTTPS env that means a fresh proxy CONNECT tunnel + TLS handshake per
# request, measured at ~40% of a single PR triage. A kept-alive pooled session pays the
# tunnel once per host and reuses it. Shared via gh_session() so comments's GitHub calls
# ride the same pool (identical requests, identical bytes - transport reuse only, no
# caching/verdict semantics touched).
_GH_SESSION: Optional[requests.Session] = None
# Captured at import: the REAL requests.get/post. The suite pervasively mocks the
# network via `requests.get = fake` (module-attr patch). gh_get/gh_post honor that seam
# - if `requests.get` is no longer the real function, a test patched it, so we call it;
# otherwise (production) we route through the pooled session for connection reuse. This
# keeps every existing mock working while still pooling live traffic.
_REAL_GET = requests.get
_REAL_POST = requests.post
_REAL_DELETE = getattr(requests, "delete", None)


def gh_session() -> requests.Session:
    """The shared pooled Session for GitHub GETs (lazily built)."""
    global _GH_SESSION
    if _GH_SESSION is None:
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _GH_SESSION = s
    return _GH_SESSION


def gh_get(url: str, **kwargs: Any):
    """Pooled GET for GitHub - reuses one kept-alive connection in production; defers to
    a test-patched `requests.get` when one is installed (identity check)."""
    if requests.get is _REAL_GET:
        return gh_session().get(url, **kwargs)
    return requests.get(url, **kwargs)


def gh_post(url: str, **kwargs: Any):
    """Pooled POST for GitHub - same identity-honoring shim as gh_get."""
    if requests.post is _REAL_POST:
        return gh_session().post(url, **kwargs)
    return requests.post(url, **kwargs)


def gh_delete(url: str, **kwargs: Any):
    """Pooled DELETE for GitHub - same identity-honoring shim as gh_get (defers to a
    test-patched `requests.delete` when one is installed)."""
    real_delete = getattr(requests, "delete", None)
    sess = gh_session()
    if real_delete is _REAL_DELETE and hasattr(sess, "delete"):
        return sess.delete(url, **kwargs)
    return requests.delete(url, **kwargs)


def _disabled() -> bool:
    return bool(os.environ.get(_DISABLE_ENV, "").strip())


def _cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "prtriage", "gh")


def _entry_path(url: str) -> str:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return os.path.join(_cache_dir(), key + ".json")


def _load(url: str) -> Optional[dict]:
    try:
        with open(_entry_path(url), "r", encoding="utf-8") as fh:
            e = json.load(fh)
        if isinstance(e, dict) and "etag" in e and "body" in e:
            return e
    except (OSError, ValueError):
        pass
    return None


def _store(url: str, etag: str, body: Any) -> None:
    try:
        os.makedirs(_cache_dir(), exist_ok=True)
        with open(_entry_path(url), "w", encoding="utf-8") as fh:
            json.dump({"etag": etag, "body": body}, fh)
    except OSError as exc:  # cache is best-effort - a write failure never breaks a read
        log.debug("http_cache: can't write %s: %s", url, exc)


def conditional_get_json(
    url: str, token: Optional[str],
    accept: str = "application/vnd.github+json", timeout: int = 30,
) -> Optional[Any]:
    """GET `url` with a stored-ETag conditional request → parsed JSON body, or None
    (no token / non-200-non-304 / transport error). Never raises. A 304 serves the
    disk-cached body (free); a 200 refreshes the cache. Uncached plain GET when
    PRTRIAGE_NO_GH_CACHE is set."""
    if not token:
        return None
    headers = {"Accept": accept, "Authorization": f"token {token}"}
    if _disabled():
        return _plain_get(url, headers, timeout)
    cached = _load(url)
    if cached and cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    try:
        # 2.34.0/2.35.0 (#2): pace + observe + Retry-After sleep-and-retry, shared governor.
        resp = governor.governed(
            lambda: gh_get(url, headers=headers, timeout=timeout))
    except Exception as exc:  # noqa: BLE001 - best-effort, never load-bearing
        log.debug("http_cache: fetch failed %s: %s", url, exc)
        return None
    if resp.status_code == 304 and cached is not None:
        log.debug("http_cache: 304 (free) %s", url)
        return cached["body"]
    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError:
            return None
        etag = resp.headers.get("ETag")
        if etag:
            _store(url, etag, body)
        return body
    log.debug("http_cache: HTTP %s %s", resp.status_code, url)
    return None


# 2.66.0 (perf #3, Opus-verified) / 2.67.0 (#3b): a disk store for text fetched at an
# IMMUTABLE 40-hex commit SHA. Content at a pinned SHA can never change, so - unlike the
# branch-keyed relatedness fetches - it needs NO revalidation: a hit skips the network
# entirely (0 GitHub calls), which the ETag path can't do (a 304 is still a round-trip and
# still counts toward the secondary/abuse limit the D1 series fought). Two callers, both
# provably immutable: (a) fetch_source on the routine/sweep path, where the `branch` arg is
# the build's git_hash (cli.py:3884); (b) the PR diff BODY keyed by head.sha in
# fetch_pr_diff_and_branch - the actual repeated fetch when a PR-range sweep is re-run. Refs
# that are NOT a 40-hex SHA (mutable branch names) are deliberately never persisted.
def _src_entry_path(cache_key: str) -> str:
    key = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:32]
    return os.path.join(_cache_dir(), "src", key + ".json")


def load_source_entry(cache_key: str) -> Optional[dict]:
    """The persisted {status, text} for an immutable-SHA source fetch, or None on a miss
    (or when disabled). status is "200" (text is the body) or "404" (definitively absent at
    that SHA - text is ""). Never raises."""
    if _disabled():
        return None
    try:
        with open(_src_entry_path(cache_key), "r", encoding="utf-8") as fh:
            e = json.load(fh)
        if isinstance(e, dict) and "status" in e and "text" in e:
            return e
    except (OSError, ValueError):
        pass
    return None


def store_source_entry(cache_key: str, status: str, text: str) -> None:
    """Persist a DEFINITIVE immutable-SHA source outcome - only status "200"/"404" (a
    transport error must never be cached as absence). Best-effort; a write failure is a
    debug log, never load-bearing."""
    if _disabled() or status not in ("200", "404"):
        return
    try:
        os.makedirs(os.path.join(_cache_dir(), "src"), exist_ok=True)
        with open(_src_entry_path(cache_key), "w", encoding="utf-8") as fh:
            json.dump({"status": status, "text": text}, fh)
    except OSError as exc:  # best-effort - a write failure never breaks a fetch
        log.debug("http_cache: can't write source %s: %s", cache_key, exc)


# --------------------------------------------------------------------------
# 2.103.0 (perf #1 follow-on): source-cache GC - the "memory leak" backstop.
# --------------------------------------------------------------------------
# Both stores under gh/ are APPEND-ONLY: the top-level ETag store (one entry per unique
# URL - old PRs' bodies never revisited) and gh/src/ (one entry per immutable-SHA source
# fetch). 2.102's base.sha pinning ACCELERATES the src store: every board PR now writes
# source entries, and each PR re-sync changes its base.sha → the old snapshot's entries
# orphan forever. Left alone the dir grows unbounded ("39479 will feel like a memory
# leak"). Eviction is ALWAYS SAFE here: every entry is content at an immutable key (a SHA
# source body, or an ETag-revalidated body), so a pruned miss just re-fetches - never a
# wrong answer, only a re-fetch cost. Policy: drop entries older than an age cap, then
# LRU-evict (oldest mtime first) until under a size cap. Tune via PRTRIAGE_CACHE_MAX_AGE_DAYS /
# PRTRIAGE_CACHE_MAX_MB; PRTRIAGE_NO_GH_CACHE disables the whole store (so nothing to prune).
_PRUNE_STAMP = ".last_prune"


def _default_max_age_days() -> int:
    try:
        return max(1, int(os.environ.get("PRTRIAGE_CACHE_MAX_AGE_DAYS", "30")))
    except (TypeError, ValueError):
        return 30


def _default_max_mb() -> int:
    try:
        return max(1, int(os.environ.get("PRTRIAGE_CACHE_MAX_MB", "200")))
    except (TypeError, ValueError):
        return 200


def _all_entry_files() -> list:
    """Every cache-entry file under gh/ (top-level ETag store + src/), as (path, stat).
    Skips the prune stamp and any non-.json file. Never raises - an unreadable entry is
    simply omitted."""
    root = _cache_dir()
    out = []
    try:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".json"):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    out.append((p, os.stat(p)))
                except OSError:
                    pass
    except OSError:
        pass
    return out


def _safe_unlink(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def cache_stats() -> dict:
    """{files, bytes, oldest, newest} across the whole gh/ cache tree (mtimes are epoch
    seconds or None on an empty cache). Read-only, no network."""
    files = _all_entry_files()
    total = sum(st.st_size for _p, st in files)
    mtimes = [st.st_mtime for _p, st in files]
    return {"files": len(files), "bytes": total,
            "oldest": min(mtimes) if mtimes else None,
            "newest": max(mtimes) if mtimes else None}


def prune_cache(max_age_days: Optional[int] = None, max_mb: Optional[int] = None,
                now: Optional[float] = None) -> tuple:
    """Evict entries older than max_age_days, then LRU-evict (oldest mtime first) until the
    survivors fit under max_mb. Returns (removed_count, freed_bytes). SAFE by construction
    (immutable-key content → a pruned miss re-fetches). Best-effort: a stat/unlink error
    skips that file, never raises. A no-op when PRTRIAGE_NO_GH_CACHE is set."""
    if _disabled():
        return 0, 0
    max_age_days = _default_max_age_days() if max_age_days is None else max_age_days
    max_mb = _default_max_mb() if max_mb is None else max_mb
    now = time.time() if now is None else now
    removed = 0
    freed = 0
    cutoff = now - max_age_days * 86400
    survivors = []
    for p, st in _all_entry_files():
        if st.st_mtime < cutoff:
            if _safe_unlink(p):
                removed += 1
                freed += st.st_size
        else:
            survivors.append((p, st))
    cap = max_mb * 1024 * 1024
    total = sum(st.st_size for _p, st in survivors)
    if total > cap:
        survivors.sort(key=lambda ps: ps[1].st_mtime)  # oldest first
        for p, st in survivors:
            if total <= cap:
                break
            if _safe_unlink(p):
                removed += 1
                freed += st.st_size
                total -= st.st_size
    return removed, freed


def maybe_autoprune(interval_hours: int = 24, max_age_days: Optional[int] = None,
                    max_mb: Optional[int] = None) -> Optional[tuple]:
    """Run prune_cache at most once per interval_hours, guarded by a stamp file's mtime -
    opportunistic self-maintenance so a board run keeps the cache bounded with no user
    action. Returns (removed, freed) when it ran, else None. Best-effort, silent, never
    raises. Call at board START (before the concurrent fetches), so age/size eviction of OLD
    entries can never race a fresh write from this run."""
    if _disabled():
        return None
    stamp = os.path.join(_cache_dir(), _PRUNE_STAMP)
    try:
        if time.time() - os.stat(stamp).st_mtime < interval_hours * 3600:
            return None
    except OSError:
        pass  # missing/unreadable stamp → due for a prune
    result = prune_cache(max_age_days=max_age_days, max_mb=max_mb)
    try:
        os.makedirs(_cache_dir(), exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
    except OSError as exc:
        log.debug("http_cache: can't write prune stamp: %s", exc)
    return result


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def _fmt_age(epoch: Optional[float], now: Optional[float] = None) -> str:
    if not epoch:
        return "-"
    days = (( time.time() if now is None else now) - epoch) / 86400
    return f"{days:.1f}d ago"


def run_cache(argv: list) -> int:
    """`prtriage cache` - inspect or prune prtriage's on-disk GitHub cache (the ETag body store +
    the immutable-SHA source store). Default prints stats; --prune evicts by age then size.
    Pruning is always safe: every entry is immutable-key content, so a miss just re-fetches."""
    import argparse
    p = argparse.ArgumentParser(prog="prtriage cache", description=run_cache.__doc__)
    p.add_argument("--prune", action="store_true",
                   help="evict entries older than --max-age-days, then LRU-evict to --max-mb")
    p.add_argument("--max-age-days", type=int, default=None,
                   help=f"age cap for --prune (default {_default_max_age_days()}, "
                        "env PRTRIAGE_CACHE_MAX_AGE_DAYS)")
    p.add_argument("--max-mb", type=int, default=None,
                   help=f"size cap for --prune (default {_default_max_mb()}MB, "
                        "env PRTRIAGE_CACHE_MAX_MB)")
    args = p.parse_args(argv)

    st = cache_stats()
    print(f"📦 prtriage GitHub cache: {_cache_dir()}")
    print(f"   {st['files']} entries · {_fmt_bytes(st['bytes'])}"
          f" · oldest {_fmt_age(st['oldest'])} · newest {_fmt_age(st['newest'])}")
    if _disabled():
        print("   (PRTRIAGE_NO_GH_CACHE is set - cache disabled)")
        return 0
    if not args.prune:
        print(f"   prune with:  prtriage cache --prune   "
              f"(defaults: age>{_default_max_age_days()}d, cap {_default_max_mb()}MB)")
        return 0
    removed, freed = prune_cache(max_age_days=args.max_age_days, max_mb=args.max_mb)
    after = cache_stats()
    print(f"🧹 pruned {removed} entr{'y' if removed == 1 else 'ies'}, "
          f"freed {_fmt_bytes(freed)} → now {after['files']} entries · "
          f"{_fmt_bytes(after['bytes'])}")
    return 0


def _plain_get(url: str, headers: dict, timeout: int) -> Optional[Any]:
    try:
        resp = governor.governed(
            lambda: gh_get(url, headers=headers, timeout=timeout))
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        log.debug("http_cache: plain fetch failed %s: %s", url, exc)
    return None
