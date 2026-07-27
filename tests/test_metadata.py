from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.metadata import collect_source_metadata


class MetadataTests(unittest.TestCase):
    def test_explicit_source_identity_does_not_require_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metadata = collect_source_metadata(
                Path(directory),
                source_commit="abc123",
                source_branch="merge-pda-3.1.x",
            )
            assert metadata is not None
            self.assertEqual(metadata["commit"], "abc123")
            self.assertEqual(metadata["branch"], "merge-pda-3.1.x")
            self.assertIsNone(metadata["dirty"])

    def test_missing_worktree_git_metadata_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metadata = collect_source_metadata(Path(directory))
            assert metadata is not None
            self.assertIn("git_error", metadata)


if __name__ == "__main__":
    unittest.main()
