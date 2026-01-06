# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_upload_manifest pipeline internals.

These tests cover:
- _MemoryPool class
- _ChunkWorkItem dataclass
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from deadline.job_attachments.asset_manifests._operations._hash_upload_manifest import (
    _ChunkWorkItem,
    _MemoryPool,
)


class TestMemoryPool:
    """Tests for the _MemoryPool class."""

    def test_allocate_within_limit(self) -> None:
        """Test allocation within memory limit."""
        pool = _MemoryPool(max_bytes=1000)
        pool.allocate(500)
        assert pool.allocated == 500

    def test_release_memory(self) -> None:
        """Test releasing memory."""
        pool = _MemoryPool(max_bytes=1000)
        pool.allocate(500)
        pool.release(300)
        assert pool.allocated == 200

    def test_allocate_releases_on_release(self) -> None:
        """Test that allocation unblocks when memory is released."""
        pool = _MemoryPool(max_bytes=100)
        pool.allocate(80)

        allocation_complete = threading.Event()

        def allocate_more() -> None:
            pool.allocate(50)
            allocation_complete.set()

        thread = threading.Thread(target=allocate_more)
        thread.start()

        # Give the thread time to start and block
        time.sleep(0.1)
        assert not allocation_complete.is_set()

        # Release memory to unblock
        pool.release(50)
        thread.join(timeout=1.0)
        assert allocation_complete.is_set()

    def test_multiple_allocations(self) -> None:
        """Test multiple sequential allocations."""
        pool = _MemoryPool(max_bytes=1000)
        pool.allocate(200)
        pool.allocate(300)
        pool.allocate(100)
        assert pool.allocated == 600

    def test_release_all_memory(self) -> None:
        """Test releasing all allocated memory."""
        pool = _MemoryPool(max_bytes=1000)
        pool.allocate(500)
        pool.release(500)
        assert pool.allocated == 0

    def test_allocate_exact_limit(self) -> None:
        """Test allocation up to exact limit."""
        pool = _MemoryPool(max_bytes=1000)
        pool.allocate(1000)
        assert pool.allocated == 1000


class TestChunkWorkItem:
    """Tests for the _ChunkWorkItem dataclass."""

    def test_create_work_item(self) -> None:
        """Test creating a chunk work item."""
        item = _ChunkWorkItem(
            file_path=Path("/test/file.txt"),
            cache_key="/test/file.txt",
            file_size=1000,
            mtime=12345,
            chunk_index=0,
            chunk_start=0,
            chunk_end=1000,
        )
        assert item.file_path == Path("/test/file.txt")
        assert item.cache_key == "/test/file.txt"
        assert item.file_size == 1000
        assert item.chunk_index == 0
        assert item.data is None
        assert item.chunk_hash is None

    def test_work_item_defaults(self) -> None:
        """Test that work item has correct defaults."""
        item = _ChunkWorkItem(
            file_path=Path("/test/file.txt"),
            cache_key="/test/file.txt",
            file_size=1000,
            mtime=12345,
            chunk_index=0,
            chunk_start=0,
            chunk_end=1000,
        )
        assert item.data is None
        assert item.chunk_hash is None
        assert item.uploaded is False
        assert item.skipped is False

    def test_work_item_with_data(self) -> None:
        """Test work item with data populated."""
        item = _ChunkWorkItem(
            file_path=Path("/test/file.txt"),
            cache_key="/test/file.txt",
            file_size=1000,
            mtime=12345,
            chunk_index=0,
            chunk_start=0,
            chunk_end=1000,
            data=b"test data",
        )
        assert item.data == b"test data"

    def test_work_item_chunk_range(self) -> None:
        """Test work item with specific chunk range."""
        item = _ChunkWorkItem(
            file_path=Path("/test/file.txt"),
            cache_key="/test/file.txt",
            file_size=3000,
            mtime=12345,
            chunk_index=1,
            chunk_start=1000,
            chunk_end=2000,
        )
        assert item.chunk_index == 1
        assert item.chunk_start == 1000
        assert item.chunk_end == 2000
