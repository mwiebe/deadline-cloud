# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_upload_manifest core functionality.

These tests cover:
- Basic file hashing and uploading
- Metadata preservation (size, mtime, runnable)
- Manifest type preservation (snapshot vs diff)
- Hash verification against direct hash_file()
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Set

import boto3
import pytest

from deadline.job_attachments._snapshots import (
    collect_manifest,
    hash_upload_manifest,
    FileSystemDataCache,
    S3DataCache,
    SymlinkPolicy,
    AbsDiffManifest,
    AbsSnapshotManifest,
    ManifestFilePath,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm, hash_file
from deadline.job_attachments.caches.s3_check_cache import S3CheckCache


TEST_BUCKET = "test-hash-upload-bucket"
TEST_KEY_PREFIX = "Data"


class TestHashUploadManifestBasicFileSystem:
    """Tests for basic hash_upload_manifest functionality with FileSystemDataCache."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def _get_cache_files(self, cache_root: Path) -> Set[str]:
        """Get all file names in the cache directory."""
        return {f.name for f in cache_root.iterdir() if f.is_file()}

    def _get_cache_file_content(self, cache_root: Path, filename: str) -> bytes:
        """Get the content of a cached file."""
        return (cache_root / filename).read_bytes()

    def test_hash_upload_empty_manifest(self, tmp_path: Path) -> None:
        """Test hashing and uploading an empty manifest."""
        cache_root = tmp_path / "cache"

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            total_size=0,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsSnapshotManifest)
        assert len(result.files) == 0
        assert result.totalSize == 0
        assert len(self._get_cache_files(cache_root)) == 0

    def test_hash_upload_single_file(self, tmp_path: Path) -> None:
        """Test hashing and uploading a single file."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello, World!")
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

        assert isinstance(result, AbsSnapshotManifest)
        assert len(result.files) == 1
        assert result.files[0].hash is not None
        assert result.files[0].hash != ""
        assert result.files[0].path == abs_path

        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 1

        expected_filename = f"{result.files[0].hash}.xxh128"
        assert expected_filename in cache_files

        cached_content = self._get_cache_file_content(cache_root, expected_filename)
        assert cached_content == test_file.read_bytes()

    def test_hash_upload_multiple_files(self, tmp_path: Path) -> None:
        """Test hashing and uploading multiple files."""
        cache_root = tmp_path / "cache"

        file1 = tmp_path / "file1.txt"
        file1.write_text("Content of file 1")
        file2 = tmp_path / "file2.txt"
        file2.write_text("Content of file 2")
        file3 = tmp_path / "subdir" / "file3.txt"
        file3.parent.mkdir()
        file3.write_text("Content of file 3")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ALL,
        )

        collected_files = [
            f for f in collected.files if "cache" not in f.path and f.symlink_target is None
        ]
        filtered_manifest = AbsSnapshotManifest(
            hash_alg=collected.hashAlg,
            files=collected_files,
            dirs=collected.dirs,
            total_size=sum(f.size or 0 for f in collected_files),
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=filtered_manifest,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsSnapshotManifest)
        file_entries = [p for p in result.files if p.symlink_target is None and not p.deleted]
        assert len(file_entries) == 3

        for entry in file_entries:
            assert entry.hash is not None
            assert entry.hash != ""
            assert len(entry.hash) == 32

        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 3

    def test_hash_matches_direct_hash(self, tmp_path: Path) -> None:
        """Test that computed hash matches direct hash_file() result."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content for hash verification")
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

        expected_hash = hash_file(str(test_file), HashAlgorithm.XXH128)
        assert result.files[0].hash == expected_hash

    def test_preserves_metadata(self, tmp_path: Path) -> None:
        """Test that file metadata is preserved in the result manifest."""
        cache_root = tmp_path / "cache"

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
                    hash=None,
                    size=original_size,
                    mtime=original_mtime,
                )
            ],
            total_size=original_size,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert result.files[0].size == original_size
        assert result.files[0].mtime == original_mtime
        assert result.files[0].path == abs_path

    @pytest.mark.parametrize("runnable", [True, False])
    def test_preserves_runnable_flag(self, tmp_path: Path, runnable: bool) -> None:
        """Test that runnable flag is preserved when set directly in manifest."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "script.sh"
        test_file.write_text("#!/bin/bash\necho hello")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                    runnable=runnable,
                )
            ],
            total_size=int(file_stat.st_size),
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert len(result.files) == 1
        assert result.files[0].runnable is runnable
        assert result.files[0].hash is not None


