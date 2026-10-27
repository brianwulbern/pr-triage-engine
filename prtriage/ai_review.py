"""3.0.37: hold AI-resolved backport pulls until a person signs off (TICKET-1).

When an auto-backport hits a merge conflict, the backport bot lets a model resolve it and opens the pull with
the `ai-resolved-conflict` label. The release team agreed on 2026-10-14 that no
such pull forwards until a person has checked the resolution:

* The backport bot asks the authors of the original commits with a GitHub review request. When it hasn't,
  prtriage asks them once in a comment. A removed request or a comment-only review doesn't release
  the hold, and the board says so;
* a pull that changes Platform Core code (CODEOWNERS on master) or has a Platform Core commit author
  asks only Platform Core's reviewer (3.0.54), and earlier asks to the authors don't count;
* sign-off is a GitHub Approve review or an `LGTM` comment, or the `ai-resolution-signed-off`
  label added by hand, from a member of the repository's GitHub org (`product`). Never the bot that
  opened the pull and never the person forwarding it (the token owner), so a second person
  always looks at the model's work. 3.0.62: an opener who also wrote the commits can sign;
* only a sign-off newer than the pull's head commit counts: a push after it holds the pull again;
* a later "Request changes" review undoes an earlier sign-off.

prtriage adds `ai-resolution-signed-off` once it sees a valid sign-off, and takes it off again when
the sign-off goes stale. The forward script refuses to forward an `ai-resolved-conflict` pull whose sign-off
label is missing or older than its head commit. Everything here fails safe: anything prtriage can't
read holds the pull."""
from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import config

log = logging.getLogger(__name__)

_API = "https://api.github.com/repos"
_MARKER = "<!-- prtriage-ai-review-hold: managed by prtriage -->"
_SIGNED_MARKER = "<!-- prtriage-ai-review-signed-off -->"
_REHOLD_MARKER = "<!-- prtriage-ai-review-rehold -->"
_REMIND_MARKER = "<!-- prtriage-ai-review-reminder -->"

# The whole comment is the sign-off, so it can't be read out of a longer remark ("LGTM once
# the test passes" doesn't count). Trailing thanks and emoji are allowed. 3.0.51: also a short
# "tested it locally and it looks good" with a leading mention (dev-r, #1234/#1234).
_LGTM_RE = re.compile(
    r"^(?:@[\w-]+[\s,:]*)*(?:(?:thanks|thank you)[\s,.!]*)?"
    r"(?:(?:i\s+)?(?:tested|checked|reviewed|verified)\s+(?:this|it|the\s+(?:resolution|changes?|pull|pr))"
    r"(?:\s+locally)?(?:\s+too)?[\s,]*(?:and\s+)?)?"
    r"(?:lgtm|(?:(?:all|it|everything)\s+)?looks?\s+good(?:\s+to\s+me)?)"
    r"[\s,.!:]*(?:(?:thanks|thank you|ty)[\s.!]*)?$",
    re.IGNORECASE)
# 3.0.63: "LGTM." followed by an explanation counts when the explanation carries no condition,
# doubt or problem (dev-u on #1234).
# 3.0.64: also an LGTM on its own line (#1234), and only words that put a condition on the
# sign-off hold it back; evidence such as "failed on every base run" doesn't (#1234, #1234).
_FIRST_SENTENCE_RE = re.compile(r"(.+?(?:[.!]|(?=\n)))\s+(.+)$", re.DOTALL)
_HEDGE_RE = re.compile(
    r"\?|\b(?:once|if|when|until|unless|but|however|though|although|pending|still|needs?|"
    r"needed|should|must|please|wait)\b", re.IGNORECASE)


def is_ai_resolved(labels: Optional[Iterable[str]]) -> bool:
    return config.AI_CONFLICT_LABEL in (labels or ())


def is_lgtm(body: Optional[str]) -> bool:
    if not body or body.lstrip().startswith("<!-- prtriage-"):
        return False
    text = "".join(ch for ch in body if ord(ch) < 0x2000).strip()   # drop emoji
    if _LGTM_RE.match(text):
        return True
    m = _FIRST_SENTENCE_RE.match(text)
    return bool(m and _LGTM_RE.match(m.group(1))
                and not _HEDGE_RE.search(re.sub(r"`[^`]*`", "", m.group(2))))


