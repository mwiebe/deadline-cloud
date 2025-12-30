# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for filling in hashes AND uploading file content to S3 in a pipelined manner.

This module implements the HASH_UPLOAD operation from the composable manifest operations design:
    HASH_UPLOAD: Manifest (with hash="") → Manifest (with hashes filled in) + S3 uploads

The operation combines hashing and uploading into a single pass over the data:
- Reads file chunks into memory
- Hashes each chunk
- Uploads to S3 before freeing memory

This avoids reading files twice (once for hashing, once for uploading) and provides
significant performance improvements for large datasets.

The pipeline uses bounded memory to prevent OOM conditions:
- READ stage reads chunks into a memory pool
- HASH stage computes hashes in-place
- UPLOAD stage uploads and releases memory
- When memory limit is reached, READ blocks until UPLOAD frees space
"""

from __future__ import annotations

import os
import time
import threading
import queue
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple
import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from ..base_manifest import BaseAssetManifest, FILE_CHUNK_SIZE_BYTES
from ..hash_algorithms import HashAlgorithm, hash_data
from ..versions import ManifestVersion
from ..v2023_03_03.asset_manifest import (
    AssetManifest as AssetManifest2023,
    ManifestPath as ManifestPath2023,
)
from ..v2025_12_04.asset_manifest import (
    AssetManifest as AssetManifest2025,
    ManifestDirectoryPath as ManifestDirectoryPath2025,
    ManifestFilePath as ManifestFilePath2025,
)
from ...caches.hash_cache import HashCache, HashCacheEntry, WHOLE_FILE_RANGE_END
from ...caches.s3_check_cache import S3CheckCache, S3CheckCacheEntry
from ..._aws.aws_clients import get_boto3_session, get_s3_client, get_account_id
from ...progress_tracker import ProgressTracker
from ...exceptions import (
    AssetSyncCancelledError,
    AssetSyncError,
    JobAttachmentsS3ClientError,
    JobAttachmentS3BotoCoreError,
    COMMON_ERROR_GUIDANCE_FOR_S3,
)

logger = logging.getLogger("deadline.job_attachments.hash_upload")

# Minimum memory limit: 256MB (one chunk)
MIN_MEMORY_BYTES = 256 * 1024 * 1024

# Sentinel value to signal pipeline shutdown
_SHUTDOWN_SENTINEL = object()


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
            f"Memory limit calculation: min=256MB, quarter_total={quarter_of_total // (1024*1024)}MB, "
            f"available-1GB={available_minus_1gb // (1024*1024)}MB, result={result // (1024*1024)}MB"
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
    rel_path: str  # Relative path for manifest entry
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

    rel_path: str
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
        except Exception as e:
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
    """Pipeline stage that uploads chunks to S3."""

    def __init__(
        self,
        input_queue: "queue.Queue[_ChunkWorkItem]",
        output_queue: "queue.Queue[_ChunkWorkItem]",
        memory_pool: _MemoryPool,
        s3_client: Any,
        s3_bucket: str,
        s3_key_prefix: str,
        hash_alg: HashAlgorithm,
        s3_check_cache: Optional[S3CheckCache],
        account_id: str,
        progress_tracker: Optional[ProgressTracker],
    ) -> None:
        super().__init__("UPLOAD", input_queue, output_queue)
        self._memory_pool = memory_pool
        self._s3_client = s3_client
        self._s3_bucket = s3_bucket
        self._s3_key_prefix = s3_key_prefix
        self._hash_alg = hash_alg
        self._s3_check_cache = s3_check_cache
        self._account_id = account_id
        self._progress_tracker = progress_tracker

    def _process(self, item: _ChunkWorkItem) -> _ChunkWorkItem:
        """Upload chunk to S3."""
        if item.data is None or item.chunk_hash is None:
            return item

        chunk_size = len(item.data)
        s3_key = f"{self._s3_key_prefix}/{item.chunk_hash}.{self._hash_alg.value}"
        cache_key = f"{self._s3_bucket}/{s3_key}"

        try:
            # Check S3 cache first
            if self._s3_check_cache is not None:
                cache_entry = self._s3_check_cache.get_entry(cache_key)
                if cache_entry is not None:
                    item.skipped = True
                    item.uploaded = False
                    logger.debug(f"Skipping upload (cached): {s3_key}")
                    return item

            # Upload to S3 with conditional write (only if object doesn't exist)
            uploaded = self._upload_to_s3_if_not_exists(item.data, s3_key)
            item.uploaded = uploaded
            item.skipped = not uploaded
            if uploaded:
                logger.debug(f"Uploaded: {s3_key}")
            else:
                logger.debug(f"Skipping upload (exists): {s3_key}")

            # Update S3 check cache
            if self._s3_check_cache is not None:
                self._s3_check_cache.put_entry(
                    S3CheckCacheEntry(s3_key=cache_key, last_seen_time=str(time.time()))
                )

            # Update progress
            if self._progress_tracker is not None:
                self._progress_tracker.track_progress_callback(chunk_size)

        finally:
            # Release memory after upload completes
            self._memory_pool.release(chunk_size)
            item.data = None  # Free the data

        return item

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
        """
        try:
            self._s3_client.put_object(
                Bucket=self._s3_bucket,
                Key=s3_key,
                Body=data,
                ExpectedBucketOwner=self._account_id,
                IfNoneMatch="*",
            )
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
                bucket_name=self._s3_bucket,
                key_or_prefix=s3_key,
                message=f"{status_code_guidance.get(status_code, '')} {str(exc)}",
            ) from exc
        except BotoCoreError as bce:
            raise JobAttachmentS3BotoCoreError(
                action="uploading chunk",
                error_details=str(bce),
            ) from bce


