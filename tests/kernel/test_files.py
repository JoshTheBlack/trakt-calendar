"""Writing a file another thread may be reading (app/files.py's `write_whole`):
the file is the old one or the new one, never part of either, and nothing is
left beside it."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import files
from app.config import DATA_DIR


class AFileIsWrittenWholeTests(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=DATA_DIR))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = self.dir / "deep" / "card.jpg"

    def test_a_new_file_and_its_folder_are_made(self):
        files.write_whole(self.path, b"new")
        self.assertEqual(self.path.read_bytes(), b"new")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["card.jpg"])

    def test_a_write_replaces_the_old_file(self):
        files.write_whole(self.path, b"old")
        files.write_whole(self.path, b"new")
        self.assertEqual(self.path.read_bytes(), b"new")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["card.jpg"])

    def test_a_write_that_fails_leaves_the_old_file_and_no_spare(self):
        files.write_whole(self.path, b"old")
        with patch.object(files.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                files.write_whole(self.path, b"new")
        self.assertEqual(self.path.read_bytes(), b"old")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["card.jpg"])

    def test_a_reader_holding_the_file_on_windows_is_waited_for(self):
        """Windows refuses to replace a file another thread has open; the move
        is tried again for a moment, and lands once the reader lets go."""
        real = files.os.replace
        refusals = iter([PermissionError("in use"), PermissionError("in use")])

        def busy_then_free(src, dst):
            for refusal in refusals:
                raise refusal
            real(src, dst)

        with patch.object(files.sys, "platform", "win32"), \
                patch.object(files.time, "sleep"), \
                patch.object(files.os, "replace", side_effect=busy_then_free):
            files.write_whole(self.path, b"new")
        self.assertEqual(self.path.read_bytes(), b"new")

    def test_elsewhere_a_refused_replace_is_an_error_at_once(self):
        with patch.object(files.sys, "platform", "linux"), \
                patch.object(files.os, "replace", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                files.write_whole(self.path, b"new")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
