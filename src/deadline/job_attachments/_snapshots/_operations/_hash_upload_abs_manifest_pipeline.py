# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Base pipeline for hash_upload_abs_manifest operation.

This module implements the base class for the hash+upload pipeline.
Subclasses implement cache-specific upload logic (S3 or filesystem).
"""

from __future__ import annotations

import concurrent.futures
import time
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
import logging

from .._content_addressed_data_cache import ContentAddressedDataCache
from ...asset_manifests.hash_algorithms import HashAlgorithm
from ...caches.hash_cache import HashCache
from ..._path_summarization import human_readable_file_size

logger = logging.getLogger("deadline.job_attachments.hash_upload")

# Default interval for progress callbacks (5 times per second)
DEFAULT_PROGRESS_CALLBACK_INTERVAL = 0.2  # seconds

# Default read buffer size for streaming hash (when fileChunkSizeBytes is WHOLE_FILE_CHUNK_SIZE)
DEFAULT_STREAM_BUFFER_SIZE = 64 * 1024 * 1024  # 64MB


@dataclass
class HashUploadProgressMetadata:
    """
    Progress metadata for hash_upload_abs_manifest operation.

    Reports separate progress for hashing and uploading phases of the pipeline.
    For chunked files, each chunk is counted separately in the file/chunk counts.
    """

    # Totals
    total_file_chunks: int  # Total files + chunks to process
    total_bytes: int

    # Hashing phase progress
    hashed_file_chunks: int
    hashed_bytes: int
    hash_skipped_file_chunks: int  # Skipped due to hash cache hit
    hash_skipped_bytes: int

    # Upload phase progress
    uploaded_file_chunks: int
    uploaded_bytes: int
    upload_skipped_file_chunks: int  # Skipped because already in data cache
    upload_skipped_bytes: int

    # Overall progress (based on upload completion, which is the final stage)
    progress: float  # 0-100
    progressMessage: str

    # Timing (only set in final statistics, 0.0 during progress callbacks)
    total_time: float = 0.0  # Total operation time in seconds
    transfer_rate: float = 0.0  # Bytes per second


# Callback type for hash_upload progress reporting
# Return True to continue, False to cancel the operation
HashUploadProgressCallback = Callable[[HashUploadProgressMetadata], bool]


@dataclass
class _HashUploadProgressState:
    """
    Thread-safe progress state for hash_upload pipeline.

    Tracks bytes and file/chunk counts. For chunked files, each chunk is counted separately.
    """

    # Totals (set once at initialization)
    total_file_chunks: int = 0
    total_bytes: int = 0

    # Byte-level progress
    hashed_bytes: int = 0
    hash_skipped_bytes: int = 0
    uploaded_bytes: int = 0
    upload_skipped_bytes: int = 0

    # File/chunk-level progress (incremented when each file or chunk completes)
    hashed_file_chunks: int = 0
    hash_skipped_file_chunks: int = 0
    uploaded_file_chunks: int = 0
    upload_skipped_file_chunks: int = 0

    # Callback and timing
    on_progress: Optional[HashUploadProgressCallback] = None
    callback_interval: float = DEFAULT_PROGRESS_CALLBACK_INTERVAL
    _last_callback_time: float = field(default_factory=time.perf_counter)
    _cancelled: bool = False

    # Thread safety
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_hash_complete(self, chunk_bytes: int, skipped: bool) -> None:
        """Record completion of hashing for a file or chunk."""
        with self._lock:
            if skipped:
                self.hash_skipped_bytes += chunk_bytes
                self.hash_skipped_file_chunks += 1
            else:
                self.hashed_bytes += chunk_bytes
                self.hashed_file_chunks += 1
            self._maybe_invoke_callback()

    def record_upload_complete(self, chunk_bytes: int, skipped: bool) -> None:
        """Record completion of uploading for a file or chunk."""
        with self._lock:
            if skipped:
                self.upload_skipped_bytes += chunk_bytes
                self.upload_skipped_file_chunks += 1
            else:
                self.uploaded_bytes += chunk_bytes
                self.uploaded_file_chunks += 1
            self._maybe_invoke_callback()

    def _maybe_invoke_callback(self) -> None:
        """Invoke callback if interval has elapsed. Must be called with lock held."""
        if self.on_progress is None or self._cancelled:
            return

        now = time.perf_counter()
        if now - self._last_callback_time < self.callback_interval:
            return

        self._last_callback_time = now
        metadata = self._build_metadata()

        # Release lock during callback to avoid deadlock
        self._lock.release()
        try:
            should_continue = self.on_progress(metadata)
            if not should_continue:
                self._cancelled = True
        finally:
            self._lock.acquire()

    def _build_metadata(self) -> HashUploadProgressMetadata:
        """Build progress metadata. Must be called with lock held."""
        completed_upload_bytes = self.uploaded_bytes + self.upload_skipped_bytes
        progress = (
            (completed_upload_bytes / self.total_bytes * 100) if self.total_bytes > 0 else 0.0
        )

        msg = (
            f"Hashed {human_readable_file_size(self.hashed_bytes + self.hash_skipped_bytes)}, "
            f"Uploaded {human_readable_file_size(completed_upload_bytes)} "
            f"/ {human_readable_file_size(self.total_bytes)}"
        )

        return HashUploadProgressMetadata(
            total_file_chunks=self.total_file_chunks,
            total_bytes=self.total_bytes,
            hashed_file_chunks=self.hashed_file_chunks,
            hashed_bytes=self.hashed_bytes,
            hash_skipped_file_chunks=self.hash_skipped_file_chunks,
            hash_skipped_bytes=self.hash_skipped_bytes,
            uploaded_file_chunks=self.uploaded_file_chunks,
            uploaded_bytes=self.uploaded_bytes,
            upload_skipped_file_chunks=self.upload_skipped_file_chunks,
            upload_skipped_bytes=self.upload_skipped_bytes,
            progress=progress,
            progressMessage=msg,
        )

    def is_cancelled(self) -> bool:
        """Check if operation was cancelled via callback."""
        with self._lock:
            return self._cancelled

    def force_callback(self) -> None:
        """Force a callback invocation (e.g., at end of operation)."""
        with self._lock:
            if self.on_progress is None or self._cancelled:
                return
            metadata = self._build_metadata()

        # Invoke without lock
        self.on_progress(metadata)


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
    skipped: bool = False  # True if already in data cache


@dataclass
class _StreamingWorkItem:
    """
    Work item for large files that don't fit in memory.

    These files are processed with a two-pass approach:
    - Pass 1: Stream through file to compute hash (discard data)
    - Pass 2: Stream through file again to upload
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


