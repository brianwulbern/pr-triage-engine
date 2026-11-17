"""Pulls waiting on a person, held out of the forward set.

Two signals, either holds:

- a person other than you labeled the pull `waiting_for_dev` after its head commit (a teammate
  pulled it back; prtriage's own red-post hand-back is excluded, it comes with a prtriage comment);
- your own latest question on the pull (a plain comment of yours with a `?` and an @-mention)
  has no reply from anyone it mentions and no push since.

A pull once sat waiting on an API owner's answer, labeled `waiting_for_dev` by a teammate, yet
the board called it SAFE TO FORWARD and the forward script sent it again."""
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

LABEL = "waiting_for_dev"
_API = "https://api.github.com/repos"
_MENTION_RE = re.compile(r"(?<![\w`])@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)")
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
_PRTRIAGE_POST_WINDOW_S = 180
_STATES: Dict[str, str] = {}


def _ts(iso: Optional[str]) -> float:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _login(o: Dict[str, Any]) -> str:
    return ((o.get("user") or o.get("actor") or {}).get("login") or "")


def _is_bot(o: Dict[str, Any]) -> bool:
    from . import ai_review
    who = o.get("user") or o.get("actor") or {}
    return ai_review._is_bot(who.get("login") or "", who.get("type") or "")


def _prose(body: str) -> str:
    lines = [l for l in _CODE_RE.sub("", body or "").splitlines() if not l.lstrip().startswith(">")]
    return "\n".join(lines)


def head_time(commit: Optional[Dict[str, Any]], events: List[Dict[str, Any]]) -> float:
    """When the head arrived: its commit date, or a later force-push."""
    at = _ts((((commit or {}).get("commit") or {}).get("committer") or {}).get("date"))
    pushes = [_ts(e.get("created_at")) for e in events if e.get("event") == "head_ref_force_pushed"]
    return max([at] + pushes)


def label_hold(events: List[Dict[str, Any]], comments: List[Dict[str, Any]], labels: List[str],
               viewer: str, head_at: float) -> str:
    if LABEL not in labels:
        return ""
    added = [e for e in events if e.get("event") == "labeled"
             and (e.get("label") or {}).get("name") == LABEL]
    if not added:
        return ""
    last = max(added, key=lambda e: e.get("created_at") or "")
    who, at = _login(last), _ts(last.get("created_at"))
    if not who or who.lower() == viewer.lower() or _is_bot(last) or at <= head_at:
        return ""
    if any(_login(c).lower() == who.lower() and "<!-- prtriage-" in (c.get("body") or "")
           and abs(_ts(c.get("created_at")) - at) <= _PRTRIAGE_POST_WINDOW_S for c in comments):
        return ""
    return f"@{who} labeled it {LABEL} on {(last.get('created_at') or '')[:10]}, after the last push"


def question_hold(comments: List[Dict[str, Any]], reviews: List[Dict[str, Any]], viewer: str,
                  head_at: float) -> str:
    v = viewer.lower()
    asks = []
    for c in comments:
        body = c.get("body") or ""
        if _login(c).lower() != v or "<!-- prtriage-" in body:
            continue
        prose = _prose(body)
        who = [m for m in dict.fromkeys(_MENTION_RE.findall(prose)) if m.lower() != v]
        if "?" in prose and who:
            asks.append((c, who))
    if not asks:
        return ""
    c, who = max(asks, key=lambda a: a[0].get("created_at") or "")
    at = _ts(c.get("created_at"))
    if head_at > at:
        return ""
    asked = {w.lower() for w in who}
    for r in list(comments) + list(reviews):
        when = _ts(r.get("created_at") or r.get("submitted_at"))
        if when > at and _login(r).lower() in asked:
            return ""
    names = " ".join(f"@{w}" for w in who)
    return f"your question to {names} on {(c.get('created_at') or '')[:10]} has no reply yet"


def pull_wait(owner: str, repo: str, num: str, token: Optional[str]) -> str:
    """Why the pull waits on a person, or "" (also "" when it can't be read: the other gates
    and the verdict still apply, and a read failure shouldn't hold the whole board)."""
    from . import ai_review, comments
    if not token:
        return ""
    pull = ai_review._get(f"{_API}/{owner}/{repo}/pulls/{num}", token)
    viewer = comments._viewer_login(token)
    if not isinstance(pull, dict) or not viewer:
        return ""
    labels = [l.get("name") for l in pull.get("labels") or [] if isinstance(l, dict)]
    sha = (pull.get("head") or {}).get("sha")
    commit = ai_review._get(f"{_API}/{owner}/{repo}/commits/{sha}", token) if sha else None
    events = ai_review._get_all(f"{_API}/{owner}/{repo}/issues/{num}/events", token)
    thread = ai_review._get_all(f"{_API}/{owner}/{repo}/issues/{num}/comments", token)
    reviews = ai_review._get_all(f"{_API}/{owner}/{repo}/pulls/{num}/reviews", token)
    if events is None or thread is None or reviews is None or not isinstance(commit, dict):
        return ""
    at = head_time(commit, events)
    return (label_hold(events, thread, labels, viewer, at)
            or question_hold(thread, reviews, viewer, at))


def forward_gate(token: Optional[str]) -> Callable[[Dict[str, Any]], str]:
    """`(record) -> reason` ("" when not waiting) for the forward set."""
    from . import comments

    def gate(record: Dict[str, Any]) -> str:
        target = ((record.get("_audit") or {}).get("target") or "").strip()
        m = comments._PR_PARTS_REGEX.match(target)
        if not m:
            return ""
        key = f"{m.group(1)}/{m.group(2)}#{m.group(3)}"
        if key not in _STATES:
            _STATES[key] = pull_wait(m.group(1), m.group(2), m.group(3), token)
        return _STATES[key]

    return gate
