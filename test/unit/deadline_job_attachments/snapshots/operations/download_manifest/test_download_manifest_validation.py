# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for download_manifest input validation.

These tests cover:
- Relative path validation (must be absolute)
- Snapshot manifest ignores apply_deletes
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deadline.job_attachments._snapshots import (
    download_manifest,
    FileSystemDataCache,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments._snapshots import (
    AbsSnapshotManifest,
    ManifestFilePath,
)


class TestDownloadManifestValidation:
    """Tests for input validation."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_relative_path_raises_error(self, tmp_path: Path) -> None:
        """Test that relative paths in manifest raise ValueError."""
        cache_root = tmp_path / "cache"

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path="relative/path/file.txt", hash="abc123", size=100, mtime=1234567890
                )
            ],
            dirs=[],
            total_size=100,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)

        with pytest.raises(ValueError, match="absolute paths"):
            download_manifest(manifest=manifest, data_cache=data_cache)

    def test_snapshot_manifest_ignores_apply_deletes(self, tmp_path: Path) -> None:
        """Test that apply_deletes has no effect on snapshot manifests."""
        cache_root = tmp_path / "cache"
        target_dir = tmp_path / "target"
        target_dir.mkdir()

        existing_file = target_dir / "existing.txt"
        existing_file.write_text("Existing content")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            dirs=[],
            total_size=0,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        download_manifest(manifest=manifest, data_cache=data_cache, apply_deletes=True)

        assert existing_file.exists()
