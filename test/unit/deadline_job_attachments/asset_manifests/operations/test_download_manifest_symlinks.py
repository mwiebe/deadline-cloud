# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for download_manifest symlink handling.

These tests cover:
- PRESERVE: Create symlinks as specified in the manifest
- EXCLUDE: Skip symlink entries entirely
- Invalid symlink policies raise ValueError
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
    hash_upload_manifest,
    download_manifest,
    join_manifest,
    subtree_manifest,
    FileSystemDataCache,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.versions import SymlinkPolicy
from deadline.job_attachments.asset_manifests.manifest import AbsSnapshotManifest


def _to_abs_snapshot(manifest: object) -> AbsSnapshotManifest:
    """Cast a manifest to AbsSnapshotManifest for type checking."""
    return cast(AbsSnapshotManifest, manifest)


class TestDownloadManifestSymlinks:
    """Tests for symlink handling during download."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_symlink_preserve(self, tmp_path: Path) -> None:
        """Test PRESERVE symlink policy creates symlinks."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        target_file = source_dir / "target.txt"
        target_file.write_text("Target content")
        link_file = source_dir / "link.txt"
        link_file.symlink_to(target_file)

        collected = collect_manifest([source_dir], [], symlink_policy=SymlinkPolicy.PRESERVE)
        data_cache = self._create_filesystem_data_cache(cache_root)
        hashed = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(hashed, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        download_manifest(
            manifest=download_manifest_obj,
            data_cache=data_cache,
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        downloaded_link = download_dir / "link.txt"
        assert downloaded_link.is_symlink()

    def test_symlink_exclude(self, tmp_path: Path) -> None:
        """Test EXCLUDE symlink policy skips symlinks."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        target_file = source_dir / "target.txt"
        target_file.write_text("Target content")
        link_file = source_dir / "link.txt"
        link_file.symlink_to(target_file)

        collected = collect_manifest([source_dir], [], symlink_policy=SymlinkPolicy.PRESERVE)
        data_cache = self._create_filesystem_data_cache(cache_root)
        hashed = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(hashed, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        download_manifest(
            manifest=download_manifest_obj,
            data_cache=data_cache,
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        downloaded_link = download_dir / "link.txt"
        assert not downloaded_link.exists()
        assert (download_dir / "target.txt").exists()

    def test_invalid_symlink_policy_raises(self, tmp_path: Path) -> None:
        """Test that unsupported symlink policies raise ValueError."""
        cache_root = tmp_path / "cache"

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            dirs=[],
            total_size=0,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)

        with pytest.raises(ValueError, match="PRESERVE or EXCLUDE"):
            download_manifest(
                manifest=manifest,
                data_cache=data_cache,
                symlink_policy=SymlinkPolicy.COLLAPSE,
            )
