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
from typing import Any, Callable, Dict, List, Optional, Tuple
import logging

from botocore.exceptions import BotoCoreError, ClientError

from ..manifest import (
    FILE_CHUNK_SIZE_BYTES,
    AbsManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    _is_absolute_path,
)
from ..hash_algorithms import HashAlgorithm, hash_data
from ...caches.hash_cache import HashCache, HashCacheEntry
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
class _FileResult:
    """Result of processing a single file."""

    cache_key: str
    file_hash: Optional[str]  # For small files
    chunkhashes: Optional[List[str]]  # For large files (>256MB)
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
    """Pipeline stage that reads file chunks from disk."""

    def __init__(
        self,
        input_queue: "queue.Queue[_ChunkWorkItem]",
        output_queue: "queue.Queue[_ChunkWorkItem]",
        memory_pool: _MemoryPool,
    ) -> None:
        super().__init__("READ", input_queue, output_queue)
        self._memory_pool = memory_pool

    def _process(self, item: _ChunkWorkItem) -> _ChunkWorkItem:
        """Read chunk data from disk."""
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
    """Pipeline stage that computes hashes of chunks."""

    def __init__(
        self,
        input_queue: "queue.Queue[_ChunkWorkItem]",
        output_queue: "queue.Queue[_ChunkWorkItem]",
        hash_alg: HashAlgorithm,
    ) -> None:
        super().__init__("HASH", input_queue, output_queue)
        self._hash_alg = hash_alg

    def _process(self, item: _ChunkWorkItem) -> _ChunkWorkItem:
        """Compute hash of chunk data."""
        if item.data is not None:
            item.chunk_hash = hash_data(item.data, self._hash_alg)
        return item


