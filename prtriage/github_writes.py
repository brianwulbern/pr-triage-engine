"""The one GitHub write the sign-off hold needs besides labels: a comment on a pull."""
from __future__ import annotations

from . import config, http_cache


def _comment(token: str, repo: str, number: str, body: str) -> bool:
    if not repo.startswith(config.POSTABLE_REPO_PREFIX):
        return False
    r = http_cache.gh_post(f"https://api.github.com/repos/{repo}/issues/{number}/comments",
                           headers={"Accept": "application/vnd.github+json",
                                    "Authorization": f"token {token}"},
                           json={"body": body}, timeout=30)
    return r.status_code in (200, 201)
