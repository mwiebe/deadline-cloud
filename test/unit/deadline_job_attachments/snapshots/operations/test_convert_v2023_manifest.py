# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for _convert_v2023_manifest module.

These tests cover:
- Converting Snapshot to v2023 AssetManifest
- Converting SnapshotDiff to v2023 AssetManifest
- Converting v2023 AssetManifest to Snapshot
- Converting v2023 AssetManifest to SnapshotDiff
- Warning messages for dropped features (empty dirs, deletions)
- Error handling for invalid inputs
"""

import logging
import pytest

from deadline.job_attachments._snapshots import (
    Snapshot,
    SnapshotDiff,
    ManifestFilePath,
    ManifestDirectoryPath,
    WHOLE_FILE_CHUNK_SIZE,
    snapshot_to_v2023_manifest,
    snapshot_diff_to_v2023_manifest,
    v2023_manifest_to_snapshot,
    v2023_manifest_to_snapshot_diff,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.v2023_03_03 import AssetManifest, ManifestPath


class TestSnapshotToV2023Manifest:
    """Tests for snapshot_to_v2023_manifest."""

    def test_basic_conversion(self) -> None:
        """Basic snapshot converts to v2023 manifest."""
        snapshot = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="file1.txt", hash="abc123", size=100, mtime=1000),
                ManifestFilePath(path="dir/file2.txt", hash="def456", size=200, mtime=2000),
            ],
            total_size=300,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = snapshot_to_v2023_manifest(snapshot)

        assert isinstance(result, AssetManifest)
        assert result.hashAlg == HashAlgorithm.XXH128
        assert result.totalSize == 300
        assert len(result.paths) == 2

        paths_by_name = {p.path: p for p in result.paths}
        assert paths_by_name["file1.txt"].hash == "abc123"
        assert paths_by_name["file1.txt"].size == 100
        assert paths_by_name["dir/file2.txt"].hash == "def456"

    def test_symlinks_collapsed(self) -> None:
        """Symlinks are collapsed to their target files."""
        snapshot = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="target.txt", hash="abc123", size=100, mtime=1000),
                ManifestFilePath(path="link.txt", symlink_target="target.txt"),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = snapshot_to_v2023_manifest(snapshot)

        assert len(result.paths) == 2
        paths_by_name = {p.path: p for p in result.paths}
        # The symlink should be collapsed to a file with the target's content
        assert paths_by_name["link.txt"].hash == "abc123"
        assert paths_by_name["link.txt"].size == 100

    def test_empty_directories_dropped_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Truly empty directories (not implied by files) are dropped with a warning."""
        snapshot = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="other/file.txt", hash="abc123", size=100, mtime=1000),
            ],
            dirs=[
                ManifestDirectoryPath(path="other"),  # Implied by file, no warning
                ManifestDirectoryPath(path="empty_dir"),  # Truly empty, should warn
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        with caplog.at_level(logging.WARNING):
            result = snapshot_to_v2023_manifest(snapshot)

        assert len(result.paths) == 1
        assert "Dropping 1 empty directories" in caplog.text
        assert "empty_dir" in caplog.text
        assert "other" not in caplog.text  # Implied dir should not be in warning

    def test_implied_directories_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Directories implied by file paths do not trigger a warning."""
        snapshot = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="a/b/c/file.txt", hash="abc123", size=100, mtime=1000),
            ],
            dirs=[
                ManifestDirectoryPath(path="a"),
                ManifestDirectoryPath(path="a/b"),
                ManifestDirectoryPath(path="a/b/c"),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        with caplog.at_level(logging.WARNING):
            result = snapshot_to_v2023_manifest(snapshot)

        assert len(result.paths) == 1
        assert "empty directories" not in caplog.text

    def test_missing_hash_raises_error(self) -> None:
        """Files without hashes raise ValueError."""
        snapshot = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="file.txt", hash=None, size=100, mtime=1000),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        with pytest.raises(ValueError, match="missing a hash"):
            snapshot_to_v2023_manifest(snapshot)

    def test_non_whole_file_chunk_size_raises_error(self) -> None:
        """Snapshots with chunking enabled raise ValueError."""
        snapshot = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="file.txt", hash="abc123", size=100, mtime=1000),
            ],
            total_size=100,
            file_chunk_size_bytes=256 * 1024 * 1024,  # Default chunk size, not WHOLE_FILE
        )

        with pytest.raises(ValueError, match="fileChunkSizeBytes"):
            snapshot_to_v2023_manifest(snapshot)

    def test_empty_snapshot(self) -> None:
        """Empty snapshot converts to empty manifest."""
        snapshot = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            total_size=0,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = snapshot_to_v2023_manifest(snapshot)

        assert len(result.paths) == 0
        assert result.totalSize == 0


class TestSnapshotDiffToV2023Manifest:
    """Tests for snapshot_diff_to_v2023_manifest."""

    def test_basic_conversion(self) -> None:
        """Basic diff converts to v2023 manifest with only additions."""
        diff = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="new_file.txt", hash="abc123", size=100, mtime=1000),
                ManifestFilePath(path="modified.txt", hash="def456", size=200, mtime=2000),
            ],
            total_size=300,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = snapshot_diff_to_v2023_manifest(diff)

        assert isinstance(result, AssetManifest)
        assert len(result.paths) == 2
        assert result.totalSize == 300

    def test_deletions_dropped_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Deletions are dropped with a warning."""
        diff = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="new_file.txt", hash="abc123", size=100, mtime=1000),
                ManifestFilePath(path="deleted_file.txt", deleted=True),
            ],
            dirs=[
                ManifestDirectoryPath(path="deleted_dir", deleted=True),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        with caplog.at_level(logging.WARNING):
            result = snapshot_diff_to_v2023_manifest(diff)

        assert len(result.paths) == 1
        assert result.paths[0].path == "new_file.txt"
        assert "Dropping 2 deletions" in caplog.text

    def test_symlinks_collapsed(self) -> None:
        """Symlinks in diff are collapsed."""
        diff = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="target.txt", hash="abc123", size=100, mtime=1000),
                ManifestFilePath(path="link.txt", symlink_target="target.txt"),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = snapshot_diff_to_v2023_manifest(diff)

        assert len(result.paths) == 2
        paths_by_name = {p.path: p for p in result.paths}
        assert paths_by_name["link.txt"].hash == "abc123"

    def test_empty_directories_dropped_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Truly empty directories (not implied by files) in diff are dropped with warning."""
        diff = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="other/file.txt", hash="abc123", size=100, mtime=1000),
            ],
            dirs=[
                ManifestDirectoryPath(path="other"),  # Implied by file, no warning
                ManifestDirectoryPath(path="new_empty_dir"),  # Truly empty, should warn
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        with caplog.at_level(logging.WARNING):
            result = snapshot_diff_to_v2023_manifest(diff)

        assert len(result.paths) == 1
        assert "Dropping 1 empty directories" in caplog.text
        assert "new_empty_dir" in caplog.text
        assert "other" not in caplog.text

    def test_missing_hash_raises_error(self) -> None:
        """Non-deleted files without hashes raise ValueError."""
        diff = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="file.txt", hash=None, size=100, mtime=1000),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        with pytest.raises(ValueError, match="missing a hash"):
            snapshot_diff_to_v2023_manifest(diff)

    def test_non_whole_file_chunk_size_raises_error(self) -> None:
        """Diffs with chunking enabled raise ValueError."""
        diff = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="file.txt", hash="abc123", size=100, mtime=1000),
            ],
            total_size=100,
            file_chunk_size_bytes=256 * 1024 * 1024,  # Default chunk size, not WHOLE_FILE
        )

        with pytest.raises(ValueError, match="fileChunkSizeBytes"):
            snapshot_diff_to_v2023_manifest(diff)

    def test_only_deletions_produces_empty_manifest(self, caplog: pytest.LogCaptureFixture) -> None:
        """Diff with only deletions produces empty manifest."""
        diff = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="deleted1.txt", deleted=True),
                ManifestFilePath(path="deleted2.txt", deleted=True),
            ],
            total_size=0,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        with caplog.at_level(logging.WARNING):
            result = snapshot_diff_to_v2023_manifest(diff)

        assert len(result.paths) == 0
        assert result.totalSize == 0


class TestV2023ManifestToSnapshot:
    """Tests for v2023_manifest_to_snapshot."""

    def test_basic_conversion(self) -> None:
        """Basic v2023 manifest converts to snapshot."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            paths=[
                ManifestPath(path="file1.txt", hash="abc123", size=100, mtime=1000),
                ManifestPath(path="dir/file2.txt", hash="def456", size=200, mtime=2000),
            ],
            total_size=300,
        )

        result = v2023_manifest_to_snapshot(manifest)

        assert isinstance(result, Snapshot)
        assert result.hashAlg == HashAlgorithm.XXH128
        assert result.totalSize == 300
        assert result.fileChunkSizeBytes == WHOLE_FILE_CHUNK_SIZE
        assert len(result.files) == 2

        files_by_path = {f.path: f for f in result.files}
        assert files_by_path["file1.txt"].hash == "abc123"
        assert files_by_path["file1.txt"].size == 100
        assert files_by_path["dir/file2.txt"].hash == "def456"

    def test_empty_manifest(self) -> None:
        """Empty v2023 manifest converts to empty snapshot."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            paths=[],
            total_size=0,
        )

        result = v2023_manifest_to_snapshot(manifest)

        assert len(result.files) == 0
        assert result.totalSize == 0

    def test_preserves_all_fields(self) -> None:
        """All fields from v2023 manifest are preserved."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            paths=[
                ManifestPath(path="test.txt", hash="hash123", size=12345, mtime=9876543210),
            ],
            total_size=12345,
        )

        result = v2023_manifest_to_snapshot(manifest)

        assert len(result.files) == 1
        entry = result.files[0]
        assert entry.path == "test.txt"
        assert entry.hash == "hash123"
        assert entry.size == 12345
        assert entry.mtime == 9876543210
        assert entry.symlink_target is None
        assert entry.deleted is False
        assert entry.chunkhashes is None


