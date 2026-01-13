# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for probabilistic S3 cache validation in hash_upload_abs_manifest.

These tests cover:
- S3CacheValidationState sampling logic
- Cache invalidation and re-queue behavior
- Recovery from stale cache entries
"""

from __future__ import annotations

from pathlib import Path
import threading

from deadline.job_attachments._snapshots._operations._hash_upload_abs_manifest import (
    _S3CacheValidationState,
    _ChunkWorkItem,
    _StreamingWorkItem,
    S3_CACHE_VALIDATION_INITIAL_COUNT,
    S3_CACHE_VALIDATION_SAMPLE_RATE,
)


class TestS3CacheValidationState:
    """Tests for _S3CacheValidationState class."""

    def test_should_verify_first_100_always_true(self) -> None:
        """First 100 cache hits should always be verified."""
        state = _S3CacheValidationState()

        for i in range(S3_CACHE_VALIDATION_INITIAL_COUNT):
            assert state.should_verify() is True, f"Item {i + 1} should be verified"

    def test_should_verify_after_100_probabilistic(self) -> None:
        """After first 100, verification should be probabilistic (1%)."""
        state = _S3CacheValidationState()

        # Exhaust the first 100
        for _ in range(S3_CACHE_VALIDATION_INITIAL_COUNT):
            state.should_verify()

        # Run many iterations and check the rate is approximately 1%
        verify_count = 0
        iterations = 10000
        for _ in range(iterations):
            # Reset counter to stay in probabilistic range
            state.cache_hit_count = S3_CACHE_VALIDATION_INITIAL_COUNT
            if state.should_verify():
                verify_count += 1

        # Should be approximately 1% (allow 0.5% - 2% range for randomness)
        rate = verify_count / iterations
        assert 0.005 < rate < 0.02, f"Verification rate {rate:.3f} outside expected range"

    def test_record_skipped_item(self) -> None:
        """Test recording skipped items for potential re-queue."""
        state = _S3CacheValidationState()

        item1 = _ChunkWorkItem(
            file_path=Path("/test/file1.txt"),
            cache_key="/test/file1.txt",
            file_size=100,
            mtime=12345,
            chunk_index=0,
            chunk_start=0,
            chunk_end=100,
        )
        item2 = _StreamingWorkItem(
            file_path=Path("/test/file2.txt"),
            cache_key="/test/file2.txt",
            file_size=1000,
            mtime=12346,
        )

        state.record_skipped_item(item1)
        state.record_skipped_item(item2)

        assert len(state.skipped_items) == 2

    def test_record_skipped_item_after_invalidation_ignored(self) -> None:
        """Items recorded after invalidation should be ignored."""
        state = _S3CacheValidationState()

        item1 = _ChunkWorkItem(
            file_path=Path("/test/file1.txt"),
            cache_key="/test/file1.txt",
            file_size=100,
            mtime=12345,
            chunk_index=0,
            chunk_start=0,
            chunk_end=100,
        )

        state.record_skipped_item(item1)
        state.invalidate()

        item2 = _ChunkWorkItem(
            file_path=Path("/test/file2.txt"),
            cache_key="/test/file2.txt",
            file_size=200,
            mtime=12346,
            chunk_index=0,
            chunk_start=0,
            chunk_end=200,
        )
        state.record_skipped_item(item2)

        # item2 should not be recorded since cache is already invalidated
        assert len(state.skipped_items) == 0

    def test_invalidate_returns_skipped_items(self) -> None:
        """Invalidation should return all skipped items."""
        state = _S3CacheValidationState()

        item1 = _ChunkWorkItem(
            file_path=Path("/test/file1.txt"),
            cache_key="/test/file1.txt",
            file_size=100,
            mtime=12345,
            chunk_index=0,
            chunk_start=0,
            chunk_end=100,
        )
        item2 = _ChunkWorkItem(
            file_path=Path("/test/file2.txt"),
            cache_key="/test/file2.txt",
            file_size=200,
            mtime=12346,
            chunk_index=0,
            chunk_start=0,
            chunk_end=200,
        )

        state.record_skipped_item(item1)
        state.record_skipped_item(item2)

        items = state.invalidate()

        assert len(items) == 2
        assert item1 in items
        assert item2 in items
        assert state.cache_invalidated is True

    def test_invalidate_second_call_returns_empty(self) -> None:
        """Second invalidation call should return empty list."""
        state = _S3CacheValidationState()

        item = _ChunkWorkItem(
            file_path=Path("/test/file.txt"),
            cache_key="/test/file.txt",
            file_size=100,
            mtime=12345,
            chunk_index=0,
            chunk_start=0,
            chunk_end=100,
        )
        state.record_skipped_item(item)

        first_items = state.invalidate()
        second_items = state.invalidate()

        assert len(first_items) == 1
        assert len(second_items) == 0

    def test_is_invalidated(self) -> None:
        """Test is_invalidated returns correct state."""
        state = _S3CacheValidationState()

        assert state.is_invalidated() is False

        state.invalidate()

        assert state.is_invalidated() is True

    def test_thread_safety(self) -> None:
        """Test that state is thread-safe."""
        state = _S3CacheValidationState()
        errors = []

        def record_items():
            try:
                for i in range(100):
                    item = _ChunkWorkItem(
                        file_path=Path(f"/test/file{i}.txt"),
                        cache_key=f"/test/file{i}.txt",
                        file_size=100,
                        mtime=12345 + i,
                        chunk_index=0,
                        chunk_start=0,
                        chunk_end=100,
                    )
                    state.record_skipped_item(item)
            except Exception as e:
                errors.append(e)

        def check_verify():
            try:
                for _ in range(200):
                    state.should_verify()
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=record_items),
            threading.Thread(target=record_items),
            threading.Thread(target=check_verify),
            threading.Thread(target=check_verify),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Thread safety errors: {errors}"


class TestS3CacheValidationConstants:
    """Tests for S3 cache validation constants."""

    def test_initial_count_is_100(self) -> None:
        """Verify the initial count constant is 100."""
        assert S3_CACHE_VALIDATION_INITIAL_COUNT == 100

    def test_sample_rate_is_one_percent(self) -> None:
        """Verify the sample rate constant is 1%."""
        assert S3_CACHE_VALIDATION_SAMPLE_RATE == 0.01
