"""The condensed verdict ladder keeps the production asymmetry: never call a real break green,
hold with a named missing piece of evidence instead of guessing."""
import unittest

from prtriage import config
from prtriage.verdict import (GREEN, INVESTIGATE, LEAN_OK, RED, REVIEW, Failure, Run, decide,
                              render_comment)


def F(name="T", **kw):
    return Failure(name=name, **kw)


class Ladder(unittest.TestCase):
    def test_compile_break_in_edited_module_blocks(self):
        v = decide(Run(1, [F("modules-compile[x]", compile_break=True, diff_touches_module=True,
                             module="x"), F("XTest", is_unique=False, blocked=True)]))
        self.assertEqual((v.orb, v.code), (RED, "ci_compile_break"))
        self.assertIn("1 downstream test(s) never ran", v.headline)

    def test_compile_break_elsewhere_is_not_attributed_to_the_change(self):
        v = decide(Run(1, [F("modules-compile[y]", compile_break=True, is_unique=False,
                             category="consistent_failure")]))
        self.assertNotEqual(v.orb, RED)

    def test_failure_in_edited_test_is_attributed(self):
        v = decide(Run(1, [F("FooTest", diff_touches_test=True)]))
        self.assertEqual((v.orb, v.code), (RED, "diff_attributed"))

    def test_usually_reliable_failure_in_edited_module_asks_for_a_rerun(self):
        v = decide(Run(1, [F("FooTest", diff_touches_module=True,
                             pass_rate=config.RELIABLE_PASS_RATE + 1)]))
        self.assertEqual((v.orb, v.code), (REVIEW, "rerun_to_decide"))
        self.assertIn("re-run at this commit", v.headline)
        self.assertNotIn("might", v.headline)          # confident about what is missing

    def test_unreliable_failure_in_edited_module_is_investigate(self):
        v = decide(Run(1, [F("FooTest", diff_touches_module=True, pass_rate=40)]))
        self.assertEqual(v.orb, INVESTIGATE)

    def test_base_noise_in_an_edited_module_masks_and_is_never_green(self):
        v = decide(Run(1, [F("FooTest", is_unique=False, category="consistent_failure",
                             diff_touches_module=True)]))
        self.assertEqual((v.orb, v.code), (REVIEW, "masked_by_base"))

    def test_base_noise_elsewhere_is_forward_safe(self):
        v = decide(Run(1, [F("FooTest", is_unique=False, category="known_flake")],
                       executed=100, expected=100))
        self.assertEqual((v.orb, v.code), (GREEN, "base_noise"))

    def test_thin_coverage_is_not_evidence_of_safety(self):
        v = decide(Run(1, [], executed=10, expected=100))
        self.assertEqual((v.orb, v.code), (REVIEW, "inconclusive_coverage"))

    def test_new_failure_outside_the_change_leans_not_this_pr(self):
        v = decide(Run(1, [F("FarAwayTest")], executed=100, expected=100))
        self.assertEqual(v.orb, LEAN_OK)

    def test_clean(self):
        self.assertEqual(decide(Run(1, [], executed=5, expected=5)).code, "clean")


class Render(unittest.TestCase):
    def test_greets_only_when_the_verdict_asks_something(self):
        red = render_comment(Run(7, [F("A", diff_touches_test=True)], authors=["dev-a"]),
                             decide(Run(7, [F("A", diff_touches_test=True)])))
        self.assertTrue(red.startswith("Hi @dev-a, "))
        green = render_comment(Run(7, [], authors=["dev-a"]), decide(Run(7, [])))
        self.assertFalse(green.startswith("Hi "))

    def test_signature_and_behind_line_are_present(self):
        run = Run(7, [], behind=12)
        body = render_comment(run, decide(run))
        self.assertIn("<!-- prtriage-sig: 🟢:clean -->", body)
        self.assertIn("Branch is 12 commit(s) behind base", body)

    def test_published_text_has_no_em_dash(self):
        run = Run(7, [F("A", diff_touches_test=True)], authors=["dev-a"], behind=500)
        self.assertNotIn(chr(0x2014), render_comment(run, decide(run)))


if __name__ == "__main__":
    unittest.main()