class TestHashUploadManifestBasicS3:
    """Tests for basic hash_upload_manifest functionality with S3DataCache."""

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
        assert len(self._get_s3_objects()) == 0

    def test_hash_upload_single_file(self, tmp_path: Path) -> None:
        """Test hashing and uploading a single file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello, World!")
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

        assert isinstance(result, AbsSnapshotManifest)
        assert len(result.files) == 1
        assert result.files[0].hash is not None
        assert result.files[0].hash != ""
        assert result.files[0].path == abs_path

        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 1

        expected_key = f"{TEST_KEY_PREFIX}/{result.files[0].hash}.xxh128"
        assert expected_key in s3_objects

        uploaded_content = self._get_s3_object_content(expected_key)
        assert uploaded_content == test_file.read_bytes()

    def test_hash_upload_multiple_files(self, tmp_path: Path) -> None:
        """Test hashing and uploading multiple files."""
        file1 = tmp_path / "file1.txt"
        file1.write_text("Content of file 1")
        file2 = tmp_path / "file2.txt"
        file2.write_text("Content of file 2")
        file3 = tmp_path / "subdir" / "file3.txt"
        file3.parent.mkdir()
        file3.write_text("Content of file 3")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ALL,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=collected,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsSnapshotManifest)
        file_entries = [p for p in result.files if p.symlink_target is None and not p.deleted]
        assert len(file_entries) == 3

        for entry in file_entries:
            assert entry.hash is not None
            assert entry.hash != ""
            assert len(entry.hash) == 32

        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 3

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
                    hash=None,
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

    def test_preserves_runnable_flag_from_filesystem(self, tmp_path: Path) -> None:
        """Test that runnable flag from filesystem is preserved (POSIX only)."""
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

        file_entries = [p for p in result.files if p.symlink_target is None]
        assert len(file_entries) == 1
        collected_file = [p for p in collected.files if p.symlink_target is None][0]
        assert file_entries[0].runnable == collected_file.runnable

    @pytest.mark.parametrize("runnable", [True, False])
    def test_preserves_runnable_flag_from_manifest(self, tmp_path: Path, runnable: bool) -> None:
        """Test that runnable flag is preserved when set directly in manifest."""
        test_file = tmp_path / "script.sh"
        test_file.write_text("#!/bin/bash\necho hello")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
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
        assert result.files[0].hash is not None


class TestManifestTypePreservation:
    """Tests verifying that manifest types are preserved through hash_upload_manifest."""

    @pytest.fixture(autouse=True)
    def setup_s3_bucket(self, s3, create_s3_bucket) -> None:
        """Create the test S3 bucket before each test."""
        create_s3_bucket(TEST_BUCKET)
        self.s3_client = s3

    def _create_s3_data_cache(self) -> S3DataCache:
        """Create an S3DataCache for testing."""
        return S3DataCache(
            s3_bucket=TEST_BUCKET,
            s3_key_prefix=TEST_KEY_PREFIX,
            s3_client=self.s3_client,
        )

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_returns_abs_snapshot_manifest_s3(self, tmp_path: Path) -> None:
        """Test that AbsSnapshotManifest input returns AbsSnapshotManifest (S3)."""
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

    def test_returns_abs_diff_manifest_s3(self, tmp_path: Path) -> None:
        """Test that AbsDiffManifest input returns AbsDiffManifest (S3)."""
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
                    hash=None,
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

    def test_returns_abs_snapshot_manifest_filesystem(self, tmp_path: Path) -> None:
        """Test that AbsSnapshotManifest input returns AbsSnapshotManifest (filesystem)."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        file_stat = test_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
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

        assert isinstance(result, AbsSnapshotManifest)

    def test_returns_abs_diff_manifest_filesystem(self, tmp_path: Path) -> None:
        """Test that AbsDiffManifest input returns AbsDiffManifest (filesystem)."""
        cache_root = tmp_path / "cache"

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
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
            parent_manifest_hash="abc123",
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsDiffManifest)
        assert result.parentManifestHash == "abc123"
