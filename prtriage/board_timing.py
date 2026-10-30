"""3.0.85: where a `prtriage pulls` board spends its time.

PRTRIAGE_BOARD_TIMINGS=<path> wraps the board's phases, the per-pull triage, the quick-fix and
model calls, every HTTP request (by host) and every subprocess (by command), then writes one
JSON report to <path> at exit and prints the top rows to stderr. Phases run one at a time, so
their seconds are wall time; the rest run on the pool's threads, so theirs are thread-seconds
(they overlap, and nested spans count in both). Off unless the variable is set.
"""
from __future__ import annotations

import atexit
import functools
import json
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

_LOCK = threading.Lock()
_SPANS: Dict[str, Dict[str, Any]] = {}
_SLOW: Dict[str, List[Tuple[float, str]]] = {}
_SLOW_KEEP = 10
_STARTED = 0.0
_INSTALLED = False

PHASES = (
    "_budget_preflight", "_resolve_targets", "_partition_by_cache", "_run_targets",
    "_recheck_pending", "_apply_deep_review", "_tag_db_infra_upgrade", "_post_reds",
    "_local_run_clearances", "_base_repro_clearances", "_ci_trigger_candidates",
    "_offer_trigger_ci", "_forward_greens", "_batch_landing_filter", "_offer_close_landed",
    "_show_same_ticket_pairs", "_offer_close_superseded", "_offer_fixes", "_offer_carry",
    "_offer_announce",
)
PER_CALL = (
    ("pulls", "_run_one_cached", True), ("pulls", "_cheap_pr_state", False),
    ("audit", "_run_one", True), ("quick_fix", "check_pull", True),
    ("deep_analysis", "_run_via_cli", False), ("deep_analysis", "_run_via_api", False),
    ("ai_review", "board_step", False), ("ai_review", "pull_state", False),
)


def _add(name: str, seconds: float, label: Optional[str] = None) -> None:
    with _LOCK:
        s = _SPANS.setdefault(name, {"calls": 0, "seconds": 0.0, "max": 0.0})
        s["calls"] += 1
        s["seconds"] += seconds
        s["max"] = max(s["max"], seconds)
        if label is not None:
            slow = _SLOW.setdefault(name, [])
            slow.append((seconds, label))
            slow.sort(reverse=True)
            del slow[_SLOW_KEEP:]


def _label(args: tuple) -> Optional[str]:
    return str(args[0])[-80:] if args and isinstance(args[0], str) else None


def _wrap(fn: Callable, name: str, keep_slow: bool) -> Callable:
    @functools.wraps(fn)
    def timed(*args, **kwargs):
        t = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            _add(name, time.monotonic() - t, _label(args) if keep_slow else None)
    timed._board_timing = True
    return timed


def _rebind(module: Any, attr: str, name: str, keep_slow: bool) -> None:
    """Replace `attr` in its module and in every loaded prtriage module that imported it by name."""
    fn = getattr(module, attr, None)
    if fn is None or getattr(fn, "_board_timing", False):
        return
    timed = _wrap(fn, name, keep_slow)
    for mod in list(sys.modules.values()):
        if (getattr(mod, "__name__", "") or "").startswith("prtriage") \
                and getattr(mod, attr, None) is fn:
            setattr(mod, attr, timed)


def _host(url: Any) -> str:
    host = (urlparse(str(url)).hostname or "?").lower()
    for key, name in (("github", "github"), ("report", "report"), ("googleapis", "gcs"),
                      ("jenkins", "jenkins"), ("test-1-", "jenkins"),
                      ("tracker", "tracker"), ("anthropic", "anthropic"),
                      ("repository", "nexus"), ("nexus", "nexus")):
        if key in host:
            return name
    return host


def _url_of(req: Any) -> Any:
    return getattr(req, "full_url", req)


_COLLAPSE_AFTER = {"contents": "{path}", "branches": "{branch}", "compare": "{range}",
                   "heads": "{ref}", "tags": "{ref}"}


