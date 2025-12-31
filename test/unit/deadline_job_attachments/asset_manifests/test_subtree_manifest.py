# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for subtree_manifest and related functions.

These tests cover:
- Basic subtree extraction for both v2023 and v2025 formats
- Path rebasing (stripping subtree prefix)
- Path style validation (relative vs absolute)
- Symlink handling with different policies
- Directory handling (v2025 only)
- Edge cases (empty subtree, non-existent subtree, etc.)
"""

import os
import pytest
from typing import List
from unittest.mock import patch

from deadline.job_attachments.asset_manifests._operations import (
    subtree_manifest,
)
from deadline.job_attachments.asset_manifests._operations._subtree_manifest import (
    _is_absolute_path,
    _is_within_subtree,
    _rebase_path,
    _normalize_subtree_path,
)
from deadline.job_attachments.asset_manifests.versions import (
    SymlinkPolicy,
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

    def test_is_absolute_path_posix(self) -> None:
        """POSIX absolute paths are detected."""
        assert _is_absolute_path("/home/user/file.txt") is True
        assert _is_absolute_path("/") is True

    @patch.object(os, "name", "nt")
    def test_is_absolute_path_windows_drive(self) -> None:
        """Windows drive letter paths are detected on Windows."""
        assert _is_absolute_path("C:/Users/file.txt") is True
        assert _is_absolute_path("D:\\Projects\\file.txt") is True

    @patch.object(os, "name", "nt")
    def test_is_absolute_path_windows_unc(self) -> None:
        """Windows UNC paths are detected on Windows."""
        assert _is_absolute_path("//server/share/file.txt") is True
        assert _is_absolute_path("\\\\server\\share\\file.txt") is True

    def test_is_absolute_path_relative(self) -> None:
        """Relative paths are not absolute on any OS."""
        assert _is_absolute_path("assets/file.txt") is False
        assert _is_absolute_path("file.txt") is False
        assert _is_absolute_path("./file.txt") is False
        assert _is_absolute_path("../file.txt") is False

    @patch.object(os, "name", "posix")
    def test_windows_absolute_not_detected_on_posix(self) -> None:
        """Windows absolute paths are not detected as absolute on POSIX."""
        # On POSIX, C:/Users/file.txt is a relative path
        assert _is_absolute_path("C:/Users/file.txt") is False
        assert _is_absolute_path("D:\\Projects\\file.txt") is False

    def test_is_within_subtree_exact_match(self) -> None:
        """Exact match is within subtree."""
        assert _is_within_subtree("assets/textures", "assets/textures") is True

    def test_is_within_subtree_child(self) -> None:
        """Child paths are within subtree."""
        assert _is_within_subtree("assets/textures/wood.png", "assets/textures") is True
        assert _is_within_subtree("assets/textures/sub/file.png", "assets/textures") is True

    def test_is_within_subtree_not_within(self) -> None:
        """Paths outside subtree are not within."""
        assert _is_within_subtree("assets/models/chair.blend", "assets/textures") is False
        assert _is_within_subtree("other/file.txt", "assets/textures") is False

    def test_is_within_subtree_prefix_not_directory(self) -> None:
        """Path that starts with subtree but isn't a child is not within."""
        # "assets/textures2" starts with "assets/textures" but isn't a child
        assert _is_within_subtree("assets/textures2/file.png", "assets/textures") is False

    def test_rebase_path(self) -> None:
        """Paths are correctly rebased."""
        assert _rebase_path("assets/textures/wood.png", "assets/textures") == "wood.png"
        assert _rebase_path("assets/textures/sub/file.png", "assets/textures") == "sub/file.png"
        assert _rebase_path("a/b/c/d.txt", "a/b") == "c/d.txt"

    def test_normalize_subtree_path(self) -> None:
        """Subtree paths are normalized."""
        assert _normalize_subtree_path("assets/textures/") == "assets/textures"
        assert _normalize_subtree_path("assets//textures") == "assets/textures"
        assert _normalize_subtree_path("assets/./textures") == "assets/textures"


