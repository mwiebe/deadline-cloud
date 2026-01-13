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
from typing import Optional

from deadline.job_attachments._snapshots._operations._hash_upload_abs_manifest_s3_pipeline import (
    _S3CacheValidationState,
    S3_CACHE_VALIDATION_INITIAL_COUNT,
    S3_CACHE_VALIDATION_SAMPLE_RATE,
)
from deadline.job_attachments._snapshots._operations._hash_upload_abs_manifest_pipeline import (
    _ChunkWorkItem,
    _StreamingWorkItem,
)
from deadline.job_attachments._snapshots import (
    hash_upload_abs_manifest,
    S3DataCache,
    AbsSnapshot,
    ManifestFilePath,
    NO_ACCOUNT_ID_CHECK,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.caches.hash_cache import HashCache
from deadline.job_attachments.caches.s3_check_cache import S3CheckCache


TEST_BUCKET = "test-s3-cache-validation-bucket"
TEST_KEY_PREFIX = "Data"


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


class TestS3CacheValidationIntegration:
    """Integration tests for S3 cache validation with moto S3."""

    @classmethod
    def setup_method(cls):
        cls.s3_client = None

    def setup_s3_bucket(self, s3, create_s3_bucket) -> None:
        """Create the test S3 bucket."""
        create_s3_bucket(TEST_BUCKET)
        self.s3_client = s3

    def _create_s3_data_cache(self, s3_check_cache: Optional[S3CheckCache] = None) -> S3DataCache:
        """Create an S3DataCache for testing."""
        return S3DataCache(
            s3_bucket=TEST_BUCKET,
            s3_key_prefix=TEST_KEY_PREFIX,
            s3_client=self.s3_client,
            s3_check_cache=s3_check_cache,
            account_id=NO_ACCOUNT_ID_CHECK,
        )

    def _create_test_manifest(self, tmp_path: Path, content: str = "Test content") -> AbsSnapshot:
        """Create a test file and manifest."""
        test_file = tmp_path / "test.txt"
        test_file.write_text(content)
        file_stat = test_file.stat()

        return AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

    def test_stale_cache_detected_and_file_uploaded(
        self, tmp_path: Path, s3, create_s3_bucket
    ) -> None:
        """Test that stale S3 check cache entries are detected and files are uploaded."""
        self.setup_s3_bucket(s3, create_s3_bucket)
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        # Create test file and manifest
        manifest = self._create_test_manifest(tmp_path)

        # First upload - populates S3 and cache
        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
            with HashCache(str(cache_dir)) as hash_cache:
                result1 = hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    hash_cache=hash_cache,
                )

        file_hash = result1.manifest.files[0].hash
        s3_key = f"{TEST_KEY_PREFIX}/{file_hash}.xxh128"

        # Verify file is in S3
        response = s3.list_objects_v2(Bucket=TEST_BUCKET, Prefix=TEST_KEY_PREFIX)
        assert any(obj["Key"] == s3_key for obj in response.get("Contents", []))

        # Delete the object from S3 (simulating lifecycle policy or manual deletion)
        s3.delete_object(Bucket=TEST_BUCKET, Key=s3_key)

        # Verify file is gone from S3
        response = s3.list_objects_v2(Bucket=TEST_BUCKET, Prefix=TEST_KEY_PREFIX)
        assert not any(obj["Key"] == s3_key for obj in response.get("Contents", []))

        # Second upload - should detect stale cache and re-upload
        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
            with HashCache(str(cache_dir)) as hash_cache:
                result2 = hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    hash_cache=hash_cache,
                )

        # Verify file is back in S3
        response = s3.list_objects_v2(Bucket=TEST_BUCKET, Prefix=TEST_KEY_PREFIX)
        assert any(obj["Key"] == s3_key for obj in response.get("Contents", []))

        # Verify hash is the same
        assert result2.manifest.files[0].hash == file_hash

    def test_skipped_items_reuploaded_after_late_invalidation(
        self, tmp_path: Path, s3, create_s3_bucket
    ) -> None:
        """
        Test that items skipped due to sampling are re-uploaded when cache is later invalidated.

        This tests the scenario where:
        1. First 100 items pass validation (objects exist in S3)
        2. Items 101+ are skipped due to 1% sampling (not verified)
        3. A later sampled item (item 104) fails validation (object missing from S3)
        4. All previously skipped items (101-103) are re-queued and uploaded
        """
        from unittest.mock import patch

        self.setup_s3_bucket(s3, create_s3_bucket)
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        # Create 105 test files - enough to get past the first 100 always-verify
        files = []
        for i in range(105):
            test_file = tmp_path / f"file{i:03d}.txt"
            test_file.write_text(f"Content for file {i}")
            file_stat = test_file.stat()
            files.append(
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            )

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=files,
            total_size=sum(f.size or 0 for f in files),
        )

        # First upload - populates S3 and cache
        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
            with HashCache(str(cache_dir)) as hash_cache:
                result1 = hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    hash_cache=hash_cache,
                )

        # Verify all files are in S3
        response = s3.list_objects_v2(Bucket=TEST_BUCKET, Prefix=TEST_KEY_PREFIX)
        s3_objects = {obj["Key"] for obj in response.get("Contents", [])}
        assert len(s3_objects) == 105

        # Delete only objects for files 100-104 (the ones past the first 100)
        # This simulates lifecycle policy deleting some objects
        for i in range(100, 105):
            s3_key = f"{TEST_KEY_PREFIX}/{result1.manifest.files[i].hash}.xxh128"
            s3.delete_object(Bucket=TEST_BUCKET, Key=s3_key)

        # Verify only 100 objects remain
        response = s3.list_objects_v2(Bucket=TEST_BUCKET, Prefix=TEST_KEY_PREFIX)
        assert len(response.get("Contents", [])) == 100

        # Mock should_verify to simulate sampling behavior:
        # - First 100: always verify (return True) - these pass (objects exist)
        # - Items 101-103: skip (return False) - not verified
        # - Item 104: verify (return True) - triggers invalidation (object missing)
        call_count = [0]

        def mock_should_verify(self):
            call_count[0] += 1
            if call_count[0] <= 100:
                return True  # First 100 always verified
            elif call_count[0] <= 103:
                return False  # Items 101-103 skipped
            else:
                return True  # Item 104+ verified (triggers invalidation)

        with patch.object(_S3CacheValidationState, "should_verify", mock_should_verify):
            with S3CheckCache(str(cache_dir)) as s3_cache:
                data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
                with HashCache(str(cache_dir)) as hash_cache:
                    result2 = hash_upload_abs_manifest(
                        manifest=manifest,
                        data_cache=data_cache,
                        hash_cache=hash_cache,
                    )

        # Verify all 105 files are back in S3 (including re-queued items 101-103)
        response = s3.list_objects_v2(Bucket=TEST_BUCKET, Prefix=TEST_KEY_PREFIX)
        s3_objects_after = {obj["Key"] for obj in response.get("Contents", [])}
        assert len(s3_objects_after) == 105

        # Verify all hashes match
        for i in range(105):
            assert result1.manifest.files[i].hash == result2.manifest.files[i].hash

    def test_late_invalidation_requeues_skipped_items(
        self, tmp_path: Path, s3, create_s3_bucket
    ) -> None:
        """
        Test that items skipped before invalidation are correctly re-queued.

        Uses mocking to force the scenario where:
        1. Items 1-3 are skipped (sampling says don't verify)
        2. Item 4 is verified and finds object missing
        3. Items 1-3 should be re-queued and uploaded
        """
        from unittest.mock import patch

        self.setup_s3_bucket(s3, create_s3_bucket)
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        # Create 5 test files
        files = []
        for i in range(5):
            test_file = tmp_path / f"file{i}.txt"
            test_file.write_text(f"Unique content {i}")
            file_stat = test_file.stat()
            files.append(
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            )

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=files,
            total_size=sum(f.size or 0 for f in files),
        )

        # First upload - populates S3 and cache
        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
            with HashCache(str(cache_dir)) as hash_cache:
                result1 = hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    hash_cache=hash_cache,
                )

        # Delete all objects from S3
        for f in result1.manifest.files:
            s3_key = f"{TEST_KEY_PREFIX}/{f.hash}.xxh128"
            s3.delete_object(Bucket=TEST_BUCKET, Key=s3_key)

        # Mock should_verify to:
        # - Return False for first 3 calls (items skipped)
        # - Return True for 4th call (triggers validation failure)
        # - Return True for remaining calls
        call_count = [0]

        def mock_should_verify(self):
            call_count[0] += 1
            # Skip first 3, verify 4th and onwards
            return call_count[0] > 3

        # Second upload with controlled sampling
        with patch.object(_S3CacheValidationState, "should_verify", mock_should_verify):
            with S3CheckCache(str(cache_dir)) as s3_cache:
                data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
                with HashCache(str(cache_dir)) as hash_cache:
                    result2 = hash_upload_abs_manifest(
                        manifest=manifest,
                        data_cache=data_cache,
                        hash_cache=hash_cache,
                    )

        # Verify all 5 files are in S3 (including the 3 that were initially skipped)
        response = s3.list_objects_v2(Bucket=TEST_BUCKET, Prefix=TEST_KEY_PREFIX)
        s3_objects = {obj["Key"] for obj in response.get("Contents", [])}
        assert len(s3_objects) == 5

        # Verify all hashes match
        for i in range(5):
            assert result1.manifest.files[i].hash == result2.manifest.files[i].hash

    def test_valid_cache_not_invalidated(self, tmp_path: Path, s3, create_s3_bucket) -> None:
        """Test that valid S3 check cache entries are not invalidated."""
        self.setup_s3_bucket(s3, create_s3_bucket)
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        manifest = self._create_test_manifest(tmp_path)

        # First upload
        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
            with HashCache(str(cache_dir)) as hash_cache:
                result1 = hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    hash_cache=hash_cache,
                )

        # Second upload - cache should be valid, file should be skipped
        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
            with HashCache(str(cache_dir)) as hash_cache:
                result2 = hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    hash_cache=hash_cache,
                )

        # Both should have same hash
        assert result1.manifest.files[0].hash == result2.manifest.files[0].hash

        # Second upload should have skipped the file (skipped_bytes should be > 0)
        assert result2.statistics.skipped_bytes > 0

    def test_multiple_files_stale_cache_all_reuploaded(
        self, tmp_path: Path, s3, create_s3_bucket
    ) -> None:
        """Test that when cache is invalidated, all skipped files are re-uploaded."""
        self.setup_s3_bucket(s3, create_s3_bucket)
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        # Create multiple test files
        files = []
        for i in range(3):
            test_file = tmp_path / f"test{i}.txt"
            test_file.write_text(f"Content {i}")
            file_stat = test_file.stat()
            files.append(
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            )

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=files,
            total_size=sum(f.size or 0 for f in files),
        )

        # First upload
        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
            with HashCache(str(cache_dir)) as hash_cache:
                result1 = hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    hash_cache=hash_cache,
                )

        # Delete ALL objects from S3
        response = s3.list_objects_v2(Bucket=TEST_BUCKET, Prefix=TEST_KEY_PREFIX)
        for obj in response.get("Contents", []):
            s3.delete_object(Bucket=TEST_BUCKET, Key=obj["Key"])

        # Verify S3 is empty
        response = s3.list_objects_v2(Bucket=TEST_BUCKET, Prefix=TEST_KEY_PREFIX)
        assert response.get("Contents") is None or len(response["Contents"]) == 0

        # Second upload - should detect stale cache and re-upload all files
        with S3CheckCache(str(cache_dir)) as s3_cache:
            data_cache = self._create_s3_data_cache(s3_check_cache=s3_cache)
            with HashCache(str(cache_dir)) as hash_cache:
                result2 = hash_upload_abs_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    hash_cache=hash_cache,
                )

        # Verify all files are back in S3
        response = s3.list_objects_v2(Bucket=TEST_BUCKET, Prefix=TEST_KEY_PREFIX)
        assert len(response.get("Contents", [])) == 3

        # Verify hashes match
        for i in range(3):
            assert result1.manifest.files[i].hash == result2.manifest.files[i].hash
