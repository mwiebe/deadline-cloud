# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for S3 multi-part parallel downloads.

These tests verify:
- Regular files use multi-part download when size >= MIN_SIZE_FOR_MULTIPART_DOWNLOAD
- Chunked files use multi-part download for large chunks
- Byte-range requests are correctly calculated
- Parts are written to correct offsets in temp file
- Small files/chunks use single-request download
"""

from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

from deadline.job_attachments._snapshots import (
    download_manifest,
    AbsSnapshot,
)
from deadline.job_attachments._snapshots._manifest import ManifestFilePath
from deadline.job_attachments._snapshots._content_addressed_data_cache import S3DataCache
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
import deadline.job_attachments._snapshots._operations._download_manifest_s3_pipeline as s3_pipeline_module


class TestMultipartDownloadRegularFiles:
    """Tests for multi-part download of regular (non-chunked) files."""

    def _create_mock_s3_client(self, file_contents: Dict[str, bytes]) -> MagicMock:
        """Create a mock S3 client that returns specified content for byte-range requests."""
        mock_client = MagicMock()

        def mock_get_object(Bucket: str, Key: str, Range: Optional[str] = None) -> Dict[str, Any]:
            content = file_contents.get(Key, b"")
            if Range:
                # Parse Range header: "bytes=start-end"
                range_str = Range.replace("bytes=", "")
                start, end = map(int, range_str.split("-"))
                content = content[start : end + 1]  # Range is inclusive
            body = MagicMock()
            body.read.return_value = content
            return {"Body": body}

        mock_client.get_object.side_effect = mock_get_object
        return mock_client

    def test_large_file_uses_multipart_download(self, tmp_path: Path) -> None:
        """Files >= MIN_SIZE_FOR_MULTIPART_DOWNLOAD use parallel byte-range requests."""
        download_dir = tmp_path / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

        # Create 100-byte file content (> 64 byte threshold)
        file_content = bytes(range(100))  # 0, 1, 2, ..., 99
        file_hash = "largefilehash"
        s3_key = f"Data/{file_hash}.xxh128"

        mock_client = self._create_mock_s3_client({s3_key: file_content})
        data_cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
        )

        target_path = download_dir / "large_file.bin"

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(target_path),
                    hash=file_hash,
                    size=len(file_content),
                    mtime=1000000,
                ),
            ],
            total_size=len(file_content),
        )

        # Patch the constants on the S3 pipeline module
        with patch.object(s3_pipeline_module, "MIN_SIZE_FOR_MULTIPART_DOWNLOAD", 64):
            with patch.object(s3_pipeline_module, "DEFAULT_MULTIPART_DOWNLOAD_PART_SIZE", 32):
                result = download_manifest(manifest=manifest, data_cache=data_cache)

        # Verify file was downloaded correctly
        assert target_path.exists()
        assert target_path.read_bytes() == file_content
        assert result.statistics.processed_files == 1

        # Verify multiple get_object calls were made (multi-part)
        # 100 bytes / 32 bytes per part = 4 parts
        assert mock_client.get_object.call_count == 4

        # Verify Range headers were used
        calls = mock_client.get_object.call_args_list
        ranges = [c.kwargs.get("Range") or c[1].get("Range") for c in calls]
        assert "bytes=0-31" in ranges
        assert "bytes=32-63" in ranges
        assert "bytes=64-95" in ranges
        assert "bytes=96-99" in ranges  # Last part is smaller

    def test_small_file_uses_single_request(self, tmp_path: Path) -> None:
        """Files < MIN_SIZE_FOR_MULTIPART_DOWNLOAD use single get_object request."""
        download_dir = tmp_path / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

        # Create 32-byte file content (< 64 byte threshold)
        file_content = b"small file content here!12345678"  # 32 bytes
        file_hash = "smallfilehash"
        s3_key = f"Data/{file_hash}.xxh128"

        mock_client = self._create_mock_s3_client({s3_key: file_content})
        data_cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
        )

        target_path = download_dir / "small_file.bin"

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(target_path),
                    hash=file_hash,
                    size=len(file_content),
                    mtime=1000000,
                ),
            ],
            total_size=len(file_content),
        )

        # Patch the constant on the S3 pipeline module
        with patch.object(s3_pipeline_module, "MIN_SIZE_FOR_MULTIPART_DOWNLOAD", 64):
            result = download_manifest(manifest=manifest, data_cache=data_cache)

        # Verify file was downloaded correctly
        assert target_path.exists()
        assert target_path.read_bytes() == file_content
        assert result.statistics.processed_files == 1

        # Verify only one get_object call was made (no multi-part)
        assert mock_client.get_object.call_count == 1

        # Verify no Range header was used
        call_kwargs = mock_client.get_object.call_args.kwargs
        assert "Range" not in call_kwargs


class TestMultipartDownloadChunkedFiles:
    """Tests for multi-part download of chunked files (files with chunkhashes)."""

    def _create_mock_s3_client(self, file_contents: Dict[str, bytes]) -> MagicMock:
        """Create a mock S3 client that returns specified content for byte-range requests."""
        mock_client = MagicMock()

        def mock_get_object(Bucket: str, Key: str, Range: Optional[str] = None) -> Dict[str, Any]:
            content = file_contents.get(Key, b"")
            if Range:
                # Parse Range header: "bytes=start-end"
                range_str = Range.replace("bytes=", "")
                start, end = map(int, range_str.split("-"))
                content = content[start : end + 1]  # Range is inclusive
            body = MagicMock()
            body.read.return_value = content
            return {"Body": body}

        mock_client.get_object.side_effect = mock_get_object
        return mock_client

    def test_large_chunk_uses_multipart_download(self, tmp_path: Path) -> None:
        """Chunks >= MIN_SIZE_FOR_MULTIPART_DOWNLOAD use parallel byte-range requests."""
        download_dir = tmp_path / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

        # Create 100-byte chunk content (> 64 byte threshold)
        chunk_content = bytes(range(100))  # 0, 1, 2, ..., 99
        chunk_hash = "largechunkhash"
        s3_key = f"Data/{chunk_hash}.xxh128"

        mock_client = self._create_mock_s3_client({s3_key: chunk_content})
        data_cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
        )

        target_path = download_dir / "chunked_file.bin"

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(target_path),
                    hash=None,
                    size=len(chunk_content),
                    mtime=1000000,
                    chunkhashes=[chunk_hash],
                ),
            ],
            total_size=len(chunk_content),
            file_chunk_size_bytes=256,  # Chunk size in manifest
        )

        # Patch the constants on the S3 pipeline module
        with patch.object(s3_pipeline_module, "MIN_SIZE_FOR_MULTIPART_DOWNLOAD", 64):
            with patch.object(s3_pipeline_module, "DEFAULT_MULTIPART_DOWNLOAD_PART_SIZE", 32):
                result = download_manifest(manifest=manifest, data_cache=data_cache)

        # Verify file was downloaded correctly
        assert target_path.exists()
        assert target_path.read_bytes() == chunk_content
        assert result.statistics.processed_files == 1

        # Verify multiple get_object calls were made (multi-part)
        # 100 bytes / 32 bytes per part = 4 parts
        assert mock_client.get_object.call_count == 4

    def test_small_chunk_uses_single_request(self, tmp_path: Path) -> None:
        """Chunks < MIN_SIZE_FOR_MULTIPART_DOWNLOAD use single get_object request."""
        download_dir = tmp_path / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

        # Create 32-byte chunk content (< 64 byte threshold)
        chunk_content = b"small chunk content here!1234567"  # 32 bytes
        chunk_hash = "smallchunkhash"
        s3_key = f"Data/{chunk_hash}.xxh128"

        mock_client = self._create_mock_s3_client({s3_key: chunk_content})
        data_cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
        )

        target_path = download_dir / "chunked_file.bin"

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(target_path),
                    hash=None,
                    size=len(chunk_content),
                    mtime=1000000,
                    chunkhashes=[chunk_hash],
                ),
            ],
            total_size=len(chunk_content),
            file_chunk_size_bytes=256,
        )

        # Patch the constant on the S3 pipeline module
        with patch.object(s3_pipeline_module, "MIN_SIZE_FOR_MULTIPART_DOWNLOAD", 64):
            result = download_manifest(manifest=manifest, data_cache=data_cache)

        # Verify file was downloaded correctly
        assert target_path.exists()
        assert target_path.read_bytes() == chunk_content
        assert result.statistics.processed_files == 1

        # Verify only one get_object call was made (no multi-part)
        assert mock_client.get_object.call_count == 1

        # Verify no Range header was used
        call_kwargs = mock_client.get_object.call_args.kwargs
        assert "Range" not in call_kwargs

    def test_multiple_large_chunks_correct_offsets(self, tmp_path: Path) -> None:
        """Multiple large chunks are written to correct offsets in output file."""
        download_dir = tmp_path / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

        # Create two 100-byte chunks
        chunk0_content = bytes([0x00] * 100)  # All zeros
        chunk1_content = bytes([0xFF] * 100)  # All 0xFF
        chunk0_hash = "chunk0hash"
        chunk1_hash = "chunk1hash"

        mock_client = self._create_mock_s3_client(
            {
                f"Data/{chunk0_hash}.xxh128": chunk0_content,
                f"Data/{chunk1_hash}.xxh128": chunk1_content,
            }
        )
        data_cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
        )

        target_path = download_dir / "multi_chunk_file.bin"
        chunk_size = 100

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(target_path),
                    hash=None,
                    size=len(chunk0_content) + len(chunk1_content),
                    mtime=1000000,
                    chunkhashes=[chunk0_hash, chunk1_hash],
                ),
            ],
            total_size=len(chunk0_content) + len(chunk1_content),
            file_chunk_size_bytes=chunk_size,
        )

        # Patch the constants on the S3 pipeline module
        with patch.object(s3_pipeline_module, "MIN_SIZE_FOR_MULTIPART_DOWNLOAD", 64):
            with patch.object(s3_pipeline_module, "DEFAULT_MULTIPART_DOWNLOAD_PART_SIZE", 32):
                result = download_manifest(manifest=manifest, data_cache=data_cache)

        # Verify file was downloaded correctly
        assert target_path.exists()
        downloaded_content = target_path.read_bytes()
        assert len(downloaded_content) == 200

        # Verify chunk0 is at offset 0 (all zeros)
        assert downloaded_content[:100] == chunk0_content

        # Verify chunk1 is at offset 100 (all 0xFF)
        assert downloaded_content[100:] == chunk1_content

        assert result.statistics.processed_files == 1

        # Verify multi-part was used for both chunks
        # Each 100-byte chunk = 4 parts (32 bytes each, last part smaller)
        # Total = 8 get_object calls
        assert mock_client.get_object.call_count == 8


class TestMultipartDownloadMixedFiles:
    """Tests for downloading a mix of small and large files."""

    def _create_mock_s3_client(self, file_contents: Dict[str, bytes]) -> MagicMock:
        """Create a mock S3 client that returns specified content for byte-range requests."""
        mock_client = MagicMock()

        def mock_get_object(Bucket: str, Key: str, Range: Optional[str] = None) -> Dict[str, Any]:
            content = file_contents.get(Key, b"")
            if Range:
                range_str = Range.replace("bytes=", "")
                start, end = map(int, range_str.split("-"))
                content = content[start : end + 1]
            body = MagicMock()
            body.read.return_value = content
            return {"Body": body}

        mock_client.get_object.side_effect = mock_get_object
        return mock_client

    def test_mixed_small_and_large_files(self, tmp_path: Path) -> None:
        """Mix of small (single request) and large (multi-part) files download correctly."""
        download_dir = tmp_path / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

        # Small file (32 bytes < 64 threshold)
        small_content = b"small file content here!12345678"
        small_hash = "smallhash"

        # Large file (100 bytes >= 64 threshold)
        large_content = bytes(range(100))
        large_hash = "largehash"

        mock_client = self._create_mock_s3_client(
            {
                f"Data/{small_hash}.xxh128": small_content,
                f"Data/{large_hash}.xxh128": large_content,
            }
        )
        data_cache = S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
        )

        small_path = download_dir / "small.bin"
        large_path = download_dir / "large.bin"

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(small_path),
                    hash=small_hash,
                    size=len(small_content),
                    mtime=1000000,
                ),
                ManifestFilePath(
                    path=str(large_path),
                    hash=large_hash,
                    size=len(large_content),
                    mtime=2000000,
                ),
            ],
            total_size=len(small_content) + len(large_content),
        )

        # Patch the constants on the S3 pipeline module
        with patch.object(s3_pipeline_module, "MIN_SIZE_FOR_MULTIPART_DOWNLOAD", 64):
            with patch.object(s3_pipeline_module, "DEFAULT_MULTIPART_DOWNLOAD_PART_SIZE", 32):
                result = download_manifest(manifest=manifest, data_cache=data_cache)

        # Verify both files downloaded correctly
        assert small_path.exists()
        assert small_path.read_bytes() == small_content
        assert large_path.exists()
        assert large_path.read_bytes() == large_content
        assert result.statistics.processed_files == 2

        # Small file = 1 call, Large file = 4 calls (100/32 = 4 parts)
        assert mock_client.get_object.call_count == 5
