# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for file_chunk_size_bytes parameter in COLLECT, HASH, and HASH_UPLOAD operations.

These tests verify:
- Default behavior (DEFAULT_FILE_CHUNK_SIZE = 256MB is the default)
- WHOLE_FILE_CHUNK_SIZE (-1) means "no chunking, hash whole file"
- Explicit positive chunk size values are used in output manifest
- Chunk size affects hashing behavior (chunked vs whole-file)
- Hash and upload behavior with small files and deduplication
"""

from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
    hash_manifest,
    hash_upload_manifest,
)
from deadline.job_attachments.asset_manifests._operations._content_addressed_data_cache import (
    FileSystemDataCache,
    S3DataCache,
)
from deadline.job_attachments.asset_manifests._manifest import (
    AbsSnapshotManifest,
    ManifestFilePath,
    DEFAULT_FILE_CHUNK_SIZE,
    WHOLE_FILE_CHUNK_SIZE,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm, hash_data
from deadline.job_attachments.asset_manifests.versions import SymlinkPolicy


class TestCollectManifestFileChunkSizeBytes:
    """Tests for file_chunk_size_bytes parameter in collect_manifest."""

    def test_default_chunk_size_when_not_specified(self, tmp_path: Path) -> None:
        """When file_chunk_size_bytes is not specified, uses DEFAULT_FILE_CHUNK_SIZE (256MB)."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_manifest(
            directories=[tmp_path],
            filenames=[],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # Default is DEFAULT_FILE_CHUNK_SIZE (256MB)
        assert manifest.fileChunkSizeBytes == DEFAULT_FILE_CHUNK_SIZE

    def test_explicit_chunk_size_is_used(self, tmp_path: Path) -> None:
        """When file_chunk_size_bytes is set to a positive int, that value is used."""
        (tmp_path / "file.txt").write_text("content")
        custom_chunk_size = 1024 * 1024  # 1MB

        manifest = collect_manifest(
            directories=[tmp_path],
            filenames=[],
            file_chunk_size_bytes=custom_chunk_size,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        assert manifest.fileChunkSizeBytes == custom_chunk_size

    def test_whole_file_chunk_size_means_no_chunking(self, tmp_path: Path) -> None:
        """When file_chunk_size_bytes is WHOLE_FILE_CHUNK_SIZE, files are hashed as a whole."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_manifest(
            directories=[tmp_path],
            filenames=[],
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # WHOLE_FILE_CHUNK_SIZE (-1) means no chunking
        assert manifest.fileChunkSizeBytes == WHOLE_FILE_CHUNK_SIZE
        assert manifest.fileChunkSizeBytes == -1


class TestHashManifestFileChunkSizeBytes:
    """Tests for file_chunk_size_bytes parameter in hash_manifest."""

    def test_preserves_input_chunk_size_when_none(self, tmp_path: Path) -> None:
        """When file_chunk_size_bytes is None, preserves input manifest's chunk size."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")

        input_chunk_size = 512 * 1024  # 512KB
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=7,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=7,
            file_chunk_size_bytes=input_chunk_size,
        )

        result = hash_manifest(
            manifest=input_manifest,
            file_chunk_size_bytes=None,  # Should preserve input
        )

        assert result.fileChunkSizeBytes == input_chunk_size

    def test_overrides_chunk_size_when_specified(self, tmp_path: Path) -> None:
        """When file_chunk_size_bytes is set, overrides input manifest's chunk size."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")

        input_chunk_size = 512 * 1024  # 512KB
        output_chunk_size = 1024 * 1024  # 1MB

        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=7,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=7,
            file_chunk_size_bytes=input_chunk_size,
        )

        result = hash_manifest(
            manifest=input_manifest,
            file_chunk_size_bytes=output_chunk_size,
        )

        assert result.fileChunkSizeBytes == output_chunk_size

    def test_whole_file_chunk_size_disables_chunking(self, tmp_path: Path) -> None:
        """When file_chunk_size_bytes is WHOLE_FILE_CHUNK_SIZE, no chunking occurs."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")

        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=7,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=7,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = hash_manifest(
            manifest=input_manifest,
            file_chunk_size_bytes=None,  # Preserve input
        )

        # Output should preserve WHOLE_FILE_CHUNK_SIZE
        assert result.fileChunkSizeBytes == WHOLE_FILE_CHUNK_SIZE
        # File should have hash (not chunkhashes)
        assert result.files[0].hash is not None
        assert result.files[0].chunkhashes is None

    def test_hash_computed_correctly_with_custom_chunk_size(self, tmp_path: Path) -> None:
        """Hash is computed correctly regardless of chunk size parameter."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content for hashing")
        abs_path = str(test_file).replace("\\", "/")
        file_stat = test_file.stat()

        input_manifest = AbsSnapshotManifest(
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
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,  # No chunking
        )

        result = hash_manifest(
            manifest=input_manifest,
            file_chunk_size_bytes=1024 * 1024,  # Set output chunk size
        )

        # Hash should be computed
        assert result.files[0].hash is not None
        assert len(result.files[0].hash) == 32  # XXH128 produces 32 hex chars
        # Output chunk size should be set
        assert result.fileChunkSizeBytes == 1024 * 1024


class TestHashUploadManifestFileChunkSizeBytes:
    """Tests for file_chunk_size_bytes parameter in hash_upload_manifest."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_preserves_input_chunk_size_when_none(self, tmp_path: Path) -> None:
        """When file_chunk_size_bytes is None, preserves input manifest's chunk size."""
        cache_root = tmp_path / "cache"
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")

        input_chunk_size = 512 * 1024  # 512KB
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=7,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=7,
            file_chunk_size_bytes=input_chunk_size,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            file_chunk_size_bytes=None,  # Should preserve input
        )

        assert result.fileChunkSizeBytes == input_chunk_size

    def test_overrides_chunk_size_when_specified(self, tmp_path: Path) -> None:
        """When file_chunk_size_bytes is set, overrides input manifest's chunk size."""
        cache_root = tmp_path / "cache"
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")

        input_chunk_size = 512 * 1024  # 512KB
        output_chunk_size = 1024 * 1024  # 1MB

        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=7,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=7,
            file_chunk_size_bytes=input_chunk_size,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            file_chunk_size_bytes=output_chunk_size,
        )

        assert result.fileChunkSizeBytes == output_chunk_size

    def test_hash_and_upload_with_custom_chunk_size(self, tmp_path: Path) -> None:
        """Hash and upload work correctly with custom chunk size."""
        cache_root = tmp_path / "cache"
        test_file = tmp_path / "file.txt"
        test_file.write_text("test content for hashing and uploading")
        abs_path = str(test_file).replace("\\", "/")
        file_stat = test_file.stat()

        input_manifest = AbsSnapshotManifest(
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
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,  # No chunking for input
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            file_chunk_size_bytes=2 * 1024 * 1024,  # Set output chunk size
        )

        # Hash should be computed
        assert result.files[0].hash is not None
        assert len(result.files[0].hash) == 32  # XXH128 produces 32 hex chars
        # Output chunk size should be set
        assert result.fileChunkSizeBytes == 2 * 1024 * 1024
        # File should be uploaded to cache
        assert data_cache.object_exists(result.files[0].hash, "xxh128")

    def test_whole_file_chunk_size_disables_chunking(self, tmp_path: Path) -> None:
        """When file_chunk_size_bytes is WHOLE_FILE_CHUNK_SIZE, no chunking occurs."""
        cache_root = tmp_path / "cache"
        test_file = tmp_path / "file.txt"
        test_file.write_text("content for whole file test")
        abs_path = str(test_file).replace("\\", "/")
        file_stat = test_file.stat()

        input_manifest = AbsSnapshotManifest(
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
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            file_chunk_size_bytes=None,  # Preserve input
        )

        # Output should preserve WHOLE_FILE_CHUNK_SIZE
        assert result.fileChunkSizeBytes == WHOLE_FILE_CHUNK_SIZE
        # File should have hash (not chunkhashes)
        assert result.files[0].hash is not None
        assert result.files[0].chunkhashes is None
        # File should be uploaded to cache
        assert data_cache.object_exists(result.files[0].hash, "xxh128")