def _gh_endpoint(url: Any) -> str:
    """3.0.86: the request's path with numbers, SHAs and free-form tails replaced, so the
    report groups calls by GitHub endpoint."""
    import re
    parts = [p for p in urlparse(str(url)).path.split("/") if p]
    out: List[str] = []
    for p in parts:
        if out and out[-1] in _COLLAPSE_AFTER:
            out.append(_COLLAPSE_AFTER[out[-1]])
            break
        if p.isdigit():
            out.append("{n}")
        elif re.fullmatch(r"[0-9a-f]{7,40}", p):
            out.append("{sha}")
        else:
            out.append(p)
    return "/" + "/".join(out)


def _http(method: str, url: Any, seconds: float, status: Optional[int],
          params: Any = None) -> None:
    host = _host(url)
    _add(f"http:{host}", seconds)
    if host == "github":
        _add(f"gh:{(method or 'GET').upper()} {_gh_endpoint(url)}", seconds)
        if status == 304:
            _add("gh:304 not modified (free)", seconds)
        if "/contents/" in str(url):
            _add(f"ghc:{_ref_kind(url, params)} {_caller()}", seconds)


_SKIP_FRAMES = {"board_timing", "governor", "http_cache"}
_SKIP_FUNCS = {"<lambda>", "fetch_source"}


def _caller() -> str:
    """3.0.87: the two nearest prtriage frames above the HTTP layer, so a /contents read can be
    traced to the code that asked for it. 3.0.89: above fetch_source, which every read
    passes through."""
    out: List[str] = []
    f = sys._getframe(1)
    while f is not None and len(out) < 2:
        mod = f.f_globals.get("__name__", "")
        if mod.startswith("prtriage.") and mod.rsplit(".", 1)[-1] not in _SKIP_FRAMES \
                and f.f_code.co_name not in _SKIP_FUNCS:
            out.append(f"{mod.rsplit('.', 1)[-1]}.{f.f_code.co_name}:{f.f_lineno}")
        f = f.f_back
    return " < ".join(out) or "?"


def _ref_kind(url: Any, params: Any = None) -> str:
    """sha, master, branch or default. 3.0.89: also from `params`, where fetch_source puts
    the ref."""
    import re
    from urllib.parse import parse_qs
    ref = (params or {}).get("ref") if isinstance(params, dict) else None
    ref = ref or (parse_qs(urlparse(str(url)).query).get("ref") or [""])[0]
    if not ref:
        return "default"
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return "sha"
    return "master" if ref == "master" else "branch"


def _status(resp: Any, err: Optional[BaseException]) -> Optional[int]:
    for obj, attr in ((resp, "status_code"), (resp, "status"), (err, "code")):
        code = getattr(obj, attr, None)
        if isinstance(code, int):
            return code
    return None


def _cmd(args: Any) -> str:
    argv = [str(a) for a in (args if isinstance(args, (list, tuple)) else str(args).split())]
    if not argv:
        return "?"
    head = os.path.basename(argv[0])
    if head == "gh" and len(argv) > 1:
        return f"gh {argv[1]}"
    if head != "git":
        return head
    rest = iter(argv[1:])
    for a in rest:
        if a in ("-C", "-c"):
            next(rest, None)
        elif not a.startswith("-"):
            return f"git {a}"
    return "git"


def _patch_io() -> None:
    import subprocess
    import urllib.request

    try:
        import requests
        orig_request = requests.Session.request

        def request(session, method, url, *a, **kw):
            t, resp, err = time.monotonic(), None, None
            try:
                resp = orig_request(session, method, url, *a, **kw)
                return resp
            except BaseException as e:
                err = e
                raise
            finally:
                _http(method, url, time.monotonic() - t, _status(resp, err), kw.get("params"))
        requests.Session.request = request
    except ImportError:
        pass

    orig_open = urllib.request.OpenerDirector.open

    def open_(opener, fullurl, *a, **kw):
        t, resp, err = time.monotonic(), None, None
        try:
            resp = orig_open(opener, fullurl, *a, **kw)
            return resp
        except BaseException as e:
            err = e
            raise
        finally:
            method = fullurl.get_method() if hasattr(fullurl, "get_method") else "GET"
            _http(method, _url_of(fullurl), time.monotonic() - t, _status(resp, err))
    urllib.request.OpenerDirector.open = open_

    orig_run = subprocess.run

    def run(args, *a, **kw):
        t = time.monotonic()
        try:
            return orig_run(args, *a, **kw)
        finally:
            _add(f"proc:{_cmd(args)}", time.monotonic() - t)
    subprocess.run = run

    orig_check_output = subprocess.check_output

    def check_output(args, *a, **kw):
        t = time.monotonic()
        try:
            return orig_check_output(args, *a, **kw)
        finally:
            _add(f"proc:{_cmd(args)}", time.monotonic() - t)
    subprocess.check_output = check_output


