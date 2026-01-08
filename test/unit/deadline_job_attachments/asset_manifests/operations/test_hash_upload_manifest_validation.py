# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_upload_manifest input validation and error cases.

These tests cover:
- Relative path rejection
- Absolute path acceptance
- DataCache validation (FileSystemDataCache, S3DataCache)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from deadline.job_attachments.asset_manifests._operations import (
    hash_upload_manifest,
    FileSystemDataCache,
    S3DataCache,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests._manifest import (
    AbsSnapshotManifest,
    ManifestFilePath,
)
from deadline.job_attachments.caches.s3_check_cache import S3CheckCache


TEST_BUCKET = "test-hash-upload-bucket"
TEST_KEY_PREFIX = "Data"


class TestInputValidationFileSystem:
    """Tests for input validation with FileSystemDataCache."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_rejects_relative_paths(self, tmp_path: Path) -> None:
        """Test that manifest with relative paths raises ValueError."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path="relative/path/file.txt",
                    hash=None,
                    size=100,
                    mtime=12345,
                )
            ],
            total_size=100,
        )

        data_cache = FileSystemDataCache(root_path=cache_root)
        with pytest.raises(ValueError, match="requires absolute paths"):
            hash_upload_manifest(
                manifest=manifest,
                data_cache=data_cache,
            )

    def test_accepts_absolute_paths(self, tmp_path: Path) -> None:
        """Test that manifest with absolute paths is accepted."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "test.txt"
        test_file.write_text("Content")
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

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )
        assert result.files[0].hash is not None
        assert result.files[0].hash != ""


class TestInputValidationS3:
    """Tests for input validation with S3DataCache."""

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
                    path="relative/path/file.txt",
                    hash=None,
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
                    hash=None,
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
        assert result.files[0].hash is not None
        assert result.files[0].hash != ""


class TestFileSystemDataCacheValidation:
    """Tests for FileSystemDataCache validation."""

    def test_rejects_relative_root_path(self) -> None:
        """Test that FileSystemDataCache rejects relative root_path."""
        with pytest.raises(ValueError, match="root_path must be absolute"):
            FileSystemDataCache(root_path=Path("relative/path"))

    def test_accepts_absolute_root_path(self, tmp_path: Path) -> None:
        """Test that FileSystemDataCache accepts absolute root_path."""
        cache = FileSystemDataCache(root_path=tmp_path)
        assert cache.root_path == tmp_path

    def test_object_exists_returns_false_for_missing(self, tmp_path: Path) -> None:
        """Test that object_exists returns False for non-existent files."""
        cache = FileSystemDataCache(root_path=tmp_path)
        assert cache.object_exists("nonexistent", "xxh128") is False

    def test_object_exists_returns_true_for_existing(self, tmp_path: Path) -> None:
        """Test that object_exists returns True for existing files."""
        cache = FileSystemDataCache(root_path=tmp_path)
        (tmp_path / "somehash.xxh128").write_text("content")
        assert cache.object_exists("somehash", "xxh128") is True

    def test_get_object_key_format(self, tmp_path: Path) -> None:
        """Test that get_object_key returns correct path format."""
        cache = FileSystemDataCache(root_path=tmp_path)
        key = cache.get_object_key("abc123", "xxh128")
        expected = str(tmp_path / "abc123.xxh128")
        assert key == expected