def _is_bot(login: str, user_type: str = "") -> bool:
    from . import comments
    l = (login or "").lower()
    return (not l or user_type == "Bot" or l.endswith("[bot]")
            or l in comments._ASSIGN_BOT_LOGINS)


def find_signer(reviews: List[Dict[str, Any]], comments: List[Dict[str, Any]],
                events: List[Dict[str, Any]], head_at: Optional[str], excluded: Iterable[str],
                is_member: Callable[[str], bool],
                head_sha: Optional[str] = None) -> Optional[Tuple[str, str, str]]:
    """(login, how, at) of the newest valid sign-off made after `head_at`, else None.

    An Approve review must also be on `head_sha` when given (GitHub records the commit a review
    was made on, which catches a push of an older-dated commit). `head_at` is the later of the
    head commit's date and the last force-push; "" accepts any time (used to tell a stale
    sign-off from none); None means it couldn't be read, which holds."""
    if head_at is None:
        return None
    pushed = max([head_at] + [e.get("created_at") or "" for e in events or []
                              if e.get("event") == "head_ref_force_pushed"]) if head_at else ""
    head_at = pushed
    excluded = {(e or "").lower() for e in excluded}
    cands: List[Tuple[str, str, str]] = []
    block_at = ""
    for r in reviews or []:
        user = r.get("user") or {}
        login, at = user.get("login") or "", r.get("submitted_at") or ""
        state = str(r.get("state") or "").upper()
        if state == "APPROVED":
            if head_sha and head_at and r.get("commit_id") and r["commit_id"] != head_sha:
                continue                          # approved an earlier head
            cands.append((at, login, "Approve review", user.get("type") or ""))
        elif state == "CHANGES_REQUESTED" and not _is_bot(login, user.get("type") or ""):
            block_at = max(block_at, at)
    for c in comments or []:
        user = c.get("user") or {}
        if is_lgtm(c.get("body")):
            cands.append((c.get("created_at") or "", user.get("login") or "", "LGTM comment",
                          user.get("type") or ""))
    for e in events or []:
        if (e.get("event") == "labeled"
                and (e.get("label") or {}).get("name") == config.AI_CONFLICT_SIGNOFF_LABEL):
            actor = e.get("actor") or {}
            cands.append((e.get("created_at") or "", actor.get("login") or "",
                          f"`{config.AI_CONFLICT_SIGNOFF_LABEL}` label", actor.get("type") or ""))
    best = None
    for at, login, how, utype in sorted(cands, reverse=True):
        if not at or at <= head_at or at <= block_at:
            continue
        if _is_bot(login, utype) or login.lower() in excluded:
            continue
        if not is_member(login):
            continue
        best = (login, how, at)
        break
    return best


def author_reply(comments: List[Dict[str, Any]], reviews: List[Dict[str, Any]],
                 authors: Iterable[str], since: str) -> Optional[Tuple[str, str, str]]:
    """3.0.62: (login, at, url) of an author's newest comment or review after `since`, `ci:`
    commands aside. An author who answered is waiting on a person to read it, not on a
    reminder (#1234)."""
    who = {(a or "").lower() for a in authors}
    if not since or not who:
        return None
    best = None
    for c, at_key in [(c, "created_at") for c in comments or []] + \
                     [(r, "submitted_at") for r in reviews or []]:
        login = ((c.get("user") or {}).get("login") or "")
        body = (c.get("body") or "").strip()
        at = c.get(at_key) or ""
        if login.lower() not in who or not body or body.lower().startswith("ci:") or at <= since:
            continue
        if not best or at > best[1]:
            best = (login, at, c.get("html_url") or "")
    return best


