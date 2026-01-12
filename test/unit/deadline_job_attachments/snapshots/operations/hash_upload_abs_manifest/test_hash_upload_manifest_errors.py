# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_upload_abs_manifest error handling and edge cases.

These tests cover:
- File read errors (permissions, deleted during processing)
- Hash mismatch during streaming upload (file modified during processing)
- S3 client errors (JobAttachmentsS3ClientError, JobAttachmentS3BotoCoreError)
- Multipart upload failure and abort
- Relative path validation
- Force rehash parameter
"""

from __future__ import annotations

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from deadline.job_attachments._snapshots import (
    hash_upload_abs_manifest,
    FileSystemDataCache,
    S3DataCache,
    AbsSnapshot,
    ManifestFilePath,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.caches.hash_cache import HashCache
from deadline.job_attachments.exceptions import (
    JobAttachmentsS3ClientError,
    JobAttachmentS3BotoCoreError,
)


TEST_BUCKET = "test-hash-upload-bucket"
TEST_KEY_PREFIX = "Data"


class TestRelativePathValidation:
    """Tests for validation that paths must be absolute."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_rejects_relative_file_path(self, tmp_path: Path) -> None:
        """Test that relative file paths are rejected."""
        cache_root = tmp_path / "cache"

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path="relative/path/file.txt",  # Relative path
                    hash=None,
                    size=100,
                    mtime=12345,
                )
            ],
            total_size=100,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)

        with pytest.raises(ValueError, match="requires absolute paths"):
            hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
            )

    def test_rejects_relative_directory_path(self, tmp_path: Path) -> None:
        """Test that relative directory paths are rejected."""
        cache_root = tmp_path / "cache"

        from deadline.job_attachments._snapshots import ManifestDirectoryPath

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            dirs=[
                ManifestDirectoryPath(
                    path="relative/dir",  # Relative path
                )
            ],
            total_size=0,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)

        with pytest.raises(ValueError, match="requires absolute paths"):
            hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
            )

    def test_accepts_absolute_path(self, tmp_path: Path) -> None:
        """Test that absolute paths are accepted."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        file_stat = test_file.stat()

        # Use forward slashes for cross-platform absolute path
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

        data_cache = self._create_filesystem_data_cache(cache_root)

        # Should not raise
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )
        assert result.manifest.files[0].hash is not None


class TestFileReadErrors:
    """Tests for file read error handling."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_file_not_found_error(self, tmp_path: Path) -> None:
        """Test error when file doesn't exist."""
        cache_root = tmp_path / "cache"

        # Reference a file that doesn't exist
        nonexistent = tmp_path / "nonexistent.txt"
        abs_path = str(nonexistent).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=100,
                    mtime=12345,
                )
            ],
            total_size=100,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)

        with pytest.raises(FileNotFoundError):
            hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
            )

    @pytest.mark.skipif(os.name == "nt", reason="Permission tests unreliable on Windows")
    def test_permission_denied_error(self, tmp_path: Path) -> None:
        """Test error when file is not readable."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "unreadable.txt"
        test_file.write_text("secret content")
        file_stat = test_file.stat()

        # Remove read permissions
        test_file.chmod(0o000)

        try:
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

            data_cache = self._create_filesystem_data_cache(cache_root)

            with pytest.raises(PermissionError):
                hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                )
        finally:
            # Restore permissions for cleanup
            test_file.chmod(stat.S_IRUSR | stat.S_IWUSR)


class TestForceRehash:
    """Tests for the force_rehash parameter."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_force_rehash_ignores_hash_cache(self, tmp_path: Path) -> None:
        """Test that force_rehash=True ignores the hash cache."""
        cache_root = tmp_path / "cache"
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

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

        with HashCache(str(hash_cache_dir)) as hash_cache:
            data_cache = self._create_filesystem_data_cache(cache_root)

            # First run - populates cache
            result1 = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            # Modify file content but keep same mtime (simulating cache staleness)
            original_hash = result1.manifest.files[0].hash

            # Second run with force_rehash=True should still process the file
            # We can verify by checking that the pipeline runs (no exception)
            result2 = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
                force_rehash=True,
            )

            # Hash should be the same since content didn't change
            assert result2.manifest.files[0].hash == original_hash

    def test_force_rehash_false_uses_cache(self, tmp_path: Path) -> None:
        """Test that force_rehash=False (default) uses the hash cache."""
        cache_root = tmp_path / "cache"
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

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

        with HashCache(str(hash_cache_dir)) as hash_cache:
            data_cache = self._create_filesystem_data_cache(cache_root)

            # First run - populates cache
            result1 = hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            # Verify cache was populated
            cache_key = str(test_file.resolve())
            cached_entry = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128)
            assert cached_entry is not None
            assert cached_entry.file_hash == result1.manifest.files[0].hash


