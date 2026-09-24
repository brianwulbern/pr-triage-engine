#!/usr/bin/env python3
"""End-to-end demo on synthetic data: no network, no tokens, no private data.

Five open PRs go through the real pipeline: the history classifier, the verdict ladder, the
concurrent board, the GitHub governor, and the thread-aware comment engine, all talking to an
in-memory GitHub (prtriage.mocks). What to watch for:

  #101  clean run                                   -> forward-safe
  #102  only flakes and infrastructure noise        -> forward-safe (base noise)
  #103  compile break in an edited module, and a    -> do not land, and the comment LEADS with
        reviewer already offered a fix in the thread    the offered cherry-pick
  #104  a usually-reliable test failed in an        -> review: re-run at this commit (the missing
        edited module                                    evidence, not a maybe)
  #105  standing base failure in an edited module;  -> review (masked), and the re-post is an
        the bot already commented, the thread moved on,      in-place EDIT, not a duplicate comment
        only the behind-count drifted

Midway, code search runs out of its own small rate bucket. The governor cools only code search;
every other GitHub call keeps its normal pace.

    python3 demo.py
"""
from __future__ import annotations

import logging
import os

os.environ.setdefault("PRTRIAGE_NO_GH_CACHE", "1")             # never touch a disk cache
os.environ.setdefault("PRTRIAGE_NO_SECONDARY_LIMIT_LOG", "1")  # never write a throttle log

from prtriage import board, board_limits, classifier, comments, governor, http_cache  # noqa: E402
from prtriage.mocks import MockGitHub, synthetic_history                             # noqa: E402
from prtriage.verdict import GREEN, Failure, Run, decide, render_comment             # noqa: E402

TOKEN = "demo-token"
REPO = "https://github.com/example-org/product/pull/{}"
FAKE_FIX_SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"


def _fail(name, error, pass_rate, pr, history_error=None, **flags):
    """Classify one synthetic failure with the REAL classifier, then attach diff facts.
    `history_error` is how the test failed on OTHER PRs; left unset it matches `error`, i.e.
    the same failure keeps recurring (a flake or a standing failure)."""
    hist = synthetic_history(pass_rate, error=error if history_error is None else history_error)
    c = classifier.classify_failure(hist, error, 1, name, f"PR#{pr}")
    return Failure(name=name, error=error, category=c.category, is_unique=c.is_unique,
                   pass_rate=c.recent_pass_rate if c.recent_pass_rate is not None else c.pass_rate,
                   **flags)


def scenarios():
    connreset = "java.net.SocketException: Connection reset while calling the image service"
    return {
        101: Run(101, [], executed=1180, expected=1200, behind=12, authors=["dev-a"]),
        102: Run(102, [
            _fail("UploadRetryTest", "AssertionError: upload took longer than 30s",
                  70, 102, module="storage"),
            _fail("ThumbnailTest", connreset, 95, 102, module="media"),
        ], executed=1150, expected=1200, behind=40, authors=["dev-b"]),
        103: Run(103, [
            Failure("modules-compile[billing]", "error: cannot find symbol: InvoiceTotals",
                    category="critical_error", module="billing", diff_touches_module=True,
                    compile_break=True),
            *[Failure(f"Billing{n}Test", ":billing:test was not executed", category=None,
                      is_unique=False, module="billing", blocked=True)
              for n in ("Invoice", "Refund", "Tax")],
        ], executed=40, expected=1200, behind=8, authors=["dev-c", "dev-d"]),
        104: Run(104, [
            # usually passes, and its rare past failures were a different (timeout) error, so
            # this rounding failure is new: the classifier keeps it as a unique failure.
            _fail("CartCheckoutTest", "AssertionError: expected total 42.00 but was 41.99",
                  96, 104, history_error="TimeoutException: cart service did not respond",
                  module="cart", diff_touches_module=True),
        ], executed=1190, expected=1200, behind=30, authors=["dev-e"]),
        105: Run(105, [
            _fail("ReportExportTest", "AssertionError: export header row missing", 0, 105,
                  module="reports", diff_touches_module=True),
        ], executed=1195, expected=1200, behind=35, authors=["dev-f"]),
    }