def _hash_upload_manifest(
    manifest: BaseAssetManifest,
    root: Path | str,
    s3_bucket: str,
    s3_key_prefix: str,
    boto3_session: Optional[boto3.Session] = None,
    hash_cache: Optional[HashCache] = None,
    s3_check_cache: Optional[S3CheckCache] = None,
    force_rehash: bool = False,
    max_memory_bytes: Optional[int] = None,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
    progress_tracker: Optional[ProgressTracker] = None,
) -> BaseAssetManifest:
    """
    Fill in hashes for a manifest AND upload file content to S3 in a pipelined manner.

    This operation combines hashing and uploading into a single pass over the data,
    avoiding the need to read files twice (once for hashing, once for uploading).

    Args:
        manifest: Manifest with empty hashes (from _collect_manifest_directory_tree)
        root: Root directory path (needed to read files for hashing/uploading)
        s3_bucket: S3 bucket name for uploads
        s3_key_prefix: S3 key prefix for content-addressable storage (e.g., "Data")
        boto3_session: Optional boto3 session for AWS credentials
        hash_cache: Optional hash cache for efficiency
        s3_check_cache: Optional S3 check cache to skip already-uploaded files
        force_rehash: If True, ignore cache and recalculate all hashes
        max_memory_bytes: Maximum memory to use for buffering (default: auto-detect)
        print_function_callback: Progress callback for status messages
        progress_tracker: Optional progress tracker for upload progress

    Returns:
        A NEW manifest with all hashes filled in

    Pipeline Architecture:
        The operation uses a multi-threaded pipeline with three stages:
        1. READ: Reads file chunks from disk into memory buffers
        2. HASH: Computes XXH128 hash of each chunk in memory
        3. UPLOAD: Uploads the chunk to S3 using the hash as the object key

        Memory is bounded by max_memory_bytes. When the limit is reached,
        READ blocks until UPLOAD completes and frees memory.

    Note:
        - Symlink entries are unchanged (they have symlink_target, not hash)
        - Directory entries are unchanged (they have no hash)
        - For v2025 large files (>256MB): computes chunkhashes and uploads each chunk
        - Returns a NEW manifest (does not mutate input)
    """
    root_path = Path(os.path.normpath(os.path.abspath(root)))

    # Set up memory limit
    if max_memory_bytes is None:
        max_memory_bytes = _get_default_max_memory_bytes()

    # Set up boto3 session and S3 client
    if boto3_session is None:
        boto3_session = get_boto3_session()
    s3_client = get_s3_client(boto3_session)
    account_id = get_account_id(session=boto3_session)

    if manifest.manifestVersion == ManifestVersion.v2023_03_03:
        if not isinstance(manifest, AssetManifest2023):
            raise TypeError(
                f"Expected AssetManifest2023 for version {manifest.manifestVersion}, "
                f"got {type(manifest).__name__}"
            )
        return _hash_upload_manifest_v2023(
            manifest=manifest,
            root_path=root_path,
            s3_bucket=s3_bucket,
            s3_key_prefix=s3_key_prefix,
            s3_client=s3_client,
            account_id=account_id,
            hash_cache=hash_cache,
            s3_check_cache=s3_check_cache,
            force_rehash=force_rehash,
            max_memory_bytes=max_memory_bytes,
            print_function_callback=print_function_callback,
            progress_tracker=progress_tracker,
        )
    elif manifest.manifestVersion == ManifestVersion.v2025_12_04_beta:
        if not isinstance(manifest, AssetManifest2025):
            raise TypeError(
                f"Expected AssetManifest2025 for version {manifest.manifestVersion}, "
                f"got {type(manifest).__name__}"
            )
        return _hash_upload_manifest_v2025(
            manifest=manifest,
            root_path=root_path,
            s3_bucket=s3_bucket,
            s3_key_prefix=s3_key_prefix,
            s3_client=s3_client,
            account_id=account_id,
            hash_cache=hash_cache,
            s3_check_cache=s3_check_cache,
            force_rehash=force_rehash,
            max_memory_bytes=max_memory_bytes,
            print_function_callback=print_function_callback,
            progress_tracker=progress_tracker,
        )
    else:
        raise ValueError(f"Unsupported manifest version: {manifest.manifestVersion}")