def _patch_waits() -> None:
    from . import governor
    orig_pace = governor.GitHubGovernor.pace

    def pace(governor, *a, **kw):
        t = time.monotonic()
        try:
            return orig_pace(governor, *a, **kw)
        finally:
            _add("wait:github-governor", time.monotonic() - t)
    governor.GitHubGovernor.pace = pace


def install() -> bool:
    """Wrap everything once; True when PRTRIAGE_BOARD_TIMINGS is set."""
    global _INSTALLED, _STARTED
    path = os.environ.get("PRTRIAGE_BOARD_TIMINGS", "").strip()
    if not path or _INSTALLED:
        return bool(path)
    import importlib
    _INSTALLED = True
    _STARTED = time.monotonic()
    pulls = importlib.import_module("prtriage.pulls")
    for attr in PHASES:
        _rebind(pulls, attr, f"phase:{attr.lstrip('_')}", False)
    for mod_name, attr, keep_slow in PER_CALL:
        try:
            mod = importlib.import_module(f"prtriage.{mod_name}")
        except ImportError:
            continue
        _rebind(mod, attr, f"call:{mod_name}.{attr}", keep_slow)
    _patch_io()
    _patch_waits()
    atexit.register(write, path)
    return True


def report() -> Dict[str, Any]:
    with _LOCK:
        spans = {k: dict(v, seconds=round(v["seconds"], 2), max=round(v["max"], 2))
                 for k, v in _SPANS.items()}
        slow = {k: [[round(s, 2), lbl] for s, lbl in v] for k, v in _SLOW.items()}
    return {"wall_seconds": round(time.monotonic() - _STARTED, 1), "spans": spans,
            "slowest": slow}


def write(path: str) -> None:
    rep = report()
    try:
        with open(path, "w") as fh:
            json.dump(rep, fh, indent=1, sort_keys=True)
    except OSError as e:
        print(f"⏱  board timings: couldn't write {path}: {e}", file=sys.stderr)
        return
    rows = sorted(((k, v) for k, v in rep["spans"].items() if not k.startswith(("gh:", "ghc:"))),
                  key=lambda kv: -kv[1]["seconds"])
    print(f"⏱  board timings ({rep['wall_seconds']}s wall) → {path}", file=sys.stderr)
    for name, s in rows[:25]:
        print(f"   {s['seconds']:>9.1f}s  {s['calls']:>6} call(s)  max {s['max']:>7.1f}s  {name}",
              file=sys.stderr)
    gh = sorted(((k, v) for k, v in rep["spans"].items() if k.startswith("gh:")),
                key=lambda kv: -kv[1]["calls"])
    if gh:
        print("   GitHub requests by endpoint (most calls first):", file=sys.stderr)
        for name, s in gh[:20]:
            print(f"   {s['calls']:>6} call(s)  {s['seconds']:>8.1f}s  {name[3:]}", file=sys.stderr)
    ghc = sorted(((k, v) for k, v in rep["spans"].items() if k.startswith("ghc:")),
                 key=lambda kv: -kv[1]["calls"])
    if ghc:
        print("   /contents reads by ref kind and caller:", file=sys.stderr)
        for name, s in ghc[:15]:
            print(f"   {s['calls']:>6} call(s)  {s['seconds']:>8.1f}s  {name[4:]}", file=sys.stderr)
