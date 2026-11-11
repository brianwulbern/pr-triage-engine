"""3.1.0: two models review an AI-resolved conflict, independently, to help the person who
signs it off.

A backtest on 85 bot-resolved pulls: both models flagged all four resolutions that were
really broken, two of which a person had approved, and passed 32 of the 35 a person accepted. It is a review aid, not a sign-off: the hold in
ai_review is unchanged, and every verdict goes to a ledger so it can be compared with the
person's call later.

Evidence per resolved file: the conflict as git leaves it (rebuilt with merge-tree, diff3),
master's own change, and what the pull applied. Each reviewer may run read-only `git grep`,
`git show` and `git ls-tree` against the pull head in the local portal clone."""
import concurrent.futures as cf
import datetime
import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from . import config

LAYOUT = 2
MARKER = "<!-- prtriage-double-agent: {sha} v%d -->" % LAYOUT
PROMPT_VERSION = 2
_MARKER_RE = re.compile(r"<!-- prtriage-double-agent: ([0-9a-f]{7,40})(?: v(\d+))? -->")
BUILD_MARKER = "<!-- prtriage-double-agent-build: {build} -->"
_BUILD_RE = re.compile(r"<!-- prtriage-double-agent-build: (\w+) -->")
_PREVIOUS_RE = re.compile(r"Previous pull requests:\s*\n((?:\s*https://github\.com/\S+\s*\n?)+)")
_PULL_NUM_RE = re.compile(r"github\.com/example-org/product/pull/(\d+)")
_COMPILE_CODES = frozenset({"compile_break", "upgrade_compile_break", "ci_compile_break",
                            "playwright_compile_break"})
_WAIT_CODES = frozenset({"head_unbuilt", "head_ci_running", "head_ci_importing"})
_NOT_COMPILE_FIXES = frozenset({"lang_auto_copy", "flag_gated_call"})
_SENTENCE_RE = re.compile(r"(.+?[.!?])(?:\s|$)", re.S)
_SECTION_RE = re.compile(r"Resolved while cherry-picking\s+https://github\.com/example-org/"
                         r"product/commit/([0-9a-f]{40})")
_FILES_RE = re.compile(r"Resolved files:\s*\n((?:\s*\*\s*`[^`]+`\s*\n?)+)")
_RISKS_RE = re.compile(r"Risks reported by the model:\s*\n(.*?)(?:\n\s*\n(?!\*)|\Z)", re.S)

SYSTEM = ("You review merge-conflict resolutions on product backports. You may only run the "
          "read-only git commands you are given. Your final message is one JSON object and "
          "nothing else.")
INSTR = """A model resolved the merge conflicts below while the backport bot cherry-picked a master commit onto a product release branch. You are one of two independent reviewers. A person may rely on your answer to decide whether the resolution is safe to forward without reading it themselves.

Release branches diverge from master on purpose: they keep older APIs, their own upgrade-step and schema versions, and their own test setup. A resolution is correct when it applies the master commit's intent to the branch's code and keeps the branch's own code working. It does not have to equal master. Leaving master code out is fine when the branch doesn't need or can't take it; extra adaptation is fine when the branch needs it.

Raise a concern only for something that would make the backport wrong on this branch:
- the master change's intent is lost or only partly applied;
- branch-only code is dropped or broken;
- the result uses a class, method, field, import, rule or signature the branch side ("ours") doesn't show it has, or calls something with arguments its branch declaration doesn't take;
- an upgrade step, schema version, bnd version or packageinfo version doesn't follow the branch's own sequence, or reuses a version range another release line uses for a different step;
- a test asserts behavior the branch doesn't have, or loses setup the branch's other tests rely on;
- duplicated, unbalanced or left-over code.
Don't raise style, naming, or "differs from master" on its own.

You can read the branch with these read-only commands (nothing else is allowed):
  git grep -n <pattern> {REF} -- <path or dir>
  git show {REF}:<path>
  git ls-tree -r --name-only {REF} -- <dir>
{REF} is the pull's head on {BASE}. Commits after the pick may already have changed the resolved files; the evidence lists them, and the pull is judged at {REF}, not at the pick. When a hunk relies on a class, method, field, rule or version you can't see in the evidence, check it on {REF} before deciding. Raise a concern only for what you confirmed is wrong, or for what you still can't settle after checking (say exactly what a person must check). Use a handful of lookups, not an exhaustive audit.

Return only: {"verdict": "pass" or "concern", "hunks": [{"file": "<file name>", "issue": "<what is wrong or must be checked>"}], "reason": "<one or two sentences>"}. "hunks" is empty for a pass.

EVIDENCE
"""


