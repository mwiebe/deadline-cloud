# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for compose_manifests and related functions.

These tests cover:
- Empty manifest list (raises ValueError)
- Single manifest (returns as-is)
- Snapshot+diffs composition
- Diff+diff composition
- Directory deletion then file added under it (reconciliation)
- Insert path through file node (raises ValueError)
- Type validation errors
"""

from __future__ import annotations

import pytest
from typing import List

from deadline.job_attachments.asset_manifests._operations import (
    compose_manifests,
)
from deadline.job_attachments.asset_manifests._operations._compose_manifest import (
    _ManifestTrieNode,
    _split_path,
)
from deadline.job_attachments.asset_manifests.versions import ManifestType
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.manifest import (
    ManifestFilePath,
    ManifestDirectoryPath,
    AbsSnapshotManifest,
    AbsDiffManifest,
    RelSnapshotManifest,
    RelDiffManifest,
)


class TestSplitPath:
    """Tests for _split_path helper."""

    def test_simple_path(self) -> None:
        """Simple path splits correctly."""
        assert _split_path("dir/file.txt") == ["dir", "file.txt"]

    def test_nested_path(self) -> None:
        """Nested path splits correctly."""
        assert _split_path("a/b/c/d.txt") == ["a", "b", "c", "d.txt"]

    def test_single_component(self) -> None:
        """Single component path."""
        assert _split_path("file.txt") == ["file.txt"]

    def test_leading_slash_preserved(self) -> None:
        """Leading slash results in empty string first component."""
        assert _split_path("/dir/file.txt") == ["", "dir", "file.txt"]

    def test_trailing_slash_ignored(self) -> None:
        """Trailing slash is ignored."""
        assert _split_path("dir/subdir/") == ["dir", "subdir"]

    def test_empty_string(self) -> None:
        """Empty string returns empty list."""
        assert _split_path("") == []


class TestManifestTrieNode:
    """Tests for _ManifestTrieNode."""

    def test_insert_path_creates_nodes(self) -> None:
        """insert_path creates intermediate nodes."""
        root = _ManifestTrieNode()
        node = root.insert_path(["dir", "subdir", "file.txt"])

        assert "dir" in root.children
        assert "subdir" in root.children["dir"].children
        assert "file.txt" in root.children["dir"].children["subdir"].children
        assert node is root.children["dir"].children["subdir"].children["file.txt"]

    def test_insert_path_reuses_existing_nodes(self) -> None:
        """insert_path reuses existing nodes."""
        root = _ManifestTrieNode()
        root.insert_path(["dir", "file1.txt"])
        root.insert_path(["dir", "file2.txt"])

        assert len(root.children) == 1
        assert len(root.children["dir"].children) == 2

    def test_insert_path_through_file_raises_error(self) -> None:
        """insert_path raises error if intermediate node is a file."""
        root = _ManifestTrieNode()
        file_node = root.insert_path(["dir", "file.txt"])
        file_node.file_entry = ManifestFilePath(
            path="dir/file.txt", hash="abc123", size=100, mtime=1000
        )

        with pytest.raises(ValueError, match="is a file, not a directory"):
            root.insert_path(["dir", "file.txt", "subfile.txt"])

    def test_delete_subtree_removes_node_and_children(self) -> None:
        """delete_subtree removes the target node and all children."""
        root = _ManifestTrieNode()
        root.insert_path(["dir", "subdir", "file.txt"])
        root.insert_path(["dir", "other.txt"])

        result = root.delete_subtree(["dir"])

        assert result is True
        assert "dir" not in root.children

    def test_delete_subtree_nonexistent_returns_false(self) -> None:
        """delete_subtree returns False for nonexistent path."""
        root = _ManifestTrieNode()
        root.insert_path(["dir", "file.txt"])

        result = root.delete_subtree(["dir", "other.txt"])

        assert result is False

    def test_delete_subtree_empty_path_returns_false(self) -> None:
        """delete_subtree returns False for empty path (can't delete root)."""
        root = _ManifestTrieNode()

        result = root.delete_subtree([])

        assert result is False

    def test_delete_if_empty_removes_empty_node(self) -> None:
        """delete_if_empty removes node with no children and no file_entry."""
        root = _ManifestTrieNode()
        root.insert_path(["dir", "empty_subdir"])

        result = root.delete_if_empty(["dir", "empty_subdir"])

        assert result is True
        assert "empty_subdir" not in root.children["dir"].children

    def test_delete_if_empty_preserves_node_with_children(self) -> None:
        """delete_if_empty does not remove node that has children."""
        root = _ManifestTrieNode()
        root.insert_path(["dir", "subdir", "file.txt"])

        result = root.delete_if_empty(["dir", "subdir"])

        assert result is False
        assert "subdir" in root.children["dir"].children

    def test_delete_if_empty_preserves_node_with_file_entry(self) -> None:
        """delete_if_empty does not remove node that has a file_entry."""
        root = _ManifestTrieNode()
        file_node = root.insert_path(["dir", "file.txt"])
        file_node.file_entry = ManifestFilePath(
            path="dir/file.txt", hash="h1", size=100, mtime=1000
        )

        result = root.delete_if_empty(["dir", "file.txt"])

        assert result is False
        assert "file.txt" in root.children["dir"].children

    def test_mark_deleted_creates_node_and_sets_flag(self) -> None:
        """mark_deleted creates node and sets deleted flag."""
        root = _ManifestTrieNode()
        node = root.mark_deleted(["dir", "file.txt"])

        assert node.deleted is True
        assert node.file_entry is None

    def test_mark_deleted_clears_file_entry(self) -> None:
        """mark_deleted clears any existing file_entry."""
        root = _ManifestTrieNode()
        file_node = root.insert_path(["dir", "file.txt"])
        file_node.file_entry = ManifestFilePath(
            path="dir/file.txt", hash="abc123", size=100, mtime=1000
        )

        root.mark_deleted(["dir", "file.txt"])

        assert file_node.file_entry is None
        assert file_node.deleted is True

    def test_iter_files_yields_non_deleted_files(self) -> None:
        """iter_files yields only non-deleted file entries."""
        root = _ManifestTrieNode()

        file1 = root.insert_path(["file1.txt"])
        file1.file_entry = ManifestFilePath(path="file1.txt", hash="h1", size=100, mtime=1000)

        file2 = root.insert_path(["file2.txt"])
        file2.file_entry = ManifestFilePath(path="file2.txt", hash="h2", size=200, mtime=2000)
        file2.deleted = True

        files = list(root.iter_files())

        assert len(files) == 1
        assert files[0][0] == "file1.txt"

    def test_iter_dirs_yields_non_deleted_directories(self) -> None:
        """iter_dirs yields only non-deleted directories."""
        root = _ManifestTrieNode()

        root.insert_path(["dir1"])

        dir2 = root.insert_path(["dir2"])
        dir2.deleted = True

        dirs = list(root.iter_dirs())

        assert "dir1" in dirs
        assert "dir2" not in dirs

    def test_reconcile_deleted_flags_clears_with_non_deleted_children(self) -> None:
        """reconcile_deleted_flags clears deleted flag if node has non-deleted children."""
        root = _ManifestTrieNode()

        dir1 = root.insert_path(["dir1"])
        dir1.deleted = True

        child = root.insert_path(["dir1", "file.txt"])
        child.file_entry = ManifestFilePath(path="dir1/file.txt", hash="h1", size=100, mtime=1000)

        root.reconcile_deleted_flags()

        assert dir1.deleted is False

    def test_reconcile_deleted_flags_preserves_with_all_deleted_children(self) -> None:
        """reconcile_deleted_flags preserves deleted flag if all children are deleted."""
        root = _ManifestTrieNode()

        dir1 = root.insert_path(["dir1"])
        dir1.deleted = True

        child = root.insert_path(["dir1", "file.txt"])
        child.deleted = True

        root.reconcile_deleted_flags()

        assert dir1.deleted is True

    def test_reconcile_deleted_flags_handles_deep_nesting(self) -> None:
        """reconcile_deleted_flags handles deeply nested structures."""
        root = _ManifestTrieNode()

        a = root.insert_path(["a"])
        a.deleted = True

        b = root.insert_path(["a", "b"])
        b.deleted = True

        c = root.insert_path(["a", "b", "c"])

        file_node = root.insert_path(["a", "b", "c", "file.txt"])
        file_node.file_entry = ManifestFilePath(
            path="a/b/c/file.txt", hash="h1", size=100, mtime=1000
        )

        root.reconcile_deleted_flags()

        assert a.deleted is False
        assert b.deleted is False
        assert c.deleted is False


class TestComposeManifestsValidation:
    """Tests for validation and error handling in compose_manifests."""

    def test_empty_list_raises_error(self) -> None:
        """Empty manifest list raises ValueError."""
        with pytest.raises(ValueError, match="Cannot compose empty list"):
            compose_manifests([])

    def test_single_manifest_returns_as_is(self) -> None:
        """Single manifest is returned unchanged."""
        manifest = RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            paths=[ManifestFilePath(path="file.txt", hash="h1", size=100, mtime=1000)],
            total_size=100,
        )

        result = compose_manifests([manifest])

        assert result is manifest

    def test_snapshot_followed_by_snapshot_raises_error(self) -> None:
        """Snapshot followed by snapshot raises ValueError."""
        snapshot1 = RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[],
            total_size=0,
        )
        snapshot2 = RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[],
            total_size=0,
        )

        with pytest.raises(ValueError, match="must be a DIFF"):
            compose_manifests([snapshot1, snapshot2])

    def test_diff_composition_with_snapshot_raises_error(self) -> None:
        """Diff composition with snapshot in the middle raises ValueError."""
        diff1 = RelDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[],
            total_size=0,
        )
        snapshot = RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[],
            total_size=0,
        )

        with pytest.raises(ValueError, match="must be a DIFF"):
            compose_manifests([diff1, snapshot])


