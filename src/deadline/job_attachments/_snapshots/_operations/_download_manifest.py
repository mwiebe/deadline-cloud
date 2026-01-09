# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for downloading files from a content-addressable data cache to local filesystem.

This module implements the DOWNLOAD operation from the composable manifest operations design:
    DOWNLOAD: (AbsManifest, DataCache) → DownloadResult

The operation downloads files from a data cache (S3 or filesystem) to the local filesystem,
recreating the directory structure specified in the manifest. The manifest must have
absolute paths - each file's path in the manifest is the exact location where it will
be written on the local filesystem.

Key features:
- Parallel downloads for improved throughput (both regular and chunked files)
- Support for chunked large files (>256MB) with parallel chunk downloads
- Asyncio-based coordination for chunked file finalization
- File conflict resolution (skip, overwrite, create copy)
- Progress tracking and cancellation support
- Symlink creation
- Diff manifest support (applies deletions)
- Modification time restoration
- Returns updated manifest with local filesystem timestamps for reliable diff operations
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable, DefaultDict, Dict, List, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from .._manifest import (
    AbsDiffManifest,
    AbsManifest,
    AbsSnapshotManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    _is_absolute_path,
    SymlinkPolicy,
)
from ...asset_manifests.hash_algorithms import HashAlgorithm
from .._content_addressed_data_cache import (
    ContentAddressedDataCache,
    S3DataCache,
    FileSystemDataCache,
)
from ...models import FileConflictResolution
from ...progress_tracker import (
    DownloadSummaryStatistics,
    ProgressStatus,
    ProgressTracker,
)
from ...exceptions import (
    AssetSyncCancelledError,
    JobAttachmentsS3ClientError,
    JobAttachmentS3BotoCoreError,
)
from ..._utils import _get_long_path_compatible_path
from ...caches.hash_cache import HashCache, HashCacheEntry, WHOLE_FILE_RANGE_END

# Import S3-specific functions
from ._download_manifest_s3 import (
    download_s3_multipart_async,
    download_s3_chunk_to_offset,
    download_s3_chunk_multipart_async,
    MIN_SIZE_FOR_MULTIPART_DOWNLOAD,
)

# Import filesystem-specific functions
from ._download_manifest_file_system import (
    download_fs_chunk_to_offset,
)

logger = logging.getLogger("deadline.job_attachments.download")

# Default number of parallel download workers
DEFAULT_MAX_WORKERS = 10


@dataclass
class DownloadResult:
    """
    Result of a download_manifest operation.

    Attributes:
        statistics: Summary statistics about the download operation.
        manifest: A copy of the input manifest with mtime values updated to match
            the actual local filesystem timestamps. This is useful for cross-OS
            scenarios where file system mtime precision differs (e.g., a snapshot
            created on Linux with nanosecond precision used on Windows with
            100-nanosecond precision). Using this updated manifest as the basis
            for subsequent diff operations ensures reliable change detection.
    """

    statistics: DownloadSummaryStatistics
    manifest: AbsManifest


def _validate_absolute_paths(manifest: AbsManifest) -> None:
    """Validate that all paths in the manifest are absolute."""
    for entry in manifest.files:
        if not _is_absolute_path(entry.path):
            raise ValueError(
                f"DOWNLOAD operation requires absolute paths. "
                f"Found relative path: '{entry.path}'. "
                f"Use join_manifest() to create a manifest with absolute paths."
            )

    for d in manifest.dirs:
        if not _is_absolute_path(d.path):
            raise ValueError(
                f"DOWNLOAD operation requires absolute paths. "
                f"Found relative directory path: '{d.path}'. "
                f"Use join_manifest() to create a manifest with absolute paths."
            )


def _check_hash_cache_for_skip(
    entry: ManifestFilePath,
    hash_alg: HashAlgorithm,
    hash_cache: Optional[HashCache],
) -> Tuple[bool, Optional[int]]:
    """
    Check if a file can be skipped because it already exists with the correct hash.

    Uses the hash cache to check if the local file's cached hash matches the
    expected hash from the manifest. This avoids re-downloading files that
    already have the correct content.

    Args:
        entry: The manifest file entry to check
        hash_alg: The hash algorithm used in the manifest
        hash_cache: Optional hash cache to check against

    Returns:
        Tuple of (can_skip, actual_mtime_us):
        - can_skip: True if the file exists and has the correct hash
        - actual_mtime_us: The file's mtime in microseconds if can_skip is True, else None
    """
    if hash_cache is None or entry.hash is None:
        return (False, None)

    local_path = _get_long_path_compatible_path(Path(entry.path))

    # File must exist to skip
    if not local_path.exists() or not local_path.is_file():
        return (False, None)

    # Get the file's current mtime
    try:
        stat_result = local_path.stat()
        current_mtime_ns = stat_result.st_mtime_ns
        current_mtime_str = str(current_mtime_ns)
    except OSError:
        return (False, None)

    # Check the hash cache for this file
    # Use resolved path because hash cache always uses resolved paths
    resolved_path = str(Path(entry.path).resolve())
    cache_entry = hash_cache.get_entry(
        file_path_key=resolved_path,
        hash_algorithm=hash_alg,
        range_start=0,
        range_end=WHOLE_FILE_RANGE_END,
    )

    if cache_entry is None:
        return (False, None)

    # Check if the cached mtime matches the current file mtime
    if cache_entry.last_modified_time != current_mtime_str:
        return (False, None)

    # Check if the cached hash matches the expected hash
    if cache_entry.file_hash != entry.hash:
        return (False, None)

    # File exists with correct hash - can skip download
    actual_mtime_us = current_mtime_ns // 1_000
    logger.debug(f"Skipping download of {entry.path} - hash cache indicates file is up to date")
    return (True, actual_mtime_us)


