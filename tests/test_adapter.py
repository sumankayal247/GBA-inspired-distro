"""
Contract Tests for GBA OS mGBA Adapter (Rules H1, H5, SV2).
"""

import os
import sys
import unittest
import tempfile
import shutil

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
from gba_adapter_mgba import sync_directory

class TestAdapterContract(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.save_dir = os.path.join(self.test_dir, "saves", "entry1")
        os.makedirs(self.save_dir, exist_ok=True)

        # Create a sample save file
        self.save_file = os.path.join(self.save_dir, "game.sav")
        with open(self.save_file, "wb") as f:
            f.write(b"SAMPLE_SAVE_STATE_DATA")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sv2_sync_directory(self):
        """SV2: Adapter must fsync save files and save directory without error."""
        try:
            sync_directory(self.save_dir)
        except Exception as e:
            self.fail(f"sync_directory raised exception: {e}")

if __name__ == "__main__":
    unittest.main()
