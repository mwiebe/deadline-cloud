# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_upload_abs_manifest special entry handling.

These tests cover:
- Symlink entries (pass through unchanged)
- Deleted entries (pass through unchanged)
- Directory entries (pass through unchanged)
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
    AbsSnapshotDiff,
    AbsSnapshot,
    ManifestFilePath,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.caches.s3_check_cache import S3CheckCache


TEST_BUCKET = "test-hash-upload-bucket"
TEST_KEY_PREFIX = "Data"


class TestSymlinkPassthroughFileSystem:
    """Tests for symlink entry pass-through with FileSystemDataCache."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def _get_cache_files(self, cache_root: Path) -> Set[str]:
        """Get all file names in the cache directory."""
        return {f.name for f in cache_root.iterdir() if f.is_file()}

    def test_symlinks_pass_through_unchanged(self, tmp_path: Path) -> None:
        """Test that symlinks are passed through without writing to cache."""
        cache_root = tmp_path / "cache"

        target = tmp_path / "target.txt"
        target.write_text("Target content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        collected = collect_abs_snapshot(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        filtered_files = [f for f in collected.files if "cache" not in f.path]
        filtered_manifest = AbsSnapshot(
            hash_alg=collected.hashAlg,
            files=filtered_files,
            dirs=[d for d in collected.dirs if "cache" not in d.path],
            total_size=sum(f.size or 0 for f in filtered_files if f.size),
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_abs_manifest(
            manifest=filtered_manifest,
            data_cache=data_cache,
        )

        symlink_entries = [p for p in result.manifest.files if p.symlink_target is not None]
        file_entries = [p for p in result.manifest.files if p.symlink_target is None]

        assert len(symlink_entries) == 1
        assert len(file_entries) == 1

        assert symlink_entries[0].hash is None
        assert file_entries[0].hash is not None
        assert file_entries[0].hash != ""

        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 1


class TestSymlinkPassthroughS3:
    """Tests for symlink entry pass-through with S3DataCache."""

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

    def _create_s3_data_cache(self, s3_check_cache: Optional[S3CheckCache] = None) -> S3DataCache:
        """Create an S3DataCache for testing."""
        return S3DataCache(
            s3_bucket=TEST_BUCKET,
            s3_key_prefix=TEST_KEY_PREFIX,
            s3_client=self.s3_client,
            s3_check_cache=s3_check_cache,
        )

    def test_symlinks_pass_through_unchanged(self, tmp_path: Path) -> None:
        """Test that symlinks are passed through without uploading."""
        target = tmp_path / "target.txt"
        target.write_text("Target content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        collected = collect_abs_snapshot(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_abs_manifest(
            manifest=collected,
            data_cache=data_cache,
        )

        symlink_entries = [p for p in result.manifest.files if p.symlink_target is not None]
        file_entries = [p for p in result.manifest.files if p.symlink_target is None]

        assert len(symlink_entries) == 1
        assert len(file_entries) == 1

        assert symlink_entries[0].hash is None
        assert file_entries[0].hash is not None
        assert file_entries[0].hash != ""

        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 1


class TestDeletedEntryPassthroughFileSystem:
    """Tests for deleted entry pass-through with FileSystemDataCache."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def _get_cache_files(self, cache_root: Path) -> Set[str]:
        """Get all file names in the cache directory."""
        return {f.name for f in cache_root.iterdir() if f.is_file()}

    def test_deleted_entries_pass_through(self, tmp_path: Path) -> None:
        """Test that deleted entries in diff manifests pass through unchanged."""
        cache_root = tmp_path / "cache"

        manifest = AbsSnapshotDiff(
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

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert len(result.manifest.files) == 1
        assert result.manifest.files[0].deleted is True
        assert result.manifest.files[0].hash is None

        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 0


class TestDeletedEntryPassthroughS3:
    """Tests for deleted entry pass-through with S3DataCache."""

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

    def test_deleted_entries_pass_through(self) -> None:
        """Test that deleted entries in diff manifests pass through unchanged."""
        manifest = AbsSnapshotDiff(
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
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert len(result.manifest.files) == 1
        assert result.manifest.files[0].deleted is True
        assert result.manifest.files[0].hash is None

        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 0


class TestDirectoryEntryHandlingS3:
    """Tests for directory entry handling with S3DataCache."""

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

    def test_directories_pass_through_unchanged(self, tmp_path: Path) -> None:
        """Test that directories are passed through without uploading."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("Content")

        collected = collect_abs_snapshot(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_abs_manifest(
            manifest=collected,
            data_cache=data_cache,
        )

        assert len(result.manifest.dirs) >= 1

        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 1
