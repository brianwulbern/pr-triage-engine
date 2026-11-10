"""The GitHub writes the sign-off hold needs besides labels: a comment on a pull, and an edit
of one in place."""
from __future__ import annotations

from typing import Any

from . import config, http_cache


def _comment(token: str, repo: str, number: str, body: str) -> bool:
    if not repo.startswith(config.POSTABLE_REPO_PREFIX):
        return False
    r = http_cache.gh_post(f"https://api.github.com/repos/{repo}/issues/{number}/comments",
                           headers={"Accept": "application/vnd.github+json",
                                    "Authorization": f"token {token}"},
                           json={"body": body}, timeout=30)
    return r.status_code in (200, 201)


def _edit_comment(token: str, repo: str, comment_id: Any, body: str) -> bool:
    if not repo.startswith(config.POSTABLE_REPO_PREFIX) or not comment_id:
        return False
    r = http_cache.gh_session().patch(
        f"https://api.github.com/repos/{repo}/issues/comments/{comment_id}",
        headers={"Accept": "application/vnd.github+json", "Authorization": f"token {token}"},
        json={"body": body}, timeout=30)
    return r.status_code == 200
