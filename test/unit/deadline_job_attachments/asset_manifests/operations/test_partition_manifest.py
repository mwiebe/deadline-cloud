# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for partition_manifest and related functions.

These tests cover:
- Basic partitioning using unified manifest classes
- Auto-root determination (POSIX and Windows)
- Explicit roots with remainder handling
- Empty partitions for explicit roots
- Path style validation (relative vs absolute)
- Root overlap validation
- referenced_paths handling
- Symlink handling with different policies
"""

import os
from typing import List
from unittest.mock import patch

import pytest

from deadline.job_attachments.asset_manifests._operations import (
    partition_manifest,
)
from deadline.job_attachments.asset_manifests._operations._partition_manifest import (
    _is_absolute_path,
    _is_path_under_root,
    _longest_common_path_prefix,
    _normalize_path,
    _collect_all_dirs,
    _get_windows_drive_root,
)
from deadline.job_attachments.asset_manifests.versions import (
    SymlinkPolicy,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.manifest import (
    AbsSnapshotManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    RelSnapshotManifest,
)


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_normalize_path_removes_trailing_slash(self) -> None:
        """Trailing slashes are removed."""
        assert _normalize_path("assets/textures/") == "assets/textures"

    def test_normalize_path_collapses_dots(self) -> None:
        """Dot components are collapsed."""
        assert _normalize_path("assets/./textures") == "assets/textures"
        assert _normalize_path("assets/../assets/textures") == "assets/textures"

    def test_normalize_path_empty_becomes_dot(self) -> None:
        """Empty path becomes '.'."""
        assert _normalize_path("") == "."

    def test_is_absolute_path_posix(self) -> None:
        """POSIX absolute paths are detected."""
        assert _is_absolute_path("/home/user/file.txt") is True
        assert _is_absolute_path("/") is True
        assert _is_absolute_path("relative/path") is False

    def test_is_absolute_path_windows(self) -> None:
        """Windows absolute paths are detected."""
        with patch.object(os, "name", "nt"):
            assert _is_absolute_path("C:/Users/file.txt") is True
            assert _is_absolute_path("//server/share/file.txt") is True
            assert _is_absolute_path("relative/path") is False

        with patch.object(os, "name", "posix"):
            assert _is_absolute_path("C:/Users/file.txt") is False
            assert _is_absolute_path("//server/share/file.txt") is True
            assert _is_absolute_path("relative/path") is False

    def test_is_path_under_root_exact_match(self) -> None:
        """Exact match is under root."""
        assert _is_path_under_root("assets/textures", "assets/textures") is True

    def test_is_path_under_root_child(self) -> None:
        """Child paths are under root."""
        assert _is_path_under_root("assets/textures/wood.png", "assets/textures") is True

    def test_is_path_under_root_not_under(self) -> None:
        """Paths outside root are not under."""
        assert _is_path_under_root("assets/models/chair.blend", "assets/textures") is False

    def test_is_path_under_root_prefix_not_directory(self) -> None:
        """Path that starts with root but isn't a child is not under."""
        assert _is_path_under_root("assets/textures2/file.png", "assets/textures") is False

    def test_longest_common_path_prefix_single_path(self) -> None:
        """Single directory path returns itself."""
        assert _longest_common_path_prefix(["a/b/c"]) == "a/b/c"

    def test_longest_common_path_prefix_common_parent(self) -> None:
        """Multiple paths return common parent."""
        paths = ["a/b/c/file1.txt", "a/b/d/file2.txt"]
        assert _longest_common_path_prefix(paths) == "a/b"

    def test_longest_common_path_prefix_same_directory(self) -> None:
        """Files in same directory return that directory."""
        paths = ["a/b/c/file1.txt", "a/b/c/file2.txt"]
        assert _longest_common_path_prefix(paths) == "a/b/c"

    def test_longest_common_path_prefix_absolute_paths(self) -> None:
        """Absolute paths preserve leading slash."""
        paths = ["/a/b/c/file1.txt", "/a/b/d/file2.txt"]
        assert _longest_common_path_prefix(paths) == "/a/b"

    def test_longest_common_path_prefix_no_common(self) -> None:
        """Paths with no common prefix return root."""
        paths = ["/a/file1.txt", "/b/file2.txt"]
        assert _longest_common_path_prefix(paths) == "/"

    def test_longest_common_path_prefix_nested_paths(self) -> None:
        """Nested paths return the shortest common ancestor."""
        paths = ["/a", "/a/b", "/a/b/c"]
        assert _longest_common_path_prefix(paths) == "/a"

    def test_get_windows_drive_root_drive_letter(self) -> None:
        """Drive letter is extracted correctly."""
        assert _get_windows_drive_root("C:/Users/file.txt") == "C:"
        assert _get_windows_drive_root("d:/projects/file.txt") == "D:"

    def test_get_windows_drive_root_unc(self) -> None:
        """UNC root is extracted correctly."""
        assert _get_windows_drive_root("//server/share/file.txt") == "//server/share"


