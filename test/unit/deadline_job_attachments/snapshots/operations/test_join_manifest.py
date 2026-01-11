# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for join_manifest and related functions.

These tests cover:
- Basic prefix joining for unified manifest classes
- Relative and absolute prefix handling
- Symlink target prefixing
- Directory prefixing
- Output type determination based on prefix (absolute vs relative)
- Edge cases (empty prefix, special characters, etc.)
"""

import os
import pytest
from typing import List
from unittest.mock import patch

from deadline.job_attachments._snapshots import (
    join_manifest,
)
from deadline.job_attachments._snapshots._operations._join_manifest import (
    _normalize_prefix,
    _join_path,
    _is_absolute_path,
    _get_output_manifest_type,
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


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_normalize_prefix_removes_trailing_slash(self) -> None:
        """Trailing slashes are removed."""
        assert _normalize_prefix("assets/textures/") == "assets/textures"
        assert _normalize_prefix("/projects/scene/") == "/projects/scene"

    def test_normalize_prefix_preserves_leading_slash(self) -> None:
        """Leading slash for absolute paths is preserved."""
        assert _normalize_prefix("/projects/scene") == "/projects/scene"

    def test_join_path_relative(self) -> None:
        """Relative paths are joined correctly."""
        assert _join_path("assets/textures", "wood.png") == "assets/textures/wood.png"
        assert _join_path("a/b", "c/d.txt") == "a/b/c/d.txt"

    @patch.object(os, "name", "posix")
    def test_join_path_absolute_posix(self) -> None:
        """Absolute POSIX prefix produces absolute paths."""
        assert _join_path("/projects/scene", "wood.png") == "/projects/scene/wood.png"
        assert _join_path("/a/b", "c/d.txt") == "/a/b/c/d.txt"

    @patch.object(os, "name", "nt")
    def test_join_path_absolute_windows(self) -> None:
        """Absolute Windows prefix produces absolute paths."""
        assert _join_path("C:/projects/scene", "wood.png") == "C:/projects/scene/wood.png"
        assert _join_path("C:/a/b", "c/d.txt") == "C:/a/b/c/d.txt"
        assert _join_path("//server/share", "file.txt") == "//server/share/file.txt"

    def test_is_absolute_path_posix(self) -> None:
        """POSIX absolute paths are detected."""
        assert _is_absolute_path("/home/user/file.txt") is True
        assert _is_absolute_path("/") is True

    def test_is_absolute_path_windows_drive(self) -> None:
        """Windows drive letter paths are detected."""
        with patch("os.name", "nt"):
            assert _is_absolute_path("C:/Users/file.txt") is True
            assert _is_absolute_path("D:/Projects/file.txt") is True

    def test_is_absolute_path_windows_unc(self) -> None:
        """Windows UNC paths are detected."""
        assert _is_absolute_path("//server/share/file.txt") is True

    def test_is_absolute_path_relative(self) -> None:
        """Relative paths are not absolute."""
        assert _is_absolute_path("assets/file.txt") is False
        assert _is_absolute_path("file.txt") is False
        assert _is_absolute_path("./file.txt") is False
        assert _is_absolute_path("../file.txt") is False

    def test_get_output_manifest_type_rel_snapshot_rel_prefix(self) -> None:
        """Snapshot + relative prefix -> Snapshot."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[ManifestFilePath(path="a.txt", hash="h1", size=10, mtime=1000)],
            total_size=10,
        )
        result = _get_output_manifest_type(manifest, "prefix")
        assert result is Snapshot

    def test_get_output_manifest_type_rel_snapshot_abs_prefix(self) -> None:
        """Snapshot + absolute prefix -> AbsSnapshot."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[ManifestFilePath(path="a.txt", hash="h1", size=10, mtime=1000)],
            total_size=10,
        )
        result = _get_output_manifest_type(manifest, "/prefix")
        assert result is AbsSnapshot

    def test_get_output_manifest_type_rel_diff_rel_prefix(self) -> None:
        """SnapshotDiff + relative prefix -> SnapshotDiff."""
        manifest = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[ManifestFilePath(path="a.txt", hash="h1", size=10, mtime=1000)],
            total_size=10,
        )
        result = _get_output_manifest_type(manifest, "prefix")
        assert result is SnapshotDiff

    def test_get_output_manifest_type_rel_diff_abs_prefix(self) -> None:
        """SnapshotDiff + absolute prefix -> AbsSnapshotDiff."""
        manifest = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[ManifestFilePath(path="a.txt", hash="h1", size=10, mtime=1000)],
            total_size=10,
        )
        result = _get_output_manifest_type(manifest, "/prefix")
        assert result is AbsSnapshotDiff


class TestJoinManifestRelSnapshot:
    """Tests for Snapshot joining."""

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

    def test_basic_join_relative_prefix(self) -> None:
        """Basic join with relative prefix works."""
        manifest = self._create_manifest(
            files=[
                {"path": "wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "metal.png", "hash": "h2", "size": 200, "mtime": 2000},
            ],
            dirs=[{"path": "sub"}],
        )

        result = join_manifest(manifest, "assets/textures")

        file_paths = {p.path for p in result.files}
        assert file_paths == {"assets/textures/wood.png", "assets/textures/metal.png"}

        dir_paths = {d.path for d in result.dirs}
        assert dir_paths == {"assets/textures/sub"}

    def test_symlink_targets_prefixed(self) -> None:
        """Symlink targets are also prefixed."""
        manifest = self._create_manifest(
            files=[
                {"path": "wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "current", "symlink_target": "wood.png"},
            ],
        )

        result = join_manifest(manifest, "assets/textures")

        paths_by_name = {p.path: p for p in result.files}

        # File path is prefixed
        assert "assets/textures/wood.png" in paths_by_name

        # Symlink path and target are both prefixed
        assert "assets/textures/current" in paths_by_name
        assert paths_by_name["assets/textures/current"].symlink_target == "assets/textures/wood.png"

    def test_preserves_file_metadata(self) -> None:
        """File metadata is preserved after joining."""
        manifest = self._create_manifest(
            files=[{"path": "wood.png", "hash": "hash1", "size": 100, "mtime": 1000}]
        )

        result = join_manifest(manifest, "prefix")

        assert len(result.files) == 1
        entry = result.files[0]
        assert entry.path == "prefix/wood.png"
        assert entry.hash == "hash1"
        assert entry.size == 100
        assert entry.mtime == 1000

    def test_preserves_total_size(self) -> None:
        """Total size is preserved."""
        manifest = self._create_manifest(
            files=[
                {"path": "a.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "b.txt", "hash": "h2", "size": 200, "mtime": 2000},
            ]
        )

        result = join_manifest(manifest, "prefix")

        assert result.totalSize == 300

    def test_preserves_runnable_flag(self) -> None:
        """Runnable flag is preserved."""
        manifest = self._create_manifest(
            files=[
                {"path": "script.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": True}
            ]
        )

        result = join_manifest(manifest, "prefix")

        assert result.files[0].runnable is True

    def test_preserves_chunkhashes(self) -> None:
        """Chunkhashes are preserved for large files."""
        manifest = self._create_manifest(
            files=[
                {
                    "path": "large.bin",
                    "chunkhashes": ["c1", "c2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                }
            ]
        )

        result = join_manifest(manifest, "prefix")

        assert result.files[0].chunkhashes == ["c1", "c2"]

    def test_returns_rel_snapshot_manifest(self) -> None:
        """Joining Snapshot returns Snapshot."""
        manifest = self._create_manifest(
            files=[{"path": "a.txt", "hash": "h1", "size": 10, "mtime": 1000}]
        )

        result = join_manifest(manifest, "prefix")

        assert isinstance(result, Snapshot)


class TestJoinManifestAbsSnapshot:
    """Tests for joining with absolute prefix to produce AbsSnapshot."""

    def test_basic_join_absolute_prefix(self) -> None:
        """Basic join with absolute prefix produces AbsSnapshot."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[ManifestDirectoryPath(path="old/sub")],
            files=[ManifestFilePath(path="old/wood.png", hash="h1", size=100, mtime=1000)],
            total_size=100,
        )

        result = join_manifest(manifest, "/projects/scene")

        file_paths = {p.path for p in result.files}
        assert file_paths == {"/projects/scene/old/wood.png"}

        dir_paths = {d.path for d in result.dirs}
        assert dir_paths == {"/projects/scene/old/sub"}

        assert isinstance(result, AbsSnapshot)

    def test_symlink_targets_prefixed_absolute(self) -> None:
        """Symlink targets are prefixed with absolute prefix."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(path="data/wood.png", hash="h1", size=100, mtime=1000),
                ManifestFilePath(path="data/current", symlink_target="data/wood.png"),
            ],
            total_size=100,
        )

        result = join_manifest(manifest, "/projects/scene")

        paths_by_name = {p.path: p for p in result.files}
        assert (
            paths_by_name["/projects/scene/data/current"].symlink_target
            == "/projects/scene/data/wood.png"
        )

    def test_returns_abs_snapshot_manifest(self) -> None:
        """Joining with absolute prefix returns AbsSnapshot."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[ManifestFilePath(path="a.txt", hash="h1", size=10, mtime=1000)],
            total_size=10,
        )

        result = join_manifest(manifest, "/prefix")

        assert isinstance(result, AbsSnapshot)