def review_requests(pull: Dict[str, Any], events: List[Dict[str, Any]],
                    reviews: List[Dict[str, Any]], authors: List[str]) -> Dict[str, Any]:
    """3.0.42: the GitHub review requests the backport bot sends the original authors. `asked` maps each
    author ever requested to when; `removed` is (reviewer, actor) for a request someone took
    away; `commented` is an asked author whose only review was a comment. None of these release
    the hold, only a sign-off does."""
    want = {a.lower(): a for a in authors}
    current = {(u.get("login") or "").lower() for u in pull.get("requested_reviewers") or []
               if isinstance(u, dict)}
    asked: Dict[str, str] = {}
    removed: List[Tuple[str, str]] = []
    for e in sorted(events or [], key=lambda e: e.get("created_at") or ""):
        who = ((e.get("requested_reviewer") or {}).get("login") or "")
        if who.lower() not in want:
            continue
        if e.get("event") == "review_requested":
            asked.setdefault(want[who.lower()], e.get("created_at") or "")
        elif e.get("event") == "review_request_removed" and who.lower() not in current:
            actor = (e.get("actor") or {}).get("login") or ""
            if actor.lower() != who.lower():
                removed.append((want[who.lower()], actor))
    for login in current:
        if login in want:
            asked.setdefault(want[login], "")
    approved = {((r.get("user") or {}).get("login") or "").lower() for r in reviews or []
                if str(r.get("state") or "").upper() == "APPROVED"}
    commented = [a for a in asked
                 if a.lower() not in approved and a.lower() not in current
                 and any(((r.get("user") or {}).get("login") or "").lower() == a.lower()
                         and str(r.get("state") or "").upper() == "COMMENTED"
                         for r in reviews or [])]
    return {"asked": asked, "removed": [x for x in removed if x[0] not in commented],
            "commented": commented}


# ---------------------------------------------------------------------------------------
# reading a pull
# ---------------------------------------------------------------------------------------
def _get(url: str, token: Optional[str]) -> Any:
    from . import http_cache
    return http_cache.conditional_get_json(url, token)


def _get_all(url: str, token: Optional[str], pages: int = 10) -> Optional[List[Any]]:
    out: List[Any] = []
    for page in range(1, pages + 1):
        data = _get(f"{url}{'&' if '?' in url else '?'}per_page=100&page={page}", token)
        if not isinstance(data, list):
            return None
        out.extend(data)
        if len(data) < 100:
            return out
    return out


_PERM: Dict[str, bool] = {}


def human_authors(commits: List[Dict[str, Any]], viewer: Optional[str]) -> List[str]:
    """The people to ask: commit authors, never a bot, the forwarder, or a login prtriage never
    mentions (3.0.49: #1234 would have asked upstream-fork)."""
    from . import comments
    out = []
    for c in commits:
        a = c.get("author") or {}
        login = a.get("login")
        if (login and not _is_bot(login, a.get("type") or "")
                and login not in comments._NON_AUTHOR_LOGINS
                and login.lower() != (viewer or "").lower()):
            out.append(login)
    return list(dict.fromkeys(out))


_CODEOWNERS: Dict[str, Optional[List[Tuple[str, List[str]]]]] = {}


def parse_codeowners(text: str) -> List[Tuple[str, List[str]]]:
    rules = []
    for line in text.splitlines():
        parts = line.split("#", 1)[0].split()
        if parts:
            rules.append((parts[0].strip("/"), [o.lstrip("@") for o in parts[1:]]))
    return rules


def owners_of(path: str, rules: List[Tuple[str, List[str]]]) -> List[str]:
    """The last matching CODEOWNERS rule wins, as on GitHub."""
    import fnmatch
    owners: List[str] = []
    for pattern, who in rules:
        if any(ch in pattern for ch in "*?["):
            hit = fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern + "/*")
        else:
            hit = path == pattern or path.startswith(pattern + "/")
        if hit:
            owners = who
    return owners


def _codeowners(owner: str, repo: str, token: Optional[str]
                ) -> Optional[List[Tuple[str, List[str]]]]:
    """master's CODEOWNERS for every line: the release lines' copies lag it (1.x has no
    Platform Core entries)."""
    import base64
    key = f"{owner}/{repo}"
    if key not in _CODEOWNERS:
        data = _get(f"{_API}/{owner}/{repo}/contents/.github/CODEOWNERS?ref=master", token)
        try:
            text = base64.b64decode((data or {}).get("content") or "").decode("utf-8")
        except (ValueError, AttributeError):
            text = ""
        _CODEOWNERS[key] = parse_codeowners(text) if text else None
    return _CODEOWNERS[key]


def platform_team(authors: List[str], files: Optional[List[str]],
               rules: Optional[List[Tuple[str, List[str]]]]) -> bool:
    """3.0.54: True when Platform Core reviews the pull (a commit author on the team, or a changed
    path it owns). Unread files or CODEOWNERS fall back to the authors."""
    team = {m.lower() for m in config.PLATFORM_TEAM_MEMBERS}
    if any(a.lower() in team for a in authors):
        return True
    if files is None or rules is None:
        return False
    return any(config.PLATFORM_TEAM_CODEOWNER in owners_of(f, rules) for f in files)


