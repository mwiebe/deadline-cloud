# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
S3 data cache support for download_manifest operation.

This module implements S3-specific download functionality including:
- Single file downloads from S3
- Multi-part parallel downloads for large files
- Byte-range requests for parallel part downloads
- Chunk downloads for chunked files
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from pathlib import Path
from typing import Any, List, Optional

from botocore.exceptions import BotoCoreError, ClientError

from ...exceptions import (
    JobAttachmentsS3ClientError,
    JobAttachmentS3BotoCoreError,
    COMMON_ERROR_GUIDANCE_FOR_S3,
)
from ...progress_tracker import ProgressTracker

logger = logging.getLogger("deadline.job_attachments.download")

# Part size for multi-part parallel downloads (8MB)
# Files/chunks larger than this will be downloaded in parallel parts
DEFAULT_MULTIPART_DOWNLOAD_PART_SIZE = 8 * 1024 * 1024  # 8MB

# Minimum file size to use multi-part download (files smaller than this use single request)
MIN_SIZE_FOR_MULTIPART_DOWNLOAD = 16 * 1024 * 1024  # 16MB


def download_file_from_s3(
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


def download_s3_part_to_offset(
    s3_client: Any,
    s3_bucket: str,
    s3_key: str,
    temp_path: Path,
    offset: int,
    part_start: int,
    part_end: int,
    part_idx: int,
    progress_tracker: Optional[ProgressTracker],
) -> int:
    """
    Download a byte range from S3 and write it to a specific offset in the temp file.

    Uses S3 GetObject with Range header to download a specific byte range.
    Each thread opens its own file handle and seeks to the correct offset.
    Since parts write to non-overlapping regions, no locking is needed.

    Args:
        s3_client: The boto3 S3 client.
        s3_bucket: The S3 bucket name.
        s3_key: The S3 object key.
        temp_path: Path to the pre-allocated temp file.
        offset: The byte offset in the temp file where this part should be written.
        part_start: The start byte in the S3 object (inclusive).
        part_end: The end byte in the S3 object (inclusive).
        part_idx: The index of this part (for logging/error messages).
        progress_tracker: Optional progress tracker for download progress.

    Returns:
        The number of bytes written.
    """
    try:
        # Use Range header for byte-range request (inclusive on both ends)
        range_header = f"bytes={part_start}-{part_end}"
        response = s3_client.get_object(
            Bucket=s3_bucket,
            Key=s3_key,
            Range=range_header,
        )

        # Read the part data
        part_data = response["Body"].read()
        bytes_written = len(part_data)

        # Write to the pre-allocated file at the correct offset
        with open(temp_path, "r+b") as f:
            f.seek(offset)
            f.write(part_data)

        if progress_tracker:
            progress_tracker.track_progress_callback(bytes_written)

        return bytes_written

    except ClientError as exc:
        status_code = int(exc.response["ResponseMetadata"]["HTTPStatusCode"])
        status_code_guidance = {
            **COMMON_ERROR_GUIDANCE_FOR_S3,
            403: (
                "Forbidden or Access denied. Please check your AWS credentials, and ensure "
                "that your AWS IAM Role or User has the 's3:GetObject' permission."
            ),
            404: "Not found. Please check your bucket name and object key.",
            416: "Range not satisfiable. The requested byte range is invalid.",
        }
        raise JobAttachmentsS3ClientError(
            action=f"downloading part {part_idx}",
            status_code=status_code,
            bucket_name=s3_bucket,
            key_or_prefix=s3_key,
            message=f"{status_code_guidance.get(status_code, '')} {str(exc)}",
        ) from exc
    except BotoCoreError as bce:
        raise JobAttachmentS3BotoCoreError(
            action=f"downloading part {part_idx}",
            error_details=str(bce),
        ) from bce


async def download_s3_multipart_async(
    s3_client: Any,
    s3_bucket: str,
    s3_key: str,
    temp_path: Path,
    file_size: int,
    executor: concurrent.futures.ThreadPoolExecutor,
    progress_tracker: Optional[ProgressTracker],
    part_size: Optional[int] = None,
) -> int:
    """
    Download an S3 object using parallel byte-range requests.

    Divides the file into parts and downloads each part in parallel using
    the shared thread pool. Each part is written directly to its correct
    offset in the pre-allocated temp file.

    Args:
        s3_client: The boto3 S3 client.
        s3_bucket: The S3 bucket name.
        s3_key: The S3 object key.
        temp_path: Path to the pre-allocated temp file.
        file_size: The total size of the file in bytes.
        executor: The shared ThreadPoolExecutor for parallel downloads.
        progress_tracker: Optional progress tracker for download progress.
        part_size: Size of each part in bytes. If None, uses DEFAULT_MULTIPART_DOWNLOAD_PART_SIZE.

    Returns:
        The total number of bytes downloaded.
    """
    loop = asyncio.get_running_loop()

    # Use default part size if not specified (read at runtime for testability)
    if part_size is None:
        part_size = DEFAULT_MULTIPART_DOWNLOAD_PART_SIZE

    # Calculate parts
    num_parts = (file_size + part_size - 1) // part_size  # Ceiling division
    part_tasks: List[asyncio.Future[int]] = []

    for part_idx in range(num_parts):
        part_start = part_idx * part_size
        part_end = min(part_start + part_size - 1, file_size - 1)  # Inclusive end
        offset = part_start  # Offset in temp file matches offset in S3 object

        task = loop.run_in_executor(
            executor,
            download_s3_part_to_offset,
            s3_client,
            s3_bucket,
            s3_key,
            temp_path,
            offset,
            part_start,
            part_end,
            part_idx,
            progress_tracker,
        )
        part_tasks.append(task)

    # Wait for all parts to complete
    part_results = await asyncio.gather(*part_tasks, return_exceptions=True)

    # Check for errors
    errors: List[Exception] = []
    total_bytes = 0
    for part_idx, result in enumerate(part_results):
        if isinstance(result, BaseException):
            logger.error(f"Failed to download part {part_idx} of {s3_key}: {result}")
            if isinstance(result, Exception):
                errors.append(result)
            else:
                raise result
        else:
            total_bytes += result

    if errors:
        raise errors[0]

    return total_bytes


def download_s3_chunk_to_offset(
    s3_client: Any,
    s3_bucket: str,
    s3_key: str,
    chunk_idx: int,
    temp_path: Path,
    offset: int,
    progress_tracker: Optional[ProgressTracker],
) -> int:
    """
    Download a single chunk from S3 and write it to a specific offset in the temp file.

    Each thread opens its own file handle and seeks to the correct offset before
    writing. Since chunks write to non-overlapping regions, no locking is needed.

    Args:
        s3_client: The boto3 S3 client.
        s3_bucket: The S3 bucket name.
        s3_key: The S3 object key for the chunk.
        chunk_idx: The index of this chunk (for logging).
        temp_path: Path to the pre-allocated temp file.
        offset: The byte offset where this chunk should be written.
        progress_tracker: Optional progress tracker for download progress.

    Returns:
        The number of bytes written.
    """
    try:
        # Use get_object to download the entire chunk in one request
        response = s3_client.get_object(
            Bucket=s3_bucket,
            Key=s3_key,
        )
        chunk_data = response["Body"].read()

        # Write to the pre-allocated file at the correct offset
        with open(temp_path, "r+b") as f:
            f.seek(offset)
            f.write(chunk_data)

        if progress_tracker:
            progress_tracker.track_progress_callback(len(chunk_data))

        return len(chunk_data)

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
            action=f"downloading chunk {chunk_idx}",
            status_code=status_code,
            bucket_name=s3_bucket,
            key_or_prefix=s3_key,
            message=f"{status_code_guidance.get(status_code, '')} {str(exc)}",
        ) from exc
    except BotoCoreError as bce:
        raise JobAttachmentS3BotoCoreError(
            action=f"downloading chunk {chunk_idx}",
            error_details=str(bce),
        ) from bce