def _check_hash_cache_for_chunked_skip(
    entry: ManifestFilePath,
    hash_alg: HashAlgorithm,
    hash_cache: Optional[HashCache],
    chunk_size_bytes: int,
) -> Tuple[bool, Optional[int]]:
    """
    Check if a chunked file can be skipped because it already exists with correct chunk hashes.

    For chunked files, we check each chunk's hash in the hash cache. If all chunks
    have matching hashes with the same mtime, the file can be skipped.

    Args:
        entry: The manifest file entry with chunkhashes to check
        hash_alg: The hash algorithm used in the manifest
        hash_cache: Optional hash cache to check against
        chunk_size_bytes: The chunk size in bytes from the manifest

    Returns:
        Tuple of (can_skip, actual_mtime_us):
        - can_skip: True if the file exists and all chunk hashes match
        - actual_mtime_us: The file's mtime in microseconds if can_skip is True, else None
    """
    if hash_cache is None or entry.chunkhashes is None:
        return (False, None)

    local_path = _get_long_path_compatible_path(Path(entry.path))

    # File must exist to skip
    if not local_path.exists() or not local_path.is_file():
        return (False, None)

    # Get the file's current mtime
    try:
        stat_result = local_path.stat()
        current_mtime_ns = stat_result.st_mtime_ns
        current_mtime_str = str(current_mtime_ns)
    except OSError:
        return (False, None)

    # Use resolved path because hash cache always uses resolved paths
    resolved_path = str(Path(entry.path).resolve())
    file_size = entry.size or 0
    num_chunks = len(entry.chunkhashes)

    # Check each chunk's hash in the cache
    for chunk_idx, expected_hash in enumerate(entry.chunkhashes):
        range_start = chunk_idx * chunk_size_bytes
        # Last chunk may be smaller
        if chunk_idx == num_chunks - 1:
            range_end = file_size
        else:
            range_end = range_start + chunk_size_bytes

        cache_entry = hash_cache.get_entry(
            file_path_key=resolved_path,
            hash_algorithm=hash_alg,
            range_start=range_start,
            range_end=range_end,
        )

        if cache_entry is None:
            return (False, None)

        # Check if the cached mtime matches the current file mtime
        if cache_entry.last_modified_time != current_mtime_str:
            return (False, None)

        # Check if the cached hash matches the expected hash
        if cache_entry.file_hash != expected_hash:
            return (False, None)

    # All chunks match - can skip download
    actual_mtime_us = current_mtime_ns // 1_000
    logger.debug(
        f"Skipping download of chunked file {entry.path} - "
        f"hash cache indicates all {num_chunks} chunks are up to date"
    )
    return (True, actual_mtime_us)


def _update_hash_cache_for_chunked_file(
    entry: ManifestFilePath,
    local_path: Path,
    hash_alg: HashAlgorithm,
    hash_cache: HashCache,
    chunk_size_bytes: int,
    mtime_ns: int,
) -> None:
    """
    Update the hash cache with all chunk hashes for a downloaded chunked file.

    After a chunked file is downloaded and finalized (os.replace'd), this function
    stores each chunk's hash in the hash cache with the appropriate byte range.

    Args:
        entry: The manifest file entry with chunkhashes
        local_path: The final local path of the downloaded file
        hash_alg: The hash algorithm used in the manifest
        hash_cache: The hash cache to update
        chunk_size_bytes: The chunk size in bytes from the manifest
        mtime_ns: The file's mtime in nanoseconds after finalization
    """
    if entry.chunkhashes is None:
        return

    resolved_path = str(local_path.resolve())
    mtime_str = str(mtime_ns)
    file_size = entry.size or 0
    num_chunks = len(entry.chunkhashes)

    for chunk_idx, chunk_hash in enumerate(entry.chunkhashes):
        range_start = chunk_idx * chunk_size_bytes
        # Last chunk may be smaller
        if chunk_idx == num_chunks - 1:
            range_end = file_size
        else:
            range_end = range_start + chunk_size_bytes

        hash_cache.put_entry(
            HashCacheEntry(
                file_path=resolved_path,
                hash_algorithm=hash_alg,
                file_hash=chunk_hash,
                last_modified_time=mtime_str,
                range_start=range_start,
                range_end=range_end,
            )
        )


def _get_new_copy_file_path(
    local_file_path: Path,
    collision_lock: Lock,
    collision_file_dict: DefaultDict[str, int],
) -> Path:
    """
    Generate a unique file path when a file already exists.

    Creates paths like "file (1).ext", "file (2).ext", etc.
    Thread-safe using the provided lock.
    """
    with collision_lock:
        file_str = str(local_file_path)
        num = collision_file_dict[file_str]
        new_file_path = local_file_path

        while True:
            try:
                # Atomic file creation to verify uniqueness
                with open(new_file_path, "x"):
                    break
            except FileExistsError:
                num += 1
                new_file_path = local_file_path.parent / (
                    f"{local_file_path.stem} ({num}){local_file_path.suffix}"
                )

        collision_file_dict[file_str] = num
        return new_file_path


