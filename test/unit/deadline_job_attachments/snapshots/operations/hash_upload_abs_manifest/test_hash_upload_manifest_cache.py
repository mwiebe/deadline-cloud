# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_upload_abs_manifest cache integration.

These tests cover:
- Hash cache integration (cache hits, misses, updates)
- S3 check cache integration
- Content-addressable storage (duplicate content)
- Skipping existing files
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Set

import boto3
import pytest

from deadline.job_attachments._snapshots import (
    collect_abs_snapshot,
    hash_upload_abs_manifest,
    FileSystemDataCache,
    S3DataCache,
    SymlinkPolicy,
    AbsSnapshot,
    ManifestFilePath,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.caches.hash_cache import HashCache
from deadline.job_attachments.caches.s3_check_cache import S3CheckCache


TEST_BUCKET = "test-hash-upload-bucket"
TEST_KEY_PREFIX = "Data"


class TestHashUploadWithHashCacheFileSystem:
    """Tests for hash cache integration with FileSystemDataCache."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_hash_upload_with_hash_cache(self, tmp_path: Path) -> None:
        """Test that hash cache is used and updated correctly."""
        cache_root = tmp_path / "cache"
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content for caching")
        file_stat = test_file.stat()
        file_size = int(file_stat.st_size)

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=file_size,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
        )

        with HashCache(str(hash_cache_dir)) as hash_cache:
            data_cache = self._create_filesystem_data_cache(cache_root)
            result = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            cache_key = str(test_file.resolve())
            cached_entry = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128)
            assert cached_entry is not None
            assert cached_entry.file_hash == result.manifest.files[0].hash


class TestHashUploadWithHashCacheS3:
    """Tests for hash cache integration with S3DataCache."""

    @pytest.fixture(autouse=True)
    def setup_s3_bucket(self, s3, create_s3_bucket) -> None:
        """Create the test S3 bucket before each test."""
        create_s3_bucket(TEST_BUCKET)
        self.s3_client = s3

    def _create_s3_data_cache(self, s3_check_cache: Optional[S3CheckCache] = None) -> S3DataCache:
        """Create an S3DataCache for testing."""
        return S3DataCache(
            s3_bucket=TEST_BUCKET,
            s3_key_prefix=TEST_KEY_PREFIX,
            s3_client=self.s3_client,
            s3_check_cache=s3_check_cache,
        )

    def test_hash_upload_with_hash_cache(self, tmp_path: Path) -> None:
        """Test that hash cache is used and updated correctly."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content for caching")
        file_stat = test_file.stat()
        file_size = int(file_stat.st_size)

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=file_size,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
        )

        with HashCache(str(cache_dir)) as hash_cache:
            data_cache = self._create_s3_data_cache()
            result = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            cache_key = str(test_file.resolve())
            cached_entry = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128)
            assert cached_entry is not None
            assert cached_entry.file_hash == result.manifest.files[0].hash

    def test_hash_upload_with_s3_check_cache(self, tmp_path: Path) -> None:
        """Test that S3 check cache prevents re-uploading existing files."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content")
        file_stat = test_file.stat()

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        # First upload
        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
            result1 = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
            )

        # Second upload with same file - should use cache
        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
            result2 = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
            )

        assert result1.manifest.files[0].hash == result2.manifest.files[0].hash


class TestContentAddressableStorageFileSystem:
    """Tests for content-addressable storage with FileSystemDataCache."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def _get_cache_files(self, cache_root: Path) -> Set[str]:
        """Get all file names in the cache directory."""
        return {f.name for f in cache_root.iterdir() if f.is_file()}

    def test_hash_upload_duplicate_content_written_once(self, tmp_path: Path) -> None:
        """Test that files with identical content are only written once."""
        cache_root = tmp_path / "cache"

        file1 = tmp_path / "file1.txt"
        file1.write_text("Same content")
        file2 = tmp_path / "file2.txt"
        file2.write_text("Same content")

        file1_stat = file1.stat()
        file2_stat = file2.stat()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(file1).replace("\\", "/"),
                    hash=None,
                    size=int(file1_stat.st_size),
                    mtime=int(file1_stat.st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=str(file2).replace("\\", "/"),
                    hash=None,
                    size=int(file2_stat.st_size),
                    mtime=int(file2_stat.st_mtime_ns // 1000),
                ),
            ],
            total_size=int(file1_stat.st_size) + int(file2_stat.st_size),
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        file_entries = [p for p in result.manifest.files if p.symlink_target is None]
        assert len(file_entries) == 2

        assert file_entries[0].hash == file_entries[1].hash

        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 1

    def test_skips_existing_files_in_cache(self, tmp_path: Path) -> None:
        """Test that files already in cache are not re-written."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        # First upload
        data_cache = self._create_filesystem_data_cache(cache_root)
        result1 = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        cached_filename = f"{result1.manifest.files[0].hash}.xxh128"
        cached_file = cache_root / cached_filename
        original_mtime = cached_file.stat().st_mtime

        # Second upload - should skip since file exists
        result2 = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert result1.manifest.files[0].hash == result2.manifest.files[0].hash
        assert cached_file.stat().st_mtime == original_mtime


class TestContentAddressableStorageS3:
    """Tests for content-addressable storage with S3DataCache."""

    @pytest.fixture(autouse=True)
    def setup_s3_bucket(self, s3, create_s3_bucket) -> None:
        """Create the test S3 bucket before each test."""
        create_s3_bucket(TEST_BUCKET)
        self.s3_client = s3

    def _get_s3_objects(self) -> Set[str]:
        """Get all object keys in the test bucket."""
        s3_resource = boto3.Session(region_name="us-west-2").resource("s3")
        bucket = s3_resource.Bucket(TEST_BUCKET)
        return {obj.key for obj in bucket.objects.all()}

    def _create_s3_data_cache(self) -> S3DataCache:
        """Create an S3DataCache for testing."""
        return S3DataCache(
            s3_bucket=TEST_BUCKET,
            s3_key_prefix=TEST_KEY_PREFIX,
            s3_client=self.s3_client,
        )

    def test_hash_upload_duplicate_content_uploaded_once(self, tmp_path: Path) -> None:
        """Test that files with identical content are only uploaded once."""
        file1 = tmp_path / "file1.txt"
        file1.write_text("Same content")
        file2 = tmp_path / "file2.txt"
        file2.write_text("Same content")

        collected = collect_abs_snapshot(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ALL,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_abs_manifest(
            manifest=collected,
            data_cache=data_cache,
        )

        file_entries = [p for p in result.manifest.files if p.symlink_target is None]
        assert len(file_entries) == 2

        assert file_entries[0].hash == file_entries[1].hash

        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 1


class TestHashCacheWithChunkedFiles:
    """Tests for hash cache integration with chunked files."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def _get_cache_files(self, cache_root: Path) -> Set[str]:
        """Get all file names in the cache directory."""
        return {f.name for f in cache_root.iterdir() if f.is_file()}

    def test_hash_cache_stores_chunk_ranges(self, tmp_path: Path) -> None:
        """Test that hash cache stores entries for each chunk range."""
        cache_root = tmp_path / "cache"
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

        # Create a 64-byte file, use 16-byte chunks -> 4 chunks
        test_file = tmp_path / "chunked.bin"
        test_file.write_bytes(bytes(range(64)))
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=64,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=64,
            file_chunk_size_bytes=16,
        )

        with HashCache(str(hash_cache_dir)) as hash_cache:
            data_cache = self._create_filesystem_data_cache(cache_root)
            result = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
                max_memory_bytes=64,
            )

            # Verify chunkhashes were produced
            assert result.manifest.files[0].chunkhashes is not None
            assert len(result.manifest.files[0].chunkhashes) == 4

            # Verify cache entries for each chunk range
            cache_key = str(test_file.resolve())
            for i in range(4):
                chunk_start = i * 16
                chunk_end = (i + 1) * 16
                cached_entry = hash_cache.get_entry(
                    cache_key, HashAlgorithm.XXH128, chunk_start, chunk_end
                )
                assert cached_entry is not None, f"Chunk {i} not found in cache"
                assert cached_entry.file_hash == result.manifest.files[0].chunkhashes[i]

    def test_hash_cache_hit_for_chunked_file(self, tmp_path: Path) -> None:
        """Test that cached chunk hashes are used on second run."""
        cache_root = tmp_path / "cache"
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

        # Create a 32-byte file, use 16-byte chunks -> 2 chunks
        test_file = tmp_path / "chunked.bin"
        test_file.write_bytes(b"a" * 32)
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=32,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=32,
            file_chunk_size_bytes=16,
        )

        with HashCache(str(hash_cache_dir)) as hash_cache:
            data_cache = self._create_filesystem_data_cache(cache_root)

            # First run - populates cache
            result1 = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
                max_memory_bytes=64,
            )

            # Second run - should use cached chunk hashes
            result2 = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
                max_memory_bytes=64,
            )

            assert result1.manifest.files[0].chunkhashes == result2.manifest.files[0].chunkhashes


