# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for filling in hashes AND uploading file content to a data cache in a pipelined manner.

This module implements the HASH_UPLOAD operation from the composable manifest operations design:
    HASH_UPLOAD: (AbsManifest, DataCache) → AbsManifest (with hashes filled in) + data cache writes

The operation combines hashing and uploading into a single pass over the data:
- Reads file chunks into memory
- Hashes each chunk
- Writes to data cache before freeing memory

This avoids reading files twice (once for hashing, once for uploading) and provides
significant performance improvements for large datasets.

The pipeline uses bounded memory to prevent OOM conditions:
- READ stage reads chunks into a memory pool
- HASH stage computes hashes in-place
- UPLOAD stage uploads/writes and releases memory
- When memory limit is reached, READ blocks until UPLOAD frees space

All composable operations use v2025 structure and semantics internally. Support for
v2023 on-disk format is provided via lossy conversion functions that drop symlinks,
deletions, and other v2025-only features.
"""

from __future__ import annotations

import time
import threading
import queue
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import logging

from botocore.exceptions import BotoCoreError, ClientError

from .._manifest import (
    AbsManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    _is_absolute_path,
)
from ..hash_algorithms import HashAlgorithm, hash_data
from ...caches.hash_cache import HashCache, HashCacheEntry, WHOLE_FILE_RANGE_END
from ...caches.s3_check_cache import S3CheckCacheEntry
from ...progress_tracker import ProgressTracker
from ...exceptions import (
    JobAttachmentsS3ClientError,
    JobAttachmentS3BotoCoreError,
    COMMON_ERROR_GUIDANCE_FOR_S3,
)
from ._content_addressed_data_cache import (
    ContentAddressedDataCache,
    S3DataCache,
    FileSystemDataCache,
)

logger = logging.getLogger("deadline.job_attachments.hash_upload")

# Minimum memory limit: 256MB (one chunk)
MIN_MEMORY_BYTES = 256 * 1024 * 1024

# Default read buffer size for streaming hash (when fileChunkSizeBytes is WHOLE_FILE_CHUNK_SIZE)
DEFAULT_STREAM_BUFFER_SIZE = 64 * 1024 * 1024  # 64MB

# Sentinel value to signal pipeline shutdown
_SHUTDOWN_SENTINEL = object()


def _validate_absolute_paths(manifest: AbsManifest) -> None:
    """Validate that all paths in the manifest are absolute."""
    for entry in manifest.files:
        if not _is_absolute_path(entry.path):
            raise ValueError(
                f"HASH_UPLOAD operation requires absolute paths. "
                f"Found relative path: '{entry.path}'. "
                f"Use collect_manifest() or join_manifest() to create a manifest with absolute paths."
            )

    for d in manifest.dirs:
        if not _is_absolute_path(d.path):
            raise ValueError(
                f"HASH_UPLOAD operation requires absolute paths. "
                f"Found relative directory path: '{d.path}'. "
                f"Use collect_manifest() or join_manifest() to create a manifest with absolute paths."
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


class _PipelineStage:
    """Base class for pipeline stages."""

    def __init__(
        self,
        name: str,
        input_queue: "queue.Queue[Any]",
        output_queue: Optional["queue.Queue[Any]"],
    ) -> None:
        self.name = name
        self._input_queue = input_queue
        self._output_queue = output_queue
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[Exception] = None
        self._shutdown = False

    def start(self) -> None:
        """Start the stage thread."""
        self._thread = threading.Thread(target=self._run, name=f"Pipeline-{self.name}")
        self._thread.daemon = True
        self._thread.start()

    def join(self) -> None:
        """Wait for the stage thread to complete."""
        if self._thread is not None:
            self._thread.join()

    def signal_shutdown(self) -> None:
        """Signal the stage to shut down."""
        self._shutdown = True

    def get_error(self) -> Optional[Exception]:
        """Get any error that occurred in this stage."""
        return self._error

    def _run(self) -> None:
        """Main loop for the stage."""
        try:
            while True:
                try:
                    item = self._input_queue.get(timeout=0.1)
                except queue.Empty:
                    if self._shutdown:
                        break
                    continue

                if item is _SHUTDOWN_SENTINEL:
                    # Pass sentinel to next stage
                    if self._output_queue is not None:
                        self._output_queue.put(_SHUTDOWN_SENTINEL)
                    break

                result = self._process(item)
                if self._output_queue is not None and result is not None:
                    self._output_queue.put(result)

        except Exception as e:
            self._error = e
            logger.exception(f"Error in pipeline stage {self.name}: {e}")
            # Signal shutdown to prevent deadlock
            if self._output_queue is not None:
                self._output_queue.put(_SHUTDOWN_SENTINEL)

    def _process(self, item: Any) -> Any:
        """Process a single item. Override in subclasses."""
        raise NotImplementedError


class _ReadStage(_PipelineStage):
    """
    Pipeline stage that reads file data from disk.

    For _ChunkWorkItem: reads chunk data into memory (blocking on memory pool).
    For _StreamingWorkItem: passes through unchanged (streaming happens in HASH stage).
    """

    def __init__(
        self,
        input_queue: "queue.Queue[WorkItem]",
        output_queue: "queue.Queue[WorkItem]",
        memory_pool: _MemoryPool,
    ) -> None:
        super().__init__("READ", input_queue, output_queue)
        self._memory_pool = memory_pool

    def _process(self, item: WorkItem) -> WorkItem:
        """Read chunk data from disk or pass through streaming items."""
        if isinstance(item, _StreamingWorkItem):
            # Streaming items don't load data into memory here
            return item

        # _ChunkWorkItem: read chunk data
        chunk_size = item.chunk_end - item.chunk_start

        # Block until we have memory available
        self._memory_pool.allocate(chunk_size)

        try:
            with open(item.file_path, "rb") as f:
                f.seek(item.chunk_start)
                item.data = f.read(chunk_size)
        except Exception:
            # Release memory on error
            self._memory_pool.release(chunk_size)
            raise

        return item


class _HashStage(_PipelineStage):
    """
    Pipeline stage that computes hashes.

    For _ChunkWorkItem: computes hash of chunk data in memory.
    For _StreamingWorkItem: streams through file to compute hash (discards data).
    """

    def __init__(
        self,
        input_queue: "queue.Queue[WorkItem]",
        output_queue: "queue.Queue[WorkItem]",
        hash_alg: HashAlgorithm,
    ) -> None:
        super().__init__("HASH", input_queue, output_queue)
        self._hash_alg = hash_alg

    def _process(self, item: WorkItem) -> WorkItem:
        """Compute hash of chunk data or stream hash for large files."""
        if isinstance(item, _StreamingWorkItem):
            # Stream through file to compute hash
            item.file_hash = self._stream_hash_file(item.file_path)
            return item

        # _ChunkWorkItem: hash in-memory data
        if item.data is not None:
            item.chunk_hash = hash_data(item.data, self._hash_alg)
        return item

    def _stream_hash_file(self, file_path: Path) -> str:
        """
        Compute hash of a file by streaming through it.

        Reads the file in chunks, updating the hash incrementally,
        and discards the data to avoid memory issues.
        """
        import xxhash

        if self._hash_alg != HashAlgorithm.XXH128:
            raise ValueError(f"Unsupported hash algorithm for streaming: {self._hash_alg}")

        hasher = xxhash.xxh128()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(DEFAULT_STREAM_BUFFER_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)

        return hasher.hexdigest()


class _UploadStage(_PipelineStage):
    """
    Pipeline stage that writes data to a data cache (S3 or filesystem).

    For _ChunkWorkItem: uploads chunk data from memory, then releases memory.
    For _StreamingWorkItem: streams file to data cache (reads file again).
    """

    def __init__(
        self,
        input_queue: "queue.Queue[WorkItem]",
        output_queue: "queue.Queue[WorkItem]",
        memory_pool: _MemoryPool,
        data_cache: ContentAddressedDataCache,
        hash_alg: HashAlgorithm,
        account_id: Optional[str],
        progress_tracker: Optional[ProgressTracker],
    ) -> None:
        super().__init__("UPLOAD", input_queue, output_queue)
        self._memory_pool = memory_pool
        self._data_cache = data_cache
        self._hash_alg = hash_alg
        self._account_id = account_id
        self._progress_tracker = progress_tracker

    def _process(self, item: WorkItem) -> WorkItem:
        """Write data to data cache."""
        if isinstance(item, _StreamingWorkItem):
            return self._process_streaming(item)
        return self._process_chunk(item)

    def _process_chunk(self, item: _ChunkWorkItem) -> _ChunkWorkItem:
        """Upload chunk data from memory."""
        if item.data is None or item.chunk_hash is None:
            return item

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

        return item

    def _process_streaming(self, item: _StreamingWorkItem) -> _StreamingWorkItem:
        """Stream file to data cache."""
        if item.file_hash is None:
            return item

        # Check if already in data cache
        if self._data_cache.object_exists(item.file_hash, self._hash_alg.value):
            item.skipped = True
            item.uploaded = False
            logger.debug(f"Skipping streaming upload (exists): {item.file_path}")
            # Still report progress for skipped files
            if self._progress_tracker is not None:
                self._progress_tracker.track_progress_callback(item.file_size)
            return item

        # Stream upload
        if isinstance(self._data_cache, S3DataCache):
            self._stream_upload_to_s3(item)
        elif isinstance(self._data_cache, FileSystemDataCache):
            self._stream_upload_to_filesystem(item)
        else:
            raise ValueError(f"Unsupported data cache type: {type(self._data_cache)}")

        item.uploaded = True
        item.skipped = False
        return item

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

    def _upload_chunk_to_filesystem(self, item: _ChunkWorkItem) -> None:
        """Write chunk to filesystem."""
        if not isinstance(self._data_cache, FileSystemDataCache):
            raise TypeError(f"Expected FileSystemDataCache, got {type(self._data_cache).__name__}")
        if item.data is None:
            raise ValueError("Chunk data is None, cannot write to filesystem")
        if item.chunk_hash is None:
            raise ValueError("Chunk hash is None, cannot determine file path")

        file_path = Path(self._data_cache.get_object_key(item.chunk_hash, self._hash_alg.value))

        # Check if file already exists
        if file_path.exists():
            item.skipped = True
            item.uploaded = False
            logger.debug(f"Skipping write (exists): {file_path}")
            return

        # Ensure parent directory exists
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to a temp file first, then rename for atomicity
        temp_path = file_path.with_suffix(f".{file_path.suffix}.tmp")
        try:
            with open(temp_path, "wb") as f:
                f.write(item.data)
            temp_path.rename(file_path)
            item.uploaded = True
            item.skipped = False
            logger.debug(f"Wrote: {file_path}")
        except Exception:
            # Clean up temp file on error
            if temp_path.exists():
                temp_path.unlink()
            raise

    def _upload_to_s3_if_not_exists(self, data: bytes, s3_key: str) -> bool:
        """
        Upload data to S3 only if the object doesn't already exist.

        Uses S3 conditional write (IfNoneMatch='*') to atomically check and upload
        in a single API call, reducing total S3 requests.

        Returns:
            True if the object was uploaded, False if it already existed
        """
        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")

        try:
            put_kwargs: Dict[str, Any] = {
                "Bucket": self._data_cache.s3_bucket,
                "Key": s3_key,
                "Body": data,
                "IfNoneMatch": "*",
            }
            if self._account_id is not None:
                put_kwargs["ExpectedBucketOwner"] = self._account_id

            self._data_cache.s3_client.put_object(**put_kwargs)
            return True
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            # PreconditionFailed means the object already exists
            if error_code == "PreconditionFailed":
                return False
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

        # We need to stream the file ourselves to compute hash while uploading
        # Use multipart upload for large files
        hasher = xxhash.xxh128()
        multipart_threshold = 8 * 1024 * 1024  # 8MB threshold for multipart

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
                multipart = self._data_cache.s3_client.create_multipart_upload(
                    Bucket=self._data_cache.s3_bucket,
                    Key=s3_key,
                    **extra_args,
                )
                upload_id = multipart["UploadId"]

                try:
                    parts: List[Dict[str, Any]] = []
                    part_number = 1
                    part_size = 64 * 1024 * 1024  # 64MB parts

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

    def _stream_upload_to_filesystem(self, item: _StreamingWorkItem) -> None:
        """
        Upload a large file to filesystem by streaming copy.

        Computes the hash while streaming and verifies it matches the pre-computed hash.
        """
        import xxhash

        if not isinstance(self._data_cache, FileSystemDataCache):
            raise TypeError(f"Expected FileSystemDataCache, got {type(self._data_cache).__name__}")
        if item.file_hash is None:
            raise ValueError("File hash is None, cannot determine file path")
        if self._hash_alg != HashAlgorithm.XXH128:
            raise ValueError(f"Unsupported hash algorithm for streaming: {self._hash_alg}")

        dest_path = Path(self._data_cache.get_object_key(item.file_hash, self._hash_alg.value))

        # Ensure parent directory exists
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Copy file in chunks while computing hash
        temp_path = dest_path.with_suffix(f"{dest_path.suffix}.tmp")
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

            temp_path.rename(dest_path)
            logger.debug(f"Streamed write (verified): {dest_path}")
        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            raise


def hash_upload_manifest(
    manifest: AbsManifest,
    data_cache: ContentAddressedDataCache,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    max_memory_bytes: Optional[int] = None,
    file_chunk_size_bytes: Optional[int] = None,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
    progress_tracker: Optional[ProgressTracker] = None,
) -> AbsManifest:
    """
    Fill in hashes for a manifest AND write file content to a data cache in a pipelined manner.

    This operation combines hashing and writing into a single pass over the data,
    avoiding the need to read files twice (once for hashing, once for writing).

    Args:
        manifest: Manifest with absolute paths and hash=None for unhashed files
            (from collect_manifest). Can be AbsSnapshotManifest or AbsDiffManifest.
        data_cache: Content-addressable data cache destination. Either S3DataCache
            for cloud storage or FileSystemDataCache for local/network storage.
        hash_cache: Optional hash cache for efficiency
        force_rehash: If True, ignore cache and recalculate all hashes
        max_memory_bytes: Maximum memory to use for buffering (default: auto-detect)
        file_chunk_size_bytes: Chunk size for large file hashing.
            - None: Preserve the chunk size from the input manifest
            - WHOLE_FILE_CHUNK_SIZE (-1): Hash files as a whole, no chunking
            - Positive int: Chunk size in bytes for large files
        print_function_callback: Progress callback for status messages
        progress_tracker: Optional progress tracker for upload progress

    Returns:
        A NEW manifest of the same type with all hashes filled in

    Raises:
        ValueError: If the manifest contains relative paths (paths must be absolute)
        ValueError: If effective chunk size is positive and max_memory_bytes is less than chunk size

    Pipeline Architecture:
        The operation uses a multi-threaded pipeline with three stages:
        1. READ: Reads file chunks from disk into memory buffers
        2. HASH: Computes XXH128 hash of each chunk in memory
        3. UPLOAD: Writes the chunk to the data cache using the hash as the key

        Memory is bounded by max_memory_bytes. When the limit is reached,
        READ blocks until UPLOAD completes and frees memory.

        All work items (small files, large streaming files, and chunks) are
        processed through a SINGLE unified pipeline for maximum throughput.

    Chunking Behavior:
        - If effective chunk size is WHOLE_FILE_CHUNK_SIZE (-1): all files are hashed
          as a whole. For files larger than max_memory_bytes, the file is streamed for
          hashing (discarding data to avoid OOM), then streamed again for uploading.
        - If effective chunk size is a positive int: files larger than this size use
          chunked hashing. max_memory_bytes must be >= chunk size.

    Note:
        - Input manifest must have absolute paths (from collect_manifest)
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

    # Check caches and filter out fully cached items
    items_to_process: List[WorkItem] = []
    cached_results: Dict[
        str, Union[str, Dict[int, str]]
    ] = {}  # cache_key -> hash or {chunk_idx -> hash}

    for item in all_work_items:
        skip_pipeline = False

        if hash_cache is not None and not force_rehash:
            mtime_str = str(item.mtime) if item.mtime is not None else ""

            if isinstance(item, _StreamingWorkItem):
                # Whole file hash lookup
                hash_cache_entry = hash_cache.get_entry(
                    item.cache_key,
                    manifest.hashAlg,
                )
                if (
                    hash_cache_entry is not None
                    and hash_cache_entry.last_modified_time == mtime_str
                ):
                    cached_hash = hash_cache_entry.file_hash
                    if data_cache.object_exists(cached_hash, manifest.hashAlg.value):
                        skip_pipeline = True
                        cached_results[item.cache_key] = cached_hash
                        print_function_callback(
                            f"Fully cached (hash + data cache): {item.file_path}"
                        )
            elif isinstance(item, _ChunkWorkItem):
                if file_chunk_counts[item.cache_key] == 0:
                    # Whole file (single chunk)
                    hash_cache_entry = hash_cache.get_entry(
                        item.cache_key,
                        manifest.hashAlg,
                    )
                else:
                    # Chunked file
                    hash_cache_entry = hash_cache.get_entry(
                        item.cache_key,
                        manifest.hashAlg,
                        item.chunk_start,
                        item.chunk_end,
                    )

                if (
                    hash_cache_entry is not None
                    and hash_cache_entry.last_modified_time == mtime_str
                ):
                    cached_hash = hash_cache_entry.file_hash
                    if data_cache.object_exists(cached_hash, manifest.hashAlg.value):
                        skip_pipeline = True
                        if file_chunk_counts[item.cache_key] == 0:
                            # Whole file
                            cached_results[item.cache_key] = cached_hash
                        else:
                            # Chunked file
                            if item.cache_key not in cached_results:
                                cached_results[item.cache_key] = {}
                            chunk_dict = cached_results[item.cache_key]
                            if isinstance(chunk_dict, dict):
                                chunk_dict[item.chunk_index] = cached_hash
                        print_function_callback(
                            f"Fully cached (hash + data cache): {item.file_path}"
                            + (
                                f" chunk {item.chunk_index}"
                                if file_chunk_counts[item.cache_key] > 0
                                else ""
                            )
                        )

        if not skip_pipeline:
            items_to_process.append(item)

    # Run the unified pipeline
    if items_to_process:
        pipeline_results = _run_pipeline(
            work_items=items_to_process,
            hash_alg=manifest.hashAlg,
            data_cache=data_cache,
            account_id=account_id,
            max_memory_bytes=max_memory_bytes,
            progress_tracker=progress_tracker,
        )

        # Process results and update caches
        for item in pipeline_results:
            if isinstance(item, _StreamingWorkItem):
                if item.file_hash is not None:
                    cached_results[item.cache_key] = item.file_hash

                    # Update hash cache
                    if hash_cache is not None:
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
                    print_function_callback(f"Hashed and uploaded (streaming): {item.file_path}")

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

                    # Update hash cache
                    if hash_cache is not None:
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
                    print_function_callback(
                        f"Hashed and uploaded: {item.file_path}"
                        + (
                            f" chunk {item.chunk_index}"
                            if file_chunk_counts[item.cache_key] > 0
                            else ""
                        )
                    )

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
    return manifest_type(
        hash_alg=manifest.hashAlg,
        dirs=dir_entries,
        files=hashed_paths,
        total_size=total_size,
        parent_manifest_hash=manifest.parentManifestHash,
        file_chunk_size_bytes=output_chunk_size,
    )