class TestV2023ManifestToSnapshotDiff:
    """Tests for v2023_manifest_to_snapshot_diff."""

    def test_basic_conversion(self) -> None:
        """Basic v2023 manifest converts to snapshot diff."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            paths=[
                ManifestPath(path="new_file.txt", hash="abc123", size=100, mtime=1000),
            ],
            total_size=100,
        )

        result = v2023_manifest_to_snapshot_diff(manifest)

        assert isinstance(result, SnapshotDiff)
        assert result.hashAlg == HashAlgorithm.XXH128
        assert result.totalSize == 100
        assert result.fileChunkSizeBytes == WHOLE_FILE_CHUNK_SIZE
        assert len(result.files) == 1
        assert result.files[0].path == "new_file.txt"
        assert result.files[0].deleted is False

    def test_with_parent_manifest_hash(self) -> None:
        """Parent manifest hash is preserved."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            paths=[
                ManifestPath(path="file.txt", hash="abc123", size=100, mtime=1000),
            ],
            total_size=100,
        )

        result = v2023_manifest_to_snapshot_diff(manifest, parent_manifest_hash="parent_hash_xyz")

        assert result.parentManifestHash == "parent_hash_xyz"

    def test_without_parent_manifest_hash(self) -> None:
        """Missing parent manifest hash results in None."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            paths=[],
            total_size=0,
        )

        result = v2023_manifest_to_snapshot_diff(manifest)

        assert result.parentManifestHash is None

    def test_empty_manifest(self) -> None:
        """Empty v2023 manifest converts to empty diff."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            paths=[],
            total_size=0,
        )

        result = v2023_manifest_to_snapshot_diff(manifest)

        assert len(result.files) == 0
        assert result.totalSize == 0


