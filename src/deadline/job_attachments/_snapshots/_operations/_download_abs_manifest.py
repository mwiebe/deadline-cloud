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
- Callback-based pipeline for efficient thread pool usage
- File conflict resolution (skip, overwrite, create copy)
- Progress tracking and cancellation support
- Symlink creation
- Diff manifest support (applies deletions)
- Modification time restoration
- Returns updated manifest with local filesystem timestamps for reliable diff operations
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import DefaultDict, Dict, List, Optional, Tuple

from .._manifest import (
    AbsSnapshot,
    AbsSnapshotDiff,
    AbsManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    _is_absolute_path,
    SymlinkPolicy,
)
from .._content_addressed_data_cache import (
    ContentAddressedDataCache,
    S3DataCache,
)
from ...models import FileConflictResolution
from ...exceptions import AssetSyncCancelledError
from ..._utils import _get_long_path_compatible_path
from ...caches.hash_cache import HashCache

# Import pipeline classes and progress types
from ._download_abs_manifest_pipeline import (
    DownloadPipelineBase,
    DownloadProgressCallback,
    DownloadProgressMetadata,
    _DownloadProgressState,
)
from ._download_abs_manifest_s3_pipeline import S3DownloadPipeline
from ._download_abs_manifest_file_system_pipeline import FileSystemDownloadPipeline
from ..._path_summarization import human_readable_file_size

logger = logging.getLogger("deadline.job_attachments.download")

# Default number of parallel download workers
DEFAULT_MAX_WORKERS = 10


@dataclass
class DownloadResult:
    """
    Result of a download_abs_manifest operation.

    Attributes:
        statistics: Progress metadata with final statistics about the download operation
            including downloaded and skipped counts.
        manifest: A copy of the input manifest with mtime values updated to match
            the actual local filesystem timestamps. This is useful for cross-OS
            scenarios where file system mtime precision differs (e.g., a snapshot
            created on Linux with nanosecond precision used on Windows with
            100-nanosecond precision). Using this updated manifest as the basis
            for subsequent diff operations ensures reliable change detection.
    """

    statistics: "DownloadProgressMetadata"
    manifest: AbsManifest


# =============================================================================
# Helper Functions
# =============================================================================


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


def _create_symlink(entry: ManifestFilePath) -> None:
    """Create a symlink for a symlink entry."""
    if entry.symlink_target is None:
        raise ValueError(f"Symlink entry '{entry.path}' has no symlink_target")

    local_path = _get_long_path_compatible_path(Path(entry.path))
    target_path = Path(entry.symlink_target)

    # Remove existing symlink if present
    if local_path.is_symlink():
        local_path.unlink()
    elif local_path.exists():
        if local_path.is_dir():
            import shutil

            shutil.rmtree(local_path)
        else:
            local_path.unlink()

    local_path.symlink_to(target_path)
    logger.debug(f"Created symlink {entry.path} -> {entry.symlink_target}")


def _sort_symlinks_by_dependency(symlinks: List[ManifestFilePath]) -> List[ManifestFilePath]:
    """
    Sort symlinks so that targets are created before symlinks that point to them.
    """
    if not symlinks:
        return []

    path_to_entry: dict[str, ManifestFilePath] = {entry.path: entry for entry in symlinks}
    dependencies: dict[str, List[str]] = {entry.path: [] for entry in symlinks}

    for entry in symlinks:
        target = entry.symlink_target
        if target and target in path_to_entry:
            dependencies[entry.path].append(target)

    in_degree: dict[str, int] = {path: 0 for path in path_to_entry}
    for path, deps in dependencies.items():
        for dep in deps:
            in_degree[dep] += 1

    queue: deque[str] = deque(path for path, degree in in_degree.items() if degree == 0)
    sorted_paths: List[str] = []

    while queue:
        path = queue.popleft()
        sorted_paths.append(path)
        for dep in dependencies[path]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    cyclic_symlinks: List[ManifestFilePath] = []
    if len(sorted_paths) != len(symlinks):
        sorted_set = set(sorted_paths)
        cyclic_symlinks = [entry for entry in symlinks if entry.path not in sorted_set]
        logger.warning(
            f"Detected cycle in symlink dependencies involving {len(cyclic_symlinks)} symlinks"
        )

    sorted_paths.reverse()
    result = [path_to_entry[path] for path in sorted_paths]
    result.extend(cyclic_symlinks)
    return result


