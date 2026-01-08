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
- Parallel downloads for improved throughput
- Support for chunked large files (>256MB)
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
    COMMON_ERROR_GUIDANCE_FOR_S3,
)
from ..._utils import _get_long_path_compatible_path
from ...caches.hash_cache import HashCache, HashCacheEntry, WHOLE_FILE_RANGE_END

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


def _download_file_from_s3(
    s3_client: Any,
    s3_bucket: str,
    s3_key: str,
    local_path: Path,
    expected_size: Optional[int],
    progress_tracker: Optional[ProgressTracker],
) -> int:
    """
    Download a single file from S3 to local filesystem atomically.

    Downloads to a temporary file first, then atomically moves to the target path.
    This ensures the target file is never in a partial/corrupt state.

    Returns the number of bytes downloaded.
    """
    import secrets
    from boto3.s3.transfer import TransferConfig

    local_path.parent.mkdir(parents=True, exist_ok=True)

    # Create temp file path beside the target
    temp_suffix = secrets.token_hex(5)
    temp_path = local_path.parent / f"{local_path.name}.tmp{temp_suffix}"

    config = TransferConfig()
    bytes_downloaded = 0

    def progress_callback(bytes_amount: int) -> None:
        nonlocal bytes_downloaded
        bytes_downloaded += bytes_amount
        if progress_tracker:
            progress_tracker.track_progress_callback(bytes_amount)

    try:
        s3_client.download_file(
            Bucket=s3_bucket,
            Key=s3_key,
            Filename=str(temp_path),
            Config=config,
            Callback=progress_callback,
        )
        # Atomically move temp file to target
        os.replace(temp_path, local_path)
        return bytes_downloaded
    except ClientError as exc:
        status_code = int(exc.response["ResponseMetadata"]["HTTPStatusCode"])
        status_code_guidance = {
            **COMMON_ERROR_GUIDANCE_FOR_S3,
            403: (
                "Forbidden or Access denied. Please check your AWS credentials, and ensure "
                "that your AWS IAM Role or User has the 's3:GetObject' permission."
            ),
            404: "Not found. Please check your bucket name and object key.",
        }
        raise JobAttachmentsS3ClientError(
            action="downloading file",
            status_code=status_code,
            bucket_name=s3_bucket,
            key_or_prefix=s3_key,
            message=f"{status_code_guidance.get(status_code, '')} {str(exc)}",
        ) from exc
    except BotoCoreError as bce:
        raise JobAttachmentS3BotoCoreError(
            action="downloading file",
            error_details=str(bce),
        ) from bce
    finally:
        # Clean up temp file if it still exists (i.e., on error before os.replace)
        temp_path.unlink(missing_ok=True)


def _download_file_from_filesystem(
    source_path: Path,
    local_path: Path,
    expected_size: Optional[int],
    progress_tracker: Optional[ProgressTracker],
) -> int:
    """
    Copy a file from filesystem cache to local filesystem atomically.

    Copies to a temporary file first, then atomically moves to the target path.
    This ensures the target file is never in a partial/corrupt state.

    Returns the number of bytes copied.
    """
    import secrets
    import shutil

    local_path.parent.mkdir(parents=True, exist_ok=True)

    # Create temp file path beside the target
    temp_suffix = secrets.token_hex(5)
    temp_path = local_path.parent / f"{local_path.name}.tmp{temp_suffix}"

    try:
        # Copy to temp file
        shutil.copy2(source_path, temp_path)

        file_size = temp_path.stat().st_size
        if progress_tracker:
            progress_tracker.track_progress_callback(file_size)

        # Atomically move temp file to target
        os.replace(temp_path, local_path)

        return file_size
    finally:
        # Clean up temp file if it still exists (i.e., on error before os.replace)
        temp_path.unlink(missing_ok=True)


