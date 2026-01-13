# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
FileSystem-specific download pipeline for download_abs_manifest operation.

This module implements the filesystem cache download pipeline, which copies
files from a local filesystem cache to the target locations.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .._manifest import ManifestFilePath
from .._content_addressed_data_cache import FileSystemDataCache
from ._download_abs_manifest_pipeline import DownloadPipelineBase, DownloadFileResult

logger = logging.getLogger("deadline.job_attachments.download")


@dataclass
class _ChunkedFileState:
    """
    State tracker for a chunked file download from filesystem cache.

    Tracks how many chunks remain to be downloaded. When the last chunk completes,
    it triggers finalization (atomic move + mtime update + hash cache update).
    """

    entry: ManifestFilePath
    local_path: Path
    temp_path: Path
    file_size: int
    chunks_remaining: int
    total_bytes_downloaded: int = 0
    chunk_errors: List[Exception] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class FileSystemDownloadPipeline(DownloadPipelineBase):
    """
    Download pipeline for filesystem cache.

    Copies files from a local filesystem cache to target locations.
    For chunked files, copies each chunk in parallel.
    """

    def _download_file_content(
        self,
        entry: ManifestFilePath,
        temp_path: Path,
        file_size: int,
    ) -> Optional[int]:
        """Copy file from filesystem cache to temp_path."""
        if not isinstance(self._data_cache, FileSystemDataCache):
            raise TypeError("Expected FileSystemDataCache")

        source_path = Path(self._data_cache.get_object_key(entry.hash, self._hash_alg))  # type: ignore
        shutil.copy2(source_path, temp_path)
        copied_size = temp_path.stat().st_size

        if self._progress_state:
            self._progress_state.record_download_complete(copied_size, skipped=False)

        return copied_size

    def _setup_and_download_chunked_file_impl(
        self,
        entry: ManifestFilePath,
        local_path: Path,
        temp_path: Path,
        file_size: int,
    ) -> None:
        """
        Setup parallel chunk downloads for a chunked file from filesystem cache.

        Submits all chunks to the executor. The last chunk to complete triggers finalization.
        """
        state = _ChunkedFileState(
            entry=entry,
            local_path=local_path,
            temp_path=temp_path,
            file_size=file_size,
            chunks_remaining=len(entry.chunkhashes),  # type: ignore
        )

        num_chunks = len(entry.chunkhashes)  # type: ignore
        for chunk_idx, chunk_hash in enumerate(entry.chunkhashes):  # type: ignore
            offset = chunk_idx * self._chunk_size_bytes
            if chunk_idx == num_chunks - 1:
                actual_chunk_size = file_size - offset
            else:
                actual_chunk_size = self._chunk_size_bytes

            self._executor.submit(
                self._download_chunk_with_callback,
                state,
                chunk_idx,
                chunk_hash,
                offset,
                actual_chunk_size,
            )

    def _download_chunk_with_callback(
        self,
        state: _ChunkedFileState,
        chunk_idx: int,
        chunk_hash: str,
        offset: int,
        chunk_size: int,
    ) -> None:
        """
        Download one chunk from filesystem cache and check if file is complete.

        The last chunk to complete triggers finalization.
        """
        try:
            if self._cancelled:
                with state.lock:
                    state.chunks_remaining -= 1
                    if state.chunks_remaining == 0:
                        state.temp_path.unlink(missing_ok=True)
                        self._decrement_pending()
                return

            if not isinstance(self._data_cache, FileSystemDataCache):
                raise TypeError("Expected FileSystemDataCache")

            # Get source path from cache
            source_path = Path(self._data_cache.get_object_key(chunk_hash, self._hash_alg))

            # Read chunk and write to temp file at correct offset
            with open(source_path, "rb") as chunk_file:
                chunk_data = chunk_file.read()

            with open(state.temp_path, "r+b") as f:
                f.seek(offset)
                f.write(chunk_data)

            bytes_written = len(chunk_data)

            if self._progress_state:
                self._progress_state.record_download_complete(bytes_written, skipped=False)

            # Update state and check if all chunks are done
            with state.lock:
                state.total_bytes_downloaded += bytes_written
                state.chunks_remaining -= 1
                all_done = state.chunks_remaining == 0
                has_errors = len(state.chunk_errors) > 0

            if all_done:
                if has_errors:
                    state.temp_path.unlink(missing_ok=True)
                    self._record_error(state.chunk_errors[0])
                else:
                    self._finalize_chunked_file(state)
                self._decrement_pending()

        except Exception as e:
            with state.lock:
                state.chunk_errors.append(e)
                state.chunks_remaining -= 1
                all_done = state.chunks_remaining == 0

            if all_done:
                state.temp_path.unlink(missing_ok=True)
                self._record_error(e)
                self._decrement_pending()

    def _finalize_chunked_file(self, state: _ChunkedFileState) -> None:
        """Finalize a chunked file download (atomic move + mtime + hash cache)."""
        try:
            entry = state.entry
            local_path = state.local_path
            temp_path = state.temp_path

            os.replace(temp_path, local_path)

            if entry.mtime is not None:
                mtime_ns = entry.mtime * 1_000
                os.utime(local_path, ns=(mtime_ns, mtime_ns))

            actual_mtime_ns = local_path.stat().st_mtime_ns
            actual_mtime_us = actual_mtime_ns // 1_000

            self._update_hash_cache_for_chunked_file(entry, local_path, actual_mtime_ns)

            logger.debug(
                f"Downloaded chunked file {entry.path} ({len(entry.chunkhashes)} chunks)"  # type: ignore
            )
            self._record_result(
                DownloadFileResult(
                    entry=entry,
                    bytes_downloaded=state.total_bytes_downloaded,
                    local_path=local_path,
                    was_skipped=False,
                    actual_mtime_us=actual_mtime_us,
                )
            )

        except Exception as e:
            state.temp_path.unlink(missing_ok=True)
            self._record_error(e)