async def _download_single_file_async(
    entry: ManifestFilePath,
    hash_alg: str,
    hash_alg_enum: HashAlgorithm,
    data_cache: ContentAddressedDataCache,
    hash_cache: Optional[HashCache],
    executor: concurrent.futures.ThreadPoolExecutor,
    collision_lock: Lock,
    collision_file_dict: DefaultDict[str, int],
    file_conflict_resolution: FileConflictResolution,
    progress_tracker: Optional[ProgressTracker],
) -> Tuple[int, Optional[Path], bool, Optional[int]]:
    """
    Download a single file entry from the data cache with parallel multi-part support.

    For S3 downloads of files larger than MIN_SIZE_FOR_MULTIPART_DOWNLOAD,
    uses parallel byte-range requests for improved throughput. Smaller files
    and filesystem cache downloads use single-threaded download.

    Args:
        entry: The manifest file entry to download.
        hash_alg: The hash algorithm string (e.g., "xxh128").
        hash_alg_enum: The hash algorithm enum.
        data_cache: The data cache to download from.
        hash_cache: Optional hash cache for skip detection.
        executor: The shared ThreadPoolExecutor for parallel downloads.
        collision_lock: Lock for thread-safe collision tracking.
        collision_file_dict: Dict for tracking file name collisions.
        file_conflict_resolution: How to handle existing files.
        progress_tracker: Optional progress tracker for download progress.

    Returns:
        Tuple of (bytes_downloaded, local_path or None if skipped, was_skipped, actual_mtime_us)
    """
    import secrets

    if entry.hash is None:
        raise ValueError(f"File entry '{entry.path}' has no hash")

    local_path = _get_long_path_compatible_path(Path(entry.path))
    file_size = entry.size or 0

    loop = asyncio.get_running_loop()

    # Check hash cache first (run in executor to avoid blocking)
    def check_cache_and_conflicts() -> Tuple[Path, bool, Optional[int], Optional[Path]]:
        """
        Check hash cache and handle file conflicts.

        Returns:
            Tuple of (local_path, should_skip, cached_mtime_us, temp_path or None)
            If should_skip is True, temp_path is None.
            If should_skip is False, temp_path is the pre-allocated temp file.
        """
        nonlocal local_path

        # Check hash cache first
        can_skip, cached_mtime_us = _check_hash_cache_for_skip(entry, hash_alg_enum, hash_cache)
        if can_skip:
            return (local_path, True, cached_mtime_us, None)

        # Handle file conflicts
        if local_path.exists():
            if file_conflict_resolution == FileConflictResolution.SKIP:
                return (local_path, True, None, None)
            elif file_conflict_resolution == FileConflictResolution.OVERWRITE:
                pass
            elif file_conflict_resolution == FileConflictResolution.CREATE_COPY:
                local_path = _get_new_copy_file_path(
                    local_path, collision_lock, collision_file_dict
                )
                local_path = _get_long_path_compatible_path(local_path)
            else:
                raise ValueError(f"Unknown file conflict resolution: {file_conflict_resolution}")

        # Create parent directories
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # Pre-allocate temp file
        temp_suffix = secrets.token_hex(5)
        temp_path = local_path.parent / f"{local_path.name}.tmp{temp_suffix}"
        with open(temp_path, "wb") as f:
            f.truncate(file_size)

        return (local_path, False, None, temp_path)

    # Run setup in executor
    local_path, should_skip, cached_mtime_us, temp_path = await loop.run_in_executor(
        executor, check_cache_and_conflicts
    )

    if should_skip:
        return (file_size, local_path if cached_mtime_us else None, True, cached_mtime_us)

    assert temp_path is not None  # For type checker

    try:
        # Download based on data cache type
        if isinstance(data_cache, S3DataCache):
            s3_key = data_cache.get_object_key(entry.hash, hash_alg)

            # Use multi-part download for large files
            if file_size >= MIN_SIZE_FOR_MULTIPART_DOWNLOAD:
                bytes_downloaded = await download_s3_multipart_async(
                    s3_client=data_cache.s3_client,
                    s3_bucket=data_cache.s3_bucket,
                    s3_key=s3_key,
                    temp_path=temp_path,
                    file_size=file_size,
                    executor=executor,
                    progress_tracker=progress_tracker,
                )
            else:
                # Small file - download in single request (run in executor)
                def download_small_file() -> int:
                    try:
                        response = data_cache.s3_client.get_object(
                            Bucket=data_cache.s3_bucket,
                            Key=s3_key,
                        )
                        data = response["Body"].read()
                        with open(temp_path, "wb") as f:
                            f.write(data)
                        if progress_tracker:
                            progress_tracker.track_progress_callback(len(data))
                        return len(data)
                    except ClientError as exc:
                        status_code = int(exc.response["ResponseMetadata"]["HTTPStatusCode"])
                        raise JobAttachmentsS3ClientError(
                            action="downloading file",
                            status_code=status_code,
                            bucket_name=data_cache.s3_bucket,
                            key_or_prefix=s3_key,
                            message=str(exc),
                        ) from exc
                    except BotoCoreError as bce:
                        raise JobAttachmentS3BotoCoreError(
                            action="downloading file",
                            error_details=str(bce),
                        ) from bce

                bytes_downloaded = await loop.run_in_executor(executor, download_small_file)

        elif isinstance(data_cache, FileSystemDataCache):
            # Filesystem cache - copy file (run in executor)
            def copy_from_filesystem() -> int:
                import shutil

                source_path = Path(data_cache.get_object_key(entry.hash, hash_alg))  # type: ignore[arg-type]
                shutil.copy2(source_path, temp_path)
                copied_size = temp_path.stat().st_size
                if progress_tracker:
                    progress_tracker.track_progress_callback(copied_size)
                return copied_size

            bytes_downloaded = await loop.run_in_executor(executor, copy_from_filesystem)
        else:
            raise TypeError(f"Unsupported data cache type: {type(data_cache)}")

        # Finalization - atomic move and mtime update (run in executor)
        def finalize_file() -> int:
            os.replace(temp_path, local_path)

            # Restore modification time if available
            if entry.mtime is not None:
                mtime_seconds = entry.mtime / 1_000_000
                os.utime(local_path, (mtime_seconds, mtime_seconds))

            # Get actual filesystem mtime
            actual_mtime_ns = local_path.stat().st_mtime_ns
            actual_mtime_us = actual_mtime_ns // 1_000

            # Update hash cache
            if hash_cache is not None and entry.hash is not None:
                resolved_path = str(local_path.resolve())
                hash_cache.put_entry(
                    HashCacheEntry(
                        file_path=resolved_path,
                        hash_algorithm=hash_alg_enum,
                        file_hash=entry.hash,
                        last_modified_time=str(actual_mtime_ns),
                        range_start=0,
                        range_end=WHOLE_FILE_RANGE_END,
                    )
                )

            return actual_mtime_us

        actual_mtime_us = await loop.run_in_executor(executor, finalize_file)

        logger.debug(f"Downloaded {entry.path} to {local_path}")
        return (bytes_downloaded, local_path, False, actual_mtime_us)

    finally:
        # Clean up temp file if it still exists
        temp_path.unlink(missing_ok=True)


