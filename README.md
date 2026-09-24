# PR triage engine

Turns a noisy CI run into one confident verdict on whether a pull request is ready to land, then
posts that verdict to the PR without spamming the thread. It triages a whole queue of open PRs
concurrently while staying inside GitHub's rate limits.

> This is a generalized, sanitized extract of an internal tool used to gate backport pull requests
> on a large product. The two external services it depends on (an internal test-reporting service
> and the organization's GitHub) are replaced here by small mocks in `prtriage/mocks.py`, so the
> demo runs offline. There is no real customer data, no credentials, and no internal references in
> this repository. The rate governor, the classifier, the comment engine, and the board-limit logic
> are the real implementation, extracted as-is; the verdict ladder is a condensed version of the
> production one.

## Why it exists

On a long-lived release branch, most CI runs are red, and most of that red has nothing to do with
the pull request: flaky tests, infrastructure hiccups, tests that are already failing on the base
branch. Reviewers end up either re-reading every failure by hand or learning to ignore red builds,
which is how a real break gets through. This engine does the reading. For each PR it separates
failures the change caused from failures it merely inherited, and says so in one line a reviewer
can act on.

## How it works

```
 CI results ─┐
             ├─▶ classifier ─▶ verdict ladder ─▶ comment engine ─▶ PR thread
 PR diff  ───┘   (history)      (one verdict)     (edit / new / skip)
                     ▲                                  ▲
                     └──────── GitHub governor ─────────┘
                        (pacing, buckets, breaker)
 board: runs the pipeline for many PRs at once and groups them by verdict
```

### Classifier (`prtriage/classifier.py`)

Looks at each failing test's recent history on other pull requests. A test that fails the same way
on most other PRs is standing on the base branch. One that flips between pass and fail is flaky.
One whose error matches a known infrastructure signature (a network reset, an HTML error page where
JSON was expected, a full disk) is environmental. What is left is genuinely new. Error matching is
fuzzy (normalized prefixes, then a similarity ratio), because the same failure rarely produces
byte-identical stack traces twice.

### Verdict ladder (`prtriage/verdict.py`)

Turns classified failures plus facts about the diff into one verdict, from most to least decisive:
a compile break in a module the change edits, a failure in code the change edits, a failure in an
edited module that usually passes (re-run to decide), base noise that sits in an edited module
(masking), thin coverage, then noise elsewhere. The rules it encodes:

* **Never call a real break green.** Calling a real break safe is the worst error; blaming the
  change for base noise is the second worst. When the evidence is short, the verdict holds.
* **A hold names what is missing.** "Not ready: this usually passes, so one red run can't tell a
  fresh break from a one-off. Re-run at this commit." Not "might be related".
* **Base noise can mask a real break.** A failure that is standing on the base does not implicate
  the change, unless it sits in a module the change edits, where it can hide one. That case never
  reads green.
* **Zero failures over a run that barely executed proves nothing.**

### GitHub governor (`prtriage/governor.py`)

Every GitHub call goes through one process-wide governor:

* **Adaptive pacing.** A small baseline spacing between request starts. Each rate-limit response
  widens it (multiplicatively) and a streak of clean responses narrows it back (additively).
  Retry-After is honored.
* **Concurrency cap.** A semaphore bounds in-flight GitHub requests independently of the pacing,
  because GitHub's secondary limit trips on simultaneity, not just rate.
* **Circuit breaker.** After enough consecutive throttled responses it stops pacing and fails fast,
  so a run that has hit a hard wall ends in seconds instead of grinding for an hour.
* **Isolated buckets.** GitHub meters code search in its own tiny bucket (10 requests a minute),
  separate from the main budget. Exhausting it says nothing about the rest of GitHub, so it cools
  only that bucket until its reset time. (This was a real bug: code-search exhaustion used to widen
  the spacing for every call and abort whole boards while most of the budget sat unused.)
* **Telemetry.** Every throttle is appended to a local log with the endpoint and bucket, because
  the secondary limit is invisible to GitHub's own rate-limit endpoint. Read the log before
  theorizing about why a run slowed down.

### Comment engine (`prtriage/comments.py`)

One self-updating comment per PR, identified by a hidden marker and carrying a hidden signature of
the conclusion:

* **Same conclusion:** edit the existing comment in place, no new notification.
* **Changed conclusion:** post a new comment and leave the old one as a paper trail.
* **Same conclusion, but the thread moved on:** post fresh at the bottom so the verdict isn't
  buried, but only if there is something new to say. A number that drifts on its own (how far
  behind base the branch is) is not news unless it crosses into rebase-needed territory.
* **Reads the thread first.** If a human already offered a fix (a commit link or a sha to
  cherry-pick) that the PR hasn't picked up, the post leads with that instead of telling the
  authors to fix the PR as if nobody had helped.
* **Never duplicates on a bad read.** If listing the existing comments fails (a rate limit, a
  network error), it skips the post rather than creating blind. It never posts to a closed PR.

### Board (`prtriage/board.py`, `prtriage/board_limits.py`)

Triages many PRs concurrently and groups them by verdict. Before starting it probes the budget it
can see (the free rate-limit endpoint) and the one it can't (a single cheap authenticated call that
reveals an active secondary limit). If the governor falls into a crawl mid-run, the board stops
with a partial result instead of grinding, but only when enough PRs remain for grinding to be the
worse option: a short tail always finishes.

## Try the demo

```bash
python3 -m pip install -e ".[test]"
python3 demo.py        # five synthetic PRs through the whole pipeline, offline
python3 -m pytest -q   # the test suite
```

The demo triages five PRs against an in-memory GitHub: a clean run, one with only flaky and
infrastructure noise, a compile break where a reviewer has already offered the fix in the thread,
a usually-reliable failure that needs a re-run, and a masked failure whose earlier verdict comment
gets edited in place instead of duplicated. Partway through, code search runs out of its own
bucket; the governor summary at the end shows the rest of GitHub never slowed down.

## Layout

```
prtriage/
  governor.py          rate governor: pacing, concurrency cap, breaker, isolated buckets
  http_cache.py        pooled GitHub HTTP + ETag conditional GETs
  classifier.py        history-based failure classification
  baseline.py          rolling failure baseline (is the environment noisier than usual?)
  standing_registry.py failures seen across many PRs (standing on base)
  verdict.py           condensed verdict ladder + comment rendering
  comments.py          idempotent, thread-aware verdict comments
  board.py             concurrent readiness board
  board_limits.py      rate-limit preflight and early-abort decisions
  config.py            tunables
  mocks.py             synthetic history + in-memory GitHub
demo.py                end-to-end demo
tests/                 the extracted production tests plus tests for the condensed parts
```

## License

MIT. See `LICENSE`.