def _delete_file(path: str) -> None:
    """Delete a file or symlink at the given path."""
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
    """Delete an empty directory at the given path."""
    local_path = _get_long_path_compatible_path(Path(path))

    if local_path.is_symlink():
        logger.debug(f"Expected directory but found symlink, skipping deletion: {path}")
    elif local_path.is_file():
        logger.debug(f"Expected directory but found file, skipping deletion: {path}")
    elif local_path.is_dir():
        try:
            local_path.rmdir()
            logger.debug(f"Deleted empty directory {path}")
        except OSError:
            logger.debug(f"Directory not empty, skipping deletion: {path}")
    else:
        logger.debug(f"Directory does not exist, skipping deletion: {path}")


def _create_directory(dir_entry: ManifestDirectoryPath) -> None:
    """Create a directory from a directory entry."""
    local_path = _get_long_path_compatible_path(Path(dir_entry.path))
    local_path.mkdir(parents=True, exist_ok=True)
    logger.debug(f"Created directory {dir_entry.path}")


def _collect_directories_with_files(
    manifest: AbsManifest,
    files_to_download: List[ManifestFilePath],
) -> Tuple[List[str], Dict[str, List[ManifestFilePath]]]:
    """Collect all directories and map each directory to the files it contains."""
    dirs_to_create: set[str] = set()
    dir_to_files: Dict[str, List[ManifestFilePath]] = {}

    for dir_entry in manifest.dirs:
        if not dir_entry.deleted:
            dirs_to_create.add(dir_entry.path)

    for entry in files_to_download:
        path = entry.path
        last_slash = path.rfind("/")
        if last_slash <= 0:
            parent_dir = ""
        else:
            parent_dir = path[:last_slash]
            if os.name == "nt" and len(parent_dir) == 2 and parent_dir[1] == ":":
                parent_dir = ""

        if parent_dir not in dir_to_files:
            dir_to_files[parent_dir] = []
        dir_to_files[parent_dir].append(entry)
        dirs_to_create.add(parent_dir)

        ancestor = parent_dir
        while ancestor:
            last_slash = ancestor.rfind("/")
            if last_slash <= 0:
                break
            ancestor = ancestor[:last_slash]
            if os.name == "nt" and len(ancestor) == 2 and ancestor[1] == ":":
                break
            dirs_to_create.add(ancestor)

    sorted_dirs = sorted(dirs_to_create, key=len)
    return sorted_dirs, dir_to_files


def _create_directory_path(dir_path: str) -> None:
    """Create a single directory."""
    local_path = _get_long_path_compatible_path(Path(dir_path))
    local_path.mkdir(exist_ok=True)
    logger.debug("Created directory %s", dir_path)


def _build_updated_manifest(
    manifest: AbsManifest,
    updated_mtimes: Dict[str, int],
) -> AbsManifest:
    """Build a copy of the manifest with mtime values updated to match filesystem."""
    updated_files: List[ManifestFilePath] = []
    for entry in manifest.files:
        if entry.path in updated_mtimes:
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
            updated_files.append(entry)

    if isinstance(manifest, AbsSnapshot):
        return AbsSnapshot(
            hash_alg=manifest.hashAlg,
            files=updated_files,
            total_size=manifest.totalSize,
            dirs=manifest.dirs,
            parent_manifest_hash=manifest.parentManifestHash,
            file_chunk_size_bytes=manifest.fileChunkSizeBytes,
        )
    else:
        return AbsSnapshotDiff(
            hash_alg=manifest.hashAlg,
            files=updated_files,
            total_size=manifest.totalSize,
            dirs=manifest.dirs,
            parent_manifest_hash=manifest.parentManifestHash,
            file_chunk_size_bytes=manifest.fileChunkSizeBytes,
        )


# =============================================================================
# Main Download Function
# =============================================================================