def _download_chunk_to_offset(
    chunk_hash: str,
    chunk_idx: int,
    temp_path: Path,
    offset: int,
    hash_alg: str,
    data_cache: ContentAddressedDataCache,
    progress_tracker: Optional[ProgressTracker],
) -> int:
    """
    Download a single chunk and write it to a specific offset in the temp file.

    Each thread opens its own file handle and seeks to the correct offset before
    writing. Since chunks write to non-overlapping regions, no locking is needed.

    This is the synchronous version used for small chunks. For large chunks,
    use _download_chunk_to_offset_async which supports multi-part parallel downloads.

    Args:
        chunk_hash: The hash of the chunk to download.
        chunk_idx: The index of this chunk (for logging).
        temp_path: Path to the pre-allocated temp file.
        offset: The byte offset where this chunk should be written.
        hash_alg: The hash algorithm string (e.g., "xxh128").
        data_cache: The data cache to download from.
        progress_tracker: Optional progress tracker for download progress.

    Returns:
        The number of bytes written.
    """
    if isinstance(data_cache, S3DataCache):
        s3_key = data_cache.get_object_key(chunk_hash, hash_alg)
        return download_s3_chunk_to_offset(
            s3_client=data_cache.s3_client,
            s3_bucket=data_cache.s3_bucket,
            s3_key=s3_key,
            chunk_idx=chunk_idx,
            temp_path=temp_path,
            offset=offset,
            progress_tracker=progress_tracker,
        )
    elif isinstance(data_cache, FileSystemDataCache):
        source_path = Path(data_cache.get_object_key(chunk_hash, hash_alg))
        return download_fs_chunk_to_offset(
            source_path=source_path,
            chunk_idx=chunk_idx,
            temp_path=temp_path,
            offset=offset,
            progress_tracker=progress_tracker,
        )
    else:
        raise TypeError(f"Unsupported data cache type: {type(data_cache)}")


async def _download_chunk_to_offset_async(
    chunk_hash: str,
    chunk_idx: int,
    temp_path: Path,
    offset: int,
    chunk_size: int,
    hash_alg: str,
    data_cache: ContentAddressedDataCache,
    executor: concurrent.futures.ThreadPoolExecutor,
    progress_tracker: Optional[ProgressTracker],
) -> int:
    """
    Download a chunk with parallel multi-part support for large chunks.

    For S3 chunks larger than MIN_SIZE_FOR_MULTIPART_DOWNLOAD, uses parallel
    byte-range requests. Smaller chunks use single-request download.

    Args:
        chunk_hash: The hash of the chunk to download.
        chunk_idx: The index of this chunk (for logging).
        temp_path: Path to the pre-allocated temp file.
        offset: The byte offset in the temp file where this chunk starts.
        chunk_size: The size of this chunk in bytes.
        hash_alg: The hash algorithm string (e.g., "xxh128").
        data_cache: The data cache to download from.
        executor: The shared ThreadPoolExecutor for parallel downloads.
        progress_tracker: Optional progress tracker for download progress.

    Returns:
        The number of bytes written.
    """
    loop = asyncio.get_running_loop()

    if isinstance(data_cache, S3DataCache):
        s3_key = data_cache.get_object_key(chunk_hash, hash_alg)

        # Use multi-part download for large chunks
        if chunk_size >= MIN_SIZE_FOR_MULTIPART_DOWNLOAD:
            return await download_s3_chunk_multipart_async(
                s3_client=data_cache.s3_client,
                s3_bucket=data_cache.s3_bucket,
                s3_key=s3_key,
                chunk_idx=chunk_idx,
                temp_path=temp_path,
                offset=offset,
                chunk_size=chunk_size,
                executor=executor,
                progress_tracker=progress_tracker,
            )
        else:
            # Small chunk - use single request (run in executor)
            return await loop.run_in_executor(
                executor,
                _download_chunk_to_offset,
                chunk_hash,
                chunk_idx,
                temp_path,
                offset,
                hash_alg,
                data_cache,
                progress_tracker,
            )

    elif isinstance(data_cache, FileSystemDataCache):
        # Filesystem cache - always use single-threaded copy
        return await loop.run_in_executor(
            executor,
            _download_chunk_to_offset,
            chunk_hash,
            chunk_idx,
            temp_path,
            offset,
            hash_alg,
            data_cache,
            progress_tracker,
        )

    else:
        raise TypeError(f"Unsupported data cache type: {type(data_cache)}")