def _download_single_file(
    entry: ManifestFilePath,
    hash_alg: str,
    hash_alg_enum: HashAlgorithm,
    data_cache: ContentAddressedDataCache,
    hash_cache: Optional[HashCache],
    collision_lock: Lock,
    collision_file_dict: DefaultDict[str, int],
    file_conflict_resolution: FileConflictResolution,
    progress_tracker: Optional[ProgressTracker],
) -> Tuple[int, Optional[Path], bool, Optional[int]]:
    """
    Download a single file entry from the data cache.

    Returns:
        Tuple of (bytes_downloaded, local_path or None if skipped, was_skipped, actual_mtime_us)
        actual_mtime_us is the actual filesystem mtime in microseconds, or None if skipped.
    """
    if entry.hash is None:
        raise ValueError(f"File entry '{entry.path}' has no hash")

    local_path = _get_long_path_compatible_path(Path(entry.path))
    file_size = entry.size or 0

    # Check hash cache first - if file already has correct content, skip download
    can_skip, cached_mtime_us = _check_hash_cache_for_skip(entry, hash_alg_enum, hash_cache)
    if can_skip:
        return (file_size, local_path, True, cached_mtime_us)

    # Handle file conflicts
    if local_path.exists():
        if file_conflict_resolution == FileConflictResolution.SKIP:
            return (file_size, None, True, None)
        elif file_conflict_resolution == FileConflictResolution.OVERWRITE:
            pass  # Continue to download
        elif file_conflict_resolution == FileConflictResolution.CREATE_COPY:
            local_path = _get_new_copy_file_path(local_path, collision_lock, collision_file_dict)
            local_path = _get_long_path_compatible_path(local_path)
        else:
            raise ValueError(f"Unknown file conflict resolution: {file_conflict_resolution}")

    # Create parent directories
    local_path.parent.mkdir(parents=True, exist_ok=True)

    # Download based on data cache type
    if isinstance(data_cache, S3DataCache):
        s3_key = data_cache.get_object_key(entry.hash, hash_alg)
        bytes_downloaded = _download_file_from_s3(
            s3_client=data_cache.s3_client,
            s3_bucket=data_cache.s3_bucket,
            s3_key=s3_key,
            local_path=local_path,
            expected_size=entry.size,
            progress_tracker=progress_tracker,
        )
    elif isinstance(data_cache, FileSystemDataCache):
        source_path = Path(data_cache.get_object_key(entry.hash, hash_alg))
        bytes_downloaded = _download_file_from_filesystem(
            source_path=source_path,
            local_path=local_path,
            expected_size=entry.size,
            progress_tracker=progress_tracker,
        )
    else:
        raise TypeError(f"Unsupported data cache type: {type(data_cache)}")

    # Restore modification time if available
    if entry.mtime is not None:
        mtime_seconds = entry.mtime / 1_000_000  # Convert from microseconds
        os.utime(local_path, (mtime_seconds, mtime_seconds))

    # Get the actual filesystem mtime (may differ from requested due to OS precision)
    # Use st_mtime_ns with integer division to avoid floating point precision issues
    stat_result = local_path.stat()
    actual_mtime_ns = stat_result.st_mtime_ns
    actual_mtime_us = actual_mtime_ns // 1_000

    # Update hash cache with the downloaded file's hash and actual mtime
    # Use resolved path because hash cache always uses resolved paths
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

    logger.debug(f"Downloaded {entry.path} to {local_path}")
    return (bytes_downloaded, local_path, False, actual_mtime_us)