def _hash_upload_manifest_v2023(
    manifest: AssetManifest2023,
    root_path: Path,
    s3_bucket: str,
    s3_key_prefix: str,
    s3_client: Any,
    account_id: str,
    hash_cache: Optional[HashCache],
    s3_check_cache: Optional[S3CheckCache],
    force_rehash: bool,
    max_memory_bytes: int,
    print_function_callback: Callable[[Any], None],
    progress_tracker: Optional[ProgressTracker],
) -> AssetManifest2023:
    """
    Fill in hashes and upload for a v2023-03-03 manifest.

    v2023 format only supports regular files with single hashes (no chunking).

    Caching logic:
    - Only skip the pipeline if BOTH hash cache AND s3 check cache hit
    - Otherwise, always read → hash → upload for data consistency
    - xxh128 is fast enough that re-hashing is preferred over trusting stale cache
    """
    # Create work items for all files
    work_items: List[_ChunkWorkItem] = []
    for entry in manifest.paths:
        abs_path = root_path / entry.path
        work_items.append(
            _ChunkWorkItem(
                file_path=abs_path,
                rel_path=entry.path,
                file_size=entry.size,
                mtime=entry.mtime,
                chunk_index=0,
                chunk_start=0,
                chunk_end=entry.size,
            )
        )

    # Check caches - only skip if BOTH hash cache AND s3 check cache hit
    cached_results: dict[str, str] = {}
    items_to_process: List[_ChunkWorkItem] = []

    for item in work_items:
        skip_pipeline = False
        cached_hash: Optional[str] = None

        if hash_cache is not None and s3_check_cache is not None and not force_rehash:
            mtime_str = str(item.mtime) if item.mtime is not None else ""
            hash_cache_entry = hash_cache.get_entry(
                item.rel_path, manifest.hashAlg, 0, WHOLE_FILE_RANGE_END
            )
            if hash_cache_entry is not None and hash_cache_entry.last_modified_time == mtime_str:
                cached_hash = hash_cache_entry.file_hash
                # Check S3 cache
                s3_key = f"{s3_key_prefix}/{cached_hash}.{manifest.hashAlg.value}"
                s3_cache_key = f"{s3_bucket}/{s3_key}"
                s3_cache_entry = s3_check_cache.get_entry(s3_cache_key)
                if s3_cache_entry is not None:
                    # Both caches hit - skip the pipeline entirely
                    skip_pipeline = True
                    cached_results[item.rel_path] = cached_hash
                    print_function_callback(f"Fully cached (hash + S3): {item.rel_path}")

        if not skip_pipeline:
            items_to_process.append(item)

    # Process remaining items through pipeline
    pipeline_results = _run_pipeline(
        work_items=items_to_process,
        hash_alg=manifest.hashAlg,
        s3_bucket=s3_bucket,
        s3_key_prefix=s3_key_prefix,
        s3_client=s3_client,
        account_id=account_id,
        s3_check_cache=s3_check_cache,
        max_memory_bytes=max_memory_bytes,
        progress_tracker=progress_tracker,
    )

    # Merge cached and pipeline results
    all_hashes: dict[str, str] = {**cached_results}
    for item in pipeline_results:
        if item.chunk_hash is not None:
            all_hashes[item.rel_path] = item.chunk_hash
            # Update hash cache
            if hash_cache is not None:
                mtime_str = str(item.mtime) if item.mtime is not None else ""
                hash_cache.put_entry(
                    HashCacheEntry(
                        file_path=item.rel_path,
                        hash_algorithm=manifest.hashAlg,
                        file_hash=item.chunk_hash,
                        last_modified_time=mtime_str,
                        range_start=0,
                        range_end=WHOLE_FILE_RANGE_END,
                    )
                )
            print_function_callback(f"Hashed and uploaded: {item.rel_path}")

    # Build result manifest
    hashed_paths: List[ManifestPath2023] = []
    total_size = 0

    for entry in manifest.paths:
        file_hash = all_hashes.get(entry.path, "")
        hashed_paths.append(
            ManifestPath2023(
                path=entry.path,
                hash=file_hash,
                size=entry.size,
                mtime=entry.mtime,
            )
        )
        total_size += entry.size

    return AssetManifest2023(
        hash_alg=manifest.hashAlg,
        paths=hashed_paths,
        total_size=total_size,
    )


