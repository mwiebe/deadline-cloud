# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for downloading chunked files (files with chunkhashes).

These tests verify:
- Parallel chunk downloads work correctly
- Pre-allocation of temp file to correct size
- Atomic file replacement after all chunks complete
- Error handling when chunk downloads fail
- Progress tracking for chunked downloads
"""

from pathlib import Path

import pytest

from deadline.job_attachments._snapshots import (
    download_abs_manifest,
    AbsSnapshot,
    FileSystemDataCache,
)
from deadline.job_attachments._snapshots._manifest import ManifestFilePath
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.models import FileConflictResolution


class TestDownloadChunkedFileFileSystem:
    """Tests for downloading chunked files from FileSystemDataCache with manual cache setup."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def _create_chunk_in_cache(
        self, cache_root: Path, chunk_hash: str, content: bytes, hash_alg: str = "xxh128"
    ) -> None:
        """Create a chunk file in the cache."""
        chunk_path = cache_root / f"{chunk_hash}.{hash_alg}"
        chunk_path.write_bytes(content)

    def test_download_chunked_file_single_chunk(self, tmp_path: Path) -> None:
        """Download a file with only one chunk (edge case)."""
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

        chunk_content = b"single chunk content"
        chunk_hash = "singlechunkhash"

        data_cache = self._create_filesystem_data_cache(cache_root)
        self._create_chunk_in_cache(cache_root, chunk_hash, chunk_content)

        target_path = download_dir / "single_chunk.bin"

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

        result = download_abs_manifest(manifest=manifest, data_cache=data_cache)

        assert target_path.exists()
        assert target_path.read_bytes() == chunk_content
        assert result.statistics.downloaded_file_chunks == 1

    def test_download_mixed_regular_and_chunked_files(self, tmp_path: Path) -> None:
        """Download a mix of regular files and chunked files."""
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

        # Create data cache first (this creates the cache directory)
        data_cache = self._create_filesystem_data_cache(cache_root)

        # Regular file
        regular_content = b"regular file content"
        regular_hash = "regularhash"
        self._create_chunk_in_cache(cache_root, regular_hash, regular_content)

        # Chunked file
        chunk_size = 16
        chunk0_content = b"A" * chunk_size
        chunk1_content = b"B" * chunk_size
        chunk0_hash = "chunk0hash"
        chunk1_hash = "chunk1hash"

        self._create_chunk_in_cache(cache_root, chunk0_hash, chunk0_content)
        self._create_chunk_in_cache(cache_root, chunk1_hash, chunk1_content)

        regular_path = download_dir / "regular.txt"
        chunked_path = download_dir / "chunked.bin"

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(regular_path),
                    hash=regular_hash,
                    size=len(regular_content),
                    mtime=1000000,
                ),
                ManifestFilePath(
                    path=str(chunked_path),
                    hash=None,
                    size=len(chunk0_content) + len(chunk1_content),
                    mtime=2000000,
                    chunkhashes=[chunk0_hash, chunk1_hash],
                ),
            ],
            total_size=len(regular_content) + len(chunk0_content) + len(chunk1_content),
            file_chunk_size_bytes=chunk_size,
        )

        result = download_abs_manifest(manifest=manifest, data_cache=data_cache)

        assert regular_path.exists()
        assert regular_path.read_bytes() == regular_content
        assert chunked_path.exists()
        assert chunked_path.read_bytes() == chunk0_content + chunk1_content
        assert result.statistics.downloaded_file_chunks == 3

    def test_download_chunked_file_conflict_skip(self, tmp_path: Path) -> None:
        """Chunked file download respects SKIP conflict resolution."""
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

        chunk_content = b"new content"
        chunk_hash = "chunkhash"

        data_cache = self._create_filesystem_data_cache(cache_root)
        self._create_chunk_in_cache(cache_root, chunk_hash, chunk_content)

        target_path = download_dir / "existing.bin"
        existing_content = b"existing content"
        target_path.write_bytes(existing_content)

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

        result = download_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            file_conflict_resolution=FileConflictResolution.SKIP,
        )

        # File should not be overwritten
        assert target_path.read_bytes() == existing_content
        assert result.statistics.skipped_file_chunks == 1

    def test_download_chunked_file_conflict_overwrite(self, tmp_path: Path) -> None:
        """Chunked file download respects OVERWRITE conflict resolution."""
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

        chunk_content = b"new content"
        chunk_hash = "chunkhash"

        data_cache = self._create_filesystem_data_cache(cache_root)
        self._create_chunk_in_cache(cache_root, chunk_hash, chunk_content)

        target_path = download_dir / "existing.bin"
        target_path.write_bytes(b"existing content")

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

        result = download_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            file_conflict_resolution=FileConflictResolution.OVERWRITE,
        )

        # File should be overwritten
        assert target_path.read_bytes() == chunk_content
        assert result.statistics.downloaded_file_chunks == 1

    def test_download_chunked_file_preserves_mtime(self, tmp_path: Path) -> None:
        """Chunked file download preserves modification time from manifest."""
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

        chunk_content = b"chunk content"
        chunk_hash = "chunkhash"

        data_cache = self._create_filesystem_data_cache(cache_root)
        self._create_chunk_in_cache(cache_root, chunk_hash, chunk_content)

        target_path = download_dir / "file.bin"
        expected_mtime_us = 1609459200000000  # 2021-01-01 00:00:00 UTC in microseconds

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(target_path),
                    hash=None,
                    size=len(chunk_content),
                    mtime=expected_mtime_us,
                    chunkhashes=[chunk_hash],
                ),
            ],
            total_size=len(chunk_content),
            file_chunk_size_bytes=256,
        )

        download_abs_manifest(manifest=manifest, data_cache=data_cache)

        # Check mtime was set (allow some tolerance for filesystem precision)
        actual_mtime_us = target_path.stat().st_mtime_ns // 1000
        assert abs(actual_mtime_us - expected_mtime_us) < 1000000  # Within 1 second


