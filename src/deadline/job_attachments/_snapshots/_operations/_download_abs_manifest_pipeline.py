# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Base download pipeline for download_abs_manifest operation.

This module implements the base class for callback-based download pipelines.
Subclasses implement cache-specific download logic (S3 or filesystem).
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import secrets
import time
import threading
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable, DefaultDict, Deque, List, Optional

from .._manifest import ManifestFilePath
from .._content_addressed_data_cache import ContentAddressedDataCache
from ...asset_manifests.hash_algorithms import HashAlgorithm
from ...caches.hash_cache import HashCache, HashCacheEntry, WHOLE_FILE_RANGE_END
from ...models import FileConflictResolution
from ..._utils import _get_long_path_compatible_path
from ..._path_summarization import human_readable_file_size
from ._sparse_file import preallocate_file

logger = logging.getLogger("deadline.job_attachments.download")

# Default interval for progress callbacks (5 times per second)
DEFAULT_PROGRESS_CALLBACK_INTERVAL = 0.2  # seconds

# Time window for calculating transfer rate (in seconds)
TRANSFER_RATE_WINDOW_SECONDS = 12.0


@dataclass
class DownloadProgressMetadata:
    """
    Progress metadata for download_abs_manifest operation.

    Reports progress for the download phase. For chunked files, each chunk
    is counted separately in the file/chunk counts.
    """

    # Totals
    total_file_chunks: int  # Total files + chunks to process
    total_bytes: int

    # Download progress
    downloaded_file_chunks: int
    downloaded_bytes: int
    skipped_file_chunks: int  # Skipped due to hash cache hit or conflict resolution
    skipped_bytes: int

    # Overall progress
    progress: float  # 0-100
    progressMessage: str

    # Timing information
    total_time: float = 0.0  # Elapsed time since operation start (seconds)
    transfer_rate: float = 0.0  # Current transfer rate (bytes/second)


# Callback type for download progress reporting
# Return True to continue, False to cancel the operation
DownloadProgressCallback = Callable[[DownloadProgressMetadata], bool]


@dataclass
class _ProgressHistoryEntry:
    """A single entry in the progress history for transfer rate calculation."""

    timestamp: float  # Time since operation start (seconds)
    downloaded_bytes: int  # Total downloaded bytes at this point


