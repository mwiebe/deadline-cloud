# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Base download pipeline for download_manifest operation.

This module implements the base class for callback-based download pipelines.
Subclasses implement cache-specific download logic (S3 or filesystem).
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import secrets
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import DefaultDict, List, Optional, TYPE_CHECKING

from .._manifest import ManifestFilePath
from .._content_addressed_data_cache import ContentAddressedDataCache
from ...asset_manifests.hash_algorithms import HashAlgorithm
from ...caches.hash_cache import HashCache, HashCacheEntry, WHOLE_FILE_RANGE_END
from ...models import FileConflictResolution
from ...progress_tracker import ProgressTracker
from ..._utils import _get_long_path_compatible_path
from ._sparse_file import preallocate_file

if TYPE_CHECKING:
    pass

logger = logging.getLogger("deadline.job_attachments.download")


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
        progress_tracker: Optional[ProgressTracker],
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
        self._progress_tracker = progress_tracker

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
                if self._progress_tracker:
                    self._progress_tracker.track_progress_callback(file_size)
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

        logger.debug(f"Downloaded {entry.path} to {local_path}")
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
                if self._progress_tracker:
                    self._progress_tracker.track_progress_callback(file_size)
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
        logger.debug(f"Skipping download of {entry.path} - hash cache indicates file is up to date")
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
        logger.debug(
            f"Skipping download of chunked file {entry.path} - "
            f"hash cache indicates all {num_chunks} chunks are up to date"
        )
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
