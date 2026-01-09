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
from deadline.job_attachments._snapshots._operations._download_manifest import (
    download_manifest,
    _check_hash_cache_for_chunked_skip,
    _update_hash_cache_for_chunked_file,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.caches.hash_cache import HashCache, HashCacheEntry
from deadline.job_attachments.models import FileConflictResolution


# Chunk size used in tests (1KB for simplicity)
TEST_CHUNK_SIZE = 1024


class TestCheckHashCacheForChunkedSkip:
    """Tests for _check_hash_cache_for_chunked_skip function."""

    def test_returns_false_when_no_hash_cache(self):
        """Should return (False, None) when hash_cache is None."""
        entry = ManifestFilePath(
            path="/test/file.txt",
            hash=None,
            size=2048,
            mtime=1000000,
            chunkhashes=["hash1", "hash2"],
        )

        can_skip, mtime = _check_hash_cache_for_chunked_skip(
            entry, HashAlgorithm.XXH128, None, TEST_CHUNK_SIZE
        )

        assert can_skip is False
        assert mtime is None

    def test_returns_false_when_no_chunkhashes(self, tmp_path):
        """Should return (False, None) when entry has no chunkhashes."""
        entry = ManifestFilePath(
            path="/test/file.txt",
            hash="wholehash",
            size=100,
            mtime=1000000,
            chunkhashes=None,
        )

        with HashCache(cache_dir=str(tmp_path / "hash_cache")) as hash_cache:
            can_skip, mtime = _check_hash_cache_for_chunked_skip(
                entry, HashAlgorithm.XXH128, hash_cache, TEST_CHUNK_SIZE
            )

        assert can_skip is False
        assert mtime is None

    def test_returns_false_when_file_does_not_exist(self, tmp_path):
        """Should return (False, None) when local file doesn't exist."""
        entry = ManifestFilePath(
            path=str(tmp_path / "nonexistent.txt"),
            hash=None,
            size=2048,
            mtime=1000000,
            chunkhashes=["hash1", "hash2"],
        )

        with HashCache(cache_dir=str(tmp_path / "hash_cache")) as hash_cache:
            can_skip, mtime = _check_hash_cache_for_chunked_skip(
                entry, HashAlgorithm.XXH128, hash_cache, TEST_CHUNK_SIZE
            )

        assert can_skip is False
        assert mtime is None

    def test_returns_false_when_chunk_not_in_cache(self, tmp_path):
        """Should return (False, None) when any chunk is not in cache."""
        # Create a local file
        local_file = tmp_path / "file.txt"
        local_file.write_bytes(b"x" * 2048)

        entry = ManifestFilePath(
            path=str(local_file),
            hash=None,
            size=2048,
            mtime=1000000,
            chunkhashes=["hash1", "hash2"],
        )

        with HashCache(cache_dir=str(tmp_path / "hash_cache")) as hash_cache:
            # Only add first chunk to cache, second chunk is missing
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path=str(local_file.resolve()),
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash="hash1",
                    last_modified_time=str(local_file.stat().st_mtime_ns),
                    range_start=0,
                    range_end=1024,
                )
            )
            # Second chunk not in cache

            can_skip, mtime = _check_hash_cache_for_chunked_skip(
                entry, HashAlgorithm.XXH128, hash_cache, TEST_CHUNK_SIZE
            )

        assert can_skip is False
        assert mtime is None

    def test_returns_false_when_mtime_mismatch(self, tmp_path):
        """Should return (False, None) when cached mtime doesn't match."""
        # Create a local file
        local_file = tmp_path / "file.txt"
        local_file.write_bytes(b"x" * 2048)

        entry = ManifestFilePath(
            path=str(local_file),
            hash=None,
            size=2048,
            mtime=1000000,
            chunkhashes=["hash1", "hash2"],
        )

        with HashCache(cache_dir=str(tmp_path / "hash_cache")) as hash_cache:
            # Add entry with different mtime than the actual file
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path=str(local_file.resolve()),
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash="hash1",
                    last_modified_time="999999999",  # Different mtime
                    range_start=0,
                    range_end=1024,
                )
            )

            can_skip, mtime = _check_hash_cache_for_chunked_skip(
                entry, HashAlgorithm.XXH128, hash_cache, TEST_CHUNK_SIZE
            )

        assert can_skip is False
        assert mtime is None

    def test_returns_false_when_hash_mismatch(self, tmp_path):
        """Should return (False, None) when cached hash doesn't match expected."""
        # Create a local file
        local_file = tmp_path / "file.txt"
        local_file.write_bytes(b"x" * 2048)
        current_mtime = str(local_file.stat().st_mtime_ns)

        entry = ManifestFilePath(
            path=str(local_file),
            hash=None,
            size=2048,
            mtime=1000000,
            chunkhashes=["hash1", "hash2"],
        )

        with HashCache(cache_dir=str(tmp_path / "hash_cache")) as hash_cache:
            # Add entry with different hash than expected
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path=str(local_file.resolve()),
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash="different_hash",  # Different hash than "hash1"
                    last_modified_time=current_mtime,
                    range_start=0,
                    range_end=1024,
                )
            )

            can_skip, mtime = _check_hash_cache_for_chunked_skip(
                entry, HashAlgorithm.XXH128, hash_cache, TEST_CHUNK_SIZE
            )

        assert can_skip is False
        assert mtime is None

    def test_returns_true_when_all_chunks_match(self, tmp_path):
        """Should return (True, mtime) when all chunk hashes match."""
        # Create a local file
        local_file = tmp_path / "file.txt"
        local_file.write_bytes(b"x" * 2048)
        current_mtime_ns = local_file.stat().st_mtime_ns
        current_mtime_str = str(current_mtime_ns)
        resolved_path = str(local_file.resolve())

        entry = ManifestFilePath(
            path=str(local_file),
            hash=None,
            size=2048,
            mtime=1000000,
            chunkhashes=["hash1", "hash2"],
        )

        with HashCache(cache_dir=str(tmp_path / "hash_cache")) as hash_cache:
            # Add both chunk entries with matching hashes and mtime
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path=resolved_path,
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash="hash1",
                    last_modified_time=current_mtime_str,
                    range_start=0,
                    range_end=1024,
                )
            )
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path=resolved_path,
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash="hash2",
                    last_modified_time=current_mtime_str,
                    range_start=1024,
                    range_end=2048,
                )
            )

            can_skip, mtime = _check_hash_cache_for_chunked_skip(
                entry, HashAlgorithm.XXH128, hash_cache, TEST_CHUNK_SIZE
            )

        assert can_skip is True
        assert mtime == current_mtime_ns // 1000


