# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for _join_manifest and related functions.

These tests cover:
- Basic prefix joining for both v2023 and v2025 formats
- Relative and absolute prefix handling
- Symlink target prefixing (v2025 only)
- Directory prefixing (v2025 only)
- Edge cases (empty prefix, special characters, etc.)
"""

import os
import pytest
from typing import List

from deadline.job_attachments.asset_manifests._operations._join_manifest import (
    _join_manifest,
    _normalize_prefix,
    _join_path,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.v2023_03_03.asset_manifest import (
    AssetManifest as AssetManifest2023,
    ManifestPath as ManifestPath2023,
)
from deadline.job_attachments.asset_manifests.v2025_12_04.asset_manifest import (
    AssetManifest as AssetManifest2025,
    ManifestDirectoryPath as ManifestDirectoryPath2025,
    ManifestFilePath as ManifestFilePath2025,
)


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_normalize_prefix_removes_trailing_slash(self) -> None:
        """Trailing slashes are removed."""
        assert _normalize_prefix("assets/textures/") == "assets/textures"
        assert _normalize_prefix("/projects/scene/") == "/projects/scene"

    def test_normalize_prefix_converts_backslashes(self) -> None:
        """Backslashes are converted to forward slashes."""
        assert _normalize_prefix("assets\\textures") == "assets/textures"
        assert _normalize_prefix("C:\\projects\\scene") == "C:/projects/scene"

    def test_normalize_prefix_preserves_leading_slash(self) -> None:
        """Leading slash for absolute paths is preserved."""
        assert _normalize_prefix("/projects/scene") == "/projects/scene"

    def test_join_path_relative(self) -> None:
        """Relative paths are joined correctly."""
        assert _join_path("assets/textures", "wood.png") == "assets/textures/wood.png"
        assert _join_path("a/b", "c/d.txt") == "a/b/c/d.txt"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX-only test")
    def test_join_path_absolute_posix(self) -> None:
        """Absolute POSIX prefix produces absolute paths."""
        assert _join_path("/projects/scene", "wood.png") == "/projects/scene/wood.png"
        assert _join_path("/a/b", "c/d.txt") == "/a/b/c/d.txt"

    @pytest.mark.skipif(os.name != "nt", reason="Windows-only test")
    def test_join_path_absolute_windows(self) -> None:
        """Absolute Windows prefix produces absolute paths."""
        assert _join_path("C:/projects/scene", "wood.png") == "C:/projects/scene/wood.png"
        assert _join_path("C:/a/b", "c/d.txt") == "C:/a/b/c/d.txt"
        assert _join_path("//server/share", "file.txt") == "//server/share/file.txt"


class TestJoinManifestV2023:
    """Tests for v2023-03-03 manifest joining."""

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

    def test_basic_join_relative_prefix(self) -> None:
        """Basic join with relative prefix works."""
        manifest = self._create_v2023_manifest(
            [
                ("wood.png", "hash1", 100, 1000),
                ("metal.png", "hash2", 200, 2000),
            ]
        )

        result = _join_manifest(manifest, "assets/textures")

        paths = {p.path for p in result.paths}
        assert paths == {"assets/textures/wood.png", "assets/textures/metal.png"}

    def test_basic_join_absolute_prefix(self) -> None:
        """Basic join with absolute prefix produces absolute paths."""
        manifest = self._create_v2023_manifest(
            [
                ("wood.png", "hash1", 100, 1000),
                ("sub/metal.png", "hash2", 200, 2000),
            ]
        )

        result = _join_manifest(manifest, "/projects/scene/assets")

        paths = {p.path for p in result.paths}
        assert paths == {
            "/projects/scene/assets/wood.png",
            "/projects/scene/assets/sub/metal.png",
        }

    def test_preserves_file_metadata(self) -> None:
        """File metadata is preserved after joining."""
        manifest = self._create_v2023_manifest(
            [
                ("wood.png", "hash1", 100, 1000),
            ]
        )

        result = _join_manifest(manifest, "prefix")

        assert len(result.paths) == 1
        entry = result.paths[0]
        assert entry.path == "prefix/wood.png"
        assert entry.hash == "hash1"
        assert entry.size == 100
        assert entry.mtime == 1000

    def test_preserves_total_size(self) -> None:
        """Total size is preserved."""
        manifest = self._create_v2023_manifest(
            [
                ("a.txt", "h1", 100, 1000),
                ("b.txt", "h2", 200, 2000),
            ]
        )

        result = _join_manifest(manifest, "prefix")

        assert result.totalSize == 300


class TestJoinManifestV2025:
    """Tests for v2025-12-04-beta manifest joining."""

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

    def test_basic_join_relative_prefix(self) -> None:
        """Basic join with relative prefix works."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "metal.png", "hash": "h2", "size": 200, "mtime": 2000},
            ],
            dirs=[{"path": "sub"}],
        )

        result = _join_manifest(manifest, "assets/textures")

        file_paths = {p.path for p in result.paths}
        assert file_paths == {"assets/textures/wood.png", "assets/textures/metal.png"}

        dir_paths = {d.path for d in result.dirs}
        assert dir_paths == {"assets/textures/sub"}

    def test_basic_join_absolute_prefix(self) -> None:
        """Basic join with absolute prefix produces absolute paths."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "wood.png", "hash": "h1", "size": 100, "mtime": 1000},
            ],
            dirs=[{"path": "sub"}],
        )

        result = _join_manifest(manifest, "/projects/scene")

        file_paths = {p.path for p in result.paths}
        assert file_paths == {"/projects/scene/wood.png"}

        dir_paths = {d.path for d in result.dirs}
        assert dir_paths == {"/projects/scene/sub"}

    def test_symlink_targets_prefixed(self) -> None:
        """Symlink targets are also prefixed."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "current", "symlink_target": "wood.png"},
            ],
        )

        result = _join_manifest(manifest, "assets/textures")

        paths_by_name = {p.path: p for p in result.paths}

        # File path is prefixed
        assert "assets/textures/wood.png" in paths_by_name

        # Symlink path and target are both prefixed
        assert "assets/textures/current" in paths_by_name
        assert paths_by_name["assets/textures/current"].symlink_target == "assets/textures/wood.png"

    def test_symlink_targets_prefixed_absolute(self) -> None:
        """Symlink targets are prefixed with absolute prefix."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "current", "symlink_target": "wood.png"},
            ],
        )

        result = _join_manifest(manifest, "/projects/scene")

        paths_by_name = {p.path: p for p in result.paths}
        assert paths_by_name["/projects/scene/current"].symlink_target == "/projects/scene/wood.png"

    def test_preserves_runnable_flag(self) -> None:
        """Runnable flag is preserved."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "script.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": True},
            ],
        )

        result = _join_manifest(manifest, "prefix")

        assert result.paths[0].runnable is True

    def test_preserves_chunkhashes(self) -> None:
        """Chunkhashes are preserved for large files."""
        manifest = self._create_v2025_manifest(
            files=[
                {
                    "path": "large.bin",
                    "chunkhashes": ["c1", "c2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                },
            ],
        )

        result = _join_manifest(manifest, "prefix")

        assert result.paths[0].chunkhashes == ["c1", "c2"]

    def test_preserves_deleted_markers(self) -> None:
        """Deleted markers are preserved and prefixed."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "old.txt", "deleted": True},
            ],
            dirs=[
                {"path": "old_dir", "deleted": True},
            ],
        )

        result = _join_manifest(manifest, "prefix")

        assert result.paths[0].path == "prefix/old.txt"
        assert result.paths[0].deleted is True

        assert result.dirs[0].path == "prefix/old_dir"
        assert result.dirs[0].deleted is True

    def test_preserves_manifest_type(self) -> None:
        """Manifest type is preserved."""
        from deadline.job_attachments.asset_manifests.versions import ManifestType

        manifest = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[ManifestFilePath2025(path="file.txt", hash="h1", size=100, mtime=1000)],
            total_size=100,
            manifest_type=ManifestType.DIFF,
            parent_manifest_hash="parent_hash_123",
        )

        result = _join_manifest(manifest, "prefix")

        assert result.manifestType == ManifestType.DIFF
        assert result.parentManifestHash == "parent_hash_123"


class TestJoinManifestValidation:
    """Tests for validation and error handling."""

    def _create_v2025_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AssetManifest2025:
        """Helper to create a v2025 manifest."""
        file_entries = [ManifestFilePath2025(**f) for f in files]
        dir_entries = [ManifestDirectoryPath2025(**d) for d in (dirs or [])]
        return AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=0,
        )

    def test_empty_prefix_raises_error(self) -> None:
        """Empty prefix raises ValueError."""
        manifest = self._create_v2025_manifest(
            files=[{"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        with pytest.raises(ValueError, match="cannot be empty"):
            _join_manifest(manifest, "")


class TestJoinManifestWindowsPaths:
    """Tests for Windows-style paths."""

    def _create_v2025_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AssetManifest2025:
        """Helper to create a v2025 manifest."""
        file_entries = [ManifestFilePath2025(**f) for f in files]
        dir_entries = [ManifestDirectoryPath2025(**d) for d in (dirs or [])]
        return AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=0,
        )

    def test_windows_absolute_prefix(self) -> None:
        """Windows-style absolute prefix works."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "wood.png", "hash": "h1", "size": 100, "mtime": 1000},
            ],
        )

        result = _join_manifest(manifest, "C:/projects/scene")

        assert result.paths[0].path == "C:/projects/scene/wood.png"

    def test_windows_backslash_prefix_normalized(self) -> None:
        """Windows backslash prefix is normalized to forward slashes."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "wood.png", "hash": "h1", "size": 100, "mtime": 1000},
            ],
        )

        result = _join_manifest(manifest, "C:\\projects\\scene")

        # Backslashes should be converted to forward slashes
        assert result.paths[0].path == "C:/projects/scene/wood.png"


class TestPathSeparatorHandling:
    """Tests for path separator handling across platforms.

    These tests verify that:
    - On Windows: backslashes in prefix are converted to forward slashes
    - On POSIX: backslashes are preserved as valid filename characters
    """

    def _create_v2025_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AssetManifest2025:
        """Helper to create a v2025 manifest."""
        file_entries = [ManifestFilePath2025(**f) for f in files]
        dir_entries = [ManifestDirectoryPath2025(**d) for d in (dirs or [])]
        return AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=0,
        )

    def test_normalize_prefix_converts_backslashes_on_windows(self) -> None:
        """On Windows, backslashes in prefix are converted to forward slashes."""
        from unittest.mock import patch

        with patch("deadline.job_attachments.asset_manifests._operations._join_manifest.os.name", "nt"):
            result = _normalize_prefix("C:\\projects\\scene")
            # On Windows, backslashes should be converted to forward slashes
            assert result == "C:/projects/scene"

    def test_normalize_prefix_preserves_backslashes_on_posix(self) -> None:
        """On POSIX, backslashes in prefix are preserved as valid filename characters."""
        from unittest.mock import patch

        with patch("deadline.job_attachments.asset_manifests._operations._join_manifest.os.name", "posix"):
            # On POSIX, a backslash is a valid filename character
            # "dir\\name" is a single directory name containing a backslash
            result = _normalize_prefix("dir\\name")
            # On POSIX, backslashes should NOT be converted - they're valid filename chars
            assert result == "dir\\name"

    def test_join_with_backslash_prefix_on_windows(self) -> None:
        """On Windows, backslashes in prefix are normalized to forward slashes."""
        from unittest.mock import patch

        with patch("deadline.job_attachments.asset_manifests.base_manifest.os.name", "nt"), \
             patch("deadline.job_attachments.asset_manifests._operations._join_manifest.os.name", "nt"):
            manifest = self._create_v2025_manifest(
                files=[
                    {"path": "wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                ],
            )

            result = _join_manifest(manifest, "C:\\projects\\scene")

            assert result.paths[0].path == "C:/projects/scene/wood.png"

    def test_join_with_backslash_prefix_on_posix(self) -> None:
        """On POSIX, backslashes in prefix are preserved as valid directory name characters."""
        from unittest.mock import patch

        with patch("deadline.job_attachments.asset_manifests.base_manifest.os.name", "posix"), \
             patch("deadline.job_attachments.asset_manifests._operations._join_manifest.os.name", "posix"):
            manifest = self._create_v2025_manifest(
                files=[
                    {"path": "wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                ],
            )

            # On POSIX, "dir\\name" is a single directory name containing a backslash
            result = _join_manifest(manifest, "dir\\name")

            # The backslash should be preserved - it's part of the directory name
            assert result.paths[0].path == "dir\\name/wood.png"

    def test_join_preserves_backslash_filenames_on_posix(self) -> None:
        """On POSIX, files with backslashes in names are handled correctly."""
        from unittest.mock import patch

        # Patch os.name in base_manifest for manifest creation
        with patch("deadline.job_attachments.asset_manifests.base_manifest.os.name", "posix"), \
             patch("deadline.job_attachments.asset_manifests._operations._join_manifest.os.name", "posix"):
            # Create a manifest with a file that has a backslash in its name (valid on POSIX)
            manifest = self._create_v2025_manifest(
                files=[
                    # A file named "file\with\backslashes.txt" (single filename with backslashes)
                    {"path": "file\\with\\backslashes.txt", "hash": "h1", "size": 100, "mtime": 1000},
                ],
            )

            result = _join_manifest(manifest, "prefix")

            # The backslash filename should be preserved as-is
            assert result.paths[0].path == "prefix/file\\with\\backslashes.txt"