class TestRoundTrip:
    """Tests for round-trip conversions."""

    def test_snapshot_roundtrip_preserves_data(self) -> None:
        """Converting snapshot to v2023 and back preserves file data."""
        original = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="file1.txt", hash="abc123", size=100, mtime=1000),
                ManifestFilePath(path="dir/file2.txt", hash="def456", size=200, mtime=2000),
            ],
            total_size=300,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        v2023 = snapshot_to_v2023_manifest(original)
        roundtrip = v2023_manifest_to_snapshot(v2023)

        assert roundtrip.hashAlg == original.hashAlg
        assert roundtrip.totalSize == original.totalSize
        assert roundtrip.fileChunkSizeBytes == WHOLE_FILE_CHUNK_SIZE
        assert len(roundtrip.files) == len(original.files)

        original_by_path = {f.path: f for f in original.files}
        for entry in roundtrip.files:
            orig = original_by_path[entry.path]
            assert entry.hash == orig.hash
            assert entry.size == orig.size
            assert entry.mtime == orig.mtime

    def test_v2023_roundtrip_preserves_data(self) -> None:
        """Converting v2023 to snapshot and back preserves all data."""
        original = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            paths=[
                ManifestPath(path="file1.txt", hash="abc123", size=100, mtime=1000),
                ManifestPath(path="dir/file2.txt", hash="def456", size=200, mtime=2000),
            ],
            total_size=300,
        )

        snapshot = v2023_manifest_to_snapshot(original)
        roundtrip = snapshot_to_v2023_manifest(snapshot)

        assert roundtrip.hashAlg == original.hashAlg
        assert roundtrip.totalSize == original.totalSize
        assert len(roundtrip.paths) == len(original.paths)

        original_by_path = {p.path: p for p in original.paths}
        for path_entry in roundtrip.paths:
            orig = original_by_path[path_entry.path]
            assert path_entry.hash == orig.hash
            assert path_entry.size == orig.size
            assert path_entry.mtime == orig.mtime