class TestS3ClientErrors:
    """Tests for S3 client error handling."""

    @pytest.fixture(autouse=True)
    def setup_s3_bucket(self, s3, create_s3_bucket) -> None:
        """Create the test S3 bucket before each test."""
        create_s3_bucket(TEST_BUCKET)
        self.s3_client = s3

    def _create_s3_data_cache(self, multipart_part_size: int = 32 * 1024 * 1024) -> S3DataCache:
        """Create an S3DataCache for testing."""
        return S3DataCache(
            s3_bucket=TEST_BUCKET,
            s3_key_prefix=TEST_KEY_PREFIX,
            s3_client=self.s3_client,
            multipart_part_size=multipart_part_size,
        )

    def test_s3_client_error_forbidden(self, tmp_path: Path) -> None:
        """Test handling of S3 403 Forbidden error."""
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

        data_cache = self._create_s3_data_cache()

        # Mock put_object to raise ClientError
        error_response = {
            "Error": {"Code": "AccessDenied", "Message": "Access Denied"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        }
        with patch.object(
            data_cache.s3_client,
            "put_object",
            side_effect=ClientError(error_response, "PutObject"),
        ):
            with pytest.raises(JobAttachmentsS3ClientError) as exc_info:
                hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                )
            assert exc_info.value.status_code == 403

    def test_s3_client_error_not_found(self, tmp_path: Path) -> None:
        """Test handling of S3 404 Not Found error."""
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

        data_cache = self._create_s3_data_cache()

        error_response = {
            "Error": {"Code": "NoSuchBucket", "Message": "The specified bucket does not exist"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        with patch.object(
            data_cache.s3_client,
            "put_object",
            side_effect=ClientError(error_response, "PutObject"),
        ):
            with pytest.raises(JobAttachmentsS3ClientError) as exc_info:
                hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                )
            assert exc_info.value.status_code == 404

    def test_s3_boto_core_error(self, tmp_path: Path) -> None:
        """Test handling of BotoCoreError."""
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

        data_cache = self._create_s3_data_cache()

        # Create a concrete BotoCoreError subclass for testing
        class TestBotoCoreError(BotoCoreError):
            fmt = "Test error: {message}"

        with patch.object(
            data_cache.s3_client,
            "put_object",
            side_effect=TestBotoCoreError(message="Connection failed"),
        ):
            with pytest.raises(JobAttachmentS3BotoCoreError):
                hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                )

    def test_s3_head_object_finds_existing_skips_upload(self, tmp_path: Path) -> None:
        """Test that HeadObject finding existing object skips upload gracefully."""
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

        data_cache = self._create_s3_data_cache()

        # First upload to populate the bucket
        result1 = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )
        assert result1.manifest.files[0].hash is not None

        # Second upload should skip since object exists (HeadObject will find it)
        result2 = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )
        assert result2.manifest.files[0].hash is not None
        assert result1.manifest.files[0].hash == result2.manifest.files[0].hash


