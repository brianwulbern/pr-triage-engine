"""The demo is part of the contract: it must run offline and show what it claims."""
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Demo(unittest.TestCase):
    def test_demo_runs_and_shows_the_design(self):
        import demo
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(demo.main(), 0)
        out = buf.getvalue()
        self.assertIn("🛑 DO NOT LAND (1)", out)
        self.assertIn("🟢 FORWARD-SAFE (2)", out)
        self.assertIn("global rate-limit hits: 0", out)          # code search stayed isolated
        self.assertIn("leads with the offered fix", out)          # #103 answers the thread
        self.assertIn("edited in place", out)                     # #105 no duplicate
        self.assertIn("#105 now has 1 verdict comment(s)", out)
        self.assertIn("names the cherry-pick: yes", out)


if __name__ == "__main__":
    unittest.main()
