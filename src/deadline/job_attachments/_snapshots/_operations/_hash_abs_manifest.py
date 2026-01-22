# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for filling in hashes for manifest objects that were created by collect_abs_snapshot
or diff_snapshots.

This module implements the HASH operation from the composable manifest operations design:
    HASH: AbsManifest (with hash=None) → HashResult

Where AbsManifest can be either:
    - AbsSnapshot: A full directory tree snapshot with absolute paths
    - AbsSnapshotDiff: A diff manifest with absolute paths (contains new/modified/deleted entries)

The separation of collection from hashing enables:
- Fast diff comparison by mtime/size without hashing unchanged files
- Hash cache integration - only hash files with cache misses
- Deferred hashing - collect structure first, hash only what's needed
- Force rehash option - recalculate all hashes when needed
- Progress tracking with cancellation support

All composable operations use v2025 structure and semantics internally. Support for
v2023 on-disk format is provided via lossy conversion functions that drop symlinks,
deletions, and other v2025-only features.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, List, Optional

from .._manifest import (
    AbsManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    _is_absolute_path,
)
from ...asset_manifests.hash_algorithms import hash_file, HashAlgorithm
from ...caches.hash_cache import HashCache, HashCacheEntry, WHOLE_FILE_RANGE_END
from ...progress_tracker import human_readable_file_size

logger = logging.getLogger(__name__)

# Default interval for progress callbacks (5 times per second)
DEFAULT_PROGRESS_CALLBACK_INTERVAL = 0.2  # seconds

# Time window for calculating rate (in seconds)
RATE_WINDOW_SECONDS = 12.0


@dataclass
class HashProgressMetadata:
    """
    Progress metadata for hash_abs_manifest operation.

    Reports progress for the hashing phase. For chunked files, each chunk
    is counted separately in the file/chunk counts.
    """

    # Totals
    total_file_chunks: int  # Total files + chunks to process
    total_bytes: int

    # Hashing phase progress
    hashed_file_chunks: int
    hashed_bytes: int
    skipped_file_chunks: int  # Skipped due to hash cache hit
    skipped_bytes: int

    # Overall progress
    progress: float  # 0-100
    progressMessage: str

    # Timing information
    total_time: float = 0.0  # Elapsed time since operation start (seconds)
    rate: float = 0.0  # Current hashing rate (bytes/second)


# Callback type for hash progress reporting
# Return True to continue, False to cancel the operation
HashProgressCallback = Callable[[HashProgressMetadata], bool]


@dataclass
class HashResult:
    """
    Result of a hash_abs_manifest operation.

    Attributes:
        statistics: Progress metadata with final statistics about the hash operation
            including hashed and skipped counts.
        manifest: The manifest with all hashes filled in.
    """

    statistics: HashProgressMetadata
    manifest: AbsManifest


@dataclass
class _ProgressHistoryEntry:
    """A single entry in the progress history for rate calculation."""

    timestamp: float  # Time since operation start (seconds)
    hashed_bytes: int  # Total hashed bytes at this point


