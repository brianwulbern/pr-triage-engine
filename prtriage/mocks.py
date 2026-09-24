"""Stand-ins for the two external services, so the demo runs with no network, no tokens and no
private data.

The production tool talks to an internal test-reporting service (builds, per-test history) and
to GitHub (PR state, head commit, comment threads, code search). Both are private to their
deployment, so here:

* `synthetic_history` fabricates a test's run history in the report service's shape, which the
  real classifier consumes unchanged.
* `MockGitHub` is an in-memory GitHub that answers exactly the REST calls the real comment
  engine and board make, including GitHub's separate, tiny code-search rate bucket (the thing
  that used to throttle every call; see governor.ISOLATED_RESOURCES).

`MockGitHub.install()` patches `requests.get/post/patch`, the same seam the test suite uses;
the HTTP helpers defer to a patched `requests` function by design.
"""
from __future__ import annotations

import contextlib
import itertools
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional
from unittest import mock

import requests

_ids = itertools.count(9000)


def synthetic_history(pass_rate: int, runs: int = 40, error: str = "",
                      pr_string: str = "") -> Dict[str, Any]:
    """A test's recent run history as the report service returns it (newest first):
    `runs` runs from other PRs, `pass_rate`% of them passing, failures carrying `error`."""
    items = []
    for i in range(runs):
        # spread the passes evenly through the window
        passed = int((i + 1) * pass_rate / 100) > int(i * pass_rate / 100)
        items.append({
            "reportCaseResultId": next(_ids),
            "reportBuildName": f"[release-1.x] ci:test - other-dev - PR#{7000 + i}",
            "reportStatusItem": {"name": "PASSED" if passed else "FAILED"},
            "error": "" if passed else error,
        })
    return {"items": items}


class _Resp:
    def __init__(self, status: int, payload: Any = None, headers: Optional[dict] = None,
                 url: str = ""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.url = url
        self.text = payload if isinstance(payload, str) else json.dumps(payload or {})

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class MockGitHub:
    """An in-memory GitHub for one repo. Threads, PR state and head commits are seeded by the
    caller; comment POST/PATCH mutate the threads like the real API."""

    API = "https://api.github.com"

    def __init__(self, owner: str = "example-org", repo: str = "product",
                 code_search_per_minute: int = 10):
        self.owner, self.repo = owner, repo
        self.prs: Dict[int, Dict[str, Any]] = {}
        self.threads: Dict[int, List[Dict[str, Any]]] = {}
        self.search_limit = code_search_per_minute
        self.search_used = 0
        self.calls: List[str] = []
        self._lock = threading.Lock()
        self._tick = itertools.count(1)

    # -- seeding -------------------------------------------------------------------------
    def add_pr(self, number: int, head_sha: str, head_date: str, state: str = "open") -> None:
        self.prs[number] = {"state": state, "head_sha": head_sha, "head_date": head_date}
        self.threads.setdefault(number, [])

    def add_comment(self, number: int, login: str, body: str, created_at: str) -> int:
        cid = next(_ids)
        self.threads.setdefault(number, []).append({
            "id": cid, "user": {"login": login}, "body": body, "created_at": created_at,
            "html_url": f"https://github.com/{self.owner}/{self.repo}/pull/{number}"
                        f"#issuecomment-{cid}"})
        return cid

    def _now(self) -> str:
        # strictly increasing timestamps after any seeded ones
        return f"2099-01-01T00:{next(self._tick):02d}:00Z"

    # -- routing -------------------------------------------------------------------------
    def _route(self, method: str, url: str, **kw) -> _Resp:
        path = url.replace(self.API, "")
        with self._lock:
            self.calls.append(f"{method} {path}")
        base = f"/repos/{self.owner}/{self.repo}"
        if path == "/user":
            return _Resp(200, {"login": "demo-bot"}, {"x-ratelimit-remaining": "4990"}, url)
        if path == "/rate_limit":
            return _Resp(200, {"resources": {"core": {"remaining": 4990, "limit": 5000,
                                                      "reset": int(time.time()) + 3600}}},
                         url=url)
        if path == "/search/code":
            return self._code_search(url)
        m = re.fullmatch(base + r"/issues/(\d+)/comments", path)
        if m and method == "GET":
            page = (kw.get("params") or {}).get("page", 1)
            items = self.threads.get(int(m.group(1)), [])
            return _Resp(200, items if page == 1 else [], url=url)
        if m and method == "POST":
            n = int(m.group(1))
            cid = self.add_comment(n, "demo-bot", (kw.get("json") or {})["body"], self._now())
            return _Resp(201, self.threads[n][-1], url=url)
        m = re.fullmatch(base + r"/issues/comments/(\d+)", path)
        if m and method == "PATCH":
            for thread in self.threads.values():
                for c in thread:
                    if c["id"] == int(m.group(1)):
                        c["body"] = (kw.get("json") or {})["body"]
                        return _Resp(200, c, url=url)
            return _Resp(404, {"message": "Not Found"}, url=url)
        m = re.fullmatch(base + r"/pulls/(\d+)", path)
        if m:
            pr = self.prs.get(int(m.group(1)))
            if not pr:
                return _Resp(404, {"message": "Not Found"}, url=url)
            return _Resp(200, {"state": pr["state"], "head": {"sha": pr["head_sha"]}}, url=url)
        m = re.fullmatch(base + r"/commits/([0-9a-f]+)", path)
        if m:
            for pr in self.prs.values():
                if pr["head_sha"] == m.group(1):
                    return _Resp(200, {"commit": {"committer": {"date": pr["head_date"]}}},
                                 url=url)
            return _Resp(404, {"message": "Not Found"}, url=url)
        return _Resp(404, {"message": f"mock has no route for {method} {path}"}, url=url)

    def _code_search(self, url: str) -> _Resp:
        """GitHub meters code search in its own bucket, separate from core. When it runs dry
        it returns 403 with X-RateLimit-Resource: code_search and remaining 0."""
        with self._lock:
            self.search_used += 1
            remaining = self.search_limit - self.search_used
        headers = {"X-RateLimit-Resource": "code_search",
                   "X-RateLimit-Limit": str(self.search_limit),
                   "X-RateLimit-Remaining": str(max(0, remaining)),
                   "X-RateLimit-Reset": str(int(time.time()) + 60)}
        if remaining < 0:
            return _Resp(403, "API rate limit exceeded for code search", headers, url)
        return _Resp(200, {"items": [{"path": "modules/app/src/test/java/demo/FooTest.java"}]},
                     headers, url)

    @contextlib.contextmanager
    def install(self):
        """Route requests.get/post/patch to this mock for the duration of the block."""
        with mock.patch.object(requests, "get", lambda url, **kw: self._route("GET", url, **kw)), \
             mock.patch.object(requests, "post", lambda url, **kw: self._route("POST", url, **kw)), \
             mock.patch.object(requests, "patch", lambda url, **kw: self._route("PATCH", url, **kw)):
            yield self