class TestComposeManifestsSnapshotDiffs:
    """Tests for (snapshot, diff, ...) -> snapshot composition."""

    def _create_snapshot(
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

    def _create_diff(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
        parent_hash: str | None = None,
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
            parent_manifest_hash=parent_hash,
        )

    def test_snapshot_with_no_diffs_returns_snapshot(self) -> None:
        """Single snapshot returns as-is."""
        snapshot = self._create_snapshot(
            [{"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )

        result = compose_manifests([snapshot])

        assert result is snapshot

    def test_diff_adds_new_file(self) -> None:
        """Diff adds a new file to the snapshot."""
        snapshot = self._create_snapshot(
            [{"path": "existing.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )
        diff = self._create_diff([{"path": "new.txt", "hash": "h2", "size": 200, "mtime": 2000}])

        result = compose_manifests([snapshot, diff])

        paths = {p.path for p in result.paths}
        assert paths == {"existing.txt", "new.txt"}
        assert result.manifestType == ManifestType.SNAPSHOT

    def test_diff_modifies_existing_file(self) -> None:
        """Diff modifies an existing file."""
        snapshot = self._create_snapshot(
            [{"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )
        diff = self._create_diff([{"path": "file.txt", "hash": "h2", "size": 200, "mtime": 2000}])

        result = compose_manifests([snapshot, diff])

        assert len(result.paths) == 1
        assert result.paths[0].hash == "h2"
        assert result.paths[0].size == 200
        assert result.paths[0].mtime == 2000

    def test_diff_deletes_file(self) -> None:
        """Diff deletes a file from the snapshot."""
        snapshot = self._create_snapshot(
            [
                {"path": "keep.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "delete.txt", "hash": "h2", "size": 200, "mtime": 2000},
            ]
        )
        diff = self._create_diff([{"path": "delete.txt", "deleted": True}])

        result = compose_manifests([snapshot, diff])

        paths = {p.path for p in result.paths}
        assert paths == {"keep.txt"}
        assert all(not p.deleted for p in result.paths)

    def test_diff_deletes_empty_directory(self) -> None:
        """Diff deletes an empty directory."""
        snapshot = self._create_snapshot(
            files=[],
            dirs=[{"path": "keep_dir"}, {"path": "delete_dir"}],
        )
        diff = self._create_diff(
            files=[],
            dirs=[{"path": "delete_dir", "deleted": True}],
        )

        result = compose_manifests([snapshot, diff])

        dir_paths = {d.path for d in result.dirs}
        assert dir_paths == {"keep_dir"}

    def test_multiple_diffs_applied_in_order(self) -> None:
        """Multiple diffs are applied in order."""
        snapshot = self._create_snapshot(
            [{"path": "file.txt", "hash": "v1", "size": 100, "mtime": 1000}]
        )
        diff1 = self._create_diff([{"path": "file.txt", "hash": "v2", "size": 100, "mtime": 2000}])
        diff2 = self._create_diff([{"path": "file.txt", "hash": "v3", "size": 100, "mtime": 3000}])

        result = compose_manifests([snapshot, diff1, diff2])

        assert result.paths[0].hash == "v3"

    def test_add_then_delete_removes_file(self) -> None:
        """File added then deleted is not in result."""
        snapshot = self._create_snapshot([])
        diff1 = self._create_diff([{"path": "temp.txt", "hash": "h1", "size": 100, "mtime": 1000}])
        diff2 = self._create_diff([{"path": "temp.txt", "deleted": True}])

        result = compose_manifests([snapshot, diff1, diff2])

        assert len(result.paths) == 0

    def test_delete_then_add_restores_file(self) -> None:
        """File deleted then added is in result."""
        snapshot = self._create_snapshot(
            [{"path": "file.txt", "hash": "v1", "size": 100, "mtime": 1000}]
        )
        diff1 = self._create_diff([{"path": "file.txt", "deleted": True}])
        diff2 = self._create_diff([{"path": "file.txt", "hash": "v2", "size": 200, "mtime": 3000}])

        result = compose_manifests([snapshot, diff1, diff2])

        assert len(result.paths) == 1
        assert result.paths[0].hash == "v2"

    def test_symlink_handling(self) -> None:
        """Symlinks are handled correctly."""
        snapshot = self._create_snapshot([{"path": "link.txt", "symlink_target": "old_target.txt"}])
        diff = self._create_diff([{"path": "link.txt", "symlink_target": "new_target.txt"}])

        result = compose_manifests([snapshot, diff])

        assert result.paths[0].symlink_target == "new_target.txt"

    def test_runnable_flag_preserved(self) -> None:
        """Runnable flag is preserved in composition."""
        snapshot = self._create_snapshot(
            [{"path": "script.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": False}]
        )
        diff = self._create_diff(
            [{"path": "script.sh", "hash": "h2", "size": 100, "mtime": 2000, "runnable": True}]
        )

        result = compose_manifests([snapshot, diff])

        assert result.paths[0].runnable is True

    def test_chunkhashes_preserved(self) -> None:
        """Chunkhashes are preserved for large files."""
        snapshot = self._create_snapshot([])
        diff = self._create_diff(
            [
                {
                    "path": "large.bin",
                    "chunkhashes": ["c1", "c2", "c3"],
                    "size": 768 * 1024 * 1024,
                    "mtime": 1000,
                }
            ]
        )

        result = compose_manifests([snapshot, diff])

        assert result.paths[0].chunkhashes == ["c1", "c2", "c3"]

    def test_total_size_excludes_symlinks(self) -> None:
        """Total size excludes symlinks."""
        snapshot = self._create_snapshot(
            [
                {"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "link.txt", "symlink_target": "file.txt"},
            ]
        )

        result = compose_manifests([snapshot])

        assert result.totalSize == 100

    def test_returns_rel_snapshot_manifest(self) -> None:
        """Composing RelSnapshotManifest with diffs returns RelSnapshotManifest."""
        snapshot = self._create_snapshot(
            [{"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )
        diff = self._create_diff([{"path": "new.txt", "hash": "h2", "size": 200, "mtime": 2000}])

        result = compose_manifests([snapshot, diff])

        assert isinstance(result, RelSnapshotManifest)
        assert result.manifestType == ManifestType.SNAPSHOT


class TestComposeManifestsSnapshotDiffsAbsolute:
    """Tests for (snapshot, diff, ...) -> snapshot composition with absolute paths."""

    def _create_snapshot(
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

    def _create_diff(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
        parent_hash: str | None = None,
    ) -> AbsDiffManifest:
        """Helper to create an AbsDiffManifest."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
            parent_manifest_hash=parent_hash,
        )

    def test_diff_adds_new_file(self) -> None:
        """Diff adds a new file to the snapshot with absolute paths."""
        snapshot = self._create_snapshot(
            [{"path": "/project/existing.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )
        diff = self._create_diff(
            [{"path": "/project/new.txt", "hash": "h2", "size": 200, "mtime": 2000}]
        )

        result = compose_manifests([snapshot, diff])

        paths = {p.path for p in result.paths}
        assert paths == {"/project/existing.txt", "/project/new.txt"}

    def test_returns_abs_snapshot_manifest(self) -> None:
        """Composing AbsSnapshotManifest with diffs returns AbsSnapshotManifest."""
        snapshot = self._create_snapshot(
            [{"path": "/file.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )
        diff = self._create_diff([{"path": "/new.txt", "hash": "h2", "size": 200, "mtime": 2000}])

        result = compose_manifests([snapshot, diff])

        assert isinstance(result, AbsSnapshotManifest)
        assert result.manifestType == ManifestType.SNAPSHOT


class TestComposeManifestsDiffs:
    """Tests for (diff, diff, ...) -> diff composition."""

    def _create_diff(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
        parent_hash: str | None = None,
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
            parent_manifest_hash=parent_hash,
        )

    def test_single_diff_returns_as_is(self) -> None:
        """Single diff returns as-is."""
        diff = self._create_diff(
            [{"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000}],
            parent_hash="parent123",
        )

        result = compose_manifests([diff])

        assert result is diff

    def test_result_is_diff_type(self) -> None:
        """Composed diffs result in a diff manifest."""
        diff1 = self._create_diff([{"path": "file1.txt", "hash": "h1", "size": 100, "mtime": 1000}])
        diff2 = self._create_diff([{"path": "file2.txt", "hash": "h2", "size": 200, "mtime": 2000}])

        result = compose_manifests([diff1, diff2])

        assert result.manifestType == ManifestType.DIFF

    def test_parent_hash_from_first_diff(self) -> None:
        """parentManifestHash comes from the first diff."""
        diff1 = self._create_diff(
            [{"path": "file1.txt", "hash": "h1", "size": 100, "mtime": 1000}],
            parent_hash="first_parent",
        )
        diff2 = self._create_diff(
            [{"path": "file2.txt", "hash": "h2", "size": 200, "mtime": 2000}],
            parent_hash="second_parent",
        )

        result = compose_manifests([diff1, diff2])

        assert result.parentManifestHash == "first_parent"

    def test_additions_merged(self) -> None:
        """Additions from multiple diffs are merged."""
        diff1 = self._create_diff([{"path": "file1.txt", "hash": "h1", "size": 100, "mtime": 1000}])
        diff2 = self._create_diff([{"path": "file2.txt", "hash": "h2", "size": 200, "mtime": 2000}])

        result = compose_manifests([diff1, diff2])

        paths = {p.path for p in result.paths if not p.deleted}
        assert paths == {"file1.txt", "file2.txt"}

    def test_later_modification_overrides_earlier(self) -> None:
        """Later modification overrides earlier one."""
        diff1 = self._create_diff([{"path": "file.txt", "hash": "v1", "size": 100, "mtime": 1000}])
        diff2 = self._create_diff([{"path": "file.txt", "hash": "v2", "size": 200, "mtime": 2000}])

        result = compose_manifests([diff1, diff2])

        file_entry = next(p for p in result.paths if p.path == "file.txt")
        assert file_entry.hash == "v2"

    def test_deletion_marker_preserved(self) -> None:
        """Deletion markers are preserved in composed diff."""
        diff1 = self._create_diff([{"path": "file.txt", "deleted": True}])

        result = compose_manifests([diff1])

        assert result.paths[0].deleted is True

    def test_add_then_delete_preserves_deletion(self) -> None:
        """File added then deleted still has deletion marker."""
        diff1 = self._create_diff([{"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000}])
        diff2 = self._create_diff([{"path": "file.txt", "deleted": True}])

        result = compose_manifests([diff1, diff2])

        file_entry = next(p for p in result.paths if p.path == "file.txt")
        assert file_entry.deleted is True

    def test_delete_then_add_clears_deletion(self) -> None:
        """File deleted then added clears the deletion marker."""
        diff1 = self._create_diff([{"path": "file.txt", "deleted": True}])
        diff2 = self._create_diff([{"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000}])

        result = compose_manifests([diff1, diff2])

        file_entry = next(p for p in result.paths if p.path == "file.txt")
        assert file_entry.deleted is False
        assert file_entry.hash == "h1"

    def test_directory_deletion_preserved(self) -> None:
        """Directory deletion markers are preserved."""
        diff1 = self._create_diff(
            files=[],
            dirs=[{"path": "old_dir", "deleted": True}],
        )

        result = compose_manifests([diff1])

        dir_entry = next(d for d in result.dirs if d.path == "old_dir")
        assert dir_entry.deleted is True

    def test_directory_deletion_then_file_added_reconciles(self) -> None:
        """Directory deleted then file added under it clears directory deletion."""
        diff1 = self._create_diff(
            files=[{"path": "dir/file.txt", "deleted": True}],
            dirs=[{"path": "dir", "deleted": True}],
        )
        diff2 = self._create_diff(
            files=[{"path": "dir/newfile.txt", "hash": "h1", "size": 100, "mtime": 1000}],
            dirs=[{"path": "dir"}],
        )

        result = compose_manifests([diff1, diff2])

        dir_entry = next((d for d in result.dirs if d.path == "dir" and not d.deleted), None)
        assert dir_entry is not None

        file_entry = next(p for p in result.paths if p.path == "dir/newfile.txt")
        assert file_entry.deleted is False

    def test_nested_directory_deletion_reconciliation(self) -> None:
        """Nested directory deletion is reconciled when child is added."""
        diff1 = self._create_diff(
            files=[{"path": "a/b/c/old.txt", "deleted": True}],
            dirs=[
                {"path": "a", "deleted": True},
                {"path": "a/b", "deleted": True},
                {"path": "a/b/c", "deleted": True},
            ],
        )
        diff2 = self._create_diff(
            files=[{"path": "a/b/c/new.txt", "hash": "h1", "size": 100, "mtime": 1000}],
            dirs=[{"path": "a"}, {"path": "a/b"}, {"path": "a/b/c"}],
        )

        result = compose_manifests([diff1, diff2])

        non_deleted_dirs = {d.path for d in result.dirs if not d.deleted}
        assert "a" in non_deleted_dirs
        assert "a/b" in non_deleted_dirs
        assert "a/b/c" in non_deleted_dirs

    def test_total_size_excludes_deleted_entries(self) -> None:
        """Total size excludes deleted entries."""
        diff1 = self._create_diff(
            [
                {"path": "keep.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "delete.txt", "deleted": True},
            ]
        )

        result = compose_manifests([diff1])

        assert result.totalSize == 100

    def test_returns_rel_diff_manifest(self) -> None:
        """Composing RelDiffManifests returns RelDiffManifest."""
        diff1 = self._create_diff([{"path": "file1.txt", "hash": "h1", "size": 100, "mtime": 1000}])
        diff2 = self._create_diff([{"path": "file2.txt", "hash": "h2", "size": 200, "mtime": 2000}])

        result = compose_manifests([diff1, diff2])

        assert isinstance(result, RelDiffManifest)
        assert result.manifestType == ManifestType.DIFF


class TestComposeManifestsDiffsAbsolute:
    """Tests for (diff, diff, ...) -> diff composition with absolute paths."""

    def _create_diff(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
        parent_hash: str | None = None,
    ) -> AbsDiffManifest:
        """Helper to create an AbsDiffManifest."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
            parent_manifest_hash=parent_hash,
        )

    def test_additions_merged(self) -> None:
        """Additions from multiple diffs are merged with absolute paths."""
        diff1 = self._create_diff(
            [{"path": "/project/file1.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )
        diff2 = self._create_diff(
            [{"path": "/project/file2.txt", "hash": "h2", "size": 200, "mtime": 2000}]
        )

        result = compose_manifests([diff1, diff2])

        paths = {p.path for p in result.paths if not p.deleted}
        assert paths == {"/project/file1.txt", "/project/file2.txt"}

    def test_returns_abs_diff_manifest(self) -> None:
        """Composing AbsDiffManifests returns AbsDiffManifest."""
        diff1 = self._create_diff(
            [{"path": "/file1.txt", "hash": "h1", "size": 100, "mtime": 1000}]
        )
        diff2 = self._create_diff(
            [{"path": "/file2.txt", "hash": "h2", "size": 200, "mtime": 2000}]
        )

        result = compose_manifests([diff1, diff2])

        assert isinstance(result, AbsDiffManifest)
        assert result.manifestType == ManifestType.DIFF


class TestComposeManifestsProgressCallback:
    """Tests for progress callback functionality."""

    def test_callback_for_additions(self) -> None:
        """Progress callback is called for additions."""
        snapshot = RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[],
            total_size=0,
        )
        diff = RelDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[ManifestFilePath(path="new.txt", hash="h1", size=100, mtime=1000)],
            total_size=100,
        )

        messages: List[str] = []
        compose_manifests([snapshot, diff], print_function_callback=messages.append)

        assert any("new.txt" in msg for msg in messages)

    def test_callback_for_deletions(self) -> None:
        """Progress callback is called for deletions."""
        snapshot = RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[ManifestFilePath(path="file.txt", hash="h1", size=100, mtime=1000)],
            total_size=100,
        )
        diff = RelDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[ManifestFilePath(path="file.txt", deleted=True)],
            total_size=0,
        )

        messages: List[str] = []
        compose_manifests([snapshot, diff], print_function_callback=messages.append)

        assert any("deleted" in msg.lower() and "file.txt" in msg for msg in messages)
