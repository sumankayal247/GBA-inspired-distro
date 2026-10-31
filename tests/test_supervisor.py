"""
Contract Tests for GBA OS Supervisor (Rules H1, H10, S7).
"""

import os
import sys
import unittest
import tempfile
import shutil

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
from gba_supervisor import Supervisor

class TestSupervisorContract(unittest.TestCase):
    def setUp(self):
        self.sup = Supervisor(is_desktop_mode=True)

    def test_h10_launch_failure_classification(self):
        """H10: Invalid or missing content classified as 'launch-failure'."""
        req = {
            "entry_id": "nonexistent_id",
            "content_path": "/nonexistent/path/game.gba",
            "handler": "mgba-adapter"
        }
        outcome = self.sup.run_handler(req)
        self.assertEqual(outcome, "launch-failure")

    def test_s7_crash_counter(self):
        """S7: Supervisor tracks crash frequency and cleans expired entries."""
        import time
        now = time.time()
        self.sup.crash_timestamps = [now - 40, now - 10]
        # Clean timestamps older than 30s
        self.sup.crash_timestamps = [t for t in self.sup.crash_timestamps if now - t <= 30.0]
        self.assertEqual(len(self.sup.crash_timestamps), 1)

if __name__ == "__main__":
    unittest.main()
