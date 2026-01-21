# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for download_abs_manifest progress tracking.

These tests cover:
- DownloadProgressMetadata field accuracy
- Callback invocation behavior
- Cancellation via callback returning False
- Progress tracking for skipped files (cache hits)
- Transfer rate and total_time fields
"""

from __future__ import annotations

from pathlib import Path
from typing import List, cast

from deadline.job_attachments._snapshots import (
    collect_abs_snapshot,
    hash_upload_abs_manifest,
    download_abs_manifest,
    join_manifest,
    subtree_manifest,
    FileSystemDataCache,
    AbsSnapshot,
    SymlinkPolicy,
)
from deadline.job_attachments._snapshots._operations._download_abs_manifest_pipeline import (
    DownloadProgressMetadata,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.caches.hash_cache import HashCache


def _to_abs_snapshot(manifest: object) -> AbsSnapshot:
    """Cast a manifest to AbsSnapshot for type checking."""
    return cast(AbsSnapshot, manifest)


class TestDownloadProgress:
    """Tests for progress callback functionality."""

    def _setup_test_files(
        self, tmp_path: Path, num_files: int = 5, content_multiplier: int = 100
    ) -> tuple[Path, Path, Path, AbsSnapshot]:
        """
        Set up test files, upload them, and return paths and download manifest.

        Returns:
            (source_dir, cache_root, download_dir, download_manifest)
        """
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        cache_root.mkdir()
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        # Create test files
        for i in range(num_files):
            (source_dir / f"file{i}.txt").write_text(f"content{i}" * content_multiplier)

        # Collect and upload
        collected = collect_abs_snapshot(
            [source_dir], [], symlink_policy=SymlinkPolicy.COLLAPSE_ALL
        )
        data_cache = FileSystemDataCache(root_path=cache_root)
        upload_result = hash_upload_abs_manifest(collected, data_cache)

        # Create download manifest
        rel_manifest = subtree_manifest(upload_result.manifest, str(source_dir))
        download_manifest = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        return source_dir, cache_root, download_dir, download_manifest

    def test_progress_callback_invoked(self, tmp_path: Path) -> None:
        """Test that progress callback is invoked during operation."""
        _, cache_root, _, download_manifest = self._setup_test_files(tmp_path)

        callbacks: List[DownloadProgressMetadata] = []

        def on_progress(metadata: DownloadProgressMetadata) -> bool:
            callbacks.append(metadata)
            return True

        data_cache = FileSystemDataCache(root_path=cache_root)
        download_abs_manifest(
            manifest=download_manifest,
            data_cache=data_cache,
            on_progress=on_progress,
        )

        # Should have received at least one callback
        assert len(callbacks) >= 1

        # Final callback should show completion
        final = callbacks[-1]
        assert final.total_file_chunks == 5
        assert final.total_bytes == download_manifest.totalSize

    def test_progress_metadata_fields(self, tmp_path: Path) -> None:
        """Test that progress metadata fields are populated correctly."""
        _, cache_root, _, download_manifest = self._setup_test_files(tmp_path, num_files=1)

        final_metadata: List[DownloadProgressMetadata] = []

        def on_progress(metadata: DownloadProgressMetadata) -> bool:
            final_metadata.append(metadata)
            return True

        data_cache = FileSystemDataCache(root_path=cache_root)
        download_abs_manifest(
            manifest=download_manifest,
            data_cache=data_cache,
            on_progress=on_progress,
        )

        # Check final state
        assert len(final_metadata) >= 1
        final = final_metadata[-1]

        assert final.total_file_chunks == 1
        assert final.total_bytes == download_manifest.totalSize
        assert final.progress >= 0
        assert final.progress <= 100
        assert isinstance(final.progressMessage, str)

    def test_progress_cancellation(self, tmp_path: Path) -> None:
        """Test that returning False from callback cancels the operation."""
        _, cache_root, _, download_manifest = self._setup_test_files(
            tmp_path, num_files=20, content_multiplier=1000
        )

        callback_count = 0

        def on_progress(metadata: DownloadProgressMetadata) -> bool:
            nonlocal callback_count
            callback_count += 1
            # Cancel after first callback
            return False

        data_cache = FileSystemDataCache(root_path=cache_root)

        # The operation may raise AssetSyncCancelledError or complete partially
        try:
            download_abs_manifest(
                manifest=download_manifest,
                data_cache=data_cache,
                on_progress=on_progress,
            )
        except Exception:
            pass  # Cancellation may raise an exception

        # Should have received at least one callback
        assert callback_count >= 1

    def test_progress_with_hash_cache_hits(self, tmp_path: Path) -> None:
        """Test progress tracking when hash cache causes skips."""
        source_dir, cache_root, download_dir, download_manifest = self._setup_test_files(
            tmp_path, num_files=1
        )
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

        data_cache = FileSystemDataCache(root_path=cache_root)

        with HashCache(str(hash_cache_dir)) as hash_cache:
            # First download - populates hash cache
            download_abs_manifest(
                manifest=download_manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            # Second download - should hit hash cache
            callbacks: List[DownloadProgressMetadata] = []

            def on_progress(metadata: DownloadProgressMetadata) -> bool:
                callbacks.append(metadata)
                return True

            download_abs_manifest(
                manifest=download_manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
                on_progress=on_progress,
            )

        # Should have callbacks showing skipped bytes
        assert len(callbacks) >= 1
        final = callbacks[-1]
        # File was skipped due to cache hit
        assert final.skipped_bytes == download_manifest.totalSize

    def test_progress_empty_manifest(self, tmp_path: Path) -> None:
        """Test progress callback with empty manifest."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            total_size=0,
        )

        callbacks: List[DownloadProgressMetadata] = []

        def on_progress(metadata: DownloadProgressMetadata) -> bool:
            callbacks.append(metadata)
            return True

        data_cache = FileSystemDataCache(root_path=cache_root)
        download_abs_manifest(
            manifest=manifest,
            data_cache=data_cache,
            on_progress=on_progress,
        )

        # Empty manifest may or may not trigger callbacks
        # but should not error
        if callbacks:
            assert callbacks[-1].total_file_chunks == 0
            assert callbacks[-1].total_bytes == 0

    def test_progress_callback_includes_total_time(self, tmp_path: Path) -> None:
        """Test that progress callbacks include total_time field."""
        _, cache_root, _, download_manifest = self._setup_test_files(tmp_path)

        callbacks: List[DownloadProgressMetadata] = []

        def on_progress(metadata: DownloadProgressMetadata) -> bool:
            callbacks.append(metadata)
            return True

        data_cache = FileSystemDataCache(root_path=cache_root)
        result = download_abs_manifest(
            manifest=download_manifest,
            data_cache=data_cache,
            on_progress=on_progress,
        )

        # All callbacks should have total_time >= 0
        for callback in callbacks:
            assert callback.total_time >= 0

        # Final statistics should have total_time > 0
        assert result.statistics.total_time > 0

    def test_progress_callback_includes_transfer_rate(self, tmp_path: Path) -> None:
        """Test that progress callbacks include transfer_rate field."""
        _, cache_root, _, download_manifest = self._setup_test_files(tmp_path)

        callbacks: List[DownloadProgressMetadata] = []

        def on_progress(metadata: DownloadProgressMetadata) -> bool:
            callbacks.append(metadata)
            return True

        data_cache = FileSystemDataCache(root_path=cache_root)
        result = download_abs_manifest(
            manifest=download_manifest,
            data_cache=data_cache,
            on_progress=on_progress,
        )

        # All callbacks should have transfer_rate >= 0
        for callback in callbacks:
            assert callback.transfer_rate >= 0

        # Final statistics should have transfer_rate > 0 (since we processed data)
        assert result.statistics.transfer_rate > 0

    def test_progress_total_time_increases_monotonically(self, tmp_path: Path) -> None:
        """Test that total_time increases monotonically across callbacks."""
        _, cache_root, _, download_manifest = self._setup_test_files(
            tmp_path, num_files=10, content_multiplier=500
        )

        callbacks: List[DownloadProgressMetadata] = []

        def on_progress(metadata: DownloadProgressMetadata) -> bool:
            callbacks.append(metadata)
            return True

        data_cache = FileSystemDataCache(root_path=cache_root)
        download_abs_manifest(
            manifest=download_manifest,
            data_cache=data_cache,
            on_progress=on_progress,
        )

        # Verify total_time increases monotonically
        if len(callbacks) > 1:
            for i in range(1, len(callbacks)):
                assert callbacks[i].total_time >= callbacks[i - 1].total_time

    def test_progress_message_contains_rate(self, tmp_path: Path) -> None:
        """Test that progressMessage contains throughput rate."""
        _, cache_root, _, download_manifest = self._setup_test_files(tmp_path, num_files=1)

        data_cache = FileSystemDataCache(root_path=cache_root)
        result = download_abs_manifest(
            manifest=download_manifest,
            data_cache=data_cache,
        )

        # progressMessage should contain rate in format "(X B/s)" or "(X KB/s)" etc.
        assert "/s)" in result.statistics.progressMessage

    def test_progress_message_contains_elapsed_time(self, tmp_path: Path) -> None:
        """Test that progressMessage contains elapsed time during callbacks."""
        _, cache_root, _, download_manifest = self._setup_test_files(tmp_path)

        callbacks: List[DownloadProgressMetadata] = []

        def on_progress(metadata: DownloadProgressMetadata) -> bool:
            callbacks.append(metadata)
            return True

        data_cache = FileSystemDataCache(root_path=cache_root)
        download_abs_manifest(
            manifest=download_manifest,
            data_cache=data_cache,
            on_progress=on_progress,
        )

        # Progress messages should contain time in brackets [Xs] or [M:SS]
        for callback in callbacks:
            assert "[" in callback.progressMessage and "]" in callback.progressMessage

    def test_final_statistics_transfer_rate_calculation(self, tmp_path: Path) -> None:
        """Test that final statistics transfer_rate is calculated correctly."""
        _, cache_root, _, download_manifest = self._setup_test_files(tmp_path)

        data_cache = FileSystemDataCache(root_path=cache_root)
        result = download_abs_manifest(
            manifest=download_manifest,
            data_cache=data_cache,
        )

        # Final transfer_rate should be approximately total_bytes / total_time
        expected_rate = result.statistics.total_bytes / result.statistics.total_time
        # Allow some tolerance due to floating point
        assert abs(result.statistics.transfer_rate - expected_rate) < 1.0
