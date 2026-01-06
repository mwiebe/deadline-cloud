# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_upload_manifest pipeline internals.

These tests cover:
- _MemoryPool class
- _ChunkWorkItem dataclass
- _StreamingWorkItem dataclass
- Streaming/large file processing
- Chunked file processing with small chunk sizes
- Pipeline stages (_ReadStage, _HashStage, _UploadStage)
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional, Set

import boto3
import pytest

from deadline.job_attachments.asset_manifests._operations import (
    hash_upload_manifest,
    FileSystemDataCache,
    S3DataCache,
)
from deadline.job_attachments.asset_manifests._operations._hash_upload_manifest import (
    _ChunkWorkItem,
    _MemoryPool,
    _StreamingWorkItem,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.manifest import (
    AbsSnapshotManifest,
    ManifestFilePath,
)
from deadline.job_attachments.caches.s3_check_cache import S3CheckCache


TEST_BUCKET = "test-hash-upload-bucket"
TEST_KEY_PREFIX = "Data"


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
        pool = _MemoryPool(max_bytes=100)
        pool.allocate(80)

        allocation_complete = threading.Event()

        def allocate_more() -> None:
            pool.allocate(50)
            allocation_complete.set()

        thread = threading.Thread(target=allocate_more)
        thread.start()

        # Give the thread time to start and block
        time.sleep(0.1)
        assert not allocation_complete.is_set()

        # Release memory to unblock
        pool.release(50)
        thread.join(timeout=1.0)
        assert allocation_complete.is_set()

    def test_multiple_allocations(self) -> None:
        """Test multiple sequential allocations."""
        pool = _MemoryPool(max_bytes=1000)
        pool.allocate(200)
        pool.allocate(300)
        pool.allocate(100)
        assert pool.allocated == 600

    def test_release_all_memory(self) -> None:
        """Test releasing all allocated memory."""
        pool = _MemoryPool(max_bytes=1000)
        pool.allocate(500)
        pool.release(500)
        assert pool.allocated == 0

    def test_allocate_exact_limit(self) -> None:
        """Test allocation up to exact limit."""
        pool = _MemoryPool(max_bytes=1000)
        pool.allocate(1000)
        assert pool.allocated == 1000


class TestChunkWorkItem:
    """Tests for the _ChunkWorkItem dataclass."""

    def test_create_work_item(self) -> None:
        """Test creating a chunk work item."""
        item = _ChunkWorkItem(
            file_path=Path("/test/file.txt"),
            cache_key="/test/file.txt",
            file_size=1000,
            mtime=12345,
            chunk_index=0,
            chunk_start=0,
            chunk_end=1000,
        )
        assert item.file_path == Path("/test/file.txt")
        assert item.cache_key == "/test/file.txt"
        assert item.file_size == 1000
        assert item.chunk_index == 0
        assert item.data is None
        assert item.chunk_hash is None

    def test_work_item_defaults(self) -> None:
        """Test that work item has correct defaults."""
        item = _ChunkWorkItem(
            file_path=Path("/test/file.txt"),
            cache_key="/test/file.txt",
            file_size=1000,
            mtime=12345,
            chunk_index=0,
            chunk_start=0,
            chunk_end=1000,
        )
        assert item.data is None
        assert item.chunk_hash is None
        assert item.uploaded is False
        assert item.skipped is False

    def test_work_item_with_data(self) -> None:
        """Test work item with data populated."""
        item = _ChunkWorkItem(
            file_path=Path("/test/file.txt"),
            cache_key="/test/file.txt",
            file_size=1000,
            mtime=12345,
            chunk_index=0,
            chunk_start=0,
            chunk_end=1000,
            data=b"test data",
        )
        assert item.data == b"test data"

    def test_work_item_chunk_range(self) -> None:
        """Test work item with specific chunk range."""
        item = _ChunkWorkItem(
            file_path=Path("/test/file.txt"),
            cache_key="/test/file.txt",
            file_size=3000,
            mtime=12345,
            chunk_index=1,
            chunk_start=1000,
            chunk_end=2000,
        )
        assert item.chunk_index == 1
        assert item.chunk_start == 1000
        assert item.chunk_end == 2000


class TestStreamingWorkItem:
    """Tests for the _StreamingWorkItem dataclass."""

    def test_create_streaming_work_item(self) -> None:
        """Test creating a streaming work item."""
        item = _StreamingWorkItem(
            file_path=Path("/test/large_file.bin"),
            cache_key="/test/large_file.bin",
            file_size=1000000,
            mtime=12345,
        )
        assert item.file_path == Path("/test/large_file.bin")
        assert item.cache_key == "/test/large_file.bin"
        assert item.file_size == 1000000
        assert item.mtime == 12345
        assert item.file_hash is None
        assert item.uploaded is False
        assert item.skipped is False

    def test_streaming_work_item_with_hash(self) -> None:
        """Test streaming work item with hash populated."""
        item = _StreamingWorkItem(
            file_path=Path("/test/large_file.bin"),
            cache_key="/test/large_file.bin",
            file_size=1000000,
            mtime=12345,
            file_hash="abc123def456",
        )
        assert item.file_hash == "abc123def456"


class TestChunkedFileProcessingFileSystem:
    """Tests for chunked file processing with FileSystemDataCache using small chunk sizes."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def _get_cache_files(self, cache_root: Path) -> Set[str]:
        """Get all file names in the cache directory."""
        return {f.name for f in cache_root.iterdir() if f.is_file()}

    def test_file_larger_than_chunk_size_produces_chunkhashes(self, tmp_path: Path) -> None:
        """File larger than chunk size produces chunkhashes instead of hash."""
        cache_root = tmp_path / "cache"

        # Create a 64-byte file, use 16-byte chunks -> 4 chunks
        test_file = tmp_path / "chunked.bin"
        test_file.write_bytes(bytes(range(64)))
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=64,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=64,
            file_chunk_size_bytes=16,  # Small chunk size
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=64,  # Small memory limit
        )

        assert len(result.files) == 1
        assert result.files[0].hash is None  # No single hash
        assert result.files[0].chunkhashes is not None
        assert len(result.files[0].chunkhashes) == 4  # 64 / 16 = 4 chunks

        # Each chunk should have a unique hash (different content)
        assert len(set(result.files[0].chunkhashes)) == 4

        # 4 chunk files should be in cache
        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 4

    def test_multiple_chunked_files(self, tmp_path: Path) -> None:
        """Multiple files larger than chunk size all produce chunkhashes."""
        cache_root = tmp_path / "cache"

        # Create two 48-byte files, use 16-byte chunks -> 3 chunks each
        file1 = tmp_path / "file1.bin"
        file1.write_bytes(b"a" * 48)
        file2 = tmp_path / "file2.bin"
        file2.write_bytes(b"b" * 48)

        file1_stat = file1.stat()
        file2_stat = file2.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(file1).replace("\\", "/"),
                    hash=None,
                    size=48,
                    mtime=int(file1_stat.st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=str(file2).replace("\\", "/"),
                    hash=None,
                    size=48,
                    mtime=int(file2_stat.st_mtime_ns // 1000),
                ),
            ],
            total_size=96,
            file_chunk_size_bytes=16,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=64,
        )

        assert len(result.files) == 2
        for entry in result.files:
            assert entry.hash is None
            assert entry.chunkhashes is not None
            assert len(entry.chunkhashes) == 3

        # file1 has identical chunks (all 'a'), file2 has identical chunks (all 'b')
        # So we should have 2 unique chunk files (one for 'a' chunks, one for 'b' chunks)
        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 2

    def test_mixed_small_and_chunked_files(self, tmp_path: Path) -> None:
        """Mix of small files (single hash) and large files (chunkhashes)."""
        cache_root = tmp_path / "cache"

        # Small file (10 bytes < 16 byte chunk) -> single hash
        small_file = tmp_path / "small.txt"
        small_file.write_bytes(b"small file")

        # Large file (48 bytes > 16 byte chunk) -> chunkhashes
        large_file = tmp_path / "large.bin"
        large_file.write_bytes(bytes(range(48)))

        small_stat = small_file.stat()
        large_stat = large_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(small_file).replace("\\", "/"),
                    hash=None,
                    size=10,
                    mtime=int(small_stat.st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=str(large_file).replace("\\", "/"),
                    hash=None,
                    size=48,
                    mtime=int(large_stat.st_mtime_ns // 1000),
                ),
            ],
            total_size=58,
            file_chunk_size_bytes=16,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=64,
        )

        assert len(result.files) == 2

        # Find entries by path
        small_entry = next(e for e in result.files if "small" in e.path)
        large_entry = next(e for e in result.files if "large" in e.path)

        # Small file has single hash
        assert small_entry.hash is not None
        assert small_entry.chunkhashes is None

        # Large file has chunkhashes
        assert large_entry.hash is None
        assert large_entry.chunkhashes is not None
        assert len(large_entry.chunkhashes) == 3

    def test_chunk_size_boundary(self, tmp_path: Path) -> None:
        """File exactly at chunk size boundary uses single hash."""
        cache_root = tmp_path / "cache"

        # File exactly 16 bytes = chunk size -> single hash (not chunked)
        test_file = tmp_path / "exact.bin"
        test_file.write_bytes(b"x" * 16)
        file_stat = test_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=16,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=16,
            file_chunk_size_bytes=16,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=64,
        )

        # File at exactly chunk size is NOT chunked (size must be > chunk_size)
        assert result.files[0].hash is not None
        assert result.files[0].chunkhashes is None

    def test_file_one_byte_over_chunk_size(self, tmp_path: Path) -> None:
        """File one byte over chunk size produces chunkhashes."""
        cache_root = tmp_path / "cache"

        # File 17 bytes > 16 byte chunk -> 2 chunks
        test_file = tmp_path / "over.bin"
        test_file.write_bytes(b"x" * 17)
        file_stat = test_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=17,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=17,
            file_chunk_size_bytes=16,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=64,
        )

        assert result.files[0].hash is None
        assert result.files[0].chunkhashes is not None
        assert len(result.files[0].chunkhashes) == 2  # 16 + 1 = 2 chunks


class TestStreamingFileProcessingFileSystem:
    """Tests for streaming file processing (files larger than max_memory_bytes)."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def _get_cache_files(self, cache_root: Path) -> Set[str]:
        """Get all file names in the cache directory."""
        return {f.name for f in cache_root.iterdir() if f.is_file()}

    def test_file_larger_than_memory_uses_streaming(self, tmp_path: Path) -> None:
        """File larger than max_memory_bytes uses streaming (whole file hash)."""
        cache_root = tmp_path / "cache"

        # Create 100-byte file with max_memory=32 and no chunking (WHOLE_FILE_CHUNK_SIZE)
        # This forces streaming mode
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(bytes(range(100)))
        file_stat = test_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=100,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=100,
            file_chunk_size_bytes=-1,  # WHOLE_FILE_CHUNK_SIZE - no chunking
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=32,  # Smaller than file size
        )

        assert len(result.files) == 1
        assert result.files[0].hash is not None
        assert result.files[0].chunkhashes is None

        # File should be in cache
        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 1

        # Verify content was correctly copied
        cached_file = cache_root / f"{result.files[0].hash}.xxh128"
        assert cached_file.read_bytes() == test_file.read_bytes()

    def test_multiple_streaming_files(self, tmp_path: Path) -> None:
        """Multiple files larger than max_memory_bytes all use streaming."""
        cache_root = tmp_path / "cache"

        # Create two files larger than max_memory
        file1 = tmp_path / "large1.bin"
        file1.write_bytes(b"a" * 50)
        file2 = tmp_path / "large2.bin"
        file2.write_bytes(b"b" * 60)

        file1_stat = file1.stat()
        file2_stat = file2.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(file1).replace("\\", "/"),
                    hash=None,
                    size=50,
                    mtime=int(file1_stat.st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=str(file2).replace("\\", "/"),
                    hash=None,
                    size=60,
                    mtime=int(file2_stat.st_mtime_ns // 1000),
                ),
            ],
            total_size=110,
            file_chunk_size_bytes=-1,  # No chunking
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=32,
        )

        assert len(result.files) == 2
        for entry in result.files:
            assert entry.hash is not None
            assert entry.chunkhashes is None

        # Both files should be in cache
        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 2

    def test_mixed_small_and_streaming_files(self, tmp_path: Path) -> None:
        """Mix of small files (in-memory) and large files (streaming)."""
        cache_root = tmp_path / "cache"

        # Small file (20 bytes < 32 max_memory) -> in-memory
        small_file = tmp_path / "small.txt"
        small_file.write_bytes(b"small content here!")

        # Large file (50 bytes > 32 max_memory) -> streaming
        large_file = tmp_path / "large.bin"
        large_file.write_bytes(bytes(range(50)))

        small_stat = small_file.stat()
        large_stat = large_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(small_file).replace("\\", "/"),
                    hash=None,
                    size=19,
                    mtime=int(small_stat.st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=str(large_file).replace("\\", "/"),
                    hash=None,
                    size=50,
                    mtime=int(large_stat.st_mtime_ns // 1000),
                ),
            ],
            total_size=69,
            file_chunk_size_bytes=-1,  # No chunking
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=32,
        )

        assert len(result.files) == 2
        for entry in result.files:
            assert entry.hash is not None
            assert entry.chunkhashes is None

        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 2


