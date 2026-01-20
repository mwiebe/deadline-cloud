# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for filling in hashes AND uploading file content to a data cache in a pipelined manner.

This module implements the HASH_UPLOAD operation from the composable manifest operations design:
    HASH_UPLOAD: (AbsManifest, DataCache) → UploadResult

The operation combines hashing and uploading into a single pass over the data,
avoiding the need to read files twice (once for hashing, once for uploading).
"""

from __future__ import annotations

import concurrent.futures
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from .._manifest import (
    AbsManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    _is_absolute_path,
)
from ...asset_manifests.hash_algorithms import HashAlgorithm
from ...caches.hash_cache import HashCache, HashCacheEntry, WHOLE_FILE_RANGE_END
from .._content_addressed_data_cache import (
    ContentAddressedDataCache,
    S3DataCache,
)
from ...progress_tracker import human_readable_file_size

# Import pipeline classes and types
from ._hash_upload_abs_manifest_pipeline import (
    HashUploadPipelineBase,
    HashUploadProgressCallback,
    HashUploadProgressMetadata,
    _HashUploadProgressState,
    _ChunkWorkItem,
    _StreamingWorkItem,
    _MemoryPool,
    PipelineWorkItem,
)
from ._hash_upload_abs_manifest_s3_pipeline import S3HashUploadPipeline
from ._hash_upload_abs_manifest_file_system_pipeline import FileSystemHashUploadPipeline

logger = logging.getLogger("deadline.job_attachments.hash_upload")

# Minimum memory limit: 256MB (one chunk)
MIN_MEMORY_BYTES = 256 * 1024 * 1024

# Maximum memory limit: 16GB
MAX_MEMORY_BYTES = 16 * 1024 * 1024 * 1024

# Default number of parallel workers
DEFAULT_MAX_WORKERS = 10


@dataclass
class UploadResult:
    """
    Result of a hash_upload_abs_manifest operation.

    Attributes:
        statistics: Progress metadata with final statistics about the hash/upload operation
            including separate counts for hashing and uploading phases.
        manifest: The manifest with all hashes filled in.
    """

    statistics: HashUploadProgressMetadata
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
    """Calculate the default memory limit for the pipeline."""
    import psutil

    try:
        mem = psutil.virtual_memory()
        total_memory = mem.total
        available_memory = mem.available

        quarter_of_total = total_memory // 4
        available_minus_1gb = available_memory - (1024 * 1024 * 1024)

        result = min(MAX_MEMORY_BYTES, max(MIN_MEMORY_BYTES, quarter_of_total, available_minus_1gb))
        logger.debug(
            f"Memory limit calculation: min=256MB, max=16GB, quarter_total={quarter_of_total // (1024 * 1024)}MB, "
            f"available-1GB={available_minus_1gb // (1024 * 1024)}MB, result={result // (1024 * 1024)}MB"
        )
        return result
    except Exception as e:
        logger.warning(f"Failed to detect system memory, using minimum: {e}")
        return MIN_MEMORY_BYTES


@dataclass
class _PipelineResult:
    """Result from running the pipeline."""

    items: List[PipelineWorkItem]
    cache_invalidated: bool


def _run_pipeline(
    work_items: List[PipelineWorkItem],
    hash_alg: HashAlgorithm,
    data_cache: ContentAddressedDataCache,
    max_memory_bytes: int,
    max_workers: int,
    progress_state: Optional[_HashUploadProgressState],
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
) -> _PipelineResult:
    """Run the pipeline on work items using separate thread pools for each stage."""
    if not work_items:
        return _PipelineResult(items=[], cache_invalidated=False)

    memory_pool = _MemoryPool(max_memory_bytes)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as read_hash_executor:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as upload_executor:
            # Select the appropriate pipeline class based on data cache type
            pipeline_class = (
                S3HashUploadPipeline
                if isinstance(data_cache, S3DataCache)
                else FileSystemHashUploadPipeline
            )

            pipeline: HashUploadPipelineBase = pipeline_class(
                read_hash_executor=read_hash_executor,
                upload_executor=upload_executor,
                memory_pool=memory_pool,
                hash_alg=hash_alg,
                data_cache=data_cache,
                progress_state=progress_state,
                hash_cache=hash_cache,
                force_rehash=force_rehash,
            )

            for item in work_items:
                pipeline.submit(item)

            items = pipeline.wait_for_completion()
            return _PipelineResult(
                items=items,
                cache_invalidated=pipeline.was_cache_invalidated(),
            )


def hash_upload_abs_manifest(
    manifest: AbsManifest,
    data_cache: ContentAddressedDataCache,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    max_memory_bytes: Optional[int] = None,
    max_workers: Optional[int] = None,
    file_chunk_size_bytes: Optional[int] = None,
    on_progress: Optional[HashUploadProgressCallback] = None,
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
        on_progress: Optional callback for progress reporting. Called periodically with
            HashUploadProgressMetadata containing separate progress for hashing and
            uploading phases. Return True to continue, False to cancel the operation.

    Returns:
        UploadResult containing:
        - statistics: SummaryStatistics with processed/skipped files and bytes
        - manifest: A NEW manifest of the same type with all hashes filled in

    Raises:
        ValueError: If the manifest contains relative paths (paths must be absolute)
        ValueError: If effective chunk size is positive and max_memory_bytes is less than chunk size
        JobAttachmentsS3ClientError: If an S3 API call fails (when using S3DataCache).
            Wraps boto3 ClientError with additional context about the operation.
        JobAttachmentS3BotoCoreError: If a low-level AWS SDK error occurs (when using S3DataCache).
            Wraps botocore.exceptions.BotoCoreError.
        OSError: If a file cannot be read from the filesystem.
    """
    _validate_absolute_paths(manifest)

    output_chunk_size = (
        file_chunk_size_bytes if file_chunk_size_bytes is not None else manifest.fileChunkSizeBytes
    )
    chunking_enabled = output_chunk_size > 0

    if max_memory_bytes is None:
        max_memory_bytes = _get_default_max_memory_bytes()

    if max_workers is None:
        max_workers = DEFAULT_MAX_WORKERS

    if chunking_enabled and max_memory_bytes < output_chunk_size:
        raise ValueError(
            f"max_memory_bytes ({max_memory_bytes}) must be >= fileChunkSizeBytes ({output_chunk_size}). "
            f"The pipeline needs at least one chunk's worth of memory to operate."
        )

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

    # Build work items
    all_work_items: List[PipelineWorkItem] = []
    entry_map: Dict[str, Tuple[int, ManifestFilePath]] = {}
    file_chunk_counts: Dict[str, int] = {}

    for idx, entry in file_entries_to_process:
        abs_path = Path(entry.path)
        cache_key = str(abs_path.resolve())

        if entry.size is None:
            raise ValueError(
                f"File entry '{entry.path}' has size=None. "
                f"File entries must have size set (symlinks and deletions are handled separately)."
            )
        if entry.mtime is None:
            raise ValueError(
                f"File entry '{entry.path}' has mtime=None. "
                f"File entries must have mtime set (symlinks and deletions are handled separately)."
            )

        file_size = entry.size
        mtime = entry.mtime

        entry_map[cache_key] = (idx, entry)

        if chunking_enabled and file_size > output_chunk_size:
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
            all_work_items.append(
                _StreamingWorkItem(
                    file_path=abs_path,
                    cache_key=cache_key,
                    file_size=file_size,
                    mtime=mtime,
                )
            )
            file_chunk_counts[cache_key] = 0
        else:
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
            file_chunk_counts[cache_key] = 0

    start_time = time.perf_counter()

    # Always create progress_state to track statistics, even without callback
    progress_state = _HashUploadProgressState(
        total_file_chunks=len(all_work_items),
        total_bytes=sum(entry.size or 0 for _, entry in entry_map.values()),
        on_progress=on_progress,  # May be None
    )

    cached_results: Dict[str, Union[str, Dict[int, str]]] = {}

    pipeline_result = _PipelineResult(items=[], cache_invalidated=False)
    if all_work_items:
        pipeline_result = _run_pipeline(
            work_items=all_work_items,
            hash_alg=manifest.hashAlg,
            data_cache=data_cache,
            max_memory_bytes=max_memory_bytes,
            max_workers=max_workers,
            progress_state=progress_state,
            hash_cache=hash_cache,
            force_rehash=force_rehash,
        )

    if progress_state.on_progress is not None:
        progress_state.force_callback()

    if pipeline_result.cache_invalidated:
        if isinstance(data_cache, S3DataCache) and data_cache.s3_check_cache is not None:
            try:
                data_cache.s3_check_cache.remove_cache()
                logger.info("S3 check cache database deleted due to stale entries")
            except Exception as e:
                logger.warning(f"Failed to delete S3 check cache: {e}")

    # Process results and update caches
    skipped_files_set: set = set()
    skipped_bytes = 0
    processed_bytes = 0

    for item in pipeline_result.items:
        if isinstance(item, _StreamingWorkItem):
            if item.file_hash is not None:
                cached_results[item.cache_key] = item.file_hash

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
                    cached_results[item.cache_key] = item.chunk_hash
                    range_start = 0
                    range_end = WHOLE_FILE_RANGE_END
                else:
                    if item.cache_key not in cached_results:
                        cached_results[item.cache_key] = {}
                    chunk_dict = cached_results[item.cache_key]
                    if isinstance(chunk_dict, dict):
                        chunk_dict[item.chunk_index] = item.chunk_hash
                    range_start = item.chunk_start
                    range_end = item.chunk_end

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

        chunk_size = (
            item.chunk_end - item.chunk_start
            if isinstance(item, _ChunkWorkItem)
            else item.file_size
        )
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
            all_skipped = all(
                item.skipped for item in pipeline_result.items if item.cache_key == cache_key
            )
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

    # Build final statistics from progress_state
    end_time = time.perf_counter()
    total_time = end_time - start_time
    transfer_rate = progress_state.total_bytes / total_time if total_time > 0 else 0.0

    # Build summary message - use total_bytes for rate calculation
    # Use "files" when processing whole files, "chunks" when chunking is enabled
    unit = "files" if output_chunk_size <= 0 else "chunks"
    summary_parts = [
        f"Hashed/uploaded {human_readable_file_size(progress_state.total_bytes)}",
        f"({progress_state.total_file_chunks} {unit})",
        f"in {total_time:.2f}s",
    ]
    if total_time > 0:
        summary_parts.append(f"({human_readable_file_size(int(transfer_rate))}/s)")

    statistics = HashUploadProgressMetadata(
        total_file_chunks=progress_state.total_file_chunks,
        total_bytes=progress_state.total_bytes,
        hashed_file_chunks=progress_state.hashed_file_chunks,
        hashed_bytes=progress_state.hashed_bytes,
        hash_skipped_file_chunks=progress_state.hash_skipped_file_chunks,
        hash_skipped_bytes=progress_state.hash_skipped_bytes,
        uploaded_file_chunks=progress_state.uploaded_file_chunks,
        uploaded_bytes=progress_state.uploaded_bytes,
        upload_skipped_file_chunks=progress_state.upload_skipped_file_chunks,
        upload_skipped_bytes=progress_state.upload_skipped_bytes,
        progress=100.0,
        progressMessage=" ".join(summary_parts),
        total_time=total_time,
        transfer_rate=transfer_rate,
    )

    return UploadResult(
        statistics=statistics,
        manifest=result_manifest,
    )