def enabled() -> bool:
    return not os.environ.get("PRTRIAGE_NO_DOUBLE_AGENT", "").strip() and bool(shutil.which("claude"))


def _dir() -> str:
    d = os.path.join(config.cache_root(), "prtriage", "double-agent")
    os.makedirs(d, exist_ok=True)
    return d


def sections(body: Optional[str]) -> List[Dict[str, Any]]:
    """The backport bot's 'Resolved while cherry-picking' sections: master commit, resolved files, the
    resolving model's own risk notes."""
    parts = _SECTION_RE.split(body or "")
    out = []
    for i in range(1, len(parts), 2):
        txt = parts[i + 1]
        m = _FILES_RE.search(txt)
        risks = _RISKS_RE.search(txt)
        out.append({"master": parts[i],
                    "files": re.findall(r"`([^`]+)`", m.group(1)) if m else [],
                    "risks": (risks.group(1).strip() if risks else "")[:3000]})
    return out


def _git(root: str, *args: str, ok: Tuple[int, ...] = (0,)) -> Optional[str]:
    try:
        p = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                           timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return p.stdout if p.returncode in ok else None


def _cap(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[:n]) + (f"\n... [{len(lines) - n} more lines cut]"
                                   if len(lines) > n else "")


def regions(text: str, ctx: int = 4) -> List[str]:
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        if lines[i].startswith("<<<<<<<"):
            j = i
            while j < len(lines) and not lines[j].startswith(">>>>>>>"):
                j += 1
            a, b = max(0, i - ctx), min(len(lines), j + 1 + ctx)
            out.append(f"@@ lines {a + 1}-{b} @@\n" + "\n".join(lines[a:b]))
            i = j + 1
        else:
            i += 1
    return out


def previous_pulls(body: Optional[str]) -> List[str]:
    """3.1.5: the pulls a resend's "Previous pull requests:" block names, newest first."""
    out: List[str] = []
    for block in _PREVIOUS_RE.findall(body or ""):
        for num in reversed(_PULL_NUM_RE.findall(block)):
            if num not in out:
                out.append(num)
    return out


def match_picks(secs: List[Dict[str, Any]], commits: List[Dict[str, Any]], root: str) -> None:
    """Each section's `pick`: the pull's product-release commit with the master commit's
    subject. 3.1.5: a rebased resend's pick is committed by whoever rebased it (#1234,
    #1234), so a commit with the subject counts when no product-release commit has it."""
    for s in secs:
        subj = (_git(root, "log", "-1", "--format=%s", s["master"]) or "").strip()
        same = [c for c in commits
                if subj and ((c.get("commit") or {}).get("message") or "").split("\n")[0] == subj]
        bot = [c for c in same
               if ((c.get("committer") or {}).get("login") or "") in config.AI_SIGNOFF_SHARED_LOGINS]
        if bot or same:
            s["pick"] = (bot or same)[0].get("sha")


def build_state(record: Optional[Dict[str, Any]]) -> str:
    """3.1.5: whether CI compiled the pull at the head the board read. "failed" on a compile
    verdict, a javac error or a compile-type quick fix (#1234's missing JUnit 4.13);
    "unbuilt" while no build covers the head; else "compiled". Both models passed #1234 and
    #1234, which didn't compile, so a pass counts only once CI compiled it."""
    v = ((record or {}).get("result") or {}).get("verdict") or {}
    if not v:
        return "unbuilt"
    code = v.get("reason_code") or ""
    if code in _COMPILE_CODES or v.get("compile_errors") or any(
            (q.get("kind") or "") not in _NOT_COMPILE_FIXES for q in v.get("quick_fixes") or []):
        return "failed"
    if code in _WAIT_CODES or v.get("action") == "wait" or not v.get("builds_triaged"):
        return "unbuilt"
    return "compiled"


