# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for _compute_diff_manifest and related functions.

These tests cover:
- Basic diff computation for both v2023 and v2025 formats
- New file detection
- Modified file detection (by hash comparison)
- Deleted file detection (v2025 only - v2023 doesn't support deletion markers)
- Directory change detection (v2025 only)
- Symlink change detection (v2025 only)
- Parent manifest hash computation
- Version mismatch error handling

Note: The diff operation is a pure comparison - it does NOT compute hashes.
Both input manifests must already have hashes computed via _hash_manifest().
"""

import pytest
from typing import List

from deadline.job_attachments.asset_manifests._operations._diff_manifest import (
    _compute_diff_manifest,
    _entries_differ,
)
from deadline.job_attachments.asset_manifests._operations._filter_manifest import (
    _filter_manifest,
    IncludeExcludePathsFilter,
)
from deadline.job_attachments.asset_manifests.versions import (
    ManifestType,
    ManifestVersion,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import (
    HashAlgorithm,
)
from deadline.job_attachments.asset_manifests.v2023_03_03.asset_manifest import (
    AssetManifest as AssetManifest2023,
    ManifestPath as ManifestPath2023,
)
from deadline.job_attachments.asset_manifests.v2025_12_04.asset_manifest import (
    AssetManifest as AssetManifest2025,
    ManifestDirectoryPath as ManifestDirectoryPath2025,
    ManifestFilePath as ManifestFilePath2025,
)


class TestComputeDiffManifestV2023:
    """Tests for v2023-03-03 diff computation."""

    def _create_v2023_manifest(self, paths: List[tuple[str, str, int, int]]) -> AssetManifest2023:
        """Helper to create a v2023 manifest.

        Args:
            paths: List of (path, hash, size, mtime) tuples
        """
        entries = [ManifestPath2023(path=p, hash=h, size=s, mtime=m) for p, h, s, m in paths]
        total_size = sum(s for _, _, s, _ in paths)
        return AssetManifest2023(
            hash_alg=HashAlgorithm.XXH128,
            paths=entries,
            total_size=total_size,
        )

    def test_no_changes_returns_empty(self) -> None:
        """No changes between parent and current returns empty manifest."""
        parent = self._create_v2023_manifest([("file.txt", "hash1", 100, 1000)])
        current = self._create_v2023_manifest([("file.txt", "hash1", 100, 1000)])

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 0

    def test_new_file_detected(self) -> None:
        """New file in current is included in diff."""
        parent = self._create_v2023_manifest([("old.txt", "hash1", 100, 1000)])
        current = self._create_v2023_manifest(
            [
                ("old.txt", "hash1", 100, 1000),
                ("new.txt", "hash2", 50, 2000),
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "new.txt"
        assert diff.paths[0].hash == "hash2"

    def test_modified_file_detected(self) -> None:
        """Modified file (different hash) is detected."""
        parent = self._create_v2023_manifest([("file.txt", "hash1", 100, 1000)])
        current = self._create_v2023_manifest([("file.txt", "hash2", 100, 2000)])

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "file.txt"
        assert diff.paths[0].hash == "hash2"

    def test_same_hash_different_mtime_is_modified(self) -> None:
        """Same hash but different mtime IS considered modified in v2023."""
        parent = self._create_v2023_manifest([("file.txt", "hash1", 100, 1000)])
        current = self._create_v2023_manifest([("file.txt", "hash1", 100, 2000)])

        diff = _compute_diff_manifest(parent, current)

        # Different mtime = modified (even with same hash)
        assert len(diff.paths) == 1
        assert diff.paths[0].path == "file.txt"
        assert diff.paths[0].mtime == 2000

    def test_same_hash_different_size_is_modified(self) -> None:
        """Same hash but different size IS considered modified in v2023."""
        parent = self._create_v2023_manifest([("file.txt", "hash1", 100, 1000)])
        current = self._create_v2023_manifest([("file.txt", "hash1", 200, 1000)])

        diff = _compute_diff_manifest(parent, current)

        # Different size = modified (even with same hash)
        assert len(diff.paths) == 1
        assert diff.paths[0].path == "file.txt"
        assert diff.paths[0].size == 200

    def test_all_same_not_modified(self) -> None:
        """File with same hash, size, and mtime is not modified."""
        parent = self._create_v2023_manifest([("file.txt", "hash1", 100, 1000)])
        current = self._create_v2023_manifest([("file.txt", "hash1", 100, 1000)])

        diff = _compute_diff_manifest(parent, current)

        # Everything same = not modified
        assert len(diff.paths) == 0

    def test_unchanged_file_not_in_diff(self) -> None:
        """Unchanged file is not included in diff."""
        parent = self._create_v2023_manifest(
            [
                ("unchanged.txt", "hash1", 100, 1000),
                ("changed.txt", "hash2", 200, 2000),
            ]
        )
        current = self._create_v2023_manifest(
            [
                ("unchanged.txt", "hash1", 100, 1000),
                ("changed.txt", "hash3", 200, 3000),  # Different hash
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        paths = {p.path for p in diff.paths}
        assert "unchanged.txt" not in paths
        assert "changed.txt" in paths

    def test_v2023_no_deletion_markers(self) -> None:
        """v2023 format doesn't include deletion markers."""
        parent = self._create_v2023_manifest(
            [
                ("keep.txt", "hash1", 100, 1000),
                ("delete.txt", "hash2", 200, 2000),
            ]
        )
        current = self._create_v2023_manifest([("keep.txt", "hash1", 100, 1000)])

        diff = _compute_diff_manifest(parent, current)

        # v2023 doesn't track deletions, so diff should be empty
        assert len(diff.paths) == 0

    def test_total_size_calculated(self) -> None:
        """Total size is sum of changed entries."""
        parent = self._create_v2023_manifest([("old.txt", "hash1", 100, 1000)])
        current = self._create_v2023_manifest(
            [
                ("old.txt", "hash1", 100, 1000),
                ("new1.txt", "hash2", 50, 2000),
                ("new2.txt", "hash3", 75, 3000),
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        assert diff.totalSize == 125  # 50 + 75


class TestComputeDiffManifestV2025:
    """Tests for v2025-12-04-beta diff computation."""

    def _create_v2025_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
        manifest_type: ManifestType = ManifestType.SNAPSHOT,
    ) -> AssetManifest2025:
        """Helper to create a v2025 manifest."""
        file_entries = [ManifestFilePath2025(**f) for f in files]
        dir_entries = [ManifestDirectoryPath2025(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
            manifest_type=manifest_type,
        )

    def test_no_changes_returns_empty_diff(self) -> None:
        """No changes returns diff manifest with no entries."""
        parent = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )

        diff = _compute_diff_manifest(parent, current)

        assert diff.manifestType == ManifestType.DIFF
        assert len(diff.paths) == 0
        assert len(diff.dirs) == 0

    def test_parent_manifest_hash_stored(self) -> None:
        """parentManifestHash is stored when provided."""
        parent = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        parent_hash = "abc123def456"

        diff = _compute_diff_manifest(parent, current, parent_manifest_hash=parent_hash)

        assert diff.parentManifestHash == parent_hash

    def test_parent_manifest_hash_optional(self) -> None:
        """parentManifestHash is optional."""
        parent = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )

        diff = _compute_diff_manifest(parent, current)

        assert diff.parentManifestHash is None

    def test_new_file_detected(self) -> None:
        """New file in current is included in diff."""
        parent = self._create_v2025_manifest(
            [{"path": "old.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_v2025_manifest(
            [
                {"path": "old.txt", "hash": "hash1", "size": 100, "mtime": 1000},
                {"path": "new.txt", "hash": "hash2", "size": 50, "mtime": 2000},
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        new_entry = next((p for p in diff.paths if p.path == "new.txt"), None)
        assert new_entry is not None
        assert new_entry.hash == "hash2"
        assert not new_entry.deleted

    def test_deleted_file_has_marker(self) -> None:
        """Deleted file has deleted=True marker in diff."""
        parent = self._create_v2025_manifest(
            [
                {"path": "keep.txt", "hash": "hash1", "size": 100, "mtime": 1000},
                {"path": "delete.txt", "hash": "hash2", "size": 200, "mtime": 2000},
            ]
        )
        current = self._create_v2025_manifest(
            [{"path": "keep.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )

        diff = _compute_diff_manifest(parent, current)

        deleted_entry = next((p for p in diff.paths if p.path == "delete.txt"), None)
        assert deleted_entry is not None
        assert deleted_entry.deleted is True

    def test_modified_file_detected(self) -> None:
        """Modified file (different hash) is detected."""
        parent = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "hash2", "size": 100, "mtime": 2000}]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "file.txt"
        assert diff.paths[0].hash == "hash2"

    def test_new_directory_included(self) -> None:
        """New directory is included in diff."""
        parent = self._create_v2025_manifest(files=[], dirs=[{"path": "old_dir"}])
        current = self._create_v2025_manifest(
            files=[], dirs=[{"path": "old_dir"}, {"path": "new_dir"}]
        )

        diff = _compute_diff_manifest(parent, current)

        dir_paths = {d.path for d in diff.dirs}
        assert "new_dir" in dir_paths

    def test_deleted_directory_has_marker(self) -> None:
        """Deleted directory has deleted=True marker."""
        parent = self._create_v2025_manifest(
            files=[], dirs=[{"path": "keep_dir"}, {"path": "delete_dir"}]
        )
        current = self._create_v2025_manifest(files=[], dirs=[{"path": "keep_dir"}])

        diff = _compute_diff_manifest(parent, current)

        deleted_dir = next((d for d in diff.dirs if d.path == "delete_dir"), None)
        assert deleted_dir is not None
        assert deleted_dir.deleted is True

    def test_symlink_change_detected(self) -> None:
        """Changed symlink target is detected."""
        parent = self._create_v2025_manifest(
            [{"path": "link.txt", "symlink_target": "old_target.txt"}]
        )
        current = self._create_v2025_manifest(
            [{"path": "link.txt", "symlink_target": "new_target.txt"}]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "link.txt"
        assert diff.paths[0].symlink_target == "new_target.txt"

    def test_new_symlink_included(self) -> None:
        """New symlink is included in diff."""
        parent = self._create_v2025_manifest(files=[])
        current = self._create_v2025_manifest(
            [{"path": "link.txt", "symlink_target": "target.txt"}]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].symlink_target == "target.txt"

    def test_deleted_symlink_has_marker(self) -> None:
        """Deleted symlink has deleted=True marker."""
        parent = self._create_v2025_manifest([{"path": "link.txt", "symlink_target": "target.txt"}])
        current = self._create_v2025_manifest(files=[])

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "link.txt"
        assert diff.paths[0].deleted is True

    def test_preserves_runnable_flag(self) -> None:
        """Runnable flag is preserved for changed files."""
        parent = self._create_v2025_manifest(files=[])
        current = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "hash1", "size": 100, "mtime": 1000, "runnable": True}]
        )

        diff = _compute_diff_manifest(parent, current)

        assert diff.paths[0].runnable is True

    def test_preserves_chunkhashes(self) -> None:
        """Chunkhashes are preserved for large files."""
        parent = self._create_v2025_manifest(files=[])
        # 512MB file = 2 chunks
        current = self._create_v2025_manifest(
            [
                {
                    "path": "large.bin",
                    "chunkhashes": ["chunk1", "chunk2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                }
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        assert diff.paths[0].chunkhashes == ["chunk1", "chunk2"]

    def test_chunked_file_modification_detected(self) -> None:
        """Modified chunked file (different chunkhashes) is detected."""
        parent = self._create_v2025_manifest(
            [
                {
                    "path": "large.bin",
                    "chunkhashes": ["chunk1", "chunk2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                }
            ]
        )
        current = self._create_v2025_manifest(
            [
                {
                    "path": "large.bin",
                    "chunkhashes": ["chunk1", "chunk3"],  # Second chunk changed
                    "size": 512 * 1024 * 1024,
                    "mtime": 2000,
                }
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].chunkhashes == ["chunk1", "chunk3"]

    def test_total_size_calculated(self) -> None:
        """Total size is sum of non-deleted, non-symlink entries."""
        parent = self._create_v2025_manifest(
            [{"path": "delete.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )
        current = self._create_v2025_manifest(
            [{"path": "new.txt", "hash": "h2", "size": 50, "mtime": 2000}]
        )

        diff = _compute_diff_manifest(parent, current)

        # Should include new.txt (50) but not deleted.txt
        assert diff.totalSize == 50

    def test_symlinks_not_counted_in_total_size(self) -> None:
        """Symlinks don't contribute to total size."""
        parent = self._create_v2025_manifest(files=[])
        current = self._create_v2025_manifest(
            [
                {"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "link.txt", "symlink_target": "file.txt"},
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        assert diff.totalSize == 100  # Only file.txt, not symlink


class TestComputeDiffManifestValidation:
    """Tests for validation and error handling."""

    def test_version_mismatch_raises_error(self) -> None:
        """Mismatched versions raise ValueError."""
        parent = AssetManifest2023(
            hash_alg=HashAlgorithm.XXH128,
            paths=[],
            total_size=0,
        )
        current = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[],
            total_size=0,
        )

        with pytest.raises(ValueError, match="does not match"):
            _compute_diff_manifest(parent, current)

    def test_type_error_for_wrong_manifest_type_v2023(self) -> None:
        """TypeError raised if manifest type doesn't match version for v2023."""
        parent = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[],
            total_size=0,
        )
        current = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[],
            total_size=0,
        )
        # Manually override version to trigger type check
        parent.manifestVersion = ManifestVersion.v2023_03_03
        current.manifestVersion = ManifestVersion.v2023_03_03

        with pytest.raises(TypeError, match="Expected AssetManifest2023"):
            _compute_diff_manifest(parent, current)


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
        parent = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath2025(path="model.blend", hash="h1", size=100, mtime=1000),
                ManifestFilePath2025(path="texture.png", hash="h2", size=200, mtime=2000),
            ],
            total_size=300,
        )
        current = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath2025(path="model.blend", hash="h1", size=100, mtime=1000),
                ManifestFilePath2025(path="new.blend", hash="h3", size=150, mtime=3000),
            ],
            total_size=250,
        )

        # Filter BOTH with same filter
        filter_obj = IncludeExcludePathsFilter(include=["*.blend"])
        filtered_parent = _filter_manifest(parent, filter_obj)
        filtered_current = _filter_manifest(current, filter_obj)

        diff = _compute_diff_manifest(filtered_parent, filtered_current)

        # Should only have new.blend as added, no deletions
        paths = {p.path for p in diff.paths}
        assert "new.blend" in paths
        assert "texture.png" not in paths  # Not deleted because it was filtered out

        # Verify no deletion markers
        deleted = [p for p in diff.paths if p.deleted]
        assert len(deleted) == 0


class TestEntriesDiffer:
    """Tests for _entries_differ helper."""

    # === Regular file comparisons ===

    def test_same_hash_not_different(self) -> None:
        """Same hash means entries don't differ."""
        e1 = ManifestFilePath2025(path="f.txt", hash="abc123", size=10, mtime=1000)
        e2 = ManifestFilePath2025(path="f.txt", hash="abc123", size=10, mtime=1000)
        assert _entries_differ(e1, e2) is False

    def test_different_hash_is_different(self) -> None:
        """Different hash means entries differ."""
        e1 = ManifestFilePath2025(path="f.txt", hash="abc123", size=10, mtime=1000)
        e2 = ManifestFilePath2025(path="f.txt", hash="def456", size=10, mtime=1000)
        assert _entries_differ(e1, e2) is True

    def test_different_mtime_is_different(self) -> None:
        """Different mtime means entries differ."""
        e1 = ManifestFilePath2025(path="f.txt", hash="abc123", size=10, mtime=1000)
        e2 = ManifestFilePath2025(path="f.txt", hash="abc123", size=10, mtime=2000)
        assert _entries_differ(e1, e2) is True

    def test_different_runnable_is_different(self) -> None:
        """Different runnable flag means entries differ."""
        e1 = ManifestFilePath2025(path="f.txt", hash="abc123", size=10, mtime=1000, runnable=False)
        e2 = ManifestFilePath2025(path="f.txt", hash="abc123", size=10, mtime=1000, runnable=True)
        assert _entries_differ(e1, e2) is True

    def test_runnable_false_to_true_is_different(self) -> None:
        """Changing runnable from False to True is detected."""
        e1 = ManifestFilePath2025(
            path="script.sh", hash="abc123", size=10, mtime=1000, runnable=False
        )
        e2 = ManifestFilePath2025(
            path="script.sh", hash="abc123", size=10, mtime=1000, runnable=True
        )
        assert _entries_differ(e1, e2) is True

    def test_runnable_true_to_false_is_different(self) -> None:
        """Changing runnable from True to False is detected."""
        e1 = ManifestFilePath2025(
            path="script.sh", hash="abc123", size=10, mtime=1000, runnable=True
        )
        e2 = ManifestFilePath2025(
            path="script.sh", hash="abc123", size=10, mtime=1000, runnable=False
        )
        assert _entries_differ(e1, e2) is True

    # === Chunked file comparisons ===

    def test_same_chunkhashes_not_different(self) -> None:
        """Same chunkhashes means entries don't differ."""
        e1 = ManifestFilePath2025(
            path="large.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        e2 = ManifestFilePath2025(
            path="large.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        assert _entries_differ(e1, e2) is False

    def test_different_chunkhashes_is_different(self) -> None:
        """Different chunkhashes means entries differ."""
        e1 = ManifestFilePath2025(
            path="large.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        e2 = ManifestFilePath2025(
            path="large.bin",
            chunkhashes=["h1", "h3"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        assert _entries_differ(e1, e2) is True

    def test_chunked_file_different_mtime_is_different(self) -> None:
        """Different mtime on chunked file means entries differ."""
        e1 = ManifestFilePath2025(
            path="large.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        e2 = ManifestFilePath2025(
            path="large.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=2000,
        )
        assert _entries_differ(e1, e2) is True

    def test_chunked_file_different_runnable_is_different(self) -> None:
        """Different runnable on chunked file means entries differ."""
        e1 = ManifestFilePath2025(
            path="large.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
            runnable=False,
        )
        e2 = ManifestFilePath2025(
            path="large.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
            runnable=True,
        )
        assert _entries_differ(e1, e2) is True

    # === Symlink comparisons ===

    def test_same_symlink_target_not_different(self) -> None:
        """Same symlink target means entries don't differ."""
        e1 = ManifestFilePath2025(path="link.txt", symlink_target="target.txt")
        e2 = ManifestFilePath2025(path="link.txt", symlink_target="target.txt")
        assert _entries_differ(e1, e2) is False

    def test_different_symlink_target_is_different(self) -> None:
        """Different symlink target means entries differ."""
        e1 = ManifestFilePath2025(path="link.txt", symlink_target="target1.txt")
        e2 = ManifestFilePath2025(path="link.txt", symlink_target="target2.txt")
        assert _entries_differ(e1, e2) is True

    # === Type transitions ===

    def test_regular_file_to_symlink_is_different(self) -> None:
        """Regular file becoming a symlink is detected."""
        e1 = ManifestFilePath2025(path="file.txt", hash="abc123", size=10, mtime=1000)
        e2 = ManifestFilePath2025(path="file.txt", symlink_target="other.txt")
        assert _entries_differ(e1, e2) is True

    def test_symlink_to_regular_file_is_different(self) -> None:
        """Symlink becoming a regular file is detected."""
        e1 = ManifestFilePath2025(path="file.txt", symlink_target="other.txt")
        e2 = ManifestFilePath2025(path="file.txt", hash="abc123", size=10, mtime=1000)
        assert _entries_differ(e1, e2) is True

    def test_regular_file_to_chunked_file_is_different(self) -> None:
        """Regular file becoming a chunked file is detected."""
        e1 = ManifestFilePath2025(path="file.bin", hash="abc123", size=100, mtime=1000)
        e2 = ManifestFilePath2025(
            path="file.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=2000,
        )
        assert _entries_differ(e1, e2) is True

    def test_chunked_file_to_regular_file_is_different(self) -> None:
        """Chunked file becoming a regular file is detected."""
        e1 = ManifestFilePath2025(
            path="file.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        e2 = ManifestFilePath2025(path="file.bin", hash="abc123", size=100, mtime=2000)
        assert _entries_differ(e1, e2) is True

    def test_symlink_to_chunked_file_is_different(self) -> None:
        """Symlink becoming a chunked file is detected."""
        e1 = ManifestFilePath2025(path="file.bin", symlink_target="other.bin")
        e2 = ManifestFilePath2025(
            path="file.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        assert _entries_differ(e1, e2) is True

    def test_chunked_file_to_symlink_is_different(self) -> None:
        """Chunked file becoming a symlink is detected."""
        e1 = ManifestFilePath2025(
            path="file.bin",
            chunkhashes=["h1", "h2"],
            size=512 * 1024 * 1024,
            mtime=1000,
        )
        e2 = ManifestFilePath2025(path="file.bin", symlink_target="other.bin")
        assert _entries_differ(e1, e2) is True


class TestProgressCallback:
    """Tests for progress callback functionality."""

    def test_callback_for_new_files(self) -> None:
        """Progress callback is called for new files."""
        parent = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[],
            total_size=0,
        )
        current = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[ManifestFilePath2025(path="new.txt", hash="h1", size=10, mtime=1000)],
            total_size=10,
        )

        messages: List[str] = []
        _compute_diff_manifest(parent, current, print_function_callback=messages.append)

        assert any("New" in msg and "new.txt" in msg for msg in messages)

    def test_callback_for_modified_files(self) -> None:
        """Progress callback is called for modified files."""
        parent = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[ManifestFilePath2025(path="file.txt", hash="h1", size=10, mtime=1000)],
            total_size=10,
        )
        current = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[ManifestFilePath2025(path="file.txt", hash="h2", size=10, mtime=2000)],
            total_size=10,
        )

        messages: List[str] = []
        _compute_diff_manifest(parent, current, print_function_callback=messages.append)

        assert any("Modified" in msg and "file.txt" in msg for msg in messages)

    def test_callback_for_deleted_files(self) -> None:
        """Progress callback is called for deleted files."""
        parent = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[ManifestFilePath2025(path="old.txt", hash="h1", size=10, mtime=1000)],
            total_size=10,
        )
        current = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[],
            total_size=0,
        )

        messages: List[str] = []
        _compute_diff_manifest(parent, current, print_function_callback=messages.append)

        assert any("Deleted" in msg and "old.txt" in msg for msg in messages)

    def test_callback_for_deleted_dirs(self) -> None:
        """Progress callback is called for deleted directories."""
        parent = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[ManifestDirectoryPath2025(path="old_dir")],
            paths=[],
            total_size=0,
        )
        current = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[],
            total_size=0,
        )

        messages: List[str] = []
        _compute_diff_manifest(parent, current, print_function_callback=messages.append)

        assert any("Deleted dir" in msg and "old_dir" in msg for msg in messages)


class TestComputeDiffManifestMetadataChanges:
    """Tests for diff manifest detection of metadata changes (mtime, runnable)."""

    def _create_v2025_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AssetManifest2025:
        """Helper to create a v2025 manifest."""
        file_entries = [ManifestFilePath2025(**f) for f in files]
        dir_entries = [ManifestDirectoryPath2025(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
        )

    def test_mtime_change_detected(self) -> None:
        """File with changed mtime (but same hash) is detected as modified."""
        parent = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "hash1", "size": 100, "mtime": 2000}]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "file.txt"
        assert diff.paths[0].mtime == 2000

    def test_runnable_change_detected(self) -> None:
        """File with changed runnable flag (but same hash) is detected as modified."""
        parent = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "hash1", "size": 100, "mtime": 1000, "runnable": False}]
        )
        current = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "hash1", "size": 100, "mtime": 1000, "runnable": True}]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "script.sh"
        assert diff.paths[0].runnable is True

    def test_runnable_removed_detected(self) -> None:
        """File with runnable flag removed is detected as modified."""
        parent = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "hash1", "size": 100, "mtime": 1000, "runnable": True}]
        )
        current = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "hash1", "size": 100, "mtime": 1000, "runnable": False}]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "script.sh"
        assert diff.paths[0].runnable is False

    def test_multiple_metadata_changes(self) -> None:
        """File with multiple metadata changes is detected."""
        parent = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "hash1", "size": 100, "mtime": 1000, "runnable": False}]
        )
        current = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "hash1", "size": 100, "mtime": 2000, "runnable": True}]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].mtime == 2000
        assert diff.paths[0].runnable is True

    def test_chunked_file_mtime_change_detected(self) -> None:
        """Chunked file with changed mtime is detected as modified."""
        parent = self._create_v2025_manifest(
            [
                {
                    "path": "large.bin",
                    "chunkhashes": ["h1", "h2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                }
            ]
        )
        current = self._create_v2025_manifest(
            [
                {
                    "path": "large.bin",
                    "chunkhashes": ["h1", "h2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 2000,
                }
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].mtime == 2000

    def test_chunked_file_runnable_change_detected(self) -> None:
        """Chunked file with changed runnable flag is detected as modified."""
        parent = self._create_v2025_manifest(
            [
                {
                    "path": "large.bin",
                    "chunkhashes": ["h1", "h2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                    "runnable": False,
                }
            ]
        )
        current = self._create_v2025_manifest(
            [
                {
                    "path": "large.bin",
                    "chunkhashes": ["h1", "h2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                    "runnable": True,
                }
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].runnable is True


class TestComputeDiffManifestTypeTransitions:
    """Tests for diff manifest detection of entry type transitions."""

    def _create_v2025_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AssetManifest2025:
        """Helper to create a v2025 manifest."""
        file_entries = [ManifestFilePath2025(**f) for f in files]
        dir_entries = [ManifestDirectoryPath2025(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
        )

    def test_regular_file_to_symlink(self) -> None:
        """Regular file becoming a symlink is detected as modified."""
        parent = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_v2025_manifest([{"path": "file.txt", "symlink_target": "other.txt"}])

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "file.txt"
        assert diff.paths[0].symlink_target == "other.txt"
        assert diff.paths[0].hash is None

    def test_symlink_to_regular_file(self) -> None:
        """Symlink becoming a regular file is detected as modified."""
        parent = self._create_v2025_manifest([{"path": "file.txt", "symlink_target": "other.txt"}])
        current = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "hash1", "size": 100, "mtime": 1000}]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "file.txt"
        assert diff.paths[0].hash == "hash1"
        assert diff.paths[0].symlink_target is None

    def test_regular_file_to_chunked_file(self) -> None:
        """Regular file becoming a chunked file is detected as modified."""
        parent = self._create_v2025_manifest(
            [{"path": "file.bin", "hash": "hash1", "size": 100, "mtime": 1000}]
        )
        current = self._create_v2025_manifest(
            [
                {
                    "path": "file.bin",
                    "chunkhashes": ["h1", "h2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 2000,
                }
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "file.bin"
        assert diff.paths[0].chunkhashes == ["h1", "h2"]
        assert diff.paths[0].hash is None

    def test_chunked_file_to_regular_file(self) -> None:
        """Chunked file becoming a regular file is detected as modified."""
        parent = self._create_v2025_manifest(
            [
                {
                    "path": "file.bin",
                    "chunkhashes": ["h1", "h2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                }
            ]
        )
        current = self._create_v2025_manifest(
            [{"path": "file.bin", "hash": "hash1", "size": 100, "mtime": 2000}]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "file.bin"
        assert diff.paths[0].hash == "hash1"
        assert diff.paths[0].chunkhashes is None

    def test_symlink_to_chunked_file(self) -> None:
        """Symlink becoming a chunked file is detected as modified."""
        parent = self._create_v2025_manifest([{"path": "file.bin", "symlink_target": "other.bin"}])
        current = self._create_v2025_manifest(
            [
                {
                    "path": "file.bin",
                    "chunkhashes": ["h1", "h2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                }
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "file.bin"
        assert diff.paths[0].chunkhashes == ["h1", "h2"]
        assert diff.paths[0].symlink_target is None

    def test_chunked_file_to_symlink(self) -> None:
        """Chunked file becoming a symlink is detected as modified."""
        parent = self._create_v2025_manifest(
            [
                {
                    "path": "file.bin",
                    "chunkhashes": ["h1", "h2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                }
            ]
        )
        current = self._create_v2025_manifest([{"path": "file.bin", "symlink_target": "other.bin"}])

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "file.bin"
        assert diff.paths[0].symlink_target == "other.bin"
        assert diff.paths[0].chunkhashes is None

    def test_multiple_type_transitions(self) -> None:
        """Multiple files with different type transitions are all detected."""
        parent = self._create_v2025_manifest(
            [
                {"path": "file1.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "file2.txt", "symlink_target": "target.txt"},
                {
                    "path": "file3.bin",
                    "chunkhashes": ["c1", "c2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                },
            ]
        )
        current = self._create_v2025_manifest(
            [
                # file1: regular -> symlink
                {"path": "file1.txt", "symlink_target": "other.txt"},
                # file2: symlink -> regular
                {"path": "file2.txt", "hash": "h2", "size": 50, "mtime": 2000},
                # file3: chunked -> regular
                {"path": "file3.bin", "hash": "h3", "size": 100, "mtime": 2000},
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 3
        paths = {p.path: p for p in diff.paths}

        assert paths["file1.txt"].symlink_target == "other.txt"
        assert paths["file2.txt"].hash == "h2"
        assert paths["file3.bin"].hash == "h3"


class TestComputeDiffManifestMixedChanges:
    """Tests for diff manifest with mixed change types."""

    def _create_v2025_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AssetManifest2025:
        """Helper to create a v2025 manifest."""
        file_entries = [ManifestFilePath2025(**f) for f in files]
        dir_entries = [ManifestDirectoryPath2025(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
        )

    def test_mixed_new_modified_deleted(self) -> None:
        """Diff with new, modified, and deleted files."""
        parent = self._create_v2025_manifest(
            [
                {"path": "unchanged.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "modified.txt", "hash": "h2", "size": 200, "mtime": 1000},
                {"path": "deleted.txt", "hash": "h3", "size": 300, "mtime": 1000},
            ]
        )
        current = self._create_v2025_manifest(
            [
                {"path": "unchanged.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "modified.txt", "hash": "h2_new", "size": 200, "mtime": 2000},
                {"path": "new.txt", "hash": "h4", "size": 400, "mtime": 2000},
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        paths = {p.path: p for p in diff.paths}
        assert "unchanged.txt" not in paths
        assert "modified.txt" in paths
        assert paths["modified.txt"].hash == "h2_new"
        assert "deleted.txt" in paths
        assert paths["deleted.txt"].deleted is True
        assert "new.txt" in paths
        assert paths["new.txt"].hash == "h4"

    def test_mixed_content_and_metadata_changes(self) -> None:
        """Diff with both content and metadata changes."""
        parent = self._create_v2025_manifest(
            [
                {"path": "content_change.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "mtime_change.txt", "hash": "h2", "size": 200, "mtime": 1000},
                {
                    "path": "runnable_change.sh",
                    "hash": "h3",
                    "size": 300,
                    "mtime": 1000,
                    "runnable": False,
                },
            ]
        )
        current = self._create_v2025_manifest(
            [
                {"path": "content_change.txt", "hash": "h1_new", "size": 100, "mtime": 2000},
                {"path": "mtime_change.txt", "hash": "h2", "size": 200, "mtime": 2000},
                {
                    "path": "runnable_change.sh",
                    "hash": "h3",
                    "size": 300,
                    "mtime": 1000,
                    "runnable": True,
                },
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        assert len(diff.paths) == 3
        paths = {p.path: p for p in diff.paths}

        assert paths["content_change.txt"].hash == "h1_new"
        assert paths["mtime_change.txt"].mtime == 2000
        assert paths["runnable_change.sh"].runnable is True

    def test_mixed_type_transitions_and_deletions(self) -> None:
        """Diff with type transitions and deletions."""
        parent = self._create_v2025_manifest(
            [
                {"path": "file_to_symlink.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "symlink_to_delete.txt", "symlink_target": "target.txt"},
                {
                    "path": "chunked_to_delete.bin",
                    "chunkhashes": ["c1", "c2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                },
            ],
            dirs=[{"path": "dir_to_delete"}],
        )
        current = self._create_v2025_manifest(
            [
                {"path": "file_to_symlink.txt", "symlink_target": "new_target.txt"},
            ],
            dirs=[],
        )

        diff = _compute_diff_manifest(parent, current)

        paths = {p.path: p for p in diff.paths}
        dirs = {d.path: d for d in diff.dirs}

        # Type transition
        assert paths["file_to_symlink.txt"].symlink_target == "new_target.txt"

        # Deletions
        assert paths["symlink_to_delete.txt"].deleted is True
        assert paths["chunked_to_delete.bin"].deleted is True
        assert dirs["dir_to_delete"].deleted is True

    def test_unchanged_symlink_not_in_diff(self) -> None:
        """Unchanged symlink is not included in diff."""
        parent = self._create_v2025_manifest(
            [
                {"path": "link.txt", "symlink_target": "target.txt"},
                {"path": "changed.txt", "hash": "h1", "size": 100, "mtime": 1000},
            ]
        )
        current = self._create_v2025_manifest(
            [
                {"path": "link.txt", "symlink_target": "target.txt"},
                {"path": "changed.txt", "hash": "h2", "size": 100, "mtime": 2000},
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        paths = {p.path for p in diff.paths}
        assert "link.txt" not in paths
        assert "changed.txt" in paths

    def test_unchanged_chunked_file_not_in_diff(self) -> None:
        """Unchanged chunked file is not included in diff."""
        parent = self._create_v2025_manifest(
            [
                {
                    "path": "large.bin",
                    "chunkhashes": ["c1", "c2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                },
                {"path": "changed.txt", "hash": "h1", "size": 100, "mtime": 1000},
            ]
        )
        current = self._create_v2025_manifest(
            [
                {
                    "path": "large.bin",
                    "chunkhashes": ["c1", "c2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                },
                {"path": "changed.txt", "hash": "h2", "size": 100, "mtime": 2000},
            ]
        )

        diff = _compute_diff_manifest(parent, current)

        paths = {p.path for p in diff.paths}
        assert "large.bin" not in paths
        assert "changed.txt" in paths


class TestDirectoryDeletionSemantics:
    """Tests for directory deletion semantics in diff manifests.

    Per the design document, a directory deletion marker means "delete this empty
    directory". To delete a non-empty directory, all its contents must be explicitly
    deleted first:
    - All files and symlinks within the directory
    - All subdirectories (recursively, following the same rule)
    - Finally, the directory itself

    This ensures diff manifests are fully composable—each deletion is self-contained
    and doesn't depend on knowing the parent snapshot's contents.
    """

    def _create_v2025_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AssetManifest2025:
        """Helper to create a v2025 manifest."""
        file_entries = [ManifestFilePath2025(**f) for f in files]
        dir_entries = [ManifestDirectoryPath2025(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
        )

    def test_deleted_directory_includes_contained_files(self) -> None:
        """When a directory is deleted, all files within it must also be deleted."""
        parent = self._create_v2025_manifest(
            files=[
                {"path": "keep.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "deleted_dir/file1.txt", "hash": "h2", "size": 200, "mtime": 1000},
                {"path": "deleted_dir/file2.txt", "hash": "h3", "size": 300, "mtime": 1000},
            ],
            dirs=[{"path": "deleted_dir"}],
        )
        current = self._create_v2025_manifest(
            files=[{"path": "keep.txt", "hash": "h1", "size": 100, "mtime": 1000}],
            dirs=[],
        )

        diff = _compute_diff_manifest(parent, current)

        # Check that the directory is marked as deleted
        deleted_dirs = {d.path for d in diff.dirs if d.deleted}
        assert "deleted_dir" in deleted_dirs

        # Check that all files within the directory are also marked as deleted
        deleted_files = {p.path for p in diff.paths if p.deleted}
        assert "deleted_dir/file1.txt" in deleted_files
        assert "deleted_dir/file2.txt" in deleted_files

        # The kept file should not be in the diff
        assert "keep.txt" not in {p.path for p in diff.paths}

    def test_deleted_directory_includes_nested_subdirectories(self) -> None:
        """When a directory is deleted, all subdirectories must also be deleted."""
        parent = self._create_v2025_manifest(
            files=[
                {"path": "deleted_dir/subdir/file.txt", "hash": "h1", "size": 100, "mtime": 1000},
            ],
            dirs=[
                {"path": "deleted_dir"},
                {"path": "deleted_dir/subdir"},
            ],
        )
        current = self._create_v2025_manifest(files=[], dirs=[])

        diff = _compute_diff_manifest(parent, current)

        # Check that both directories are marked as deleted
        deleted_dirs = {d.path for d in diff.dirs if d.deleted}
        assert "deleted_dir" in deleted_dirs
        assert "deleted_dir/subdir" in deleted_dirs

        # Check that the file is also marked as deleted
        deleted_files = {p.path for p in diff.paths if p.deleted}
        assert "deleted_dir/subdir/file.txt" in deleted_files

    def test_deleted_directory_with_deep_nesting(self) -> None:
        """Deeply nested directory deletion includes all contents."""
        parent = self._create_v2025_manifest(
            files=[
                {"path": "a/b/c/d/file.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "a/b/other.txt", "hash": "h2", "size": 200, "mtime": 1000},
            ],
            dirs=[
                {"path": "a"},
                {"path": "a/b"},
                {"path": "a/b/c"},
                {"path": "a/b/c/d"},
            ],
        )
        current = self._create_v2025_manifest(files=[], dirs=[])

        diff = _compute_diff_manifest(parent, current)

        # All directories should be deleted
        deleted_dirs = {d.path for d in diff.dirs if d.deleted}
        assert deleted_dirs == {"a", "a/b", "a/b/c", "a/b/c/d"}

        # All files should be deleted
        deleted_files = {p.path for p in diff.paths if p.deleted}
        assert deleted_files == {"a/b/c/d/file.txt", "a/b/other.txt"}

    def test_partial_directory_deletion(self) -> None:
        """When only some files in a directory are deleted, directory is not deleted."""
        parent = self._create_v2025_manifest(
            files=[
                {"path": "dir/keep.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "dir/delete.txt", "hash": "h2", "size": 200, "mtime": 1000},
            ],
            dirs=[{"path": "dir"}],
        )
        current = self._create_v2025_manifest(
            files=[{"path": "dir/keep.txt", "hash": "h1", "size": 100, "mtime": 1000}],
            dirs=[{"path": "dir"}],
        )

        diff = _compute_diff_manifest(parent, current)

        # Directory should NOT be deleted (it still has files)
        deleted_dirs = {d.path for d in diff.dirs if d.deleted}
        assert "dir" not in deleted_dirs

        # Only the deleted file should be marked
        deleted_files = {p.path for p in diff.paths if p.deleted}
        assert deleted_files == {"dir/delete.txt"}

    def test_deleted_directory_with_symlinks(self) -> None:
        """Deleted directory includes symlinks within it."""
        parent = self._create_v2025_manifest(
            files=[
                {"path": "deleted_dir/file.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "deleted_dir/link.txt", "symlink_target": "file.txt"},
            ],
            dirs=[{"path": "deleted_dir"}],
        )
        current = self._create_v2025_manifest(files=[], dirs=[])

        diff = _compute_diff_manifest(parent, current)

        # Directory should be deleted
        deleted_dirs = {d.path for d in diff.dirs if d.deleted}
        assert "deleted_dir" in deleted_dirs

        # Both file and symlink should be deleted
        deleted_files = {p.path for p in diff.paths if p.deleted}
        assert "deleted_dir/file.txt" in deleted_files
        assert "deleted_dir/link.txt" in deleted_files

    def test_multiple_directories_deleted(self) -> None:
        """Multiple independent directories can be deleted."""
        parent = self._create_v2025_manifest(
            files=[
                {"path": "dir1/file.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "dir2/file.txt", "hash": "h2", "size": 200, "mtime": 1000},
                {"path": "keep_dir/file.txt", "hash": "h3", "size": 300, "mtime": 1000},
            ],
            dirs=[{"path": "dir1"}, {"path": "dir2"}, {"path": "keep_dir"}],
        )
        current = self._create_v2025_manifest(
            files=[{"path": "keep_dir/file.txt", "hash": "h3", "size": 300, "mtime": 1000}],
            dirs=[{"path": "keep_dir"}],
        )

        diff = _compute_diff_manifest(parent, current)

        # dir1 and dir2 should be deleted, keep_dir should not
        deleted_dirs = {d.path for d in diff.dirs if d.deleted}
        assert deleted_dirs == {"dir1", "dir2"}

        # Files in deleted directories should be deleted
        deleted_files = {p.path for p in diff.paths if p.deleted}
        assert deleted_files == {"dir1/file.txt", "dir2/file.txt"}


class TestPreserveRunnable:
    """Tests for preserve_runnable parameter.

    The preserve_runnable parameter addresses cross-platform workflows where:
    - A manifest is created on POSIX with runnable=True for executable files
    - Files are modified on Windows where runnable is always False
    - Without preserve_runnable, the diff would incorrectly change runnable to False
    """

    def _create_v2025_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
        manifest_type: ManifestType = ManifestType.SNAPSHOT,
    ) -> AssetManifest2025:
        """Helper to create a v2025 manifest."""
        file_entries = [ManifestFilePath2025(**f) for f in files]
        dir_entries = [ManifestDirectoryPath2025(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
            manifest_type=manifest_type,
        )

    def test_preserve_runnable_false_uses_current_value(self) -> None:
        """With preserve_runnable=False (default), modified files use current's runnable."""
        parent = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": True}]
        )
        current = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "h2", "size": 100, "mtime": 2000, "runnable": False}]
        )

        diff = _compute_diff_manifest(parent, current, preserve_runnable=False)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "script.sh"
        assert diff.paths[0].runnable is False  # Uses current's value

    def test_preserve_runnable_true_uses_parent_value_for_modified(self) -> None:
        """With preserve_runnable=True, modified files use parent's runnable."""
        parent = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": True}]
        )
        current = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "h2", "size": 100, "mtime": 2000, "runnable": False}]
        )

        diff = _compute_diff_manifest(parent, current, preserve_runnable=True)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "script.sh"
        assert diff.paths[0].runnable is True  # Preserved from parent

    def test_preserve_runnable_new_files_use_current_value(self) -> None:
        """New files always use current's runnable, even with preserve_runnable=True."""
        parent = self._create_v2025_manifest(files=[])
        current = self._create_v2025_manifest(
            [{"path": "new_script.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": False}]
        )

        diff = _compute_diff_manifest(parent, current, preserve_runnable=True)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "new_script.sh"
        assert diff.paths[0].runnable is False  # New file uses current's value

    def test_preserve_runnable_multiple_files(self) -> None:
        """preserve_runnable works correctly with multiple modified files."""
        parent = self._create_v2025_manifest(
            [
                {"path": "script1.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": True},
                {"path": "script2.sh", "hash": "h2", "size": 100, "mtime": 1000, "runnable": True},
                {"path": "data.txt", "hash": "h3", "size": 100, "mtime": 1000, "runnable": False},
            ]
        )
        current = self._create_v2025_manifest(
            [
                # Modified - runnable should be preserved from parent
                {
                    "path": "script1.sh",
                    "hash": "h1a",
                    "size": 100,
                    "mtime": 2000,
                    "runnable": False,
                },
                # Unchanged - not in diff
                {"path": "script2.sh", "hash": "h2", "size": 100, "mtime": 1000, "runnable": True},
                # Modified - runnable should be preserved from parent (False)
                {"path": "data.txt", "hash": "h3a", "size": 100, "mtime": 2000, "runnable": False},
            ]
        )

        diff = _compute_diff_manifest(parent, current, preserve_runnable=True)

        paths_by_name = {p.path: p for p in diff.paths}
        assert len(paths_by_name) == 2  # Only modified files

        assert paths_by_name["script1.sh"].runnable is True  # Preserved from parent
        assert paths_by_name["data.txt"].runnable is False  # Preserved from parent (was False)

    def test_preserve_runnable_with_new_and_modified(self) -> None:
        """preserve_runnable correctly handles mix of new and modified files."""
        parent = self._create_v2025_manifest(
            [{"path": "existing.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": True}]
        )
        current = self._create_v2025_manifest(
            [
                # Modified - runnable preserved from parent
                {
                    "path": "existing.sh",
                    "hash": "h2",
                    "size": 100,
                    "mtime": 2000,
                    "runnable": False,
                },
                # New - uses current's runnable
                {"path": "new.sh", "hash": "h3", "size": 50, "mtime": 2000, "runnable": False},
            ]
        )

        diff = _compute_diff_manifest(parent, current, preserve_runnable=True)

        paths_by_name = {p.path: p for p in diff.paths}
        assert paths_by_name["existing.sh"].runnable is True  # Preserved from parent
        assert paths_by_name["new.sh"].runnable is False  # New file, uses current

    def test_preserve_runnable_parent_false_current_true(self) -> None:
        """preserve_runnable preserves False from parent even if current is True."""
        parent = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000, "runnable": False}]
        )
        current = self._create_v2025_manifest(
            [{"path": "file.txt", "hash": "h2", "size": 100, "mtime": 2000, "runnable": True}]
        )

        diff = _compute_diff_manifest(parent, current, preserve_runnable=True)

        assert diff.paths[0].runnable is False  # Preserved from parent

    def test_preserve_runnable_chunked_files(self) -> None:
        """preserve_runnable works with chunked (large) files."""
        parent = self._create_v2025_manifest(
            [
                {
                    "path": "large_script.sh",
                    "chunkhashes": ["c1", "c2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                    "runnable": True,
                }
            ]
        )
        current = self._create_v2025_manifest(
            [
                {
                    "path": "large_script.sh",
                    "chunkhashes": ["c1", "c3"],  # Second chunk changed
                    "size": 512 * 1024 * 1024,
                    "mtime": 2000,
                    "runnable": False,
                }
            ]
        )

        diff = _compute_diff_manifest(parent, current, preserve_runnable=True)

        assert diff.paths[0].runnable is True  # Preserved from parent
        assert diff.paths[0].chunkhashes == ["c1", "c3"]  # Content from current

    def test_preserve_runnable_default_is_false(self) -> None:
        """Default behavior (no preserve_runnable arg) uses current's runnable."""
        parent = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": True}]
        )
        current = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "h2", "size": 100, "mtime": 2000, "runnable": False}]
        )

        # Call without preserve_runnable argument
        diff = _compute_diff_manifest(parent, current)

        assert diff.paths[0].runnable is False  # Default behavior uses current

    def test_preserve_runnable_v2023_ignored(self) -> None:
        """preserve_runnable has no effect on v2023 (which doesn't have runnable)."""
        parent = AssetManifest2023(
            hash_alg=HashAlgorithm.XXH128,
            paths=[ManifestPath2023(path="file.txt", hash="h1", size=100, mtime=1000)],
            total_size=100,
        )
        current = AssetManifest2023(
            hash_alg=HashAlgorithm.XXH128,
            paths=[ManifestPath2023(path="file.txt", hash="h2", size=100, mtime=2000)],
            total_size=100,
        )

        # Should not raise, just ignored for v2023
        diff = _compute_diff_manifest(parent, current, preserve_runnable=True)

        assert len(diff.paths) == 1
        assert diff.paths[0].path == "file.txt"

    def test_preserve_runnable_only_runnable_changed_not_in_diff(self) -> None:
        """When only runnable changed and preserve_runnable=True, file is not in diff."""
        parent = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": True}]
        )
        current = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": False}]
        )

        diff = _compute_diff_manifest(parent, current, preserve_runnable=True)

        # File should NOT be in diff since only runnable changed and we're preserving it
        assert len(diff.paths) == 0

    def test_preserve_runnable_only_runnable_changed_in_diff_when_false(self) -> None:
        """When only runnable changed and preserve_runnable=False, file IS in diff."""
        parent = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": True}]
        )
        current = self._create_v2025_manifest(
            [{"path": "script.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": False}]
        )

        diff = _compute_diff_manifest(parent, current, preserve_runnable=False)

        # File SHOULD be in diff since runnable changed and we're not preserving
        assert len(diff.paths) == 1
        assert diff.paths[0].runnable is False