async def _download_chunked_file_async(
    entry: ManifestFilePath,
    hash_alg: str,
    hash_alg_enum: HashAlgorithm,
    data_cache: ContentAddressedDataCache,
    chunk_size_bytes: int,
    hash_cache: Optional[HashCache],
    executor: concurrent.futures.ThreadPoolExecutor,
    collision_lock: Lock,
    collision_file_dict: DefaultDict[str, int],
    file_conflict_resolution: FileConflictResolution,
    progress_tracker: Optional[ProgressTracker],
) -> Tuple[int, Optional[Path], bool, Optional[int]]:
    """
    Async wrapper for downloading a chunked file with parallel chunk downloads.

    Downloads all chunks in parallel using the thread pool, then performs the
    atomic os.replace and mtime update as a continuation after all chunks complete.

    This allows multiple chunked files to have their chunks downloading concurrently,
    rather than waiting for one chunked file to complete before starting the next.

    The async flow uses continuations to chain phases together:
    1. [preallocate] - run_in_executor: check hash cache, create temp file, handle conflicts, mkdir
       └─► continuation: schedule all chunk downloads
    2. [chunk downloads] - asyncio.gather over multiple run_in_executor calls
       └─► continuation: finalization
    3. [finalization] - run_in_executor: atomic os.replace + mtime update + hash cache update

    If pre-allocation fails, the continuation is never scheduled and the error propagates.

    Args:
        entry: The manifest file entry with chunkhashes.
        hash_alg: The hash algorithm string (e.g., "xxh128").
        hash_alg_enum: The hash algorithm enum.
        data_cache: The data cache to download from.
        chunk_size_bytes: The chunk size in bytes from the manifest.
        hash_cache: Optional hash cache for skip detection and update.
        executor: The shared ThreadPoolExecutor for parallel downloads.
        collision_lock: Lock for thread-safe collision tracking.
        collision_file_dict: Dict for tracking file name collisions.
        file_conflict_resolution: How to handle existing files.
        progress_tracker: Optional progress tracker for download progress.

    Returns:
        Tuple of (bytes_downloaded, local_path or None if skipped, was_skipped, actual_mtime_us)
        actual_mtime_us is the actual filesystem mtime in microseconds, or None if skipped.
    """
    import secrets

    if entry.chunkhashes is None:
        raise ValueError(f"Chunked file entry '{entry.path}' has no chunkhashes")

    local_path = _get_long_path_compatible_path(Path(entry.path))
    file_size = entry.size or 0

    loop = asyncio.get_running_loop()

    # Pre-allocation function (runs in executor)
    def preallocate_temp_file() -> Tuple[Path, Path, bool, Optional[int]]:
        """
        Check hash cache, handle file conflicts, create directories, and pre-allocate temp file.

        Returns:
            Tuple of (local_path, temp_path, should_skip, cached_mtime_us)
        """
        nonlocal local_path

        # Check hash cache first for chunked files
        can_skip, cached_mtime_us = _check_hash_cache_for_chunked_skip(
            entry, hash_alg_enum, hash_cache, chunk_size_bytes
        )
        if can_skip:
            return (local_path, Path(), True, cached_mtime_us)

        # Handle file conflicts
        if local_path.exists():
            if file_conflict_resolution == FileConflictResolution.SKIP:
                return (local_path, Path(), True, None)
            elif file_conflict_resolution == FileConflictResolution.OVERWRITE:
                pass  # Continue to download
            elif file_conflict_resolution == FileConflictResolution.CREATE_COPY:
                local_path = _get_new_copy_file_path(
                    local_path, collision_lock, collision_file_dict
                )
                local_path = _get_long_path_compatible_path(local_path)
            else:
                raise ValueError(f"Unknown file conflict resolution: {file_conflict_resolution}")

        # Create parent directories
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # Create temp file path beside the target for atomic write
        temp_suffix = secrets.token_hex(5)
        temp_path = local_path.parent / f"{local_path.name}.tmp{temp_suffix}"

        # Pre-allocate the temp file to the exact size
        # This creates a sparse file on filesystems that support it
        with open(temp_path, "wb") as f:
            f.truncate(file_size)

        return (local_path, temp_path, False, None)

    # Continuation: download chunks and finalize (scheduled after pre-allocation)
    async def download_chunks_and_finalize(
        final_local_path: Path, temp_path: Path
    ) -> Tuple[int, Optional[Path], bool, Optional[int]]:
        """Download all chunks in parallel with multi-part support, then finalize."""
        try:
            # Download all chunks in parallel (with multi-part for large chunks)
            chunk_tasks: List[asyncio.Task[int]] = []
            num_chunks = len(entry.chunkhashes)  # type: ignore[arg-type]

            for chunk_idx, chunk_hash in enumerate(entry.chunkhashes):  # type: ignore[arg-type]
                offset = chunk_idx * chunk_size_bytes
                # Calculate actual chunk size (last chunk may be smaller)
                if chunk_idx == num_chunks - 1:
                    # Last chunk: remaining bytes
                    actual_chunk_size = file_size - offset
                else:
                    actual_chunk_size = chunk_size_bytes

                # Use async chunk download with multi-part support
                task = asyncio.create_task(
                    _download_chunk_to_offset_async(
                        chunk_hash,
                        chunk_idx,
                        temp_path,
                        offset,
                        actual_chunk_size,
                        hash_alg,
                        data_cache,
                        executor,
                        progress_tracker,
                    )
                )
                chunk_tasks.append(task)

            # Wait for all chunks to complete concurrently
            chunk_results = await asyncio.gather(*chunk_tasks, return_exceptions=True)

            # Check for errors
            errors: List[Exception] = []
            total_bytes = 0
            for chunk_idx, result in enumerate(chunk_results):
                if isinstance(result, BaseException):
                    logger.error(f"Failed to download chunk {chunk_idx} of {entry.path}: {result}")
                    if isinstance(result, Exception):
                        errors.append(result)
                    else:
                        # BaseException but not Exception (e.g., KeyboardInterrupt)
                        raise result
                else:
                    total_bytes += result

            # If any chunk failed, raise the first error
            if errors:
                raise errors[0]

            # Finalization - atomic move, mtime update, and hash cache update (run in executor)
            def finalize_chunked_file() -> int:
                """Finalize the chunked file download (atomic move + mtime + hash cache)."""
                os.replace(temp_path, final_local_path)

                # Restore modification time if available
                if entry.mtime is not None:
                    mtime_seconds = entry.mtime / 1_000_000  # Convert from microseconds
                    os.utime(final_local_path, (mtime_seconds, mtime_seconds))

                # Get the actual filesystem mtime (may differ from requested due to OS precision)
                actual_mtime_ns = final_local_path.stat().st_mtime_ns

                # Update hash cache with all chunk hashes
                if hash_cache is not None:
                    _update_hash_cache_for_chunked_file(
                        entry,
                        final_local_path,
                        hash_alg_enum,
                        hash_cache,
                        chunk_size_bytes,
                        actual_mtime_ns,
                    )

                return actual_mtime_ns // 1_000

            actual_mtime_us = await loop.run_in_executor(executor, finalize_chunked_file)

            logger.debug(
                f"Downloaded chunked file {entry.path} ({len(entry.chunkhashes)} chunks)"  # type: ignore[arg-type]
            )
            return (total_bytes, final_local_path, False, actual_mtime_us)

        finally:
            # Clean up temp file if it still exists (i.e., on error before os.replace)
            temp_path.unlink(missing_ok=True)

    # Chain pre-allocation → chunk downloads → finalization using add_done_callback
    # This schedules the continuation immediately when pre-allocation completes,
    # without an intermediate await in this function's body.
    result_future: asyncio.Future[Tuple[int, Optional[Path], bool, Optional[int]]] = (
        loop.create_future()
    )

    def on_prealloc_done(
        prealloc_future: asyncio.Future[Tuple[Path, Path, bool, Optional[int]]],
    ) -> None:
        """Callback that schedules chunk downloads after pre-allocation completes."""
        try:
            final_local_path, temp_path, should_skip, cached_mtime_us = prealloc_future.result()

            if should_skip:
                result_future.set_result((file_size, None, True, cached_mtime_us))
                return

            # Schedule the continuation (chunk downloads + finalization)
            async def run_continuation() -> None:
                try:
                    result = await download_chunks_and_finalize(final_local_path, temp_path)
                    result_future.set_result(result)
                except Exception as e:
                    result_future.set_exception(e)

            asyncio.ensure_future(run_continuation())

        except Exception as e:
            result_future.set_exception(e)

    # Schedule pre-allocation and attach the continuation callback
    prealloc_future = asyncio.ensure_future(loop.run_in_executor(executor, preallocate_temp_file))
    prealloc_future.add_done_callback(on_prealloc_done)

    # Single await for the entire operation
    return await result_future