class TestChunkedFileProcessingS3:
    """Tests for chunked file processing with S3DataCache using small chunk sizes."""

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

    def test_file_larger_than_chunk_size_produces_chunkhashes(self, tmp_path: Path) -> None:
        """File larger than chunk size produces chunkhashes (S3)."""
        # Create a 64-byte file, use 16-byte chunks -> 4 chunks
        test_file = tmp_path / "chunked.bin"
        test_file.write_bytes(bytes(range(64)))
        file_stat = test_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=64,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=64,
            file_chunk_size_bytes=16,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=64,
        )

        assert len(result.files) == 1
        assert result.files[0].hash is None
        assert result.files[0].chunkhashes is not None
        assert len(result.files[0].chunkhashes) == 4

        # 4 chunk objects should be in S3
        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 4

    def test_duplicate_chunks_uploaded_once(self, tmp_path: Path) -> None:
        """Identical chunks are only uploaded once (content-addressable)."""
        # Create file with identical chunks
        test_file = tmp_path / "repeated.bin"
        test_file.write_bytes(b"x" * 64)  # 4 identical 16-byte chunks
        file_stat = test_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=64,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=64,
            file_chunk_size_bytes=16,
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=64,
        )

        assert result.files[0].chunkhashes is not None
        assert len(result.files[0].chunkhashes) == 4

        # All chunks have same hash
        assert len(set(result.files[0].chunkhashes)) == 1

        # Only 1 object in S3 (content-addressable)
        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 1


