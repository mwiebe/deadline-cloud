# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for download_manifest diff manifest support.

These tests cover:
- File deletions from diff manifests
- Directory deletions (empty only)
- apply_deletes parameter
- Deletion ordering (children before parents)
"""

from __future__ import annotations

from pathlib import Path

from deadline.job_attachments.asset_manifests._operations import (
    download_manifest,
    FileSystemDataCache,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.manifest import (
    AbsDiffManifest,
    ManifestFilePath,
    ManifestDirectoryPath,
)


class TestDownloadManifestDiff:
    """Tests for diff manifest download (deletions)."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_diff_deletes_files(self, tmp_path: Path) -> None:
        """Test that diff manifest deletions remove files."""
        cache_root = tmp_path / "cache"
        target_dir = tmp_path / "target"
        target_dir.mkdir()

        file_to_delete = target_dir / "delete_me.txt"
        file_to_delete.write_text("Delete this")
        file_to_keep = target_dir / "keep_me.txt"
        file_to_keep.write_text("Keep this")

        abs_path = str(file_to_delete).replace("\\", "/")
        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[ManifestFilePath(path=abs_path, deleted=True)],
            dirs=[],
            total_size=0,
            parent_manifest_hash="abc123",
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        download_manifest(manifest=diff_manifest, data_cache=data_cache, apply_deletes=True)

        assert not file_to_delete.exists()
        assert file_to_keep.exists()

    def test_diff_apply_deletes_false_skips_deletions(self, tmp_path: Path) -> None:
        """Test that apply_deletes=False skips deletions."""
        cache_root = tmp_path / "cache"
        target_dir = tmp_path / "target"
        target_dir.mkdir()

        file_to_delete = target_dir / "delete_me.txt"
        file_to_delete.write_text("Delete this")

        abs_path = str(file_to_delete).replace("\\", "/")
        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[ManifestFilePath(path=abs_path, deleted=True)],
            dirs=[],
            total_size=0,
            parent_manifest_hash="abc123",
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        download_manifest(manifest=diff_manifest, data_cache=data_cache, apply_deletes=False)

        assert file_to_delete.exists()

    def test_diff_deletes_empty_directory(self, tmp_path: Path) -> None:
        """Test that diff manifest can delete empty directories."""
        cache_root = tmp_path / "cache"
        target_dir = tmp_path / "target"
        target_dir.mkdir()

        dir_to_delete = target_dir / "empty_dir"
        dir_to_delete.mkdir()

        abs_path = str(dir_to_delete).replace("\\", "/")
        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            dirs=[ManifestDirectoryPath(path=abs_path, deleted=True)],
            total_size=0,
            parent_manifest_hash="abc123",
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        download_manifest(manifest=diff_manifest, data_cache=data_cache, apply_deletes=True)

        assert not dir_to_delete.exists()

    def test_diff_non_empty_directory_not_deleted(self, tmp_path: Path) -> None:
        """Test that non-empty directories are not deleted."""
        cache_root = tmp_path / "cache"
        target_dir = tmp_path / "target"
        target_dir.mkdir()

        dir_to_delete = target_dir / "non_empty_dir"
        dir_to_delete.mkdir()
        (dir_to_delete / "file.txt").write_text("Content")

        abs_path = str(dir_to_delete).replace("\\", "/")
        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            dirs=[ManifestDirectoryPath(path=abs_path, deleted=True)],
            total_size=0,
            parent_manifest_hash="abc123",
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        download_manifest(manifest=diff_manifest, data_cache=data_cache, apply_deletes=True)

        assert dir_to_delete.exists()
        assert (dir_to_delete / "file.txt").exists()

    def test_deletion_order_children_before_parents(self, tmp_path: Path) -> None:
        """Test that deletions are ordered so children are deleted before parents."""
        cache_root = tmp_path / "cache"
        target_dir = tmp_path / "target"
        target_dir.mkdir()

        parent_dir = target_dir / "parent"
        parent_dir.mkdir()
        child_dir = parent_dir / "child"
        child_dir.mkdir()
        file_in_child = child_dir / "file.txt"
        file_in_child.write_text("Content")

        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[ManifestFilePath(path=str(file_in_child).replace("\\", "/"), deleted=True)],
            dirs=[
                ManifestDirectoryPath(path=str(parent_dir).replace("\\", "/"), deleted=True),
                ManifestDirectoryPath(path=str(child_dir).replace("\\", "/"), deleted=True),
            ],
            total_size=0,
            parent_manifest_hash="abc123",
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        download_manifest(manifest=diff_manifest, data_cache=data_cache, apply_deletes=True)

        assert not file_in_child.exists()
        assert not child_dir.exists()
        assert not parent_dir.exists()


class TestDownloadManifestDiffSymlinks:
    """Tests for diff manifest symlink deletions."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_diff_deletes_symlink(self, tmp_path: Path) -> None:
        """Test that diff manifest deletions remove symlinks."""
        cache_root = tmp_path / "cache"
        target_dir = tmp_path / "target"
        target_dir.mkdir()

        # Create a target file and a symlink to it
        target_file = target_dir / "target.txt"
        target_file.write_text("Target content")
        symlink_to_delete = target_dir / "link.txt"
        symlink_to_delete.symlink_to(target_file)

        abs_path = str(symlink_to_delete).replace("\\", "/")
        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[ManifestFilePath(path=abs_path, deleted=True)],
            dirs=[],
            total_size=0,
            parent_manifest_hash="abc123",
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        download_manifest(manifest=diff_manifest, data_cache=data_cache, apply_deletes=True)

        # Symlink should be deleted, but target should remain
        assert not symlink_to_delete.exists()
        assert target_file.exists()
        assert target_file.read_text() == "Target content"
