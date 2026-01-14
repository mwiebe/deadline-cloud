# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for concurrent upload deduplication in hash_upload_abs_manifest.

These tests verify that when multiple chunks or files have identical content (same hash),
only one upload occurs and subsequent uploads are skipped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Set

import boto3
import pytest

from deadline.job_attachments._snapshots import (
    hash_upload_abs_manifest,
    FileSystemDataCache,
    S3DataCache,
    AbsSnapshot,
    ManifestFilePath,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm


TEST_BUCKET = "test-dedup-bucket"
TEST_KEY_PREFIX = "Data"


class TestConcurrentUploadDeduplicationFileSystem:
    """Tests for concurrent upload deduplication with FileSystemDataCache."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def _get_cache_files(self, cache_root: Path) -> Set[str]:
        """Get all file names in the cache directory."""
        return {f.name for f in cache_root.iterdir() if f.is_file()}

    def test_identical_chunks_uploaded_once(self, tmp_path: Path) -> None:
        """File with identical chunks results in single cache entry."""
        cache_root = tmp_path / "cache"

        # Create 128KB file with 4 identical 32KB chunks for better concurrency testing
        chunk_size = 32 * 1024  # 32KB
        file_size = 4 * chunk_size  # 128KB
        test_file = tmp_path / "repeated.bin"
        test_file.write_bytes(b"x" * file_size)
        file_stat = test_file.stat()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=file_size,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=chunk_size,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=file_size,
            max_workers=4,  # Multiple workers to trigger concurrent uploads
        )

        # All 4 chunks have the same hash
        assert result.manifest.files[0].chunkhashes is not None
        assert len(result.manifest.files[0].chunkhashes) == 4
        assert len(set(result.manifest.files[0].chunkhashes)) == 1

        # Only 1 file in cache (deduplication worked)
        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 1

    def test_multiple_files_same_content_uploaded_once(self, tmp_path: Path) -> None:
        """Multiple files with identical content result in single cache entry."""
        cache_root = tmp_path / "cache"

        # Create 3 files with identical content
        content = b"identical content here"
        files = []
        for i in range(3):
            f = tmp_path / f"file{i}.txt"
            f.write_bytes(content)
            files.append(f)

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(f).replace("\\", "/"),
                    hash=None,
                    size=len(content),
                    mtime=int(f.stat().st_mtime_ns // 1000),
                )
                for f in files
            ],
            total_size=len(content) * 3,
            file_chunk_size_bytes=-1,  # No chunking
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=1024,
            max_workers=4,
        )

        # All files have the same hash
        hashes = [f.hash for f in result.manifest.files]
        assert len(set(hashes)) == 1

        # Only 1 file in cache
        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 1

    def test_mixed_unique_and_duplicate_content(self, tmp_path: Path) -> None:
        """Mix of unique and duplicate files results in correct cache entries."""
        cache_root = tmp_path / "cache"

        # Create files: 2 identical + 1 unique (different lengths to ensure different hashes)
        dup_content = b"duplicate content here"
        unique_content = b"this is completely different unique content"

        dup1 = tmp_path / "dup1.txt"
        dup1.write_bytes(dup_content)
        dup2 = tmp_path / "dup2.txt"
        dup2.write_bytes(dup_content)
        different = tmp_path / "different.txt"
        different.write_bytes(unique_content)

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(dup1).replace("\\", "/"),
                    hash=None,
                    size=len(dup_content),
                    mtime=int(dup1.stat().st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=str(dup2).replace("\\", "/"),
                    hash=None,
                    size=len(dup_content),
                    mtime=int(dup2.stat().st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=str(different).replace("\\", "/"),
                    hash=None,
                    size=len(unique_content),
                    mtime=int(different.stat().st_mtime_ns // 1000),
                ),
            ],
            total_size=len(dup_content) * 2 + len(unique_content),
            file_chunk_size_bytes=-1,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=1024,
            max_workers=4,
        )

        # Verify hashes - use endswith for precise matching
        dup1_hash = next(f.hash for f in result.manifest.files if f.path.endswith("dup1.txt"))
        dup2_hash = next(f.hash for f in result.manifest.files if f.path.endswith("dup2.txt"))
        different_hash = next(f.hash for f in result.manifest.files if f.path.endswith("different.txt"))

        assert dup1_hash == dup2_hash
        assert dup1_hash != different_hash

        # 2 files in cache (1 for duplicates, 1 for different)
        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 2

    def test_statistics_count_skipped_bytes(self, tmp_path: Path) -> None:
        """Statistics correctly count skipped bytes due to deduplication."""
        cache_root = tmp_path / "cache"

        # Create file with 4 identical chunks
        test_file = tmp_path / "repeated.bin"
        test_file.write_bytes(b"y" * 64)
        file_stat = test_file.stat()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=64,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=64,
            file_chunk_size_bytes=16,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=64,
            max_workers=4,
        )

        # Total bytes should be 64 (processed + skipped)
        stats = result.statistics
        assert stats.processed_bytes + stats.skipped_bytes == 64
        # At least some bytes should be skipped (3 of 4 chunks are duplicates)
        assert stats.skipped_bytes >= 16 * 3  # At least 3 chunks skipped


class TestConcurrentUploadDeduplicationS3:
    """Tests for concurrent upload deduplication with S3DataCache."""

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

    def test_identical_chunks_uploaded_once(self, tmp_path: Path) -> None:
        """File with identical chunks results in single S3 object."""
        # Create 128KB file with 4 identical 32KB chunks for better concurrency testing
        chunk_size = 32 * 1024  # 32KB
        file_size = 4 * chunk_size  # 128KB
        test_file = tmp_path / "repeated.bin"
        test_file.write_bytes(b"x" * file_size)
        file_stat = test_file.stat()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=file_size,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=chunk_size,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=file_size,
            max_workers=4,
        )

        # All 4 chunks have the same hash
        assert result.manifest.files[0].chunkhashes is not None
        assert len(result.manifest.files[0].chunkhashes) == 4
        assert len(set(result.manifest.files[0].chunkhashes)) == 1

        # Only 1 object in S3
        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 1

    def test_multiple_files_same_content_uploaded_once(self, tmp_path: Path) -> None:
        """Multiple files with identical content result in single S3 object."""
        # Create 3 files with identical content
        content = b"identical content here"
        files = []
        for i in range(3):
            f = tmp_path / f"file{i}.txt"
            f.write_bytes(content)
            files.append(f)

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(f).replace("\\", "/"),
                    hash=None,
                    size=len(content),
                    mtime=int(f.stat().st_mtime_ns // 1000),
                )
                for f in files
            ],
            total_size=len(content) * 3,
            file_chunk_size_bytes=-1,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=1024,
            max_workers=4,
        )

        # All files have the same hash
        hashes = [f.hash for f in result.manifest.files]
        assert len(set(hashes)) == 1

        # Only 1 object in S3
        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 1

    def test_mixed_unique_and_duplicate_content(self, tmp_path: Path) -> None:
        """Mix of unique and duplicate files results in correct S3 objects."""
        # Create files: 2 identical + 1 unique (different lengths to ensure different hashes)
        dup_content = b"duplicate content here"
        unique_content = b"this is completely different unique content"

        dup1 = tmp_path / "dup1.txt"
        dup1.write_bytes(dup_content)
        dup2 = tmp_path / "dup2.txt"
        dup2.write_bytes(dup_content)
        different = tmp_path / "different.txt"
        different.write_bytes(unique_content)

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(dup1).replace("\\", "/"),
                    hash=None,
                    size=len(dup_content),
                    mtime=int(dup1.stat().st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=str(dup2).replace("\\", "/"),
                    hash=None,
                    size=len(dup_content),
                    mtime=int(dup2.stat().st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=str(different).replace("\\", "/"),
                    hash=None,
                    size=len(unique_content),
                    mtime=int(different.stat().st_mtime_ns // 1000),
                ),
            ],
            total_size=len(dup_content) * 2 + len(unique_content),
            file_chunk_size_bytes=-1,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=1024,
            max_workers=4,
        )

        # Verify hashes - use endswith for precise matching
        dup1_hash = next(f.hash for f in result.manifest.files if f.path.endswith("dup1.txt"))
        dup2_hash = next(f.hash for f in result.manifest.files if f.path.endswith("dup2.txt"))
        different_hash = next(f.hash for f in result.manifest.files if f.path.endswith("different.txt"))

        assert dup1_hash == dup2_hash
        assert dup1_hash != different_hash

        # 2 objects in S3
        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 2

    def test_chunked_file_with_some_duplicate_chunks(self, tmp_path: Path) -> None:
        """File with mix of unique and duplicate chunks."""
        # Create file: chunk0=A, chunk1=B, chunk2=A, chunk3=C (A appears twice)
        chunk_a = b"a" * 16
        chunk_b = b"b" * 16
        chunk_c = b"c" * 16
        content = chunk_a + chunk_b + chunk_a + chunk_c

        test_file = tmp_path / "mixed.bin"
        test_file.write_bytes(content)
        file_stat = test_file.stat()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=64,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=64,
            file_chunk_size_bytes=16,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=64,
            max_workers=4,
        )

        # 4 chunks, but only 3 unique hashes (A, B, C)
        chunkhashes = result.manifest.files[0].chunkhashes
        assert chunkhashes is not None
        assert len(chunkhashes) == 4
        assert len(set(chunkhashes)) == 3
        assert chunkhashes[0] == chunkhashes[2]  # chunk 0 and 2 are both 'A'

        # 3 objects in S3
        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 3
