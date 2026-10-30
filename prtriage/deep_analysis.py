"""The deep layer's model chain and prompt version.

The deep layer asks a model whether a pull's diff caused its failures. These are the two facts a
cached deep verdict has to be keyed on: which model answers first, and which prompt it was given.

Extracted from the production tool; the definitions below are the real implementation.
"""
from __future__ import annotations

import os
from typing import List, Optional

from . import config


# Backend sentinels.
BACKEND_CLI = "claude-cli"


# 0.20.3: bump whenever _SYSTEM_PROMPT (or the context schema) changes
# materially - it's part of the cache key, so stale-prompt analyses
# auto-miss instead of being served against a new prompt's behavior.
# 0.26.0: bumped to "5" for the compile-step blame context + STRONG gate.
# 1.41.0: bumped to "6" for the failure_roster context + its prompt guidance.
_PROMPT_VERSION = "7"


_UNAVAILABLE: set = set()      # models refused this process: skip them for the next pulls


def model_chain(model: Optional[str] = None, backend: Optional[str] = None) -> List[str]:
    """2.279.1: the models to try, in order. An explicit model or PRTRIAGE_DEEP_MODEL (a comma
    list is allowed) replaces the configured chain. On the CLI, "" last means the CLI's own
    default, so a teammate without any listed model still gets a deep answer."""
    raw = model or os.environ.get(config.DEEP_ANALYSIS_MODEL_ENV) or ""
    chain = [m.strip() for m in raw.split(",") if m.strip()] or \
        list(config.DEEP_ANALYSIS_MODEL_FALLBACKS)
    if backend == BACKEND_CLI and "" not in chain:
        chain.append("")
    chain = [m for m in chain if m not in _UNAVAILABLE] or chain[-1:]
    return list(dict.fromkeys(chain))