async def download_s3_chunk_multipart_async(
    s3_client: Any,
    s3_bucket: str,
    s3_key: str,
    chunk_idx: int,
    temp_path: Path,
    offset: int,
    chunk_size: int,
    executor: concurrent.futures.ThreadPoolExecutor,
    progress_tracker: Optional[ProgressTracker],
) -> int:
    """
    Download a chunk from S3 using parallel byte-range requests for large chunks.

    For chunks larger than MIN_SIZE_FOR_MULTIPART_DOWNLOAD, uses parallel
    byte-range requests. The offset parameter specifies where in the temp file
    this chunk's data should be written.

    Args:
        s3_client: The boto3 S3 client.
        s3_bucket: The S3 bucket name.
        s3_key: The S3 object key for the chunk.
        chunk_idx: The index of this chunk (for logging).
        temp_path: Path to the pre-allocated temp file.
        offset: The byte offset in the temp file where this chunk starts.
        chunk_size: The size of this chunk in bytes.
        executor: The shared ThreadPoolExecutor for parallel downloads.
        progress_tracker: Optional progress tracker for download progress.

    Returns:
        The number of bytes written.
    """
    loop = asyncio.get_running_loop()

    # Download parts in parallel, writing to correct offsets in temp file
    part_size = DEFAULT_MULTIPART_DOWNLOAD_PART_SIZE
    num_parts = (chunk_size + part_size - 1) // part_size
    part_tasks: List[asyncio.Future[int]] = []

    for part_idx in range(num_parts):
        part_start = part_idx * part_size
        part_end = min(part_start + part_size - 1, chunk_size - 1)
        # Offset in temp file = chunk offset + part offset within chunk
        file_offset = offset + part_start

        task = loop.run_in_executor(
            executor,
            download_s3_part_to_offset,
            s3_client,
            s3_bucket,
            s3_key,
            temp_path,
            file_offset,
            part_start,
            part_end,
            part_idx,
            progress_tracker,
        )
        part_tasks.append(task)

    # Wait for all parts
    part_results = await asyncio.gather(*part_tasks, return_exceptions=True)

    # Check for errors
    errors: List[Exception] = []
    total_bytes = 0
    for part_idx, result in enumerate(part_results):
        if isinstance(result, BaseException):
            logger.error(f"Failed to download part {part_idx} of chunk {chunk_idx}: {result}")
            if isinstance(result, Exception):
                errors.append(result)
            else:
                raise result
        else:
            total_bytes += result

    if errors:
        raise errors[0]

    return total_bytes