def _org_member(org: str, login: str, token: Optional[str]) -> bool:
    """GET /orgs/{org}/members/{login}: 204 for a member. Developers send backports from forks
    and have no write access to product, so org membership is the "a person at
    product" test. Anything else, including a read failure, is not a member."""
    from . import http_cache, governor
    try:
        resp = governor.governed(lambda: http_cache.gh_get(
            f"https://api.github.com/orgs/{org}/members/{login}", timeout=30,
            headers={"Accept": "application/vnd.github+json",
                     "Authorization": f"token {token}"}))
        return resp.status_code == 204
    except Exception:  # noqa: BLE001
        return False


def _members(org: str, token: Optional[str]) -> Callable[[str], bool]:
    def check(login: str) -> bool:
        key = f"{org}/{login.lower()}"
        if key not in _PERM:
            _PERM[key] = _org_member(org, login, token)
        return _PERM[key]
    return check


def pull_state(owner: str, repo: str, num: str, token: Optional[str]) -> Dict[str, Any]:
    """Everything the hold needs about one pull. {"ai": False} for a pull without the label;
    "readable": False when anything needed couldn't be read (the pull then holds)."""
    from . import comments
    if not token:
        return {"ai": False}          # nothing can be read or forwarded without a token
    pull = _get(f"{_API}/{owner}/{repo}/pulls/{num}", token)
    if not isinstance(pull, dict):
        return {"ai": None, "readable": False}
    labels = [l.get("name") for l in pull.get("labels") or [] if isinstance(l, dict)]
    if not is_ai_resolved(labels):
        return {"ai": False}
    st: Dict[str, Any] = {
        "ai": True, "readable": False, "signer": None, "stale": None, "authors": [],
        "noticed": False, "base": (pull.get("base") or {}).get("ref") or "",
        "signed_label": config.AI_CONFLICT_SIGNOFF_LABEL in labels}
    viewer = comments._viewer_login(token)
    sha = (pull.get("head") or {}).get("sha")
    commit = _get(f"{_API}/{owner}/{repo}/commits/{sha}", token) if sha else None
    head_at = (((commit or {}).get("commit") or {}).get("committer") or {}).get("date")
    reviews = _get_all(f"{_API}/{owner}/{repo}/pulls/{num}/reviews", token)
    comments = _get_all(f"{_API}/{owner}/{repo}/issues/{num}/comments", token)
    events = _get_all(f"{_API}/{owner}/{repo}/issues/{num}/events", token)
    commits = _get_all(f"{_API}/{owner}/{repo}/pulls/{num}/commits", token) or []
    st["authors"] = human_authors(commits, viewer)
    files = _get_all(f"{_API}/{owner}/{repo}/pulls/{num}/files", token)
    paths = None if files is None else [f.get("filename") or "" for f in files]
    rules = _codeowners(owner, repo, token) if paths is not None else None
    core = platform_team(st["authors"], paths, rules)
    if not (viewer and head_at and reviews is not None and comments is not None
            and events is not None):
        return st
    if core:
        st["team"] = "Platform Core"
        st["authors"] = [config.PLATFORM_TEAM_AI_REVIEWER]
    st["readable"] = True

    def ours(c: Dict[str, Any], marker: str) -> bool:
        body = (c.get("body") or "").lstrip()
        return body.startswith(marker) and (
            not core or config.PLATFORM_TEAM_AI_REVIEWER.lower() in body.lower())

    st["noticed"] = any(ours(c, _MARKER) for c in comments)
    asks = [c.get("created_at") or "" for c in comments if ours(c, _MARKER)]
    reminders = [c.get("created_at") or "" for c in comments if ours(c, _REMIND_MARKER)]
    st["asked_at"] = min([a for a in asks if a], default="")
    st["reminded_at"] = max([a for a in reminders if a], default="")
    st["replied"] = author_reply(comments, reviews, st["authors"], st["asked_at"])
    st["requests"] = review_requests(pull, events, reviews, st["authors"])
    requested = [at for at in (st["requests"].get("asked") or {}).values() if at]
    if requested:
        st["asked_at"] = min([st["asked_at"]] + requested if st["asked_at"] else requested)
    opener = (pull.get("user") or {}).get("login") or ""
    excluded = [viewer] + ([] if opener.lower() in {x.lower() for x in st["authors"]} else [opener])
    is_member = _members(owner, token)
    st["signer"] = find_signer(reviews, comments, events, head_at, excluded, is_member, sha)
    if not st["signer"]:
        st["stale"] = find_signer(reviews, comments, events, "", excluded, is_member)
    return st


_STATES: Dict[str, Dict[str, Any]] = {}


def _state(owner: str, repo: str, num: str, token: Optional[str]) -> Dict[str, Any]:
    key = f"{owner}/{repo}#{num}"
    if key not in _STATES:
        _STATES[key] = pull_state(owner, repo, num, token)
    return _STATES[key]


def held(st: Dict[str, Any]) -> bool:
    """True when the pull must not forward: AI-resolved (or unreadable) and not signed off."""
    if st.get("ai") is False:
        return False
    return not (st.get("readable") and st.get("signer"))


def forward_gate(token: Optional[str]) -> Callable[[Dict[str, Any]], bool]:
    """`(record) -> held` for the forward set."""
    from . import comments

    def gate(record: Dict[str, Any]) -> bool:
        target = ((record.get("_audit") or {}).get("target") or "").strip()
        m = comments._PR_PARTS_REGEX.match(target)
        if not m:
            return False
        return held(_state(m.group(1), m.group(2), m.group(3), token))

    return gate


# ---------------------------------------------------------------------------------------
# the board step
# ---------------------------------------------------------------------------------------
def _mentions(authors: List[str]) -> str:
    return " ".join(f"@{a}" for a in authors)


def ask_text(st: Dict[str, Any]) -> str:
    who = _mentions(st.get("authors") or [])
    lead = f"{who} the backport bot's" if who else "The backport bot's"
    base = f" to `{st['base']}`" if st.get("base") else ""
    out = "If you're out" if st.get("team") else "If the original authors are out"
    return (f"{lead} model resolved a merge conflict to backport this change{base} "
            f"(`{config.AI_CONFLICT_LABEL}`), so a person needs to check the resolution before "
            f"it can forward. Please review the resolved files listed in the description, then "
            f"approve the pull or comment `LGTM`. {out}, anyone in the "
            f"`product` GitHub org can sign off; the person forwarding it can't. prtriage "
            f"and the forward script hold it until then, and a push after the sign-off needs a new one.")


def group_text(others: List[str]) -> str:
    return (f"\n\nThe same request is open on {', '.join('#' + n for n in others)}; one review "
            f"pass can cover them all.") if others else ""


def pointer_text(first: str, kind: str = "asked", st: Optional[Dict[str, Any]] = None) -> str:
    who = config.PLATFORM_TEAM_AI_REVIEWER if (st or {}).get("team") else "the authors"
    return (f"{'Reminded' if kind == 'remind' else 'Asked'} {who} on #{first} for this "
            f"pull too: a person needs to check `{config.AI_CONFLICT_LABEL}`'s resolution here "
            f"before it can forward (approve the pull or comment `LGTM`).")


def remind_text(st: Dict[str, Any]) -> str:
    who = _mentions(st.get("authors") or [])
    lead = f"{who} a" if who else "A"
    return (f"{lead} reminder: this pull still needs a sign-off on the model's conflict "
            f"resolution before it can forward. Please check the resolved files, then approve "
            f"the pull or comment `LGTM`. If you're not the right person, anyone in the "
            f"`product` GitHub org other than the forwarder can sign off.")


def signed_text(signer: Tuple[str, str, str]) -> str:
    login, how, at = signer
    return (f"`{login}` signed off the AI conflict resolution ({how}, {at}). prtriage added "
            f"`{config.AI_CONFLICT_SIGNOFF_LABEL}`, so this pull can forward.")


def rehold_text(st: Dict[str, Any]) -> str:
    who = _mentions(st.get("authors") or [])
    lead = f"{who} " if who else ""
    if st.get("stale"):
        login, how, _at = st["stale"]
        why = (f"new commits were pushed after `{login}`'s sign-off ({how}), so this pull is "
               f"held again")
    else:
        why = (f"`{config.AI_CONFLICT_SIGNOFF_LABEL}` was added without a sign-off prtriage can "
               f"verify (an Approve review or `LGTM` comment from a `product` org member "
               f"other than the forwarder), so this pull is held again")
    return (f"{lead}{why}. Please review the current changes, then approve the pull or comment "
            f"`LGTM`.")


def waiting_text(st: Dict[str, Any]) -> str:
    rq = st.get("requests") or {}
    asked = rq.get("asked") or {}
    if not asked:
        return "prtriage asked in a comment"
    parts = ["review requested from " + ", ".join(
        f"@{a}" + (f" since {at[:10]}" if at else "") for a, at in asked.items())]
    parts += [f"@{a} commented without approving" for a in rq.get("commented") or []]
    parts += [f"@{actor or '?'} removed the request to @{who}, still held"
              for who, actor in rq.get("removed") or []]
    return "; ".join(parts)


def classify(records: List[Dict[str, Any]], token: Optional[str],
             repo_filter: str, now: Optional[datetime.date] = None,
             replaced: Optional[Dict[str, str]] = None) -> Dict[str, List[Tuple[str, str, str, Dict[str, Any]]]]:
    from . import comments
    out: Dict[str, List[Tuple[str, str, str, Dict[str, Any]]]] = {
        "ask": [], "release": [], "rehold": [], "waiting": [], "unreadable": [],
        "remind": [], "overdue": [], "landed": [], "replaced": [], "replied": []}
    seen: set = set()
    for r in records:
        target = ((r.get("_audit") or {}).get("target") or "").strip()
        m = comments._PR_PARTS_REGEX.match(target)
        if not m or repo_filter not in target or m.group(3) in seen:
            continue
        owner, repo, num = m.group(1), m.group(2), m.group(3)
        seen.add(num)
        st = _state(owner, repo, num, token)
        if st.get("ai") is False:
            continue
        row = (owner, repo, num, st)
        if not st.get("readable"):
            out["unreadable"].append(row)
        elif st.get("signer"):
            if not st.get("signed_label"):
                out["release"].append(row)
        elif st.get("signed_label"):
            out["rehold"].append(row)
        elif _landed(r):
            out["landed"].append(row)
        elif num in (replaced or {}):
            out["replaced"].append(row)
        elif not st.get("noticed") and not (st.get("requests") or {}).get("asked"):
            out["ask"].append(row)
        elif st.get("replied"):
            out["replied"].append(row)
        elif _due(st.get("reminded_at") or st.get("asked_at"), now):
            out["overdue" if st.get("reminded_at") else "remind"].append(row)
        else:
            out["waiting"].append(row)
    return out


def _landed(record: Dict[str, Any]) -> bool:
    """3.0.53: the pull's change is already on the upstream fork (#1234, #1234), so it closes; no ask."""
    verdict = ((record.get("result") or {}).get("verdict") or {})
    return ((verdict.get("landing") or {}).get("state") == "landed")


def workdays_between(start: str, end: datetime.date) -> int:
    """Weekdays after `start`'s date up to and including `end` (0 when unreadable)."""
    try:
        day = datetime.date.fromisoformat(start[:10])
    except ValueError:
        return 0
    n = 0
    while day < end:
        day += datetime.timedelta(days=1)
        n += day.weekday() < 5
    return n


def _due(since: Optional[str], now: Optional[datetime.date]) -> bool:
    return bool(since) and workdays_between(since, now or datetime.date.today()) \
        >= config.AI_REVIEW_REMIND_WORKDAYS


