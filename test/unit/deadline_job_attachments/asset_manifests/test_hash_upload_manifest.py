# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Unit tests for the HASH_UPLOAD manifest operation.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch


from deadline.job_attachments.asset_manifests._operations import (
    hash_upload_manifest,
)
from deadline.job_attachments.asset_manifests._operations._hash_upload_manifest import (
    _ChunkWorkItem,
    _MemoryPool,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.v2023_03_03.asset_manifest import (
    AssetManifest as AssetManifest2023,
    ManifestPath as ManifestPath2023,
)
from deadline.job_attachments.asset_manifests.v2025_12_04.asset_manifest import (
    AssetManifest as AssetManifest2025,
    ManifestFilePath as ManifestFilePath2025,
)


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
        import threading

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
        import time

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
            rel_path="file.txt",
            file_size=1000,
            mtime=12345,
            chunk_index=0,
            chunk_start=0,
            chunk_end=1000,
        )
        assert item.file_path == Path("/test/file.txt")
        assert item.rel_path == "file.txt"
        assert item.file_size == 1000
        assert item.chunk_index == 0
        assert item.data is None
        assert item.chunk_hash is None


class TestHashUploadManifestV2023:
    """Tests for _hash_upload_manifest with v2023 manifests."""

    @patch(
        "deadline.job_attachments.asset_manifests._operations._hash_upload_manifest.get_boto3_session"
    )
    @patch(
        "deadline.job_attachments.asset_manifests._operations._hash_upload_manifest.get_s3_client"
    )
    @patch(
        "deadline.job_attachments.asset_manifests._operations._hash_upload_manifest.get_account_id"
    )
    def test_hash_upload_empty_manifest(
        self,
        mock_get_account_id: MagicMock,
        mock_get_s3_client: MagicMock,
        mock_get_boto3_session: MagicMock,
    ) -> None:
        """Test hashing and uploading an empty manifest."""
        mock_get_account_id.return_value = "123456789012"
        mock_s3_client = MagicMock()
        mock_get_s3_client.return_value = mock_s3_client

        manifest = AssetManifest2023(
            hash_alg=HashAlgorithm.XXH128,
            paths=[],
            total_size=0,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = hash_upload_manifest(
                manifest=manifest,
                root=tmpdir,
                s3_bucket="test-bucket",
                s3_key_prefix="Data",
            )

        assert isinstance(result, AssetManifest2023)
        assert len(result.paths) == 0
        assert result.totalSize == 0

    @patch(
        "deadline.job_attachments.asset_manifests._operations._hash_upload_manifest.get_boto3_session"
    )
    @patch(
        "deadline.job_attachments.asset_manifests._operations._hash_upload_manifest.get_s3_client"
    )
    @patch(
        "deadline.job_attachments.asset_manifests._operations._hash_upload_manifest.get_account_id"
    )
    def test_hash_upload_single_file(
        self,
        mock_get_account_id: MagicMock,
        mock_get_s3_client: MagicMock,
        mock_get_boto3_session: MagicMock,
    ) -> None:
        """Test hashing and uploading a single file."""
        mock_get_account_id.return_value = "123456789012"
        mock_s3_client = MagicMock()
        # Simulate file not existing in S3
        from botocore.exceptions import ClientError

        mock_s3_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )
        mock_get_s3_client.return_value = mock_s3_client

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a test file
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello, World!")
            file_stat = test_file.stat()

            manifest = AssetManifest2023(
                hash_alg=HashAlgorithm.XXH128,
                paths=[
                    ManifestPath2023(
                        path="test.txt",
                        hash="",  # Empty hash to be filled
                        size=int(file_stat.st_size),
                        mtime=int(file_stat.st_mtime_ns // 1000),
                    )
                ],
                total_size=int(file_stat.st_size),
            )

            result = hash_upload_manifest(
                manifest=manifest,
                root=tmpdir,
                s3_bucket="test-bucket",
                s3_key_prefix="Data",
            )

        assert isinstance(result, AssetManifest2023)
        assert len(result.paths) == 1
        assert result.paths[0].hash != ""  # Hash should be filled in
        assert result.paths[0].path == "test.txt"

        # Verify S3 upload was called
        mock_s3_client.put_object.assert_called()


class TestHashUploadManifestV2025:
    """Tests for _hash_upload_manifest with v2025 manifests."""

    @patch(
        "deadline.job_attachments.asset_manifests._operations._hash_upload_manifest.get_boto3_session"
    )
    @patch(
        "deadline.job_attachments.asset_manifests._operations._hash_upload_manifest.get_s3_client"
    )
    @patch(
        "deadline.job_attachments.asset_manifests._operations._hash_upload_manifest.get_account_id"
    )
    def test_hash_upload_with_symlink(
        self,
        mock_get_account_id: MagicMock,
        mock_get_s3_client: MagicMock,
        mock_get_boto3_session: MagicMock,
    ) -> None:
        """Test that symlinks are passed through unchanged."""
        mock_get_account_id.return_value = "123456789012"
        mock_s3_client = MagicMock()
        mock_get_s3_client.return_value = mock_s3_client

        manifest = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath2025(
                    path="link.txt",
                    symlink_target="target.txt",
                )
            ],
            total_size=0,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = hash_upload_manifest(
                manifest=manifest,
                root=tmpdir,
                s3_bucket="test-bucket",
                s3_key_prefix="Data",
            )

        assert isinstance(result, AssetManifest2025)
        assert len(result.paths) == 1
        assert result.paths[0].symlink_target == "target.txt"
        # Symlinks should not trigger S3 uploads
        mock_s3_client.put_object.assert_not_called()
