"""Idempotent, thread-aware PR verdict comments.

One self-updating comment per PR, identified by a hidden marker. A re-run edits it in place
while the conclusion is unchanged; a changed conclusion posts a new comment (a paper trail);
an unchanged conclusion on a thread that has moved on re-surfaces at the bottom, but only when
there is something new to say. Before posting it reads the thread: a fix offered in a human
comment that the PR has not picked up leads the post instead of a generic "please fix".

A failed read of the existing comments never turns into a blind create (that is how duplicates
happen), and a closed PR is never posted to.

Extracted from the production tool; the functions below are the real implementation.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import config
from . import http_cache
from . import governor

log = logging.getLogger(__name__)


# For parsing the components out of a PR URL when we want to hit the
# GitHub REST API. Anchored at start so we don't accidentally match a
# URL embedded inside another string. Capture groups are owner / repo / number.
_PR_PARTS_REGEX = re.compile(
    r"^https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)",
    re.IGNORECASE,
)


def _pr_current_state(owner: str, repo: str, number, token: Optional[str],
                      timeout: int = 30) -> Optional[str]:
    """2.199.0: the PR's state ('open'/'closed') read FRESH, bypassing the get_pull_json
    process memo - which holds the triage-time snapshot, so a board that triaged an open PR
    would still see 'open' at post time even after the human closed it. Uses an ETag
    conditional GET (http_cache), so the state is current whenever GitHub revalidates.
    None when unreadable (no token / transport error); callers fail-open on None."""
    if not token:
        return None
    try:
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
        data = http_cache.conditional_get_json(url, token, timeout=timeout)
        st = (data or {}).get("state")
        return st.lower() if isinstance(st, str) else None
    except Exception as exc:  # noqa: BLE001 - best-effort; caller fails open
        log.debug("[pr_state] fresh state check failed for %s/%s#%s: %s",
                  owner, repo, number, exc)
        return None


# Logins that are never the person to @-mention for action: the CI/forwarding
# automation (ci-bot*) and upstream-fork (the fork the
# backport is forwarded INTO - it shows up as a commit author by artifact, not because
# a human there is responsible for the PR).
# 2.273.0: product-release is the account the backport bot commits its cherry-picks as; #1234 greeted
# and assigned it as the dev.
_NON_AUTHOR_LOGINS = frozenset({"upstream-fork", "product-release", "web-flow"})


def _is_ci_bot_login(login: Optional[str]) -> bool:
    return bool(login) and str(login).startswith("ci-bot")


_VIEWER: Dict[str, str] = {}


def _viewer_login(token: Optional[str]) -> str:
    """The token owner's login, memoized; "" when it can't be read."""
    if not token:
        return ""
    if token not in _VIEWER:
        try:
            _VIEWER[token] = resolve_viewer_login(token)
        except Exception:  # noqa: BLE001
            return ""
    return _VIEWER[token]


# --------------------------------------------------------------------------
# 2.16.0 (#5): post the verdict to the PR - idempotent marker-edit.
# --------------------------------------------------------------------------
# The team relies on prtriage's verdict; the adoption lever is putting it WHERE they
# already look - on the PR - instead of a terminal only the runner sees. A hidden
# HTML-comment marker makes the post idempotent: the first run creates one comment,
# every re-run EDITS that same comment in place (no thread spam). Marker-only match
# is a sufficient idempotency key - prtriage is the only writer of this marker.
_VERDICT_MARKER = ("<!-- prtriage-verdict: managed by prtriage; edited in place while the "
                   "verdict is unchanged and the thread is quiet, a fresh comment when the "
                   "verdict changes or the thread has moved on (paper trail) -->")


# Prefix used to RECOGNIZE our prior comment - stable across footer/version wording
# changes, so a marker tweak never orphans an existing comment into a duplicate.
_VERDICT_MARKER_PREFIX = "<!-- prtriage-verdict"


# 2.156.0: the hidden verdict SIGNATURE the render embeds (pr_triage._gh_verdict_sig_marker
# / the single-build path). Matched off ANY prtriage comment body to compare conclusions.
_VERDICT_SIG_RE = re.compile(r"<!--\s*prtriage-sig:\s*(.+?)\s*-->")


def _verdict_sig(body: Optional[str]) -> Optional[str]:
    """2.156.0: the embedded `<orb>:<code>` (or `exit<N>`) verdict signature in a prtriage
    comment body, or None when absent (an older comment predating the signature, or a
    body that carries none - the caller then falls back to edit-in-place, never spam)."""
    if not body:
        return None
    m = _VERDICT_SIG_RE.search(body)
    return m.group(1).strip() if m else None


class _CommentLookupError(Exception):
    """The 'find my prior comment' read couldn't complete (rate-limit / non-200 /
    transient). Distinct from 'looked, found none' - the caller MUST NOT create on
    this, or a flaky read spawns a duplicate verdict comment (observed live on
    #1234: two duplicates posted during a rate-limited window because a 403 on the
    comment-list read was indistinguishable from 'no prior comment')."""


def _find_latest_verdict_comment(
    owner: str, repo: str, number: str, headers: dict,
    timeout: int, max_pages: int = 10, thread_out: Optional[list] = None,
) -> Optional[Tuple[int, str, bool]]:
    """(id, body, later_activity) of prtriage's MOST RECENT verdict comment on the PR (body
    starts with the marker prefix), or None if it CONFIDENTLY hasn't posted one yet (a
    clean read that found no match).

    2.156.0: NEWEST wins - prtriage now keeps a CHAIN (a fresh comment whenever the verdict
    changes, per the paper-trail behavior), so a re-run must update the LATEST comment
    and compare against ITS signature, not the oldest. GitHub lists comments ascending
    (oldest→newest), so we scan every page and keep the last match. Returning the body
    lets the caller read the embedded signature without a second fetch.

    2.189.0: `later_activity` is True when at least one NON-prtriage comment (a human reply, or
    a `ci:test:relevant` result the CI bot posts) is newer than prtriage's latest verdict
    comment - i.e. the thread has moved on and prtriage's comment is now buried above the fold.
    The caller uses it to REFRESH (post a fresh comment at the bottom) instead of editing
    in place, so an unchanged verdict re-surfaces amid new discussion. ISO-8601 created_at
    compares lexicographically = chronologically.

    Raises _CommentLookupError if the read itself failed (non-200 / network) - the
    result is UNKNOWN, not 'none'. Callers translate that into an aborted post rather
    than a create, so idempotency survives a flaky/rate-limited GitHub.

    2.239.0: with `thread_out`, every comment seen is appended to it as {id, login, body,
    url, created_at, prtriage} (no extra calls; it's the same pages), so the post step can read
    what the thread said since prtriage's last verdict (an offered fix, #1234)."""
    latest: Optional[Tuple[int, str, str]] = None   # (id, body, created_at)
    newest_nontrt_ts: Optional[str] = None
    page = 1
    while page <= max_pages:
        try:
            resp = governor.governed(lambda: http_cache.gh_get(
                f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
                headers=headers, params={"per_page": 100, "page": page}, timeout=timeout,
            ))
        except Exception as exc:  # noqa: BLE001 - any read failure is 'unknown', not 'none'
            raise _CommentLookupError(f"comment-list read failed: {exc}") from exc
        if resp.status_code != 200:
            # non-200 (esp. a 403 rate limit) → UNKNOWN, never 'confidently none'.
            rl = " (rate limit)" if resp.status_code in (403, 429) else ""
            raise _CommentLookupError(
                f"HTTP {resp.status_code} listing comments{rl}")
        batch = resp.json()
        if not batch:
            break
        for c in batch:
            ts = c.get("created_at") or ""
            is_trt = (c.get("body") or "").lstrip().startswith(_VERDICT_MARKER_PREFIX)
            if thread_out is not None:
                thread_out.append({"id": c.get("id"),
                                   "login": (c.get("user") or {}).get("login") or "",
                                   "body": c.get("body") or "",
                                   "url": c.get("html_url") or "",
                                   "created_at": ts, "prtriage": is_trt})
            if is_trt:
                cid = c.get("id")
                if cid is not None:
                    latest = (int(cid), c.get("body") or "", ts)   # later rows are newer
            elif newest_nontrt_ts is None or ts > newest_nontrt_ts:
                newest_nontrt_ts = ts
        if len(batch) < 100:
            break
        page += 1
    if latest is None:
        return None
    later_activity = bool(newest_nontrt_ts and latest[2]
                          and newest_nontrt_ts > latest[2])
    return (latest[0], latest[1], later_activity)


# 2.239.0 (#1234) - read the thread before posting. A dev replied "you guys need to cherry
# pick this commit <url>" with the correct fix; prtriage's next post ignored it, re-told the authors
# to fix the PR themselves, and (since the only change was the behind-count) was a near-copy
# of its prior comment. So at post time prtriage now (a) looks for a fix offered in a human comment
# that the PR hasn't picked up and LEADS with it, and (b) won't post a fresh "refresh" copy when
# nothing but volatile numbers changed.
_OFFERED_COMMIT_URL_RE = re.compile(
    r"https://github\.com/[\w.-]+/[\w.-]+/(?:pull/\d+/)?commits?/([0-9a-fA-F]{7,40})\b")


_CHERRY_RE = re.compile(r"cherry[- ]?pick", re.IGNORECASE)


_CHERRY_PICK_SHA_RE = re.compile(
    r"cherry[- ]?pick\w*[^\n]{0,160}?\b([0-9a-fA-F]{7,40})\b", re.IGNORECASE)


_FIX_WORDS_RE = re.compile(r"\b(?:fix(?:es|ed)?|patch(?:ed)?|apply|applied)\b",
                           re.IGNORECASE)


_GREETING_RE = re.compile(r"^Hi ((?:@[\w-]+\s*)+)")


# any "<N> commit(s) behind" - the count drifts every hour, so only its ZONE is substance.
_BEHIND_NUM_RE = re.compile(r"\b(\d+)( commit\(s\) behind)")


def _refresh_enabled() -> bool:
    """2.254.0: an UNCHANGED verdict is edited in place by default, never posted again as a
    fresh comment. The 2.189 refresh (re-post at the bottom once the thread moved on) put three
    same-conclusion verdicts on #1234 while the dev was already discussing the fix; a new
    comment now means a new conclusion. PRTRIAGE_GH_REFRESH=1 turns the refresh back on."""
    return (os.environ.get("PRTRIAGE_GH_REFRESH") == "1"
            and os.environ.get("PRTRIAGE_NO_GH_REFRESH") != "1")


def _is_human_login(login: str) -> bool:
    return bool(login) and not _is_ci_bot_login(login) and not login.endswith("[bot]")


def _comment_fix_offers(comment: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fixes a single human comment offers: a commit link alongside fix wording, or a sha
    alongside "cherry-pick". Each {login, sha, commit_url, comment_url}. A bare commit link
    with no fix wording (e.g. "this commit broke it") is NOT an offer. Shas must contain a
    digit, which drops all-letter hex words ("defaced") at ~no cost to real shas."""
    body = comment.get("body") or ""
    cherry = bool(_CHERRY_RE.search(body))
    if not cherry and not _FIX_WORDS_RE.search(body):
        return []
    cands: List[Tuple[str, Optional[str]]] = [
        (m.group(1).lower(), m.group(0)) for m in _OFFERED_COMMIT_URL_RE.finditer(body)]
    if cherry:
        cands += [(m.group(1).lower(), None) for m in _CHERRY_PICK_SHA_RE.finditer(body)]
    out, seen = [], set()
    for sha, url in cands:
        if not any(ch.isdigit() for ch in sha) or sha[:7] in seen:
            continue
        seen.add(sha[:7])
        out.append({"login": comment.get("login") or "", "sha": sha, "commit_url": url,
                    "comment_url": comment.get("url") or ""})
    return out


def _pr_head(owner: str, repo: str, number: str, headers: dict,
             timeout: int) -> Tuple[Optional[str], Optional[str]]:
    """(head sha, head commit's committer date) - best-effort, (None, None) on any failure."""
    try:
        r = governor.governed(lambda: http_cache.gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}",
            headers=headers, timeout=timeout))
        if r.status_code != 200:
            return None, None
        sha = ((r.json() or {}).get("head") or {}).get("sha")
        if not sha:
            return None, None
        r2 = governor.governed(lambda: http_cache.gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
            headers=headers, timeout=timeout))
        if r2.status_code != 200:
            return sha, None
        c = (r2.json() or {}).get("commit") or {}
        return sha, ((c.get("committer") or {}).get("date")) or None
    except Exception:  # noqa: BLE001 - best-effort; the caller falls back
        return None, None


