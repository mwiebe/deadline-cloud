# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
FileSystem data cache support for download_manifest operation.

This module implements filesystem-specific download functionality including:
- Single file copies from filesystem cache
- Chunk copies for chunked files
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from ...progress_tracker import ProgressTracker


def download_file_from_filesystem(
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


def download_fs_chunk_to_offset(
    source_path: Path,
    chunk_idx: int,
    temp_path: Path,
    offset: int,
    progress_tracker: Optional[ProgressTracker],
) -> int:
    """
    Copy a single chunk from filesystem cache and write it to a specific offset in the temp file.

    Each thread opens its own file handle and seeks to the correct offset before
    writing. Since chunks write to non-overlapping regions, no locking is needed.

    Args:
        source_path: Path to the chunk file in the filesystem cache.
        chunk_idx: The index of this chunk (for logging).
        temp_path: Path to the pre-allocated temp file.
        offset: The byte offset where this chunk should be written.
        progress_tracker: Optional progress tracker for download progress.

    Returns:
        The number of bytes written.
    """
    # Read the chunk from the filesystem cache
    with open(source_path, "rb") as chunk_file:
        chunk_data = chunk_file.read()

    # Write to the pre-allocated file at the correct offset
    with open(temp_path, "r+b") as f:
        f.seek(offset)
        f.write(chunk_data)

    if progress_tracker:
        progress_tracker.track_progress_callback(len(chunk_data))

    return len(chunk_data)