def download_abs_manifest(
    manifest: AbsManifest,
    data_cache: ContentAddressedDataCache,
    *,
    hash_cache: Optional[HashCache] = None,
    file_conflict_resolution: FileConflictResolution = FileConflictResolution.OVERWRITE,
    apply_deletes: bool = True,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.PRESERVE,
    max_workers: Optional[int] = None,
    on_progress: Optional[DownloadProgressCallback] = None,
) -> DownloadResult:
    """
    Download files from a data cache to the local filesystem.

    Downloads all files in the manifest from the data cache (S3 or filesystem)
    to the local filesystem, using the absolute paths in the manifest as the
    target locations.

    Args:
        manifest: Manifest with absolute paths and hashes. Can be
            AbsSnapshot or AbsSnapshotDiff.
        data_cache: Data cache to download from (S3DataCache or FileSystemDataCache)
        hash_cache: Optional hash cache to check for files that already have the
            correct content. If a file exists locally and its cached hash matches
            the expected hash from the manifest, the download is skipped.
        file_conflict_resolution: How to handle existing files. Default OVERWRITE.
        apply_deletes: If True (default), apply deletions from diff manifests.
        symlink_policy: How to handle symlinks. Default PRESERVE.
        max_workers: Maximum parallel download workers. Default: 10.
        on_progress: Optional callback for progress reporting. Called periodically with
            DownloadProgressMetadata. Return True to continue, False to cancel.

    Returns:
        DownloadResult containing statistics and updated manifest.

    Raises:
        ValueError: If the manifest contains relative paths or unsupported symlink_policy
        AssetSyncCancelledError: If cancelled via on_progress callback returning False
    """
    _validate_absolute_paths(manifest)

    if symlink_policy not in (SymlinkPolicy.PRESERVE, SymlinkPolicy.EXCLUDE_ALL):
        raise ValueError(
            f"DOWNLOAD operation only supports PRESERVE or EXCLUDE_ALL symlink policies. "
            f"Got: {symlink_policy.value}"
        )

    hash_alg = manifest.hashAlg.value
    hash_alg_enum = manifest.hashAlg

    if max_workers is None:
        max_workers = DEFAULT_MAX_WORKERS

    # Categorize entries
    regular_files: List[ManifestFilePath] = []
    chunked_files: List[ManifestFilePath] = []
    symlinks: List[ManifestFilePath] = []
    deleted_files: List[ManifestFilePath] = []
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

    # Calculate totals - count chunks separately for chunked files
    chunk_size_bytes = manifest.fileChunkSizeBytes
    total_file_chunks = len(regular_files)
    for entry in chunked_files:
        if entry.chunkhashes:
            total_file_chunks += len(entry.chunkhashes)
    # DOWNLOAD only supports PRESERVE (create symlinks) or EXCLUDE_ALL (skip symlinks).
    # Only count symlinks in progress when they'll actually be created.
    if symlink_policy == SymlinkPolicy.PRESERVE:
        total_file_chunks += len(symlinks)

    total_bytes = sum((e.size or 0) for e in regular_files) + sum(
        (e.size or 0) for e in chunked_files
    )

    # Set up progress state - always create to track statistics, even without callback
    progress_state = _DownloadProgressState(
        total_file_chunks=total_file_chunks,
        total_bytes=total_bytes,
        on_progress=on_progress,  # May be None
    )

    start_time = time.perf_counter()

    collision_lock = Lock()
    collision_file_dict: DefaultDict[str, int] = defaultdict(int)
    updated_mtimes: Dict[str, int] = {}

    try:
        # 1. Process deletions first (for diff manifests only)
        if apply_deletes and isinstance(manifest, AbsSnapshotDiff):
            sorted_deleted_files = sorted(deleted_files, key=lambda e: len(e.path), reverse=True)
            sorted_deleted_dirs = sorted(
                deleted_directories, key=lambda d: len(d.path), reverse=True
            )

            for entry in sorted_deleted_files:
                _delete_file(entry.path)
                logger.debug("Deleted: %s", entry.path)

            for dir_entry in sorted_deleted_dirs:
                _delete_directory(dir_entry.path)
                logger.debug("Deleted directory: %s", dir_entry.path)

        # 2. Collect directories
        all_files_to_download = regular_files + chunked_files
        sorted_dirs, dir_to_files = _collect_directories_with_files(manifest, all_files_to_download)

        # 3. Download files using callback-based pipeline
        # Interleave directory creation with file submission so the pipeline
        # can start processing files while directories are still being created
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            pipeline_class = (
                S3DownloadPipeline
                if isinstance(data_cache, S3DataCache)
                else FileSystemDownloadPipeline
            )
            pipeline: DownloadPipelineBase = pipeline_class(
                executor=executor,
                data_cache=data_cache,
                hash_alg=hash_alg,
                hash_alg_enum=hash_alg_enum,
                chunk_size_bytes=chunk_size_bytes,
                hash_cache=hash_cache,
                collision_lock=collision_lock,
                collision_file_dict=collision_file_dict,
                file_conflict_resolution=file_conflict_resolution,
                progress_state=progress_state,
            )

            # Create each directory and immediately submit its files
            for dir_path in sorted_dirs:
                # Create directory (skip empty string which represents root)
                if dir_path:
                    _create_directory_path(dir_path)

                # Submit files for this directory immediately
                if dir_path in dir_to_files:
                    for entry in dir_to_files[dir_path]:
                        if entry.chunkhashes is not None:
                            pipeline.submit_chunked_file(entry)
                        else:
                            pipeline.submit_single_file(entry)

            has_files = len(regular_files) > 0 or len(chunked_files) > 0
            if has_files:
                while not pipeline._done_event.wait(timeout=0.1):
                    if progress_state and progress_state.is_cancelled():
                        pipeline.cancel()
                        raise AssetSyncCancelledError("Download cancelled.")

            results = pipeline.wait_for_completion()

        # Process results
        for result in results:
            if result.was_skipped:
                logger.debug("Skipped: %s", result.entry.path)
            else:
                if result.actual_mtime_us is not None:
                    updated_mtimes[result.entry.path] = result.actual_mtime_us
                file_type = "chunked file" if result.entry.chunkhashes else "file"
                logger.debug("Downloaded %s: %s", file_type, result.entry.path)

        # 5. Create symlinks (if policy is PRESERVE)
        if symlink_policy == SymlinkPolicy.PRESERVE:
            sorted_symlinks = _sort_symlinks_by_dependency(symlinks)
            for entry in sorted_symlinks:
                _create_symlink(entry)
                # Record symlink as downloaded (0 bytes)
                progress_state.record_download_complete(0, skipped=False)
                logger.debug("Created symlink: %s", entry.path)

    except AssetSyncCancelledError:
        downloaded = progress_state.downloaded_file_chunks
        raise AssetSyncCancelledError(
            f"Download cancelled. "
            f"(Downloaded {downloaded} file{'s' if downloaded != 1 else ''} "
            f"before cancellation.)"
        )

    # Force final progress callback
    if progress_state.on_progress is not None:
        progress_state.force_callback()

    total_time = time.perf_counter() - start_time
    transfer_rate = progress_state.total_bytes / total_time if total_time > 0 else 0.0

    # Build summary message - use total_bytes for rate calculation
    # Use "files" when processing whole files, "chunks" when chunking is enabled
    unit = "files" if chunk_size_bytes <= 0 else "chunks"
    summary_parts = [
        f"Downloaded {human_readable_file_size(progress_state.total_bytes)}",
        f"({progress_state.total_file_chunks} {unit})",
        f"in {total_time:.2f}s",
    ]
    if total_time > 0:
        summary_parts.append(f"({human_readable_file_size(int(transfer_rate))}/s)")

    statistics = DownloadProgressMetadata(
        total_file_chunks=progress_state.total_file_chunks,
        total_bytes=progress_state.total_bytes,
        downloaded_file_chunks=progress_state.downloaded_file_chunks,
        downloaded_bytes=progress_state.downloaded_bytes,
        skipped_file_chunks=progress_state.skipped_file_chunks,
        skipped_bytes=progress_state.skipped_bytes,
        progress=100.0,
        progressMessage=" ".join(summary_parts),
        total_time=total_time,
        transfer_rate=transfer_rate,
    )

    updated_manifest = _build_updated_manifest(manifest, updated_mtimes)
    return DownloadResult(statistics=statistics, manifest=updated_manifest)