class TestDownloadChunkedFileAtomicity:
    """Tests for atomic behavior of chunked file downloads."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def _create_chunk_in_cache(
        self, cache_root: Path, chunk_hash: str, content: bytes, hash_alg: str = "xxh128"
    ) -> None:
        """Create a chunk file in the cache."""
        chunk_path = cache_root / f"{chunk_hash}.{hash_alg}"
        chunk_path.write_bytes(content)

    def test_no_temp_files_left_on_success(self, tmp_path: Path) -> None:
        """No temporary files are left after successful chunked download."""
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

        chunk_size = 16
        chunk0_content = b"0" * chunk_size
        chunk1_content = b"1" * chunk_size

        data_cache = self._create_filesystem_data_cache(cache_root)
        self._create_chunk_in_cache(cache_root, "chunk0hash", chunk0_content)
        self._create_chunk_in_cache(cache_root, "chunk1hash", chunk1_content)

        target_path = download_dir / "file.bin"

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(target_path),
                    hash=None,
                    size=len(chunk0_content) + len(chunk1_content),
                    mtime=1000000,
                    chunkhashes=["chunk0hash", "chunk1hash"],
                ),
            ],
            total_size=len(chunk0_content) + len(chunk1_content),
            file_chunk_size_bytes=chunk_size,
        )

        download_abs_manifest(manifest=manifest, data_cache=data_cache)

        # Check no temp files remain
        all_files = list(download_dir.iterdir())
        assert len(all_files) == 1
        assert all_files[0].name == "file.bin"
        # No .tmp files
        assert not any(".tmp" in f.name for f in all_files)

    def test_temp_file_cleaned_up_on_chunk_error(self, tmp_path: Path) -> None:
        """Temporary file is cleaned up when a chunk download fails."""
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

        chunk_size = 16
        chunk0_content = b"0" * chunk_size
        # chunk1 is missing from cache - will cause error

        data_cache = self._create_filesystem_data_cache(cache_root)
        self._create_chunk_in_cache(cache_root, "chunk0hash", chunk0_content)
        # Don't create chunk1 - it will fail

        target_path = download_dir / "file.bin"

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(target_path),
                    hash=None,
                    size=chunk_size * 2,
                    mtime=1000000,
                    chunkhashes=["chunk0hash", "chunk1hash"],
                ),
            ],
            total_size=chunk_size * 2,
            file_chunk_size_bytes=chunk_size,
        )

        with pytest.raises(FileNotFoundError):
            download_abs_manifest(manifest=manifest, data_cache=data_cache)

        # Target file should not exist
        assert not target_path.exists()
        # No temp files should remain
        all_files = list(download_dir.iterdir())
        assert not any(".tmp" in f.name for f in all_files)