class TestEndToEndFileChunkSizeBytes:
    """End-to-end tests for file_chunk_size_bytes through the pipeline."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_collect_then_hash_preserves_chunk_size(self, tmp_path: Path) -> None:
        """Chunk size flows from collect_manifest through hash_manifest."""
        (tmp_path / "file.txt").write_text("content")
        custom_chunk_size = 1024 * 1024  # 1MB

        # Collect with custom chunk size
        collected = collect_manifest(
            directories=[tmp_path],
            filenames=[],
            file_chunk_size_bytes=custom_chunk_size,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        assert collected.fileChunkSizeBytes == custom_chunk_size

        # Hash should preserve chunk size when None
        hashed = hash_manifest(
            manifest=collected,
            file_chunk_size_bytes=None,
        )
        assert hashed.fileChunkSizeBytes == custom_chunk_size

    def test_collect_then_hash_upload_preserves_chunk_size(self, tmp_path: Path) -> None:
        """Chunk size flows from collect_manifest through hash_upload_manifest."""
        cache_root = tmp_path / "cache"
        (tmp_path / "file.txt").write_text("content")
        custom_chunk_size = 1024 * 1024  # 1MB

        # Collect with custom chunk size
        collected = collect_manifest(
            directories=[tmp_path],
            filenames=[],
            file_chunk_size_bytes=custom_chunk_size,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        assert collected.fileChunkSizeBytes == custom_chunk_size

        # Hash+upload should preserve chunk size when None
        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=collected,
            data_cache=data_cache,
            file_chunk_size_bytes=None,
        )
        assert result.fileChunkSizeBytes == custom_chunk_size

    def test_override_chunk_size_at_each_stage(self, tmp_path: Path) -> None:
        """Chunk size can be overridden at each stage of the pipeline."""
        (tmp_path / "file.txt").write_text("content")

        collect_chunk_size = 512 * 1024  # 512KB
        hash_chunk_size = 1024 * 1024  # 1MB

        # Collect with one chunk size
        collected = collect_manifest(
            directories=[tmp_path],
            filenames=[],
            file_chunk_size_bytes=collect_chunk_size,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        assert collected.fileChunkSizeBytes == collect_chunk_size

        # Hash with different chunk size
        hashed = hash_manifest(
            manifest=collected,
            file_chunk_size_bytes=hash_chunk_size,
        )
        assert hashed.fileChunkSizeBytes == hash_chunk_size

    def test_override_chunk_size_affects_chunking_decision_hash(self, tmp_path: Path) -> None:
        """
        BUG TEST: Overriding chunk size should affect chunking decision.

        If input manifest has large chunk size (256MB) but we override with small
        chunk size (16 bytes), files larger than 16 bytes should be chunked.
        """
        test_file = tmp_path / "file.bin"
        file_size = 64  # 64 bytes - larger than 16, smaller than 256MB
        content = bytes(range(64))
        test_file.write_bytes(content)
        abs_path = str(test_file).replace("\\", "/")

        # Input manifest has large chunk size (no chunking for 64-byte file)
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=file_size,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=DEFAULT_FILE_CHUNK_SIZE,  # 256MB - file won't be chunked
        )

        # Override with small chunk size - file SHOULD be chunked now
        small_chunk_size = 16
        result = hash_manifest(
            manifest=input_manifest,
            file_chunk_size_bytes=small_chunk_size,
        )

        # Output should have the overridden chunk size
        assert result.fileChunkSizeBytes == small_chunk_size

        # File (64 bytes) is larger than chunk size (16 bytes), so it SHOULD have chunkhashes
        # Expected: 64 / 16 = 4 chunks
        assert result.files[0].hash is None, "File should have chunkhashes, not hash"
        assert result.files[0].chunkhashes is not None, "File should have chunkhashes"
        assert len(result.files[0].chunkhashes) == 4, "File should have 4 chunks (64/16)"

    def test_override_chunk_size_affects_chunking_decision_hash_upload(
        self, tmp_path: Path
    ) -> None:
        """
        BUG TEST: Overriding chunk size should affect chunking decision in hash_upload.

        If input manifest has large chunk size (256MB) but we override with small
        chunk size (16 bytes), files larger than 16 bytes should be chunked.
        """
        cache_root = tmp_path / "cache"
        test_file = tmp_path / "file.bin"
        file_size = 64  # 64 bytes - larger than 16, smaller than 256MB
        content = bytes(range(64))
        test_file.write_bytes(content)
        abs_path = str(test_file).replace("\\", "/")

        # Input manifest has large chunk size (no chunking for 64-byte file)
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=file_size,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=DEFAULT_FILE_CHUNK_SIZE,  # 256MB - file won't be chunked
        )

        # Override with small chunk size - file SHOULD be chunked now
        small_chunk_size = 16
        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            file_chunk_size_bytes=small_chunk_size,
        )

        # Output should have the overridden chunk size
        assert result.fileChunkSizeBytes == small_chunk_size

        # File (64 bytes) is larger than chunk size (16 bytes), so it SHOULD have chunkhashes
        # Expected: 64 / 16 = 4 chunks
        assert result.files[0].hash is None, "File should have chunkhashes, not hash"
        assert result.files[0].chunkhashes is not None, "File should have chunkhashes"
        assert len(result.files[0].chunkhashes) == 4, "File should have 4 chunks (64/16)"


class TestHashManifestSmallFiles:
    """
    Tests for hash_manifest with small files and small chunk sizes.

    These tests verify that chunking works correctly with small chunk sizes,
    producing chunkhashes for files larger than the chunk size.
    """

    def test_hash_manifest_small_file_with_small_chunk_size_produces_chunkhashes(
        self, tmp_path: Path
    ) -> None:
        """Test that small files with small chunk sizes produce chunkhashes."""
        test_file = tmp_path / "file.bin"
        file_size = 1024  # 1KB

        # Create file with known content
        content = bytes(range(256)) * 4  # 1024 bytes of repeating pattern
        test_file.write_bytes(content)
        abs_path = str(test_file).replace("\\", "/")

        # With 16-byte chunk size, a 1024-byte file should have 64 chunks
        chunk_size = 16
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=file_size,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=chunk_size,
        )

        result = hash_manifest(manifest=input_manifest)

        # File should have chunkhashes, not hash
        assert result.files[0].hash is None
        assert result.files[0].chunkhashes is not None
        assert len(result.files[0].chunkhashes) == 64  # 1024 / 16 = 64 chunks

        # Verify each chunk hash is correct
        for i, chunk_hash in enumerate(result.files[0].chunkhashes):
            chunk_start = i * chunk_size
            chunk_end = min(chunk_start + chunk_size, file_size)
            expected_hash = hash_data(content[chunk_start:chunk_end], HashAlgorithm.XXH128)
            assert chunk_hash == expected_hash, f"Chunk {i} hash mismatch"

    def test_hash_manifest_file_smaller_than_chunk_size_produces_single_hash(
        self, tmp_path: Path
    ) -> None:
        """Test that files smaller than chunk size produce a single hash."""
        test_file = tmp_path / "file.bin"
        file_size = 100  # 100 bytes

        content = bytes(range(100))
        test_file.write_bytes(content)
        abs_path = str(test_file).replace("\\", "/")

        # With 256-byte chunk size, a 100-byte file should have a single hash
        chunk_size = 256
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=file_size,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=chunk_size,
        )

        result = hash_manifest(manifest=input_manifest)

        # File should have hash, not chunkhashes
        assert result.files[0].hash is not None
        assert result.files[0].chunkhashes is None

        # Verify hash is correct
        expected_hash = hash_data(content, HashAlgorithm.XXH128)
        assert result.files[0].hash == expected_hash

    def test_hash_manifest_multiple_small_files_with_small_chunks(self, tmp_path: Path) -> None:
        """Test hashing multiple small files with small chunk sizes."""
        files_data = [
            ("tiny.bin", 32),  # 2 chunks with 16-byte chunk size
            ("small.bin", 64),  # 4 chunks
            ("medium.bin", 128),  # 8 chunks
        ]

        file_entries = []
        file_contents: Dict[str, bytes] = {}
        for filename, size in files_data:
            test_file = tmp_path / filename
            content = bytes([i % 256 for i in range(size)])
            test_file.write_bytes(content)
            abs_path = str(test_file).replace("\\", "/")
            file_contents[abs_path] = content
            file_entries.append(
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=size,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            )

        chunk_size = 16
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=file_entries,
            total_size=sum(size for _, size in files_data),
            file_chunk_size_bytes=chunk_size,
        )

        result = hash_manifest(manifest=input_manifest)

        # All files should have chunkhashes since they're all > chunk_size
        for i, (filename, size) in enumerate(files_data):
            expected_chunks = size // chunk_size
            assert result.files[i].hash is None, f"File {filename} should not have hash"
            chunkhashes = result.files[i].chunkhashes
            assert chunkhashes is not None, f"File {filename} should have chunkhashes"
            assert len(chunkhashes) == expected_chunks, (
                f"File {filename} should have {expected_chunks} chunks"
            )

    def test_hash_manifest_identical_files_same_hash(self, tmp_path: Path) -> None:
        """Test that identical files produce the same hash."""
        content = bytes(range(256))

        file1 = tmp_path / "file1.bin"
        file2 = tmp_path / "file2.bin"
        file1.write_bytes(content)
        file2.write_bytes(content)

        abs_path1 = str(file1).replace("\\", "/")
        abs_path2 = str(file2).replace("\\", "/")

        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path1,
                    hash=None,
                    size=256,
                    mtime=int(file1.stat().st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=abs_path2,
                    hash=None,
                    size=256,
                    mtime=int(file2.stat().st_mtime_ns // 1000),
                ),
            ],
            total_size=512,
        )

        result = hash_manifest(manifest=input_manifest)

        # Both files should have the same hash
        assert result.files[0].hash == result.files[1].hash

    def test_hash_manifest_different_files_different_hash(self, tmp_path: Path) -> None:
        """Test that different files produce different hashes."""
        file1 = tmp_path / "file1.bin"
        file2 = tmp_path / "file2.bin"
        file1.write_bytes(b"content1")
        file2.write_bytes(b"content2")

        abs_path1 = str(file1).replace("\\", "/")
        abs_path2 = str(file2).replace("\\", "/")

        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path1,
                    hash=None,
                    size=8,
                    mtime=int(file1.stat().st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=abs_path2,
                    hash=None,
                    size=8,
                    mtime=int(file2.stat().st_mtime_ns // 1000),
                ),
            ],
            total_size=16,
        )

        result = hash_manifest(manifest=input_manifest)

        # Files should have different hashes
        assert result.files[0].hash != result.files[1].hash


class TestHashUploadManifestFilesystem:
    """
    Tests for hash_upload_manifest with FileSystemDataCache.

    These tests verify that chunking works correctly with small chunk sizes,
    and that deduplication and idempotent uploads work as expected.
    """

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_hash_upload_small_file_with_small_chunks_filesystem(self, tmp_path: Path) -> None:
        """Test hash+upload a small file with small chunk size to filesystem cache."""
        cache_root = tmp_path / "cache"
        test_file = tmp_path / "file.bin"
        file_size = 1024  # 1KB

        # Create file with known content
        content = bytes(range(256)) * 4  # 1024 bytes
        test_file.write_bytes(content)
        abs_path = str(test_file).replace("\\", "/")

        # With 64-byte chunk size, a 1024-byte file should have 16 chunks
        chunk_size = 64
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=file_size,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=chunk_size,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            max_memory_bytes=1024 * 1024,  # 1MB memory limit
        )

        # File should have chunkhashes, not hash
        assert result.files[0].hash is None
        assert result.files[0].chunkhashes is not None
        assert len(result.files[0].chunkhashes) == 16  # 1024 / 64 = 16 chunks

        # Verify each chunk was uploaded to cache
        for chunk_hash in result.files[0].chunkhashes:
            assert data_cache.object_exists(chunk_hash, "xxh128")

    def test_hash_upload_multiple_files_with_chunks_filesystem(self, tmp_path: Path) -> None:
        """Test hash+upload multiple small files with small chunk sizes to filesystem cache."""
        cache_root = tmp_path / "cache"
        files_data = [
            ("small.bin", 64),  # 2 chunks with 32-byte chunk size
            ("medium.bin", 128),  # 4 chunks
            ("large.bin", 256),  # 8 chunks
        ]

        file_entries = []
        file_contents: Dict[str, bytes] = {}
        for filename, size in files_data:
            test_file = tmp_path / filename
            content = bytes([i % 256 for i in range(size)])
            test_file.write_bytes(content)
            abs_path = str(test_file).replace("\\", "/")
            file_contents[abs_path] = content
            file_entries.append(
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=size,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            )

        chunk_size = 32
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=file_entries,
            total_size=sum(size for _, size in files_data),
            file_chunk_size_bytes=chunk_size,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            max_memory_bytes=1024 * 1024,
        )

        # Verify each file has chunkhashes and chunks were uploaded
        for i, (filename, size) in enumerate(files_data):
            expected_chunks = size // chunk_size
            assert result.files[i].hash is None
            chunkhashes = result.files[i].chunkhashes
            assert chunkhashes is not None
            assert len(chunkhashes) == expected_chunks

            for chunk_hash in chunkhashes:
                assert data_cache.object_exists(chunk_hash, "xxh128")

    def test_hash_upload_idempotent_filesystem(self, tmp_path: Path) -> None:
        """Test that re-uploading same file doesn't duplicate chunks in filesystem cache."""
        cache_root = tmp_path / "cache"
        test_file = tmp_path / "file.bin"
        file_size = 256

        content = bytes(range(256))
        test_file.write_bytes(content)
        abs_path = str(test_file).replace("\\", "/")

        # Use 64-byte chunks (4 chunks total)
        chunk_size = 64
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=file_size,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=chunk_size,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)

        # First upload
        result1 = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            max_memory_bytes=1024 * 1024,
        )

        # Count files in cache
        cache_files_after_first = list(cache_root.glob("*.xxh128"))

        # Second upload (same file)
        result2 = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            max_memory_bytes=1024 * 1024,
        )

        # Count files in cache after second upload
        cache_files_after_second = list(cache_root.glob("*.xxh128"))

        # Should have same chunkhashes
        assert result1.files[0].chunkhashes == result2.files[0].chunkhashes

        # Should have same number of files (no duplicates)
        assert len(cache_files_after_first) == len(cache_files_after_second)
        assert len(cache_files_after_first) == 4  # 4 unique chunks

    def test_hash_upload_deduplication_filesystem(self, tmp_path: Path) -> None:
        """Test that identical chunks are deduplicated in filesystem cache."""
        cache_root = tmp_path / "cache"

        # Create two files with identical content
        content = bytes(range(128))
        file1 = tmp_path / "file1.bin"
        file2 = tmp_path / "file2.bin"
        file1.write_bytes(content)
        file2.write_bytes(content)

        abs_path1 = str(file1).replace("\\", "/")
        abs_path2 = str(file2).replace("\\", "/")

        # Use 32-byte chunks (4 chunks per file)
        chunk_size = 32
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path1,
                    hash=None,
                    size=128,
                    mtime=int(file1.stat().st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=abs_path2,
                    hash=None,
                    size=128,
                    mtime=int(file2.stat().st_mtime_ns // 1000),
                ),
            ],
            total_size=256,
            file_chunk_size_bytes=chunk_size,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            max_memory_bytes=1024 * 1024,
        )

        # Both files should have the same chunkhashes
        assert result.files[0].chunkhashes == result.files[1].chunkhashes

        # Only 4 unique chunks should be in cache (not 8)
        cache_files = list(cache_root.glob("*.xxh128"))
        assert len(cache_files) == 4