def _thread_offered_fixes(owner: str, repo: str, number: str, headers: dict,
                          timeout: int, thread: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fixes offered in human comments that the PR hasn't picked up: newer than the PR head
    commit (no commit has landed since the offer), and not the head itself. If the head can't
    be read, fall back to offers newer than prtriage's latest verdict comment. The head is only
    fetched when some human comment actually looks like an offer (the common PR pays nothing)."""
    humans = [c for c in thread if not c.get("prtriage") and _is_human_login(c.get("login") or "")]
    offers = [(c, f) for c in humans for f in _comment_fix_offers(c)]
    if not offers:
        return []
    head_sha, head_ts = _pr_head(owner, repo, number, headers, timeout)
    if head_ts:
        since = head_ts
    else:
        trt_ts = [c.get("created_at") or "" for c in thread if c.get("prtriage")]
        since = max(trt_ts) if trt_ts else ""
    out = []
    for c, f in offers:
        if since and (c.get("created_at") or "") <= since:
            continue
        if head_sha and head_sha.lower().startswith(f["sha"][:7]):
            continue                                   # the offered commit IS the head
        out.append(f)
    return out


def _compose_post_body(verdict_md: str, fixes: List[Dict[str, Any]]) -> str:
    """The verdict body to post, leading with any offered fix. Replaces prtriage's greeting (which
    otherwise asks the authors to change the PR as if nothing had been offered); with no
    greeting, the lead goes on top. Skipped on a green verdict. PUBLIC-FACING: no em-dashes."""
    if not fixes:
        return verdict_md
    sig = _verdict_sig(verdict_md)
    if sig and sig.startswith("🟢"):
        return verdict_md
    first, sep, rest = verdict_md.partition("\n")
    m = _GREETING_RE.match(first)
    offerers = {f["login"].lower() for f in fixes[:3]}
    greeted = [h for h in (m.group(1).split() if m else []) if h[1:].lower() not in offerers]
    thank = bool(m) and not greeted
    parts = []
    for f in fixes[:3]:
        sha7 = f["sha"][:7]
        ref = f"[`{sha7}`]({f['commit_url']})" if f.get("commit_url") else f"`{sha7}`"
        where = f" ([comment]({f['comment_url']}))" if f.get("comment_url") else ""
        parts.append(f"thanks @{f['login']} for the fix: cherry-pick {ref}{where}" if thank
                     else f"{f['login']} offered a fix: cherry-pick {ref}{where}")
    lead = ("; ".join(parts) + ". prtriage's read below doesn't account for it yet; once it's "
            "applied, re-run `ci:test:relevant` and prtriage will re-judge on the new build.")
    if thank:
        return f"{lead[0].upper()}{lead[1:]}{sep}{rest}"
    if m:
        return f"Hi {' '.join(greeted)}, {lead}{sep}{rest}"
    return f"💬 {lead}\n\n{verdict_md}"


def _post_substance(body: Optional[str]) -> str:
    """A body reduced to what's worth a new comment: no marker line, and every
    "<N> commit(s) behind" collapsed to its zone (within vs past the rebase-needed line,
    config.STALE_REBASE_BEHIND_THRESHOLD). A behind-count that drifts but stays in its zone
    is not a change; crossing into rebase-needed territory is."""
    from . import config
    thresh = config.STALE_REBASE_BEHIND_THRESHOLD
    # 2.276.1: compare as posted: a comment from before the em-dash cleanup is the same
    # substance as today's, so it must not read as "moved on" and repost
    lines = [ln.rstrip() for ln in public_text(body or "").splitlines()
             if not ln.lstrip().startswith(_VERDICT_MARKER_PREFIX)]
    txt = "\n".join(lines).strip()
    return _BEHIND_NUM_RE.sub(
        lambda m: ("rebase-needed" if int(m.group(1)) > thresh else "within-range")
        + m.group(2), txt)


_FENCE_RE = re.compile(r"(```.*?```)", re.S)


def public_text(md: str) -> str:
    """2.276.1: prtriage's terminal wording uses em-dashes; a GitHub comment is public text,
    which never carries one (the team's rule). Replace each outside fenced code blocks,
    so a quoted diff or log is left exactly as it is. Hidden markers carry none."""
    parts = _FENCE_RE.split(md or "")
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"[ \t]*\u2014[ \t]*", ": ", parts[i]).replace("\u2014", ", ")
    return "".join(parts)


def preview_pr_verdict_action(
    pr_url: str, verdict_md: str, token: Optional[str], timeout: int = 30,
) -> dict:
    """2.171.0: the READ-ONLY twin of upsert_pr_verdict_comment - determine which action
    a --gh-post WOULD take, WITHOUT posting, so the dry-run can tell the user per PR
    whether the run would:
      * "create" - a brand-new comment (prtriage has never posted here),
      * "update" - EDIT the existing comment in place (same conclusion, no thread spam),
      * "repost" - a NEW comment because the conclusion CHANGED since the last one.
    Returns {"action": <one of those>, "url": <existing comment url or "">}, or
    {"action": "unknown", "reason": <human>} when it can't tell (no token, bad URL, or
    the comment-list read failed - in which the real post ABORTS rather than create).
    Mirrors upsert_pr_verdict_comment's signature-compare exactly so the preview matches
    the apply."""
    if not token:
        return {"action": "unknown", "reason": "no GitHub token"}
    m = _PR_PARTS_REGEX.match((pr_url or "").strip())
    if not m:
        return {"action": "unknown", "reason": "not a GitHub PR URL"}
    owner, repo, number = m.group(1), m.group(2), m.group(3)
    headers = {"Accept": "application/vnd.github+json",
               "Authorization": f"token {token}"}
    thread: list = []
    try:
        prev = _find_latest_verdict_comment(owner, repo, number, headers, timeout,
                                                thread_out=thread)
    except _CommentLookupError as exc:
        return {"action": "unknown",
                "reason": f"couldn't read existing comments ({exc}); a real post would skip"}
    # 2.239.0: compose exactly what the apply would post (leading with any offered fix).
    fixes = _thread_offered_fixes(owner, repo, number, headers, timeout, thread)
    composed = _compose_post_body(verdict_md, fixes)
    if prev is None:
        return {"action": "create", "url": "", "body": composed, "fixes": fixes}
    prev_id, prev_body, later_activity = prev
    prev_sig, new_sig = _verdict_sig(prev_body), _verdict_sig(verdict_md)
    verdict_changed = (new_sig is not None and prev_sig is not None
                       and new_sig != prev_sig)
    # 2.189.0: mirror upsert - an unchanged verdict on a thread that moved on since prtriage's
    # last comment REFRESHES (fresh bottom-of-thread comment), not an invisible in-place edit.
    refresh = (not verdict_changed and later_activity and _refresh_enabled())
    # 2.239.0: ...unless there's nothing new to say - then edit in place, don't post a copy.
    quiet = refresh and _post_substance(prev_body) == _post_substance(composed)
    if quiet:
        refresh = False
    action = "repost" if verdict_changed else ("refresh" if refresh else "update")
    return {"action": action,
            "url": f"https://github.com/{owner}/{repo}/pull/{number}"
                   f"#issuecomment-{prev_id}",
            "prev_sig": prev_sig, "new_sig": new_sig,
            "body": composed, "fixes": fixes, "quiet": quiet,
            "replies": thread_replies(thread, prev_id, _viewer_login(token))}


def thread_replies(thread: list, since_id: int, viewer: str = "") -> List[Dict[str, object]]:
    """3.0.65: the human comments posted after prtriage's verdict comment `since_id`, oldest
    first, each {login, at, url, mentions_me}. CI commands and bots don't count. 3.0.66:
    nor does anything you already answered with a comment of your own."""
    since = next((c.get("created_at") or "" for c in thread if c.get("id") == since_id), "")
    if not since:
        return []
    if viewer:
        since = max([since] + [c.get("created_at") or "" for c in thread if not c.get("prtriage")
                               and (c.get("login") or "").lower() == viewer.lower()
                               and not (c.get("body") or "").strip().lower().startswith("ci:")])
    me = re.compile(rf"@{re.escape(viewer)}\b", re.IGNORECASE) if viewer else None
    out = []
    for c in thread:
        body = (c.get("body") or "").strip()
        if (c.get("prtriage") or not _is_human_login(c.get("login") or "") or not body
                or body.lower().startswith("ci:") or (c.get("created_at") or "") <= since
                or (viewer and (c.get("login") or "").lower() == viewer.lower())):
            continue
        out.append({"login": c.get("login"), "at": c.get("created_at") or "",
                    "url": c.get("url") or "", "mentions_me": bool(me and me.search(body))})
    return out


def upsert_pr_verdict_comment(
    pr_url: str, verdict_md: str, token: Optional[str], timeout: int = 30,
) -> dict:
    """Post prtriage's verdict to the PR as a single self-updating comment: create it the
    first time, EDIT it in place on every re-run (idempotent via the hidden marker).

    Returns a status dict - {"ok": True, "action": "created"|"updated"|"reposted"|
    "refreshed", "url": <html_url>} on success, or {"ok": False, "error": <msg>} otherwise.
    Never raises: this is a user-invoked side effect (`--gh-post`), so it reports failure
    but must not crash the triage that already ran. The body is the marker + the rendered
    verdict markdown.

    2.156.0: the post is idempotent PER VERDICT, not forever-one-comment. A re-run whose
    verdict SIGNATURE (the embedded `<!-- prtriage-sig -->`) matches the latest prtriage comment
    edits it in place (no thread spam). A CHANGED conclusion - the dev fixed it, or a prtriage
    upgrade re-judged the same run - posts a NEW comment ("reposted"), leaving the prior
    one as a paper trail. Missing signature on either side → fall back to edit-in-place
    (conservative: never spawn a duplicate when we can't confirm the verdict moved)."""
    verdict_md = public_text(verdict_md)            # 2.276.1: public text, no em-dashes
    if not token:
        return {"ok": False, "error": "no GitHub token (set PRTRIAGE_GITHUB_TOKEN)"}
    m = _PR_PARTS_REGEX.match((pr_url or "").strip())
    if not m:
        return {"ok": False, "error": f"not a GitHub PR URL: {pr_url!r}"}
    owner, repo, number = m.group(1), m.group(2), m.group(3)
    # 2.199.0 (#1234): a board sweep triages while the PR is open, but the human may CLOSE it
    # (superseded / ci:close) before the post lands minutes later - and asking a dev to look at
    # failures on a PR they just closed is noise (a dev closed it as superseded, then prtriage
    # pinged them on it). Re-check the state FRESH at post time and skip a closed PR. Fail-open:
    # only a CONFIRMED 'closed' skips; an unreadable state still posts (a transient error must
    # not block a legit post, and a stray comment is recoverable while a blocked one is not).
    if _pr_current_state(owner, repo, number, token, timeout) == "closed":
        return {"ok": False, "skipped": True,
                "error": "PR is closed - not posting a verdict on a closed PR"}
    headers = {"Accept": "application/vnd.github+json",
               "Authorization": f"token {token}"}
    thread: list = []
    try:
        prev = _find_latest_verdict_comment(owner, repo, number, headers, timeout,
                                                thread_out=thread)
    except _CommentLookupError as exc:
        # Couldn't determine whether a prior comment exists → refuse to post. Creating
        # blind here is how duplicates happen (a rate-limited read ≠ 'no comment yet').
        # A skipped post is recoverable - just re-run once GitHub recovers.
        return {"ok": False, "skipped": True,
                "error": f"couldn't check for an existing comment ({exc}) - "
                         f"skipped to avoid a duplicate; re-run to post"}
    prev_id = prev[0] if prev else None
    later_activity = prev[2] if prev else False
    prev_sig = _verdict_sig(prev[1]) if prev else None
    new_sig = _verdict_sig(verdict_md)
    # The verdict CHANGED when we have both signatures and they differ - then a new
    # comment (paper trail). Unchanged, or a signature we can't read on either side →
    # edit the latest in place.
    verdict_changed = (prev_id is not None and new_sig is not None
                       and prev_sig is not None and new_sig != prev_sig)
    # 2.189.0 (opt-in since 2.254.0, PRTRIAGE_GH_REFRESH=1): an UNCHANGED verdict re-surfaces as a FRESH comment when
    # the thread has moved on since prtriage's last one (a new reply or ci:test:relevant result),
    # because an in-place edit near the top of a long thread is invisible. Opt out with
    # PRTRIAGE_NO_GH_REFRESH=1. Self-limiting: the fresh comment becomes the newest prtriage comment,
    # so it won't repost again until there's NEW activity after it.
    refresh = (prev_id is not None and not verdict_changed and later_activity
               and _refresh_enabled())
    # 2.239.0 (#1234): lead with any fix offered in the thread, and don't post a fresh
    # copy when nothing but volatile numbers changed (edit the latest in place instead).
    fixes = _thread_offered_fixes(owner, repo, number, headers, timeout, thread)
    composed = _compose_post_body(verdict_md, fixes)
    body = f"{_VERDICT_MARKER}\n{composed}"
    if refresh and _post_substance(prev[1]) == _post_substance(composed):
        refresh = False
    make_new = verdict_changed or refresh
    try:
        if prev_id is not None and not make_new:
            resp = governor.governed(lambda: requests.patch(
                f"https://api.github.com/repos/{owner}/{repo}"
                f"/issues/comments/{prev_id}",
                headers=headers, json={"body": body}, timeout=timeout,
            ))
            action = "updated"
        else:
            resp = governor.governed(lambda: http_cache.gh_post(
                f"https://api.github.com/repos/{owner}/{repo}"
                f"/issues/{number}/comments",
                headers=headers, json={"body": body}, timeout=timeout,
            ))
            action = ("reposted" if verdict_changed
                      else "refreshed" if refresh else "created")
    except Exception as exc:  # noqa: BLE001 - report, never crash the triage
        return {"ok": False, "error": f"request failed: {exc}"}
    if resp.status_code in (200, 201):
        url = ""
        try:
            url = (resp.json() or {}).get("html_url") or ""
        except ValueError:
            pass
        return {"ok": True, "action": action, "url": url}
    hint = ""
    if resp.status_code in (401, 403):
        hint = " - token lacks write scope (needs 'repo' / pull-request write)"
    elif resp.status_code == 404:
        hint = " - PR not found or token can't see it"
    return {"ok": False,
            "error": f"HTTP {resp.status_code} on {action[:-1]}e{hint}"}


# Bots we never assign / never treat as human authors.
_ASSIGN_BOT_LOGINS = frozenset({
    "ci-bot", "ci-bot-hu", "product-release"})


def add_pr_labels(owner: str, repo: str, number, labels, token: Optional[str],
                  timeout: int = 30) -> dict:
    """ADD labels to a PR, ADDITIVELY. POST /issues/{n}/labels appends to the existing set
    (never replaces - so other labels stay). {'ok': bool, 'error': str}. Never raises."""
    labels = [x for x in (labels or []) if x]
    if not token or not labels:
        return {"ok": False, "error": "no token or no labels"}
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"token {token}"}
    try:
        resp = governor.governed(lambda: http_cache.gh_post(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/labels",
            headers=headers, json={"labels": labels}, timeout=timeout))
    except Exception as exc:  # noqa: BLE001 - best-effort, never crash the post
        return {"ok": False, "error": f"request failed: {exc}"}
    if resp.status_code in (200, 201):
        return {"ok": True}
    return {"ok": False, "error": f"HTTP {resp.status_code}"}