class TestCollectAllDirs:
    """Tests for _collect_all_dirs helper."""

    def _create_rel_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> RelSnapshotManifest:
        """Helper to create a relative manifest."""
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
            files=file_entries,
            total_size=total_size,
        )

    def _create_abs_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AbsSnapshotManifest:
        """Helper to create an absolute manifest."""
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
            files=file_entries,
            total_size=total_size,
        )

    def test_collects_parent_directories(self) -> None:
        """Parent directories of files are collected."""
        manifest = self._create_rel_manifest(
            files=[
                {"path": "a/b/file1.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "a/b/file2.txt", "hash": "h2", "size": 100, "mtime": 1000},
                {"path": "a/c/file3.txt", "hash": "h3", "size": 100, "mtime": 1000},
            ]
        )

        dirs = _collect_all_dirs(manifest)

        assert dirs == {"a/b", "a/c"}

    def test_collects_explicit_directories(self) -> None:
        """Explicit directory entries are collected."""
        manifest = self._create_rel_manifest(
            files=[],
            dirs=[{"path": "a/b"}, {"path": "a/c/d"}],
        )

        dirs = _collect_all_dirs(manifest)

        assert dirs == {"a/b", "a/c/d"}

    def test_root_level_relative_file_adds_dot(self) -> None:
        """Root-level relative file adds '.' to dirs."""
        manifest = self._create_rel_manifest(
            files=[{"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        dirs = _collect_all_dirs(manifest)

        assert "." in dirs

    def test_root_level_absolute_file_adds_slash(self) -> None:
        """Root-level absolute file adds '/' to dirs."""
        manifest = self._create_abs_manifest(
            files=[{"path": "/file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        with patch.object(os, "name", "posix"):
            dirs = _collect_all_dirs(manifest)

        assert "/" in dirs


class TestPartitionManifestRelative:
    """Tests for partitioning with relative paths."""

    def _create_rel_manifest(self, paths: List[tuple[str, str, int, int]]) -> RelSnapshotManifest:
        """Helper to create a relative manifest."""
        entries = [ManifestFilePath(path=p, hash=h, size=s, mtime=m) for p, h, s, m in paths]
        total_size = sum(s for _, _, s, _ in paths)
        return RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=entries,
            total_size=total_size,
        )

    def test_single_partition_relative_paths(self) -> None:
        """Single partition for relative paths with common root."""
        manifest = self._create_rel_manifest(
            [
                ("assets/textures/wood.png", "hash1", 100, 1000),
                ("assets/textures/metal.png", "hash2", 200, 2000),
                ("assets/models/chair.blend", "hash3", 300, 3000),
            ]
        )

        result = partition_manifest(manifest)

        assert len(result) == 1
        root, subtree = result[0]
        assert root == "assets"
        assert len(subtree.files) == 3

    def test_explicit_roots(self) -> None:
        """Explicit roots partition correctly."""
        manifest = self._create_rel_manifest(
            [
                ("assets/textures/wood.png", "hash1", 100, 1000),
                ("assets/models/chair.blend", "hash2", 200, 2000),
            ]
        )

        result = partition_manifest(manifest, roots=["assets/textures", "assets/models"])

        assert len(result) == 2
        roots = [r for r, _ in result]
        assert roots == ["assets/textures", "assets/models"]

        # Check first partition
        _, textures_manifest = result[0]
        assert len(textures_manifest.files) == 1
        assert textures_manifest.files[0].path == "wood.png"

        # Check second partition
        _, models_manifest = result[1]
        assert len(models_manifest.files) == 1
        assert models_manifest.files[0].path == "chair.blend"

    def test_empty_partition_for_explicit_root(self) -> None:
        """Empty partition returned for explicit root with no entries."""
        manifest = self._create_rel_manifest(
            [
                ("assets/textures/wood.png", "hash1", 100, 1000),
            ]
        )

        result = partition_manifest(manifest, roots=["assets/textures", "assets/models"])

        assert len(result) == 2
        roots = [r for r, _ in result]
        assert roots == ["assets/textures", "assets/models"]

        # Second partition should be empty
        _, models_manifest = result[1]
        assert len(models_manifest.files) == 0


class TestPartitionManifestV2025:
    """Tests for v2025-12-04-beta partitioning with relative paths."""

    def _create_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> RelSnapshotManifest:
        """Helper to create a relative manifest."""
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
            files=file_entries,
            total_size=total_size,
        )

    def test_single_partition_relative_paths(self) -> None:
        """Single partition for relative paths with common root."""
        manifest = self._create_manifest(
            files=[
                {"path": "project/src/main.py", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "project/src/utils.py", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "project/tests/test_main.py", "hash": "h3", "size": 150, "mtime": 3000},
            ],
            dirs=[
                {"path": "project"},
                {"path": "project/src"},
                {"path": "project/tests"},
            ],
        )

        result = partition_manifest(manifest)

        assert len(result) == 1
        root, subtree = result[0]
        assert root == "project"
        assert len(subtree.files) == 3

    def test_preserves_directories(self) -> None:
        """Directories are preserved in partitions."""
        manifest = self._create_manifest(
            files=[
                {"path": "project/src/main.py", "hash": "h1", "size": 100, "mtime": 1000},
            ],
            dirs=[
                {"path": "project"},
                {"path": "project/src"},
                {"path": "project/empty"},
            ],
        )

        result = partition_manifest(manifest, roots=["project"])

        assert len(result) == 1
        _, subtree = result[0]
        dir_paths = {d.path for d in subtree.dirs}
        assert "src" in dir_paths
        assert "empty" in dir_paths

    def test_preserves_symlinks_within_partition(self) -> None:
        """Symlinks within partition are preserved."""
        manifest = self._create_manifest(
            files=[
                {"path": "project/src/main.py", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "project/src/link", "symlink_target": "project/src/main.py"},
            ],
            dirs=[{"path": "project"}, {"path": "project/src"}],
        )

        result = partition_manifest(manifest, roots=["project"])

        assert len(result) == 1
        _, subtree = result[0]
        link_entry = next((p for p in subtree.files if p.path == "src/link"), None)
        assert link_entry is not None
        assert link_entry.symlink_target == "src/main.py"


class TestPartitionManifestAutoRoots:
    """Tests for auto-root determination."""

    def _create_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AbsSnapshotManifest:
        """Helper to create a v2025 manifest."""
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
            files=file_entries,
            total_size=total_size,
        )

    def test_posix_auto_root_longest_common_prefix(self) -> None:
        """POSIX auto-root is longest common prefix."""
        manifest = self._create_manifest(
            files=[
                {
                    "path": "/projects/scene/assets/model.blend",
                    "hash": "h1",
                    "size": 100,
                    "mtime": 1000,
                },
                {
                    "path": "/projects/scene/assets/texture.png",
                    "hash": "h2",
                    "size": 200,
                    "mtime": 2000,
                },
                {
                    "path": "/projects/scene/render/output.exr",
                    "hash": "h3",
                    "size": 300,
                    "mtime": 3000,
                },
            ]
        )

        with patch.object(os, "name", "posix"):
            result = partition_manifest(manifest)

        assert len(result) == 1
        root, _ = result[0]
        assert root == "/projects/scene"

    def test_posix_explicit_roots_with_remainder(self) -> None:
        """POSIX explicit roots with remaining paths creates additional roots."""
        manifest = self._create_manifest(
            files=[
                {"path": "/projects/scene/model.blend", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "/data/shared/texture.png", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "/home/user/cache/temp.bin", "hash": "h3", "size": 300, "mtime": 3000},
            ]
        )

        with patch.object(os, "name", "posix"):
            result = partition_manifest(manifest, roots=["/projects/scene"])

        # Should have explicit root first, then auto-determined roots for remainder
        # Can't use "/" because it would include /projects/scene as a subpath
        # Auto-roots should be as deep as possible (longest common prefix per group)
        roots = [r for r, _ in result]
        assert roots[0] == "/projects/scene"
        # Remaining roots should be the deepest valid paths (sorted)
        remaining_roots = sorted(roots[1:])
        assert remaining_roots == ["/data/shared", "/home/user/cache"]

    def test_root_level_files_returns_dot_root(self) -> None:
        """Root-level relative files return '.' as root."""
        manifest = self._create_manifest(
            files=[
                {"path": "file1.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "file2.txt", "hash": "h2", "size": 200, "mtime": 2000},
            ]
        )

        result = partition_manifest(manifest)

        assert len(result) == 1
        root, subtree = result[0]
        assert root == "."
        # Manifest should be returned as-is for "." root
        assert len(subtree.files) == 2


class TestPartitionManifestReferencedPaths:
    """Tests for referenced_paths handling."""

    def _create_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> RelSnapshotManifest:
        """Helper to create a relative manifest."""
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
            files=file_entries,
            total_size=total_size,
        )

    def test_referenced_paths_affects_root_determination(self) -> None:
        """referenced_paths affects auto-root determination."""
        manifest = self._create_manifest(
            files=[
                {"path": "project/src/main.py", "hash": "h1", "size": 100, "mtime": 1000},
            ]
        )

        # Without referenced_paths, root would be "project/src"
        result_without = partition_manifest(manifest)
        assert result_without[0][0] == "project/src"

        # With referenced_paths at project level, root should be "project"
        result_with = partition_manifest(
            manifest,
            referenced_paths=["project/output"],
        )
        assert result_with[0][0] == "project"

    def test_referenced_paths_creates_additional_roots(self) -> None:
        """referenced_paths can create additional roots."""
        manifest = self._create_manifest(
            files=[
                {"path": "project/src/main.py", "hash": "h1", "size": 100, "mtime": 1000},
            ]
        )

        result = partition_manifest(
            manifest,
            roots=["project"],
            referenced_paths=["other/output"],
        )

        # Should have explicit root plus auto-determined root for referenced_paths
        roots = [r for r, _ in result]
        assert "project" in roots
        assert any("other" in r for r in roots)


class TestPartitionManifestValidation:
    """Tests for validation and error handling."""

    def _create_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AbsSnapshotManifest:
        """Helper to create a v2025 manifest."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        return AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            files=file_entries,
            total_size=0,
        )

    def test_overlapping_roots_raises_error(self) -> None:
        """Overlapping roots raise ValueError."""
        manifest = self._create_manifest(
            files=[{"path": "a/b/c/file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        with pytest.raises(ValueError, match="subpath"):
            partition_manifest(manifest, roots=["a/b", "a/b/c"])

    def test_relative_root_with_absolute_manifest_raises_error(self) -> None:
        """Relative root with absolute manifest paths raises ValueError."""
        if os.name == "nt":
            abs_path = "C:/Users/user/file.txt"
        else:
            abs_path = "/home/user/file.txt"

        manifest = self._create_manifest(
            files=[{"path": abs_path, "hash": "h1", "size": 100, "mtime": 1000}]
        )

        with pytest.raises(ValueError, match="relative.*absolute"):
            partition_manifest(manifest, roots=["subdir"])

    def test_absolute_root_with_relative_manifest_raises_error(self) -> None:
        """Absolute root with relative manifest paths raises ValueError."""
        manifest = self._create_manifest(
            files=[{"path": "assets/file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        if os.name == "nt":
            abs_root = "C:/Users/user/assets"
        else:
            abs_root = "/home/user/assets"

        with pytest.raises(ValueError, match="absolute.*relative"):
            partition_manifest(manifest, roots=[abs_root])

    def test_preserve_policy_raises_error(self) -> None:
        """PRESERVE policy raises ValueError."""
        manifest = self._create_manifest(
            files=[{"path": "a/b/file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        with pytest.raises(ValueError, match="preserve.*not supported"):
            partition_manifest(manifest, roots=["a/b"], symlink_policy=SymlinkPolicy.PRESERVE)

    def test_transitive_include_targets_raises_error(self) -> None:
        """TRANSITIVE_INCLUDE_TARGETS policy raises ValueError."""
        manifest = self._create_manifest(
            files=[{"path": "a/b/file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        with pytest.raises(ValueError, match="transitive_include_targets.*not supported"):
            partition_manifest(
                manifest, roots=["a/b"], symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS
            )

    def test_referenced_paths_style_mismatch_raises_error(self) -> None:
        """referenced_paths with wrong path style raises ValueError."""
        manifest = self._create_manifest(
            files=[{"path": "assets/file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        if os.name == "nt":
            abs_ref = "C:/Users/output"
        else:
            abs_ref = "/home/user/output"

        with pytest.raises(ValueError, match="absolute.*relative"):
            partition_manifest(manifest, referenced_paths=[abs_ref])


class TestPartitionManifestSymlinks:
    """Tests for symlink handling in partitioning."""

    def _create_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> RelSnapshotManifest:
        """Helper to create a relative manifest."""
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
            files=file_entries,
            total_size=total_size,
        )

    def test_escaping_symlink_collapsed(self) -> None:
        """Escaping symlinks are collapsed with COLLAPSE_ESCAPING."""
        manifest = self._create_manifest(
            files=[
                {"path": "project/src/main.py", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "project/src/link", "symlink_target": "shared/lib.py"},
                {"path": "shared/lib.py", "hash": "h2", "size": 200, "mtime": 2000},
            ],
            dirs=[
                {"path": "project"},
                {"path": "project/src"},
                {"path": "shared"},
            ],
        )

        result = partition_manifest(
            manifest,
            roots=["project", "shared"],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        # Find the project partition
        project_partition = next((m for r, m in result if r == "project"), None)
        assert project_partition is not None

        # The symlink should be collapsed
        link_entry = next((p for p in project_partition.files if p.path == "src/link"), None)
        assert link_entry is not None
        assert link_entry.symlink_target is None
        assert link_entry.hash == "h2"

    def test_escaping_symlink_excluded(self) -> None:
        """Escaping symlinks are excluded with EXCLUDE policy."""
        manifest = self._create_manifest(
            files=[
                {"path": "project/src/main.py", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "project/src/link", "symlink_target": "shared/lib.py"},
            ],
            dirs=[{"path": "project"}, {"path": "project/src"}],
        )

        result = partition_manifest(
            manifest,
            roots=["project"],
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        _, project_manifest = result[0]
        paths = {p.path for p in project_manifest.files}
        assert "src/link" not in paths
        assert "src/main.py" in paths


class TestPartitionManifestOrdering:
    """Tests for output ordering."""

    def _create_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> RelSnapshotManifest:
        """Helper to create a relative manifest."""
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
            files=file_entries,
            total_size=total_size,
        )

    def test_explicit_roots_first_in_order(self) -> None:
        """Explicit roots appear first in the order provided."""
        manifest = self._create_manifest(
            files=[
                {"path": "z/file.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "a/file.txt", "hash": "h2", "size": 100, "mtime": 1000},
                {"path": "m/file.txt", "hash": "h3", "size": 100, "mtime": 1000},
            ]
        )

        result = partition_manifest(manifest, roots=["z", "a", "m"])

        roots = [r for r, _ in result]
        assert roots == ["z", "a", "m"]

    def test_auto_roots_sorted_alphabetically(self) -> None:
        """Auto-determined roots are sorted alphabetically."""
        manifest = self._create_manifest(
            files=[
                {"path": "zebra/file.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "apple/file.txt", "hash": "h2", "size": 100, "mtime": 1000},
                {"path": "mango/file.txt", "hash": "h3", "size": 100, "mtime": 1000},
            ]
        )

        result = partition_manifest(
            manifest,
            roots=["zebra"],  # Only zebra is explicit
        )

        roots = [r for r, _ in result]
        assert roots[0] == "zebra"  # Explicit first
        # Remaining should be sorted
        remaining = roots[1:]
        assert remaining == sorted(remaining)


class TestPartitionManifestAdditionalRoots:
    """Tests for auto-determining additional roots beyond explicit ones.

    These tests cover edge cases where explicit roots are provided but don't
    cover all paths in the manifest, requiring additional roots to be determined.
    """

    def _create_abs_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> AbsSnapshotManifest:
        """Helper to create an absolute manifest."""
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
            files=file_entries,
            total_size=total_size,
        )

    def _create_rel_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
    ) -> RelSnapshotManifest:
        """Helper to create a relative manifest."""
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
            files=file_entries,
            total_size=total_size,
        )

    def test_windows_explicit_root_with_remainder_same_drive(self) -> None:
        """Windows: explicit root with remainder on same drive finds deepest common root."""
        manifest = self._create_abs_manifest(
            files=[
                {"path": "C:/projects/scene/model.blend", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "C:/data/textures/wood.png", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "C:/data/textures/metal.png", "hash": "h3", "size": 300, "mtime": 3000},
            ]
        )

        with patch.object(os, "name", "nt"):
            result = partition_manifest(manifest, roots=["C:/projects/scene"])

        roots = [r for r, _ in result]
        assert roots[0] == "C:/projects/scene"
        # Remainder should be C:/data/textures (deepest common prefix)
        assert "C:/data/textures" in roots

    def test_windows_explicit_root_with_remainder_multiple_drives(self) -> None:
        """Windows: explicit root with remainder across multiple drives."""
        manifest = self._create_abs_manifest(
            files=[
                {"path": "C:/projects/scene/model.blend", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "D:/assets/textures/wood.png", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "E:/cache/temp/data.bin", "hash": "h3", "size": 300, "mtime": 3000},
            ]
        )

        with patch.object(os, "name", "nt"):
            result = partition_manifest(manifest, roots=["C:/projects/scene"])

        roots = [r for r, _ in result]
        assert roots[0] == "C:/projects/scene"
        # Each drive should get its own deepest root
        remaining = sorted(roots[1:])
        assert remaining == ["D:/assets/textures", "E:/cache/temp"]

    def test_windows_explicit_root_with_unc_remainder(self) -> None:
        """Windows: explicit root with UNC path remainder."""
        manifest = self._create_abs_manifest(
            files=[
                {"path": "C:/projects/scene/model.blend", "hash": "h1", "size": 100, "mtime": 1000},
                {
                    "path": "//server/share/assets/texture.png",
                    "hash": "h2",
                    "size": 200,
                    "mtime": 2000,
                },
                {
                    "path": "//server/share/assets/model.obj",
                    "hash": "h3",
                    "size": 300,
                    "mtime": 3000,
                },
            ]
        )

        with patch.object(os, "name", "nt"):
            result = partition_manifest(manifest, roots=["C:/projects/scene"])

        roots = [r for r, _ in result]
        assert roots[0] == "C:/projects/scene"
        # UNC remainder should be deepest common prefix
        assert "//server/share/assets" in roots

    def test_relative_explicit_root_with_remainder(self) -> None:
        """Relative paths: explicit root with remainder finds deepest common root."""
        manifest = self._create_rel_manifest(
            files=[
                {"path": "project/src/main.py", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "libs/common/utils.py", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "libs/common/helpers.py", "hash": "h3", "size": 300, "mtime": 3000},
                {"path": "data/cache/temp.bin", "hash": "h4", "size": 400, "mtime": 4000},
            ]
        )

        result = partition_manifest(manifest, roots=["project/src"])

        roots = [r for r, _ in result]
        assert roots[0] == "project/src"
        # Remainder should be deepest common prefixes per top-level
        remaining = sorted(roots[1:])
        assert remaining == ["data/cache", "libs/common"]

    def test_referenced_paths_only_introduces_remainder(self) -> None:
        """referenced_paths introduces additional root when no manifest files there."""
        manifest = self._create_rel_manifest(
            files=[
                {"path": "project/src/main.py", "hash": "h1", "size": 100, "mtime": 1000},
            ]
        )

        result = partition_manifest(
            manifest,
            roots=["project"],
            referenced_paths=["output/renders/final"],
        )

        roots = [r for r, _ in result]
        assert "project" in roots
        # referenced_paths should create additional root
        assert "output/renders/final" in roots

    def test_referenced_paths_deepens_remainder_root(self) -> None:
        """referenced_paths affects the depth of remainder root determination."""
        manifest = self._create_rel_manifest(
            files=[
                {"path": "project/src/main.py", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "data/assets/texture.png", "hash": "h2", "size": 200, "mtime": 2000},
            ]
        )

        # Without referenced_paths, remainder root would be data/assets
        result_without = partition_manifest(manifest, roots=["project"])
        roots_without = [r for r, _ in result_without]
        assert "data/assets" in roots_without

        # With referenced_paths at data level, remainder root should be data
        result_with = partition_manifest(
            manifest,
            roots=["project"],
            referenced_paths=["data/cache"],
        )
        roots_with = [r for r, _ in result_with]
        assert "data" in roots_with

    def test_posix_many_remainder_paths_same_toplevel(self) -> None:
        """POSIX: many remainder paths under same top-level find deepest common."""
        manifest = self._create_abs_manifest(
            files=[
                {"path": "/projects/scene/model.blend", "hash": "h1", "size": 100, "mtime": 1000},
                {
                    "path": "/data/assets/textures/wood.png",
                    "hash": "h2",
                    "size": 200,
                    "mtime": 2000,
                },
                {
                    "path": "/data/assets/textures/metal.png",
                    "hash": "h3",
                    "size": 300,
                    "mtime": 3000,
                },
                {"path": "/data/assets/models/chair.obj", "hash": "h4", "size": 400, "mtime": 4000},
                {"path": "/data/assets/models/table.obj", "hash": "h5", "size": 500, "mtime": 5000},
            ]
        )

        with patch.object(os, "name", "posix"):
            result = partition_manifest(manifest, roots=["/projects/scene"])

        roots = [r for r, _ in result]
        assert roots[0] == "/projects/scene"
        # All /data paths share /data/assets as common prefix
        assert "/data/assets" in roots

    def test_posix_remainder_with_nested_explicit_root(self) -> None:
        """POSIX: remainder paths when explicit root is deeply nested."""
        manifest = self._create_abs_manifest(
            files=[
                {
                    "path": "/projects/client/job/scene/assets/model.blend",
                    "hash": "h1",
                    "size": 100,
                    "mtime": 1000,
                },
                {"path": "/shared/lib/utils.py", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "/tmp/cache/data.bin", "hash": "h3", "size": 300, "mtime": 3000},
            ]
        )

        with patch.object(os, "name", "posix"):
            result = partition_manifest(manifest, roots=["/projects/client/job/scene/assets"])

        roots = [r for r, _ in result]
        assert roots[0] == "/projects/client/job/scene/assets"
        remaining = sorted(roots[1:])
        # Each top-level gets its deepest path
        assert remaining == ["/shared/lib", "/tmp/cache"]

    def test_multiple_explicit_roots_with_remainder(self) -> None:
        """Multiple explicit roots with paths not covered by any."""
        manifest = self._create_rel_manifest(
            files=[
                {"path": "project/src/main.py", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "project/tests/test_main.py", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "libs/utils/helpers.py", "hash": "h3", "size": 300, "mtime": 3000},
            ]
        )

        result = partition_manifest(manifest, roots=["project/src", "project/tests"])

        roots = [r for r, _ in result]
        assert roots[0] == "project/src"
        assert roots[1] == "project/tests"
        # Remainder
        assert "libs/utils" in roots

    def test_explicit_root_covers_all_no_remainder(self) -> None:
        """Explicit root that covers all paths produces no remainder roots."""
        manifest = self._create_rel_manifest(
            files=[
                {"path": "project/src/main.py", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "project/src/utils.py", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "project/tests/test.py", "hash": "h3", "size": 300, "mtime": 3000},
            ]
        )

        result = partition_manifest(manifest, roots=["project"])

        roots = [r for r, _ in result]
        assert roots == ["project"]

    def test_posix_single_file_remainder(self) -> None:
        """POSIX: single file in remainder gets its parent as root."""
        manifest = self._create_abs_manifest(
            files=[
                {"path": "/projects/scene/model.blend", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "/etc/config.ini", "hash": "h2", "size": 200, "mtime": 2000},
            ]
        )

        with patch.object(os, "name", "posix"):
            result = partition_manifest(manifest, roots=["/projects/scene"])

        roots = [r for r, _ in result]
        assert roots[0] == "/projects/scene"
        # Single file's parent directory becomes the root
        assert "/etc" in roots

    def test_windows_mixed_drives_and_unc_remainder(self) -> None:
        """Windows: remainder with both drive letters and UNC paths."""
        manifest = self._create_abs_manifest(
            files=[
                {"path": "C:/projects/scene/model.blend", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "D:/data/texture1.png", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "D:/data/texture2.png", "hash": "h3", "size": 300, "mtime": 3000},
                {"path": "//server/share/lib/utils.py", "hash": "h4", "size": 400, "mtime": 4000},
                {"path": "//server/share/lib/helpers.py", "hash": "h5", "size": 500, "mtime": 5000},
                {"path": "//other/backup/file.dat", "hash": "h6", "size": 600, "mtime": 6000},
            ]
        )

        with patch.object(os, "name", "nt"):
            result = partition_manifest(manifest, roots=["C:/projects/scene"])

        roots = [r for r, _ in result]
        assert roots[0] == "C:/projects/scene"
        remaining = sorted(roots[1:])
        # D: drive, two UNC roots
        assert remaining == ["//other/backup", "//server/share/lib", "D:/data"]

    def test_remainder_with_common_prefix_at_different_depths(self) -> None:
        """Remainder paths with varying depths find appropriate common prefixes."""
        manifest = self._create_rel_manifest(
            files=[
                {"path": "project/main.py", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "libs/a/b/c/deep.py", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "libs/a/b/c/deeper.py", "hash": "h3", "size": 300, "mtime": 3000},
                {"path": "libs/a/shallow.py", "hash": "h4", "size": 400, "mtime": 4000},
            ]
        )

        result = partition_manifest(manifest, roots=["project"])

        roots = [r for r, _ in result]
        assert roots[0] == "project"
        # Common prefix of all libs paths is libs/a
        assert "libs/a" in roots

    def test_posix_explicit_root_is_subpath_of_potential_remainder(self) -> None:
        """POSIX: explicit root prevents its ancestors from being remainder roots."""
        manifest = self._create_abs_manifest(
            files=[
                {
                    "path": "/data/project/scene/model.blend",
                    "hash": "h1",
                    "size": 100,
                    "mtime": 1000,
                },
                {"path": "/data/shared/texture.png", "hash": "h2", "size": 200, "mtime": 2000},
                {"path": "/home/user/cache.bin", "hash": "h3", "size": 300, "mtime": 3000},
            ]
        )

        with patch.object(os, "name", "posix"):
            result = partition_manifest(manifest, roots=["/data/project/scene"])

        roots = [r for r, _ in result]
        assert roots[0] == "/data/project/scene"
        # /data can't be a remainder root because /data/project/scene is under it
        # So we should get /data/shared and /home/user separately
        remaining = sorted(roots[1:])
        assert remaining == ["/data/shared", "/home/user"]
