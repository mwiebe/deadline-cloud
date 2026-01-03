# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for subtree_manifest and related functions.

These tests cover:
- Basic subtree extraction using unified manifest classes
- Path rebasing (stripping subtree prefix)
- Path style validation (relative vs absolute)
- Symlink handling with different policies
- Directory handling
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
from deadline.job_attachments.asset_manifests.manifest import (
    AbsSnapshotManifest,
    RelSnapshotManifest,
    RelDiffManifest,
    ManifestFilePath,
    ManifestDirectoryPath,
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


class TestSubtreeManifestRelative:
    """Tests for subtree extraction with relative paths."""

    def _create_rel_snapshot(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> RelSnapshotManifest:
        """Helper to create a RelSnapshotManifest."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
        )

    def test_basic_subtree_extraction(self) -> None:
        """Basic subtree extraction works."""
        manifest = self._create_rel_snapshot(
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

        assert isinstance(result, RelSnapshotManifest)
        file_paths = {p.path for p in result.paths}
        assert file_paths == {"wood.png", "metal.png"}

    def test_directories_rebased(self) -> None:
        """Directories are rebased correctly."""
        manifest = self._create_rel_snapshot(
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

    def test_preserves_file_metadata(self) -> None:
        """File metadata is preserved after rebasing."""
        manifest = self._create_rel_snapshot(
            files=[
                {"path": "assets/textures/wood.png", "hash": "hash1", "size": 100, "mtime": 1000},
            ],
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
        manifest = self._create_rel_snapshot(
            files=[
                {"path": "assets/textures/wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "assets/textures/metal.png", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "assets/models/chair.blend", "hash": "h3", "size": 300, "mtime": 3000},
            ],
        )

        result = subtree_manifest(manifest, "assets/textures")

        assert result.totalSize == 300  # 100 + 200

    def test_nested_subtree(self) -> None:
        """Nested subtree extraction works."""
        manifest = self._create_rel_snapshot(
            files=[
                {"path": "a/b/c/d/file.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "a/b/c/other.txt", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "a/b/outside.txt", "hash": "h3", "size": 300, "mtime": 3000},
            ],
        )

        result = subtree_manifest(manifest, "a/b/c")

        assert len(result.paths) == 2
        paths = {p.path for p in result.paths}
        assert paths == {"d/file.txt", "other.txt"}

    def test_empty_result(self) -> None:
        """Subtree with no matching entries returns empty manifest."""
        manifest = self._create_rel_snapshot(
            files=[
                {"path": "assets/models/chair.blend", "hash": "h1", "size": 100, "mtime": 1000},
            ],
        )

        result = subtree_manifest(manifest, "assets/textures")

        assert len(result.paths) == 0
        assert result.totalSize == 0

    def test_preserves_runnable_flag(self) -> None:
        """Runnable flag is preserved."""
        manifest = self._create_rel_snapshot(
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
        manifest = self._create_rel_snapshot(
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


class TestSubtreeManifestDiff:
    """Tests for subtree extraction with diff manifests."""

    def _create_rel_diff(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> RelDiffManifest:
        """Helper to create a RelDiffManifest."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return RelDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
            parent_manifest_hash="parent123",
        )

    def test_deleted_markers_preserved(self) -> None:
        """Deleted markers are preserved and rebased."""
        manifest = self._create_rel_diff(
            files=[
                {"path": "assets/textures/old.png", "deleted": True},
            ],
            dirs=[
                {"path": "assets"},
                {"path": "assets/textures"},
            ],
        )

        result = subtree_manifest(manifest, "assets/textures")

        assert isinstance(result, RelDiffManifest)
        assert len(result.paths) == 1
        assert result.paths[0].path == "old.png"
        assert result.paths[0].deleted is True

    def test_discards_parent_manifest_hash(self) -> None:
        """Parent manifest hash is discarded for diff manifests (subtree changes the root)."""
        manifest = self._create_rel_diff(
            files=[
                {"path": "assets/textures/file.png", "hash": "h1", "size": 100, "mtime": 1000},
            ],
        )

        result = subtree_manifest(manifest, "assets/textures")

        # Parent manifest hash should be None because the subtree operation changes
        # the root path, making the original parent manifest hash invalid
        assert result.parentManifestHash is None


class TestSubtreeManifestSymlinks:
    """Tests for symlink handling in subtree extraction."""

    def _create_rel_snapshot(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> RelSnapshotManifest:
        """Helper to create a RelSnapshotManifest."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
        )

    def test_symlink_within_subtree_preserved(self) -> None:
        """Symlinks pointing within subtree are preserved."""
        manifest = self._create_rel_snapshot(
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
        manifest = self._create_rel_snapshot(
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
        manifest = self._create_rel_snapshot(
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
        manifest = self._create_rel_snapshot(
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

    def test_symlink_to_missing_target_excluded(self) -> None:
        """Symlinks to missing targets are excluded with warning."""
        manifest = self._create_rel_snapshot(
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

    def _create_rel_snapshot(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> RelSnapshotManifest:
        """Helper to create a RelSnapshotManifest."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        return RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=0,
        )

    def _create_abs_snapshot(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AbsSnapshotManifest:
        """Helper to create an AbsSnapshotManifest."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        return AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=0,
        )

    def test_preserve_policy_raises_error(self) -> None:
        """PRESERVE policy raises ValueError."""
        manifest = self._create_rel_snapshot(
            files=[{"path": "subdir/file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        with pytest.raises(ValueError, match="preserve.*not supported"):
            subtree_manifest(manifest, "subdir", symlink_policy=SymlinkPolicy.PRESERVE)

    def test_transitive_include_targets_raises_error(self) -> None:
        """TRANSITIVE_INCLUDE_TARGETS policy raises ValueError."""
        manifest = self._create_rel_snapshot(
            files=[{"path": "subdir/file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        with pytest.raises(ValueError, match="transitive_include_targets.*not supported"):
            subtree_manifest(
                manifest, "subdir", symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS
            )

    def test_empty_subtree_raises_error(self) -> None:
        """Empty subtree path raises ValueError."""
        manifest = self._create_rel_snapshot(
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

        manifest = self._create_abs_snapshot(
            files=[{"path": abs_path, "hash": "h1", "size": 100, "mtime": 1000}]
        )

        with pytest.raises(ValueError, match="relative.*absolute"):
            subtree_manifest(manifest, "subdir")

    def test_absolute_subtree_with_relative_manifest_raises_error(self) -> None:
        """Absolute subtree with relative manifest paths raises ValueError."""
        manifest = self._create_rel_snapshot(
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

    def _create_abs_snapshot(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AbsSnapshotManifest:
        """Helper to create an AbsSnapshotManifest."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AbsSnapshotManifest(
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

        manifest = self._create_abs_snapshot(
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

        # Output should be RelSnapshotManifest with relative paths
        assert isinstance(result, RelSnapshotManifest)
        file_paths = {p.path for p in result.paths}
        assert file_paths == {"wood.png", "metal.png"}
        # Verify paths are relative (don't start with / or drive letter)
        for entry in result.paths:
            assert not entry.path.startswith("/")
            assert not (len(entry.path) >= 2 and entry.path[1] == ":")

    def test_posix_root_subtree(self) -> None:
        """POSIX root '/' subtree extracts all files with paths relative to root."""
        with patch.object(os, "name", "posix"):
            manifest = self._create_abs_snapshot(
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

    def _create_rel_snapshot(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> RelSnapshotManifest:
        """Helper to create a RelSnapshotManifest."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
        )

    def test_directory_symlink_collapsed(self) -> None:
        """Symlink to directory is collapsed to include all directory contents."""
        manifest = self._create_rel_snapshot(
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
        manifest = self._create_rel_snapshot(
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
        manifest = self._create_rel_snapshot(
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


class TestPathSeparatorHandling:
    """Tests for path separator handling across platforms."""

    def _create_rel_snapshot(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> RelSnapshotManifest:
        """Helper to create a RelSnapshotManifest."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
        )

    def test_normalize_subtree_path_converts_backslashes_on_windows(self) -> None:
        """On Windows, backslashes in subtree path are converted to forward slashes."""
        with patch(
            "deadline.job_attachments.asset_manifests._operations._subtree_manifest.os.name", "nt"
        ):
            result = _normalize_subtree_path("assets\\textures\\wood")
            # On Windows, backslashes should be converted to forward slashes
            assert result == "assets/textures/wood"

    def test_normalize_subtree_path_preserves_backslashes_on_posix(self) -> None:
        """On POSIX, backslashes in subtree path are preserved as valid filename characters."""
        with patch(
            "deadline.job_attachments.asset_manifests._operations._subtree_manifest.os.name",
            "posix",
        ):
            # On POSIX, a backslash is a valid filename character
            # "assets\\textures" is a single directory name containing a backslash
            result = _normalize_subtree_path("assets\\textures")
            # On POSIX, backslashes should NOT be converted - they're valid filename chars
            assert result == "assets\\textures"

    def test_subtree_with_backslash_in_subtree_param_on_windows(self) -> None:
        """On Windows, backslashes in subtree parameter are normalized."""
        with patch(
            "deadline.job_attachments.asset_manifests._operations._subtree_manifest.os.name", "nt"
        ), patch("deadline.job_attachments.asset_manifests.manifest.os.name", "nt"):
            manifest = self._create_rel_snapshot(
                files=[
                    {"path": "assets/textures/wood.png", "hash": "h1", "size": 100, "mtime": 1000},
                ],
                dirs=[{"path": "assets"}, {"path": "assets/textures"}],
            )

            # On Windows, user might pass "assets\\textures" which should work
            result = subtree_manifest(manifest, "assets\\textures")

            paths = {p.path for p in result.paths}
            assert "wood.png" in paths


class TestSubtreeManifestUNCPaths:
    """Tests for Windows UNC path handling in subtree extraction."""

    def _create_abs_snapshot(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AbsSnapshotManifest:
        """Helper to create an AbsSnapshotManifest."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
        )

    @patch.object(os, "name", "nt")
    def test_unc_path_subtree_extraction(self) -> None:
        """UNC path subtree extraction works correctly."""
        manifest = self._create_abs_snapshot(
            files=[
                {
                    "path": "//server/share/assets/textures/wood.png",
                    "hash": "h1",
                    "size": 100,
                    "mtime": 1000,
                },
                {
                    "path": "//server/share/assets/textures/metal.png",
                    "hash": "h2",
                    "size": 200,
                    "mtime": 2000,
                },
                {
                    "path": "//server/share/assets/models/chair.blend",
                    "hash": "h3",
                    "size": 300,
                    "mtime": 3000,
                },
            ],
            dirs=[
                {"path": "//server/share"},
                {"path": "//server/share/assets"},
                {"path": "//server/share/assets/textures"},
                {"path": "//server/share/assets/models"},
            ],
        )

        result = subtree_manifest(manifest, "//server/share/assets/textures")

        file_paths = {p.path for p in result.paths}
        assert file_paths == {"wood.png", "metal.png"}

    @patch.object(os, "name", "nt")
    def test_unc_path_deep_nesting_no_infinite_loop(self) -> None:
        """Deeply nested UNC paths don't cause infinite loop in dir_lookup building."""
        manifest = self._create_abs_snapshot(
            files=[
                {
                    "path": "//server/share/a/b/c/d/e/f/file.txt",
                    "hash": "h1",
                    "size": 100,
                    "mtime": 1000,
                },
            ],
            dirs=[],  # No explicit dirs - forces dir_lookup to be built from file paths
        )

        # This should complete without hanging
        result = subtree_manifest(manifest, "//server/share/a/b/c")

        file_paths = {p.path for p in result.paths}
        assert file_paths == {"d/e/f/file.txt"}

    @patch.object(os, "name", "nt")
    def test_unc_path_multiple_servers(self) -> None:
        """Manifest with files from multiple UNC servers works correctly."""
        manifest = self._create_abs_snapshot(
            files=[
                {
                    "path": "//server1/share/assets/file1.txt",
                    "hash": "h1",
                    "size": 100,
                    "mtime": 1000,
                },
                {
                    "path": "//server1/share/assets/file2.txt",
                    "hash": "h2",
                    "size": 200,
                    "mtime": 2000,
                },
                {
                    "path": "//server2/share/assets/file3.txt",
                    "hash": "h3",
                    "size": 300,
                    "mtime": 3000,
                },
            ],
            dirs=[],
        )

        result = subtree_manifest(manifest, "//server1/share/assets")

        file_paths = {p.path for p in result.paths}
        # Only files from server1 should be included
        assert file_paths == {"file1.txt", "file2.txt"}
