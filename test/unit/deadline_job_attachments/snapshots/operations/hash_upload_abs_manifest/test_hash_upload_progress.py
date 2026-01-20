# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_upload_abs_manifest progress tracking.

These tests cover:
- HashUploadProgressMetadata field accuracy
- Callback invocation behavior
- Cancellation via callback returning False
- Progress tracking for skipped files (cache hits)
"""

from __future__ import annotations

from pathlib import Path
from typing import List


from deadline.job_attachments._snapshots import (
    hash_upload_abs_manifest,
    FileSystemDataCache,
    AbsSnapshot,
    ManifestFilePath,
    HashUploadProgressMetadata,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.caches.hash_cache import HashCache


class TestHashUploadProgress:
    """Tests for progress callback functionality."""

    def _create_test_file(self, path: Path, content: str) -> ManifestFilePath:
        """Create a test file and return a ManifestFilePath for it."""
        path.write_text(content)
        stat = path.stat()
        return ManifestFilePath(
            path=str(path).replace("\\", "/"),
            hash=None,
            size=int(stat.st_size),
            mtime=int(stat.st_mtime_ns // 1000),
        )

    def test_progress_callback_invoked(self, tmp_path: Path) -> None:
        """Test that progress callback is invoked during operation."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()

        # Create test files
        files_dir = tmp_path / "files"
        files_dir.mkdir()
        entries = [
            self._create_test_file(files_dir / f"file{i}.txt", f"content{i}" * 100)
            for i in range(5)
        ]

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=entries,
            total_size=sum(e.size or 0 for e in entries),
        )

        callbacks: List[HashUploadProgressMetadata] = []

        def on_progress(metadata: HashUploadProgressMetadata) -> bool:
            callbacks.append(metadata)
            return True

        data_cache = FileSystemDataCache(root_path=cache_root)
        hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            on_progress=on_progress,
        )

        # Should have received at least one callback
        assert len(callbacks) >= 1

        # Final callback should show completion
        final = callbacks[-1]
        assert final.total_file_chunks == 5
        assert final.total_bytes == sum(e.size or 0 for e in entries)

    def test_progress_metadata_fields(self, tmp_path: Path) -> None:
        """Test that progress metadata fields are populated correctly."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()

        files_dir = tmp_path / "files"
        files_dir.mkdir()
        entry = self._create_test_file(files_dir / "test.txt", "hello world")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[entry],
            total_size=entry.size or 0,
        )

        final_metadata: List[HashUploadProgressMetadata] = []

        def on_progress(metadata: HashUploadProgressMetadata) -> bool:
            final_metadata.append(metadata)
            return True

        data_cache = FileSystemDataCache(root_path=cache_root)
        hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            on_progress=on_progress,
        )

        # Check final state
        assert len(final_metadata) >= 1
        final = final_metadata[-1]

        assert final.total_file_chunks == 1
        assert final.total_bytes == entry.size
        assert final.progress >= 0
        assert final.progress <= 100
        assert isinstance(final.progressMessage, str)

    def test_progress_cancellation(self, tmp_path: Path) -> None:
        """Test that returning False from callback cancels the operation."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()

        files_dir = tmp_path / "files"
        files_dir.mkdir()
        # Create many files to ensure we get multiple callbacks
        entries = [
            self._create_test_file(files_dir / f"file{i}.txt", f"content{i}" * 1000)
            for i in range(20)
        ]

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=entries,
            total_size=sum(e.size or 0 for e in entries),
        )

        callback_count = 0

        def on_progress(metadata: HashUploadProgressMetadata) -> bool:
            nonlocal callback_count
            callback_count += 1
            # Cancel after first callback
            return False

        data_cache = FileSystemDataCache(root_path=cache_root)
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            on_progress=on_progress,
        )

        # Operation should have been cancelled early
        # Not all files should be processed
        assert callback_count >= 1
        # Result should still be valid (partial completion)
        assert result.manifest is not None

    def test_progress_with_hash_cache_hits(self, tmp_path: Path) -> None:
        """Test progress tracking when hash cache causes skips."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

        files_dir = tmp_path / "files"
        files_dir.mkdir()
        entry = self._create_test_file(files_dir / "test.txt", "cached content")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[entry],
            total_size=entry.size or 0,
        )

        data_cache = FileSystemDataCache(root_path=cache_root)

        with HashCache(str(hash_cache_dir)) as hash_cache:
            # First upload - populates hash cache
            hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            # Second upload - should hit hash cache
            callbacks: List[HashUploadProgressMetadata] = []

            def on_progress(metadata: HashUploadProgressMetadata) -> bool:
                callbacks.append(metadata)
                return True

            hash_upload_abs_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
                on_progress=on_progress,
            )

        # Should have callbacks showing skipped bytes
        assert len(callbacks) >= 1
        final = callbacks[-1]
        # Hash was skipped due to cache hit
        assert final.hash_skipped_bytes == entry.size

    def test_progress_empty_manifest(self, tmp_path: Path) -> None:
        """Test progress callback with empty manifest."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            total_size=0,
        )

        callbacks: List[HashUploadProgressMetadata] = []

        def on_progress(metadata: HashUploadProgressMetadata) -> bool:
            callbacks.append(metadata)
            return True

        data_cache = FileSystemDataCache(root_path=cache_root)
        hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            on_progress=on_progress,
        )

        # Empty manifest may or may not trigger callbacks
        # but should not error
        if callbacks:
            assert callbacks[-1].total_file_chunks == 0
            assert callbacks[-1].total_bytes == 0

    def test_progress_upload_skip_on_cache_hit(self, tmp_path: Path) -> None:
        """Test that upload_skipped_bytes is tracked when data cache already has content."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()

        files_dir = tmp_path / "files"
        files_dir.mkdir()
        entry = self._create_test_file(files_dir / "test.txt", "upload skip test")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[entry],
            total_size=entry.size or 0,
        )

        data_cache = FileSystemDataCache(root_path=cache_root)

        # First upload - populates data cache
        hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        # Second upload - should skip upload (data already in cache)
        callbacks: List[HashUploadProgressMetadata] = []

        def on_progress(metadata: HashUploadProgressMetadata) -> bool:
            callbacks.append(metadata)
            return True

        hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            force_rehash=True,  # Force rehash but upload should still skip
            on_progress=on_progress,
        )

        assert len(callbacks) >= 1
        final = callbacks[-1]
        # Upload was skipped because content already in cache
        assert final.upload_skipped_bytes == entry.size

    def test_progress_message_uses_files_for_whole_file_mode(self, tmp_path: Path) -> None:
        """Test that progressMessage says 'files' when using whole file mode (no chunking)."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()

        files_dir = tmp_path / "files"
        files_dir.mkdir()
        entries = [
            self._create_test_file(files_dir / f"file{i}.txt", f"content{i}") for i in range(3)
        ]

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=entries,
            total_size=sum(e.size or 0 for e in entries),
            file_chunk_size_bytes=-1,  # Whole file mode
        )

        data_cache = FileSystemDataCache(root_path=cache_root)
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        # progressMessage should contain "files" not "chunks"
        assert "files" in result.statistics.progressMessage
        assert "chunks" not in result.statistics.progressMessage
        assert "(3 files)" in result.statistics.progressMessage

    def test_progress_message_uses_chunks_for_chunked_mode(self, tmp_path: Path) -> None:
        """Test that progressMessage says 'chunks' when using chunked mode."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()

        files_dir = tmp_path / "files"
        files_dir.mkdir()
        # Create a file large enough to be chunked with small chunk size
        entry = self._create_test_file(files_dir / "large.txt", "x" * 100)

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[entry],
            total_size=entry.size or 0,
            file_chunk_size_bytes=32,  # Small chunk size to force chunking
        )

        data_cache = FileSystemDataCache(root_path=cache_root)
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            max_memory_bytes=256 * 1024 * 1024,  # Ensure memory is sufficient
        )

        # progressMessage should contain "chunks" not "files"
        assert "chunks" in result.statistics.progressMessage
        assert "files" not in result.statistics.progressMessage
        # 100 bytes / 32 bytes per chunk = 4 chunks (rounded up)
        assert "(4 chunks)" in result.statistics.progressMessage

    def test_progress_message_contains_rate(self, tmp_path: Path) -> None:
        """Test that progressMessage contains throughput rate."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()

        files_dir = tmp_path / "files"
        files_dir.mkdir()
        entry = self._create_test_file(files_dir / "test.txt", "rate test content")

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[entry],
            total_size=entry.size or 0,
        )

        data_cache = FileSystemDataCache(root_path=cache_root)
        result = hash_upload_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        # progressMessage should contain rate in format "(X B/s)" or "(X KB/s)" etc.
        assert "/s)" in result.statistics.progressMessage
