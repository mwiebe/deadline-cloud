# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for download_manifest file conflict resolution options.

These tests cover:
- SKIP: Skip download if file already exists
- OVERWRITE: Overwrite existing file with downloaded content
- CREATE_COPY: Create a new file with suffix if file exists
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
    hash_upload_manifest,
    download_manifest,
    join_manifest,
    subtree_manifest,
    FileSystemDataCache,
)
from deadline.job_attachments.asset_manifests._manifest import AbsSnapshotManifest
from deadline.job_attachments.models import FileConflictResolution


def _to_abs_snapshot(manifest: object) -> AbsSnapshotManifest:
    """Cast a manifest to AbsSnapshotManifest for type checking."""
    return cast(AbsSnapshotManifest, manifest)


class TestDownloadManifestFileConflictResolution:
    """Tests for file conflict resolution options."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_conflict_skip(self, tmp_path: Path) -> None:
        """Test SKIP conflict resolution skips existing files."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        test_file = source_dir / "test.txt"
        test_file.write_text("New content")

        existing_file = download_dir / "test.txt"
        existing_file.write_text("Existing content")

        collected = collect_manifest([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        hashed = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(hashed, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        stats = download_manifest(
            manifest=download_manifest_obj,
            data_cache=data_cache,
            file_conflict_resolution=FileConflictResolution.SKIP,
        )

        assert existing_file.read_text() == "Existing content"
        assert stats.skipped_files == 1

    def test_conflict_overwrite(self, tmp_path: Path) -> None:
        """Test OVERWRITE conflict resolution overwrites existing files."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        test_file = source_dir / "test.txt"
        test_file.write_text("New content")

        existing_file = download_dir / "test.txt"
        existing_file.write_text("Existing content")

        collected = collect_manifest([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        hashed = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(hashed, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        stats = download_manifest(
            manifest=download_manifest_obj,
            data_cache=data_cache,
            file_conflict_resolution=FileConflictResolution.OVERWRITE,
        )

        assert existing_file.read_text() == "New content"
        assert stats.processed_files == 1

    def test_conflict_create_copy(self, tmp_path: Path) -> None:
        """Test CREATE_COPY conflict resolution creates numbered copies."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        test_file = source_dir / "test.txt"
        test_file.write_text("New content")

        existing_file = download_dir / "test.txt"
        existing_file.write_text("Existing content")

        collected = collect_manifest([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        hashed = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(hashed, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        stats = download_manifest(
            manifest=download_manifest_obj,
            data_cache=data_cache,
            file_conflict_resolution=FileConflictResolution.CREATE_COPY,
        )

        assert existing_file.read_text() == "Existing content"
        copy_file = download_dir / "test (1).txt"
        assert copy_file.exists()
        assert copy_file.read_text() == "New content"
        assert stats.processed_files == 1