class TestSubtreeManifestV2023:
    """Tests for v2023-03-03 subtree extraction."""

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

    def test_basic_subtree_extraction(self) -> None:
        """Basic subtree extraction works."""
        manifest = self._create_v2023_manifest(
            [
                ("assets/textures/wood.png", "hash1", 100, 1000),
                ("assets/textures/metal.png", "hash2", 200, 2000),
                ("assets/models/chair.blend", "hash3", 300, 3000),
                ("scripts/render.py", "hash4", 50, 4000),
            ]
        )

        result = subtree_manifest(manifest, "assets/textures")

        assert len(result.paths) == 2
        paths = {p.path for p in result.paths}
        assert paths == {"wood.png", "metal.png"}

    def test_preserves_file_metadata(self) -> None:
        """File metadata is preserved after rebasing."""
        manifest = self._create_v2023_manifest(
            [
                ("assets/textures/wood.png", "hash1", 100, 1000),
            ]
        )

        result = subtree_manifest(manifest, "assets/textures")

        assert len(result.paths) == 1
        entry = result.paths[0]
        assert entry.path == "wood.png"
        assert entry.hash == "hash1"
        assert entry.size == 100
        assert entry.mtime == 1000

    def test_total_size_recalculated(self) -> None:
        """Total size is recalculated for subtree."""
        manifest = self._create_v2023_manifest(
            [
                ("assets/textures/wood.png", "hash1", 100, 1000),
                ("assets/textures/metal.png", "hash2", 200, 2000),
                ("assets/models/chair.blend", "hash3", 300, 3000),
            ]
        )

        result = subtree_manifest(manifest, "assets/textures")

        assert result.totalSize == 300  # 100 + 200

    def test_nested_subtree(self) -> None:
        """Nested subtree extraction works."""
        manifest = self._create_v2023_manifest(
            [
                ("a/b/c/d/file.txt", "hash1", 100, 1000),
                ("a/b/c/other.txt", "hash2", 200, 2000),
                ("a/b/outside.txt", "hash3", 300, 3000),
            ]
        )

        result = subtree_manifest(manifest, "a/b/c")

        assert len(result.paths) == 2
        paths = {p.path for p in result.paths}
        assert paths == {"d/file.txt", "other.txt"}

    def test_empty_result(self) -> None:
        """Subtree with no matching entries returns empty manifest."""
        manifest = self._create_v2023_manifest(
            [
                ("assets/models/chair.blend", "hash1", 100, 1000),
            ]
        )

        result = subtree_manifest(manifest, "assets/textures")

        assert len(result.paths) == 0
        assert result.totalSize == 0


