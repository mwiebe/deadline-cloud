# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for compute_diff_manifest and related functions.

These tests cover:
- Basic diff computation for unified manifest classes
- New file detection
- Modified file detection (by hash comparison)
- Deleted file detection with deletion markers
- Directory change detection
- Symlink change detection
- Parent manifest hash computation
- Path type validation (absolute vs relative)
- ignore_hashes mode for fast diff
- preserve_runnable mode for Windows compatibility

Note: The diff operation is a pure comparison - it does NOT compute hashes.
Both input manifests must already have hashes computed via hash_manifest().
"""

import pytest
from typing import List

from deadline.job_attachments._snapshots import (
    compute_diff_manifest,
    filter_manifest,
    IncludeExcludePathsFilter,
)
from deadline.job_attachments._snapshots._operations._diff_manifest import (
    _entries_differ,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments._snapshots import (
    ManifestFilePath,
    ManifestDirectoryPath,
    AbsSnapshot,
    AbsSnapshotDiff,
    Snapshot,
    SnapshotDiff,
)


class TestComputeDiffManifestAbsSnapshot:
    """Tests for diff computation with AbsSnapshot."""

    def _create_manifest(self, files: List[dict], dirs: List[dict] | None = None) -> AbsSnapshot:
        """Helper to create an AbsSnapshot."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            files=file_entries,
            total_size=total_size,
        )

    def test_no_changes_returns_empty_diff(self) -> None:
        """No changes returns diff manifest with no entries."""
        parent = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )

        diff = compute_diff_manifest(parent, current)

        assert isinstance(diff, AbsSnapshotDiff)
        assert len(diff.files) == 0
        assert len(diff.dirs) == 0

    def test_new_file_detected(self) -> None:
        """New file in current is included in diff."""
        parent = self._create_manifest(
            [{"path": "/old.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_manifest(
            [
                {"path": "/old.txt", "hash": "hash1", "size": 100, "mtime": 1000},
                {"path": "/new.txt", "hash": "hash2", "size": 50, "mtime": 2000},
            ]
        )

        diff = compute_diff_manifest(parent, current)

        new_entry = next((p for p in diff.files if p.path == "/new.txt"), None)
        assert new_entry is not None
        assert new_entry.hash == "hash2"
        assert not new_entry.deleted

    def test_modified_file_detected(self) -> None:
        """Modified file (different hash) is detected."""
        parent = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash2", "size": 100, "mtime": 2000}]
        )

        diff = compute_diff_manifest(parent, current)

        assert len(diff.files) == 1
        assert diff.files[0].path == "/file.txt"
        assert diff.files[0].hash == "hash2"

    def test_same_hash_different_mtime_is_modified(self) -> None:
        """Same hash but different mtime IS considered modified."""
        parent = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 2000}]
        )

        diff = compute_diff_manifest(parent, current)

        assert len(diff.files) == 1
        assert diff.files[0].path == "/file.txt"
        assert diff.files[0].mtime == 2000

    def test_same_hash_different_size_is_modified(self) -> None:
        """Same hash but different size IS considered modified."""
        parent = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 200, "mtime": 1000}]
        )

        diff = compute_diff_manifest(parent, current)

        assert len(diff.files) == 1
        assert diff.files[0].path == "/file.txt"
        assert diff.files[0].size == 200

    def test_deleted_file_has_marker(self) -> None:
        """Deleted file has deleted=True marker in diff."""
        parent = self._create_manifest(
            [
                {"path": "/keep.txt", "hash": "hash1", "size": 100, "mtime": 1000},
                {"path": "/delete.txt", "hash": "hash2", "size": 200, "mtime": 2000},
            ]
        )
        current = self._create_manifest(
            [{"path": "/keep.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )

        diff = compute_diff_manifest(parent, current)

        deleted_entry = next((p for p in diff.files if p.path == "/delete.txt"), None)
        assert deleted_entry is not None
        assert deleted_entry.deleted is True

    def test_unchanged_file_not_in_diff(self) -> None:
        """Unchanged file is not included in diff."""
        parent = self._create_manifest(
            [
                {"path": "/unchanged.txt", "hash": "hash1", "size": 100, "mtime": 1000},
                {"path": "/changed.txt", "hash": "hash2", "size": 200, "mtime": 2000},
            ]
        )
        current = self._create_manifest(
            [
                {"path": "/unchanged.txt", "hash": "hash1", "size": 100, "mtime": 1000},
                {"path": "/changed.txt", "hash": "hash3", "size": 200, "mtime": 3000},
            ]
        )

        diff = compute_diff_manifest(parent, current)

        paths = {p.path for p in diff.files}
        assert "/unchanged.txt" not in paths
        assert "/changed.txt" in paths

    def test_parent_manifest_hash_stored(self) -> None:
        """parentManifestHash is stored when provided."""
        parent = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        parent_hash = "abc123def456"

        diff = compute_diff_manifest(parent, current, parent_manifest_hash=parent_hash)

        assert diff.parentManifestHash == parent_hash

    def test_parent_manifest_hash_optional(self) -> None:
        """parentManifestHash is optional."""
        parent = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )

        diff = compute_diff_manifest(parent, current)

        assert diff.parentManifestHash is None

    def test_new_directory_included(self) -> None:
        """New directory is included in diff."""
        parent = self._create_manifest(files=[], dirs=[{"path": "/old_dir"}])
        current = self._create_manifest(files=[], dirs=[{"path": "/old_dir"}, {"path": "/new_dir"}])

        diff = compute_diff_manifest(parent, current)

        dir_paths = {d.path for d in diff.dirs}
        assert "/new_dir" in dir_paths

    def test_deleted_directory_has_marker(self) -> None:
        """Deleted directory has deleted=True marker."""
        parent = self._create_manifest(
            files=[], dirs=[{"path": "/keep_dir"}, {"path": "/delete_dir"}]
        )
        current = self._create_manifest(files=[], dirs=[{"path": "/keep_dir"}])

        diff = compute_diff_manifest(parent, current)

        deleted_dir = next((d for d in diff.dirs if d.path == "/delete_dir"), None)
        assert deleted_dir is not None
        assert deleted_dir.deleted is True

    def test_symlink_change_detected(self) -> None:
        """Changed symlink target is detected."""
        parent = self._create_manifest([{"path": "/link.txt", "symlink_target": "/old_target.txt"}])
        current = self._create_manifest(
            [{"path": "/link.txt", "symlink_target": "/new_target.txt"}]
        )

        diff = compute_diff_manifest(parent, current)

        assert len(diff.files) == 1
        assert diff.files[0].path == "/link.txt"
        assert diff.files[0].symlink_target == "/new_target.txt"

    def test_new_symlink_included(self) -> None:
        """New symlink is included in diff."""
        parent = self._create_manifest(files=[])
        current = self._create_manifest([{"path": "/link.txt", "symlink_target": "/target.txt"}])

        diff = compute_diff_manifest(parent, current)

        assert len(diff.files) == 1
        assert diff.files[0].symlink_target == "/target.txt"

    def test_deleted_symlink_has_marker(self) -> None:
        """Deleted symlink has deleted=True marker."""
        parent = self._create_manifest([{"path": "/link.txt", "symlink_target": "/target.txt"}])
        current = self._create_manifest(files=[])

        diff = compute_diff_manifest(parent, current)

        assert len(diff.files) == 1
        assert diff.files[0].path == "/link.txt"
        assert diff.files[0].deleted is True

    def test_preserves_runnable_flag(self) -> None:
        """Runnable flag is preserved for changed files."""
        parent = self._create_manifest(files=[])
        current = self._create_manifest(
            [{"path": "/script.sh", "hash": "hash1", "size": 100, "mtime": 1000, "runnable": True}]
        )

        diff = compute_diff_manifest(parent, current)

        assert diff.files[0].runnable is True

    def test_preserves_chunkhashes(self) -> None:
        """Chunkhashes are preserved for large files."""
        parent = self._create_manifest(files=[])
        current = self._create_manifest(
            [
                {
                    "path": "/large.bin",
                    "chunkhashes": ["chunk1", "chunk2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                }
            ]
        )

        diff = compute_diff_manifest(parent, current)

        assert diff.files[0].chunkhashes == ["chunk1", "chunk2"]

    def test_chunked_file_modification_detected(self) -> None:
        """Modified chunked file (different chunkhashes) is detected."""
        parent = self._create_manifest(
            [
                {
                    "path": "/large.bin",
                    "chunkhashes": ["chunk1", "chunk2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                }
            ]
        )
        current = self._create_manifest(
            [
                {
                    "path": "/large.bin",
                    "chunkhashes": ["chunk1", "chunk3"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 2000,
                }
            ]
        )

        diff = compute_diff_manifest(parent, current)

        assert len(diff.files) == 1
        assert diff.files[0].chunkhashes == ["chunk1", "chunk3"]

    def test_total_size_calculated(self) -> None:
        """Total size is sum of non-deleted, non-symlink entries."""
        parent = self._create_manifest(
            [{"path": "/delete.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )
        current = self._create_manifest(
            [{"path": "/new.txt", "hash": "h2", "size": 50, "mtime": 2000}]
        )

        diff = compute_diff_manifest(parent, current)

        assert diff.totalSize == 50

    def test_symlinks_not_counted_in_total_size(self) -> None:
        """Symlinks don't contribute to total size."""
        parent = self._create_manifest(files=[])
        current = self._create_manifest(
            [
                {"path": "/file.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "/link.txt", "symlink_target": "/file.txt"},
            ]
        )

        diff = compute_diff_manifest(parent, current)

        assert diff.totalSize == 100

    def test_returns_abs_diff_manifest(self) -> None:
        """Diff of AbsSnapshots returns AbsSnapshotDiff."""
        parent = self._create_manifest(
            [{"path": "/a.txt", "hash": "h1", "size": 10, "mtime": 1000}]
        )
        current = self._create_manifest(
            [{"path": "/a.txt", "hash": "h1", "size": 10, "mtime": 1000}]
        )

        diff = compute_diff_manifest(parent, current)

        assert isinstance(diff, AbsSnapshotDiff)


class TestComputeDiffManifestRelSnapshot:
    """Tests for diff computation with Snapshot."""

    def _create_manifest(self, files: List[dict], dirs: List[dict] | None = None) -> Snapshot:
        """Helper to create a Snapshot."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            files=file_entries,
            total_size=total_size,
        )

    def test_no_changes_returns_empty_diff(self) -> None:
        """No changes returns diff manifest with no entries."""
        parent = self._create_manifest(
            [{"path": "file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_manifest(
            [{"path": "file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )

        diff = compute_diff_manifest(parent, current)

        assert isinstance(diff, SnapshotDiff)
        assert len(diff.files) == 0
        assert len(diff.dirs) == 0

    def test_new_file_detected(self) -> None:
        """New file in current is included in diff."""
        parent = self._create_manifest(
            [{"path": "old.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_manifest(
            [
                {"path": "old.txt", "hash": "hash1", "size": 100, "mtime": 1000},
                {"path": "new.txt", "hash": "hash2", "size": 50, "mtime": 2000},
            ]
        )

        diff = compute_diff_manifest(parent, current)

        new_entry = next((p for p in diff.files if p.path == "new.txt"), None)
        assert new_entry is not None
        assert new_entry.hash == "hash2"

    def test_deleted_file_has_marker(self) -> None:
        """Deleted file has deleted=True marker in diff."""
        parent = self._create_manifest(
            [
                {"path": "keep.txt", "hash": "hash1", "size": 100, "mtime": 1000},
                {"path": "delete.txt", "hash": "hash2", "size": 200, "mtime": 2000},
            ]
        )
        current = self._create_manifest(
            [{"path": "keep.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )

        diff = compute_diff_manifest(parent, current)

        deleted_entry = next((p for p in diff.files if p.path == "delete.txt"), None)
        assert deleted_entry is not None
        assert deleted_entry.deleted is True

    def test_returns_rel_diff_manifest(self) -> None:
        """Diff of Snapshots returns SnapshotDiff."""
        parent = self._create_manifest([{"path": "a.txt", "hash": "h1", "size": 10, "mtime": 1000}])
        current = self._create_manifest(
            [{"path": "a.txt", "hash": "h1", "size": 10, "mtime": 1000}]
        )

        diff = compute_diff_manifest(parent, current)

        assert isinstance(diff, SnapshotDiff)


class TestComputeDiffManifestValidation:
    """Tests for validation and error handling."""

    def test_path_type_mismatch_raises_error(self) -> None:
        """Mismatched path types (abs vs rel) raise ValueError."""
        parent = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[],
            total_size=0,
        )
        current = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[],
            total_size=0,
        )

        with pytest.raises(ValueError, match="same path type"):
            compute_diff_manifest(parent, current)

        with pytest.raises(ValueError, match="same path type"):
            compute_diff_manifest(parent, current)


class TestComputeDiffWithFilter:
    """Tests for diff computation with filtering.

    These tests verify the critical requirement that both parent and current
    must be filtered with the same filter for correct diff computation.
    """

    def test_filter_both_for_correct_deletions(self) -> None:
        """Filtering both manifests gives correct deletion detection.

        Scenario:
        - Parent has: [model.blend, texture.png]
        - Current has: [model.blend, new.blend]
        - Filter: *.blend

        Without filtering parent, texture.png would incorrectly appear as deleted.
        With filtering both, only new.blend appears as added.
        """
        parent = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(path="/model.blend", hash="h1", size=100, mtime=1000),
                ManifestFilePath(path="/texture.png", hash="h2", size=200, mtime=2000),
            ],
            total_size=300,
        )
        current = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(path="/model.blend", hash="h1", size=100, mtime=1000),
                ManifestFilePath(path="/new.blend", hash="h3", size=150, mtime=3000),
            ],
            total_size=250,
        )

        # Filter BOTH with same filter
        # Note: filter_manifest preserves the manifest type, so we cast back
        filter_obj = IncludeExcludePathsFilter(include=["*.blend"])
        filtered_parent = filter_manifest(parent, filter_obj)
        filtered_current = filter_manifest(current, filter_obj)
        assert isinstance(filtered_parent, AbsSnapshot)
        assert isinstance(filtered_current, AbsSnapshot)

        diff = compute_diff_manifest(filtered_parent, filtered_current)

        # Should only have new.blend as added, no deletions
        paths = {p.path for p in diff.files}
        assert "/new.blend" in paths
        assert "/texture.png" not in paths

        # Verify no deletion markers
        deleted = [p for p in diff.files if p.deleted]
        assert len(deleted) == 0


class TestIgnoreHashesMode:
    """Tests for ignore_hashes mode (fast diff without hash comparison)."""

    def _create_manifest(self, files: List[dict]) -> AbsSnapshot:
        """Helper to create an AbsSnapshot."""
        file_entries = [ManifestFilePath(**f) for f in files]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=file_entries,
            total_size=total_size,
        )

    def test_same_metadata_not_modified_with_ignore_hashes(self) -> None:
        """Same metadata means not modified when ignore_hashes=True."""
        parent = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash2", "size": 100, "mtime": 1000}]
        )

        diff = compute_diff_manifest(parent, current, ignore_hashes=True)

        # Different hash but same metadata = not modified when ignore_hashes=True
        assert len(diff.files) == 0

    def test_different_mtime_is_modified_with_ignore_hashes(self) -> None:
        """Different mtime is still detected when ignore_hashes=True."""
        parent = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 2000}]
        )

        diff = compute_diff_manifest(parent, current, ignore_hashes=True)

        assert len(diff.files) == 1
        assert diff.files[0].path == "/file.txt"

    def test_different_size_is_modified_with_ignore_hashes(self) -> None:
        """Different size is still detected when ignore_hashes=True."""
        parent = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_manifest(
            [{"path": "/file.txt", "hash": "hash1", "size": 200, "mtime": 1000}]
        )

        diff = compute_diff_manifest(parent, current, ignore_hashes=True)

        assert len(diff.files) == 1
        assert diff.files[0].path == "/file.txt"


class TestPreserveRunnableMode:
    """Tests for preserve_runnable mode (Windows compatibility)."""

    def _create_manifest(self, files: List[dict]) -> AbsSnapshot:
        """Helper to create an AbsSnapshot."""
        file_entries = [ManifestFilePath(**f) for f in files]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=file_entries,
            total_size=total_size,
        )

    def test_preserve_runnable_copies_from_parent(self) -> None:
        """preserve_runnable copies runnable from parent for modified files."""
        parent = self._create_manifest(
            [{"path": "/script.sh", "hash": "hash1", "size": 100, "mtime": 1000, "runnable": True}]
        )
        current = self._create_manifest(
            [{"path": "/script.sh", "hash": "hash2", "size": 100, "mtime": 2000, "runnable": False}]
        )

        diff = compute_diff_manifest(parent, current, preserve_runnable=True)

        assert len(diff.files) == 1
        assert diff.files[0].runnable is True  # Preserved from parent

    def test_preserve_runnable_does_not_affect_new_files(self) -> None:
        """preserve_runnable doesn't affect new files (no parent entry)."""
        parent = self._create_manifest(files=[])
        current = self._create_manifest(
            [{"path": "/script.sh", "hash": "hash1", "size": 100, "mtime": 1000, "runnable": True}]
        )

        diff = compute_diff_manifest(parent, current, preserve_runnable=True)

        assert len(diff.files) == 1
        assert diff.files[0].runnable is True  # From current (no parent)


class TestEntriesDiffer:
    """Tests for _entries_differ helper."""

    # === Regular file comparisons ===

    def test_same_hash_not_different(self) -> None:
        """Same hash means entries don't differ."""
        e1 = ManifestFilePath(path="/f.txt", hash="abc123", size=10, mtime=1000)
        e2 = ManifestFilePath(path="/f.txt", hash="abc123", size=10, mtime=1000)
        assert _entries_differ(e1, e2) is False

    def test_different_hash_is_different(self) -> None:
        """Different hash means entries differ."""
        e1 = ManifestFilePath(path="/f.txt", hash="abc123", size=10, mtime=1000)
        e2 = ManifestFilePath(path="/f.txt", hash="def456", size=10, mtime=1000)
        assert _entries_differ(e1, e2) is True

    def test_different_mtime_is_different(self) -> None:
        """Different mtime means entries differ."""
        e1 = ManifestFilePath(path="/f.txt", hash="abc123", size=10, mtime=1000)
        e2 = ManifestFilePath(path="/f.txt", hash="abc123", size=10, mtime=2000)
        assert _entries_differ(e1, e2) is True

    def test_different_size_is_different(self) -> None:
        """Different size means entries differ."""
        e1 = ManifestFilePath(path="/f.txt", hash="abc123", size=10, mtime=1000)
        e2 = ManifestFilePath(path="/f.txt", hash="abc123", size=20, mtime=1000)
        assert _entries_differ(e1, e2) is True

    def test_different_runnable_is_different(self) -> None:
        """Different runnable flag means entries differ."""
        e1 = ManifestFilePath(path="/f.txt", hash="abc123", size=10, mtime=1000, runnable=False)
        e2 = ManifestFilePath(path="/f.txt", hash="abc123", size=10, mtime=1000, runnable=True)
        assert _entries_differ(e1, e2) is True

    def test_ignore_runnable_skips_runnable_comparison(self) -> None:
        """ignore_runnable=True skips runnable comparison."""
        e1 = ManifestFilePath(path="/f.txt", hash="abc123", size=10, mtime=1000, runnable=False)
        e2 = ManifestFilePath(path="/f.txt", hash="abc123", size=10, mtime=1000, runnable=True)
        assert _entries_differ(e1, e2, ignore_runnable=True) is False

    # === Chunked file comparisons ===

    def test_same_chunkhashes_not_different(self) -> None:
        """Same chunkhashes means entries don't differ."""
        e1 = ManifestFilePath(
            path="/large.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        e2 = ManifestFilePath(
            path="/large.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        assert _entries_differ(e1, e2) is False

    def test_different_chunkhashes_is_different(self) -> None:
        """Different chunkhashes means entries differ."""
        e1 = ManifestFilePath(
            path="/large.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        e2 = ManifestFilePath(
            path="/large.bin",
            chunkhashes=["h1", "h3"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        assert _entries_differ(e1, e2) is True

    # === Symlink comparisons ===

    def test_same_symlink_target_not_different(self) -> None:
        """Same symlink target means entries don't differ."""
        e1 = ManifestFilePath(path="/link.txt", symlink_target="/target.txt")
        e2 = ManifestFilePath(path="/link.txt", symlink_target="/target.txt")
        assert _entries_differ(e1, e2) is False

    def test_different_symlink_target_is_different(self) -> None:
        """Different symlink target means entries differ."""
        e1 = ManifestFilePath(path="/link.txt", symlink_target="/target1.txt")
        e2 = ManifestFilePath(path="/link.txt", symlink_target="/target2.txt")
        assert _entries_differ(e1, e2) is True

    # === Type transitions ===

    def test_regular_file_to_symlink_is_different(self) -> None:
        """Regular file becoming a symlink is detected."""
        e1 = ManifestFilePath(path="/file.txt", hash="abc123", size=10, mtime=1000)
        e2 = ManifestFilePath(path="/file.txt", symlink_target="/other.txt")
        assert _entries_differ(e1, e2) is True

    def test_symlink_to_regular_file_is_different(self) -> None:
        """Symlink becoming a regular file is detected."""
        e1 = ManifestFilePath(path="/file.txt", symlink_target="/other.txt")
        e2 = ManifestFilePath(path="/file.txt", hash="abc123", size=10, mtime=1000)
        assert _entries_differ(e1, e2) is True

    def test_regular_file_to_chunked_file_is_different(self) -> None:
        """Regular file becoming a chunked file is detected."""
        e1 = ManifestFilePath(path="/file.bin", hash="abc123", size=100, mtime=1000)
        e2 = ManifestFilePath(
            path="/file.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=2000,
        )
        assert _entries_differ(e1, e2) is True

    def test_chunked_file_to_regular_file_is_different(self) -> None:
        """Chunked file becoming a regular file is detected."""
        e1 = ManifestFilePath(
            path="/file.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        e2 = ManifestFilePath(path="/file.bin", hash="abc123", size=100, mtime=2000)
        assert _entries_differ(e1, e2) is True

    # === ignore_hashes mode ===

    def test_ignore_hashes_skips_hash_comparison(self) -> None:
        """ignore_hashes=True skips hash comparison."""
        e1 = ManifestFilePath(path="/f.txt", hash="abc123", size=10, mtime=1000)
        e2 = ManifestFilePath(path="/f.txt", hash="def456", size=10, mtime=1000)
        assert _entries_differ(e1, e2, ignore_hashes=True) is False

    def test_ignore_hashes_skips_chunkhashes_comparison(self) -> None:
        """ignore_hashes=True skips chunkhashes comparison."""
        e1 = ManifestFilePath(
            path="/large.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        e2 = ManifestFilePath(
            path="/large.bin",
            chunkhashes=["h1", "h3"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        assert _entries_differ(e1, e2, ignore_hashes=True) is False


class TestDirectoryDeletionSemantics:
    """Tests for directory deletion semantics.

    A directory deletion marker means "delete this empty directory". To delete
    a non-empty directory, all its contents must be explicitly deleted first.
    """

    def _create_manifest(self, files: List[dict], dirs: List[dict] | None = None) -> AbsSnapshot:
        """Helper to create an AbsSnapshot."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            files=file_entries,
            total_size=total_size,
        )

    def test_deleted_directory_includes_contained_files(self) -> None:
        """Deleting a directory also deletes files within it."""
        parent = self._create_manifest(
            files=[
                {"path": "/keep.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "/deleted_dir/file1.txt", "hash": "h2", "size": 50, "mtime": 2000},
                {"path": "/deleted_dir/file2.txt", "hash": "h3", "size": 75, "mtime": 3000},
            ],
            dirs=[{"path": "/deleted_dir"}],
        )
        current = self._create_manifest(
            files=[{"path": "/keep.txt", "hash": "h1", "size": 100, "mtime": 1000}],
            dirs=[],
        )

        diff = compute_diff_manifest(parent, current)

        # Should have deletion markers for both files and the directory
        deleted_files = {p.path for p in diff.files if p.deleted}
        deleted_dirs = {d.path for d in diff.dirs if d.deleted}

        assert "/deleted_dir/file1.txt" in deleted_files
        assert "/deleted_dir/file2.txt" in deleted_files
        assert "/deleted_dir" in deleted_dirs

    def test_deleted_directory_includes_subdirectories(self) -> None:
        """Deleting a directory also deletes subdirectories within it."""
        parent = self._create_manifest(
            files=[],
            dirs=[
                {"path": "/deleted_dir"},
                {"path": "/deleted_dir/subdir"},
            ],
        )
        current = self._create_manifest(files=[], dirs=[])

        diff = compute_diff_manifest(parent, current)

        deleted_dirs = {d.path for d in diff.dirs if d.deleted}
        assert "/deleted_dir" in deleted_dirs
        assert "/deleted_dir/subdir" in deleted_dirs
