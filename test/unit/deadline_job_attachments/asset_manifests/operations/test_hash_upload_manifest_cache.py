# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_upload_manifest cache integration.

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

from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
    hash_upload_manifest,
    FileSystemDataCache,
    S3DataCache,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.versions import SymlinkPolicy
from deadline.job_attachments.asset_manifests.manifest import (
    AbsSnapshotManifest,
    ManifestFilePath,
)
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

        manifest = AbsSnapshotManifest(
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
            result = hash_upload_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            cache_key = str(test_file.resolve())
            cached_entry = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128)
            assert cached_entry is not None
            assert cached_entry.file_hash == result.files[0].hash


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

        manifest = AbsSnapshotManifest(
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
            result = hash_upload_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            cache_key = str(test_file.resolve())
            cached_entry = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128)
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

        assert result1.files[0].hash == result2.files[0].hash


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

        manifest = AbsSnapshotManifest(
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
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        file_entries = [p for p in result.files if p.symlink_target is None]
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

        manifest = AbsSnapshotManifest(
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
        result1 = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        cached_filename = f"{result1.files[0].hash}.xxh128"
        cached_file = cache_root / cached_filename
        original_mtime = cached_file.stat().st_mtime

        # Second upload - should skip since file exists
        result2 = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert result1.files[0].hash == result2.files[0].hash
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

        file_entries = [p for p in result.files if p.symlink_target is None]
        assert len(file_entries) == 2

        assert file_entries[0].hash == file_entries[1].hash

        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 1