class TestSubtreeManifestV2025:
    """Tests for v2025-12-04-beta subtree extraction."""

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

    def test_basic_subtree_extraction(self) -> None:
        """Basic subtree extraction works."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "assets/textures/wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "assets/textures/metal.png", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "assets/models/chair.blend", "hash": "h3", "size": 300, "mtime": 3000},
            ],
            dirs=[
                {"path": "assets"},
                {"path": "assets/textures"},
                {"path": "assets/models"},
            ],
        )

        result = subtree_manifest(manifest, "assets/textures")

        file_paths = {p.path for p in result.paths}
        assert file_paths == {"wood.png", "metal.png"}

    def test_directories_rebased(self) -> None:
        """Directories are rebased correctly."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "assets/textures/sub/file.png", "hash": "h1", "size": 100, "mtime": 1000},
            ],
            dirs=[
                {"path": "assets"},
                {"path": "assets/textures"},
                {"path": "assets/textures/sub"},
            ],
        )

        result = subtree_manifest(manifest, "assets/textures")

        dir_paths = {d.path for d in result.dirs}
        assert dir_paths == {"sub"}

    def test_preserves_runnable_flag(self) -> None:
        """Runnable flag is preserved."""
        manifest = self._create_v2025_manifest(
            files=[
                {
                    "path": "scripts/bin/run.sh",
                    "hash": "h1",
                    "size": 100,
                    "mtime": 1000,
                    "runnable": True,
                },
            ],
            dirs=[{"path": "scripts"}, {"path": "scripts/bin"}],
        )

        result = subtree_manifest(manifest, "scripts/bin")

        assert result.paths[0].runnable is True

    def test_preserves_chunkhashes(self) -> None:
        """Chunkhashes are preserved for large files."""
        manifest = self._create_v2025_manifest(
            files=[
                {
                    "path": "data/large/file.bin",
                    "chunkhashes": ["c1", "c2"],
                    "size": 512 * 1024 * 1024,
                    "mtime": 1000,
                },
            ],
            dirs=[{"path": "data"}, {"path": "data/large"}],
        )

        result = subtree_manifest(manifest, "data/large")

        assert result.paths[0].chunkhashes == ["c1", "c2"]

    def test_deleted_markers_preserved(self) -> None:
        """Deleted markers are preserved and rebased."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "assets/textures/old.png", "deleted": True},
            ],
            dirs=[
                {"path": "assets"},
                {"path": "assets/textures"},
            ],
        )

        result = subtree_manifest(manifest, "assets/textures")

        assert len(result.paths) == 1
        assert result.paths[0].path == "old.png"
        assert result.paths[0].deleted is True


class TestSubtreeManifestSymlinks:
    """Tests for symlink handling in subtree extraction."""

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

    def test_symlink_within_subtree_preserved(self) -> None:
        """Symlinks pointing within subtree are preserved."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "assets/textures/wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                # Target is relative to manifest root, not to symlink location
                {"path": "assets/textures/current", "symlink_target": "assets/textures/wood.png"},
            ],
            dirs=[{"path": "assets"}, {"path": "assets/textures"}],
        )

        result = subtree_manifest(
            manifest, "assets/textures", symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING
        )

        paths_by_name = {p.path: p for p in result.paths}
        assert "current" in paths_by_name
        # After rebasing, target should be relative to new root
        assert paths_by_name["current"].symlink_target == "wood.png"

    def test_escaping_symlink_collapsed(self) -> None:
        """Escaping symlinks are collapsed with COLLAPSE_ESCAPING."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "assets/textures/wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                # Target is relative to manifest root - points outside subtree
                {"path": "assets/textures/current", "symlink_target": "assets/shared/latest.png"},
                {"path": "assets/shared/latest.png", "hash": "h2", "size": 200, "mtime": 2000},
            ],
            dirs=[
                {"path": "assets"},
                {"path": "assets/textures"},
                {"path": "assets/shared"},
            ],
        )

        result = subtree_manifest(
            manifest, "assets/textures", symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING
        )

        paths_by_name = {p.path: p for p in result.paths}
        # "current" should be collapsed to a file with latest.png's content
        assert "current" in paths_by_name
        assert paths_by_name["current"].symlink_target is None
        assert paths_by_name["current"].hash == "h2"
        assert paths_by_name["current"].size == 200

    def test_escaping_symlink_excluded(self) -> None:
        """Escaping symlinks are excluded with EXCLUDE policy."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "assets/textures/wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                # Target is relative to manifest root - points outside subtree
                {"path": "assets/textures/current", "symlink_target": "assets/shared/latest.png"},
            ],
            dirs=[{"path": "assets"}, {"path": "assets/textures"}],
        )

        result = subtree_manifest(manifest, "assets/textures", symlink_policy=SymlinkPolicy.EXCLUDE)

        paths = {p.path for p in result.paths}
        assert "current" not in paths
        assert "wood.png" in paths

    def test_collapse_all_symlinks(self) -> None:
        """All symlinks are collapsed with COLLAPSE policy."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "assets/textures/wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                # Target is relative to manifest root
                {"path": "assets/textures/current", "symlink_target": "assets/textures/wood.png"},
            ],
            dirs=[{"path": "assets"}, {"path": "assets/textures"}],
        )

        result = subtree_manifest(
            manifest, "assets/textures", symlink_policy=SymlinkPolicy.COLLAPSE
        )

        paths_by_name = {p.path: p for p in result.paths}
        # "current" should be collapsed even though it's within subtree
        assert "current" in paths_by_name
        assert paths_by_name["current"].symlink_target is None
        assert paths_by_name["current"].hash == "h1"

    def test_symlink_target_rebased(self) -> None:
        """Preserved symlink targets are rebased correctly."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "a/b/c/target.txt", "hash": "h1", "size": 100, "mtime": 1000},
                # Target is relative to manifest root
                {"path": "a/b/c/sub/link.txt", "symlink_target": "a/b/c/target.txt"},
            ],
            dirs=[
                {"path": "a"},
                {"path": "a/b"},
                {"path": "a/b/c"},
                {"path": "a/b/c/sub"},
            ],
        )

        result = subtree_manifest(manifest, "a/b/c", symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING)

        link_entry = next(p for p in result.paths if p.path == "sub/link.txt")
        # Target should be rebased relative to new root
        assert link_entry.symlink_target == "target.txt"

    def test_symlink_to_missing_target_excluded(self) -> None:
        """Symlinks to missing targets are excluded with warning."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "assets/textures/wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                # Symlink to non-existent target (relative to manifest root)
                {"path": "assets/textures/broken", "symlink_target": "assets/nonexistent.png"},
            ],
            dirs=[{"path": "assets"}, {"path": "assets/textures"}],
        )

        messages: List[str] = []
        result = subtree_manifest(
            manifest,
            "assets/textures",
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
            print_function_callback=messages.append,
        )

        paths = {p.path for p in result.paths}
        assert "broken" not in paths
        assert any("Warning" in msg and "broken" in msg for msg in messages)


class TestSubtreeManifestValidation:
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

    def test_preserve_policy_raises_error(self) -> None:
        """PRESERVE policy raises ValueError."""
        manifest = self._create_v2025_manifest(
            files=[{"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        with pytest.raises(ValueError, match="preserve.*not supported"):
            subtree_manifest(manifest, "subdir", symlink_policy=SymlinkPolicy.PRESERVE)

    def test_transitive_include_targets_raises_error(self) -> None:
        """TRANSITIVE_INCLUDE_TARGETS policy raises ValueError."""
        manifest = self._create_v2025_manifest(
            files=[{"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        with pytest.raises(ValueError, match="transitive_include_targets.*not supported"):
            subtree_manifest(
                manifest, "subdir", symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS
            )

    def test_empty_subtree_raises_error(self) -> None:
        """Empty subtree path raises ValueError."""
        manifest = self._create_v2025_manifest(
            files=[{"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        with pytest.raises(ValueError, match="cannot be empty"):
            subtree_manifest(manifest, ".")

    def test_relative_subtree_with_absolute_manifest_raises_error(self) -> None:
        """Relative subtree with absolute manifest paths raises ValueError."""
        # Use OS-appropriate absolute path
        if os.name == "nt":
            abs_path = "C:/Users/user/file.txt"
        else:
            abs_path = "/home/user/file.txt"

        manifest = self._create_v2025_manifest(
            files=[{"path": abs_path, "hash": "h1", "size": 100, "mtime": 1000}]
        )

        with pytest.raises(ValueError, match="relative.*absolute"):
            subtree_manifest(manifest, "subdir")

    def test_absolute_subtree_with_relative_manifest_raises_error(self) -> None:
        """Absolute subtree with relative manifest paths raises ValueError."""
        manifest = self._create_v2025_manifest(
            files=[{"path": "assets/file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        # Use OS-appropriate absolute path for subtree
        if os.name == "nt":
            abs_subtree = "C:/Users/user/assets"
        else:
            abs_subtree = "/home/user/assets"

        with pytest.raises(ValueError, match="absolute.*relative"):
            subtree_manifest(manifest, abs_subtree)


class TestSubtreeManifestAbsolutePaths:
    """Tests for subtree extraction with absolute paths."""

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

    def test_absolute_paths_converted_to_relative(self) -> None:
        """Absolute paths in manifest are converted to relative in output."""
        # Use OS-appropriate absolute paths
        if os.name == "nt":
            base = "C:/projects/scene"
        else:
            base = "/projects/scene"

        manifest = self._create_v2025_manifest(
            files=[
                {
                    "path": f"{base}/assets/textures/wood.png",
                    "hash": "h1",
                    "size": 100,
                    "mtime": 1000,
                },
                {
                    "path": f"{base}/assets/textures/metal.png",
                    "hash": "h2",
                    "size": 200,
                    "mtime": 2000,
                },
            ],
            dirs=[
                {"path": f"{base}/assets"},
                {"path": f"{base}/assets/textures"},
            ],
        )

        result = subtree_manifest(manifest, f"{base}/assets/textures")

        # Output should have relative paths
        file_paths = {p.path for p in result.paths}
        assert file_paths == {"wood.png", "metal.png"}
        # Verify paths are relative (don't start with / or drive letter)
        for entry in result.paths:
            assert not entry.path.startswith("/")
            assert not (len(entry.path) >= 2 and entry.path[1] == ":")

    def test_posix_root_subtree(self) -> None:
        """POSIX root '/' subtree extracts all files with paths relative to root."""
        from unittest.mock import patch

        with patch.object(os, "name", "posix"):
            manifest = self._create_v2025_manifest(
                files=[
                    {"path": "/home/user/file.txt", "hash": "h1", "size": 100, "mtime": 1000},
                    {"path": "/var/data/other.txt", "hash": "h2", "size": 200, "mtime": 2000},
                ],
                dirs=[
                    {"path": "/home"},
                    {"path": "/home/user"},
                    {"path": "/var"},
                    {"path": "/var/data"},
                ],
            )

            result = subtree_manifest(manifest, "/")

            # All files should be included with paths relative to "/"
            file_paths = {p.path for p in result.paths}
            assert file_paths == {"home/user/file.txt", "var/data/other.txt"}

            # Directories should also be rebased
            dir_paths = {d.path for d in result.dirs}
            assert "home" in dir_paths
            assert "home/user" in dir_paths
            assert "var" in dir_paths
            assert "var/data" in dir_paths


class TestSubtreeManifestDirectorySymlinks:
    """Tests for collapsing symlinks that point to directories."""

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

    def test_directory_symlink_collapsed(self) -> None:
        """Symlink to directory is collapsed to include all directory contents."""
        manifest = self._create_v2025_manifest(
            files=[
                # Target is relative to manifest root - points outside subtree
                {"path": "assets/textures/link", "symlink_target": "assets/shared/v2"},
                {"path": "assets/shared/v2/a.png", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "assets/shared/v2/b.png", "hash": "h2", "size": 200, "mtime": 2000},
            ],
            dirs=[
                {"path": "assets"},
                {"path": "assets/textures"},
                {"path": "assets/shared"},
                {"path": "assets/shared/v2"},
            ],
        )

        result = subtree_manifest(
            manifest, "assets/textures", symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING
        )

        paths = {p.path for p in result.paths}
        # The symlink "link" should be expanded to include the directory contents
        assert "link/a.png" in paths
        assert "link/b.png" in paths

    def test_directory_symlink_collapsed_with_implicit_dirs(self) -> None:
        """Symlink to directory works even when dirs list doesn't include all parent dirs."""
        # Create manifest WITHOUT explicit dir entries - dirs are implicit from file paths
        manifest = self._create_v2025_manifest(
            files=[
                # Target is relative to manifest root - points outside subtree
                {"path": "assets/textures/link", "symlink_target": "assets/shared/v2"},
                {"path": "assets/shared/v2/a.png", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "assets/shared/v2/b.png", "hash": "h2", "size": 200, "mtime": 2000},
            ],
            dirs=[],  # No explicit dirs - they should be inferred from file paths
        )

        result = subtree_manifest(
            manifest, "assets/textures", symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING
        )

        paths = {p.path for p in result.paths}
        # The symlink "link" should be expanded to include the directory contents
        assert "link/a.png" in paths
        assert "link/b.png" in paths

    def test_directory_symlink_with_nested_symlink_collapsed(self) -> None:
        """Symlink to directory containing nested symlinks recursively collapses all symlinks."""
        manifest = self._create_v2025_manifest(
            files=[
                # Symlink in subtree pointing to a directory outside subtree
                {"path": "assets/textures/link", "symlink_target": "assets/shared/v2"},
                # Directory contains a nested symlink pointing to another file
                {
                    "path": "assets/shared/v2/nested_link",
                    "symlink_target": "assets/data/actual.png",
                },
                {"path": "assets/shared/v2/regular.png", "hash": "h1", "size": 100, "mtime": 1000},
                # The actual target of the nested symlink
                {"path": "assets/data/actual.png", "hash": "h2", "size": 200, "mtime": 2000},
            ],
            dirs=[
                {"path": "assets"},
                {"path": "assets/textures"},
                {"path": "assets/shared"},
                {"path": "assets/shared/v2"},
                {"path": "assets/data"},
            ],
        )

        result = subtree_manifest(
            manifest, "assets/textures", symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING
        )

        paths_by_name = {p.path: p for p in result.paths}

        # Regular file should be collapsed normally
        assert "link/regular.png" in paths_by_name
        assert paths_by_name["link/regular.png"].hash == "h1"
        assert paths_by_name["link/regular.png"].symlink_target is None

        # Nested symlink should be recursively collapsed to the actual file
        assert "link/nested_link" in paths_by_name
        assert paths_by_name["link/nested_link"].hash == "h2"
        assert paths_by_name["link/nested_link"].size == 200
        assert paths_by_name["link/nested_link"].symlink_target is None

    def test_directory_symlink_with_nested_symlink_to_directory(self) -> None:
        """Nested symlink pointing to a directory is recursively collapsed."""
        manifest = self._create_v2025_manifest(
            files=[
                # Symlink in subtree pointing to a directory outside subtree
                {"path": "assets/textures/link", "symlink_target": "assets/shared"},
                # Directory contains a nested symlink pointing to another directory
                {"path": "assets/shared/nested_dir_link", "symlink_target": "assets/data"},
                {"path": "assets/shared/file.png", "hash": "h1", "size": 100, "mtime": 1000},
                # Files in the nested directory target
                {"path": "assets/data/a.png", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "assets/data/b.png", "hash": "h3", "size": 300, "mtime": 3000},
            ],
            dirs=[
                {"path": "assets"},
                {"path": "assets/textures"},
                {"path": "assets/shared"},
                {"path": "assets/data"},
            ],
        )

        result = subtree_manifest(
            manifest, "assets/textures", symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING
        )

        paths = {p.path for p in result.paths}

        # Regular file should be collapsed
        assert "link/file.png" in paths

        # Nested directory symlink should be recursively collapsed
        # The symlink "nested_dir_link" pointed to "assets/data" which contains a.png and b.png
        assert "link/nested_dir_link/a.png" in paths
        assert "link/nested_dir_link/b.png" in paths

        # Verify the files have correct content
        paths_by_name = {p.path: p for p in result.paths}
        assert paths_by_name["link/nested_dir_link/a.png"].hash == "h2"
        assert paths_by_name["link/nested_dir_link/b.png"].hash == "h3"