def _download_chunked_file(
    entry: ManifestFilePath,
    hash_alg: str,
    data_cache: ContentAddressedDataCache,
    collision_lock: Lock,
    collision_file_dict: DefaultDict[str, int],
    file_conflict_resolution: FileConflictResolution,
    progress_tracker: Optional[ProgressTracker],
) -> Tuple[int, Optional[Path], bool, Optional[int]]:
    """
    Download a chunked file (>256MB) by downloading each chunk and concatenating.

    Downloads to a temporary file first, then atomically moves to the target path.
    This ensures the target file is never in a partial/corrupt state during the
    potentially long download of multiple chunks.

    Returns:
        Tuple of (bytes_downloaded, local_path or None if skipped, was_skipped, actual_mtime_us)
        actual_mtime_us is the actual filesystem mtime in microseconds, or None if skipped.
    """
    import secrets

    if entry.chunkhashes is None:
        raise ValueError(f"Chunked file entry '{entry.path}' has no chunkhashes")

    local_path = _get_long_path_compatible_path(Path(entry.path))
    file_size = entry.size or 0

    # Handle file conflicts
    if local_path.exists():
        if file_conflict_resolution == FileConflictResolution.SKIP:
            return (file_size, None, True, None)
        elif file_conflict_resolution == FileConflictResolution.OVERWRITE:
            pass  # Continue to download
        elif file_conflict_resolution == FileConflictResolution.CREATE_COPY:
            local_path = _get_new_copy_file_path(local_path, collision_lock, collision_file_dict)
            local_path = _get_long_path_compatible_path(local_path)
        else:
            raise ValueError(f"Unknown file conflict resolution: {file_conflict_resolution}")

    # Create parent directories
    local_path.parent.mkdir(parents=True, exist_ok=True)

    # Create temp file path beside the target for atomic write
    temp_suffix = secrets.token_hex(5)
    temp_path = local_path.parent / f"{local_path.name}.tmp{temp_suffix}"

    # Download chunks sequentially and write to temp file
    total_bytes = 0
    try:
        with open(temp_path, "wb") as f:
            for chunk_idx, chunk_hash in enumerate(entry.chunkhashes):
                if isinstance(data_cache, S3DataCache):
                    s3_key = data_cache.get_object_key(chunk_hash, hash_alg)
                    # Download chunk to another temp location then append
                    chunk_temp_suffix = secrets.token_hex(5)
                    chunk_tmp_path = (
                        local_path.parent / f"{local_path.name}.chunk{chunk_temp_suffix}"
                    )
                    try:
                        # Use a simple download without atomic move for chunks
                        # since we're writing to our own temp file
                        from boto3.s3.transfer import TransferConfig

                        config = TransferConfig()
                        chunk_bytes = 0

                        def chunk_progress(bytes_amount: int) -> None:
                            nonlocal chunk_bytes
                            chunk_bytes += bytes_amount
                            if progress_tracker:
                                progress_tracker.track_progress_callback(bytes_amount)

                        data_cache.s3_client.download_file(
                            Bucket=data_cache.s3_bucket,
                            Key=s3_key,
                            Filename=str(chunk_tmp_path),
                            Config=config,
                            Callback=chunk_progress,
                        )
                        with open(chunk_tmp_path, "rb") as chunk_file:
                            chunk_data = chunk_file.read()
                            f.write(chunk_data)
                            total_bytes += len(chunk_data)
                    finally:
                        chunk_tmp_path.unlink(missing_ok=True)

                elif isinstance(data_cache, FileSystemDataCache):
                    source_path = Path(data_cache.get_object_key(chunk_hash, hash_alg))
                    with open(source_path, "rb") as chunk_file:
                        chunk_data = chunk_file.read()
                        f.write(chunk_data)
                        total_bytes += len(chunk_data)
                        if progress_tracker:
                            progress_tracker.track_progress_callback(len(chunk_data))
                else:
                    raise TypeError(f"Unsupported data cache type: {type(data_cache)}")

        # Atomically move temp file to target
        os.replace(temp_path, local_path)

        # Restore modification time if available
        if entry.mtime is not None:
            mtime_seconds = entry.mtime / 1_000_000  # Convert from microseconds
            os.utime(local_path, (mtime_seconds, mtime_seconds))

        # Get the actual filesystem mtime (may differ from requested due to OS precision)
        # Use st_mtime_ns with integer division to avoid floating point precision issues
        actual_mtime_us = local_path.stat().st_mtime_ns // 1_000

        logger.debug(f"Downloaded chunked file {entry.path} ({len(entry.chunkhashes)} chunks)")
        return (total_bytes, local_path, False, actual_mtime_us)

    finally:
        # Clean up temp file if it still exists (i.e., on error before os.replace)
        temp_path.unlink(missing_ok=True)


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

        # 3. Download regular files in parallel
        if regular_files:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        _download_single_file,
                        entry,
                        hash_alg,
                        hash_alg_enum,
                        data_cache,
                        hash_cache,
                        collision_lock,
                        collision_file_dict,
                        file_conflict_resolution,
                        progress_tracker,
                    ): entry
                    for entry in regular_files
                }

                for future in concurrent.futures.as_completed(futures):
                    entry = futures[future]
                    try:
                        bytes_downloaded, local_path, was_skipped, actual_mtime_us = future.result()

                        if was_skipped:
                            skipped_files += 1
                            skipped_bytes += bytes_downloaded
                            progress_tracker.increase_skipped(1, bytes_downloaded)
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

                        progress_tracker.report_progress()
                        print_function_callback(f"Downloaded: {entry.path}")

                    except Exception:
                        if progress_tracker and not progress_tracker.continue_reporting:
                            raise AssetSyncCancelledError("Download cancelled.")
                        raise

        # 4. Download chunked files sequentially (they're large)
        for entry in chunked_files:
            if progress_tracker and not progress_tracker.continue_reporting:
                raise AssetSyncCancelledError("Download cancelled.")

            bytes_downloaded, local_path, was_skipped, actual_mtime_us = _download_chunked_file(
                entry,
                hash_alg,
                data_cache,
                collision_lock,
                collision_file_dict,
                file_conflict_resolution,
                progress_tracker,
            )

            if was_skipped:
                skipped_files += 1
                skipped_bytes += bytes_downloaded
                progress_tracker.increase_skipped(1, bytes_downloaded)
            else:
                processed_files += 1
                processed_bytes += bytes_downloaded
                progress_tracker.increase_processed(1, 0)
                if local_path:
                    root = str(local_path.parent)
                    downloaded_files_by_root[root].append(str(local_path))
                # Track the actual mtime from the filesystem
                if actual_mtime_us is not None:
                    updated_mtimes[entry.path] = actual_mtime_us

            progress_tracker.report_progress()
            print_function_callback(f"Downloaded chunked file: {entry.path}")

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
