# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for filling in hashes AND uploading file content to a data cache in a pipelined manner.

This module implements the HASH_UPLOAD operation from the composable manifest operations design:
    HASH_UPLOAD: (AbsManifest, DataCache) → AbsManifest (with hashes filled in) + data cache writes

The operation combines hashing and uploading into a single pass over the data:
- Reads file chunks into memory while computing the hash as bytes stream in
- Writes to data cache before freeing memory

This avoids reading files twice (once for hashing, once for uploading) and provides
significant performance improvements for large datasets.

The pipeline uses separate thread pools for READ+HASH and UPLOAD stages to prevent
deadlock. READ+HASH tasks block on memory allocation, while UPLOAD tasks run
independently to release memory.

Architecture:
- READ+HASH pool: reads files and computes hashes, blocks waiting for memory
- UPLOAD pool: writes to data cache and releases memory
- Memory pool bounds total in-flight data; READ+HASH blocks when memory is exhausted
- Separate pools ensure UPLOAD can always run to release memory

All composable operations use v2025 structure and semantics internally. Support for
v2023 on-disk format is provided via lossy conversion functions that drop symlinks,
deletions, and other v2025-only features.
"""

from __future__ import annotations

import concurrent.futures
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import logging

from botocore.exceptions import BotoCoreError, ClientError

from .._manifest import (
    AbsManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    _is_absolute_path,
)
from ...asset_manifests.hash_algorithms import HashAlgorithm, hash_data
from ...caches.hash_cache import HashCache, HashCacheEntry, WHOLE_FILE_RANGE_END
from ...caches.s3_check_cache import S3CheckCacheEntry
from ...progress_tracker import ProgressTracker
from ...exceptions import (
    JobAttachmentsS3ClientError,
    JobAttachmentS3BotoCoreError,
    COMMON_ERROR_GUIDANCE_FOR_S3,
)
from .._content_addressed_data_cache import (
    ContentAddressedDataCache,
    S3DataCache,
    FileSystemDataCache,
)
from ...progress_tracker import SummaryStatistics

logger = logging.getLogger("deadline.job_attachments.hash_upload")

# Minimum memory limit: 256MB (one chunk)
MIN_MEMORY_BYTES = 256 * 1024 * 1024

# Default read buffer size for streaming hash (when fileChunkSizeBytes is WHOLE_FILE_CHUNK_SIZE)
DEFAULT_STREAM_BUFFER_SIZE = 64 * 1024 * 1024  # 64MB

# Default number of parallel workers
DEFAULT_MAX_WORKERS = 10


@dataclass
class UploadResult:
    """
    Result of a hash_upload_manifest operation.

    Attributes:
        statistics: Summary statistics about the upload operation including
            processed/skipped files and bytes.
        manifest: The manifest with all hashes filled in.
    """

    statistics: SummaryStatistics
    manifest: AbsManifest


def _validate_absolute_paths(manifest: AbsManifest) -> None:
    """Validate that all paths in the manifest are absolute."""
    for entry in manifest.files:
        if not _is_absolute_path(entry.path):
            raise ValueError(
                f"HASH_UPLOAD operation requires absolute paths. "
                f"Found relative path: '{entry.path}'. "
                f"Use collect_abs_snapshot() or join_manifest() to create a manifest with absolute paths."
            )

    for d in manifest.dirs:
        if not _is_absolute_path(d.path):
            raise ValueError(
                f"HASH_UPLOAD operation requires absolute paths. "
                f"Found relative directory path: '{d.path}'. "
                f"Use collect_abs_snapshot() or join_manifest() to create a manifest with absolute paths."
            )


def _get_default_max_memory_bytes() -> int:
    """
    Calculate the default memory limit for the pipeline.

    Returns the maximum of:
    1. 256MB (one chunk) - minimum to process at least one chunk at a time
    2. One quarter of total system memory - use a reasonable portion of system resources
    3. Available memory - 1GB - use most of free memory when lots is available

    This ensures we use as much memory as safely available while maintaining
    a reasonable lower bound.
    """
    import psutil

    try:
        mem = psutil.virtual_memory()
        total_memory = mem.total
        available_memory = mem.available

        quarter_of_total = total_memory // 4
        available_minus_1gb = available_memory - (1024 * 1024 * 1024)

        result = max(MIN_MEMORY_BYTES, quarter_of_total, available_minus_1gb)
        logger.debug(
            f"Memory limit calculation: min=256MB, quarter_total={quarter_of_total // (1024 * 1024)}MB, "
            f"available-1GB={available_minus_1gb // (1024 * 1024)}MB, result={result // (1024 * 1024)}MB"
        )
        return result
    except Exception as e:
        logger.warning(f"Failed to detect system memory, using minimum: {e}")
        return MIN_MEMORY_BYTES


@dataclass
class _ChunkWorkItem:
    """Work item representing a chunk to be processed through the pipeline."""

    # File identification
    file_path: Path  # Absolute path to the file
    cache_key: str  # Cache key (resolved absolute path) for hash cache lookups
    file_size: int  # Total file size
    mtime: int  # File modification time in microseconds

    # Chunk identification
    chunk_index: int  # 0-based chunk index
    chunk_start: int  # Byte offset where chunk starts
    chunk_end: int  # Byte offset where chunk ends (exclusive)

    # Data (populated by READ stage)
    data: Optional[bytes] = None

    # Hash result (populated by HASH stage)
    chunk_hash: Optional[str] = None

    # Upload status (set by UPLOAD stage)
    uploaded: bool = False
    skipped: bool = False  # True if already in S3


@dataclass
class _StreamingWorkItem:
    """
    Work item for large files that don't fit in memory.

    These files are processed with a two-pass approach:
    - Pass 1: Stream through file to compute hash (discard data)
    - Pass 2: Stream through file again to upload

    The streaming is done within the pipeline stages, not by loading
    the entire file into memory.
    """

    # File identification
    file_path: Path  # Absolute path to the file
    cache_key: str  # Cache key (resolved absolute path) for hash cache lookups
    file_size: int  # Total file size
    mtime: int  # File modification time in microseconds

    # Hash result (populated by HASH stage via streaming)
    file_hash: Optional[str] = None

    # Upload status (set by UPLOAD stage)
    uploaded: bool = False
    skipped: bool = False  # True if already in data cache


# Union type for work items
WorkItem = Union[_ChunkWorkItem, _StreamingWorkItem]


@dataclass
class _FileResult:
    """Result of processing a single file."""

    cache_key: str
    file_hash: Optional[str]  # For small files or streaming files
    chunkhashes: Optional[List[str]]  # For chunked files
    size: int
    mtime: int
    runnable: bool
    uploaded_bytes: int
    skipped_bytes: int


class _MemoryPool:
    """
    Thread-safe memory pool that tracks allocated memory and blocks when limit is reached.

    This ensures the pipeline doesn't exceed the configured memory limit by blocking
    the READ stage when too much data is in flight.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._allocated_bytes = 0
        self._lock = threading.Lock()
        self._space_available = threading.Condition(self._lock)

    def allocate(self, size: int) -> None:
        """
        Allocate memory from the pool, blocking if necessary.

        Args:
            size: Number of bytes to allocate
        """
        with self._space_available:
            while self._allocated_bytes + size > self._max_bytes:
                self._space_available.wait()
            self._allocated_bytes += size

    def release(self, size: int) -> None:
        """
        Release memory back to the pool.

        Args:
            size: Number of bytes to release
        """
        with self._space_available:
            self._allocated_bytes -= size
            self._space_available.notify_all()

    @property
    def allocated(self) -> int:
        """Current allocated bytes."""
        with self._lock:
            return self._allocated_bytes