def _hash_upload_manifest_v2025(
    manifest: AssetManifest2025,
    root_path: Path,
    s3_bucket: str,
    s3_key_prefix: str,
    s3_client: Any,
    account_id: str,
    hash_cache: Optional[HashCache],
    s3_check_cache: Optional[S3CheckCache],
    force_rehash: bool,
    max_memory_bytes: int,
    print_function_callback: Callable[[Any], None],
    progress_tracker: Optional[ProgressTracker],
) -> AssetManifest2025:
    """
    Fill in hashes and upload for a v2025-12-04-beta manifest.

    Handles:
    - Regular files: single hash or chunkhashes (>256MB)
    - Symlinks: unchanged (no hash needed)
    - Directories: unchanged (no hash needed)

    Caching logic:
    - Only skip the pipeline if BOTH hash cache AND s3 check cache hit
    - Otherwise, always read → hash → upload for data consistency
    - xxh128 is fast enough that re-hashing is preferred over trusting stale cache
    """
    # Separate entries by type
    file_entries_to_process: List[Tuple[int, ManifestFilePath2025]] = []
    symlink_entries: List[Tuple[int, ManifestFilePath2025]] = []
    deleted_entries: List[Tuple[int, ManifestFilePath2025]] = []

    for idx, entry in enumerate(manifest.paths):
        if entry.symlink_target is not None:
            symlink_entries.append((idx, entry))
        elif entry.deleted:
            deleted_entries.append((idx, entry))
        else:
            file_entries_to_process.append((idx, entry))

    # Create work items for all file chunks
    work_items: List[_ChunkWorkItem] = []
    # Map from rel_path to list of chunk indices in work_items
    file_chunk_map: dict[str, List[int]] = {}

    for idx, entry in file_entries_to_process:
        abs_path = root_path / entry.path
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
                        rel_path=entry.path,
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
            file_chunk_map[entry.path] = chunk_indices
        else:
            # Small file: single chunk
            work_items.append(
                _ChunkWorkItem(
                    file_path=abs_path,
                    rel_path=entry.path,
                    file_size=file_size,
                    mtime=entry.mtime or 0,
                    chunk_index=0,
                    chunk_start=0,
                    chunk_end=file_size,
                )
            )
            file_chunk_map[entry.path] = [len(work_items) - 1]

    # Check caches - only skip if BOTH hash cache AND s3 check cache hit
    cached_chunk_hashes: dict[str, dict[int, str]] = {}  # rel_path -> {chunk_idx -> hash}
    items_to_process: List[_ChunkWorkItem] = []

    for item in work_items:
        skip_pipeline = False

        if hash_cache is not None and s3_check_cache is not None and not force_rehash:
            mtime_str = str(item.mtime) if item.mtime is not None else ""
            hash_cache_entry = hash_cache.get_entry(
                item.rel_path,
                manifest.hashAlg,
                item.chunk_start,
                item.chunk_end,
            )
            if hash_cache_entry is not None and hash_cache_entry.last_modified_time == mtime_str:
                cached_hash = hash_cache_entry.file_hash
                # Check S3 cache
                s3_key = f"{s3_key_prefix}/{cached_hash}.{manifest.hashAlg.value}"
                s3_cache_key = f"{s3_bucket}/{s3_key}"
                s3_cache_entry = s3_check_cache.get_entry(s3_cache_key)
                if s3_cache_entry is not None:
                    # Both caches hit - skip the pipeline entirely
                    skip_pipeline = True
                    if item.rel_path not in cached_chunk_hashes:
                        cached_chunk_hashes[item.rel_path] = {}
                    cached_chunk_hashes[item.rel_path][item.chunk_index] = cached_hash
                    print_function_callback(
                        f"Fully cached (hash + S3): {item.rel_path} chunk {item.chunk_index}"
                    )

        if not skip_pipeline:
            items_to_process.append(item)

    # Process remaining items through pipeline
    pipeline_results = _run_pipeline(
        work_items=items_to_process,
        hash_alg=manifest.hashAlg,
        s3_bucket=s3_bucket,
        s3_key_prefix=s3_key_prefix,
        s3_client=s3_client,
        account_id=account_id,
        s3_check_cache=s3_check_cache,
        max_memory_bytes=max_memory_bytes,
        progress_tracker=progress_tracker,
    )

    # Collect pipeline results
    pipeline_chunk_hashes: dict[str, dict[int, str]] = {}
    for item in pipeline_results:
        if item.chunk_hash is not None:
            if item.rel_path not in pipeline_chunk_hashes:
                pipeline_chunk_hashes[item.rel_path] = {}
            pipeline_chunk_hashes[item.rel_path][item.chunk_index] = item.chunk_hash

            # Update hash cache
            if hash_cache is not None:
                mtime_str = str(item.mtime) if item.mtime is not None else ""
                hash_cache.put_entry(
                    HashCacheEntry(
                        file_path=item.rel_path,
                        hash_algorithm=manifest.hashAlg,
                        file_hash=item.chunk_hash,
                        last_modified_time=mtime_str,
                        range_start=item.chunk_start,
                        range_end=item.chunk_end,
                    )
                )
            print_function_callback(
                f"Hashed and uploaded: {item.rel_path} chunk {item.chunk_index}"
            )

    # Merge cached and pipeline results
    all_chunk_hashes: dict[str, dict[int, str]] = {}
    for rel_path in file_chunk_map:
        all_chunk_hashes[rel_path] = {}
        if rel_path in cached_chunk_hashes:
            all_chunk_hashes[rel_path].update(cached_chunk_hashes[rel_path])
        if rel_path in pipeline_chunk_hashes:
            all_chunk_hashes[rel_path].update(pipeline_chunk_hashes[rel_path])

    # Build result manifest
    hashed_paths: List[ManifestFilePath2025] = []
    total_size = 0

    # Process file entries
    for idx, entry in file_entries_to_process:
        file_size = entry.size or 0
        chunk_hashes = all_chunk_hashes.get(entry.path, {})

        if file_size > FILE_CHUNK_SIZE_BYTES:
            # Large file: use chunkhashes
            expected_chunks = (file_size + FILE_CHUNK_SIZE_BYTES - 1) // FILE_CHUNK_SIZE_BYTES
            chunkhashes_list = [
                chunk_hashes.get(i, "") for i in range(expected_chunks)
            ]
            hashed_paths.append(
                ManifestFilePath2025(
                    path=entry.path,
                    chunkhashes=chunkhashes_list,
                    size=entry.size,
                    mtime=entry.mtime,
                    runnable=getattr(entry, "runnable", False),
                )
            )
        else:
            # Small file: single hash
            file_hash = chunk_hashes.get(0, "")
            hashed_paths.append(
                ManifestFilePath2025(
                    path=entry.path,
                    hash=file_hash,
                    size=entry.size,
                    mtime=entry.mtime,
                    runnable=getattr(entry, "runnable", False),
                )
            )

        if entry.size is not None:
            total_size += entry.size

    # Add symlink entries unchanged
    for idx, entry in symlink_entries:
        hashed_paths.append(
            ManifestFilePath2025(
                path=entry.path,
                symlink_target=entry.symlink_target,
            )
        )

    # Add deleted entries unchanged
    for idx, entry in deleted_entries:
        hashed_paths.append(
            ManifestFilePath2025(
                path=entry.path,
                deleted=True,
            )
        )

    # Copy directory entries unchanged
    dir_entries: List[ManifestDirectoryPath2025] = []
    for d in manifest.dirs:
        dir_entries.append(
            ManifestDirectoryPath2025(
                path=d.path,
                deleted=d.deleted,
            )
        )

    return AssetManifest2025(
        hash_alg=manifest.hashAlg,
        dirs=dir_entries,
        paths=hashed_paths,
        total_size=total_size,
        manifest_type=manifest.manifestType,
        parent_manifest_hash=manifest.parentManifestHash,
    )


def _run_pipeline(
    work_items: List[_ChunkWorkItem],
    hash_alg: HashAlgorithm,
    s3_bucket: str,
    s3_key_prefix: str,
    s3_client: Any,
    account_id: str,
    s3_check_cache: Optional[S3CheckCache],
    max_memory_bytes: int,
    progress_tracker: Optional[ProgressTracker],
) -> List[_ChunkWorkItem]:
    """
    Run the READ -> HASH -> UPLOAD pipeline on work items.

    Args:
        work_items: List of chunk work items to process
        hash_alg: Hash algorithm to use
        s3_bucket: S3 bucket for uploads
        s3_key_prefix: S3 key prefix for CAS
        s3_client: Boto3 S3 client
        account_id: AWS account ID
        s3_check_cache: Optional S3 check cache
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
        s3_client,
        s3_bucket,
        s3_key_prefix,
        hash_alg,
        s3_check_cache,
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
