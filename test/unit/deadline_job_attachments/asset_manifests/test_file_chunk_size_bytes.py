# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for file_chunk_size_bytes parameter in COLLECT, HASH, and HASH_UPLOAD operations.

These tests verify:
- Default behavior (None means "not provided", preserves input manifest value)
- WHOLE_FILE_CHUNK_SIZE (-1) means "no chunking, hash whole file"
- Explicit positive chunk size values are used in output manifest
- Chunk size affects hashing behavior (chunked vs whole-file)
"""

from pathlib import Path

from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
    hash_manifest,
    hash_upload_manifest,
)
from deadline.job_attachments.asset_manifests._operations._content_addressed_data_cache import (
    FileSystemDataCache,
)
from deadline.job_attachments.asset_manifests.manifest import (
    AbsSnapshotManifest,
    ManifestFilePath,
    WHOLE_FILE_CHUNK_SIZE,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.versions import SymlinkPolicy


class TestCollectManifestFileChunkSizeBytes:
    """Tests for file_chunk_size_bytes parameter in collect_manifest."""

    def test_default_chunk_size_when_not_specified(self, tmp_path: Path) -> None:
        """When file_chunk_size_bytes is not specified, uses None (not provided)."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_manifest(
            directories=[tmp_path],
            filenames=[],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # Default is None (not provided - downstream operations apply default)
        assert manifest.fileChunkSizeBytes is None

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
            file_chunk_size_bytes=None,  # No chunking
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
            file_chunk_size_bytes=None,  # No chunking for input
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