class _TaskBasedPipeline:
    """
    Task-based pipeline using separate thread pools for READ+HASH and UPLOAD stages.

    Using separate pools prevents deadlock: READ+HASH tasks can block waiting for
    memory while UPLOAD tasks run independently to release that memory.

    Flow: READ+HASH task (pool 1) → UPLOAD task (pool 2) → completion

    Cache checks (hash cache, S3 check cache, HeadObject) are performed in the
    READ+HASH worker threads to parallelize S3 API calls.
    """

    def __init__(
        self,
        read_hash_executor: concurrent.futures.ThreadPoolExecutor,
        upload_executor: concurrent.futures.ThreadPoolExecutor,
        memory_pool: _MemoryPool,
        hash_alg: HashAlgorithm,
        data_cache: ContentAddressedDataCache,
        account_id: Optional[str],
        progress_tracker: Optional[ProgressTracker],
        hash_cache: Optional[HashCache] = None,
        force_rehash: bool = False,
    ) -> None:
        self._read_hash_executor = read_hash_executor
        self._upload_executor = upload_executor
        self._memory_pool = memory_pool
        self._hash_alg = hash_alg
        self._data_cache = data_cache
        self._account_id = account_id
        self._progress_tracker = progress_tracker
        self._hash_cache = hash_cache
        self._force_rehash = force_rehash

        # Track completion
        self._pending_count = 0
        self._lock = threading.Lock()
        self._done_event = threading.Event()

        # Collect results
        self._results: List[WorkItem] = []
        self._results_lock = threading.Lock()

        # Track errors
        self._error: Optional[Exception] = None
        self._error_lock = threading.Lock()

        # Per-hash locks for filesystem writes to prevent race conditions
        # when multiple chunks with the same hash are uploaded concurrently
        self._fs_write_locks: Dict[str, threading.Lock] = {}
        self._fs_write_locks_lock = threading.Lock()

    def submit(self, item: WorkItem) -> None:
        """Submit a work item to start processing through the pipeline."""
        with self._lock:
            self._pending_count += 1
        # Start with combined READ+HASH stage in the read_hash pool
        self._read_hash_executor.submit(self._do_read_and_hash, item)

    def wait_for_completion(self) -> List[WorkItem]:
        """Wait for all submitted items to complete and return results."""
        self._done_event.wait()

        # Check for errors
        with self._error_lock:
            if self._error is not None:
                raise self._error

        with self._results_lock:
            return list(self._results)

    def _decrement_pending(self) -> None:
        """Decrement pending count and signal completion if done."""
        with self._lock:
            self._pending_count -= 1
            if self._pending_count == 0:
                self._done_event.set()

    def _record_error(self, error: Exception) -> None:
        """Record an error (first error wins)."""
        with self._error_lock:
            if self._error is None:
                self._error = error
                logger.exception(f"Pipeline error: {error}")
        # Signal completion so wait doesn't hang
        self._done_event.set()

    def _record_result(self, item: WorkItem) -> None:
        """Record a completed work item."""
        with self._results_lock:
            self._results.append(item)

    def _get_fs_write_lock(self, hash_key: str) -> threading.Lock:
        """Get or create a lock for a specific hash to prevent concurrent writes."""
        with self._fs_write_locks_lock:
            if hash_key not in self._fs_write_locks:
                self._fs_write_locks[hash_key] = threading.Lock()
            return self._fs_write_locks[hash_key]

    # =========================================================================
    # READ+HASH Stage (Combined for CPU cache efficiency)
    # =========================================================================

    def _check_cache_and_skip(self, item: WorkItem) -> tuple[Optional[str], bool]:
        """
        Check hash cache and data cache to see if this item can be skipped.

        Returns (cached_hash, can_skip):
        - (hash, True): Hash found and object exists in data cache - skip entirely
        - (hash, False): Hash found but object not in data cache - need to verify
        - (None, False): No cached hash - need to read and hash
        
        When can_skip is False but cached_hash is not None, the caller should
        compare the computed hash with cached_hash. If they match, no need to
        re-check HeadObject. If they differ, should check HeadObject with new hash.
        
        This is called from worker threads to parallelize cache checks.
        """
        if self._hash_cache is None or self._force_rehash:
            return None, False

        mtime_str = str(item.mtime) if item.mtime is not None else ""

        # Get hash cache entry (uses thread-local SQLite connection)
        if isinstance(item, _StreamingWorkItem):
            hash_cache_entry = self._hash_cache.get_entry(
                item.cache_key,
                self._hash_alg,
            )
        else:
            # _ChunkWorkItem
            if item.chunk_start == 0 and item.chunk_end == item.file_size:
                # Whole file (single chunk)
                hash_cache_entry = self._hash_cache.get_entry(
                    item.cache_key,
                    self._hash_alg,
                )
            else:
                # Chunked file
                hash_cache_entry = self._hash_cache.get_entry(
                    item.cache_key,
                    self._hash_alg,
                    item.chunk_start,
                    item.chunk_end,
                )

        if hash_cache_entry is None or hash_cache_entry.last_modified_time != mtime_str:
            return None, False

        cached_hash = hash_cache_entry.file_hash

        # Check if object exists in data cache (may call HeadObject for S3)
        if self._data_cache.object_exists(cached_hash, self._hash_alg.value):
            return cached_hash, True  # Can skip entirely

        return cached_hash, False  # Have cached hash but need to verify

    def _do_read_and_hash(self, item: WorkItem) -> None:
        """
        Combined READ+HASH stage: Read file data and compute hash while bytes stream into buffer.

        First checks caches to skip items that are already uploaded. Cache checks are done
        in worker threads to parallelize S3 HeadObject calls.

        For _ChunkWorkItem: allocate memory, read chunk data, hash as bytes stream in, submit UPLOAD task.
        For _StreamingWorkItem: stream through file computing hash as we read, submit UPLOAD task.

        Combining read and hash improves performance by computing the hash while bytes
        are streaming into the memory buffer, rather than re-processing the buffer
        in a separate stage.
        """
        try:
            # Check for prior error - don't start new work
            with self._error_lock:
                if self._error is not None:
                    self._decrement_pending()
                    return

            # Check caches first (before allocating memory)
            # This parallelizes HeadObject calls across worker threads
            cached_hash, can_skip = self._check_cache_and_skip(item)
            if can_skip:
                # Item is fully cached - mark as skipped and record result
                if isinstance(item, _StreamingWorkItem):
                    item.file_hash = cached_hash
                else:
                    item.chunk_hash = cached_hash
                item.skipped = True
                item.uploaded = False
                # Track progress for skipped items
                if self._progress_tracker is not None:
                    if isinstance(item, _StreamingWorkItem):
                        self._progress_tracker.track_progress_callback(item.file_size)
                    else:
                        self._progress_tracker.track_progress_callback(item.chunk_end - item.chunk_start)
                self._record_result(item)
                self._decrement_pending()
                logger.debug(f"Skipped (cache hit): {item.file_path}")
                return

            if isinstance(item, _StreamingWorkItem):
                # Streaming items compute hash while reading (already combined)
                item.file_hash = self._stream_hash_file(item.file_path)
                
                # If hash changed from cached, check if new hash exists in S3
                if cached_hash is not None and item.file_hash != cached_hash:
                    if self._data_cache.object_exists(item.file_hash, self._hash_alg.value):
                        item.skipped = True
                        item.uploaded = False
                        if self._progress_tracker is not None:
                            self._progress_tracker.track_progress_callback(item.file_size)
                        self._record_result(item)
                        self._decrement_pending()
                        logger.debug(f"Skipped (hash changed, but exists): {item.file_path}")
                        return
                
                # Submit to UPLOAD stage in the upload pool
                self._upload_executor.submit(self._do_upload, item)
                return

            # _ChunkWorkItem: read chunk data and hash immediately
            chunk_size = item.chunk_end - item.chunk_start

            # Block until we have memory available (backpressure)
            self._memory_pool.allocate(chunk_size)

            try:
                with open(item.file_path, "rb") as f:
                    f.seek(item.chunk_start)
                    item.data = f.read(chunk_size)

                # Hash while data is fresh in memory
                if item.data is not None:
                    item.chunk_hash = hash_data(item.data, self._hash_alg)
                    
                    # If hash changed from cached, check if new hash exists in S3
                    if cached_hash is not None and item.chunk_hash != cached_hash:
                        if self._data_cache.object_exists(item.chunk_hash, self._hash_alg.value):
                            # Release memory - we're not uploading
                            self._memory_pool.release(chunk_size)
                            item.data = None
                            item.skipped = True
                            item.uploaded = False
                            if self._progress_tracker is not None:
                                self._progress_tracker.track_progress_callback(chunk_size)
                            self._record_result(item)
                            self._decrement_pending()
                            logger.debug(f"Skipped (hash changed, but exists): {item.file_path}")
                            return
            except Exception:
                # Release memory on error
                self._memory_pool.release(chunk_size)
                raise

            # Submit to UPLOAD stage in the upload pool
            self._upload_executor.submit(self._do_upload, item)

        except Exception as e:
            self._record_error(e)
            self._decrement_pending()

    def _stream_hash_file(self, file_path: Path) -> str:
        """
        Compute hash of a file by streaming through it.

        Reads the file in chunks, updating the hash incrementally as each
        chunk streams in, and discards the data to avoid memory issues.
        """
        import xxhash

        if self._hash_alg != HashAlgorithm.XXH128:
            raise ValueError(f"Unsupported hash algorithm for streaming: {self._hash_alg}")

        # Use the data cache's part size if available, otherwise use default
        if isinstance(self._data_cache, S3DataCache):
            buffer_size = self._data_cache.multipart_part_size
        else:
            buffer_size = DEFAULT_STREAM_BUFFER_SIZE

        hasher = xxhash.xxh128()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(buffer_size)
                if not chunk:
                    break
                hasher.update(chunk)

        return hasher.hexdigest()

    # =========================================================================
    # UPLOAD Stage
    # =========================================================================

    def _do_upload(self, item: WorkItem) -> None:
        """
        UPLOAD stage: Write data to data cache.

        For _ChunkWorkItem: upload from memory, release memory, record result.
        For _StreamingWorkItem: stream file to data cache, record result.
        """
        try:
            # Check for prior error
            with self._error_lock:
                if self._error is not None:
                    if isinstance(item, _ChunkWorkItem) and item.data is not None:
                        self._memory_pool.release(len(item.data))
                    self._decrement_pending()
                    return

            if isinstance(item, _StreamingWorkItem):
                self._upload_streaming(item)
            else:
                self._upload_chunk(item)

            # Record successful result
            self._record_result(item)

        except Exception as e:
            # Release memory on error
            if isinstance(item, _ChunkWorkItem) and item.data is not None:
                self._memory_pool.release(len(item.data))
                item.data = None
            self._record_error(e)

        finally:
            self._decrement_pending()

    def _upload_chunk(self, item: _ChunkWorkItem) -> None:
        """Upload chunk data from memory."""
        if item.data is None or item.chunk_hash is None:
            return

        chunk_size = len(item.data)

        try:
            if isinstance(self._data_cache, S3DataCache):
                self._upload_chunk_to_s3(item)
            elif isinstance(self._data_cache, FileSystemDataCache):
                self._upload_chunk_to_filesystem(item)
            else:
                raise ValueError(f"Unsupported data cache type: {type(self._data_cache)}")

            # Update progress
            if self._progress_tracker is not None:
                self._progress_tracker.track_progress_callback(chunk_size)

        finally:
            # Release memory after write completes
            self._memory_pool.release(chunk_size)
            item.data = None  # Free the data

    def _upload_streaming(self, item: _StreamingWorkItem) -> None:
        """Stream file to data cache."""
        if item.file_hash is None:
            return

        # Check if already in data cache
        if self._data_cache.object_exists(item.file_hash, self._hash_alg.value):
            item.skipped = True
            item.uploaded = False
            logger.debug(f"Skipping streaming upload (exists): {item.file_path}")
            # Still report progress for skipped files
            if self._progress_tracker is not None:
                self._progress_tracker.track_progress_callback(item.file_size)
            return

        # Stream upload
        if isinstance(self._data_cache, S3DataCache):
            self._stream_upload_to_s3(item)
        elif isinstance(self._data_cache, FileSystemDataCache):
            self._stream_upload_to_filesystem(item)
        else:
            raise ValueError(f"Unsupported data cache type: {type(self._data_cache)}")

        item.uploaded = True
        item.skipped = False

    # =========================================================================
    # S3 Upload Methods
    # =========================================================================

    def _upload_chunk_to_s3(self, item: _ChunkWorkItem) -> None:
        """Upload chunk to S3."""
        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")
        if item.data is None:
            raise ValueError("Chunk data is None, cannot upload")
        if item.chunk_hash is None:
            raise ValueError("Chunk hash is None, cannot determine S3 key")

        s3_key = self._data_cache.get_object_key(item.chunk_hash, self._hash_alg.value)
        cache_key = f"{self._data_cache.s3_bucket}/{s3_key}"

        # Check S3 cache first
        if self._data_cache.s3_check_cache is not None:
            cache_entry = self._data_cache.s3_check_cache.get_entry(cache_key)
            if cache_entry is not None:
                item.skipped = True
                item.uploaded = False
                logger.debug(f"Skipping upload (cached): {s3_key}")
                return

        # Upload to S3 with conditional write (only if object doesn't exist)
        uploaded = self._upload_to_s3_if_not_exists(item.data, s3_key)
        item.uploaded = uploaded
        item.skipped = not uploaded
        if uploaded:
            logger.debug(f"Uploaded: {s3_key}")
        else:
            logger.debug(f"Skipping upload (exists): {s3_key}")

        # Update S3 check cache
        if self._data_cache.s3_check_cache is not None:
            self._data_cache.s3_check_cache.put_entry(
                S3CheckCacheEntry(s3_key=cache_key, last_seen_time=str(time.time()))
            )

    def _upload_to_s3_if_not_exists(self, data: bytes, s3_key: str) -> bool:
        """
        Upload data to S3 only if the object doesn't already exist.

        Uses HeadObject to check existence, then unconditional PutObject if needed.
        This is more efficient than IfNoneMatch='*' because HeadObject provides
        early rejection without requiring the request body to be sent.

        Returns:
            True if the object was uploaded, False if it already existed
        """
        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")

        # Check if object exists with HeadObject
        try:
            head_kwargs: Dict[str, Any] = {
                "Bucket": self._data_cache.s3_bucket,
                "Key": s3_key,
            }
            if self._account_id is not None:
                head_kwargs["ExpectedBucketOwner"] = self._account_id

            self._data_cache.s3_client.head_object(**head_kwargs)
            # Object exists, skip upload
            return False
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            if error_code != "404":
                status_code = int(exc.response["ResponseMetadata"]["HTTPStatusCode"])
                status_code_guidance = {
                    **COMMON_ERROR_GUIDANCE_FOR_S3,
                    403: (
                        "Forbidden or Access denied. Please check your AWS credentials, and ensure "
                        "that your AWS IAM Role or User has the 's3:HeadObject' permission."
                    ),
                }
                raise JobAttachmentsS3ClientError(
                    action="checking chunk existence",
                    status_code=status_code,
                    bucket_name=self._data_cache.s3_bucket,
                    key_or_prefix=s3_key,
                    message=f"{status_code_guidance.get(status_code, '')} {str(exc)}",
                ) from exc
            # 404 means object doesn't exist, proceed with upload
        except BotoCoreError as bce:
            raise JobAttachmentS3BotoCoreError(
                action="checking chunk existence",
                error_details=str(bce),
            ) from bce

        # Upload unconditionally since object doesn't exist
        try:
            put_kwargs: Dict[str, Any] = {
                "Bucket": self._data_cache.s3_bucket,
                "Key": s3_key,
                "Body": data,
            }
            if self._account_id is not None:
                put_kwargs["ExpectedBucketOwner"] = self._account_id

            self._data_cache.s3_client.put_object(**put_kwargs)
            return True
        except ClientError as exc:
            status_code = int(exc.response["ResponseMetadata"]["HTTPStatusCode"])
            status_code_guidance = {
                **COMMON_ERROR_GUIDANCE_FOR_S3,
                403: (
                    "Forbidden or Access denied. Please check your AWS credentials, and ensure "
                    "that your AWS IAM Role or User has the 's3:PutObject' permission."
                ),
                404: "Not found. Please check your bucket name.",
            }
            raise JobAttachmentsS3ClientError(
                action="uploading chunk",
                status_code=status_code,
                bucket_name=self._data_cache.s3_bucket,
                key_or_prefix=s3_key,
                message=f"{status_code_guidance.get(status_code, '')} {str(exc)}",
            ) from exc
        except BotoCoreError as bce:
            raise JobAttachmentS3BotoCoreError(
                action="uploading chunk",
                error_details=str(bce),
            ) from bce

    def _stream_upload_to_s3(self, item: _StreamingWorkItem) -> None:
        """
        Upload a large file to S3 using streaming/multipart upload.

        Computes the hash while streaming and verifies it matches the pre-computed hash.
        """
        import xxhash

        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")
        if item.file_hash is None:
            raise ValueError("File hash is None, cannot determine S3 key")
        if self._hash_alg != HashAlgorithm.XXH128:
            raise ValueError(f"Unsupported hash algorithm for streaming: {self._hash_alg}")

        s3_key = self._data_cache.get_object_key(item.file_hash, self._hash_alg.value)

        hasher = xxhash.xxh128()
        multipart_threshold = 2 * self._data_cache.multipart_part_size

        try:
            extra_args: Dict[str, Any] = {}
            if self._account_id is not None:
                extra_args["ExpectedBucketOwner"] = self._account_id

            if item.file_size <= multipart_threshold:
                # Small enough for single PUT - read entire file
                with open(item.file_path, "rb") as f:
                    data = f.read()
                hasher.update(data)
                upload_hash = hasher.hexdigest()

                # Verify hash before uploading
                if upload_hash != item.file_hash:
                    raise ValueError(
                        f"Hash mismatch during streaming upload of '{item.file_path}': "
                        f"expected {item.file_hash}, got {upload_hash}. "
                        f"File may have been modified during processing."
                    )

                put_kwargs: Dict[str, Any] = {
                    "Bucket": self._data_cache.s3_bucket,
                    "Key": s3_key,
                    "Body": data,
                }
                put_kwargs.update(extra_args)
                self._data_cache.s3_client.put_object(**put_kwargs)

                if self._progress_tracker is not None:
                    self._progress_tracker.track_progress_callback(len(data))
            else:
                # Use multipart upload for large files
                self._stream_multipart_upload_to_s3(item, s3_key, hasher, extra_args)

            logger.debug(f"Streamed upload (verified): {s3_key}")
        except ClientError as exc:
            status_code = int(exc.response["ResponseMetadata"]["HTTPStatusCode"])
            raise JobAttachmentsS3ClientError(
                action="uploading large file",
                status_code=status_code,
                bucket_name=self._data_cache.s3_bucket,
                key_or_prefix=s3_key,
                message=str(exc),
            ) from exc
        except BotoCoreError as bce:
            raise JobAttachmentS3BotoCoreError(
                action="uploading large file",
                error_details=str(bce),
            ) from bce

    def _stream_multipart_upload_to_s3(
        self,
        item: _StreamingWorkItem,
        s3_key: str,
        hasher: Any,
        extra_args: Dict[str, Any],
    ) -> None:
        """Handle multipart upload for large streaming files."""
        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")

        multipart = self._data_cache.s3_client.create_multipart_upload(
            Bucket=self._data_cache.s3_bucket,
            Key=s3_key,
            **extra_args,
        )
        upload_id = multipart["UploadId"]

        try:
            parts: List[Dict[str, Any]] = []
            part_number = 1
            part_size = self._data_cache.multipart_part_size

            with open(item.file_path, "rb") as f:
                while True:
                    chunk = f.read(part_size)
                    if not chunk:
                        break

                    hasher.update(chunk)

                    response = self._data_cache.s3_client.upload_part(
                        Bucket=self._data_cache.s3_bucket,
                        Key=s3_key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=chunk,
                    )
                    parts.append({"PartNumber": part_number, "ETag": response["ETag"]})
                    part_number += 1

                    if self._progress_tracker is not None:
                        self._progress_tracker.track_progress_callback(len(chunk))

            upload_hash = hasher.hexdigest()

            # Verify hash before completing upload
            if upload_hash != item.file_hash:
                # Abort the multipart upload
                self._data_cache.s3_client.abort_multipart_upload(
                    Bucket=self._data_cache.s3_bucket,
                    Key=s3_key,
                    UploadId=upload_id,
                )
                raise ValueError(
                    f"Hash mismatch during streaming upload of '{item.file_path}': "
                    f"expected {item.file_hash}, got {upload_hash}. "
                    f"File may have been modified during processing."
                )

            # Complete the multipart upload
            self._data_cache.s3_client.complete_multipart_upload(
                Bucket=self._data_cache.s3_bucket,
                Key=s3_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            # Abort multipart upload on any error
            try:
                self._data_cache.s3_client.abort_multipart_upload(
                    Bucket=self._data_cache.s3_bucket,
                    Key=s3_key,
                    UploadId=upload_id,
                )
            except Exception:
                pass  # Best effort cleanup
            raise

    # =========================================================================
    # Filesystem Upload Methods
    # =========================================================================

    def _upload_chunk_to_filesystem(self, item: _ChunkWorkItem) -> None:
        """Write chunk to filesystem."""
        import os
        import secrets

        if not isinstance(self._data_cache, FileSystemDataCache):
            raise TypeError(f"Expected FileSystemDataCache, got {type(self._data_cache).__name__}")
        if item.data is None:
            raise ValueError("Chunk data is None, cannot write to filesystem")
        if item.chunk_hash is None:
            raise ValueError("Chunk hash is None, cannot determine file path")

        file_path = Path(self._data_cache.get_object_key(item.chunk_hash, self._hash_alg.value))

        # Use per-hash lock to prevent race conditions when multiple threads
        # try to write the same content (same hash) concurrently
        hash_lock = self._get_fs_write_lock(item.chunk_hash)

        with hash_lock:
            # Check if file already exists (inside lock to prevent TOCTOU race)
            if file_path.exists():
                item.skipped = True
                item.uploaded = False
                logger.debug(f"Skipping write (exists): {file_path}")
                return

            # Ensure parent directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # Write to a temp file with unique suffix, then use os.replace for atomicity
            # os.replace is atomic on both POSIX and Windows
            temp_suffix = secrets.token_hex(8)
            temp_path = file_path.parent / f"{file_path.name}.tmp.{temp_suffix}"
            try:
                with open(temp_path, "wb") as f:
                    f.write(item.data)
                os.replace(temp_path, file_path)
                item.uploaded = True
                item.skipped = False
                logger.debug(f"Wrote: {file_path}")
            except Exception:
                # Clean up temp file on error
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except Exception:
                    pass  # Best effort cleanup
                raise

    def _stream_upload_to_filesystem(self, item: _StreamingWorkItem) -> None:
        """
        Upload a large file to filesystem by streaming copy.

        Computes the hash while streaming and verifies it matches the pre-computed hash.
        """
        import os
        import secrets
        import xxhash

        if not isinstance(self._data_cache, FileSystemDataCache):
            raise TypeError(f"Expected FileSystemDataCache, got {type(self._data_cache).__name__}")
        if item.file_hash is None:
            raise ValueError("File hash is None, cannot determine file path")
        if self._hash_alg != HashAlgorithm.XXH128:
            raise ValueError(f"Unsupported hash algorithm for streaming: {self._hash_alg}")

        dest_path = Path(self._data_cache.get_object_key(item.file_hash, self._hash_alg.value))

        # Use per-hash lock to prevent race conditions
        hash_lock = self._get_fs_write_lock(item.file_hash)

        with hash_lock:
            # Check if file already exists (inside lock)
            if dest_path.exists():
                item.skipped = True
                item.uploaded = False
                logger.debug(f"Skipping streaming write (exists): {dest_path}")
                # Still report progress for skipped files
                if self._progress_tracker is not None:
                    self._progress_tracker.track_progress_callback(item.file_size)
                return

            # Ensure parent directory exists
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            # Copy file in chunks while computing hash
            temp_suffix = secrets.token_hex(8)
            temp_path = dest_path.parent / f"{dest_path.name}.tmp.{temp_suffix}"
            hasher = xxhash.xxh128()

            try:
                with open(item.file_path, "rb") as src, open(temp_path, "wb") as dst:
                    while True:
                        chunk = src.read(DEFAULT_STREAM_BUFFER_SIZE)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        dst.write(chunk)
                        if self._progress_tracker is not None:
                            self._progress_tracker.track_progress_callback(len(chunk))

                upload_hash = hasher.hexdigest()

                # Verify hash before finalizing
                if upload_hash != item.file_hash:
                    raise ValueError(
                        f"Hash mismatch during streaming upload of '{item.file_path}': "
                        f"expected {item.file_hash}, got {upload_hash}. "
                        f"File may have been modified during processing."
                    )

                os.replace(temp_path, dest_path)
                logger.debug(f"Streamed write (verified): {dest_path}")
            except Exception:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except Exception:
                    pass  # Best effort cleanup
                raise


def _run_pipeline(
    work_items: List[WorkItem],
    hash_alg: HashAlgorithm,
    data_cache: ContentAddressedDataCache,
    account_id: Optional[str],
    max_memory_bytes: int,
    max_workers: int,
    progress_tracker: Optional[ProgressTracker],
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
) -> List[WorkItem]:
    """
    Run the pipeline on work items using separate thread pools for each stage.

    Uses two thread pools to prevent deadlock:
    - READ+HASH pool: reads files and computes hashes, blocks on memory allocation
    - UPLOAD pool: writes to data cache and releases memory

    Cache checks (hash cache, S3 check cache, HeadObject) are performed in the
    READ+HASH worker threads to parallelize S3 API calls.

    This separation ensures UPLOAD tasks can always run to release memory,
    even when READ+HASH tasks are blocked waiting for memory.

    Args:
        work_items: List of work items to process (chunks and/or streaming items)
        hash_alg: Hash algorithm to use
        data_cache: Content-addressable data cache for writes
        account_id: AWS account ID (for S3DataCache ExpectedBucketOwner)
        max_memory_bytes: Maximum memory for buffering
        max_workers: Maximum number of parallel workers per pool
        progress_tracker: Optional progress tracker
        hash_cache: Optional hash cache for skipping already-hashed files
        force_rehash: If True, ignore hash cache

    Returns:
        List of processed work items with hashes filled in
    """
    if not work_items:
        return []

    # Create memory pool
    memory_pool = _MemoryPool(max_memory_bytes)

    # Create separate thread pools for READ+HASH and UPLOAD stages
    # This prevents deadlock: READ+HASH can block on memory while UPLOAD runs to release it
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as read_hash_executor:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as upload_executor:
            pipeline = _TaskBasedPipeline(
                read_hash_executor=read_hash_executor,
                upload_executor=upload_executor,
                memory_pool=memory_pool,
                hash_alg=hash_alg,
                data_cache=data_cache,
                account_id=account_id,
                progress_tracker=progress_tracker,
                hash_cache=hash_cache,
                force_rehash=force_rehash,
            )

            # Submit all work items
            for item in work_items:
                pipeline.submit(item)

            # Wait for completion and return results
            return pipeline.wait_for_completion()


def hash_upload_manifest(
    manifest: AbsManifest,
    data_cache: ContentAddressedDataCache,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    max_memory_bytes: Optional[int] = None,
    max_workers: Optional[int] = None,
    file_chunk_size_bytes: Optional[int] = None,
    progress_tracker: Optional[ProgressTracker] = None,
) -> UploadResult:
    """
    Fill in hashes for a manifest AND write file content to a data cache in a pipelined manner.

    This operation combines hashing and writing into a single pass over the data,
    avoiding the need to read files twice (once for hashing, once for writing).

    Args:
        manifest: Manifest with absolute paths and hash=None for unhashed files
            (from collect_abs_snapshot). Can be AbsSnapshot or AbsSnapshotDiff.
        data_cache: Content-addressable data cache destination. Either S3DataCache
            for cloud storage or FileSystemDataCache for local/network storage.
        hash_cache: Optional hash cache for efficiency
        force_rehash: If True, ignore cache and recalculate all hashes
        max_memory_bytes: Maximum memory to use for buffering (default: auto-detect)
        max_workers: Maximum number of parallel workers (default: 10)
        file_chunk_size_bytes: Chunk size for large file hashing.
            - None: Preserve the chunk size from the input manifest
            - WHOLE_FILE_CHUNK_SIZE (-1): Hash files as a whole, no chunking
            - Positive int: Chunk size in bytes for large files
        progress_tracker: Optional progress tracker for upload progress

    Returns:
        UploadResult containing:
        - statistics: SummaryStatistics with processed/skipped files and bytes
        - manifest: A NEW manifest of the same type with all hashes filled in

    Raises:
        ValueError: If the manifest contains relative paths (paths must be absolute)
        ValueError: If effective chunk size is positive and max_memory_bytes is less than chunk size

    Pipeline Architecture:
        The operation uses separate thread pools for each stage to prevent deadlock:
        - READ+HASH pool: reads files and computes hashes, blocks on memory allocation
        - UPLOAD pool: writes to data cache and releases memory
        This separation ensures UPLOAD tasks can always run to release memory,
        even when READ+HASH tasks are blocked waiting for memory.

    Chunking Behavior:
        - If effective chunk size is WHOLE_FILE_CHUNK_SIZE (-1): all files are hashed
          as a whole. For files larger than max_memory_bytes, the file is streamed for
          hashing (discarding data to avoid OOM), then streamed again for uploading.
        - If effective chunk size is a positive int: files larger than this size use
          chunked hashing. max_memory_bytes must be >= chunk size.

    Note:
        - Input manifest must have absolute paths (from collect_abs_snapshot)
        - Symlink entries are unchanged (they have symlink_target, not hash)
        - Directory entries are unchanged (they have no hash)
        - Deleted entries are unchanged (they mark deletions, no hash needed)
        - Returns a NEW manifest (does not mutate input)
    """
    # Validate that manifest has absolute paths
    _validate_absolute_paths(manifest)

    # Determine output chunk size: use parameter if provided, otherwise preserve from input manifest
    output_chunk_size = (
        file_chunk_size_bytes if file_chunk_size_bytes is not None else manifest.fileChunkSizeBytes
    )

    # WHOLE_FILE_CHUNK_SIZE (-1) means no chunking
    chunking_enabled = output_chunk_size > 0

    # Set up memory limit
    if max_memory_bytes is None:
        max_memory_bytes = _get_default_max_memory_bytes()

    # Set up worker count
    if max_workers is None:
        max_workers = DEFAULT_MAX_WORKERS

    # Validate memory limit against chunk size (only if chunking is enabled)
    if chunking_enabled and max_memory_bytes < output_chunk_size:
        raise ValueError(
            f"max_memory_bytes ({max_memory_bytes}) must be >= fileChunkSizeBytes ({output_chunk_size}). "
            f"The pipeline needs at least one chunk's worth of memory to operate."
        )

    # Get account_id for S3DataCache (used for ExpectedBucketOwner)
    account_id: Optional[str] = None
    if isinstance(data_cache, S3DataCache):
        from ..._aws.aws_clients import get_account_id, get_boto3_session

        try:
            session = get_boto3_session()
            account_id = get_account_id(session=session)
        except Exception:
            logger.warning(
                "Could not determine AWS account ID, proceeding without ExpectedBucketOwner"
            )
            account_id = None

    # Separate entries by type
    file_entries_to_process: List[Tuple[int, ManifestFilePath]] = []
    symlink_entries: List[Tuple[int, ManifestFilePath]] = []
    deleted_entries: List[Tuple[int, ManifestFilePath]] = []

    for idx, entry in enumerate(manifest.files):
        if entry.symlink_target is not None:
            symlink_entries.append((idx, entry))
        elif entry.deleted:
            deleted_entries.append((idx, entry))
        else:
            file_entries_to_process.append((idx, entry))

    # Build all work items for a single unified pipeline
    all_work_items: List[WorkItem] = []
    entry_map: Dict[str, Tuple[int, ManifestFilePath]] = {}
    file_chunk_counts: Dict[str, int] = {}  # cache_key -> expected chunk count

    for idx, entry in file_entries_to_process:
        abs_path = Path(entry.path)
        cache_key = str(abs_path.resolve())
        file_size = entry.size or 0
        mtime = entry.mtime or 0

        entry_map[cache_key] = (idx, entry)

        # Determine how to process this file
        if chunking_enabled and file_size > output_chunk_size:
            # Chunked file: create work items for each chunk
            offset = 0
            chunk_idx = 0
            while offset < file_size:
                chunk_end = min(offset + output_chunk_size, file_size)
                all_work_items.append(
                    _ChunkWorkItem(
                        file_path=abs_path,
                        cache_key=cache_key,
                        file_size=file_size,
                        mtime=mtime,
                        chunk_index=chunk_idx,
                        chunk_start=offset,
                        chunk_end=chunk_end,
                    )
                )
                offset = chunk_end
                chunk_idx += 1
            file_chunk_counts[cache_key] = chunk_idx
        elif file_size > max_memory_bytes:
            # Large whole file: use streaming work item
            all_work_items.append(
                _StreamingWorkItem(
                    file_path=abs_path,
                    cache_key=cache_key,
                    file_size=file_size,
                    mtime=mtime,
                )
            )
            file_chunk_counts[cache_key] = 0  # 0 means whole file (not chunked)
        else:
            # Small whole file: single chunk covering entire file
            all_work_items.append(
                _ChunkWorkItem(
                    file_path=abs_path,
                    cache_key=cache_key,
                    file_size=file_size,
                    mtime=mtime,
                    chunk_index=0,
                    chunk_start=0,
                    chunk_end=file_size,
                )
            )
            file_chunk_counts[cache_key] = 0  # 0 means whole file (not chunked)

    # Track statistics
    start_time = time.perf_counter()

    # Results from pipeline (cache checks happen in worker threads)
    cached_results: Dict[
        str, Union[str, Dict[int, str]]
    ] = {}  # cache_key -> hash or {chunk_idx -> hash}

    # Run the unified pipeline (cache checks are parallelized in worker threads)
    pipeline_results: List[WorkItem] = []
    if all_work_items:
        pipeline_results = _run_pipeline(
            work_items=all_work_items,
            hash_alg=manifest.hashAlg,
            data_cache=data_cache,
            account_id=account_id,
            max_memory_bytes=max_memory_bytes,
            max_workers=max_workers,
            progress_tracker=progress_tracker,
            hash_cache=hash_cache,
            force_rehash=force_rehash,
        )

    # Process results and update caches
    # Track skipped vs processed statistics
    skipped_files_set: set = set()
    skipped_bytes = 0
    processed_bytes = 0

    for item in pipeline_results:
        if isinstance(item, _StreamingWorkItem):
            if item.file_hash is not None:
                cached_results[item.cache_key] = item.file_hash

                # Update hash cache (only for items that weren't skipped from cache)
                if hash_cache is not None and not item.skipped:
                    mtime_str = str(item.mtime) if item.mtime is not None else ""
                    hash_cache.put_entry(
                        HashCacheEntry(
                            file_path=item.cache_key,
                            hash_algorithm=manifest.hashAlg,
                            file_hash=item.file_hash,
                            last_modified_time=mtime_str,
                            range_start=0,
                            range_end=WHOLE_FILE_RANGE_END,
                        )
                    )

        elif isinstance(item, _ChunkWorkItem):
            if item.chunk_hash is not None:
                if file_chunk_counts[item.cache_key] == 0:
                    # Whole file
                    cached_results[item.cache_key] = item.chunk_hash
                    range_start = 0
                    range_end = WHOLE_FILE_RANGE_END
                else:
                    # Chunked file
                    if item.cache_key not in cached_results:
                        cached_results[item.cache_key] = {}
                    chunk_dict = cached_results[item.cache_key]
                    if isinstance(chunk_dict, dict):
                        chunk_dict[item.chunk_index] = item.chunk_hash
                    range_start = item.chunk_start
                    range_end = item.chunk_end

                # Update hash cache (only for items that weren't skipped from cache)
                if hash_cache is not None and not item.skipped:
                    mtime_str = str(item.mtime) if item.mtime is not None else ""
                    hash_cache.put_entry(
                        HashCacheEntry(
                            file_path=item.cache_key,
                            hash_algorithm=manifest.hashAlg,
                            file_hash=item.chunk_hash,
                            last_modified_time=mtime_str,
                            range_start=range_start,
                            range_end=range_end,
                        )
                    )

        # Track statistics
        chunk_size = item.chunk_end - item.chunk_start if isinstance(item, _ChunkWorkItem) else item.file_size
        if item.skipped:
            skipped_bytes += chunk_size
            skipped_files_set.add(item.cache_key)
        else:
            processed_bytes += chunk_size

    # Calculate final file-level statistics
    skipped_files = 0
    processed_files = 0
    for cache_key in entry_map:
        if cache_key in skipped_files_set:
            # Check if ALL chunks of this file were skipped
            all_skipped = all(item.skipped for item in pipeline_results if item.cache_key == cache_key)
            if all_skipped:
                skipped_files += 1
            else:
                processed_files += 1
        else:
            processed_files += 1

    # Build result manifest
    hashed_paths: List[ManifestFilePath] = []
    total_size = 0

    for cache_key, (idx, entry) in entry_map.items():
        file_size = entry.size or 0
        expected_chunks = file_chunk_counts[cache_key]

        if expected_chunks == 0:
            # Whole file (either small or streaming)
            file_hash = cached_results.get(cache_key)
            if not isinstance(file_hash, str):
                raise ValueError(f"Internal error: file '{entry.path}' was not hashed")
            hashed_paths.append(
                ManifestFilePath(
                    path=entry.path,
                    hash=file_hash,
                    size=file_size,
                    mtime=entry.mtime,
                    runnable=entry.runnable,
                )
            )
        else:
            # Chunked file
            chunk_dict_result = cached_results.get(cache_key)
            if not isinstance(chunk_dict_result, dict):
                raise ValueError(f"Internal error: chunked file '{entry.path}' has no chunk hashes")
            chunk_dict = chunk_dict_result

            chunkhashes_list: List[str] = []
            for i in range(expected_chunks):
                chunk_hash = chunk_dict.get(i)
                if chunk_hash is None:
                    raise ValueError(
                        f"Internal error: chunk {i} of file '{entry.path}' was not hashed"
                    )
                chunkhashes_list.append(chunk_hash)

            hashed_paths.append(
                ManifestFilePath(
                    path=entry.path,
                    chunkhashes=chunkhashes_list,
                    size=file_size,
                    mtime=entry.mtime,
                    runnable=entry.runnable,
                )
            )

        total_size += file_size

    # Add symlink entries unchanged
    for idx, entry in symlink_entries:
        hashed_paths.append(
            ManifestFilePath(
                path=entry.path,
                symlink_target=entry.symlink_target,
            )
        )

    # Add deleted entries unchanged
    for idx, entry in deleted_entries:
        hashed_paths.append(
            ManifestFilePath(
                path=entry.path,
                deleted=True,
            )
        )

    # Copy directory entries unchanged
    dir_entries: List[ManifestDirectoryPath] = []
    for d in manifest.dirs:
        dir_entries.append(
            ManifestDirectoryPath(
                path=d.path,
                deleted=d.deleted,
            )
        )

    # Return the same manifest type as input
    manifest_type = type(manifest)
    result_manifest = manifest_type(
        hash_alg=manifest.hashAlg,
        dirs=dir_entries,
        files=hashed_paths,
        total_size=total_size,
        parent_manifest_hash=manifest.parentManifestHash,
        file_chunk_size_bytes=output_chunk_size,
    )

    # Build statistics
    end_time = time.perf_counter()
    total_time = end_time - start_time
    statistics = SummaryStatistics(
        total_time=total_time,
        total_files=len(entry_map),
        total_bytes=total_size,
        processed_files=processed_files,
        processed_bytes=processed_bytes,
        skipped_files=skipped_files,
        skipped_bytes=skipped_bytes,
        transfer_rate=processed_bytes / total_time if total_time > 0 else 0.0,
    )

    return UploadResult(
        statistics=statistics,
        manifest=result_manifest,
    )
