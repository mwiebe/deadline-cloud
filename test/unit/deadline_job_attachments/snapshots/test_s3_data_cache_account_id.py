# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for S3DataCache account_id handling and ExpectedBucketOwner.

These tests cover:
- Auto-detection of account ID from credentials
- Custom provided account ID
- NO_ACCOUNT_ID_CHECK to disable the check
- ExpectedBucketOwner passed to S3 API calls in upload and download
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from botocore.exceptions import ClientError

from deadline.job_attachments._snapshots import (
    S3DataCache,
    NO_ACCOUNT_ID_CHECK,
    hash_upload_abs_manifest,
    download_abs_manifest,
    AbsSnapshot,
    ManifestFilePath,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm


class TestS3DataCacheAccountId:
    """Tests for S3DataCache account_id initialization."""

    def test_no_account_id_check_disables_expected_bucket_owner(self) -> None:
        """NO_ACCOUNT_ID_CHECK should result in None expected_bucket_owner."""
        mock_client = MagicMock()
        cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
            account_id=NO_ACCOUNT_ID_CHECK,
        )
        assert cache.expected_bucket_owner is None

    def test_custom_account_id_used(self) -> None:
        """Custom account ID should be used as expected_bucket_owner."""
        mock_client = MagicMock()
        cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
            account_id="123456789012",
        )
        assert cache.expected_bucket_owner == "123456789012"

    def test_auto_detect_account_id_success(self) -> None:
        """Auto-detection should get account ID from credentials."""
        mock_client = MagicMock()

        with patch(
            "deadline.job_attachments._aws.aws_clients.get_boto3_session"
        ) as mock_session, patch(
            "deadline.job_attachments._aws.aws_clients.get_account_id"
        ) as mock_get_account:
            mock_get_account.return_value = "987654321098"

            cache = S3DataCache(
                s3_bucket="test-bucket",
                s3_key_prefix="Data",
                s3_client=mock_client,
                account_id=None,  # Auto-detect
            )

            assert cache.expected_bucket_owner == "987654321098"
            mock_session.assert_called_once()
            mock_get_account.assert_called_once()

    def test_auto_detect_account_id_failure_raises(self) -> None:
        """Auto-detection failure should raise ValueError."""
        mock_client = MagicMock()

        with patch("deadline.job_attachments._aws.aws_clients.get_boto3_session") as mock_session:
            mock_session.side_effect = Exception("No credentials")

            with pytest.raises(ValueError, match="Could not determine AWS account ID"):
                S3DataCache(
                    s3_bucket="test-bucket",
                    s3_key_prefix="Data",
                    s3_client=mock_client,
                    account_id=None,  # Auto-detect
                )


class TestS3DataCacheExpectedBucketOwnerInApiCalls:
    """Tests that ExpectedBucketOwner is passed to S3 API calls."""

    def test_head_object_includes_expected_bucket_owner(self) -> None:
        """head_object_exists should include ExpectedBucketOwner when set."""
        mock_client = MagicMock()
        cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
            account_id="123456789012",
        )

        cache.head_object_exists("abc123", "xxh128")

        mock_client.head_object.assert_called_once_with(
            Bucket="test-bucket",
            Key="Data/abc123.xxh128",
            ExpectedBucketOwner="123456789012",
        )

    def test_head_object_excludes_expected_bucket_owner_when_disabled(self) -> None:
        """head_object_exists should not include ExpectedBucketOwner when disabled."""
        mock_client = MagicMock()
        cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
            account_id=NO_ACCOUNT_ID_CHECK,
        )

        cache.head_object_exists("abc123", "xxh128")

        mock_client.head_object.assert_called_once_with(
            Bucket="test-bucket",
            Key="Data/abc123.xxh128",
        )


class TestUploadExpectedBucketOwner:
    """Tests that upload passes ExpectedBucketOwner to S3 calls."""

    def test_upload_includes_expected_bucket_owner(self, tmp_path: Path) -> None:
        """Upload should include ExpectedBucketOwner in put_object calls."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")
        file_stat = test_file.stat()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file),
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        mock_client = MagicMock()
        # Make head_object raise 404
        error_response = {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}
        mock_client.head_object.side_effect = ClientError(error_response, "HeadObject")
        mock_client.exceptions.ClientError = ClientError

        cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
            account_id="111122223333",
        )

        hash_upload_abs_manifest(manifest=manifest, data_cache=cache)

        # Verify put_object was called with ExpectedBucketOwner
        put_calls = mock_client.put_object.call_args_list
        assert len(put_calls) == 1
        assert put_calls[0].kwargs.get("ExpectedBucketOwner") == "111122223333"

    def test_upload_excludes_expected_bucket_owner_when_disabled(self, tmp_path: Path) -> None:
        """Upload should not include ExpectedBucketOwner when disabled."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")
        file_stat = test_file.stat()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file),
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        mock_client = MagicMock()
        error_response = {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}
        mock_client.head_object.side_effect = ClientError(error_response, "HeadObject")
        mock_client.exceptions.ClientError = ClientError

        cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
            account_id=NO_ACCOUNT_ID_CHECK,
        )

        hash_upload_abs_manifest(manifest=manifest, data_cache=cache)

        put_calls = mock_client.put_object.call_args_list
        assert len(put_calls) == 1
        assert "ExpectedBucketOwner" not in put_calls[0].kwargs


class TestDownloadExpectedBucketOwner:
    """Tests that download passes ExpectedBucketOwner to S3 calls."""

    def test_download_includes_expected_bucket_owner(self, tmp_path: Path) -> None:
        """Download should include ExpectedBucketOwner in get_object calls."""
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        file_hash = "abc123def456"
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(download_dir / "test.txt"),
                    hash=file_hash,
                    size=12,
                    mtime=1000000,
                )
            ],
            total_size=12,
        )

        mock_client = MagicMock()
        body_mock = MagicMock()
        body_mock.read.return_value = b"test content"
        mock_client.get_object.return_value = {"Body": body_mock}

        cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
            account_id="444455556666",
        )

        download_abs_manifest(manifest=manifest, data_cache=cache)

        get_calls = mock_client.get_object.call_args_list
        assert len(get_calls) == 1
        assert get_calls[0].kwargs.get("ExpectedBucketOwner") == "444455556666"

    def test_download_excludes_expected_bucket_owner_when_disabled(self, tmp_path: Path) -> None:
        """Download should not include ExpectedBucketOwner when disabled."""
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        file_hash = "abc123def456"
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(download_dir / "test.txt"),
                    hash=file_hash,
                    size=12,
                    mtime=1000000,
                )
            ],
            total_size=12,
        )

        mock_client = MagicMock()
        body_mock = MagicMock()
        body_mock.read.return_value = b"test content"
        mock_client.get_object.return_value = {"Body": body_mock}

        cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
            account_id=NO_ACCOUNT_ID_CHECK,
        )

        download_abs_manifest(manifest=manifest, data_cache=cache)

        get_calls = mock_client.get_object.call_args_list
        assert len(get_calls) == 1
        assert "ExpectedBucketOwner" not in get_calls[0].kwargs