class TestStreamingFileProcessingS3:
    """Tests for streaming file processing with S3DataCache."""

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

    def _create_s3_data_cache(self) -> S3DataCache:
        """Create an S3DataCache for testing."""
        return S3DataCache(
            s3_bucket=TEST_BUCKET,
            s3_key_prefix=TEST_KEY_PREFIX,
            s3_client=self.s3_client,
        )

    def test_file_larger_than_memory_uses_streaming(self, tmp_path: Path) -> None:
        """File larger than max_memory_bytes uses streaming (S3)."""
        # Create 100-byte file with max_memory=32 and no chunking
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(bytes(range(100)))
        file_stat = test_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=100,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=100,
            file_chunk_size_bytes=-1,  # No chunking
        )

        data_cache = self._create_s3_data_cache()
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=32,
        )

        assert len(result.files) == 1
        assert result.files[0].hash is not None
        assert result.files[0].chunkhashes is None

        # File should be in S3
        s3_objects = self._get_s3_objects()
        assert len(s3_objects) == 1

        # Verify content
        s3_key = f"{TEST_KEY_PREFIX}/{result.files[0].hash}.xxh128"
        uploaded_content = self._get_s3_object_content(s3_key)
        assert uploaded_content == test_file.read_bytes()


class TestMemoryLimitValidation:
    """Tests for memory limit validation."""

    def test_rejects_memory_less_than_chunk_size(self, tmp_path: Path) -> None:
        """Error when max_memory_bytes < file_chunk_size_bytes."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()

        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"x" * 100)
        file_stat = test_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=100,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=100,
            file_chunk_size_bytes=64,  # Chunk size
        )

        data_cache = FileSystemDataCache(root_path=cache_root)

        with pytest.raises(ValueError, match="max_memory_bytes.*must be >= fileChunkSizeBytes"):
            hash_upload_manifest(
                manifest=manifest,
                data_cache=data_cache,
                max_memory_bytes=32,  # Less than chunk size
            )

    def test_accepts_memory_equal_to_chunk_size(self, tmp_path: Path) -> None:
        """Accepts max_memory_bytes == file_chunk_size_bytes."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"x" * 32)
        file_stat = test_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=32,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=32,
            file_chunk_size_bytes=32,
        )

        data_cache = FileSystemDataCache(root_path=cache_root)

        # Should not raise
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=32,  # Equal to chunk size
        )
        assert result.files[0].hash is not None