def _create_symlink(entry: ManifestFilePath) -> None:
    """Create a symlink for a symlink entry."""
    if entry.symlink_target is None:
        raise ValueError(f"Symlink entry '{entry.path}' has no symlink_target")

    local_path = _get_long_path_compatible_path(Path(entry.path))
    target_path = Path(entry.symlink_target)

    # Create parent directories
    local_path.parent.mkdir(parents=True, exist_ok=True)

    # Remove existing symlink if present
    if local_path.is_symlink():
        local_path.unlink()
    elif local_path.exists():
        # If it's a regular file/dir, we need to handle conflict
        # For symlinks, we always overwrite
        if local_path.is_dir():
            import shutil

            shutil.rmtree(local_path)
        else:
            local_path.unlink()

    # Create the symlink
    local_path.symlink_to(target_path)
    logger.debug(f"Created symlink {entry.path} -> {entry.symlink_target}")


def _sort_symlinks_by_dependency(symlinks: List[ManifestFilePath]) -> List[ManifestFilePath]:
    """
    Sort symlinks so that targets are created before symlinks that point to them.

    For chained symlinks (A -> B -> C), we need to create C first, then B, then A.
    This uses a topological sort based on the dependency graph.

    If there are cycles in the symlink dependencies (which shouldn't happen in valid
    manifests), the symlinks that are part of the cycle are placed at the end in
    their original order, after all non-cyclic symlinks have been properly sorted.

    Args:
        symlinks: List of symlink entries to sort

    Returns:
        List of symlink entries in dependency order (targets before dependents)
    """
    if not symlinks:
        return []

    # Build a map from path to entry for quick lookup
    path_to_entry: dict[str, ManifestFilePath] = {entry.path: entry for entry in symlinks}

    # Build adjacency list: symlink -> list of symlinks it depends on
    # A symlink depends on another if its target is that other symlink's path
    dependencies: dict[str, List[str]] = {entry.path: [] for entry in symlinks}

    for entry in symlinks:
        target = entry.symlink_target
        if target and target in path_to_entry:
            # This symlink depends on another symlink (chained)
            dependencies[entry.path].append(target)

    # Topological sort using Kahn's algorithm
    # Count incoming edges (how many symlinks point to each symlink)
    in_degree: dict[str, int] = {path: 0 for path in path_to_entry}
    for path, deps in dependencies.items():
        for dep in deps:
            in_degree[dep] += 1

    # Start with symlinks that no other symlink points to
    # These are the "leaf" symlinks that should be created first
    queue: deque[str] = deque(path for path, degree in in_degree.items() if degree == 0)
    sorted_paths: List[str] = []

    while queue:
        path = queue.popleft()
        sorted_paths.append(path)

        # For each symlink that this one depends on, decrement its in-degree
        for dep in dependencies[path]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    # If we couldn't sort all symlinks, there's a cycle
    cyclic_symlinks: List[ManifestFilePath] = []
    if len(sorted_paths) != len(symlinks):
        # Find symlinks that are part of the cycle (not in sorted_paths)
        sorted_set = set(sorted_paths)
        cyclic_symlinks = [entry for entry in symlinks if entry.path not in sorted_set]
        logger.warning(
            f"Detected cycle in symlink dependencies involving {len(cyclic_symlinks)} symlinks, "
            "placing them at the end"
        )

    # Reverse to get targets before dependents, then append any cyclic symlinks
    sorted_paths.reverse()
    result = [path_to_entry[path] for path in sorted_paths]
    result.extend(cyclic_symlinks)

    return result


def _delete_file(path: str) -> None:
    """
    Delete a file or symlink at the given path.

    Only deletes if the path is a file or symlink. If the path is a directory
    or doesn't exist, it is left alone. This prevents accidental deletion
    if the filesystem state doesn't match the manifest (e.g., a directory
    exists where a file was expected).

    Args:
        path: The path to delete
    """
    local_path = _get_long_path_compatible_path(Path(path))

    if local_path.is_symlink():
        local_path.unlink()
        logger.debug(f"Deleted symlink {path}")
    elif local_path.is_file():
        local_path.unlink()
        logger.debug(f"Deleted file {path}")
    elif local_path.is_dir():
        logger.debug(f"Expected file but found directory, skipping deletion: {path}")
    else:
        logger.debug(f"File does not exist, skipping deletion: {path}")


def _delete_directory(path: str) -> None:
    """
    Delete an empty directory at the given path.

    Only deletes if the path is an empty directory. If the directory contains
    files, it is left in place. If the path is a file or doesn't exist, it is
    left alone. This prevents accidental deletion if the filesystem state
    doesn't match the manifest.

    Diff manifests must explicitly include deletion markers for all contained
    files and subdirectories before the parent directory can be deleted.
    This prevents accidental deletion of files that were added outside the
    manifest system.

    Args:
        path: The path to delete
    """
    local_path = _get_long_path_compatible_path(Path(path))

    if local_path.is_symlink():
        # Symlink to a directory - treat as a symlink, not a directory
        logger.debug(f"Expected directory but found symlink, skipping deletion: {path}")
    elif local_path.is_file():
        logger.debug(f"Expected directory but found file, skipping deletion: {path}")
    elif local_path.is_dir():
        try:
            local_path.rmdir()  # Only removes empty directories
            logger.debug(f"Deleted empty directory {path}")
        except OSError:
            # Directory not empty - this is expected if there are files
            # not tracked by the manifest. Leave it in place.
            logger.debug(f"Directory not empty, skipping deletion: {path}")
    else:
        logger.debug(f"Directory does not exist, skipping deletion: {path}")


def _create_directory(dir_entry: ManifestDirectoryPath) -> None:
    """Create a directory from a directory entry."""
    local_path = _get_long_path_compatible_path(Path(dir_entry.path))
    local_path.mkdir(parents=True, exist_ok=True)
    logger.debug(f"Created directory {dir_entry.path}")


