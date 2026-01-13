# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
S3-specific download pipeline for download_abs_manifest operation.

This module implements the S3 cache download pipeline, which downloads
files from S3 using parallel byte-range requests for large files.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from botocore.exceptions import BotoCoreError, ClientError

from .._manifest import ManifestFilePath
from .._content_addressed_data_cache import S3DataCache
from ._download_abs_manifest_pipeline import DownloadPipelineBase, DownloadFileResult
from ._sparse_file import preallocate_file
from ...exceptions import (
    JobAttachmentsS3ClientError,
    JobAttachmentS3BotoCoreError,
)

logger = logging.getLogger("deadline.job_attachments.download")


@dataclass
class S3ParallelDownloadState:
    """
    State tracker for parallel byte-range downloads from S3.

    Used for both single large files and chunked files when downloading from S3.
    Tracks how many byte-range parts remain to be downloaded across all chunks
    (or the single file). When the last part completes, it triggers finalization.

    For single files: parts_remaining = number of byte-range parts
    For chunked files: parts_remaining = sum of parts across all chunks
    """

    entry: ManifestFilePath
    local_path: Path
    temp_path: Path
    file_size: int
    parts_remaining: int
    total_bytes_downloaded: int = 0
    part_errors: List[Exception] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class S3DownloadPipeline(DownloadPipelineBase):
    """
    Download pipeline for S3 cache.

    Downloads files from S3, using parallel byte-range requests for large files.
    For chunked files, downloads all parts of all chunks in parallel.
    """

    def _download_file_content(
        self,
        entry: ManifestFilePath,
        temp_path: Path,
        file_size: int,
    ) -> Optional[int]:
        """
        Download file from S3 to temp_path.

        For large files, initiates parallel multipart download and returns None
        (completion is handled asynchronously). For small files, downloads
        synchronously and returns bytes downloaded.
        """
        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError("Expected S3DataCache")

        # For large files, use parallel multipart download
        if file_size >= 2 * self._data_cache.multipart_part_size:
            local_path = temp_path.parent / temp_path.name.replace(".tmp", "")
            # Extract actual local_path from the entry
            from ..._utils import _get_long_path_compatible_path

            local_path = _get_long_path_compatible_path(Path(entry.path))
            self._setup_and_download_large_single_file(entry, local_path, temp_path, file_size)
            return None  # Async completion

        # Small file - download in single request
        s3_key = self._data_cache.get_object_key(entry.hash, self._hash_alg)  # type: ignore

        try:
            get_kwargs = {
                "Bucket": self._data_cache.s3_bucket,
                "Key": s3_key,
            }
            if self._data_cache.expected_bucket_owner is not None:
                get_kwargs["ExpectedBucketOwner"] = self._data_cache.expected_bucket_owner
            response = self._data_cache.s3_client.get_object(**get_kwargs)
            data = response["Body"].read()
            with open(temp_path, "wb") as f:
                f.write(data)
            if self._progress_tracker:
                self._progress_tracker.track_progress_callback(len(data))
            return len(data)

        except ClientError as exc:
            status_code = int(exc.response["ResponseMetadata"]["HTTPStatusCode"])
            raise JobAttachmentsS3ClientError(
                action="downloading file",
                status_code=status_code,
                bucket_name=self._data_cache.s3_bucket,
                key_or_prefix=s3_key,
                message=str(exc),
            ) from exc
        except BotoCoreError as bce:
            raise JobAttachmentS3BotoCoreError(
                action="downloading file",
                error_details=str(bce),
            ) from bce

    def _setup_and_download_large_single_file(
        self,
        entry: ManifestFilePath,
        local_path: Path,
        temp_path: Path,
        file_size: int,
    ) -> None:
        """
        Setup parallel multipart download for a large single file.

        Pre-allocates the temp file and submits all parts to the executor.
        The last part to complete triggers finalization.
        """
        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError("Expected S3DataCache")

        # Pre-allocate the temp file
        with open(temp_path, "wb") as f:
            preallocate_file(f, file_size)

        part_size = self._data_cache.multipart_part_size
        num_parts = (file_size + part_size - 1) // part_size

        state = S3ParallelDownloadState(
            entry=entry,
            local_path=local_path,
            temp_path=temp_path,
            file_size=file_size,
            parts_remaining=num_parts,
        )

        s3_key = self._data_cache.get_object_key(entry.hash, self._hash_alg)  # type: ignore

        for part_idx in range(num_parts):
            offset = part_idx * part_size
            end = min(offset + part_size - 1, file_size - 1)

            self._executor.submit(
                self._download_part_with_callback,
                state,
                s3_key,
                offset,
                end,
                is_chunked=False,
            )

    def _setup_and_download_chunked_file_impl(
        self,
        entry: ManifestFilePath,
        local_path: Path,
        temp_path: Path,
        file_size: int,
    ) -> None:
        """
        Setup parallel part downloads for a chunked file from S3.

        For small chunks (< MIN_SIZE_FOR_MULTIPART_DOWNLOAD), downloads without Range header.
        For large chunks, calculates all parts and submits them all to the executor.
        The last part to complete triggers finalization.
        """
        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError("Expected S3DataCache")

        part_size = self._data_cache.multipart_part_size
        num_chunks = len(entry.chunkhashes)  # type: ignore

        # Calculate total number of parts across all chunks
        total_parts = 0
        for chunk_idx in range(num_chunks):
            chunk_file_offset = chunk_idx * self._chunk_size_bytes
            if chunk_idx == num_chunks - 1:
                chunk_size = file_size - chunk_file_offset
            else:
                chunk_size = self._chunk_size_bytes

            if chunk_size < 2 * self._data_cache.multipart_part_size:
                total_parts += 1
            else:
                num_parts_in_chunk = (chunk_size + part_size - 1) // part_size
                total_parts += num_parts_in_chunk

        state = S3ParallelDownloadState(
            entry=entry,
            local_path=local_path,
            temp_path=temp_path,
            file_size=file_size,
            parts_remaining=total_parts,
        )

        # Submit all parts of all chunks to executor
        for chunk_idx, chunk_hash in enumerate(entry.chunkhashes):  # type: ignore
            chunk_file_offset = chunk_idx * self._chunk_size_bytes
            if chunk_idx == num_chunks - 1:
                chunk_size = file_size - chunk_file_offset
            else:
                chunk_size = self._chunk_size_bytes

            s3_key = self._data_cache.get_object_key(chunk_hash, self._hash_alg)

            if chunk_size < 2 * self._data_cache.multipart_part_size:
                self._executor.submit(
                    self._download_small_chunk_with_callback,
                    state,
                    s3_key,
                    chunk_file_offset,
                )
            else:
                num_parts_in_chunk = (chunk_size + part_size - 1) // part_size
                for part_idx in range(num_parts_in_chunk):
                    chunk_offset = part_idx * part_size
                    end_in_chunk = min(chunk_offset + part_size - 1, chunk_size - 1)
                    file_offset = chunk_file_offset + chunk_offset

                    self._executor.submit(
                        self._download_chunk_part_with_callback,
                        state,
                        s3_key,
                        chunk_offset,
                        end_in_chunk,
                        file_offset,
                    )

    def _download_part_with_callback(
        self,
        state: S3ParallelDownloadState,
        s3_key: str,
        offset: int,
        end: int,
        is_chunked: bool,
    ) -> None:
        """
        Download one part of a large file and check if all parts are complete.

        The last part to complete triggers finalization.
        """
        try:
            if self._cancelled:
                with state.lock:
                    state.parts_remaining -= 1
                    if state.parts_remaining == 0:
                        state.temp_path.unlink(missing_ok=True)
                        self._decrement_pending()
                return

            if not isinstance(self._data_cache, S3DataCache):
                raise TypeError("Expected S3DataCache")

            range_header = f"bytes={offset}-{end}"

            get_kwargs = {
                "Bucket": self._data_cache.s3_bucket,
                "Key": s3_key,
                "Range": range_header,
            }
            if self._data_cache.expected_bucket_owner is not None:
                get_kwargs["ExpectedBucketOwner"] = self._data_cache.expected_bucket_owner
            response = self._data_cache.s3_client.get_object(**get_kwargs)
            data = response["Body"].read()

            with open(state.temp_path, "r+b") as f:
                f.seek(offset)
                f.write(data)

            if self._progress_tracker:
                self._progress_tracker.track_progress_callback(len(data))

            with state.lock:
                state.total_bytes_downloaded += len(data)
                state.parts_remaining -= 1
                all_done = state.parts_remaining == 0
                has_errors = len(state.part_errors) > 0

            if all_done:
                if has_errors:
                    state.temp_path.unlink(missing_ok=True)
                    self._record_error(state.part_errors[0])
                else:
                    self._finalize_s3_parallel_download(state, is_chunked)
                self._decrement_pending()

        except (ClientError, BotoCoreError) as e:
            with state.lock:
                state.part_errors.append(e)
                state.parts_remaining -= 1
                all_done = state.parts_remaining == 0

            if all_done:
                state.temp_path.unlink(missing_ok=True)
                self._record_error(e)
                self._decrement_pending()

        except Exception as e:
            with state.lock:
                state.part_errors.append(e)
                state.parts_remaining -= 1
                all_done = state.parts_remaining == 0

            if all_done:
                state.temp_path.unlink(missing_ok=True)
                self._record_error(e)
                self._decrement_pending()

    def _download_small_chunk_with_callback(
        self,
        state: S3ParallelDownloadState,
        s3_key: str,
        file_offset: int,
    ) -> None:
        """
        Download a small chunk without Range header and check if all parts are complete.

        The last part to complete triggers finalization.
        """
        try:
            if self._cancelled:
                with state.lock:
                    state.parts_remaining -= 1
                    if state.parts_remaining == 0:
                        state.temp_path.unlink(missing_ok=True)
                        self._decrement_pending()
                return

            if not isinstance(self._data_cache, S3DataCache):
                raise TypeError("Expected S3DataCache")

            get_kwargs = {
                "Bucket": self._data_cache.s3_bucket,
                "Key": s3_key,
            }
            if self._data_cache.expected_bucket_owner is not None:
                get_kwargs["ExpectedBucketOwner"] = self._data_cache.expected_bucket_owner
            response = self._data_cache.s3_client.get_object(**get_kwargs)
            data = response["Body"].read()

            with open(state.temp_path, "r+b") as f:
                f.seek(file_offset)
                f.write(data)

            if self._progress_tracker:
                self._progress_tracker.track_progress_callback(len(data))

            with state.lock:
                state.total_bytes_downloaded += len(data)
                state.parts_remaining -= 1
                all_done = state.parts_remaining == 0
                has_errors = len(state.part_errors) > 0

            if all_done:
                if has_errors:
                    state.temp_path.unlink(missing_ok=True)
                    self._record_error(state.part_errors[0])
                else:
                    self._finalize_s3_parallel_download(state, is_chunked=True)
                self._decrement_pending()

        except (ClientError, BotoCoreError) as e:
            with state.lock:
                state.part_errors.append(e)
                state.parts_remaining -= 1
                all_done = state.parts_remaining == 0

            if all_done:
                state.temp_path.unlink(missing_ok=True)
                self._record_error(e)
                self._decrement_pending()

        except Exception as e:
            with state.lock:
                state.part_errors.append(e)
                state.parts_remaining -= 1
                all_done = state.parts_remaining == 0

            if all_done:
                state.temp_path.unlink(missing_ok=True)
                self._record_error(e)
                self._decrement_pending()

    def _download_chunk_part_with_callback(
        self,
        state: S3ParallelDownloadState,
        s3_key: str,
        chunk_offset: int,
        end_in_chunk: int,
        file_offset: int,
    ) -> None:
        """
        Download one part of a chunk and check if all parts are complete.

        The last part to complete triggers finalization.
        """
        try:
            if self._cancelled:
                with state.lock:
                    state.parts_remaining -= 1
                    if state.parts_remaining == 0:
                        state.temp_path.unlink(missing_ok=True)
                        self._decrement_pending()
                return

            if not isinstance(self._data_cache, S3DataCache):
                raise TypeError("Expected S3DataCache")

            range_header = f"bytes={chunk_offset}-{end_in_chunk}"

            get_kwargs = {
                "Bucket": self._data_cache.s3_bucket,
                "Key": s3_key,
                "Range": range_header,
            }
            if self._data_cache.expected_bucket_owner is not None:
                get_kwargs["ExpectedBucketOwner"] = self._data_cache.expected_bucket_owner
            response = self._data_cache.s3_client.get_object(**get_kwargs)
            data = response["Body"].read()

            with open(state.temp_path, "r+b") as f:
                f.seek(file_offset)
                f.write(data)

            if self._progress_tracker:
                self._progress_tracker.track_progress_callback(len(data))

            with state.lock:
                state.total_bytes_downloaded += len(data)
                state.parts_remaining -= 1
                all_done = state.parts_remaining == 0
                has_errors = len(state.part_errors) > 0

            if all_done:
                if has_errors:
                    state.temp_path.unlink(missing_ok=True)
                    self._record_error(state.part_errors[0])
                else:
                    self._finalize_s3_parallel_download(state, is_chunked=True)
                self._decrement_pending()

        except (ClientError, BotoCoreError) as e:
            with state.lock:
                state.part_errors.append(e)
                state.parts_remaining -= 1
                all_done = state.parts_remaining == 0

            if all_done:
                state.temp_path.unlink(missing_ok=True)
                self._record_error(e)
                self._decrement_pending()

        except Exception as e:
            with state.lock:
                state.part_errors.append(e)
                state.parts_remaining -= 1
                all_done = state.parts_remaining == 0

            if all_done:
                state.temp_path.unlink(missing_ok=True)
                self._record_error(e)
                self._decrement_pending()

    def _finalize_s3_parallel_download(
        self, state: S3ParallelDownloadState, is_chunked: bool
    ) -> None:
        """Finalize a large file download (atomic move + mtime + hash cache)."""
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

            if is_chunked:
                self._update_hash_cache_for_chunked_file(entry, local_path, actual_mtime_ns)
            elif self._hash_cache is not None and entry.hash is not None:
                from ...caches.hash_cache import HashCacheEntry, WHOLE_FILE_RANGE_END

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

            file_type = "chunked file" if is_chunked else "file"
            logger.debug(f"Downloaded {file_type} {entry.path} to {local_path}")
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