class TestJoinManifestDiff:
    """Tests for diff manifest joining."""

    def test_preserves_deleted_markers(self) -> None:
        """Deleted markers are preserved and prefixed."""
        manifest = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[ManifestDirectoryPath(path="old_dir", deleted=True)],
            files=[ManifestFilePath(path="old.txt", deleted=True)],
            total_size=0,
        )

        result = join_manifest(manifest, "prefix")

        assert result.files[0].path == "prefix/old.txt"
        assert result.files[0].deleted is True

        assert result.dirs[0].path == "prefix/old_dir"
        assert result.dirs[0].deleted is True

    def test_preserves_parent_manifest_hash(self) -> None:
        """Parent manifest hash is preserved."""
        manifest = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[ManifestFilePath(path="file.txt", hash="h1", size=100, mtime=1000)],
            total_size=100,
            parent_manifest_hash="parent_hash_123",
        )

        result = join_manifest(manifest, "prefix")

        assert result.parentManifestHash == "parent_hash_123"

    def test_returns_rel_diff_manifest_with_rel_prefix(self) -> None:
        """Joining SnapshotDiff with relative prefix returns SnapshotDiff."""
        manifest = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[ManifestFilePath(path="a.txt", hash="h1", size=10, mtime=1000)],
            total_size=10,
        )

        result = join_manifest(manifest, "prefix")

        assert isinstance(result, SnapshotDiff)

    def test_returns_abs_diff_manifest_with_abs_prefix(self) -> None:
        """Joining SnapshotDiff with absolute prefix returns AbsSnapshotDiff."""
        manifest = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[ManifestFilePath(path="a.txt", hash="h1", size=10, mtime=1000)],
            total_size=10,
        )

        result = join_manifest(manifest, "/prefix")

        assert isinstance(result, AbsSnapshotDiff)