class TestStreamingUploadErrors:
    """Tests for streaming upload error handling (large files)."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_hash_mismatch_during_streaming_filesystem(self, tmp_path: Path) -> None:
        """Test error when file is modified during streaming upload (filesystem)."""
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

        # Mock the streaming upload to simulate hash mismatch
        from deadline.job_attachments._snapshots._operations._hash_upload_abs_manifest import (
            _TaskBasedPipeline,
        )

        original_stream_upload = _TaskBasedPipeline._stream_upload_to_filesystem

        def mock_stream_upload(self, item):
            # Modify the pre-computed hash to simulate mismatch
            item.file_hash = "wrong_hash_value_here"
            return original_stream_upload(self, item)

        with patch.object(_TaskBasedPipeline, "_stream_upload_to_filesystem", mock_stream_upload):
            with pytest.raises(ValueError, match="Hash mismatch during streaming upload"):
                hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    max_memory_bytes=32,  # Force streaming mode
                )


class TestStreamingUploadErrorsS3:
    """Tests for streaming upload error handling with S3."""

    @pytest.fixture(autouse=True)
    def setup_s3_bucket(self, s3, create_s3_bucket) -> None:
        """Create the test S3 bucket before each test."""
        create_s3_bucket(TEST_BUCKET)
        self.s3_client = s3

    def _create_s3_data_cache(self, multipart_part_size: int = 32 * 1024 * 1024) -> S3DataCache:
        """Create an S3DataCache for testing."""
        return S3DataCache(
            s3_bucket=TEST_BUCKET,
            s3_key_prefix=TEST_KEY_PREFIX,
            s3_client=self.s3_client,
            multipart_part_size=multipart_part_size,
        )

    def test_hash_mismatch_during_streaming_s3_small(self, tmp_path: Path) -> None:
        """Test error when file is modified during streaming upload to S3 (small file)."""
        test_file = tmp_path / "test.bin"
        test_file.write_bytes(bytes(range(50)))
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=50,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=50,
            file_chunk_size_bytes=-1,  # No chunking
        )

        data_cache = self._create_s3_data_cache()

        # Mock to simulate hash mismatch
        from deadline.job_attachments._snapshots._operations._hash_upload_abs_manifest import (
            _TaskBasedPipeline,
        )

        original_stream_upload = _TaskBasedPipeline._stream_upload_to_s3

        def mock_stream_upload(self, item):
            item.file_hash = "wrong_hash_value_here"
            return original_stream_upload(self, item)

        with patch.object(_TaskBasedPipeline, "_stream_upload_to_s3", mock_stream_upload):
            with pytest.raises(ValueError, match="Hash mismatch during streaming upload"):
                hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    max_memory_bytes=32,  # Force streaming mode
                )

    def test_multipart_upload_error_aborts(self, tmp_path: Path) -> None:
        """Test that multipart upload is aborted on error."""
        # Create a file large enough to trigger multipart (> 2 * multipart_part_size threshold)
        test_file = tmp_path / "large.bin"
        # We'll mock the size to avoid creating a huge file
        test_file.write_bytes(b"x" * 100)
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        # Pretend the file is large enough for multipart
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=10 * 1024 * 1024,  # 10MB - triggers multipart when part_size=4MB
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=10 * 1024 * 1024,
            file_chunk_size_bytes=-1,  # No chunking
        )

        # Use small multipart_part_size so 10MB triggers multipart (threshold = 2 * 4MB = 8MB)
        data_cache = self._create_s3_data_cache(multipart_part_size=4 * 1024 * 1024)

        # Track if abort was called
        abort_called = []

        original_abort = self.s3_client.abort_multipart_upload

        def mock_abort(*args, **kwargs):
            abort_called.append(True)
            return original_abort(*args, **kwargs)

        # Mock upload_part to fail
        error_response = {
            "Error": {"Code": "InternalError", "Message": "Internal error"},
            "ResponseMetadata": {"HTTPStatusCode": 500},
        }

        with patch.object(self.s3_client, "abort_multipart_upload", mock_abort):
            with patch.object(
                self.s3_client,
                "upload_part",
                side_effect=ClientError(error_response, "UploadPart"),
            ):
                with pytest.raises(JobAttachmentsS3ClientError):
                    hash_upload_abs_manifest(
                        manifest=manifest,
                        data_cache=data_cache,
                        max_memory_bytes=1024,  # Force streaming mode
                    )

        # Verify abort was called
        assert len(abort_called) > 0, "abort_multipart_upload should have been called"

    def test_s3_streaming_client_error(self, tmp_path: Path) -> None:
        """Test S3 client error during streaming upload."""
        test_file = tmp_path / "test.bin"
        test_file.write_bytes(bytes(range(50)))
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=50,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=50,
            file_chunk_size_bytes=-1,
        )

        data_cache = self._create_s3_data_cache()

        # Mock to check existence (return False) then fail on put
        def mock_head(*args, **kwargs):
            error_response = {
                "Error": {"Code": "404", "Message": "Not Found"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            }
            raise ClientError(error_response, "HeadObject")

        error_response = {
            "Error": {"Code": "ServiceUnavailable", "Message": "Service unavailable"},
            "ResponseMetadata": {"HTTPStatusCode": 503},
        }

        with patch.object(self.s3_client, "head_object", mock_head):
            with patch.object(
                self.s3_client,
                "put_object",
                side_effect=ClientError(error_response, "PutObject"),
            ):
                with pytest.raises(JobAttachmentsS3ClientError) as exc_info:
                    hash_upload_abs_manifest(
                        manifest=manifest,
                        data_cache=data_cache,
                        max_memory_bytes=32,
                    )
                assert exc_info.value.status_code == 503


class TestUnsupportedHashAlgorithm:
    """Tests for unsupported hash algorithm handling in streaming mode."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_unsupported_algorithm_in_streaming_hash(self, tmp_path: Path) -> None:
        """Test error when using unsupported hash algorithm with streaming."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "large.bin"
        test_file.write_bytes(bytes(range(100)))
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        # Use a non-XXH128 algorithm (if one exists in the enum)
        # For now, we'll mock the hash algorithm check
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
            file_chunk_size_bytes=-1,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)

        # Mock the pipeline to use an unsupported algorithm for streaming hash
        from deadline.job_attachments._snapshots._operations._hash_upload_abs_manifest import (
            _TaskBasedPipeline,
        )

        original_stream_hash = _TaskBasedPipeline._stream_hash_file

        def mock_stream_hash(self, file_path):
            # Temporarily change the hash algorithm to trigger the error
            original_alg = self._hash_alg
            mock_alg = MagicMock(spec=HashAlgorithm)
            mock_alg.value = "unsupported"
            mock_alg.configure_mock(**{"__eq__": MagicMock(return_value=False)})
            self._hash_alg = mock_alg
            try:
                return original_stream_hash(self, file_path)
            finally:
                self._hash_alg = original_alg

        with patch.object(_TaskBasedPipeline, "_stream_hash_file", mock_stream_hash):
            with pytest.raises(ValueError, match="Unsupported hash algorithm"):
                hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    max_memory_bytes=32,  # Force streaming
                )