class TestUpdateHashCacheForChunkedFile:
    """Tests for _update_hash_cache_for_chunked_file function."""

    def test_does_nothing_when_no_chunkhashes(self, tmp_path):
        """Should do nothing when entry has no chunkhashes."""
        local_file = tmp_path / "file.txt"
        local_file.write_bytes(b"test")

        entry = ManifestFilePath(
            path=str(local_file),
            hash="wholehash",
            size=4,
            mtime=1000000,
            chunkhashes=None,
        )

        with HashCache(cache_dir=str(tmp_path / "hash_cache")) as hash_cache:
            _update_hash_cache_for_chunked_file(
                entry, local_file, HashAlgorithm.XXH128, hash_cache, TEST_CHUNK_SIZE, 123456789
            )

            # Verify no entries were added (try to get any entry for this file)
            result = hash_cache.get_entry(
                file_path_key=str(local_file.resolve()),
                hash_algorithm=HashAlgorithm.XXH128,
                range_start=0,
                range_end=4,
            )
            assert result is None

    def test_stores_all_chunk_hashes(self, tmp_path):
        """Should store all chunk hashes with correct byte ranges."""
        local_file = tmp_path / "file.txt"
        local_file.write_bytes(b"x" * 2048)
        resolved_path = str(local_file.resolve())

        entry = ManifestFilePath(
            path=str(local_file),
            hash=None,
            size=2048,
            mtime=1000000,
            chunkhashes=["hash1", "hash2"],
        )

        mtime_ns = 123456789000

        with HashCache(cache_dir=str(tmp_path / "hash_cache")) as hash_cache:
            _update_hash_cache_for_chunked_file(
                entry, local_file, HashAlgorithm.XXH128, hash_cache, TEST_CHUNK_SIZE, mtime_ns
            )

            # Check first chunk entry
            entry1 = hash_cache.get_entry(
                file_path_key=resolved_path,
                hash_algorithm=HashAlgorithm.XXH128,
                range_start=0,
                range_end=1024,
            )
            assert entry1 is not None
            assert entry1.file_path == resolved_path
            assert entry1.hash_algorithm == HashAlgorithm.XXH128
            assert entry1.file_hash == "hash1"
            assert entry1.last_modified_time == str(mtime_ns)
            assert entry1.range_start == 0
            assert entry1.range_end == 1024

            # Check second chunk entry
            entry2 = hash_cache.get_entry(
                file_path_key=resolved_path,
                hash_algorithm=HashAlgorithm.XXH128,
                range_start=1024,
                range_end=2048,
            )
            assert entry2 is not None
            assert entry2.file_hash == "hash2"
            assert entry2.range_start == 1024
            assert entry2.range_end == 2048

    def test_handles_last_chunk_smaller(self, tmp_path):
        """Should handle last chunk being smaller than chunk_size."""
        local_file = tmp_path / "file.txt"
        # 1.5 chunks worth of data
        local_file.write_bytes(b"x" * 1536)
        resolved_path = str(local_file.resolve())

        entry = ManifestFilePath(
            path=str(local_file),
            hash=None,
            size=1536,
            mtime=1000000,
            chunkhashes=["hash1", "hash2"],
        )

        mtime_ns = 123456789000

        with HashCache(cache_dir=str(tmp_path / "hash_cache")) as hash_cache:
            _update_hash_cache_for_chunked_file(
                entry, local_file, HashAlgorithm.XXH128, hash_cache, TEST_CHUNK_SIZE, mtime_ns
            )

            # Check second (last) chunk has correct range_end
            entry2 = hash_cache.get_entry(
                file_path_key=resolved_path,
                hash_algorithm=HashAlgorithm.XXH128,
                range_start=1024,
                range_end=1536,  # File size, not chunk_size
            )
            assert entry2 is not None
            assert entry2.range_start == 1024
            assert entry2.range_end == 1536  # File size, not chunk_size


class TestDownloadManifestChunkedHashCacheIntegration:
    """Integration tests for chunked file hash cache with download_manifest."""

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
        # FileSystemDataCache.get_object_key returns "{root_path}/{hash}.{algorithm}"
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
            result = download_manifest(
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
            result = download_manifest(
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
            result = download_manifest(
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
            result1 = download_manifest(
                manifest,
                data_cache,
                hash_cache=hash_cache,
                file_conflict_resolution=FileConflictResolution.OVERWRITE,
            )
            assert result1.statistics.processed_files == 1
            assert result1.statistics.skipped_files == 0

            # Second download should skip
            result2 = download_manifest(
                manifest,
                data_cache,
                hash_cache=hash_cache,
                file_conflict_resolution=FileConflictResolution.OVERWRITE,
            )
            assert result2.statistics.processed_files == 0
            assert result2.statistics.skipped_files == 1