class TestJoinManifestValidation:
    """Tests for validation and error handling."""

    def test_empty_prefix_raises_error(self) -> None:
        """Empty prefix raises ValueError."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[ManifestFilePath(path="file.txt", hash="h1", size=100, mtime=1000)],
            total_size=100,
        )

        with pytest.raises(ValueError, match="cannot be empty"):
            join_manifest(manifest, "")


class TestJoinManifestWindowsPaths:
    """Tests for Windows-style paths."""

    def test_windows_absolute_prefix(self) -> None:
        """Windows-style absolute prefix works."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[ManifestFilePath(path="wood.png", hash="h1", size=100, mtime=1000)],
            total_size=100,
        )

        result = join_manifest(manifest, "C:/projects/scene")

        assert result.files[0].path == "C:/projects/scene/wood.png"

    def test_windows_backslash_prefix_normalized(self) -> None:
        """Windows backslash prefix is normalized to forward slashes."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[ManifestFilePath(path="wood.png", hash="h1", size=100, mtime=1000)],
            total_size=100,
        )

        with patch("os.name", "nt"):
            result = join_manifest(manifest, "C:\\projects\\scene")

            # Backslashes should be converted to forward slashes
            assert result.files[0].path == "C:/projects/scene/wood.png"


class TestPathSeparatorHandling:
    """Tests for path separator handling across platforms."""

    def test_normalize_prefix_converts_backslashes_on_windows(self) -> None:
        """On Windows, backslashes in prefix are converted to forward slashes."""
        with patch("os.name", "nt"):
            result = _normalize_prefix("C:\\projects\\scene")
            assert result == "C:/projects/scene"

    def test_normalize_prefix_preserves_backslashes_on_posix(self) -> None:
        """On POSIX, backslashes in prefix are preserved as valid filename characters."""
        with patch("os.name", "posix"):
            result = _normalize_prefix("dir\\name")
            assert result == "dir\\name"