class _UploadStage(_PipelineStage):
    """Pipeline stage that writes chunks to a data cache (S3 or filesystem)."""

    def __init__(
        self,
        input_queue: "queue.Queue[_ChunkWorkItem]",
        output_queue: "queue.Queue[_ChunkWorkItem]",
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

    def _process(self, item: _ChunkWorkItem) -> _ChunkWorkItem:
        """Write chunk to data cache."""
        if item.data is None or item.chunk_hash is None:
            return item

        chunk_size = len(item.data)

        try:
            if isinstance(self._data_cache, S3DataCache):
                self._process_s3(item)
            elif isinstance(self._data_cache, FileSystemDataCache):
                self._process_filesystem(item)
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

    def _process_s3(self, item: _ChunkWorkItem) -> None:
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

    def _process_filesystem(self, item: _ChunkWorkItem) -> None:
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

        Args:
            data: The data to upload
            s3_key: The S3 key to upload to

        Returns:
            True if the object was uploaded, False if it already existed

        Raises:
            TypeError: If data_cache is not an S3DataCache
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


def hash_upload_manifest(
    manifest: AbsManifest,
    data_cache: ContentAddressedDataCache,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    max_memory_bytes: Optional[int] = None,
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
        print_function_callback: Progress callback for status messages
        progress_tracker: Optional progress tracker for upload progress

    Returns:
        A NEW manifest of the same type with all hashes filled in

    Raises:
        ValueError: If the manifest contains relative paths (paths must be absolute)

    Pipeline Architecture:
        The operation uses a multi-threaded pipeline with three stages:
        1. READ: Reads file chunks from disk into memory buffers
        2. HASH: Computes XXH128 hash of each chunk in memory
        3. UPLOAD: Writes the chunk to the data cache using the hash as the key

        Memory is bounded by max_memory_bytes. When the limit is reached,
        READ blocks until UPLOAD completes and frees memory.

    Note:
        - Input manifest must have absolute paths (from collect_manifest)
        - Symlink entries are unchanged (they have symlink_target, not hash)
        - Directory entries are unchanged (they have no hash)
        - Deleted entries are unchanged (they mark deletions, no hash needed)
        - For large files (>256MB): computes chunkhashes and writes each chunk
        - Returns a NEW manifest (does not mutate input)
    """
    # Validate that manifest has absolute paths
    _validate_absolute_paths(manifest)

    # Set up memory limit
    if max_memory_bytes is None:
        max_memory_bytes = _get_default_max_memory_bytes()

    # Get account_id for S3DataCache (used for ExpectedBucketOwner)
    account_id: Optional[str] = None
    if isinstance(data_cache, S3DataCache):
        # Import here to avoid circular dependency and only when needed
        from ..._aws.aws_clients import get_account_id, get_boto3_session

        # Try to get account ID from the S3 client's session
        try:
            # Create a session from the client's credentials if possible
            session = get_boto3_session()
            account_id = get_account_id(session=session)
        except Exception:
            # If we can't get account ID, proceed without ExpectedBucketOwner
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

    # Create work items for all file chunks
    work_items: List[_ChunkWorkItem] = []
    # Map from cache_key to list of chunk indices in work_items
    file_chunk_map: Dict[str, List[int]] = {}

    for idx, entry in file_entries_to_process:
        abs_path = Path(entry.path)
        # Use resolved path as cache key for consistency
        cache_key = str(abs_path.resolve())
        file_size = entry.size or 0

        if file_size > FILE_CHUNK_SIZE_BYTES:
            # Large file: create multiple chunk work items
            chunk_indices: List[int] = []
            offset = 0
            chunk_idx = 0
            while offset < file_size:
                chunk_end = min(offset + FILE_CHUNK_SIZE_BYTES, file_size)
                work_items.append(
                    _ChunkWorkItem(
                        file_path=abs_path,
                        cache_key=cache_key,
                        file_size=file_size,
                        mtime=entry.mtime or 0,
                        chunk_index=chunk_idx,
                        chunk_start=offset,
                        chunk_end=chunk_end,
                    )
                )
                chunk_indices.append(len(work_items) - 1)
                offset = chunk_end
                chunk_idx += 1
            file_chunk_map[cache_key] = chunk_indices
        else:
            # Small file: single chunk
            work_items.append(
                _ChunkWorkItem(
                    file_path=abs_path,
                    cache_key=cache_key,
                    file_size=file_size,
                    mtime=entry.mtime or 0,
                    chunk_index=0,
                    chunk_start=0,
                    chunk_end=file_size,
                )
            )
            file_chunk_map[cache_key] = [len(work_items) - 1]

    # Check caches - for S3DataCache, skip if BOTH hash cache AND s3 check cache hit
    # For FileSystemDataCache, skip if hash cache hits AND file exists
    cached_chunk_hashes: Dict[str, Dict[int, str]] = {}  # cache_key -> {chunk_idx -> hash}
    items_to_process: List[_ChunkWorkItem] = []

    for item in work_items:
        skip_pipeline = False

        if hash_cache is not None and not force_rehash:
            mtime_str = str(item.mtime) if item.mtime is not None else ""
            hash_cache_entry = hash_cache.get_entry(
                item.cache_key,
                manifest.hashAlg,
                item.chunk_start,
                item.chunk_end,
            )
            if hash_cache_entry is not None and hash_cache_entry.last_modified_time == mtime_str:
                cached_hash = hash_cache_entry.file_hash

                # Check if object exists in data cache
                if data_cache.object_exists(cached_hash, manifest.hashAlg.value):
                    # Both caches hit - skip the pipeline entirely
                    skip_pipeline = True
                    if item.cache_key not in cached_chunk_hashes:
                        cached_chunk_hashes[item.cache_key] = {}
                    cached_chunk_hashes[item.cache_key][item.chunk_index] = cached_hash
                    print_function_callback(
                        f"Fully cached (hash + data cache): {item.file_path} chunk {item.chunk_index}"
                    )

        if not skip_pipeline:
            items_to_process.append(item)

    # Process remaining items through pipeline
    pipeline_results = _run_pipeline(
        work_items=items_to_process,
        hash_alg=manifest.hashAlg,
        data_cache=data_cache,
        account_id=account_id,
        max_memory_bytes=max_memory_bytes,
        progress_tracker=progress_tracker,
    )

    # Collect pipeline results
    pipeline_chunk_hashes: Dict[str, Dict[int, str]] = {}
    for item in pipeline_results:
        if item.chunk_hash is not None:
            if item.cache_key not in pipeline_chunk_hashes:
                pipeline_chunk_hashes[item.cache_key] = {}
            pipeline_chunk_hashes[item.cache_key][item.chunk_index] = item.chunk_hash

            # Update hash cache
            if hash_cache is not None:
                mtime_str = str(item.mtime) if item.mtime is not None else ""
                hash_cache.put_entry(
                    HashCacheEntry(
                        file_path=item.cache_key,
                        hash_algorithm=manifest.hashAlg,
                        file_hash=item.chunk_hash,
                        last_modified_time=mtime_str,
                        range_start=item.chunk_start,
                        range_end=item.chunk_end,
                    )
                )
            print_function_callback(
                f"Hashed and uploaded: {item.file_path} chunk {item.chunk_index}"
            )

    # Merge cached and pipeline results
    all_chunk_hashes: Dict[str, Dict[int, str]] = {}
    for cache_key in file_chunk_map:
        all_chunk_hashes[cache_key] = {}
        if cache_key in cached_chunk_hashes:
            all_chunk_hashes[cache_key].update(cached_chunk_hashes[cache_key])
        if cache_key in pipeline_chunk_hashes:
            all_chunk_hashes[cache_key].update(pipeline_chunk_hashes[cache_key])

    # Build result manifest
    hashed_paths: List[ManifestFilePath] = []
    total_size = 0

    # Process file entries
    for idx, entry in file_entries_to_process:
        abs_path = Path(entry.path)
        cache_key = str(abs_path.resolve())
        file_size = entry.size or 0
        chunk_hashes = all_chunk_hashes.get(cache_key, {})

        if file_size > FILE_CHUNK_SIZE_BYTES:
            # Large file: use chunkhashes
            expected_chunks = (file_size + FILE_CHUNK_SIZE_BYTES - 1) // FILE_CHUNK_SIZE_BYTES
            # All chunks must have been hashed
            chunkhashes_list: List[str] = []
            for i in range(expected_chunks):
                chunk_hash = chunk_hashes.get(i)
                if chunk_hash is None:
                    raise ValueError(
                        f"Internal error: chunk {i} of file '{entry.path}' was not hashed"
                    )
                chunkhashes_list.append(chunk_hash)
            hashed_paths.append(
                ManifestFilePath(
                    path=entry.path,
                    chunkhashes=chunkhashes_list,
                    size=entry.size,
                    mtime=entry.mtime,
                    runnable=entry.runnable,
                )
            )
        else:
            # Small file: single hash
            # Hash must have been computed
            file_hash = chunk_hashes.get(0)
            if file_hash is None:
                raise ValueError(f"Internal error: file '{entry.path}' was not hashed")
            hashed_paths.append(
                ManifestFilePath(
                    path=entry.path,
                    hash=file_hash,
                    size=entry.size,
                    mtime=entry.mtime,
                    runnable=entry.runnable,
                )
            )

        if entry.size is not None:
            total_size += entry.size

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
    )


def _run_pipeline(
    work_items: List[_ChunkWorkItem],
    hash_alg: HashAlgorithm,
    data_cache: ContentAddressedDataCache,
    account_id: Optional[str],
    max_memory_bytes: int,
    progress_tracker: Optional[ProgressTracker],
) -> List[_ChunkWorkItem]:
    """
    Run the READ -> HASH -> UPLOAD pipeline on work items.

    Args:
        work_items: List of chunk work items to process
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
    results: List[_ChunkWorkItem] = []
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