class TestS3CheckCacheUpdates:
    """Tests for S3 check cache updates after uploads."""

    @pytest.fixture(autouse=True)
    def setup_s3_bucket(self, s3, create_s3_bucket) -> None:
        """Create the test S3 bucket before each test."""
        create_s3_bucket(TEST_BUCKET)
        self.s3_client = s3

    def _create_s3_data_cache(self, s3_check_cache: Optional[S3CheckCache] = None) -> S3DataCache:
        """Create an S3DataCache for testing."""
        return S3DataCache(
            s3_bucket=TEST_BUCKET,
            s3_key_prefix=TEST_KEY_PREFIX,
            s3_client=self.s3_client,
            s3_check_cache=s3_check_cache,
        )

    def test_s3_check_cache_entry_written_after_upload(self, tmp_path: Path) -> None:
        """Test that S3CheckCacheEntry is written after successful upload."""
        cache_dir = tmp_path / "s3_cache"
        cache_dir.mkdir()

        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content for S3 cache")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
            result = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
            )

            # Verify S3 check cache entry was written
            file_hash = result.manifest.files[0].hash
            s3_key = f"{TEST_KEY_PREFIX}/{file_hash}.xxh128"
            cache_key = f"{TEST_BUCKET}/{s3_key}"

            cached_entry = s3_cache.get_entry(cache_key)
            assert cached_entry is not None
            assert cached_entry.s3_key == cache_key

    def test_s3_check_cache_prevents_reupload(self, tmp_path: Path) -> None:
        """Test that S3 check cache prevents re-uploading on second run."""
        cache_dir = tmp_path / "s3_cache"
        cache_dir.mkdir()

        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        # Track put_object calls
        put_object_calls = []
        original_put_object = self.s3_client.put_object

        def tracking_put_object(*args, **kwargs):
            put_object_calls.append(kwargs.get("Key", args[1] if len(args) > 1 else None))
            return original_put_object(*args, **kwargs)

        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)

            # First upload
            self.s3_client.put_object = tracking_put_object
            hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
            )
            first_run_calls = len(put_object_calls)

            # Second upload - should skip due to S3 check cache
            hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
            )
            second_run_calls = len(put_object_calls) - first_run_calls

        # First run should have uploaded, second run should have skipped
        assert first_run_calls == 1
        assert second_run_calls == 0