def _run_pipeline(
    work_items: List[WorkItem],
    hash_alg: HashAlgorithm,
    data_cache: ContentAddressedDataCache,
    account_id: Optional[str],
    max_memory_bytes: int,
    progress_tracker: Optional[ProgressTracker],
) -> List[WorkItem]:
    """
    Run the READ -> HASH -> UPLOAD pipeline on work items.

    Handles both _ChunkWorkItem (in-memory processing) and _StreamingWorkItem
    (streaming processing for large files) in a single unified pipeline.

    Args:
        work_items: List of work items to process (chunks and/or streaming items)
        hash_alg: Hash algorithm to use
        data_cache: Content-addressable data cache for writes
        account_id: AWS account ID (for S3DataCache ExpectedBucketOwner)
        max_memory_bytes: Maximum memory for buffering
        progress_tracker: Optional progress tracker

    Returns:
        List of processed work items with hashes filled in
    """
    if not work_items:
        return []

    # Create queues for pipeline stages
    read_queue: queue.Queue[Any] = queue.Queue()
    hash_queue: queue.Queue[Any] = queue.Queue()
    upload_queue: queue.Queue[Any] = queue.Queue()
    result_queue: queue.Queue[Any] = queue.Queue()

    # Create memory pool
    memory_pool = _MemoryPool(max_memory_bytes)

    # Create pipeline stages
    read_stage = _ReadStage(read_queue, hash_queue, memory_pool)
    hash_stage = _HashStage(hash_queue, upload_queue, hash_alg)
    upload_stage = _UploadStage(
        upload_queue,
        result_queue,
        memory_pool,
        data_cache,
        hash_alg,
        account_id,
        progress_tracker,
    )

    # Start pipeline stages
    read_stage.start()
    hash_stage.start()
    upload_stage.start()

    # Feed work items to pipeline
    for item in work_items:
        read_queue.put(item)

    # Signal end of input
    read_queue.put(_SHUTDOWN_SENTINEL)

    # Collect results
    results: List[WorkItem] = []
    while True:
        try:
            item = result_queue.get(timeout=0.1)
            if item is _SHUTDOWN_SENTINEL:
                break
            results.append(item)
        except queue.Empty:
            # Check for errors in pipeline stages
            for stage in [read_stage, hash_stage, upload_stage]:
                error = stage.get_error()
                if error is not None:
                    # Signal shutdown to all stages
                    read_stage.signal_shutdown()
                    hash_stage.signal_shutdown()
                    upload_stage.signal_shutdown()
                    raise error
            continue

    # Wait for all stages to complete
    read_stage.join()
    hash_stage.join()
    upload_stage.join()

    # Check for any final errors
    for stage in [read_stage, hash_stage, upload_stage]:
        error = stage.get_error()
        if error is not None:
            raise error

    return results
