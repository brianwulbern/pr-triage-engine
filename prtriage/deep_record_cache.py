"""3.0.88: a cache of the board's --deep-review re-triage (design review 10-30).

The deep pass re-triages every cautious-band row in full on every board, though the model's
answer is already cached (one model call in a 1044 s phase on 10-30). A deep record is reused
only while everything its verdict rests on is unchanged: the head, the build set, the
sign-off, the primary model, the prompt version, VERDICT_LOGIC, and a digest of the CURRENT
no-deep record (reason, counts and every gate input per failure), so base history, a moved
branch tip or a new standing failure that changes the no-deep row also drops the deep one.
Every deep record ages out after config.PULLS_CACHE_TTL_SECONDS, greens included.

Shadow first: until PRTRIAGE_DEEP_CACHE_SERVE=1 the board still runs the fresh deep pass and only
logs what the cache would have served next to it (deep-cache-shadow.jsonl).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence

_SCHEMA = 1


def serving() -> bool:
    return os.environ.get("PRTRIAGE_DEEP_CACHE_SERVE", "") == "1"


def _dir() -> str:
    from . import config
    return os.path.join(config.cache_root(), "prtriage", "deep-records")


def _path(repo: str, pr: str) -> str:
    return os.path.join(_dir(), f"{repo.replace('/', '_')}__{pr}.json")


def _failure_inputs(f: Dict[str, Any]) -> Dict[str, Any]:
    return {k: f.get(k) for k in ("name", "relatedness_level", "diff_source_file",
                                  "is_critical", "task_not_executed", "error_digest",
                                  "fails_in_reference_release")} | \
        {"never_ran_standing": bool(f.get("never_ran_standing"))}


def gate_digest(nodeep_result: Optional[Dict[str, Any]]) -> Optional[str]:
    """sha256 of what the no-deep row and the deep green gate read, or None when the record
    can't be digested (then nothing is stored or served)."""
    if not isinstance(nodeep_result, dict) or not isinstance(nodeep_result.get("verdict"), dict):
        return None
    v = nodeep_result["verdict"]
    fails = sorted((_failure_inputs(f) for f in nodeep_result.get("unioned_failures") or []
                    if isinstance(f, dict)), key=lambda f: str(f.get("name")))
    body = {"reason_code": v.get("reason_code"), "action": v.get("action"), "orb": v.get("orb"),
            "unique_count": v.get("unique_count"), "per_trigger_unique": v.get("per_trigger_unique"),
            "failures": fails}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def key(head_sha: Optional[str], build_ids: Sequence[Any], signed_off: bool,
        digest: Optional[str]) -> Optional[Dict[str, Any]]:
    from . import config, deep_analysis
    builds = sorted({str(b) for b in build_ids or [] if b})
    if not (head_sha and builds and digest):
        return None
    chain = deep_analysis.model_chain()
    return {"head_sha": head_sha, "build_ids": builds, "signed_off": bool(signed_off),
            "model": chain[0] if chain else "", "prompt": deep_analysis._PROMPT_VERSION,
            "verdict_logic": int(getattr(config, "VERDICT_LOGIC", 0)), "gate_digest": digest}


def lookup(repo: str, pr: str, k: Optional[Dict[str, Any]],
           now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The stored deep record for exactly this key, younger than the TTL, else None."""
    from . import config
    if not k:
        return None
    try:
        with open(_path(repo, pr), encoding="utf-8") as fh:
            entry = json.load(fh)
    except (OSError, ValueError):
        return None
    if entry.get("schema") != _SCHEMA or entry.get("key") != k:
        return None
    age = (now if now is not None else time.time()) - float(entry.get("stored_at") or 0)
    if age > config.PULLS_CACHE_TTL_SECONDS:
        return None
    return entry.get("record")


def storable(record: Dict[str, Any], k: Optional[Dict[str, Any]]) -> bool:
    """Only a complete, error-free deep record whose answer came from the primary model."""
    if not k or not isinstance(record, dict):
        return False
    env, result = record.get("_audit") or {}, record.get("result")
    if env.get("error") or not isinstance(result, dict):
        return False
    v = result.get("verdict") or {}
    if not isinstance(v, dict) or v.get("incomplete"):
        return False
    da = result.get("deep_analysis") or {}
    return isinstance(da, dict) and bool(da.get("model")) and da.get("model") == k["model"]


def store(repo: str, pr: str, k: Dict[str, Any], record: Dict[str, Any]) -> None:
    try:
        os.makedirs(_dir(), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_dir(), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"schema": _SCHEMA, "key": k, "stored_at": time.time(),
                       "record": record}, fh)
        os.replace(tmp, _path(repo, pr))
    except OSError:
        pass


def evict(repo: str, pr: str) -> None:
    try:
        os.remove(_path(repo, pr))
    except OSError:
        pass


def _orb(record: Optional[Dict[str, Any]]) -> Optional[List[Any]]:
    v = ((record or {}).get("result") or {}).get("verdict") or {}
    return [v.get("orb"), v.get("reason_code"), v.get("action")] if v else None


_RANK = {"🟢": 0, "🟡": 1, "⏳": 2, "⚠️": 3, "🟠": 4, "🔴": 5, "⛔": 6}


def shadow(url: str, cached: Optional[Dict[str, Any]], fresh: Dict[str, Any]) -> None:
    """One jsonl row per candidate: what the cache would have served next to the fresh deep
    verdict. `greener` marks the failure the review's pass mark counts."""
    from . import config
    c, f = _orb(cached), _orb(fresh)
    row = {"at": time.time(), "url": url, "hit": cached is not None, "cached": c, "fresh": f,
           "match": c == f if cached is not None else None,
           "greener": bool(c and f and _RANK.get(c[0], 9) < _RANK.get(f[0], 9))}
    try:
        d = os.path.join(config.cache_root(), "prtriage")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "deep-cache-shadow.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