@dataclass
class _HashProgressState:
    """
    Progress state for hash operation.

    Tracks bytes and file/chunk counts. For chunked files, each chunk is counted separately.
    """

    # Totals (set once at initialization)
    total_file_chunks: int = 0
    total_bytes: int = 0

    # Progress counters
    hashed_file_chunks: int = 0
    hashed_bytes: int = 0
    skipped_file_chunks: int = 0
    skipped_bytes: int = 0

    # Callback and timing
    on_progress: Optional[HashProgressCallback] = None
    callback_interval: float = DEFAULT_PROGRESS_CALLBACK_INTERVAL
    _last_callback_time: float = field(default_factory=time.perf_counter)
    _cancelled: bool = False

    # Start time for total_time calculation
    _start_time: float = field(default_factory=time.perf_counter)

    # Progress history for sliding window rate calculation
    _progress_history: Deque[_ProgressHistoryEntry] = field(default_factory=deque)

    def record_hash_complete(self, chunk_bytes: int, skipped: bool) -> None:
        """Record completion of hashing for a file or chunk."""
        if skipped:
            self.skipped_bytes += chunk_bytes
            self.skipped_file_chunks += 1
        else:
            self.hashed_bytes += chunk_bytes
            self.hashed_file_chunks += 1
        self._maybe_invoke_callback()

    def _maybe_invoke_callback(self) -> None:
        """Invoke callback if interval has elapsed."""
        if self.on_progress is None or self._cancelled:
            return

        now = time.perf_counter()
        if now - self._last_callback_time < self.callback_interval:
            return

        self._last_callback_time = now
        metadata = self._build_metadata()

        should_continue = self.on_progress(metadata)
        if not should_continue:
            self._cancelled = True

    def _build_metadata(self) -> HashProgressMetadata:
        """Build progress metadata."""
        completed_bytes = self.hashed_bytes + self.skipped_bytes
        progress = (completed_bytes / self.total_bytes * 100) if self.total_bytes > 0 else 0.0

        # Calculate total_time since operation started
        now = time.perf_counter()
        total_time = now - self._start_time

        # Calculate rate using sliding window
        rate = self._calculate_rate(total_time, completed_bytes)

        # Format elapsed time
        if total_time < 60:
            time_str = f"{total_time:.1f}s"
        else:
            minutes = int(total_time // 60)
            seconds = total_time % 60
            time_str = f"{minutes}:{seconds:04.1f}"

        # Build progress message with rate
        rate_str = f"{human_readable_file_size(int(rate))}/s" if rate > 0 else ""

        msg_parts = [
            f"Hashed {human_readable_file_size(completed_bytes)}",
            f"/ {human_readable_file_size(self.total_bytes)}",
            f"[{time_str}]",
        ]
        if rate_str:
            msg_parts.append(f"({rate_str})")

        msg = " ".join(msg_parts)

        return HashProgressMetadata(
            total_file_chunks=self.total_file_chunks,
            total_bytes=self.total_bytes,
            hashed_file_chunks=self.hashed_file_chunks,
            hashed_bytes=self.hashed_bytes,
            skipped_file_chunks=self.skipped_file_chunks,
            skipped_bytes=self.skipped_bytes,
            progress=progress,
            progressMessage=msg,
            total_time=total_time,
            rate=rate,
        )

    def _calculate_rate(self, current_time: float, current_bytes: int) -> float:
        """
        Calculate rate using a sliding window approach.

        Uses a deque of progress history entries. The window is RATE_WINDOW_SECONDS.
        """
        # Add current progress to history
        self._progress_history.append(
            _ProgressHistoryEntry(timestamp=current_time, hashed_bytes=current_bytes)
        )

        # Remove old entries from the front of the deque
        while (
            len(self._progress_history) > 1
            and current_time - self._progress_history[1].timestamp > RATE_WINDOW_SECONDS
        ):
            self._progress_history.popleft()

        # Calculate rate based on oldest entry in the window vs current
        if len(self._progress_history) < 2:
            return 0.0

        oldest = self._progress_history[0]
        time_delta = current_time - oldest.timestamp
        bytes_delta = current_bytes - oldest.hashed_bytes

        if time_delta > 0:
            return bytes_delta / time_delta
        return 0.0

    def is_cancelled(self) -> bool:
        """Check if operation was cancelled via callback."""
        return self._cancelled

    def force_callback(self) -> None:
        """Force a callback invocation (e.g., at end of operation)."""
        if self.on_progress is None or self._cancelled:
            return
        metadata = self._build_metadata()
        self.on_progress(metadata)


def hash_abs_manifest(
    manifest: AbsManifest,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    file_chunk_size_bytes: Optional[int] = None,
    on_progress: Optional[HashProgressCallback] = None,
) -> HashResult:
    """
    Fill in hashes for a manifest structure with absolute paths.

    Given a manifest with hash=None for file entries (from collect_abs_snapshot or
    diff_snapshots), computes and fills in the actual hashes.

    Args:
        manifest: Manifest with absolute paths and hash=None for unhashed files.
            Can be either:
            - AbsSnapshot (from collect_abs_snapshot)
            - AbsSnapshotDiff (from diff_snapshots with ignore_hashes=True)
        hash_cache: Optional hash cache for efficiency
        force_rehash: If True, ignore cache and recalculate all hashes
        file_chunk_size_bytes: Chunk size for output manifest.
            - None: Preserve the chunk size from the input manifest
            - WHOLE_FILE_CHUNK_SIZE (-1): Hash files as a whole, no chunking
            - Positive int: Chunk size in bytes for large files
        on_progress: Optional callback for progress reporting. Called periodically with
            HashProgressMetadata. Return True to continue, False to cancel the operation.

    Returns:
        HashResult containing:
        - statistics: HashProgressMetadata with hashed/skipped files and bytes
        - manifest: A NEW manifest of the same type with all hashes filled in

    Raises:
        ValueError: If the manifest contains relative paths (paths must be absolute)

    Hash Cache Behavior:
        - If hash_cache is provided and force_rehash=False:
          - Check cache using (path, mtime) as key
          - On cache hit: use cached hash
          - On cache miss: compute hash and update cache
        - If force_rehash=True: always compute hash, update cache
        - If hash_cache is None: always compute hash

    Manifest Type Handling:
        - Snapshot manifests: All file entries are hashed
        - Diff manifests: Only new/modified file entries are hashed;
          deleted entries are passed through unchanged (no hash needed)

    Chunking Behavior:
        - If effective chunk size is WHOLE_FILE_CHUNK_SIZE (-1): all files
          are hashed as a whole (no chunking regardless of file size)
        - If effective chunk size is a positive int: files larger than this size
          use chunked hashing with chunkhashes field

    Note:
        - Input manifest must have absolute paths (from collect_abs_snapshot or join_manifest)
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

    # Calculate totals for progress tracking
    total_file_chunks = 0
    total_bytes = 0
    for entry in manifest.files:
        if entry.symlink_target is not None or entry.deleted:
            continue
        if entry.size is not None:
            total_bytes += entry.size
            if chunking_enabled and entry.size > output_chunk_size:
                # Count chunks for large files
                num_chunks = (entry.size + output_chunk_size - 1) // output_chunk_size
                total_file_chunks += num_chunks
            else:
                total_file_chunks += 1

    start_time = time.perf_counter()

    # Create progress state
    progress_state = _HashProgressState(
        total_file_chunks=total_file_chunks,
        total_bytes=total_bytes,
        on_progress=on_progress,
    )

    hashed_paths: List[ManifestFilePath] = []
    result_total_size = 0

    for entry in manifest.files:
        # Check for cancellation
        if progress_state.is_cancelled():
            break

        # Symlinks don't need hashing - pass through unchanged
        if entry.symlink_target is not None:
            hashed_paths.append(
                ManifestFilePath(
                    path=entry.path,
                    symlink_target=entry.symlink_target,
                )
            )
            logger.debug("Symlink (no hash): %s", entry.path)
            continue

        # Deleted entries don't need hashing - pass through unchanged
        if entry.deleted:
            hashed_paths.append(
                ManifestFilePath(
                    path=entry.path,
                    deleted=True,
                )
            )
            continue

        abs_path = Path(entry.path)
        # Use resolved path as cache key for consistency
        cache_key = str(abs_path.resolve())

        # Check if file needs chunking (only if chunking enabled and file is larger than chunk size)
        needs_chunking = (
            chunking_enabled and entry.size is not None and entry.size > output_chunk_size
        )

        if needs_chunking:
            # Large file: hash and chunkhashes should both be None (unhashed)
            if entry.hash is not None:
                raise ValueError(
                    f"Large file '{entry.path}' (size={entry.size}) should have hash=None, "
                    f"got hash={entry.hash!r}"
                )
            if entry.chunkhashes is not None:
                raise ValueError(
                    f"Large file '{entry.path}' (size={entry.size}) should have chunkhashes=None "
                    f"(unhashed), got chunkhashes={entry.chunkhashes!r}"
                )

            # Compute chunk hashes
            chunk_hashes = _hash_file_chunked(
                file_path=abs_path,
                cache_key=cache_key,
                file_size=entry.size,  # type: ignore[arg-type]
                mtime=entry.mtime,
                hash_alg=manifest.hashAlg,
                chunk_size=output_chunk_size,
                hash_cache=hash_cache,
                force_rehash=force_rehash,
                progress_state=progress_state,
            )

            hashed_paths.append(
                ManifestFilePath(
                    path=entry.path,
                    chunkhashes=chunk_hashes,
                    size=entry.size,
                    mtime=entry.mtime,
                    runnable=entry.runnable,
                )
            )
            logger.debug("Hashed (chunked, %d chunks): %s", len(chunk_hashes), entry.path)
        else:
            # Small file or no chunking: hash should be None (unhashed), chunkhashes should be None
            if entry.hash is not None:
                raise ValueError(
                    f"File '{entry.path}' should have hash=None (unhashed), got hash={entry.hash!r}"
                )
            if entry.chunkhashes is not None:
                raise ValueError(
                    f"File '{entry.path}' should have chunkhashes=None, "
                    f"got chunkhashes={entry.chunkhashes!r}"
                )

            # Compute hash (whole file)
            file_hash, skipped = _get_or_compute_hash(
                file_path=abs_path,
                cache_key=cache_key,
                mtime=entry.mtime,
                hash_alg=manifest.hashAlg,
                hash_cache=hash_cache,
                force_rehash=force_rehash,
            )

            # Record progress
            chunk_bytes = entry.size if entry.size is not None else 0
            progress_state.record_hash_complete(chunk_bytes, skipped=skipped)

            hashed_paths.append(
                ManifestFilePath(
                    path=entry.path,
                    hash=file_hash,
                    size=entry.size,
                    mtime=entry.mtime,
                    runnable=entry.runnable,
                )
            )
            logger.debug("Hashed: %s", entry.path)

        if entry.size is not None:
            result_total_size += entry.size

    # Copy directory entries unchanged
    dir_entries: List[ManifestDirectoryPath] = []
    for d in manifest.dirs:
        dir_entries.append(
            ManifestDirectoryPath(
                path=d.path,
                deleted=d.deleted,
            )
        )

    # Force final progress callback
    if progress_state.on_progress is not None:
        progress_state.force_callback()

    # Build final statistics
    end_time = time.perf_counter()
    total_time = end_time - start_time
    rate = progress_state.total_bytes / total_time if total_time > 0 else 0.0

    # Build summary message
    unit = "files" if output_chunk_size <= 0 else "chunks"
    summary_parts = [
        f"Hashed {human_readable_file_size(progress_state.total_bytes)}",
        f"({progress_state.total_file_chunks} {unit})",
        f"in {total_time:.2f}s",
    ]
    if total_time > 0:
        summary_parts.append(f"({human_readable_file_size(int(rate))}/s)")

    statistics = HashProgressMetadata(
        total_file_chunks=progress_state.total_file_chunks,
        total_bytes=progress_state.total_bytes,
        hashed_file_chunks=progress_state.hashed_file_chunks,
        hashed_bytes=progress_state.hashed_bytes,
        skipped_file_chunks=progress_state.skipped_file_chunks,
        skipped_bytes=progress_state.skipped_bytes,
        progress=100.0,
        progressMessage=" ".join(summary_parts),
        total_time=total_time,
        rate=rate,
    )

    # Return the same manifest type as input
    manifest_type = type(manifest)
    result_manifest = manifest_type(
        hash_alg=manifest.hashAlg,
        dirs=dir_entries,
        files=hashed_paths,
        total_size=result_total_size,
        parent_manifest_hash=manifest.parentManifestHash,
        file_chunk_size_bytes=output_chunk_size,
    )

    return HashResult(
        statistics=statistics,
        manifest=result_manifest,
    )


def _validate_absolute_paths(manifest: AbsManifest) -> None:
    """Validate that all paths in the manifest are absolute."""
    for entry in manifest.files:
        if not _is_absolute_path(entry.path):
            raise ValueError(
                f"HASH operation requires absolute paths. "
                f"Found relative path: '{entry.path}'. "
                f"Use collect_abs_snapshot() or join_manifest() to create a manifest with absolute paths."
            )

    for d in manifest.dirs:
        if not _is_absolute_path(d.path):
            raise ValueError(
                f"HASH operation requires absolute paths. "
                f"Found relative directory path: '{d.path}'. "
                f"Use collect_abs_snapshot() or join_manifest() to create a manifest with absolute paths."
            )


def _get_or_compute_hash(
    file_path: Path,
    cache_key: str,
    mtime: Optional[int],
    hash_alg: HashAlgorithm,
    hash_cache: Optional[HashCache],
    force_rehash: bool,
    range_start: int = 0,
    range_end: int = WHOLE_FILE_RANGE_END,
) -> tuple[str, bool]:
    """
    Get hash from cache or compute it.

    Supports both whole-file hashes and byte-range hashes for chunked files.

    Args:
        file_path: Absolute path to the file
        cache_key: Key for cache lookup (the absolute path string)
        mtime: File modification time in microseconds
        hash_alg: Hash algorithm to use
        hash_cache: Optional hash cache
        force_rehash: If True, ignore cache
        range_start: Start byte offset (0 for whole-file or chunk start)
        range_end: End byte offset (-1 for whole-file, or chunk end exclusive)

    Returns:
        Tuple of (file_hash, skipped) where skipped is True if hash came from cache
    """
    # Convert mtime to string for cache lookup (cache stores as timestamp string)
    mtime_str = str(mtime) if mtime is not None else ""

    # Try cache first (unless force_rehash)
    if hash_cache is not None and not force_rehash:
        cache_entry = hash_cache.get_entry(cache_key, hash_alg, range_start, range_end)
        if cache_entry is not None and cache_entry.last_modified_time == mtime_str:
            return cache_entry.file_hash, True

    # Compute hash (works for both whole-file and byte-range)
    file_hash = hash_file(str(file_path), hash_alg, range_start, range_end)

    # Update cache
    if hash_cache is not None:
        hash_cache.put_entry(
            HashCacheEntry(
                file_path=cache_key,
                hash_algorithm=hash_alg,
                file_hash=file_hash,
                last_modified_time=mtime_str,
                range_start=range_start,
                range_end=range_end,
            )
        )

    return file_hash, False


def _hash_file_chunked(
    file_path: Path,
    cache_key: str,
    file_size: int,
    mtime: Optional[int],
    hash_alg: HashAlgorithm,
    chunk_size: int,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    progress_state: Optional[_HashProgressState] = None,
) -> List[str]:
    """
    Hash a file in chunks, returning a list of chunk hashes.

    Uses the hash cache for each chunk when available, allowing it to
    resume part-way through a file without recomputing hashes when
    hashing is interrupted.

    Args:
        file_path: Absolute path to the file
        cache_key: Key for cache lookup (the absolute path string)
        file_size: Size of the file in bytes
        mtime: File modification time in microseconds
        hash_alg: Hash algorithm to use
        chunk_size: Size of each chunk in bytes
        hash_cache: Optional hash cache for efficiency
        force_rehash: If True, ignore cache and recalculate all hashes
        progress_state: Optional progress state for tracking

    Returns:
        List of hash strings, one per chunk
    """
    chunk_hashes: List[str] = []
    offset = 0

    while offset < file_size:
        range_start = offset
        range_end = min(offset + chunk_size, file_size)
        chunk_bytes = range_end - range_start

        chunk_hash, skipped = _get_or_compute_hash(
            file_path=file_path,
            cache_key=cache_key,
            mtime=mtime,
            hash_alg=hash_alg,
            hash_cache=hash_cache,
            force_rehash=force_rehash,
            range_start=range_start,
            range_end=range_end,
        )
        chunk_hashes.append(chunk_hash)

        # Record progress for this chunk
        if progress_state is not None:
            progress_state.record_hash_complete(chunk_bytes, skipped=skipped)

        offset = range_end

    return chunk_hashes