@dataclass
class _MultipartUploadState:
    """
    State tracker for parallel multipart uploads to S3.

    Tracks completion of all parts for a single file and triggers
    CompleteMultipartUpload when all parts are done.
    """

    file_hash: str  # Final hash of the complete file
    s3_key: str  # S3 object key
    upload_id: str  # S3 multipart upload ID
    parts_remaining: int  # Number of parts still being uploaded
    completed_parts: List[Dict[str, Any]]  # List of {"PartNumber": int, "ETag": str}
    file_size: int = 0  # Total file size for progress tracking
    total_bytes_uploaded: int = 0
    part_errors: List[Exception] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Per-part hashes for verification (indexed by part_number - 1)
    part_hashes: List[str] = field(default_factory=list)


@dataclass
class _MultipartPartWorkItem:
    """
    Work item for a single part of a multipart upload.

    Each part is uploaded independently in the thread pool.
    """

    # Multipart coordination
    multipart_state: _MultipartUploadState
    part_number: int  # 1-based part number for S3

    # Part data
    data: bytes  # Part content to upload

    # Expected hash for verification (computed during first pass)
    expected_hash: Optional[str] = None

    # Upload status
    uploaded: bool = False
    etag: Optional[str] = None


# Union type for work items that go through the full pipeline
PipelineWorkItem = Union[_ChunkWorkItem, _StreamingWorkItem]

# Union type for all work items (including internal multipart parts)
WorkItem = Union[_ChunkWorkItem, _StreamingWorkItem, _MultipartPartWorkItem]