class TestPathSeparatorHandling:
    """Tests for path separator handling across platforms.

    These tests verify that:
    - On Windows: backslashes in input paths are converted to forward slashes
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

    def test_normalize_subtree_path_converts_backslashes_on_windows(self) -> None:
        """On Windows, backslashes in subtree path are converted to forward slashes."""
        from unittest.mock import patch

        with patch(
            "deadline.job_attachments.asset_manifests._operations._subtree_manifest.os.name", "nt"
        ):
            result = _normalize_subtree_path("assets\\textures\\wood")
            # On Windows, backslashes should be converted to forward slashes
            assert result == "assets/textures/wood"

    def test_normalize_subtree_path_preserves_backslashes_on_posix(self) -> None:
        """On POSIX, backslashes in subtree path are preserved as valid filename characters."""
        from unittest.mock import patch

        with patch(
            "deadline.job_attachments.asset_manifests._operations._subtree_manifest.os.name",
            "posix",
        ):
            # On POSIX, a backslash is a valid filename character
            # "assets\\textures" is a single directory name containing a backslash
            result = _normalize_subtree_path("assets\\textures")
            # On POSIX, backslashes should NOT be converted - they're valid filename chars
            assert result == "assets\\textures"

    def test_subtree_with_backslash_filename_on_posix(self) -> None:
        """On POSIX, files with backslashes in names are handled correctly."""
        from unittest.mock import patch

        # Patch os.name in both modules - base_manifest (for manifest creation)
        # and _subtree_manifest (for subtree operation)
        with patch(
            "deadline.job_attachments.asset_manifests.base_manifest.os.name", "posix"
        ), patch(
            "deadline.job_attachments.asset_manifests._operations._subtree_manifest.os.name",
            "posix",
        ):
            # Create a manifest with a file that has a backslash in its name (valid on POSIX)
            manifest = self._create_v2025_manifest(
                files=[
                    # A file named "file\with\backslashes.txt" (single filename with backslashes)
                    {
                        "path": "assets/file\\with\\backslashes.txt",
                        "hash": "h1",
                        "size": 100,
                        "mtime": 1000,
                    },
                    {"path": "assets/normal.txt", "hash": "h2", "size": 200, "mtime": 2000},
                ],
                dirs=[{"path": "assets"}],
            )

            result = subtree_manifest(manifest, "assets")

            paths = {p.path for p in result.paths}
            # The backslash filename should be preserved as-is
            assert "file\\with\\backslashes.txt" in paths
            assert "normal.txt" in paths

    def test_subtree_with_backslash_in_subtree_param_on_windows(self) -> None:
        """On Windows, backslashes in subtree parameter are normalized."""
        from unittest.mock import patch

        with patch("deadline.job_attachments.asset_manifests.base_manifest.os.name", "nt"), patch(
            "deadline.job_attachments.asset_manifests._operations._subtree_manifest.os.name", "nt"
        ):
            manifest = self._create_v2025_manifest(
                files=[
                    {"path": "assets/textures/wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                ],
                dirs=[{"path": "assets"}, {"path": "assets/textures"}],
            )

            # On Windows, user might pass "assets\\textures" which should work
            result = subtree_manifest(manifest, "assets\\textures")

            paths = {p.path for p in result.paths}
            assert "wood.png" in paths