def remove_pr_label(owner: str, repo: str, number, label: str, token: Optional[str],
                    timeout: int = 30) -> dict:
    """2.221.0: remove ONE label from a PR. DELETE /issues/{n}/labels/{label}. A 404 means the
    label wasn't there - treated as ok (idempotent). {'ok': bool, 'error': str}. Never raises."""
    if not token or not label:
        return {"ok": False, "error": "no token or no label"}
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"token {token}"}
    import urllib.parse as _up
    url = (f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/labels/"
           f"{_up.quote(label)}")
    try:
        resp = governor.governed(lambda: http_cache.gh_delete(
            url, headers=headers, timeout=timeout))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"request failed: {exc}"}
    if resp.status_code in (200, 404):
        return {"ok": True}
    return {"ok": False, "error": f"HTTP {resp.status_code}"}


def resolve_viewer_login(token: str, timeout: int = 30) -> str:
    """The authenticated user's GitHub login (the token owner), via GET /user - so
    `prtriage pulls --me` can filter to PRs assigned to whoever's token this is, without the
    user having to type their own login. Raises on a non-200 (a silent empty would make
    --me quietly match nothing)."""
    resp = governor.governed(lambda: http_cache.gh_get(
        "https://api.github.com/user",
        headers={"Accept": "application/vnd.github+json",
                 "Authorization": f"token {token}"},
        timeout=timeout,
    ))
    if resp.status_code != 200:
        raise RuntimeError(
            f"GitHub /user HTTP {resp.status_code}: {resp.text[:200]}")
    return ((resp.json() or {}).get("login")) or ""