class _MemoryPool:
    """
    Thread-safe memory pool that tracks allocated memory and blocks when limit is reached.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._allocated_bytes = 0
        self._lock = threading.Lock()
        self._space_available = threading.Condition(self._lock)

    def allocate(self, size: int) -> None:
        """Allocate memory from the pool, blocking if necessary."""
        with self._space_available:
            while self._allocated_bytes + size > self._max_bytes:
                self._space_available.wait()
            self._allocated_bytes += size

    def release(self, size: int) -> None:
        """Release memory back to the pool."""
        with self._space_available:
            self._allocated_bytes -= size
            self._space_available.notify_all()

    @property
    def allocated(self) -> int:
        """Current allocated bytes."""
        with self._lock:
            return self._allocated_bytes


class HashUploadPipelineBase(ABC):
    """
    Base class for hash+upload pipelines.

    Uses separate thread pools for READ+HASH and UPLOAD stages to prevent deadlock.
    Subclasses implement cache-specific upload methods.
    """

    def __init__(
        self,
        read_hash_executor: concurrent.futures.ThreadPoolExecutor,
        upload_executor: concurrent.futures.ThreadPoolExecutor,
        memory_pool: _MemoryPool,
        hash_alg: HashAlgorithm,
        data_cache: ContentAddressedDataCache,
        progress_state: Optional[_HashUploadProgressState],
        hash_cache: Optional[HashCache] = None,
        force_rehash: bool = False,
    ) -> None:
        self._read_hash_executor = read_hash_executor
        self._upload_executor = upload_executor
        self._memory_pool = memory_pool
        self._hash_alg = hash_alg
        self._data_cache = data_cache
        self._progress_state = progress_state
        self._hash_cache = hash_cache
        self._force_rehash = force_rehash

        # Track completion
        self._pending_count = 0
        self._lock = threading.Lock()
        self._done_event = threading.Event()

        # Collect results
        self._results: List[PipelineWorkItem] = []
        self._results_lock = threading.Lock()

        # Track errors
        self._error: Optional[Exception] = None
        self._error_lock = threading.Lock()

        # Deduplication: track hashes currently being uploaded to prevent concurrent
        # uploads of the same content. Maps hash -> Event that signals upload complete.
        self._uploading_hashes: Dict[str, threading.Event] = {}
        self._uploading_hashes_lock = threading.Lock()

    # =========================================================================
    # Public API
    # =========================================================================

    def submit(self, item: PipelineWorkItem) -> None:
        """Submit a work item to start processing through the pipeline."""
        with self._lock:
            self._pending_count += 1
            self._done_event.clear()
        self._read_hash_executor.submit(self._do_read_and_hash, item)

    def wait_for_completion(self) -> List[PipelineWorkItem]:
        """Wait for all submitted items to complete and return results."""
        self._done_event.wait()

        with self._error_lock:
            if self._error is not None:
                raise self._error

        with self._results_lock:
            return list(self._results)

    def was_cache_invalidated(self) -> bool:
        """Check if the data cache was invalidated during pipeline execution.

        Subclasses override this to report cache invalidation (e.g., S3 check cache).
        """
        return False

    # =========================================================================
    # Internal: Completion tracking
    # =========================================================================

    def _decrement_pending(self) -> None:
        """Decrement pending count and signal completion if done."""
        with self._lock:
            self._pending_count -= 1
            if self._pending_count == 0:
                self._done_event.set()

    def _increment_pending(self) -> None:
        """Increment pending count for additional work items."""
        with self._lock:
            self._pending_count += 1
            self._done_event.clear()

    def _record_error(self, error: Exception) -> None:
        """Record an error (first error wins)."""
        with self._error_lock:
            if self._error is None:
                self._error = error
                logger.exception(f"Pipeline error: {error}")
        self._done_event.set()

    def _record_result(self, item: PipelineWorkItem) -> None:
        """Record a completed work item."""
        with self._results_lock:
            self._results.append(item)

    # =========================================================================
    # READ+HASH Stage
    # =========================================================================

    def _do_read_and_hash(self, item: PipelineWorkItem) -> None:
        """Combined READ+HASH stage."""
        try:
            with self._error_lock:
                if self._error is not None:
                    self._decrement_pending()
                    return

            # Check caches first
            cached_hash, can_skip = self._check_cache_and_skip(item)
            if can_skip:
                if isinstance(item, _StreamingWorkItem):
                    item.file_hash = cached_hash
                else:
                    item.chunk_hash = cached_hash
                item.skipped = True
                item.uploaded = False

                self._on_item_skipped(item)

                if self._progress_state is not None:
                    chunk_bytes = (
                        item.file_size
                        if isinstance(item, _StreamingWorkItem)
                        else item.chunk_end - item.chunk_start
                    )
                    self._progress_state.record_hash_complete(chunk_bytes, skipped=True)
                    self._progress_state.record_upload_complete(chunk_bytes, skipped=True)
                self._record_result(item)
                self._decrement_pending()
                logger.debug(f"Skipped (cache hit): {item.file_path}")
                return

            if isinstance(item, _StreamingWorkItem):
                self._process_streaming_item(item, cached_hash)
            else:
                self._process_chunk_item(item, cached_hash)

        except Exception as e:
            self._record_error(e)
            self._decrement_pending()

    def _check_cache_and_skip(self, item: PipelineWorkItem) -> tuple[Optional[str], bool]:
        """Check hash cache and data cache to see if this item can be skipped.

        Returns:
            (cached_hash, can_skip) tuple
        """
        if self._hash_cache is None or self._force_rehash:
            return None, False

        mtime_str = str(item.mtime) if item.mtime is not None else ""

        if isinstance(item, _StreamingWorkItem):
            hash_cache_entry = self._hash_cache.get_entry(item.cache_key, self._hash_alg)
        else:
            if item.chunk_start == 0 and item.chunk_end == item.file_size:
                hash_cache_entry = self._hash_cache.get_entry(item.cache_key, self._hash_alg)
            else:
                hash_cache_entry = self._hash_cache.get_entry(
                    item.cache_key, self._hash_alg, item.chunk_start, item.chunk_end
                )

        if hash_cache_entry is None or hash_cache_entry.last_modified_time != mtime_str:
            return None, False

        cached_hash = hash_cache_entry.file_hash

        # Check if object exists in data cache (subclasses may override for special handling)
        if self._check_data_cache_exists(cached_hash):
            return cached_hash, True

        return cached_hash, False

    def _check_data_cache_exists(self, cached_hash: str) -> bool:
        """Check if object exists in data cache.

        Subclasses can override for special handling (e.g., S3 check cache validation).
        """
        return self._data_cache.object_exists(cached_hash, self._hash_alg.value)

    def _on_item_skipped(self, item: PipelineWorkItem) -> None:
        """Called when an item is skipped due to cache hit.

        Subclasses can override to track skipped items (e.g., for cache invalidation).
        """
        pass

    @abstractmethod
    def _process_streaming_item(self, item: _StreamingWorkItem, cached_hash: Optional[str]) -> None:
        """Process a streaming work item. Subclasses implement cache-specific logic."""
        pass

    @abstractmethod
    def _process_chunk_item(self, item: _ChunkWorkItem, cached_hash: Optional[str]) -> None:
        """Process a chunk work item. Subclasses implement cache-specific logic."""
        pass

    def _stream_hash_file(self, file_path: Path) -> str:
        """Compute hash of a file by streaming through it."""
        import xxhash

        if self._hash_alg != HashAlgorithm.XXH128:
            raise ValueError(f"Unsupported hash algorithm for streaming: {self._hash_alg}")

        buffer_size = self._get_stream_buffer_size()

        hasher = xxhash.xxh128()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(buffer_size)
                if not chunk:
                    break
                hasher.update(chunk)

        return hasher.hexdigest()

    def _get_stream_buffer_size(self) -> int:
        """Get buffer size for streaming operations. Subclasses can override."""
        return DEFAULT_STREAM_BUFFER_SIZE

    # =========================================================================
    # UPLOAD Stage
    # =========================================================================

    def _do_upload(self, item: WorkItem) -> None:
        """UPLOAD stage: Write data to data cache."""
        should_decrement_pending = True

        try:
            with self._error_lock:
                if self._error is not None:
                    if isinstance(item, _ChunkWorkItem) and item.data is not None:
                        self._memory_pool.release(len(item.data))
                    elif isinstance(item, _MultipartPartWorkItem):
                        self._memory_pool.release(len(item.data))
                    return

            if isinstance(item, _StreamingWorkItem):
                sync_complete = self._upload_streaming(item)
                if sync_complete:
                    self._record_result(item)
                else:
                    should_decrement_pending = False
            elif isinstance(item, _MultipartPartWorkItem):
                self._upload_multipart_part(item)
                should_decrement_pending = False
                return
            else:
                # Deduplicate concurrent uploads of the same hash
                if item.chunk_hash is not None:
                    wait_event: Optional[threading.Event] = None
                    with self._uploading_hashes_lock:
                        if item.chunk_hash in self._uploading_hashes:
                            # Another thread is uploading this hash, wait for it
                            wait_event = self._uploading_hashes[item.chunk_hash]
                        else:
                            # Register this hash as being uploaded
                            self._uploading_hashes[item.chunk_hash] = threading.Event()

                    if wait_event is not None:
                        # Wait for the other upload to complete, then skip this one
                        wait_event.wait()
                        item.skipped = True
                        item.uploaded = False
                        if item.data is not None:
                            chunk_size = len(item.data)
                            self._memory_pool.release(chunk_size)
                            item.data = None
                            if self._progress_state is not None:
                                self._progress_state.record_upload_complete(
                                    chunk_size, skipped=True
                                )
                        self._record_result(item)
                        return

                try:
                    self._upload_chunk(item)
                finally:
                    # Unregister and signal completion
                    if item.chunk_hash is not None:
                        with self._uploading_hashes_lock:
                            event = self._uploading_hashes.pop(item.chunk_hash, None)
                            if event is not None:
                                event.set()
                self._record_result(item)

        except Exception as e:
            if isinstance(item, _ChunkWorkItem) and item.data is not None:
                self._memory_pool.release(len(item.data))
                item.data = None
            self._record_error(e)

        finally:
            if should_decrement_pending:
                self._decrement_pending()

    @abstractmethod
    def _upload_chunk(self, item: _ChunkWorkItem) -> None:
        """Upload chunk data from memory. Subclasses implement cache-specific logic."""
        pass

    @abstractmethod
    def _upload_streaming(self, item: _StreamingWorkItem) -> bool:
        """Stream file to data cache. Returns True if sync, False if async."""
        pass

    @abstractmethod
    def _upload_multipart_part(self, item: _MultipartPartWorkItem) -> None:
        """Upload a single part of a multipart upload."""
        pass
