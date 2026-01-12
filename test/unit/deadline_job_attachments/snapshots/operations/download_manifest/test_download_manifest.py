# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for download_manifest basic functionality.

These tests cover:
- Basic file downloading from FileSystemDataCache and S3DataCache
- Directory tree recreation
- Modification time restoration
- Round-trip testing (collect -> hash_upload -> download)
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List, Set, cast

import pytest

from deadline.job_attachments._snapshots import (
    collect_manifest,
    hash_upload_manifest,
    download_manifest,
    join_manifest,
    subtree_manifest,
    FileSystemDataCache,
    S3DataCache,
    SymlinkPolicy,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments._snapshots import (
    AbsSnapshot,
)


TEST_BUCKET = "test-download-bucket"
TEST_KEY_PREFIX = "Data"


def _to_abs_snapshot(manifest: object) -> AbsSnapshot:
    """Cast a manifest to AbsSnapshot for type checking."""
    return cast(AbsSnapshot, manifest)


def _compare_directory_trees(
    source_dir: Path,
    target_dir: Path,
    check_mtime: bool = True,
    mtime_tolerance_us: int = 1,
) -> List[str]:
    """
    Compare two directory trees and return a list of differences.

    Args:
        source_dir: The original source directory
        target_dir: The downloaded/restored directory
        check_mtime: Whether to compare modification times
        mtime_tolerance_us: Tolerance for mtime comparison in microseconds

    Returns:
        List of difference descriptions. Empty list means trees are identical.
    """
    differences: List[str] = []

    source_paths: Set[Path] = set()
    target_paths: Set[Path] = set()

    for path in source_dir.rglob("*"):
        rel_path = path.relative_to(source_dir)
        source_paths.add(rel_path)

    for path in target_dir.rglob("*"):
        rel_path = path.relative_to(target_dir)
        target_paths.add(rel_path)

    missing_in_target = source_paths - target_paths
    extra_in_target = target_paths - source_paths

    for p in missing_in_target:
        differences.append(f"Missing in target: {p}")
    for p in extra_in_target:
        differences.append(f"Extra in target: {p}")

    common_paths = source_paths & target_paths
    for rel_path in common_paths:
        source_path = source_dir / rel_path
        target_path = target_dir / rel_path

        if source_path.is_file() != target_path.is_file():
            differences.append(
                f"Type mismatch for {rel_path}: source is_file={source_path.is_file()}"
            )
            continue
        if source_path.is_dir() != target_path.is_dir():
            differences.append(
                f"Type mismatch for {rel_path}: source is_dir={source_path.is_dir()}"
            )
            continue
        if source_path.is_symlink() != target_path.is_symlink():
            differences.append(f"Symlink mismatch for {rel_path}")
            continue

        if source_path.is_symlink():
            # Compare symlink targets relative to their containing directories
            # For absolute symlinks, we need to check if they point to equivalent
            # locations within their respective directory trees
            source_target = Path(os.readlink(source_path))
            target_target = Path(os.readlink(target_path))

            # If both are relative, compare directly
            if not source_target.is_absolute() and not target_target.is_absolute():
                if source_target != target_target:
                    differences.append(
                        f"Symlink target mismatch for {rel_path}: "
                        f"{source_target} vs {target_target}"
                    )
            # If both are absolute, check if they point to equivalent relative locations
            elif source_target.is_absolute() and target_target.is_absolute():
                try:
                    source_rel = source_target.relative_to(source_dir)
                    target_rel = target_target.relative_to(target_dir)
                    if source_rel != target_rel:
                        differences.append(
                            f"Symlink target mismatch for {rel_path}: {source_rel} vs {target_rel}"
                        )
                except ValueError:
                    # Target is outside the directory tree - compare raw values
                    if source_target != target_target:
                        differences.append(
                            f"Symlink target mismatch for {rel_path}: "
                            f"{source_target} vs {target_target}"
                        )
            else:
                # One absolute, one relative - that's a mismatch
                differences.append(
                    f"Symlink target type mismatch for {rel_path}: "
                    f"{source_target} vs {target_target}"
                )
        elif source_path.is_file():
            if source_path.read_bytes() != target_path.read_bytes():
                differences.append(f"Content mismatch for {rel_path}")

            source_stat = source_path.stat()
            target_stat = target_path.stat()
            if source_stat.st_size != target_stat.st_size:
                differences.append(
                    f"Size mismatch for {rel_path}: {source_stat.st_size} vs {target_stat.st_size}"
                )

            if check_mtime:
                source_mtime_us = int(source_stat.st_mtime_ns // 1000)
                target_mtime_us = int(target_stat.st_mtime_ns // 1000)
                if abs(source_mtime_us - target_mtime_us) > mtime_tolerance_us:
                    differences.append(
                        f"Mtime mismatch for {rel_path}: {source_mtime_us} vs {target_mtime_us}"
                    )

    return differences


class TestDownloadManifestFileSystem:
    """Tests for download_manifest with FileSystemDataCache."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_download_empty_manifest(self, tmp_path: Path) -> None:
        """Test downloading an empty manifest."""
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            dirs=[],
            total_size=0,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = download_manifest(manifest=manifest, data_cache=data_cache)

        assert result.statistics.processed_files == 0
        assert result.statistics.total_bytes == 0

    def test_download_single_file(self, tmp_path: Path) -> None:
        """Test downloading a single file."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        test_file = source_dir / "test.txt"
        test_file.write_text("Hello, World!")

        collected = collect_manifest([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        upload_result = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(upload_result.manifest, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        result = download_manifest(manifest=download_manifest_obj, data_cache=data_cache)

        assert result.statistics.processed_files == 1
        differences = _compare_directory_trees(source_dir, download_dir)
        assert differences == [], f"Directory trees differ: {differences}"

    def test_download_multiple_files(self, tmp_path: Path) -> None:
        """Test downloading multiple files with subdirectories."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        (source_dir / "file1.txt").write_text("Content 1")
        (source_dir / "file2.txt").write_text("Content 2")
        subdir = source_dir / "subdir"
        subdir.mkdir()
        (subdir / "file3.txt").write_text("Content 3")

        collected = collect_manifest([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        upload_result = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(upload_result.manifest, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        result = download_manifest(manifest=download_manifest_obj, data_cache=data_cache)

        assert result.statistics.processed_files == 3
        differences = _compare_directory_trees(source_dir, download_dir)
        assert differences == [], f"Directory trees differ: {differences}"

    def test_download_preserves_mtime(self, tmp_path: Path) -> None:
        """Test that downloaded files have correct modification times."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        test_file = source_dir / "test.txt"
        test_file.write_text("Test content")
        old_mtime = time.time() - 3600
        os.utime(test_file, (old_mtime, old_mtime))

        collected = collect_manifest([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        upload_result = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(upload_result.manifest, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        download_manifest(manifest=download_manifest_obj, data_cache=data_cache)

        differences = _compare_directory_trees(source_dir, download_dir)
        assert differences == [], f"Directory trees differ: {differences}"

    def test_round_trip_directory_tree(self, tmp_path: Path) -> None:
        """Test full round-trip: collect -> upload -> download matches original."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        (source_dir / "file1.txt").write_text("File 1 content")
        (source_dir / "file2.bin").write_bytes(b"\x00\x01\x02\x03")
        subdir1 = source_dir / "subdir1"
        subdir1.mkdir()
        (subdir1 / "nested.txt").write_text("Nested content")
        subdir2 = source_dir / "subdir1" / "subdir2"
        subdir2.mkdir()
        (subdir2 / "deep.txt").write_text("Deep content")

        collected = collect_manifest([source_dir], [], symlink_policy=SymlinkPolicy.COLLAPSE_ALL)
        data_cache = self._create_filesystem_data_cache(cache_root)
        upload_result = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(upload_result.manifest, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        download_manifest(manifest=download_manifest_obj, data_cache=data_cache)

        differences = _compare_directory_trees(source_dir, download_dir)
        assert differences == [], f"Directory trees differ: {differences}"


class TestDownloadManifestS3:
    """Tests for download_manifest with S3DataCache."""

    @pytest.fixture(autouse=True)
    def setup_s3_bucket(self, s3, create_s3_bucket) -> None:
        """Create the test S3 bucket before each test."""
        create_s3_bucket(TEST_BUCKET)
        self.s3_client = s3

    def _create_s3_data_cache(self) -> S3DataCache:
        return S3DataCache(
            s3_bucket=TEST_BUCKET,
            s3_key_prefix=TEST_KEY_PREFIX,
            s3_client=self.s3_client,
        )

    def test_download_single_file_s3(self, tmp_path: Path) -> None:
        """Test downloading a single file from S3."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        test_file = source_dir / "test.txt"
        test_file.write_text("Hello from S3!")

        collected = collect_manifest([source_dir], [])
        data_cache = self._create_s3_data_cache()
        upload_result = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(upload_result.manifest, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        result = download_manifest(manifest=download_manifest_obj, data_cache=data_cache)

        assert result.statistics.processed_files == 1
        differences = _compare_directory_trees(source_dir, download_dir)
        assert differences == [], f"Directory trees differ: {differences}"

    def test_round_trip_s3(self, tmp_path: Path) -> None:
        """Test full round-trip with S3: collect -> upload -> download."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        (source_dir / "file1.txt").write_text("Content 1")
        (source_dir / "file2.bin").write_bytes(b"\x00\x01\x02\x03\x04")
        subdir = source_dir / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("Nested content")

        collected = collect_manifest([source_dir], [], symlink_policy=SymlinkPolicy.COLLAPSE_ALL)
        data_cache = self._create_s3_data_cache()
        upload_result = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(upload_result.manifest, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        download_manifest(manifest=download_manifest_obj, data_cache=data_cache)

        differences = _compare_directory_trees(source_dir, download_dir)
        assert differences == [], f"Directory trees differ: {differences}"