class TestHashUploadManifestS3:
    """
    Tests for hash_upload_manifest with S3DataCache.

    These tests verify that chunking works correctly with small chunk sizes,
    and that deduplication and idempotent uploads work as expected with S3.
    """

    def _create_mock_s3_client(self) -> MagicMock:
        """Create a mock S3 client that tracks uploaded objects."""
        mock_client = MagicMock()
        uploaded_objects: Dict[str, bytes] = {}

        def mock_put_object(**kwargs: Any) -> Dict[str, Any]:
            bucket = kwargs["Bucket"]
            key = kwargs["Key"]
            body = kwargs["Body"]
            full_key = f"{bucket}/{key}"

            # Check IfNoneMatch for conditional write
            if kwargs.get("IfNoneMatch") == "*" and full_key in uploaded_objects:
                # Simulate PreconditionFailed
                from botocore.exceptions import ClientError

                error_response = {
                    "Error": {"Code": "PreconditionFailed", "Message": "Object already exists"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                }
                raise ClientError(error_response, "PutObject")

            uploaded_objects[full_key] = body
            return {"ETag": f'"{hash_data(body, HashAlgorithm.XXH128)}"'}

        def mock_head_object(**kwargs: Any) -> Dict[str, Any]:
            bucket = kwargs["Bucket"]
            key = kwargs["Key"]
            full_key = f"{bucket}/{key}"

            if full_key not in uploaded_objects:
                from botocore.exceptions import ClientError

                error_response = {
                    "Error": {"Code": "404", "Message": "Not Found"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                }
                raise ClientError(error_response, "HeadObject")

            return {"ContentLength": len(uploaded_objects[full_key])}

        mock_client.put_object = MagicMock(side_effect=mock_put_object)
        mock_client.head_object = MagicMock(side_effect=mock_head_object)
        mock_client._uploaded_objects = uploaded_objects  # For test verification

        return mock_client

    def _create_s3_data_cache(self, mock_client: MagicMock) -> S3DataCache:
        """Create an S3DataCache with a mock client."""
        return S3DataCache(
            s3_bucket="test-bucket",
            s3_key_prefix="Data",
            s3_client=mock_client,
            s3_check_cache=None,
        )

    def test_hash_upload_small_file_with_small_chunks_s3(self, tmp_path: Path) -> None:
        """Test hash+upload a small file with small chunk size to S3 cache."""
        test_file = tmp_path / "file.bin"
        file_size = 1024  # 1KB

        # Create file with known content
        content = bytes(range(256)) * 4  # 1024 bytes
        test_file.write_bytes(content)
        abs_path = str(test_file).replace("\\", "/")

        # With 64-byte chunk size, a 1024-byte file should have 16 chunks
        chunk_size = 64
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=file_size,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=chunk_size,
        )

        mock_client = self._create_mock_s3_client()
        data_cache = self._create_s3_data_cache(mock_client)

        result = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            max_memory_bytes=1024 * 1024,
        )

        # File should have chunkhashes, not hash
        assert result.files[0].hash is None
        assert result.files[0].chunkhashes is not None
        assert len(result.files[0].chunkhashes) == 16  # 1024 / 64 = 16 chunks

        # Verify each chunk was uploaded to S3
        uploaded = mock_client._uploaded_objects
        for chunk_hash in result.files[0].chunkhashes:
            s3_key = f"test-bucket/Data/{chunk_hash}.xxh128"
            assert s3_key in uploaded

    def test_hash_upload_multiple_files_with_chunks_s3(self, tmp_path: Path) -> None:
        """Test hash+upload multiple small files with small chunk sizes to S3 cache."""
        files_data = [
            ("small.bin", 64),  # 2 chunks with 32-byte chunk size
            ("medium.bin", 128),  # 4 chunks
            ("large.bin", 256),  # 8 chunks
        ]

        file_entries = []
        file_contents: Dict[str, bytes] = {}
        for filename, size in files_data:
            test_file = tmp_path / filename
            content = bytes([i % 256 for i in range(size)])
            test_file.write_bytes(content)
            abs_path = str(test_file).replace("\\", "/")
            file_contents[abs_path] = content
            file_entries.append(
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=size,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            )

        chunk_size = 32
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=file_entries,
            total_size=sum(size for _, size in files_data),
            file_chunk_size_bytes=chunk_size,
        )

        mock_client = self._create_mock_s3_client()
        data_cache = self._create_s3_data_cache(mock_client)

        result = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            max_memory_bytes=1024 * 1024,
        )

        # Verify each file has chunkhashes and chunks were uploaded
        uploaded = mock_client._uploaded_objects
        for i, (filename, size) in enumerate(files_data):
            expected_chunks = size // chunk_size
            assert result.files[i].hash is None
            chunkhashes = result.files[i].chunkhashes
            assert chunkhashes is not None
            assert len(chunkhashes) == expected_chunks

            for chunk_hash in chunkhashes:
                s3_key = f"test-bucket/Data/{chunk_hash}.xxh128"
                assert s3_key in uploaded

    def test_hash_upload_idempotent_s3(self, tmp_path: Path) -> None:
        """Test that re-uploading same file uses conditional writes to avoid duplicates."""
        test_file = tmp_path / "file.bin"
        file_size = 256

        content = bytes(range(256))
        test_file.write_bytes(content)
        abs_path = str(test_file).replace("\\", "/")

        # Use 64-byte chunks (4 chunks total)
        chunk_size = 64
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=file_size,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
            file_chunk_size_bytes=chunk_size,
        )

        mock_client = self._create_mock_s3_client()
        data_cache = self._create_s3_data_cache(mock_client)

        # First upload
        result1 = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            max_memory_bytes=1024 * 1024,
        )

        # Count put_object calls
        first_upload_calls = mock_client.put_object.call_count

        # Second upload (same file) - should use conditional writes
        result2 = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            max_memory_bytes=1024 * 1024,
        )

        # Should have same chunkhashes
        assert result1.files[0].chunkhashes == result2.files[0].chunkhashes

        # Second upload should also call put_object (with IfNoneMatch),
        # but the mock will raise PreconditionFailed for existing objects
        second_upload_calls = mock_client.put_object.call_count - first_upload_calls
        assert second_upload_calls == 4  # Still attempts 4 uploads (one per chunk)

    def test_hash_upload_deduplication_s3(self, tmp_path: Path) -> None:
        """Test that identical chunks are deduplicated in S3."""
        # Create two files with identical content
        content = bytes(range(128))
        file1 = tmp_path / "file1.bin"
        file2 = tmp_path / "file2.bin"
        file1.write_bytes(content)
        file2.write_bytes(content)

        abs_path1 = str(file1).replace("\\", "/")
        abs_path2 = str(file2).replace("\\", "/")

        # Use 32-byte chunks (4 chunks per file)
        chunk_size = 32
        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path1,
                    hash=None,
                    size=128,
                    mtime=int(file1.stat().st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=abs_path2,
                    hash=None,
                    size=128,
                    mtime=int(file2.stat().st_mtime_ns // 1000),
                ),
            ],
            total_size=256,
            file_chunk_size_bytes=chunk_size,
        )

        mock_client = self._create_mock_s3_client()
        data_cache = self._create_s3_data_cache(mock_client)

        result = hash_upload_manifest(
            manifest=input_manifest,
            data_cache=data_cache,
            max_memory_bytes=1024 * 1024,
        )

        # Both files should have the same chunkhashes
        assert result.files[0].chunkhashes == result.files[1].chunkhashes

        # Only 4 unique chunks should be in S3 (not 8)
        uploaded = mock_client._uploaded_objects
        assert len(uploaded) == 4