def _build_updated_manifest(
    manifest: AbsManifest,
    updated_mtimes: Dict[str, int],
) -> AbsManifest:
    """
    Build a copy of the manifest with mtime values updated to match actual filesystem timestamps.

    This is useful for cross-OS scenarios where file system mtime precision differs.
    For example, a snapshot created on Linux with nanosecond precision may have different
    mtime values when the files are written to Windows (100-nanosecond precision) or
    macOS (microsecond precision). Using the updated manifest as the basis for subsequent
    diff operations ensures reliable change detection.

    Args:
        manifest: The original manifest with absolute paths.
        updated_mtimes: Dict mapping file paths to their actual filesystem mtime in microseconds.

    Returns:
        A new manifest of the same type with updated mtime values for downloaded files.
        Files not in updated_mtimes (e.g., skipped files, symlinks, deleted entries)
        retain their original mtime values.
    """
    # Build updated file entries
    updated_files: List[ManifestFilePath] = []
    for entry in manifest.files:
        if entry.path in updated_mtimes:
            # Create a new entry with the updated mtime
            updated_files.append(
                ManifestFilePath(
                    path=entry.path,
                    hash=entry.hash,
                    size=entry.size,
                    mtime=updated_mtimes[entry.path],
                    runnable=entry.runnable,
                    chunkhashes=entry.chunkhashes,
                    symlink_target=entry.symlink_target,
                    deleted=entry.deleted,
                )
            )
        else:
            # Keep the original entry unchanged
            updated_files.append(entry)

    # Create the appropriate manifest type
    if isinstance(manifest, AbsSnapshotManifest):
        return AbsSnapshotManifest(
            hash_alg=manifest.hashAlg,
            files=updated_files,
            total_size=manifest.totalSize,
            dirs=manifest.dirs,
            parent_manifest_hash=manifest.parentManifestHash,
            file_chunk_size_bytes=manifest.fileChunkSizeBytes,
        )
    else:
        # AbsDiffManifest
        return AbsDiffManifest(
            hash_alg=manifest.hashAlg,
            files=updated_files,
            total_size=manifest.totalSize,
            dirs=manifest.dirs,
            parent_manifest_hash=manifest.parentManifestHash,
            file_chunk_size_bytes=manifest.fileChunkSizeBytes,
        )