def bundle(num: str, base: str, title: str, secs: List[Dict[str, Any]], root: str,
           commits: Optional[List[Dict[str, Any]]] = None,
           origin: Optional[str] = None) -> Optional[str]:
    """The evidence both reviewers read, or None when no section matched a pick."""
    parts = [f"Pull #{num} on {base}: {title}"]
    if origin:
        parts.append(f"This pull resends #{origin}, whose conflicts the backport bot's model resolved. The "
                     f"resolved commits were carried over and rebased onto the current base, "
                     f"so a resolution below can also include changes made while rebasing.")
    if not any(s.get("pick") for s in secs):
        return None
    for s in secs:
        pick, m = s.get("pick"), s["master"]
        if not pick:
            parts.append(f"\n## master commit {m[:12]}: backport-bot commit not found in the pull; "
                         f"skipped")
            continue
        subj = (_git(root, "log", "-1", "--format=%s", m) or "").strip()
        out = _git(root, "-c", "merge.conflictStyle=diff3", "merge-tree", "--write-tree",
                   "--name-only", f"--merge-base={m}^", f"{pick}^", m, ok=(0, 1)) or ""
        tree = out.split("\n", 1)[0].strip()
        parts.append(f"\n## Cherry-pick of master commit {m[:12]} \"{subj}\" onto {base}")
        if s.get("risks"):
            parts.append("Notes the resolving model left in the pull body (they may be "
                         "wrong):\n" + _cap(s["risks"], 30))
        for f in s["files"]:
            regs = regions((_git(root, "show", f"{tree}:{f}") if tree else None) or "")
            mdiff = _git(root, "diff", f"{m}^", m, "--", f) or ""
            applied = _git(root, "diff", f"{pick}^", pick, "--", f) or ""
            parts.append(f"\n### File {f}")
            parts.append(f"#### Conflict as git left it (diff3: ours = {base} before the pick, "
                         f"base = master's parent, theirs = master), {len(regs)} region(s)\n"
                         + (_cap("\n\n".join(regs), 220) if regs
                            else "(no textual conflict markers rebuilt for this file)"))
            parts.append("#### Master's own change to this file\n" + (_cap(mdiff, 160) or "(none)"))
            parts.append(f"#### What the pull applied to {base} for this file (the resolution)\n"
                         + (_cap(applied, 220)
                            or "(no change: the file was left as the branch had it)"))
    later = later_commits(secs, commits, root)
    if later:
        parts.append("\n## Later commits on the pull that change the resolved files (the pull "
                     "head includes them)")
        for sha, subj, files in later:
            diff = _git(root, "diff", f"{sha}^", sha, "--", *files) or ""
            parts.append(f"\n### {sha[:12]} \"{subj}\"\n" + _cap(diff, 160))
    return "\n".join(parts)


