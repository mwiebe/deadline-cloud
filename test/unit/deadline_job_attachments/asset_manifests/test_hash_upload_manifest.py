# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Unit tests for the HASH_UPLOAD manifest operation.

These tests use moto to mock S3 and perform end-to-end testing of the
hash_upload_manifest function, verifying that files are correctly hashed
and uploaded to S3.

All tests use the unified manifest classes (AbsSnapshotManifest, AbsDiffManifest)
from manifest.py, following the Wave 2 refactoring pattern.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional, Set

import boto3
import pytest

from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
    hash_upload_manifest,
    S3DataCache,
)
from deadline.job_attachments.asset_manifests._operations._hash_upload_manifest import (
    _ChunkWorkItem,
    _MemoryPool,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm, hash_file
from deadline.job_attachments.asset_manifests.versions import SymlinkPolicy
from deadline.job_attachments.asset_manifests.manifest import (
    AbsDiffManifest,
    AbsSnapshotManifest,
    ManifestFilePath,
)
from deadline.job_attachments.caches.hash_cache import HashCache
from deadline.job_attachments.caches.s3_check_cache import S3CheckCache


# Test constants
TEST_BUCKET = "test-hash-upload-bucket"
TEST_KEY_PREFIX = "Data"


class TestMemoryPool:
    """Tests for the _MemoryPool class."""

    def test_allocate_within_limit(self) -> None:
        """Test allocation within memory limit."""
        pool = _MemoryPool(max_bytes=1000)
        pool.allocate(500)
        assert pool.allocated == 500

    def test_release_memory(self) -> None:
        """Test releasing memory."""
        pool = _MemoryPool(max_bytes=1000)
        pool.allocate(500)
        pool.release(300)
        assert pool.allocated == 200

    def test_allocate_releases_on_release(self) -> None:
        """Test that allocation unblocks when memory is released."""
        pool = _MemoryPool(max_bytes=100)
        pool.allocate(80)

        # This should block initially
        allocation_complete = threading.Event()

        def allocate_more() -> None:
            pool.allocate(50)
            allocation_complete.set()

        thread = threading.Thread(target=allocate_more)
        thread.start()

        # Give the thread time to start and block
        time.sleep(0.1)
        assert not allocation_complete.is_set()

        # Release memory to unblock
        pool.release(50)
        thread.join(timeout=1.0)
        assert allocation_complete.is_set()


class TestChunkWorkItem:
    """Tests for the _ChunkWorkItem dataclass."""

    def test_create_work_item(self) -> None:
        """Test creating a chunk work item."""
        item = _ChunkWorkItem(
            file_path=Path("/test/file.txt"),
            cache_key="/test/file.txt",
            file_size=1000,
            mtime=12345,
            chunk_index=0,
            chunk_start=0,
            chunk_end=1000,
        )
        assert item.file_path == Path("/test/file.txt")
        assert item.cache_key == "/test/file.txt"
        assert item.file_size == 1000
        assert item.chunk_index == 0
        assert item.data is None
        assert item.chunk_hash is None


class TestHashUploadManifest:
    """Tests for hash_upload_manifest with unified manifest classes."""

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

    def _get_s3_object_content(self, key: str) -> bytes:
        """Get the content of an S3 object."""
        response = self.s3_client.get_object(Bucket=TEST_BUCKET, Key=key)
        return response["Body"].read()

    def _create_s3_data_cache(self, s3_check_cache: Optional[S3CheckCache] = None) -> S3DataCache:
        """Create an S3DataCache for testing."""
        return S3DataCache(
            s3_bucket=TEST_BUCKET,
            s3_key_prefix=TEST_KEY_PREFIX,
            s3_client=self.s3_client,
            s3_check_cache=s3_check_cache,
        )

    def test_hash_upload_empty_manifest(self) -> None:
        """Test hashing and uploading an empty manifest."""
        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            total_size=0,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsSnapshotManifest)
        assert len(result.files) == 0
        assert result.totalSize == 0
        # No objects should be uploaded
        assert len(self._get_s3_objects()) == 0

    def test_hash_upload_single_file(self, tmp_path: Path) -> None:
        """Test hashing and uploading a single file."""
        # Create a test file
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello, World!")
        file_stat = test_file.stat()

        # Use absolute path in manifest (as required by HASH_UPLOAD)
        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",  # Empty hash to be filled
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsSnapshotManifest)
        assert len(result.files) == 1
        assert result.files[0].hash != ""  # Hash should be filled in
        assert result.files[0].path == abs_path

        # Verify the file was uploaded to S3
        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 1

        # Verify the S3 key format is correct (hash.algorithm)
        expected_key = f"{TEST_KEY_PREFIX}/{result.files[0].hash}.xxh128"
        assert expected_key in s3_objects

        # Verify the uploaded content matches the original file
        uploaded_content = self._get_s3_object_content(expected_key)
        assert uploaded_content == test_file.read_bytes()

    def test_hash_upload_multiple_files(self, tmp_path: Path) -> None:
        """Test hashing and uploading multiple files."""
        # Create test files
        file1 = tmp_path / "file1.txt"
        file1.write_text("Content of file 1")
        file2 = tmp_path / "file2.txt"
        file2.write_text("Content of file 2")
        file3 = tmp_path / "subdir" / "file3.txt"
        file3.parent.mkdir()
        file3.write_text("Content of file 3")

        # Collect the manifest (returns AbsSnapshotManifest)
        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=collected,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsSnapshotManifest)
        # Filter to file entries only
        file_entries = [p for p in result.files if p.symlink_target is None and not p.deleted]
        assert len(file_entries) == 3

        # All files should have hashes
        for entry in file_entries:
            assert entry.hash is not None
            assert entry.hash != ""
            assert len(entry.hash) == 32  # XXH128 produces 32 hex chars

        # Verify all files were uploaded
        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 3

    def test_hash_upload_duplicate_content_uploaded_once(self, tmp_path: Path) -> None:
        """Test that files with identical content are only uploaded once."""
        # Create files with identical content
        file1 = tmp_path / "file1.txt"
        file1.write_text("Same content")
        file2 = tmp_path / "file2.txt"
        file2.write_text("Same content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=collected,
            data_cache=data_cache,
        )

        # Filter to file entries
        file_entries = [p for p in result.files if p.symlink_target is None]
        assert len(file_entries) == 2

        # Both files should have the same hash
        assert file_entries[0].hash == file_entries[1].hash

        # Only one object should be in S3 (content-addressable storage)
        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 1

    def test_hash_upload_with_hash_cache(self, tmp_path: Path) -> None:
        """Test that hash cache is used and updated correctly."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content for caching")
        file_stat = test_file.stat()
        file_size = int(file_stat.st_size)

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=file_size,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
        )

        with HashCache(str(cache_dir)) as hash_cache:
            data_cache = self._create_s3_data_cache()
            result = hash_upload_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            # Verify hash was computed and cached
            # The cache stores entries with range_start=0 and range_end=file_size
            cache_key = str(test_file.resolve())
            cached_entry = hash_cache.get_entry(
                cache_key, HashAlgorithm.XXH128, range_start=0, range_end=file_size
            )
            assert cached_entry is not None
            assert cached_entry.file_hash == result.files[0].hash

    def test_hash_upload_with_s3_check_cache(self, tmp_path: Path) -> None:
        """Test that S3 check cache prevents re-uploading existing files."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content")
        file_stat = test_file.stat()

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        # First upload
        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
            result1 = hash_upload_manifest(
                manifest=manifest,
                data_cache=data_cache,
            )

        # Second upload with same file - should use cache
        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
            result2 = hash_upload_manifest(
                manifest=manifest,
                data_cache=data_cache,
            )

        # Both results should have the same hash
        assert result1.files[0].hash == result2.files[0].hash

    def test_hash_matches_direct_hash(self, tmp_path: Path) -> None:
        """Test that computed hash matches direct hash_file() result."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content for hash verification")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        # Compute hash directly
        expected_hash = hash_file(str(test_file), HashAlgorithm.XXH128)
        assert result.files[0].hash == expected_hash

    def test_preserves_metadata(self, tmp_path: Path) -> None:
        """Test that file metadata is preserved in the result manifest."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Content")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")
        original_size = int(file_stat.st_size)
        original_mtime = int(file_stat.st_mtime_ns // 1000)

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=original_size,
                    mtime=original_mtime,
                )
            ],
            total_size=original_size,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert result.files[0].size == original_size
        assert result.files[0].mtime == original_mtime
        assert result.files[0].path == abs_path

    def test_symlinks_pass_through_unchanged(self, tmp_path: Path) -> None:
        """Test that symlinks are passed through without uploading."""
        # Create a target file and symlink
        target = tmp_path / "target.txt"
        target.write_text("Target content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=collected,
            data_cache=data_cache,
        )

        # Find symlink and file entries
        symlink_entries = [p for p in result.files if p.symlink_target is not None]
        file_entries = [p for p in result.files if p.symlink_target is None]

        assert len(symlink_entries) == 1
        assert len(file_entries) == 1

        # Symlink should have no hash
        assert symlink_entries[0].hash is None
        # File should have hash
        assert file_entries[0].hash != ""

        # Only the target file should be uploaded (not the symlink)
        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 1

    def test_directories_pass_through_unchanged(self, tmp_path: Path) -> None:
        """Test that directories are passed through without uploading."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("Content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=collected,
            data_cache=data_cache,
        )

        # Should have directory entries
        assert len(result.dirs) >= 1

        # Only the file should be uploaded
        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 1

    def test_deleted_entries_pass_through(self) -> None:
        """Test that deleted entries in diff manifests pass through unchanged."""
        # Create a diff manifest with a deleted entry
        manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path="/some/deleted/file.txt",
                    deleted=True,
                )
            ],
            total_size=0,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert len(result.files) == 1
        assert result.files[0].deleted is True
        assert result.files[0].hash is None

        # No uploads for deleted entries
        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 0

    def test_preserves_runnable_flag_from_filesystem(self, tmp_path: Path) -> None:
        """Test that runnable flag from filesystem is preserved (POSIX only)."""
        import os

        test_file = tmp_path / "script.sh"
        test_file.write_text("#!/bin/bash\necho hello")
        if os.name != "nt":
            test_file.chmod(0o755)

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=collected,
            data_cache=data_cache,
        )

        # Find the file entry
        file_entries = [p for p in result.files if p.symlink_target is None]
        assert len(file_entries) == 1
        # Runnable flag should be preserved from collected manifest
        collected_file = [p for p in collected.files if p.symlink_target is None][0]
        assert file_entries[0].runnable == collected_file.runnable

    @pytest.mark.parametrize("runnable", [True, False])
    def test_preserves_runnable_flag_from_manifest(self, tmp_path: Path, runnable: bool) -> None:
        """Test that runnable flag is preserved when set directly in manifest."""
        test_file = tmp_path / "script.sh"
        test_file.write_text("#!/bin/bash\necho hello")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        # Construct manifest directly with specified runnable value
        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                    runnable=runnable,
                )
            ],
            total_size=int(file_stat.st_size),
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert len(result.files) == 1
        assert result.files[0].runnable is runnable
        assert result.files[0].hash != ""

    def test_returns_abs_snapshot_manifest(self, tmp_path: Path) -> None:
        """Test that AbsSnapshotManifest input returns AbsSnapshotManifest."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=collected,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsSnapshotManifest)

    def test_returns_abs_diff_manifest(self, tmp_path: Path) -> None:
        """Test that AbsDiffManifest input returns AbsDiffManifest."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
            parent_manifest_hash="abc123",
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsDiffManifest)
        assert result.parentManifestHash == "abc123"


class TestHashUploadInputValidation:
    """Tests for input validation in hash_upload_manifest."""

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

    def test_rejects_relative_paths(self) -> None:
        """Test that manifest with relative paths raises ValueError."""
        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path="relative/path/file.txt",  # Relative path
                    hash="",
                    size=100,
                    mtime=12345,
                )
            ],
            total_size=100,
        )

        data_cache = self._create_s3_data_cache()
        with pytest.raises(ValueError, match="requires absolute paths"):
            hash_upload_manifest(
                manifest=manifest,
                data_cache=data_cache,
            )

    def test_accepts_absolute_paths(self, tmp_path: Path) -> None:
        """Test that manifest with absolute paths is accepted."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Content")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        data_cache = self._create_s3_data_cache()
        # Should not raise
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )
        assert result.files[0].hash != ""
