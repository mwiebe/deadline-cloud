# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash cache support with chunked file downloads.

These tests verify that:
1. Chunked files can be skipped when all chunk hashes match in the hash cache
2. Chunked files are re-downloaded when any chunk hash doesn't match
3. Hash cache is updated with all chunk hashes after successful download
"""

import pytest

from deadline.job_attachments._snapshots._manifest import (
    AbsSnapshot,
    ManifestFilePath,
)
from deadline.job_attachments._snapshots._content_addressed_data_cache import (
    FileSystemDataCache,
)
from deadline.job_attachments._snapshots._operations._download_abs_manifest import (
    download_abs_manifest,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.caches.hash_cache import HashCache, HashCacheEntry
from deadline.job_attachments.models import FileConflictResolution


# Chunk size used in tests (1KB for simplicity)
TEST_CHUNK_SIZE = 1024


class TestDownloadAbsManifestChunkedHashCacheIntegration:
    """Integration tests for chunked file hash cache with download_abs_manifest."""

    @pytest.fixture
    def setup_data_cache(self, tmp_path):
        """Set up a filesystem data cache with chunked file content."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        # Create chunk files in the cache
        chunk1_content = b"A" * TEST_CHUNK_SIZE
        chunk2_content = b"B" * 512  # Last chunk is smaller

        chunk1_hash = "chunk1hash"
        chunk2_hash = "chunk2hash"

        # Store chunks in cache (using hash.algorithm as filename)
        (cache_dir / f"{chunk1_hash}.xxh128").write_bytes(chunk1_content)
        (cache_dir / f"{chunk2_hash}.xxh128").write_bytes(chunk2_content)

        data_cache = FileSystemDataCache(cache_dir)

        return data_cache, chunk1_hash, chunk2_hash, chunk1_content + chunk2_content

    def test_chunked_file_skipped_when_hash_cache_matches(self, tmp_path, setup_data_cache):
        """Chunked file should be skipped when all chunk hashes match in cache."""
        data_cache, chunk1_hash, chunk2_hash, full_content = setup_data_cache
        file_size = len(full_content)

        # Create the destination file (already exists with correct content)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        dest_file = dest_dir / "chunked_file.bin"
        dest_file.write_bytes(full_content)
        current_mtime_ns = dest_file.stat().st_mtime_ns

        # Create manifest with chunked file
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(dest_file),
                    hash=None,
                    size=file_size,
                    mtime=current_mtime_ns // 1000,
                    chunkhashes=[chunk1_hash, chunk2_hash],
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=TEST_CHUNK_SIZE,
        )

        # Set up hash cache with matching entries
        with HashCache(cache_dir=str(tmp_path / "hash_cache")) as hash_cache:
            resolved_path = str(dest_file.resolve())
            mtime_str = str(current_mtime_ns)

            # Add chunk entries to cache
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path=resolved_path,
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash=chunk1_hash,
                    last_modified_time=mtime_str,
                    range_start=0,
                    range_end=TEST_CHUNK_SIZE,
                )
            )
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path=resolved_path,
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash=chunk2_hash,
                    last_modified_time=mtime_str,
                    range_start=TEST_CHUNK_SIZE,
                    range_end=file_size,
                )
            )

            # Download should skip the file
            result = download_abs_manifest(
                manifest,
                data_cache,
                hash_cache=hash_cache,
                file_conflict_resolution=FileConflictResolution.OVERWRITE,
            )

        # File should be skipped (not re-downloaded)
        assert result.statistics.skipped_files == 1
        assert result.statistics.processed_files == 0

    def test_chunked_file_downloaded_when_hash_mismatch(self, tmp_path, setup_data_cache):
        """Chunked file should be downloaded when any chunk hash doesn't match."""
        data_cache, chunk1_hash, chunk2_hash, full_content = setup_data_cache
        file_size = len(full_content)

        # Create the destination file with different content
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        dest_file = dest_dir / "chunked_file.bin"
        dest_file.write_bytes(b"old content" + b"\x00" * (file_size - 11))
        current_mtime_ns = dest_file.stat().st_mtime_ns

        # Create manifest with chunked file
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(dest_file),
                    hash=None,
                    size=file_size,
                    mtime=current_mtime_ns // 1000,
                    chunkhashes=[chunk1_hash, chunk2_hash],
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=TEST_CHUNK_SIZE,
        )

        # Set up hash cache with mismatched hash for first chunk
        with HashCache(cache_dir=str(tmp_path / "hash_cache")) as hash_cache:
            resolved_path = str(dest_file.resolve())
            mtime_str = str(current_mtime_ns)

            # Add chunk entries with wrong hash
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path=resolved_path,
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash="wrong_hash",  # Doesn't match chunk1_hash
                    last_modified_time=mtime_str,
                    range_start=0,
                    range_end=TEST_CHUNK_SIZE,
                )
            )

            # Download should re-download the file
            result = download_abs_manifest(
                manifest,
                data_cache,
                hash_cache=hash_cache,
                file_conflict_resolution=FileConflictResolution.OVERWRITE,
            )

        # File should be downloaded (not skipped)
        assert result.statistics.processed_files == 1
        assert result.statistics.skipped_files == 0

        # Verify content was downloaded correctly
        assert dest_file.read_bytes() == full_content

    def test_hash_cache_updated_after_chunked_download(self, tmp_path, setup_data_cache):
        """Hash cache should be updated with all chunk hashes after download."""
        data_cache, chunk1_hash, chunk2_hash, full_content = setup_data_cache
        file_size = len(full_content)

        # Create destination directory (file doesn't exist yet)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        dest_file = dest_dir / "chunked_file.bin"

        # Create manifest with chunked file
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(dest_file),
                    hash=None,
                    size=file_size,
                    mtime=1000000,
                    chunkhashes=[chunk1_hash, chunk2_hash],
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=TEST_CHUNK_SIZE,
        )

        # Download with empty hash cache
        with HashCache(cache_dir=str(tmp_path / "hash_cache")) as hash_cache:
            result = download_abs_manifest(
                manifest,
                data_cache,
                hash_cache=hash_cache,
                file_conflict_resolution=FileConflictResolution.OVERWRITE,
            )

            # Verify file was downloaded
            assert result.statistics.processed_files == 1
            assert dest_file.read_bytes() == full_content

            # Verify hash cache was updated with chunk entries
            resolved_path = str(dest_file.resolve())
            actual_mtime_ns = dest_file.stat().st_mtime_ns

            # Check first chunk entry
            entry1 = hash_cache.get_entry(
                file_path_key=resolved_path,
                hash_algorithm=HashAlgorithm.XXH128,
                range_start=0,
                range_end=TEST_CHUNK_SIZE,
            )
            assert entry1 is not None
            assert entry1.file_hash == chunk1_hash
            assert entry1.last_modified_time == str(actual_mtime_ns)

            # Check second chunk entry
            entry2 = hash_cache.get_entry(
                file_path_key=resolved_path,
                hash_algorithm=HashAlgorithm.XXH128,
                range_start=TEST_CHUNK_SIZE,
                range_end=file_size,
            )
            assert entry2 is not None
            assert entry2.file_hash == chunk2_hash
            assert entry2.last_modified_time == str(actual_mtime_ns)

    def test_second_download_skips_chunked_file(self, tmp_path, setup_data_cache):
        """Second download should skip chunked file due to hash cache."""
        data_cache, chunk1_hash, chunk2_hash, full_content = setup_data_cache
        file_size = len(full_content)

        # Create destination directory
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        dest_file = dest_dir / "chunked_file.bin"

        # Create manifest with chunked file
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(dest_file),
                    hash=None,
                    size=file_size,
                    mtime=1000000,
                    chunkhashes=[chunk1_hash, chunk2_hash],
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=TEST_CHUNK_SIZE,
        )

        with HashCache(cache_dir=str(tmp_path / "hash_cache")) as hash_cache:
            # First download
            result1 = download_abs_manifest(
                manifest,
                data_cache,
                hash_cache=hash_cache,
                file_conflict_resolution=FileConflictResolution.OVERWRITE,
            )
            assert result1.statistics.processed_files == 1
            assert result1.statistics.skipped_files == 0

            # Second download should skip
            result2 = download_abs_manifest(
                manifest,
                data_cache,
                hash_cache=hash_cache,
                file_conflict_resolution=FileConflictResolution.OVERWRITE,
            )
            assert result2.statistics.processed_files == 0
            assert result2.statistics.skipped_files == 1