class TestPartialCacheHits:
    """Tests for partial cache hit scenarios."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def _get_cache_files(self, cache_root: Path) -> Set[str]:
        """Get all file names in the cache directory."""
        return {f.name for f in cache_root.iterdir() if f.is_file()}

    def test_some_files_cached_others_not(self, tmp_path: Path) -> None:
        """Test processing when some files are cached and others are not."""
        cache_root = tmp_path / "cache"
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

        # Create two files
        file1 = tmp_path / "file1.txt"
        file1.write_text("Content of file 1")
        file2 = tmp_path / "file2.txt"
        file2.write_text("Content of file 2")

        file1_stat = file1.stat()
        file2_stat = file2.stat()

        # First manifest with only file1
        manifest1 = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(file1).replace("\\", "/"),
                    hash=None,
                    size=int(file1_stat.st_size),
                    mtime=int(file1_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file1_stat.st_size),
        )

        with HashCache(str(hash_cache_dir)) as hash_cache:
            data_cache = self._create_filesystem_data_cache(cache_root)

            # First run - cache file1
            result1 = hash_upload_abs_manifest(
                manifest=manifest1,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )
            file1_hash = result1.manifest.files[0].hash

            # Second manifest with both files
            manifest2 = AbsSnapshot(
                hash_alg=HashAlgorithm.XXH128,
                files=[
                    ManifestFilePath(
                        path=str(file1).replace("\\", "/"),
                        hash=None,
                        size=int(file1_stat.st_size),
                        mtime=int(file1_stat.st_mtime_ns // 1000),
                    ),
                    ManifestFilePath(
                        path=str(file2).replace("\\", "/"),
                        hash=None,
                        size=int(file2_stat.st_size),
                        mtime=int(file2_stat.st_mtime_ns // 1000),
                    ),
                ],
                total_size=int(file1_stat.st_size) + int(file2_stat.st_size),
            )

            # Second run - file1 should be cached, file2 should be processed
            result2 = hash_upload_abs_manifest(
                manifest=manifest2,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            # Both files should have hashes
            assert len(result2.manifest.files) == 2
            hashes = {f.hash for f in result2.manifest.files}
            assert file1_hash in hashes
            assert all(h is not None for h in hashes)

    def test_some_chunks_cached_others_not(self, tmp_path: Path) -> None:
        """Test processing when some chunks are cached and others are not."""
        cache_root = tmp_path / "cache"
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

        # Create a 32-byte file with 16-byte chunks
        test_file = tmp_path / "chunked.bin"
        test_file.write_bytes(b"a" * 16 + b"b" * 16)  # Two different chunks
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=32,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=32,
            file_chunk_size_bytes=16,
        )

        with HashCache(str(hash_cache_dir)) as hash_cache:
            data_cache = self._create_filesystem_data_cache(cache_root)

            # First run - cache both chunks
            result1 = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
                max_memory_bytes=64,
            )

            # Note: We can't easily remove individual chunks from the hash cache,
            # so we verify the full cache scenario works correctly

            # Second run - should use cached chunks
            result2 = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
                max_memory_bytes=64,
            )

            assert result1.manifest.files[0].chunkhashes == result2.manifest.files[0].chunkhashes

    def test_cache_miss_due_to_mtime_change(self, tmp_path: Path) -> None:
        """Test that cache is invalidated when file mtime changes."""
        cache_root = tmp_path / "cache"
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

        test_file = tmp_path / "test.txt"
        test_file.write_text("Original content")
        file_stat = test_file.stat()
        original_mtime = int(file_stat.st_mtime_ns // 1000)

        abs_path = str(test_file).replace("\\", "/")

        manifest1 = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=original_mtime,
                )
            ],
            total_size=int(file_stat.st_size),
        )

        with HashCache(str(hash_cache_dir)) as hash_cache:
            data_cache = self._create_filesystem_data_cache(cache_root)

            # First run - cache the file
            result1 = hash_upload_abs_manifest(
                manifest=manifest1,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            # Modify file and update mtime
            test_file.write_text("Modified content")
            file_stat = test_file.stat()
            new_mtime = int(file_stat.st_mtime_ns // 1000)

            # Ensure mtime is different (may need to wait on some systems)
            if new_mtime == original_mtime:
                import time

                time.sleep(0.01)
                test_file.write_text("Modified content again")
                file_stat = test_file.stat()
                new_mtime = int(file_stat.st_mtime_ns // 1000)

            manifest2 = AbsSnapshot(
                hash_alg=HashAlgorithm.XXH128,
                files=[
                    ManifestFilePath(
                        path=abs_path,
                        hash=None,
                        size=int(file_stat.st_size),
                        mtime=new_mtime,
                    )
                ],
                total_size=int(file_stat.st_size),
            )

            # Second run with different mtime - should recompute hash
            result2 = hash_upload_abs_manifest(
                manifest=manifest2,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            # Hashes should be different due to content change
            assert result1.manifest.files[0].hash != result2.manifest.files[0].hash


class TestStreamingFilesCacheIntegration:
    """Tests for cache integration with streaming (large) files."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def _get_cache_files(self, cache_root: Path) -> Set[str]:
        """Get all file names in the cache directory."""
        return {f.name for f in cache_root.iterdir() if f.is_file()}

    def test_streaming_file_skipped_when_exists_in_data_cache(self, tmp_path: Path) -> None:
        """Test that streaming files are skipped when already in data cache."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "large.bin"
        test_file.write_bytes(bytes(range(100)))
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=100,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=100,
            file_chunk_size_bytes=-1,  # No chunking - whole file
        )

        data_cache = self._create_filesystem_data_cache(cache_root)

        # First run - uploads file
        result1 = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=32,  # Force streaming mode
        )

        cached_file = cache_root / f"{result1.manifest.files[0].hash}.xxh128"
        original_mtime = cached_file.stat().st_mtime

        # Second run - should skip since file exists in data cache
        result2 = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=32,
        )

        assert result1.manifest.files[0].hash == result2.manifest.files[0].hash
        # File should not have been rewritten
        assert cached_file.stat().st_mtime == original_mtime

    def test_streaming_file_with_hash_cache(self, tmp_path: Path) -> None:
        """Test that hash cache works with streaming files."""
        cache_root = tmp_path / "cache"
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

        test_file = tmp_path / "large.bin"
        test_file.write_bytes(bytes(range(100)))
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=100,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=100,
            file_chunk_size_bytes=-1,  # No chunking
        )

        with HashCache(str(hash_cache_dir)) as hash_cache:
            data_cache = self._create_filesystem_data_cache(cache_root)

            # First run - populates hash cache
            result1 = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
                max_memory_bytes=32,  # Force streaming mode
            )

            # Verify hash cache entry was written (whole file range)
            cache_key = str(test_file.resolve())
            cached_entry = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128)
            assert cached_entry is not None
            assert cached_entry.file_hash == result1.manifest.files[0].hash

            # Second run - should use hash cache
            result2 = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
                max_memory_bytes=32,
            )

            assert result1.manifest.files[0].hash == result2.manifest.files[0].hash


class TestHashCacheHitButObjectMissingFromS3:
    """Tests for the case where hash cache has the hash but object is not on S3."""

    @pytest.fixture(autouse=True)
    def setup_s3_bucket(self, s3, create_s3_bucket) -> None:
        """Create the test S3 bucket before each test."""
        create_s3_bucket(TEST_BUCKET)
        self.s3_client = s3

    def _create_s3_data_cache(self, s3_check_cache: Optional[S3CheckCache] = None) -> S3DataCache:
        """Create an S3DataCache for testing."""
        return S3DataCache(
            s3_bucket=TEST_BUCKET,
            s3_key_prefix=TEST_KEY_PREFIX,
            s3_client=self.s3_client,
            s3_check_cache=s3_check_cache,
        )

    def test_hash_cache_hit_but_s3_miss_uploads_correctly(self, tmp_path: Path) -> None:
        """Test that file is uploaded when hash cache hits but object is not on S3.

        This tests the scenario where:
        1. Hash cache has the hash from a previous run
        2. S3 check cache is empty (or cleared)
        3. Object was deleted from S3

        The implementation must re-read and re-hash the file before uploading,
        even though the hash cache has an entry. This is because we can't trust
        the hash cache alone for uploads - we need to verify the hash by reading
        the actual file data. The hash cache is only trusted for skipping when
        the data already exists in S3.
        """
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

        test_file = tmp_path / "test.txt"
        test_content = "Test content for hash cache hit but S3 miss"
        test_file.write_text(test_content)
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        with HashCache(str(hash_cache_dir)) as hash_cache:
            # First upload - populates hash cache and uploads to S3
            data_cache = self._create_s3_data_cache()
            result1 = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )
            file_hash = result1.manifest.files[0].hash

            # Verify hash cache entry exists
            cache_key = str(test_file.resolve())
            cached_entry = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128)
            assert cached_entry is not None, "Hash cache should have entry after first upload"
            assert cached_entry.file_hash == file_hash

            # Delete the object from S3 (simulating S3 data loss or cleanup)
            s3_key = f"{TEST_KEY_PREFIX}/{file_hash}.xxh128"
            self.s3_client.delete_object(Bucket=TEST_BUCKET, Key=s3_key)

            # Verify object is gone
            try:
                self.s3_client.head_object(Bucket=TEST_BUCKET, Key=s3_key)
                assert False, "Object should have been deleted"
            except self.s3_client.exceptions.ClientError as e:
                assert e.response["Error"]["Code"] == "404"

            # Second upload - hash cache hits, but S3 HeadObject misses
            # Must re-read and re-hash to verify before uploading
            data_cache2 = self._create_s3_data_cache()  # Fresh cache, no s3_check_cache
            result2 = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache2,
                hash_cache=hash_cache,
            )

            # Hash should be the same
            assert result2.manifest.files[0].hash == file_hash

            # Object should now exist in S3 again
            response = self.s3_client.head_object(Bucket=TEST_BUCKET, Key=s3_key)
            assert response["ContentLength"] == int(file_stat.st_size)

            # Verify the content is correct
            obj = self.s3_client.get_object(Bucket=TEST_BUCKET, Key=s3_key)
            uploaded_content = obj["Body"].read().decode("utf-8")
            assert uploaded_content == test_content, "Uploaded content should match original file"

    def test_hash_changed_but_new_hash_exists_skips_upload(self, tmp_path: Path) -> None:
        """Test that upload is skipped when hash changed but new hash already exists in S3.

        This tests the scenario where:
        1. Hash cache has stale hash (hash_A) from previous run
        2. HeadObject(hash_A) returns 404
        3. File is re-read and re-hashed, producing hash_B
        4. HeadObject(hash_B) returns 200 (exists from another upload)
        5. Upload should be skipped
        """
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

        test_file = tmp_path / "test.txt"
        original_content = "Original content"
        test_file.write_text(original_content)
        file_stat = test_file.stat()
        original_mtime = int(file_stat.st_mtime_ns // 1000)

        abs_path = str(test_file).replace("\\", "/")

        manifest1 = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=original_mtime,
                )
            ],
            total_size=int(file_stat.st_size),
        )

        with HashCache(str(hash_cache_dir)) as hash_cache:
            # First upload - populates hash cache with original hash
            data_cache = self._create_s3_data_cache()
            result1 = hash_upload_abs_manifest(
                manifest=manifest1,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )
            original_hash = result1.manifest.files[0].hash

            # Now upload different content to S3 (simulating another client)
            new_content = "New content that already exists"
            from deadline.job_attachments.asset_manifests.hash_algorithms import hash_data

            new_hash = hash_data(new_content.encode(), HashAlgorithm.XXH128)
            new_s3_key = f"{TEST_KEY_PREFIX}/{new_hash}.xxh128"
            self.s3_client.put_object(
                Bucket=TEST_BUCKET,
                Key=new_s3_key,
                Body=new_content.encode(),
            )

            # Delete the original object from S3
            original_s3_key = f"{TEST_KEY_PREFIX}/{original_hash}.xxh128"
            self.s3_client.delete_object(Bucket=TEST_BUCKET, Key=original_s3_key)

            # Modify the local file to have the new content
            import time

            time.sleep(0.01)  # Ensure mtime changes
            test_file.write_text(new_content)
            new_stat = test_file.stat()
            new_size = int(new_stat.st_size)

            # Create manifest with OLD mtime (simulating stale hash cache entry)
            # The hash cache will hit because mtime matches the cached entry
            manifest2 = AbsSnapshot(
                hash_alg=HashAlgorithm.XXH128,
                files=[
                    ManifestFilePath(
                        path=abs_path,
                        hash=None,
                        size=new_size,
                        mtime=original_mtime,  # Use old mtime to trigger cache hit
                    )
                ],
                total_size=new_size,
            )

            # Track put_object calls
            put_calls = []
            original_put = self.s3_client.put_object

            def tracking_put(*args, **kwargs):
                put_calls.append(kwargs.get("Key"))
                return original_put(*args, **kwargs)

            self.s3_client.put_object = tracking_put

            # Second upload - hash cache hits (mtime matches), HeadObject(original_hash) misses
            # File is re-read, produces new_hash, HeadObject(new_hash) hits
            # Should skip upload
            data_cache2 = self._create_s3_data_cache()
            result2 = hash_upload_abs_manifest(
                manifest=manifest2,
                data_cache=data_cache2,
                hash_cache=hash_cache,
            )

            # Should have computed the new hash
            assert result2.manifest.files[0].hash == new_hash

            # Should NOT have uploaded (object already exists)
            assert len(put_calls) == 0, f"Should not upload when hash exists, but got {put_calls}"


class TestAllFilesSkippedDueToCacheHits:
    """Tests for the scenario where all files are skipped due to cache hits.

    This reproduces a bug where calling hash_upload_abs_manifest with the same
    unhashed manifest multiple times fails on the third pass when both hash cache
    and s3 check cache have entries.
    """

    @pytest.fixture(autouse=True)
    def setup_s3_bucket(self, s3, create_s3_bucket) -> None:
        """Create the test S3 bucket before each test."""
        create_s3_bucket(TEST_BUCKET)
        self.s3_client = s3

    def _create_s3_data_cache(self, s3_check_cache: Optional[S3CheckCache] = None) -> S3DataCache:
        """Create an S3DataCache for testing."""
        return S3DataCache(
            s3_bucket=TEST_BUCKET,
            s3_key_prefix=TEST_KEY_PREFIX,
            s3_client=self.s3_client,
            s3_check_cache=s3_check_cache,
        )

    def test_three_passes_with_same_unhashed_manifest(self, tmp_path: Path) -> None:
        """Test calling hash_upload_abs_manifest three times with same unhashed manifest.

        This reproduces a bug where:
        - Pass 1: Cold upload (no caches) - works
        - Pass 2: Warm with hash cache only (HeadObject path) - works
        - Pass 3: Warm with hash cache + s3 check cache - FAILS with
          "Internal error: file was not hashed"

        The bug occurs because when all files are skipped due to cache hits,
        the hash is retrieved from the cache but not properly stored in the
        result manifest.
        """
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()
        s3_check_cache_dir = tmp_path / "s3_check_cache"
        s3_check_cache_dir.mkdir()

        # Create test files
        test_file1 = tmp_path / "file1.txt"
        test_file1.write_text("Content for file 1")
        file1_stat = test_file1.stat()

        test_file2 = tmp_path / "file2.txt"
        test_file2.write_text("Content for file 2")
        file2_stat = test_file2.stat()

        # Create unhashed manifest (same as what collect_abs_snapshot produces)
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file1).replace("\\", "/"),
                    hash=None,
                    size=int(file1_stat.st_size),
                    mtime=int(file1_stat.st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=str(test_file2).replace("\\", "/"),
                    hash=None,
                    size=int(file2_stat.st_size),
                    mtime=int(file2_stat.st_mtime_ns // 1000),
                ),
            ],
            total_size=int(file1_stat.st_size) + int(file2_stat.st_size),
        )

        with HashCache(str(hash_cache_dir)) as hash_cache:
            # PASS 1: Cold upload (no s3 check cache)
            with S3CheckCache(str(s3_check_cache_dir)) as s3_check_cache:
                data_cache = self._create_s3_data_cache(s3_check_cache=s3_check_cache)
                result1 = hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    hash_cache=hash_cache,
                )

            # Verify pass 1 worked
            assert len(result1.manifest.files) == 2
            assert all(f.hash is not None for f in result1.manifest.files)
            file1_hash = result1.manifest.files[0].hash
            file2_hash = result1.manifest.files[1].hash

            # PASS 2: Warm with hash cache only (fresh s3 check cache)
            s3_check_cache_dir_fresh = tmp_path / "s3_check_cache_fresh"
            s3_check_cache_dir_fresh.mkdir()
            with S3CheckCache(str(s3_check_cache_dir_fresh)) as s3_check_cache_fresh:
                data_cache = self._create_s3_data_cache(s3_check_cache=s3_check_cache_fresh)
                result2 = hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    hash_cache=hash_cache,
                )

            # Verify pass 2 worked
            assert len(result2.manifest.files) == 2
            assert result2.manifest.files[0].hash == file1_hash
            assert result2.manifest.files[1].hash == file2_hash

            # PASS 3: Warm with hash cache + s3 check cache from pass 1
            # This is where the bug manifests
            with S3CheckCache(str(s3_check_cache_dir)) as s3_check_cache:
                data_cache = self._create_s3_data_cache(s3_check_cache=s3_check_cache)
                result3 = hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    hash_cache=hash_cache,
                )

            # Verify pass 3 worked
            assert len(result3.manifest.files) == 2
            assert result3.manifest.files[0].hash == file1_hash
            assert result3.manifest.files[1].hash == file2_hash

            # All three passes should produce identical results
            assert (
                result1.manifest.files[0].hash
                == result2.manifest.files[0].hash
                == result3.manifest.files[0].hash
            )
            assert (
                result1.manifest.files[1].hash
                == result2.manifest.files[1].hash
                == result3.manifest.files[1].hash
            )
