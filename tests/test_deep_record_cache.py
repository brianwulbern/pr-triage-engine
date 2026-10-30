import copy
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from prtriage import config, deep_analysis, deep_record_cache as D

URL = "https://github.com/example-org/product/pull/1234"
FAIL = {"name": "formFields.spec.ts > rich text", "relatedness_level": "NONE",
        "diff_source_file": None, "is_critical": False, "task_not_executed": False,
        "error_digest": "abc", "fails_in_reference_release": None, "never_ran_standing": None,
        "source_triggers": ["x"]}
NODEEP = {"_audit": {"target": URL, "head_sha": "a" * 40},
          "result": {"verdict": {"orb": "⚠️", "action": "review",
                                 "reason_code": "characterized_review", "unique_count": 1},
                     "unioned_failures": [FAIL]}}


def _deep(orb="🟢", code="deep_de_escalate", model="m1", head="a" * 40, **verdict):
    return {"_audit": {"target": URL, "head_sha": head},
            "result": {"verdict": {"orb": orb, "reason_code": code, "action": "forward",
                                   **verdict},
                       "deep_analysis": {"model": model}, "unioned_failures": [FAIL]}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp.name,
                                                "PRTRIAGE_DEEP_CACHE_SERVE": ""})
        self.env.start()
        self.chain = mock.patch.object(deep_analysis, "model_chain", return_value=["m1", "m2"])
        self.chain.start()

    def tearDown(self):
        self.chain.stop()
        self.env.stop()
        self.tmp.cleanup()

    def key(self, nodeep=NODEEP, head="a" * 40, builds=("557",), signed=False):
        return D.key(head, list(builds), signed, D.gate_digest(nodeep["result"]))

    def shadow_rows(self):
        p = os.path.join(self.tmp.name, "prtriage", "deep-cache-shadow.jsonl")
        return [json.loads(l) for l in open(p, encoding="utf-8")] if os.path.exists(p) else []


class KeyAndGates(Base):
    def test_hit_on_the_same_key(self):
        k = self.key()
        D.store("o/r", "1", k, _deep())
        self.assertEqual(D.lookup("o/r", "1", self.key())["result"]["verdict"]["orb"], "🟢")

    def test_each_gate_input_flipped_alone_misses(self):
        D.store("o/r", "1", self.key(), _deep())
        flips = {"relatedness_level": "MODULE", "diff_source_file": "A.java", "is_critical": True,
                 "task_not_executed": True, "error_digest": "def", "name": "other",
                 "fails_in_reference_release": True, "never_ran_standing": {"runs": 4}}
        for field, value in flips.items():
            nd = copy.deepcopy(NODEEP)
            nd["result"]["unioned_failures"][0][field] = value
            self.assertIsNone(D.lookup("o/r", "1", self.key(nd)), field)
        for field, value in {"reason_code": "base_standing", "orb": "🔴",
                             "unique_count": 2, "action": "hold"}.items():
            nd = copy.deepcopy(NODEEP)
            nd["result"]["verdict"][field] = value
            self.assertIsNone(D.lookup("o/r", "1", self.key(nd)), field)
        nd = copy.deepcopy(NODEEP)
        nd["result"]["unioned_failures"].append(dict(FAIL, name="second"))
        self.assertIsNone(D.lookup("o/r", "1", self.key(nd)))

    def test_head_builds_sign_off_model_prompt_and_logic_miss(self):
        D.store("o/r", "1", self.key(), _deep())
        self.assertIsNone(D.lookup("o/r", "1", self.key(head="b" * 40)))
        self.assertIsNone(D.lookup("o/r", "1", self.key(builds=("557", "558"))))
        self.assertIsNone(D.lookup("o/r", "1", self.key(signed=True)))
        with mock.patch.object(deep_analysis, "model_chain", return_value=["m2"]):
            self.assertIsNone(D.lookup("o/r", "1", self.key()))
        with mock.patch.object(deep_analysis, "_PROMPT_VERSION", "8"):
            self.assertIsNone(D.lookup("o/r", "1", self.key()))
        with mock.patch.object(config, "VERDICT_LOGIC", config.VERDICT_LOGIC + 1):
            self.assertIsNone(D.lookup("o/r", "1", self.key()))

    def test_a_green_still_ages_out(self):
        D.store("o/r", "1", self.key(), _deep())
        later = time.time() + config.PULLS_CACHE_TTL_SECONDS + 5
        self.assertIsNone(D.lookup("o/r", "1", self.key(), now=later))

    def test_only_complete_primary_model_records_are_storable(self):
        k = self.key()
        self.assertTrue(D.storable(_deep(), k))
        self.assertFalse(D.storable(_deep(model="m2"), k))
        self.assertFalse(D.storable(_deep(model=None), k))
        self.assertFalse(D.storable(_deep(incomplete=True), k))
        err = _deep()
        err["_audit"]["error"] = "boom"
        self.assertFalse(D.storable(err, k))
        self.assertFalse(D.storable(_deep(), None))

    def test_no_key_without_head_builds_or_digest(self):
        self.assertIsNone(D.key(None, ["1"], False, "d"))
        self.assertIsNone(D.key("a" * 40, [], False, "d"))
        self.assertIsNone(D.key("a" * 40, ["1"], False, None))
        self.assertIsNone(D.gate_digest({"unioned_failures": []}))

    def test_shadow_flags_a_greener_cache(self):
        D.shadow(URL, _deep(), _deep(orb="⚠️", code="characterized_review"))
        D.shadow(URL, None, _deep())
        a, b = self.shadow_rows()
        self.assertEqual((a["hit"], a["match"], a["greener"]), (True, False, True))
        self.assertEqual((b["hit"], b["match"], b["greener"]), (False, None, False))




if __name__ == "__main__":
    unittest.main()