def seed_github(gh: MockGitHub, runs) -> None:
    for n in runs:
        gh.add_pr(n, head_sha=f"{n:04x}" + "0" * 36, head_date="2098-12-31T10:00:00Z")
    # #103: a reviewer offered the fix AFTER the head commit, so the PR hasn't picked it up.
    gh.add_comment(103, "dev-g",
                   f"you need to cherry pick this commit to fix the build: "
                   f"https://github.com/dev-g/product/commit/{FAKE_FIX_SHA}",
                   "2098-12-31T12:00:00Z")
    # #105: the tool already posted this same verdict (behind 20), then the thread moved on.
    prior = render_comment(Run(105, runs[105].failures, runs[105].executed, runs[105].expected,
                               behind=20, authors=["dev-f"]), decide(runs[105]))
    gh.add_comment(105, "demo-bot", f"{comments._VERDICT_MARKER}\n{prior}", "2098-12-31T11:00:00Z")
    gh.add_comment(105, "dev-f", "thanks, looking into it", "2098-12-31T13:00:00Z")


def locate_source(filename: str):
    """Find a test's source via code search, the way relatedness scoring does when a test's
    module layout is unusual. Skips while the code-search bucket is cooling."""
    if governor.resource_cooling("code_search") > 0:
        return None
    resp = governor.governed(lambda: http_cache.gh_get(
        "https://api.github.com/search/code", params={"q": f"filename:{filename}"},
        headers={"Authorization": f"token {TOKEN}"}, timeout=10))
    if resp.status_code != 200:
        return None
    items = resp.json().get("items") or []
    return items[0]["path"] if items else None


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    # A real (enabled) governor with the sleep stubbed: the demo shows decisions, not waiting.
    governor._singleton = governor.GitHubGovernor(enabled=True, sleep=lambda s: None)
    runs = scenarios()
    gh = MockGitHub(code_search_per_minute=2)
    seed_github(gh, runs)

    with gh.install():
        board_limits._budget_preflight(TOKEN, len(runs))

        def triage_one(n):
            for f in runs[n].failures:
                if f.is_unique and not f.compile_break:
                    locate_source(f"{f.name}.java")
            v = decide(runs[n])
            return {"pr": n, "url": REPO.format(n), "verdict": v,
                    "body": render_comment(runs[n], v)}

        records, aborted = board.run_board(sorted(runs), triage_one, concurrency=3)
        # two more lookups: the bucket is exhausted by now, so these are skipped, not throttled
        locate_source("AnotherTest.java")
        locate_source("YetAnotherTest.java")
        print(board.render_board(records, aborted))

        s = governor.governor().snapshot()
        print("\n── governor after the board")
        print(f"   code search: {gh.search_used} request(s) against a {gh.search_limit}/min "
              f"bucket, now cooling {s['resource_cooling'].get('code_search', 0):.0f}s")
        print(f"   shared spacing: {s['spacing']}s (baseline {s['baseline']}s), global "
              f"rate-limit hits: {s['rate_limit_hits']}  -> the rest of GitHub never slowed")

        print("\n── posting verdicts (non-green only)")
        for r in records:
            if r["verdict"].orb == GREEN:
                continue
            pv = comments.preview_pr_verdict_action(r["url"], r["body"], TOKEN)
            res = comments.upsert_pr_verdict_comment(r["url"], r["body"], TOKEN)
            notes = []
            if pv.get("quiet"):
                notes.append("thread moved on, nothing new: edited in place")
            if pv.get("fixes"):
                notes.append("leads with the offered fix")
            first = (pv.get("body") or r["body"]).split("\n", 1)[0]
            print(f"   #{r['pr']}  {res.get('action', res.get('error'))}"
                  f"{'  (' + '; '.join(notes) + ')' if notes else ''}")
            print(f"         {first[:150]}{'...' if len(first) > 150 else ''}")

        dup = [c for c in gh.threads[105] if c["body"].startswith(comments._VERDICT_MARKER_PREFIX)]
        print(f"\n   #105 now has {len(dup)} verdict comment(s) (no duplicate), "
              f"#103's comment names the cherry-pick: "
              f"{'yes' if 'cherry-pick' in gh.threads[103][-1]['body'] else 'NO'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