def board_step(records: List[Dict[str, Any]], token: Optional[str], apply: bool,
               repo_filter: str, interactive: bool,
               prompt: Callable[[str], str],
               replaced: Optional[Dict[str, str]] = None) -> None:
    """Print the AI-resolution holds and, on `apply` or a [y/N], ask / release / re-hold."""
    from . import github_writes, comments
    c = classify(records, token, repo_filter, replaced=replaced)
    if not any(c.values()):
        return
    print("\n" + "─" * 70)
    print(f"🤖 AI-resolved conflicts (`{config.AI_CONFLICT_LABEL}`): held until a person other "
          f"than you signs off")
    for _o, _r, num, st in c["release"]:
        login, how, _at = st["signer"]
        print(f"    ✅ #{num}  signed off by @{login} ({how}); would add "
              f"`{config.AI_CONFLICT_SIGNOFF_LABEL}`")
    for _o, _r, num, st in c["rehold"]:
        why = (f"pushed to after @{st['stale'][0]}'s sign-off" if st.get("stale")
               else "label added without a sign-off prtriage can verify")
        print(f"    ↩ #{num}  {why}; would remove `{config.AI_CONFLICT_SIGNOFF_LABEL}` and ask "
              f"again")
    for _o, _r, num, st in c["ask"]:
        who = _mentions(st.get("authors") or []) or "(no human commit author found)"
        team = f" ({st['team']})" if st.get("team") else ""
        print(f"    → #{num}  would ask {who}{team} to review the resolution")
    for _o, _r, num, st in c["remind"]:
        print(f"    🔔 #{num}  no sign-off since {st['asked_at'][:10]}; would remind "
              f"{_mentions(st.get('authors') or []) or 'the authors'}")
    for _o, _r, num, st in c["waiting"]:
        print(f"    ⏸  #{num}  awaiting sign-off: {waiting_text(st)}")
    for _o, _r, num, st in c["overdue"]:
        print(f"    ⏰ #{num}  reminded {st['reminded_at'][:10]}, still no sign-off: ask someone "
              f"else in the `product` org to review it")
    for _o, _r, num, st in c["replied"]:
        login, at, url = st["replied"]
        print(f"    💬 #{num}  @{login} replied {at[:10]}, no reminder: read it"
              + (f" ({url})" if url else ""))
    for _o, _r, num, _st in c["replaced"]:
        print(f"    ⏭  #{num}  #{replaced[num]} carries the same ticket on this branch and goes "
              f"forward, no ask")
    for _o, _r, num, _st in c["landed"]:
        print(f"    ⏭  #{num}  already on the upstream fork, no ask (close it instead)")
    if c["unreadable"]:
        print("    ⚠️  couldn't read the sign-off state, held: "
              + ", ".join(f"#{n}" for _o, _r, n, _s in c["unreadable"]))
    writes = c["release"] + c["rehold"] + c["ask"] + c["remind"]
    if not writes:
        return
    do = apply
    if not apply and interactive:
        ans = prompt(f"\n   Apply AI-review changes ({len(c['release'])} release, "
                     f"{len(c['ask'])} ask, {len(c['remind'])} remind, "
                     f"{len(c['rehold'])} re-hold)? [y/N] ")
        do = (ans or "").strip().lower() in ("y", "yes")
    if not do:
        print("   (dry-run - nothing changed; --apply or answer y to act)")
        return
    full = lambda o, r: f"{o}/{r}"                                     # noqa: E731
    for o, r, num, st in c["release"]:
        lab = comments.add_pr_labels(o, r, num, [config.AI_CONFLICT_SIGNOFF_LABEL], token)
        ok = lab.get("ok") and github_writes._comment(
            token, full(o, r), num, f"{_SIGNED_MARKER}\n{signed_text(st['signer'])}")
        print(f"    ✅ #{num}  " + ("released" if ok else f"release incomplete ({lab})"))
    for o, r, num, st in c["rehold"]:
        rm = comments.remove_pr_label(o, r, num, config.AI_CONFLICT_SIGNOFF_LABEL, token)
        ok = github_writes._comment(token, full(o, r), num, f"{_REHOLD_MARKER}\n{rehold_text(st)}")
        print(f"    ↩ #{num}  " + ("held again" if ok else f"re-hold incomplete ({rm})"))
    for kind, marker, text in (("ask", _MARKER, ask_text), ("remind", _REMIND_MARKER,
                                                                remind_text)):
        for rows in by_authors(c[kind]):
            first = rows[0][2]
            for i, (o, r, num, st) in enumerate(rows):
                body = (text(st) + group_text([x[2] for x in rows[1:]]) if i == 0
                        else pointer_text(first, kind, st))
                ok = github_writes._comment(token, full(o, r), num, f"{marker}\n{body}")
                verb = "asked" if kind == "ask" else "reminded"
                print(f"    {'→' if kind == 'ask' else '🔔'} #{num}  "
                      + ((verb if i == 0 else f"{verb} (pointer to #{first})") if ok
                         else "couldn't post; ask by hand"))


def by_authors(rows: List[Tuple[str, str, str, Dict[str, Any]]]
               ) -> List[List[Tuple[str, str, str, Dict[str, Any]]]]:
    """3.0.53: one @-mention per author set; the set's other pulls get a pointer comment that
    still carries the marker, so each pull counts as asked."""
    groups: Dict[Tuple[str, ...], List[Tuple[str, str, str, Dict[str, Any]]]] = {}
    for row in rows:
        key = tuple(sorted(a.lower() for a in row[3].get("authors") or [])) or (row[2],)
        groups.setdefault(key, []).append(row)
    return [sorted(g, key=lambda x: int(x[2])) for g in groups.values()]