def later_commits(secs: List[Dict[str, Any]], commits: List[Dict[str, Any]], root: str
                  ) -> List[Tuple[str, str, List[str]]]:
    """3.1.1: commits after the first pick that change a resolved file. A
    reviewer shown only the pick read a test the pull had already fixed as broken."""
    picks = {s.get("pick") for s in secs if s.get("pick")}
    resolved = {f for s in secs for f in s["files"]}
    shas = [c.get("sha") or "" for c in commits]
    first = min((shas.index(p) for p in picks if p in shas), default=None)
    if first is None:
        return []
    out = []
    for sha, c in zip(shas[first + 1:], commits[first + 1:]):
        if sha in picks:
            continue
        names = (_git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", sha) or "").split()
        hit = [n for n in names if n in resolved]
        if hit:
            subj = ((c.get("commit") or {}).get("message") or "").split("\n")[0]
            out.append((sha, subj, hit))
    return out


def ensure_local(root: str, owner: str, repo: str, num: str, head: str) -> bool:
    """The pull head and master in the local clone, fetched when missing."""
    from . import local_clone as rfc
    if not rfc._master_ref(root, owner, repo):
        return False
    if _git(root, "cat-file", "-e", f"{head}^{{commit}}") is None:
        remote = rfc._master_ref(root, owner, repo).split("/", 1)[0]
        rfc._git(root, ["fetch", "-q", remote, f"pull/{num}/head"], timeout=180)
    return _git(root, "cat-file", "-e", f"{head}^{{commit}}") is not None


def last_json(text: str) -> Optional[Dict[str, Any]]:
    """The last JSON object with a verdict; also one whose closing brackets were cut off
    (three of the backtest's 170 answers)."""
    dec = json.JSONDecoder()
    for tail in ("", "}", "]}", "\"}", "\"}]}"):
        best = None
        for i, ch in enumerate(text + tail):
            if ch != "{":
                continue
            try:
                obj, _ = dec.raw_decode((text + tail)[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("verdict") in ("pass", "concern"):
                best = obj
        if best:
            return best
    return None


def _ask(model: str, prompt: str, root: str) -> Dict[str, Any]:
    tools = [f"Bash(git {c}:*)" for c in ("grep", "show", "ls-tree")]
    try:
        p = subprocess.run(
            ["claude", "-p", "--output-format", "json", "--model", model, "--tools", "Bash",
             "--allowedTools", *tools, "--strict-mcp-config", "--system-prompt", SYSTEM,
             "--no-session-persistence"],
            input=prompt, capture_output=True, text=True, timeout=900, cwd=root)
        env = json.loads(p.stdout)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return {"model": model, "answer": None}
    return {"model": model, "answer": last_json(env.get("result") or ""),
            "cost": env.get("total_cost_usd")}


def review(num: str, head: str, base: str, title: str, body: str,
           commits: List[Dict[str, Any]], root: str,
           origin: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Both verdicts for `num` at `head`, cached on disk per head. None when there is no
    evidence to review or a model gave no usable answer (nothing is cached then). `body` is
    the resent pull's own body when `origin` is set."""
    path = os.path.join(_dir(), f"{num}-{head[:12]}-v{PROMPT_VERSION}.json")
    if os.path.exists(path):
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    secs = sections(body)
    if not secs:
        return None
    match_picks(secs, commits, root)
    ev = bundle(num, base, title, secs, root, commits, origin)
    if not ev:
        return None
    prompt = INSTR.replace("{REF}", head).replace("{BASE}", base) + ev
    with cf.ThreadPoolExecutor(len(config.DOUBLE_AGENT_MODELS)) as ex:
        got = list(ex.map(lambda m: _ask(m[1], prompt, root), config.DOUBLE_AGENT_MODELS))
    if not all(g["answer"] for g in got):
        return None
    rec = {"pr": num, "head": head, "base": base, **({"origin": origin} if origin else {}),
           "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "reviews": [dict(g, name=n) for g, (n, _m) in zip(got, config.DOUBLE_AGENT_MODELS)]}
    with open(path, "w") as fh:
        json.dump(rec, fh, indent=1)
    return rec


def both_pass(rec: Dict[str, Any]) -> bool:
    return all((r.get("answer") or {}).get("verdict") == "pass" for r in rec["reviews"])


_NOT_COUNTED = {"unbuilt": "CI hasn't compiled this head yet",
                "failed": "CI found a compile problem at this head"}


def summary(rec: Dict[str, Any], build: Optional[str] = None) -> str:
    v = ", ".join(f"{r['name']} {(r['answer'] or {}).get('verdict')}" for r in rec["reviews"])
    if both_pass(rec) and build in _NOT_COUNTED:
        return f"both pass, not counted yet: {_NOT_COUNTED[build]} ({v})"
    return ("both pass" if both_pass(rec) else "concern") + f" ({v})"


def cached(num: str, head: str) -> Optional[Dict[str, Any]]:
    """The review already on disk for `num` at `head`, from this prompt version or before."""
    for name in (f"{num}-{head[:12]}-v{PROMPT_VERSION}.json", f"{num}-{head[:12]}.json"):
        try:
            with open(os.path.join(_dir(), name)) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            continue
    return None


def _first_sentence(text: str, cap: int = 240) -> str:
    t = " ".join((text or "").split())
    m = _SENTENCE_RE.match(t)
    t = m.group(1) if m else t
    return t if len(t) <= cap else t[:cap - 1].rstrip() + "…"


def concerns(rec: Dict[str, Any]) -> List[Tuple[str, str, List[str]]]:
    """(file or "", first sentence, model names): one row per flagged file, both models'
    flags on the same file merged, the shorter sentence kept."""
    rows: Dict[str, Tuple[str, List[str]]] = {}
    for r in rec["reviews"]:
        a = r.get("answer") or {}
        if a.get("verdict") != "concern":
            continue
        flags = [(h.get("file") or "", h.get("issue") or "") for h in a.get("hunks") or []] \
            or [("", a.get("reason") or "")]
        for f, issue in flags:
            line = _first_sentence(issue)
            if f in rows:
                old, names = rows[f]
                rows[f] = (min(old, line, key=len), names + [r["name"]])
            else:
                rows[f] = (line, [r["name"]])
    return [(f, line, names) for f, (line, names) in rows.items()]


def comment_text(rec: Dict[str, Any], mention: Optional[List[str]] = None,
                 build: Optional[str] = None) -> str:
    """3.1.2: the verdict first, each model's full review folded below it. `mention` pings
    the people the sign-off ask pings, only on a concern. 3.1.5: `build` says whether CI
    compiled this head; a pass is only presented as one once it did."""
    head = rec["head"]
    out = [MARKER.format(sha=head) + (BUILD_MARKER.format(build=build) if build else "")]
    if both_pass(rec) and build == "unbuilt":
        out.append(f"🕵 **Double-agent review: both models pass, but CI hasn't compiled "
                   f"{head[:10]} yet.** The models read the resolution, not the build, so this "
                   f"pass counts once CI compiles this head.")
    elif both_pass(rec) and build == "failed":
        out.append(f"🕵 **Double-agent review: both models pass, but CI found a compile problem "
                   f"at {head[:10]}.** The models read the resolution, not the build; see prtriage's "
                   f"verdict on this pull for the compile problem.")
    elif both_pass(rec):
        out.append(f"🕵 **Double-agent review: both models pass.** Fable and Opus each "
                   f"reviewed this conflict resolution on their own and found nothing wrong "
                   f"at {head[:10]}" + (", and CI compiled it." if build == "compiled" else "."))
    else:
        rows = concerns(rec)
        who = " ".join(f"@{a}" for a in mention or [])
        what = "a concern" if len(rows) == 1 else f"{len(rows)} concerns"
        out.append(f"{who + ' ' if who else ''}🕵 **Double-agent review: {what} to check "
                   f"before signing off.**")
        out += [(f"- `{f.rsplit('/', 1)[-1]}`: " if f else "- ") + f"{line} "
                f"({', '.join(names)})" for f, line, names in rows]
    out.append("\n<details><summary>Both reviews</summary>\n")
    for r in rec["reviews"]:
        a = r.get("answer") or {}
        out.append(f"**{r['name']}** (`{r['model']}`): {a.get('verdict')}. "
                   f"{(a.get('reason') or '').strip()}")
        out += [f"- `{h.get('file', '')}`: {(h.get('issue') or '').strip()}"
                for h in a.get("hunks") or []]
        out.append("")
    out.append(f"Each model read the conflict as git left it, master's change, what this pull "
               f"applied and any later commits on the resolved files, and checked the branch "
               f"at {head[:10]} with read-only git lookups.\n</details>")
    out.append("\nThis is a review aid, not a sign-off: the pull stays held until a person "
               "other than the forwarder approves it or comments `LGTM`.")
    return "\n".join(out).replace("\u2014", ",")


def ask_line(rec: Optional[Dict[str, Any]], build: Optional[str] = None) -> str:
    """3.1.2: what a sign-off ask or reminder says about the review on the same pull."""
    if not rec:
        return ""
    if both_pass(rec) and build in _NOT_COUNTED:
        return (f"\n\nTwo models also reviewed the resolution here and both pass, but "
                f"{_NOT_COUNTED[build]}; see the double-agent review on this pull.")
    if both_pass(rec):
        return ("\n\nTwo models also reviewed the resolution here and both pass; see the "
                "double-agent review on this pull.")
    return ("\n\nTwo models also reviewed the resolution here and raised a concern; see the "
            "double-agent review on this pull before signing off.")


def posted_heads(comments: List[Dict[str, Any]], viewer: Optional[str]) -> set:
    return set(posted_comments(comments, viewer))


def posted_comments(comments: List[Dict[str, Any]], viewer: Optional[str]
                    ) -> Dict[str, Tuple[Any, int, Optional[str]]]:
    """{head: (comment id, layout, build state or None)} for the viewer's double-agent
    comments; layout 1 is the 3.1.0 comment, which had no version in its marker."""
    me = (viewer or "").lower()
    out: Dict[str, Tuple[Any, int, Optional[str]]] = {}
    for c in comments or []:
        if ((c.get("user") or {}).get("login") or "").lower() != me:
            continue
        body = c.get("body") or ""
        m = _MARKER_RE.search(body)
        if m:
            b = _BUILD_RE.search(body)
            out[m.group(1)] = (c.get("id"), int(m.group(2) or 1), b.group(1) if b else None)
    return out


def ledger(rec: Dict[str, Any], compile_label: bool, build: Optional[str] = None) -> None:
    row = {"pr": rec["pr"], "head": rec["head"], "base": rec["base"], "at": rec["at"],
           "verdicts": {r["name"]: (r["answer"] or {}).get("verdict") for r in rec["reviews"]},
           "both_pass": both_pass(rec), "compile_label": compile_label, "build": build,
           **({"origin": rec["origin"]} if rec.get("origin") else {})}
    try:
        with open(os.path.join(_dir(), "ledger.jsonl"), "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass
