# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for download_manifest hash cache skip functionality.

These tests verify that:
- Files are skipped when hash cache indicates they already have correct content
- Hash cache is updated after downloading files
- Second download of same manifest skips all files
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import patch

from deadline.job_attachments._snapshots import (
    collect_manifest,
    hash_upload_manifest,
    download_manifest,
    join_manifest,
    subtree_manifest,
    FileSystemDataCache,
)
from deadline.job_attachments._snapshots import AbsSnapshot
from deadline.job_attachments.caches.hash_cache import HashCache


def _to_abs_snapshot(manifest: object) -> AbsSnapshot:
    """Cast a manifest to AbsSnapshot for type checking."""
    return cast(AbsSnapshot, manifest)


class TestDownloadManifestHashCacheSkip:
    """Tests for hash cache skip optimization."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_second_download_skips_files_with_hash_cache(self, tmp_path: Path) -> None:
        """Test that downloading the same manifest twice skips files on second download."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()
        hash_cache_dir = tmp_path / "hash_cache"

        # Create test files
        (source_dir / "file1.txt").write_text("Content 1")
        (source_dir / "file2.txt").write_text("Content 2")

        # Collect and upload
        collected = collect_manifest([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        hashed = hash_upload_manifest(collected, data_cache)

        # Create download manifest
        rel_manifest = subtree_manifest(hashed, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        with HashCache(str(hash_cache_dir)) as hash_cache:
            # First download - should download all files
            result1 = download_manifest(
                manifest=download_manifest_obj,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            assert result1.statistics.processed_files == 2
            assert result1.statistics.skipped_files == 0

            # Verify files were downloaded
            assert (download_dir / "file1.txt").read_text() == "Content 1"
            assert (download_dir / "file2.txt").read_text() == "Content 2"

            # Second download - should skip all files (hash cache hit)
            result2 = download_manifest(
                manifest=download_manifest_obj,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            assert result2.statistics.processed_files == 0
            assert result2.statistics.skipped_files == 2

    def test_hash_cache_skip_does_not_call_filesystem_copy(self, tmp_path: Path) -> None:
        """Test that hash cache skip actually avoids filesystem operations."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()
        hash_cache_dir = tmp_path / "hash_cache"

        # Create test file
        (source_dir / "test.txt").write_text("Test content")

        # Collect and upload
        collected = collect_manifest([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        hashed = hash_upload_manifest(collected, data_cache)

        # Create download manifest
        rel_manifest = subtree_manifest(hashed, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        with HashCache(str(hash_cache_dir)) as hash_cache:
            # First download
            download_manifest(
                manifest=download_manifest_obj,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            # Patch the filesystem copy function to track calls
            with patch(
                "deadline.job_attachments._snapshots._operations._download_manifest_file_system.download_file_from_filesystem"
            ) as mock_download:
                # Second download - should skip without calling download function
                result = download_manifest(
                    manifest=download_manifest_obj,
                    data_cache=data_cache,
                    hash_cache=hash_cache,
                )

                # Verify download function was NOT called
                mock_download.assert_not_called()
                assert result.statistics.skipped_files == 1
                assert result.statistics.processed_files == 0

    def test_modified_file_is_redownloaded(self, tmp_path: Path) -> None:
        """Test that modifying a file causes it to be re-downloaded."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()
        hash_cache_dir = tmp_path / "hash_cache"

        # Create test file
        (source_dir / "test.txt").write_text("Original content")

        # Collect and upload
        collected = collect_manifest([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        hashed = hash_upload_manifest(collected, data_cache)

        # Create download manifest
        rel_manifest = subtree_manifest(hashed, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        with HashCache(str(hash_cache_dir)) as hash_cache:
            # First download
            result1 = download_manifest(
                manifest=download_manifest_obj,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )
            assert result1.statistics.processed_files == 1

            # Modify the downloaded file (changes mtime)
            downloaded_file = download_dir / "test.txt"
            downloaded_file.write_text("Modified content")

            # Second download - should re-download because mtime changed
            result2 = download_manifest(
                manifest=download_manifest_obj,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            # File should be re-downloaded (mtime changed, so cache miss)
            assert result2.statistics.processed_files == 1
            assert result2.statistics.skipped_files == 0

            # Content should be restored to original
            assert downloaded_file.read_text() == "Original content"

    def test_deleted_file_is_redownloaded(self, tmp_path: Path) -> None:
        """Test that deleting a file causes it to be re-downloaded."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()
        hash_cache_dir = tmp_path / "hash_cache"

        # Create test file
        (source_dir / "test.txt").write_text("Test content")

        # Collect and upload
        collected = collect_manifest([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        hashed = hash_upload_manifest(collected, data_cache)

        # Create download manifest
        rel_manifest = subtree_manifest(hashed, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        with HashCache(str(hash_cache_dir)) as hash_cache:
            # First download
            result1 = download_manifest(
                manifest=download_manifest_obj,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )
            assert result1.statistics.processed_files == 1

            # Delete the downloaded file
            downloaded_file = download_dir / "test.txt"
            downloaded_file.unlink()
            assert not downloaded_file.exists()

            # Second download - should re-download because file doesn't exist
            result2 = download_manifest(
                manifest=download_manifest_obj,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            assert result2.statistics.processed_files == 1
            assert result2.statistics.skipped_files == 0
            assert downloaded_file.exists()
            assert downloaded_file.read_text() == "Test content"

    def test_without_hash_cache_always_downloads(self, tmp_path: Path) -> None:
        """Test that without hash cache, files are always downloaded."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        # Create test file
        (source_dir / "test.txt").write_text("Test content")

        # Collect and upload
        collected = collect_manifest([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        hashed = hash_upload_manifest(collected, data_cache)

        # Create download manifest
        rel_manifest = subtree_manifest(hashed, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        # First download without hash cache
        result1 = download_manifest(
            manifest=download_manifest_obj,
            data_cache=data_cache,
            # No hash_cache provided
        )
        assert result1.statistics.processed_files == 1

        # Second download without hash cache - still downloads
        result2 = download_manifest(
            manifest=download_manifest_obj,
            data_cache=data_cache,
            # No hash_cache provided
        )
        # Without hash cache, file conflict resolution (default OVERWRITE) applies
        # File exists but gets overwritten
        assert result2.statistics.processed_files == 1
