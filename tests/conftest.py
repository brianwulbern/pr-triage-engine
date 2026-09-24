"""Hermetic tests: no real pacing (no sleeps), no disk cache, no throttle log, a fresh governor
and an empty standing-failure registry for every test."""
import os
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _hermetic():
    from prtriage import governor, standing_registry
    standing_registry._reset_cache()
    governor._singleton = None          # rebuilt disabled under the env below
    governor.reset_concurrency()
    with mock.patch.dict(os.environ, {"PRTRIAGE_NO_GH_CACHE": "1",
                                      "PRTRIAGE_NO_GH_GOVERNOR": "1",
                                      "PRTRIAGE_NO_SECONDARY_LIMIT_LOG": "1"}, clear=False), \
         mock.patch.object(standing_registry, "load_registry", return_value={}):
        yield
    governor._singleton = None
