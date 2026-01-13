# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
S3-specific pipeline for hash_upload_abs_manifest operation.
"""

from __future__ import annotations

import random
import time
import threading
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from botocore.exceptions import BotoCoreError, ClientError

from .._content_addressed_data_cache import S3DataCache
from ...asset_manifests.hash_algorithms import HashAlgorithm, hash_data
from ...caches.s3_check_cache import S3CheckCacheEntry
from ...exceptions import (
    JobAttachmentsS3ClientError,
    JobAttachmentS3BotoCoreError,
    COMMON_ERROR_GUIDANCE_FOR_S3,
)
from ._hash_upload_abs_manifest_pipeline import (
    HashUploadPipelineBase,
    PipelineWorkItem,
    _ChunkWorkItem,
    _StreamingWorkItem,
    _MultipartPartWorkItem,
    _MultipartUploadState,
)

logger = logging.getLogger("deadline.job_attachments.hash_upload")

# Constants for probabilistic S3 cache validation
S3_CACHE_VALIDATION_INITIAL_COUNT = 100  # Always verify first N cache hits
S3_CACHE_VALIDATION_SAMPLE_RATE = 0.01  # 1% sampling after initial count


@dataclass
class _S3CacheValidationState:
    """
    Tracks probabilistic S3 cache validation during upload.

    Performs HeadObject verification on a sample of S3 check cache hits to detect
    stale cache entries (objects deleted from S3). If any verification fails,
    the cache is invalidated and all previously skipped items are re-queued.
    """

    cache_hit_count: int = 0
    cache_invalidated: bool = False
    skipped_items: List[PipelineWorkItem] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def should_verify(self) -> bool:
        """Determine if this cache hit should be verified with HeadObject."""
        with self.lock:
            self.cache_hit_count += 1
            if self.cache_hit_count <= S3_CACHE_VALIDATION_INITIAL_COUNT:
                return True
            return random.random() < S3_CACHE_VALIDATION_SAMPLE_RATE

    def record_skipped_item(self, item: PipelineWorkItem) -> None:
        """Record an item that was skipped due to cache hit."""
        with self.lock:
            if not self.cache_invalidated:
                self.skipped_items.append(item)

    def invalidate(self) -> List[PipelineWorkItem]:
        """Mark cache as invalid and return items to re-queue."""
        with self.lock:
            if self.cache_invalidated:
                return []
            self.cache_invalidated = True
            items = self.skipped_items
            self.skipped_items = []
            return items

    def is_invalidated(self) -> bool:
        """Check if cache has been invalidated."""
        with self.lock:
            return self.cache_invalidated


class S3HashUploadPipeline(HashUploadPipelineBase):
    """S3-specific implementation of the hash+upload pipeline."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # S3 cache validation state (only when s3_check_cache is enabled)
        self._s3_cache_validation: Optional[_S3CacheValidationState] = None
        if isinstance(self._data_cache, S3DataCache):
            if self._data_cache.s3_check_cache is not None and not self._data_cache.force_s3_check:
                self._s3_cache_validation = _S3CacheValidationState()

    def was_cache_invalidated(self) -> bool:
        """Check if the S3 check cache was invalidated during pipeline execution."""
        if self._s3_cache_validation is None:
            return False
        return self._s3_cache_validation.is_invalidated()

    def _check_data_cache_exists(self, cached_hash: str) -> bool:
        """Check if object exists in S3, with probabilistic cache validation."""
        if not isinstance(self._data_cache, S3DataCache):
            return self._data_cache.object_exists(cached_hash, self._hash_alg.value)

        # If cache was already invalidated, use HeadObject directly
        if self._s3_cache_validation is not None and self._s3_cache_validation.is_invalidated():
            return self._data_cache.head_object_exists(cached_hash, self._hash_alg.value)

        # Check S3 check cache with probabilistic validation
        if self._s3_cache_validation is not None:
            if (
                self._data_cache.get_check_cache_entry(cached_hash, self._hash_alg.value)
                is not None
            ):
                # Cache hit - probabilistically verify with HeadObject
                if self._s3_cache_validation.should_verify():
                    if not self._data_cache.head_object_exists(cached_hash, self._hash_alg.value):
                        self._handle_cache_invalidation(cached_hash)
                        return False
                return True

        # No cache hit or no cache - use HeadObject
        return self._data_cache.head_object_exists(cached_hash, self._hash_alg.value)

    def _on_item_skipped(self, item: PipelineWorkItem) -> None:
        """Track skipped items for potential re-queue if cache is invalidated."""
        if self._s3_cache_validation is not None:
            self._s3_cache_validation.record_skipped_item(item)

    def _handle_cache_invalidation(self, hash_value: str) -> None:
        """Handle S3 cache invalidation when a cached object is found missing."""
        if self._s3_cache_validation is None:
            return

        logger.warning(
            f"S3 check cache validation failed: object with hash {hash_value[:16]}... "
            f"not found in S3. Invalidating cache and re-queuing skipped items."
        )

        items_to_requeue = self._s3_cache_validation.invalidate()

        for item in items_to_requeue:
            item.skipped = False
            item.uploaded = False
            if isinstance(item, _StreamingWorkItem):
                item.file_hash = None
            else:
                item.chunk_hash = None
            self.submit(item)
            logger.debug(f"Re-queued item after cache invalidation: {item.file_path}")

    def _get_stream_buffer_size(self) -> int:
        """Use S3 multipart part size for streaming buffer."""
        if isinstance(self._data_cache, S3DataCache):
            return self._data_cache.multipart_part_size
        return super()._get_stream_buffer_size()

    def _process_streaming_item(self, item: _StreamingWorkItem, cached_hash: Optional[str]) -> None:
        """Process a streaming work item for S3."""
        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")

        multipart_threshold = 2 * self._data_cache.multipart_part_size
        if item.file_size > multipart_threshold:
            self._stream_hash_and_submit_multipart(item)
            return

        # Small file: hash first, then upload in UPLOAD stage
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
                logger.debug(f"Skipped (hash changed, but exists): {item.file_path}")
                return

        self._upload_executor.submit(self._do_upload, item)

    def _process_chunk_item(self, item: _ChunkWorkItem, cached_hash: Optional[str]) -> None:
        """Process a chunk work item for S3."""
        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")

        chunk_size = item.chunk_end - item.chunk_start
        use_multipart = chunk_size >= 2 * self._data_cache.multipart_part_size

        if use_multipart:
            self._read_hash_and_submit_multipart(item, chunk_size)
        else:
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
                                self._progress_state.record_upload_complete(
                                    chunk_size, skipped=True
                                )
                            self._record_result(item)
                            self._decrement_pending()
                            logger.debug(f"Skipped (hash changed, but exists): {item.file_path}")
                            return
            except Exception:
                self._memory_pool.release(chunk_size)
                raise

            self._upload_executor.submit(self._do_upload, item)

    def _read_hash_and_submit_multipart(self, item: _ChunkWorkItem, chunk_size: int) -> None:
        """Read a chunk part-by-part, hash incrementally, then submit parts for parallel upload."""
        import xxhash

        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")
        if self._hash_alg != HashAlgorithm.XXH128:
            raise ValueError(f"Unsupported hash algorithm for multipart: {self._hash_alg}")

        part_size = self._data_cache.multipart_part_size
        part_buffers: List[bytes] = []
        hasher = xxhash.xxh128()

        self._memory_pool.allocate(chunk_size)

        try:
            with open(item.file_path, "rb") as f:
                f.seek(item.chunk_start)
                bytes_remaining = chunk_size

                while bytes_remaining > 0:
                    this_part_size = min(part_size, bytes_remaining)
                    part_data = f.read(this_part_size)
                    if len(part_data) != this_part_size:
                        raise IOError(
                            f"Short read: expected {this_part_size} bytes, got {len(part_data)}"
                        )
                    hasher.update(part_data)
                    part_buffers.append(part_data)
                    bytes_remaining -= this_part_size

            item.chunk_hash = hasher.hexdigest()

            if self._progress_state is not None:
                self._progress_state.record_hash_complete(chunk_size, skipped=False)

            if self._data_cache.object_exists(item.chunk_hash, self._hash_alg.value):
                self._memory_pool.release(chunk_size)
                item.skipped = True
                item.uploaded = False
                if self._progress_state is not None:
                    self._progress_state.record_upload_complete(chunk_size, skipped=True)
                self._record_result(item)
                self._decrement_pending()
                logger.debug(f"Skipped multipart (exists): {item.file_path}")
                return

            s3_key = self._data_cache.get_object_key(item.chunk_hash, self._hash_alg.value)
            upload_id = self._create_multipart_upload(s3_key)

            state = _MultipartUploadState(
                file_hash=item.chunk_hash,
                s3_key=s3_key,
                upload_id=upload_id,
                parts_remaining=len(part_buffers),
                completed_parts=[],
                file_size=chunk_size,
            )

            for _ in range(len(part_buffers) - 1):
                self._increment_pending()

            item.uploaded = True
            item.skipped = False
            self._record_result(item)

            for part_idx, part_data in enumerate(part_buffers):
                part_item = _MultipartPartWorkItem(
                    multipart_state=state,
                    part_number=part_idx + 1,
                    data=part_data,
                )
                self._upload_executor.submit(self._do_upload, part_item)

        except Exception:
            self._memory_pool.release(chunk_size)
            raise

    def _stream_hash_and_submit_multipart(self, item: _StreamingWorkItem) -> None:
        """Stream a large file, hash it, then submit parts for parallel upload."""
        import xxhash

        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")
        if self._hash_alg != HashAlgorithm.XXH128:
            raise ValueError(f"Unsupported hash algorithm for streaming: {self._hash_alg}")

        part_size = self._data_cache.multipart_part_size

        # First pass: compute full file hash AND per-part hashes (discard data)
        file_hasher = xxhash.xxh128()
        part_hashes: List[str] = []
        with open(item.file_path, "rb") as f:
            while True:
                chunk = f.read(part_size)
                if not chunk:
                    break
                file_hasher.update(chunk)
                part_hashes.append(xxhash.xxh128(chunk).hexdigest())

        item.file_hash = file_hasher.hexdigest()

        if self._progress_state is not None:
            self._progress_state.record_hash_complete(item.file_size, skipped=False)

        if self._data_cache.object_exists(item.file_hash, self._hash_alg.value):
            item.skipped = True
            item.uploaded = False
            if self._progress_state is not None:
                self._progress_state.record_upload_complete(item.file_size, skipped=True)
            self._record_result(item)
            self._decrement_pending()
            logger.debug(f"Skipped multipart streaming (exists): {item.file_path}")
            return

        s3_key = self._data_cache.get_object_key(item.file_hash, self._hash_alg.value)
        upload_id = self._create_multipart_upload(s3_key)

        num_parts = len(part_hashes)

        state = _MultipartUploadState(
            file_hash=item.file_hash,
            s3_key=s3_key,
            upload_id=upload_id,
            parts_remaining=num_parts,
            completed_parts=[],
            file_size=item.file_size,
            part_hashes=part_hashes,
        )

        for _ in range(num_parts - 1):
            self._increment_pending()

        item.uploaded = True
        item.skipped = False
        self._record_result(item)

        # Second pass: read and submit parts one at a time (with expected hash for verification)
        parts_submitted = 0
        try:
            with open(item.file_path, "rb") as f:
                part_number = 1
                bytes_remaining = item.file_size

                while bytes_remaining > 0:
                    with self._error_lock:
                        if self._error is not None:
                            for _ in range(num_parts - parts_submitted):
                                self._decrement_pending()
                            return

                    this_part_size = min(part_size, bytes_remaining)
                    self._memory_pool.allocate(this_part_size)

                    try:
                        part_data = f.read(this_part_size)
                        if len(part_data) != this_part_size:
                            self._memory_pool.release(this_part_size)
                            raise IOError(
                                f"Short read: expected {this_part_size} bytes, got {len(part_data)}"
                            )

                        part_item = _MultipartPartWorkItem(
                            multipart_state=state,
                            part_number=part_number,
                            data=part_data,
                            expected_hash=part_hashes[part_number - 1],
                        )
                        try:
                            self._upload_executor.submit(self._do_upload, part_item)
                        except RuntimeError:
                            self._memory_pool.release(this_part_size)
                            for _ in range(num_parts - parts_submitted):
                                self._decrement_pending()
                            return
                        parts_submitted += 1

                        part_number += 1
                        bytes_remaining -= this_part_size

                    except Exception:
                        self._memory_pool.release(this_part_size)
                        raise

        except Exception as e:
            with state.lock:
                state.part_errors.append(e)
            for _ in range(num_parts - parts_submitted):
                self._decrement_pending()
            raise

    def _upload_chunk(self, item: _ChunkWorkItem) -> None:
        """Upload chunk to S3."""
        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")
        if item.data is None or item.chunk_hash is None:
            return

        chunk_size = len(item.data)

        try:
            s3_key = self._data_cache.get_object_key(item.chunk_hash, self._hash_alg.value)
            cache_key = f"{self._data_cache.s3_bucket}/{s3_key}"

            cache_invalidated = self.was_cache_invalidated()
            if (
                not self._data_cache.force_s3_check
                and not cache_invalidated
                and self._data_cache.s3_check_cache is not None
            ):
                cache_entry = self._data_cache.s3_check_cache.get_entry(cache_key)
                if cache_entry is not None:
                    item.skipped = True
                    item.uploaded = False
                    logger.debug(f"Skipping upload (cached): {s3_key}")
                    if self._progress_state is not None:
                        self._progress_state.record_upload_complete(chunk_size, skipped=True)
                    return

            uploaded = self._upload_to_s3_if_not_exists(item.data, s3_key)
            item.uploaded = uploaded
            item.skipped = not uploaded

            if not cache_invalidated and self._data_cache.s3_check_cache is not None:
                self._data_cache.s3_check_cache.put_entry(
                    S3CheckCacheEntry(s3_key=cache_key, last_seen_time=str(time.time()))
                )

            if self._progress_state is not None:
                self._progress_state.record_upload_complete(chunk_size, skipped=item.skipped)

        finally:
            self._memory_pool.release(chunk_size)
            item.data = None

    def _upload_streaming(self, item: _StreamingWorkItem) -> bool:
        """Stream file to S3 using single PUT."""
        import xxhash

        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")
        if item.file_hash is None:
            return True
        if self._hash_alg != HashAlgorithm.XXH128:
            raise ValueError(f"Unsupported hash algorithm for streaming: {self._hash_alg}")

        if self._data_cache.object_exists(item.file_hash, self._hash_alg.value):
            item.skipped = True
            item.uploaded = False
            logger.debug(f"Skipping streaming upload (exists): {item.file_path}")
            if self._progress_state is not None:
                self._progress_state.record_upload_complete(item.file_size, skipped=True)
            return True

        s3_key = self._data_cache.get_object_key(item.file_hash, self._hash_alg.value)

        try:
            extra_args: Dict[str, Any] = {}
            if self._data_cache.expected_bucket_owner is not None:
                extra_args["ExpectedBucketOwner"] = self._data_cache.expected_bucket_owner

            with open(item.file_path, "rb") as f:
                data = f.read()

            hasher = xxhash.xxh128()
            hasher.update(data)
            upload_hash = hasher.hexdigest()

            if upload_hash != item.file_hash:
                raise ValueError(
                    f"Hash mismatch during streaming upload of '{item.file_path}': "
                    f"expected {item.file_hash}, got {upload_hash}. "
                    f"File may have been modified during processing."
                )

            put_kwargs: Dict[str, Any] = {
                "Bucket": self._data_cache.s3_bucket,
                "Key": s3_key,
                "Body": data,
            }
            put_kwargs.update(extra_args)
            self._data_cache.s3_client.put_object(**put_kwargs)

            if self._progress_state is not None:
                self._progress_state.record_upload_complete(len(data), skipped=False)

            item.uploaded = True
            item.skipped = False
            logger.debug(f"Streamed upload (verified): {s3_key}")
            return True

        except ClientError as exc:
            status_code = int(exc.response["ResponseMetadata"]["HTTPStatusCode"])
            raise JobAttachmentsS3ClientError(
                action="uploading file",
                status_code=status_code,
                bucket_name=self._data_cache.s3_bucket,
                key_or_prefix=s3_key,
                message=str(exc),
            ) from exc
        except BotoCoreError as bce:
            raise JobAttachmentS3BotoCoreError(
                action="uploading file",
                error_details=str(bce),
            ) from bce

    def _upload_multipart_part(self, item: _MultipartPartWorkItem) -> None:
        """Upload a single part of a multipart upload."""
        import xxhash

        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")

        state = item.multipart_state
        part_size = len(item.data)

        try:
            # Verify part hash if expected (for streaming uploads where file may have changed)
            if item.expected_hash is not None:
                actual_hash = xxhash.xxh128(item.data).hexdigest()
                if actual_hash != item.expected_hash:
                    raise ValueError(
                        f"Hash mismatch during multipart upload: part {item.part_number} "
                        f"expected {item.expected_hash}, got {actual_hash}. "
                        f"File may have been modified during processing."
                    )

            extra_args: Dict[str, Any] = {}
            if self._data_cache.expected_bucket_owner is not None:
                extra_args["ExpectedBucketOwner"] = self._data_cache.expected_bucket_owner

            response = self._data_cache.s3_client.upload_part(
                Bucket=self._data_cache.s3_bucket,
                Key=state.s3_key,
                UploadId=state.upload_id,
                PartNumber=item.part_number,
                Body=item.data,
                **extra_args,
            )
            item.etag = response["ETag"]
            item.uploaded = True

            with state.lock:
                state.completed_parts.append({"PartNumber": item.part_number, "ETag": item.etag})
                state.total_bytes_uploaded += part_size
                state.parts_remaining -= 1
                all_done = state.parts_remaining == 0
                has_errors = len(state.part_errors) > 0

            if all_done:
                if has_errors:
                    self._abort_multipart_upload(state)
                    self._record_error(state.part_errors[0])
                else:
                    self._complete_multipart_upload(state)

        except ValueError as ve:
            # Hash mismatch - abort the multipart upload
            with state.lock:
                state.part_errors.append(ve)
                state.parts_remaining -= 1
                first_error = len(state.part_errors) == 1

            if first_error:
                self._abort_multipart_upload(state)
                self._record_error(ve)

        except (ClientError, BotoCoreError) as e:
            wrapped_error: Exception
            if isinstance(e, ClientError):
                status_code = int(e.response["ResponseMetadata"]["HTTPStatusCode"])
                wrapped_error = JobAttachmentsS3ClientError(
                    action="uploading multipart part",
                    status_code=status_code,
                    bucket_name=self._data_cache.s3_bucket,
                    key_or_prefix=state.s3_key,
                    message=str(e),
                )
            else:
                wrapped_error = JobAttachmentS3BotoCoreError(
                    action="uploading multipart part",
                    error_details=str(e),
                )

            with state.lock:
                state.part_errors.append(wrapped_error)
                state.parts_remaining -= 1
                first_error = len(state.part_errors) == 1

            if first_error:
                self._abort_multipart_upload(state)
                self._record_error(wrapped_error)

        finally:
            self._memory_pool.release(part_size)
            self._decrement_pending()

    def _upload_to_s3_if_not_exists(self, data: bytes, s3_key: str) -> bool:
        """Upload data to S3 only if the object doesn't already exist."""
        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")

        try:
            head_kwargs: Dict[str, Any] = {
                "Bucket": self._data_cache.s3_bucket,
                "Key": s3_key,
            }
            if self._data_cache.expected_bucket_owner is not None:
                head_kwargs["ExpectedBucketOwner"] = self._data_cache.expected_bucket_owner

            self._data_cache.s3_client.head_object(**head_kwargs)
            return False
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            if error_code != "404":
                status_code = int(exc.response["ResponseMetadata"]["HTTPStatusCode"])
                status_code_guidance = {
                    **COMMON_ERROR_GUIDANCE_FOR_S3,
                    403: (
                        "Forbidden or Access denied. Please check your AWS credentials, and ensure "
                        "that your AWS IAM Role or User has the 's3:HeadObject' permission."
                    ),
                }
                raise JobAttachmentsS3ClientError(
                    action="checking chunk existence",
                    status_code=status_code,
                    bucket_name=self._data_cache.s3_bucket,
                    key_or_prefix=s3_key,
                    message=f"{status_code_guidance.get(status_code, '')} {str(exc)}",
                ) from exc
        except BotoCoreError as bce:
            raise JobAttachmentS3BotoCoreError(
                action="checking chunk existence",
                error_details=str(bce),
            ) from bce

        try:
            put_kwargs: Dict[str, Any] = {
                "Bucket": self._data_cache.s3_bucket,
                "Key": s3_key,
                "Body": data,
            }
            if self._data_cache.expected_bucket_owner is not None:
                put_kwargs["ExpectedBucketOwner"] = self._data_cache.expected_bucket_owner

            self._data_cache.s3_client.put_object(**put_kwargs)
            return True
        except ClientError as exc:
            status_code = int(exc.response["ResponseMetadata"]["HTTPStatusCode"])
            status_code_guidance = {
                **COMMON_ERROR_GUIDANCE_FOR_S3,
                403: (
                    "Forbidden or Access denied. Please check your AWS credentials, and ensure "
                    "that your AWS IAM Role or User has the 's3:PutObject' permission."
                ),
                404: "Not found. Please check your bucket name.",
            }
            raise JobAttachmentsS3ClientError(
                action="uploading chunk",
                status_code=status_code,
                bucket_name=self._data_cache.s3_bucket,
                key_or_prefix=s3_key,
                message=f"{status_code_guidance.get(status_code, '')} {str(exc)}",
            ) from exc
        except BotoCoreError as bce:
            raise JobAttachmentS3BotoCoreError(
                action="uploading chunk",
                error_details=str(bce),
            ) from bce

    def _create_multipart_upload(self, s3_key: str) -> str:
        """Create a new multipart upload and return the upload ID."""
        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")

        extra_args: Dict[str, Any] = {}
        if self._data_cache.expected_bucket_owner is not None:
            extra_args["ExpectedBucketOwner"] = self._data_cache.expected_bucket_owner

        response = self._data_cache.s3_client.create_multipart_upload(
            Bucket=self._data_cache.s3_bucket,
            Key=s3_key,
            **extra_args,
        )
        return response["UploadId"]

    def _complete_multipart_upload(self, state: _MultipartUploadState) -> None:
        """Complete a multipart upload after all parts are uploaded."""
        if not isinstance(self._data_cache, S3DataCache):
            raise TypeError(f"Expected S3DataCache, got {type(self._data_cache).__name__}")

        sorted_parts = sorted(state.completed_parts, key=lambda p: p["PartNumber"])

        try:
            extra_args: Dict[str, Any] = {}
            if self._data_cache.expected_bucket_owner is not None:
                extra_args["ExpectedBucketOwner"] = self._data_cache.expected_bucket_owner

            self._data_cache.s3_client.complete_multipart_upload(
                Bucket=self._data_cache.s3_bucket,
                Key=state.s3_key,
                UploadId=state.upload_id,
                MultipartUpload={"Parts": sorted_parts},
                **extra_args,
            )
            logger.debug(f"Completed multipart upload: {state.s3_key}")

            if self._data_cache.s3_check_cache is not None:
                cache_key = f"{self._data_cache.s3_bucket}/{state.s3_key}"
                self._data_cache.s3_check_cache.put_entry(
                    S3CheckCacheEntry(s3_key=cache_key, last_seen_time=str(time.time()))
                )

            if self._progress_state is not None:
                self._progress_state.record_upload_complete(state.file_size, skipped=False)

        except (ClientError, BotoCoreError) as e:
            logger.error(f"Failed to complete multipart upload: {e}")
            self._abort_multipart_upload(state)
            raise

    def _abort_multipart_upload(self, state: _MultipartUploadState) -> None:
        """Abort a multipart upload on error."""
        if not isinstance(self._data_cache, S3DataCache):
            return

        try:
            extra_args: Dict[str, Any] = {}
            if self._data_cache.expected_bucket_owner is not None:
                extra_args["ExpectedBucketOwner"] = self._data_cache.expected_bucket_owner

            self._data_cache.s3_client.abort_multipart_upload(
                Bucket=self._data_cache.s3_bucket,
                Key=state.s3_key,
                UploadId=state.upload_id,
                **extra_args,
            )
            logger.debug(f"Aborted multipart upload: {state.s3_key}")
        except Exception:
            pass  # Best effort cleanup
