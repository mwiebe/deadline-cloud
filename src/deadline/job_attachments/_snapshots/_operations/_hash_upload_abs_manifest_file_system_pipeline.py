# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
FileSystem-specific pipeline for hash_upload_abs_manifest operation.
"""

from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path
from typing import Dict, Optional

from .._content_addressed_data_cache import FileSystemDataCache
from ...asset_manifests.hash_algorithms import HashAlgorithm, hash_data
from ._hash_upload_abs_manifest_pipeline import (
    HashUploadPipelineBase,
    _ChunkWorkItem,
    _StreamingWorkItem,
    _MultipartPartWorkItem,
    DEFAULT_STREAM_BUFFER_SIZE,
)


class FileSystemHashUploadPipeline(HashUploadPipelineBase):
    """FileSystem-specific implementation of the hash+upload pipeline."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Per-hash locks for filesystem writes to prevent race conditions
        self._fs_write_locks: Dict[str, threading.Lock] = {}
        self._fs_write_locks_lock = threading.Lock()

    def _get_fs_write_lock(self, hash_key: str) -> threading.Lock:
        """Get or create a lock for a specific hash to prevent concurrent writes."""
        with self._fs_write_locks_lock:
            if hash_key not in self._fs_write_locks:
                self._fs_write_locks[hash_key] = threading.Lock()
            return self._fs_write_locks[hash_key]

    def _process_streaming_item(self, item: _StreamingWorkItem, cached_hash: Optional[str]) -> None:
        """Process a streaming work item for filesystem."""
        # Hash first, then upload in UPLOAD stage
        item.file_hash = self._stream_hash_file(item.file_path)

        if self._progress_state is not None:
            self._progress_state.record_hash_complete(item.file_size, skipped=False)

        if cached_hash is not None and item.file_hash != cached_hash:
            if self._data_cache.object_exists(item.file_hash, self._hash_alg.value):
                item.skipped = True
                item.uploaded = False
                if self._progress_state is not None:
                    self._progress_state.record_upload_complete(item.file_size, skipped=True)
                self._record_result(item)
                self._decrement_pending()
                return

        self._upload_executor.submit(self._do_upload, item)

    def _process_chunk_item(self, item: _ChunkWorkItem, cached_hash: Optional[str]) -> None:
        """Process a chunk work item for filesystem."""
        chunk_size = item.chunk_end - item.chunk_start

        self._memory_pool.allocate(chunk_size)

        try:
            with open(item.file_path, "rb") as f:
                f.seek(item.chunk_start)
                item.data = f.read(chunk_size)

            if item.data is not None:
                item.chunk_hash = hash_data(item.data, self._hash_alg)

                if self._progress_state is not None:
                    self._progress_state.record_hash_complete(chunk_size, skipped=False)

                if cached_hash is not None and item.chunk_hash != cached_hash:
                    if self._data_cache.object_exists(item.chunk_hash, self._hash_alg.value):
                        self._memory_pool.release(chunk_size)
                        item.data = None
                        item.skipped = True
                        item.uploaded = False
                        if self._progress_state is not None:
                            self._progress_state.record_upload_complete(chunk_size, skipped=True)
                        self._record_result(item)
                        self._decrement_pending()
                        return
        except Exception:
            self._memory_pool.release(chunk_size)
            raise

        self._upload_executor.submit(self._do_upload, item)

    def _upload_chunk(self, item: _ChunkWorkItem) -> None:
        """Write chunk to filesystem."""
        if not isinstance(self._data_cache, FileSystemDataCache):
            raise TypeError(f"Expected FileSystemDataCache, got {type(self._data_cache).__name__}")
        if item.data is None or item.chunk_hash is None:
            return

        chunk_size = len(item.data)

        try:
            file_path = Path(self._data_cache.get_object_key(item.chunk_hash, self._hash_alg.value))

            hash_lock = self._get_fs_write_lock(item.chunk_hash)

            with hash_lock:
                if file_path.exists():
                    item.skipped = True
                    item.uploaded = False
                    if self._progress_state is not None:
                        self._progress_state.record_upload_complete(chunk_size, skipped=True)
                    return

                file_path.parent.mkdir(parents=True, exist_ok=True)

                temp_suffix = secrets.token_hex(8)
                temp_path = file_path.parent / f"{file_path.name}.tmp.{temp_suffix}"
                try:
                    with open(temp_path, "wb") as f:
                        f.write(item.data)
                    os.replace(temp_path, file_path)
                    item.uploaded = True
                    item.skipped = False
                    if self._progress_state is not None:
                        self._progress_state.record_upload_complete(chunk_size, skipped=False)
                except Exception:
                    try:
                        if temp_path.exists():
                            temp_path.unlink()
                    except Exception:
                        pass
                    raise

        finally:
            self._memory_pool.release(chunk_size)
            item.data = None

    def _upload_streaming(self, item: _StreamingWorkItem) -> bool:
        """Upload a large file to filesystem by streaming copy."""
        import xxhash

        if not isinstance(self._data_cache, FileSystemDataCache):
            raise TypeError(f"Expected FileSystemDataCache, got {type(self._data_cache).__name__}")
        if item.file_hash is None:
            return True
        if self._hash_alg != HashAlgorithm.XXH128:
            raise ValueError(f"Unsupported hash algorithm for streaming: {self._hash_alg}")

        if self._data_cache.object_exists(item.file_hash, self._hash_alg.value):
            item.skipped = True
            item.uploaded = False
            if self._progress_state is not None:
                self._progress_state.record_upload_complete(item.file_size, skipped=True)
            return True

        dest_path = Path(self._data_cache.get_object_key(item.file_hash, self._hash_alg.value))

        hash_lock = self._get_fs_write_lock(item.file_hash)

        with hash_lock:
            if dest_path.exists():
                item.skipped = True
                item.uploaded = False
                if self._progress_state is not None:
                    self._progress_state.record_upload_complete(item.file_size, skipped=True)
                return True

            dest_path.parent.mkdir(parents=True, exist_ok=True)

            temp_suffix = secrets.token_hex(8)
            temp_path = dest_path.parent / f"{dest_path.name}.tmp.{temp_suffix}"
            hasher = xxhash.xxh128()
            bytes_written = 0

            try:
                with open(item.file_path, "rb") as src, open(temp_path, "wb") as dst:
                    while True:
                        chunk = src.read(DEFAULT_STREAM_BUFFER_SIZE)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        dst.write(chunk)
                        bytes_written += len(chunk)

                upload_hash = hasher.hexdigest()

                if upload_hash != item.file_hash:
                    raise ValueError(
                        f"Hash mismatch during streaming upload of '{item.file_path}': "
                        f"expected {item.file_hash}, got {upload_hash}. "
                        f"File may have been modified during processing."
                    )

                os.replace(temp_path, dest_path)

                if self._progress_state is not None:
                    self._progress_state.record_upload_complete(bytes_written, skipped=False)

                item.uploaded = True
                item.skipped = False
                return True
            except Exception:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except Exception:
                    pass
                raise

    def _upload_multipart_part(self, item: _MultipartPartWorkItem) -> None:
        """FileSystem doesn't use multipart uploads."""
        raise NotImplementedError("FileSystem pipeline does not support multipart uploads")