@dataclass
class _DownloadProgressState:
    """
    Thread-safe progress state for download pipeline.

    Tracks bytes and file/chunk counts. For chunked files, each chunk is counted separately.
    """

    # Totals (set once at initialization)
    total_file_chunks: int = 0
    total_bytes: int = 0

    # Progress counters
    downloaded_file_chunks: int = 0
    downloaded_bytes: int = 0
    skipped_file_chunks: int = 0
    skipped_bytes: int = 0

    # Separate progress tracking for smoother transfer rate calculation.
    # This is updated on every part download (not just file/chunk completion),
    # providing more granular updates for transfer rate calculation.
    _downloaded_bytes_for_progress: int = 0

    # Callback and timing
    on_progress: Optional[DownloadProgressCallback] = None
    callback_interval: float = DEFAULT_PROGRESS_CALLBACK_INTERVAL
    _last_callback_time: float = field(default_factory=time.perf_counter)
    _cancelled: bool = False

    # Start time for total_time calculation
    _start_time: float = field(default_factory=time.perf_counter)

    # Progress history for sliding window transfer rate calculation
    # Each entry records (timestamp, downloaded_bytes) at a progress update
    _progress_history: Deque[_ProgressHistoryEntry] = field(default_factory=deque)

    # Thread safety
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_bytes_downloaded(self, num_bytes: int) -> None:
        """Record bytes downloaded (for progress bar smoothness). Does not increment file_chunks."""
        with self._lock:
            self.downloaded_bytes += num_bytes
            self._downloaded_bytes_for_progress += num_bytes
            self._maybe_invoke_callback()

    def record_file_chunk_complete(self, chunk_bytes: int, skipped: bool) -> None:
        """Record completion of downloading for a file or chunk (increments file_chunks counter)."""
        with self._lock:
            if skipped:
                self.skipped_bytes += chunk_bytes
                self.skipped_file_chunks += 1
            else:
                # Note: bytes may already be counted via record_bytes_downloaded for multipart
                # Only increment file_chunks here
                self.downloaded_file_chunks += 1
            self._maybe_invoke_callback()

    def record_download_complete(self, chunk_bytes: int, skipped: bool) -> None:
        """Record completion of downloading for a file or chunk (legacy method for non-multipart)."""
        with self._lock:
            if skipped:
                self.skipped_bytes += chunk_bytes
                self.skipped_file_chunks += 1
            else:
                self.downloaded_bytes += chunk_bytes
                self._downloaded_bytes_for_progress += chunk_bytes
                self.downloaded_file_chunks += 1
            self._maybe_invoke_callback()

    def record_part_downloaded(self, part_bytes: int) -> None:
        """Record bytes downloaded for a part (for smoother progress tracking).

        This is called after each part download completes, providing more granular
        progress updates than record_download_complete which is called per file/chunk.
        """
        with self._lock:
            self._downloaded_bytes_for_progress += part_bytes
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

    def _build_metadata(self) -> DownloadProgressMetadata:
        """Build progress metadata. Must be called with lock held."""
        completed_bytes = self.downloaded_bytes + self.skipped_bytes
        progress = (completed_bytes / self.total_bytes * 100) if self.total_bytes > 0 else 0.0

        # Calculate total_time since operation started
        now = time.perf_counter()
        total_time = now - self._start_time

        # Use _downloaded_bytes_for_progress for smoother transfer rate calculation
        # This is updated more frequently (per-part) than completed_bytes (per-file/chunk)
        progress_bytes = self._downloaded_bytes_for_progress
        transfer_rate = self._calculate_transfer_rate(total_time, progress_bytes)

        # Format elapsed time
        if total_time < 60:
            time_str = f"{total_time:.1f}s"
        else:
            minutes = int(total_time // 60)
            seconds = total_time % 60
            time_str = f"{minutes}:{seconds:04.1f}"

        # Build progress message with rate
        rate_str = f"{human_readable_file_size(int(transfer_rate))}/s" if transfer_rate > 0 else ""

        msg_parts = [
            f"Downloaded {human_readable_file_size(completed_bytes)}",
            f"/ {human_readable_file_size(self.total_bytes)}",
            f"[{time_str}]",
        ]
        if rate_str:
            msg_parts.append(f"({rate_str})")

        msg = " ".join(msg_parts)

        return DownloadProgressMetadata(
            total_file_chunks=self.total_file_chunks,
            total_bytes=self.total_bytes,
            downloaded_file_chunks=self.downloaded_file_chunks,
            downloaded_bytes=self.downloaded_bytes,
            skipped_file_chunks=self.skipped_file_chunks,
            skipped_bytes=self.skipped_bytes,
            progress=progress,
            progressMessage=msg,
            total_time=total_time,
            transfer_rate=transfer_rate,
        )

    def _calculate_transfer_rate(self, current_time: float, current_bytes: int) -> float:
        """
        Calculate transfer rate using a sliding window approach.

        Uses a deque of progress history entries. The window is TRANSFER_RATE_WINDOW_SECONDS
        (12s). At the beginning when less than 12s has elapsed, the window extends from
        start to current time. Once 12s has passed, we use a sliding window by removing
        entries from the front when the second entry is older than 12s ago.

        Must be called with lock held.
        """
        # Add current progress to history
        self._progress_history.append(
            _ProgressHistoryEntry(timestamp=current_time, downloaded_bytes=current_bytes)
        )

        # Remove old entries from the front of the deque
        # Keep removing while we have at least 2 entries and the second entry
        # is older than TRANSFER_RATE_WINDOW_SECONDS ago
        while (
            len(self._progress_history) > 1
            and current_time - self._progress_history[1].timestamp > TRANSFER_RATE_WINDOW_SECONDS
        ):
            self._progress_history.popleft()

        # Calculate rate based on oldest entry in the window vs current
        if len(self._progress_history) < 2:
            # Not enough data points yet
            return 0.0

        oldest = self._progress_history[0]
        time_delta = current_time - oldest.timestamp
        bytes_delta = current_bytes - oldest.downloaded_bytes

        if time_delta > 0:
            return bytes_delta / time_delta
        return 0.0

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
class DownloadFileResult:
    """Result of downloading a single file (regular or chunked)."""

    entry: ManifestFilePath
    bytes_downloaded: int
    local_path: Optional[Path]
    was_skipped: bool
    actual_mtime_us: Optional[int]


class DownloadPipelineBase(ABC):
    """
    Base class for callback-based download pipelines.

    This avoids asyncio overhead by using executor.submit() with callbacks
    instead of run_in_executor() with await.

    For single files: one executor submission handles setup + copy + finalize
    For chunked files: fan-out to parallel chunk downloads, fan-in via atomic counter

    Subclasses implement:
    - _download_file_content(): Download file data from the specific cache type
    - _setup_and_download_chunked_file_impl(): Cache-specific chunked file handling
    """

    def __init__(
        self,
        executor: concurrent.futures.ThreadPoolExecutor,
        data_cache: ContentAddressedDataCache,
        hash_alg: str,
        hash_alg_enum: HashAlgorithm,
        chunk_size_bytes: int,
        hash_cache: Optional[HashCache],
        collision_lock: Lock,
        collision_file_dict: DefaultDict[str, int],
        file_conflict_resolution: FileConflictResolution,
        progress_state: Optional[_DownloadProgressState],
    ) -> None:
        self._executor = executor
        self._data_cache = data_cache
        self._hash_alg = hash_alg
        self._hash_alg_enum = hash_alg_enum
        self._chunk_size_bytes = chunk_size_bytes
        self._hash_cache = hash_cache
        self._collision_lock = collision_lock
        self._collision_file_dict = collision_file_dict
        self._file_conflict_resolution = file_conflict_resolution
        self._progress_state = progress_state

        # Track completion
        self._pending_count = 0
        self._submitted_count = 0
        self._lock = threading.Lock()
        self._done_event = threading.Event()

        # Collect results
        self._results: List[DownloadFileResult] = []
        self._results_lock = threading.Lock()

        # Track errors
        self._error: Optional[Exception] = None
        self._error_lock = threading.Lock()

        # Cancellation flag
        self._cancelled = False

    # =========================================================================
    # Public API
    # =========================================================================

    def submit_single_file(self, entry: ManifestFilePath) -> None:
        """Submit a single (non-chunked) file for download."""
        with self._lock:
            self._pending_count += 1
            self._submitted_count += 1
        self._executor.submit(self._download_single_file_sync, entry)

    def submit_chunked_file(self, entry: ManifestFilePath) -> None:
        """Submit a chunked file for download."""
        with self._lock:
            self._pending_count += 1
            self._submitted_count += 1
        self._executor.submit(self._setup_and_download_chunked_file, entry)

    def wait_for_completion(self) -> List[DownloadFileResult]:
        """Wait for all submitted files to complete and return results."""
        with self._lock:
            if self._submitted_count == 0:
                return []

        self._done_event.wait()

        with self._error_lock:
            if self._error is not None:
                raise self._error

        with self._results_lock:
            return list(self._results)

    def cancel(self) -> None:
        """Signal cancellation to stop processing new work."""
        self._cancelled = True

    # =========================================================================
    # Internal: Completion tracking
    # =========================================================================

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
                logger.exception(f"Download pipeline error: {error}")
        self._done_event.set()

    def _record_result(self, result: DownloadFileResult) -> None:
        """Record a completed download result."""
        with self._results_lock:
            self._results.append(result)

    # =========================================================================
    # Internal: Single file download
    # =========================================================================

    def _download_single_file_sync(self, entry: ManifestFilePath) -> None:
        """
        Download a single file synchronously (setup + copy + finalize in one call).

        Subclasses may override to add cache-specific behavior (e.g., S3 multipart).
        """
        try:
            if self._cancelled:
                self._decrement_pending()
                return

            if entry.hash is None:
                raise ValueError(f"File entry '{entry.path}' has no hash")

            local_path = _get_long_path_compatible_path(Path(entry.path))
            file_size = entry.size or 0

            # Check hash cache first
            can_skip, cached_mtime_us = self._check_hash_cache_for_skip(entry)
            if can_skip:
                if self._progress_state:
                    self._progress_state.record_download_complete(file_size, skipped=True)
                self._record_result(
                    DownloadFileResult(
                        entry=entry,
                        bytes_downloaded=file_size,
                        local_path=local_path,
                        was_skipped=True,
                        actual_mtime_us=cached_mtime_us,
                    )
                )
                self._decrement_pending()
                return

            # Handle file conflicts
            resolved_path = self._handle_file_conflict(entry, local_path, file_size)
            if resolved_path is None:
                self._decrement_pending()
                return

            # Create temp file
            temp_suffix = secrets.token_hex(5)
            temp_path = resolved_path.parent / f"{resolved_path.name}.tmp{temp_suffix}"

            # Download file content (subclass-specific)
            try:
                bytes_downloaded = self._download_file_content(entry, temp_path, file_size)
                if bytes_downloaded is None:
                    # Subclass is handling async (e.g., S3 multipart)
                    return

                # Finalize
                self._finalize_single_file(entry, resolved_path, temp_path, bytes_downloaded)
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise

            self._decrement_pending()

        except Exception as e:
            self._record_error(e)
            self._decrement_pending()

    @abstractmethod
    def _download_file_content(
        self,
        entry: ManifestFilePath,
        temp_path: Path,
        file_size: int,
    ) -> Optional[int]:
        """
        Download file content from the data cache to temp_path.

        Returns:
            Number of bytes downloaded, or None if the subclass is handling
            completion asynchronously (e.g., S3 multipart downloads).
        """
        pass

    def _finalize_single_file(
        self,
        entry: ManifestFilePath,
        local_path: Path,
        temp_path: Path,
        bytes_downloaded: int,
    ) -> None:
        """Finalize a single file download (atomic move + mtime + hash cache)."""
        os.replace(temp_path, local_path)

        if entry.mtime is not None:
            mtime_ns = entry.mtime * 1_000
            os.utime(local_path, ns=(mtime_ns, mtime_ns))

        actual_mtime_ns = local_path.stat().st_mtime_ns
        actual_mtime_us = actual_mtime_ns // 1_000

        if self._hash_cache is not None and entry.hash is not None:
            resolved_path = str(local_path.resolve())
            self._hash_cache.put_entry(
                HashCacheEntry(
                    file_path=resolved_path,
                    hash_algorithm=self._hash_alg_enum,
                    file_hash=entry.hash,
                    last_modified_time=str(actual_mtime_ns),
                    range_start=0,
                    range_end=WHOLE_FILE_RANGE_END,
                )
            )

        self._record_result(
            DownloadFileResult(
                entry=entry,
                bytes_downloaded=bytes_downloaded,
                local_path=local_path,
                was_skipped=False,
                actual_mtime_us=actual_mtime_us,
            )
        )

    # =========================================================================
    # Internal: Chunked file download
    # =========================================================================

    def _setup_and_download_chunked_file(self, entry: ManifestFilePath) -> None:
        """Setup a chunked file download and submit all downloads."""
        try:
            if self._cancelled:
                self._decrement_pending()
                return

            if entry.chunkhashes is None:
                raise ValueError(f"Chunked file entry '{entry.path}' has no chunkhashes")

            local_path = _get_long_path_compatible_path(Path(entry.path))
            file_size = entry.size or 0

            # Check hash cache first
            can_skip, cached_mtime_us = self._check_hash_cache_for_chunked_skip(entry)
            if can_skip:
                # Record progress for each chunk that was skipped
                if self._progress_state:
                    num_chunks = len(entry.chunkhashes)
                    for chunk_idx in range(num_chunks):
                        chunk_start = chunk_idx * self._chunk_size_bytes
                        if chunk_idx == num_chunks - 1:
                            chunk_bytes = file_size - chunk_start
                        else:
                            chunk_bytes = self._chunk_size_bytes
                        self._progress_state.record_download_complete(chunk_bytes, skipped=True)
                self._record_result(
                    DownloadFileResult(
                        entry=entry,
                        bytes_downloaded=file_size,
                        local_path=local_path,
                        was_skipped=True,
                        actual_mtime_us=cached_mtime_us,
                    )
                )
                self._decrement_pending()
                return

            # Handle file conflicts
            resolved_path = self._handle_file_conflict(entry, local_path, file_size)
            if resolved_path is None:
                self._decrement_pending()
                return

            # Create and pre-allocate temp file
            temp_suffix = secrets.token_hex(5)
            temp_path = resolved_path.parent / f"{resolved_path.name}.tmp{temp_suffix}"

            with open(temp_path, "wb") as f:
                preallocate_file(f, file_size)

            # Delegate to subclass for cache-specific chunked download
            self._setup_and_download_chunked_file_impl(entry, resolved_path, temp_path, file_size)

        except Exception as e:
            self._record_error(e)
            self._decrement_pending()

    @abstractmethod
    def _setup_and_download_chunked_file_impl(
        self,
        entry: ManifestFilePath,
        local_path: Path,
        temp_path: Path,
        file_size: int,
    ) -> None:
        """
        Cache-specific implementation for chunked file downloads.

        Must handle submitting chunk downloads and finalization.
        """
        pass

    def _update_hash_cache_for_chunked_file(
        self,
        entry: ManifestFilePath,
        local_path: Path,
        mtime_ns: int,
    ) -> None:
        """Update the hash cache with all chunk hashes for a downloaded chunked file."""
        if entry.chunkhashes is None or self._hash_cache is None:
            return

        resolved_path = str(local_path.resolve())
        mtime_str = str(mtime_ns)
        file_size = entry.size or 0
        num_chunks = len(entry.chunkhashes)

        for chunk_idx, chunk_hash in enumerate(entry.chunkhashes):
            range_start = chunk_idx * self._chunk_size_bytes
            if chunk_idx == num_chunks - 1:
                range_end = file_size
            else:
                range_end = range_start + self._chunk_size_bytes

            self._hash_cache.put_entry(
                HashCacheEntry(
                    file_path=resolved_path,
                    hash_algorithm=self._hash_alg_enum,
                    file_hash=chunk_hash,
                    last_modified_time=mtime_str,
                    range_start=range_start,
                    range_end=range_end,
                )
            )

    # =========================================================================
    # Internal: Hash cache checks
    # =========================================================================

    def _check_hash_cache_for_skip(self, entry: ManifestFilePath) -> tuple[bool, Optional[int]]:
        """Check if a file can be skipped because it already exists with correct hash."""
        if self._hash_cache is None or entry.hash is None:
            return (False, None)

        local_path = _get_long_path_compatible_path(Path(entry.path))

        if not local_path.exists() or not local_path.is_file():
            return (False, None)

        try:
            stat_result = local_path.stat()
            current_mtime_ns = stat_result.st_mtime_ns
            current_mtime_str = str(current_mtime_ns)
        except OSError:
            return (False, None)

        resolved_path = str(Path(entry.path).resolve())
        cache_entry = self._hash_cache.get_entry(
            file_path_key=resolved_path,
            hash_algorithm=self._hash_alg_enum,
            range_start=0,
            range_end=WHOLE_FILE_RANGE_END,
        )

        if cache_entry is None:
            return (False, None)

        if cache_entry.last_modified_time != current_mtime_str:
            return (False, None)

        if cache_entry.file_hash != entry.hash:
            return (False, None)

        actual_mtime_us = current_mtime_ns // 1_000
        return (True, actual_mtime_us)

    def _check_hash_cache_for_chunked_skip(
        self, entry: ManifestFilePath
    ) -> tuple[bool, Optional[int]]:
        """Check if a chunked file can be skipped."""
        if self._hash_cache is None or entry.chunkhashes is None:
            return (False, None)

        local_path = _get_long_path_compatible_path(Path(entry.path))

        if not local_path.exists() or not local_path.is_file():
            return (False, None)

        try:
            stat_result = local_path.stat()
            current_mtime_ns = stat_result.st_mtime_ns
            current_mtime_str = str(current_mtime_ns)
        except OSError:
            return (False, None)

        resolved_path = str(Path(entry.path).resolve())
        file_size = entry.size or 0
        num_chunks = len(entry.chunkhashes)

        for chunk_idx, expected_hash in enumerate(entry.chunkhashes):
            range_start = chunk_idx * self._chunk_size_bytes
            if chunk_idx == num_chunks - 1:
                range_end = file_size
            else:
                range_end = range_start + self._chunk_size_bytes

            cache_entry = self._hash_cache.get_entry(
                file_path_key=resolved_path,
                hash_algorithm=self._hash_alg_enum,
                range_start=range_start,
                range_end=range_end,
            )

            if cache_entry is None:
                return (False, None)

            if cache_entry.last_modified_time != current_mtime_str:
                return (False, None)

            if cache_entry.file_hash != expected_hash:
                return (False, None)

        actual_mtime_us = current_mtime_ns // 1_000
        return (True, actual_mtime_us)

    # =========================================================================
    # Internal: File conflict handling
    # =========================================================================

    def _handle_file_conflict(
        self,
        entry: ManifestFilePath,
        local_path: Path,
        file_size: int,
    ) -> Optional[Path]:
        """
        Handle file conflicts based on resolution policy.

        Returns:
            The path to use for the file, or None if the file should be skipped.
        """
        if not local_path.exists():
            return local_path

        if self._file_conflict_resolution == FileConflictResolution.SKIP:
            # Record progress for skipped file
            if self._progress_state:
                self._progress_state.record_download_complete(file_size, skipped=True)
            self._record_result(
                DownloadFileResult(
                    entry=entry,
                    bytes_downloaded=file_size,
                    local_path=None,
                    was_skipped=True,
                    actual_mtime_us=None,
                )
            )
            return None
        elif self._file_conflict_resolution == FileConflictResolution.OVERWRITE:
            return local_path
        elif self._file_conflict_resolution == FileConflictResolution.CREATE_COPY:
            new_path = self._get_new_copy_file_path(local_path)
            return _get_long_path_compatible_path(new_path)
        else:
            raise ValueError(f"Unknown file conflict resolution: {self._file_conflict_resolution}")

    def _get_new_copy_file_path(self, local_file_path: Path) -> Path:
        """Generate a unique file path when a file already exists."""
        with self._collision_lock:
            file_str = str(local_file_path)
            num = self._collision_file_dict[file_str]
            new_file_path = local_file_path

            while True:
                try:
                    with open(new_file_path, "x"):
                        break
                except FileExistsError:
                    num += 1
                    new_file_path = local_file_path.parent / (
                        f"{local_file_path.stem} ({num}){local_file_path.suffix}"
                    )

            self._collision_file_dict[file_str] = num
            return new_file_path