def download_manifest(
    manifest: AbsManifest,
    data_cache: ContentAddressedDataCache,
    *,
    hash_cache: Optional[HashCache] = None,
    file_conflict_resolution: FileConflictResolution = FileConflictResolution.OVERWRITE,
    apply_deletes: bool = True,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.PRESERVE,
    max_workers: Optional[int] = None,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
    progress_tracker: Optional[ProgressTracker] = None,
) -> DownloadResult:
    """
    Download files from a data cache to the local filesystem.

    Downloads all files in the manifest from the data cache (S3 or filesystem)
    to the local filesystem, using the absolute paths in the manifest as the
    target locations.

    Args:
        manifest: Manifest with absolute paths and hashes. Can be
            AbsSnapshotManifest or AbsDiffManifest.
        data_cache: Data cache to download from (S3DataCache or FileSystemDataCache)
        hash_cache: Optional hash cache to check for files that already have the
            correct content. If a file exists locally and its cached hash matches
            the expected hash from the manifest, the download is skipped. This
            avoids re-downloading files that are already up to date.
        file_conflict_resolution: How to handle existing files. Default OVERWRITE.
            Note: When hash_cache is provided, files with matching hashes are
            skipped regardless of this setting.
        apply_deletes: If True (default), apply deletions from diff manifests.
            If False, skip deletions and only download new/modified files.
        symlink_policy: How to handle symlinks. Default PRESERVE.
            PRESERVE: Create symlinks as specified in the manifest.
            EXCLUDE: Skip symlink entries entirely.
            Other policies are not supported for DOWNLOAD.
        max_workers: Maximum parallel download workers. Default: auto-detect.
        print_function_callback: Progress callback for status messages
        progress_tracker: Optional progress tracker for download progress and cancellation

    Returns:
        DownloadResult containing:
        - statistics: DownloadSummaryStatistics with download results
        - manifest: A copy of the input manifest with mtime values updated to match
          the actual local filesystem timestamps. This is useful for cross-OS scenarios
          where file system mtime precision differs.

    Raises:
        ValueError: If the manifest contains relative paths or unsupported symlink_policy
        AssetSyncCancelledError: If cancelled via progress tracker
    """
    # Validate absolute paths
    _validate_absolute_paths(manifest)

    # Validate symlink_policy
    if symlink_policy not in (SymlinkPolicy.PRESERVE, SymlinkPolicy.EXCLUDE):
        raise ValueError(
            f"DOWNLOAD operation only supports PRESERVE or EXCLUDE symlink policies. "
            f"Got: {symlink_policy.value}"
        )

    hash_alg = manifest.hashAlg.value
    hash_alg_enum = manifest.hashAlg

    # Determine number of workers
    if max_workers is None:
        max_workers = DEFAULT_MAX_WORKERS

    # Categorize entries
    regular_files: List[ManifestFilePath] = []
    chunked_files: List[ManifestFilePath] = []
    symlinks: List[ManifestFilePath] = []
    deleted_files: List[ManifestFilePath] = []
    directories: List[ManifestDirectoryPath] = []
    deleted_directories: List[ManifestDirectoryPath] = []

    for entry in manifest.files:
        if entry.deleted:
            deleted_files.append(entry)
        elif entry.symlink_target is not None:
            symlinks.append(entry)
        elif entry.chunkhashes is not None:
            chunked_files.append(entry)
        else:
            regular_files.append(entry)

    for dir_entry in manifest.dirs:
        if dir_entry.deleted:
            deleted_directories.append(dir_entry)
        else:
            directories.append(dir_entry)

    # Calculate totals for progress tracking
    # Only count symlinks if policy is PRESERVE
    symlink_count = len(symlinks) if symlink_policy == SymlinkPolicy.PRESERVE else 0
    total_files = len(regular_files) + len(chunked_files) + symlink_count
    total_bytes = sum((e.size or 0) for e in regular_files) + sum(
        (e.size or 0) for e in chunked_files
    )

    # Set up progress tracker if not provided
    if progress_tracker is None:
        progress_tracker = ProgressTracker(
            status=ProgressStatus.DOWNLOAD_IN_PROGRESS,
            total_files=total_files,
            total_bytes=total_bytes,
        )
    else:
        progress_tracker.total_files = total_files
        progress_tracker.total_bytes = total_bytes

    start_time = time.perf_counter()

    # Thread-safe collision tracking
    collision_lock = Lock()
    collision_file_dict: DefaultDict[str, int] = defaultdict(int)

    # Track downloaded files by root for statistics
    downloaded_files_by_root: DefaultDict[str, List[str]] = defaultdict(list)

    # Track updated mtimes for each file path (path -> actual_mtime_us)
    updated_mtimes: Dict[str, int] = {}
    updated_mtimes_lock = Lock()

    processed_files = 0
    processed_bytes = 0
    skipped_files = 0
    skipped_bytes = 0

    try:
        # 1. Process deletions first (for diff manifests only), if enabled
        if apply_deletes and isinstance(manifest, AbsDiffManifest):
            # Sort by path length descending so children are deleted before parents
            sorted_deleted_files = sorted(deleted_files, key=lambda e: len(e.path), reverse=True)
            sorted_deleted_dirs = sorted(
                deleted_directories, key=lambda d: len(d.path), reverse=True
            )

            for entry in sorted_deleted_files:
                _delete_file(entry.path)
                print_function_callback(f"Deleted: {entry.path}")

            for dir_entry in sorted_deleted_dirs:
                _delete_directory(dir_entry.path)
                print_function_callback(f"Deleted directory: {dir_entry.path}")

        # 2. Create directories
        for dir_entry in directories:
            _create_directory(dir_entry)

        # Get chunk size from manifest for chunked file downloads
        chunk_size_bytes = manifest.fileChunkSizeBytes

        # 3. Download all files (regular and chunked) in parallel using asyncio
        # Both regular files and chunked files use async downloads with multi-part support
        async def download_all_files(executor: concurrent.futures.ThreadPoolExecutor) -> None:
            """Download all regular and chunked files in parallel using asyncio."""
            nonlocal processed_files, processed_bytes, skipped_files, skipped_bytes

            # Type alias for download result
            DownloadResultTuple = Tuple[int, Optional[Path], bool, Optional[int]]

            # Helper to wrap an awaitable with its entry for tracking
            async def download_with_entry(
                entry: ManifestFilePath,
                awaitable: Any,
            ) -> Tuple[ManifestFilePath, DownloadResultTuple]:
                """Wrap a download awaitable to return the entry alongside the result."""
                result: DownloadResultTuple = await awaitable
                return (entry, result)

            # Create tasks for all files
            all_tasks: List[asyncio.Task[Tuple[ManifestFilePath, DownloadResultTuple]]] = []

            # Submit regular file downloads as async tasks (with multi-part support for large files)
            for entry in regular_files:
                coro = _download_single_file_async(
                    entry,
                    hash_alg,
                    hash_alg_enum,
                    data_cache,
                    hash_cache,
                    executor,
                    collision_lock,
                    collision_file_dict,
                    file_conflict_resolution,
                    progress_tracker,
                )
                task = asyncio.create_task(download_with_entry(entry, coro))
                all_tasks.append(task)

            # Submit chunked file downloads as async tasks (with parallel chunk downloads)
            for entry in chunked_files:
                coro = _download_chunked_file_async(
                    entry,
                    hash_alg,
                    hash_alg_enum,
                    data_cache,
                    chunk_size_bytes,
                    hash_cache,
                    executor,
                    collision_lock,
                    collision_file_dict,
                    file_conflict_resolution,
                    progress_tracker,
                )
                task = asyncio.create_task(download_with_entry(entry, coro))
                all_tasks.append(task)

            # Process results as they complete
            for completed_coro in asyncio.as_completed(all_tasks):
                # Check for cancellation
                if progress_tracker and not progress_tracker.continue_reporting:
                    # Cancel remaining tasks
                    for task in all_tasks:
                        if not task.done():
                            task.cancel()
                    raise AssetSyncCancelledError("Download cancelled.")

                try:
                    entry, result = await completed_coro
                    bytes_downloaded, local_path, was_skipped, actual_mtime_us = result

                    if was_skipped:
                        skipped_files += 1
                        skipped_bytes += bytes_downloaded
                        progress_tracker.increase_skipped(1, bytes_downloaded)
                        print_function_callback(f"Skipped: {entry.path}")
                    else:
                        processed_files += 1
                        processed_bytes += bytes_downloaded
                        progress_tracker.increase_processed(1, 0)
                        if local_path:
                            root = str(local_path.parent)
                            downloaded_files_by_root[root].append(str(local_path))
                        # Track the actual mtime from the filesystem
                        if actual_mtime_us is not None:
                            with updated_mtimes_lock:
                                updated_mtimes[entry.path] = actual_mtime_us
                        file_type = "chunked file" if entry.chunkhashes else "file"
                        print_function_callback(f"Downloaded {file_type}: {entry.path}")

                    progress_tracker.report_progress()

                except asyncio.CancelledError:
                    raise AssetSyncCancelledError("Download cancelled.")
                except Exception:
                    if progress_tracker and not progress_tracker.continue_reporting:
                        raise AssetSyncCancelledError("Download cancelled.")
                    raise

        # Run the async download coordinator with a shared thread pool
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            asyncio.run(download_all_files(executor))

        # 5. Create symlinks (if policy is PRESERVE)
        # Sort symlinks so targets are created before symlinks that point to them
        # This handles chained symlinks (A -> B -> C) correctly
        if symlink_policy == SymlinkPolicy.PRESERVE:
            sorted_symlinks = _sort_symlinks_by_dependency(symlinks)
            for entry in sorted_symlinks:
                _create_symlink(entry)
                processed_files += 1
                progress_tracker.increase_processed(1, 0)
                progress_tracker.report_progress()
                print_function_callback(f"Created symlink: {entry.path}")
        # If EXCLUDE, symlinks are skipped (already not in total_files count)

    except AssetSyncCancelledError:
        raise AssetSyncCancelledError(
            f"Download cancelled. "
            f"(Downloaded {processed_files} file{'s' if processed_files != 1 else ''} "
            f"before cancellation.)"
        )

    progress_tracker.total_time = time.perf_counter() - start_time

    # Build the updated manifest with actual filesystem mtimes
    updated_manifest = _build_updated_manifest(manifest, updated_mtimes)

    statistics = progress_tracker.get_download_summary_statistics(dict(downloaded_files_by_root))
    return DownloadResult(statistics=statistics, manifest=updated_manifest)
