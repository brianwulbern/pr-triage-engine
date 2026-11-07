"""The local product clone the double agent reads: where it is, and git calls against it.

A pull's head and master are fetched into the clone when missing, one fetch at a time per
clone, so parallel reviews don't race on the same FETCH_HEAD.

Extracted from the production tool; the functions below are the real implementation.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional


# GitHub's commit search does not index product (0 hits for a ticket with a master
# commit), so a ticket's master commits come from a LOCAL clone: PRTRIAGE_PORTAL_ROOT, else the
# conventional ~/dev/projects/product. No clone -> every ticket reads UNKNOWN.
_FETCHED: Dict[str, bool] = {}


def portal_root() -> Optional[str]:
    """The local product clone, or None (PRTRIAGE_NO_LOCAL_PORTAL=1 forces None)."""
    import os
    if os.environ.get("PRTRIAGE_NO_LOCAL_PORTAL", "").strip():
        return None
    for cand in (os.environ.get("PRTRIAGE_PORTAL_ROOT"),
                 os.path.expanduser("~/dev/projects/product")):
        if cand and os.path.isdir(os.path.join(cand, ".git")):
            return cand
    return None


def _git(root: str, args: List[str], timeout: int = 60) -> Optional[str]:
    import subprocess
    if args and args[0] == "fetch":
        return _git_fetch(root, args, timeout)
    try:
        p = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                           timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    return p.stdout if p.returncode == 0 else None


def _fetch_lock_path(root: str) -> str:
    import hashlib
    import os
    from . import config as _config
    d = os.path.join(_config.cache_root(), "prtriage", "locks")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, hashlib.sha1(os.path.abspath(root).encode()).hexdigest()[:16]
                        + ".fetch.lock")


def _git_fetch(root: str, args: List[str], timeout: int) -> Optional[str]:
    """3.0.23 (#1234): one `git fetch` at a time per clone, across the board's threads and
    processes, and one retry. Parallel scans fetched into the same clone at once (the same
    refs/prtriage/ ref, the same FETCH_HEAD), one failed, and a 3,908-file pull read as having no
    diff at all. Waits for the lock up to `timeout`; None when it can't get it or both tries
    fail."""
    import fcntl
    import subprocess
    import time
    deadline = time.monotonic() + timeout
    try:
        fh = open(_fetch_lock_path(root), "w")
    except OSError:
        fh = None
    try:
        while fh is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.5)
        for attempt in (1, 2):
            left = max(10, int(deadline - time.monotonic()))
            try:
                p = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                                   timeout=left)
            except Exception:  # noqa: BLE001
                return None
            if p.returncode == 0:
                return p.stdout
            if attempt == 1:
                time.sleep(2)
        return None
    finally:
        if fh is not None:
            fh.close()


def _master_ref(root: str, owner: str, repo: str) -> Optional[str]:
    """`<remote>/master` for the remote pointing at owner/repo, fetched once per process
    (60s budget; a failed fetch keeps whatever master the clone already has)."""
    remotes = _git(root, ["remote", "-v"]) or ""
    name = next((ln.split()[0] for ln in remotes.splitlines()
                 if re.search(rf"github\.com[:/]{re.escape(owner)}/{re.escape(repo)}(\.git)?\s",
                              ln + " ")), None)
    if not name:
        return None
    if not _FETCHED.get(root):
        _FETCHED[root] = True
        _git(root, ["fetch", "-q", name, "master"], timeout=60)
    ref = f"{name}/master"
    return ref if _git(root, ["rev-parse", "-q", "--verify", ref]) else None
