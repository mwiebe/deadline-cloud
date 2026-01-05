# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Unit tests for the HASH_UPLOAD manifest operation with FileSystemDataCache.

These tests verify that hash_upload_manifest correctly hashes files and writes
them to a local filesystem cache using content-addressable storage.

All tests use the unified manifest classes (AbsSnapshotManifest, AbsDiffManifest)
from manifest.py, following the Wave 2 refactoring pattern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Set

import pytest

from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
    hash_upload_manifest,
    FileSystemDataCache,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm, hash_file
from deadline.job_attachments.asset_manifests.versions import SymlinkPolicy
from deadline.job_attachments.asset_manifests.manifest import (
    AbsDiffManifest,
    AbsSnapshotManifest,
    ManifestFilePath,
)
from deadline.job_attachments.caches.hash_cache import HashCache


class TestHashUploadManifestFileSystem:
    """Tests for hash_upload_manifest with FileSystemDataCache."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        """Create a FileSystemDataCache for testing."""
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def _get_cache_files(self, cache_root: Path) -> Set[str]:
        """Get all file names in the cache directory."""
        return {f.name for f in cache_root.iterdir() if f.is_file()}

    def _get_cache_file_content(self, cache_root: Path, filename: str) -> bytes:
        """Get the content of a cached file."""
        return (cache_root / filename).read_bytes()

    def test_hash_upload_empty_manifest(self, tmp_path: Path) -> None:
        """Test hashing and uploading an empty manifest."""
        cache_root = tmp_path / "cache"

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            total_size=0,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsSnapshotManifest)
        assert len(result.files) == 0
        assert result.totalSize == 0
        # No files should be written
        assert len(self._get_cache_files(cache_root)) == 0

    def test_hash_upload_single_file(self, tmp_path: Path) -> None:
        """Test hashing and uploading a single file."""
        cache_root = tmp_path / "cache"

        # Create a test file
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello, World!")
        file_stat = test_file.stat()

        # Use absolute path in manifest (as required by HASH_UPLOAD)
        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",  # Empty hash to be filled
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsSnapshotManifest)
        assert len(result.files) == 1
        assert result.files[0].hash != ""  # Hash should be filled in
        assert result.files[0].path == abs_path

        # Verify the file was written to the cache
        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 1

        # Verify the filename format is correct (hash.algorithm)
        expected_filename = f"{result.files[0].hash}.xxh128"
        assert expected_filename in cache_files

        # Verify the cached content matches the original file
        cached_content = self._get_cache_file_content(cache_root, expected_filename)
        assert cached_content == test_file.read_bytes()

    def test_hash_upload_multiple_files(self, tmp_path: Path) -> None:
        """Test hashing and uploading multiple files."""
        cache_root = tmp_path / "cache"

        # Create test files
        file1 = tmp_path / "file1.txt"
        file1.write_text("Content of file 1")
        file2 = tmp_path / "file2.txt"
        file2.write_text("Content of file 2")
        file3 = tmp_path / "subdir" / "file3.txt"
        file3.parent.mkdir()
        file3.write_text("Content of file 3")

        # Collect the manifest (returns AbsSnapshotManifest)
        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # Filter out the cache directory from the manifest
        collected_files = [
            f for f in collected.files if "cache" not in f.path and f.symlink_target is None
        ]
        filtered_manifest = AbsSnapshotManifest(
            hash_alg=collected.hashAlg,
            files=collected_files,
            dirs=collected.dirs,
            total_size=sum(f.size or 0 for f in collected_files),
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=filtered_manifest,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsSnapshotManifest)
        # Filter to file entries only
        file_entries = [p for p in result.files if p.symlink_target is None and not p.deleted]
        assert len(file_entries) == 3

        # All files should have hashes
        for entry in file_entries:
            assert entry.hash is not None
            assert entry.hash != ""
            assert len(entry.hash) == 32  # XXH128 produces 32 hex chars

        # Verify all files were written to cache
        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 3

    def test_hash_upload_duplicate_content_written_once(self, tmp_path: Path) -> None:
        """Test that files with identical content are only written once."""
        cache_root = tmp_path / "cache"

        # Create files with identical content
        file1 = tmp_path / "file1.txt"
        file1.write_text("Same content")
        file2 = tmp_path / "file2.txt"
        file2.write_text("Same content")

        # Create manifest manually to avoid including cache dir
        file1_stat = file1.stat()
        file2_stat = file2.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(file1).replace("\\", "/"),
                    hash="",
                    size=int(file1_stat.st_size),
                    mtime=int(file1_stat.st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=str(file2).replace("\\", "/"),
                    hash="",
                    size=int(file2_stat.st_size),
                    mtime=int(file2_stat.st_mtime_ns // 1000),
                ),
            ],
            total_size=int(file1_stat.st_size) + int(file2_stat.st_size),
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        # Filter to file entries
        file_entries = [p for p in result.files if p.symlink_target is None]
        assert len(file_entries) == 2

        # Both files should have the same hash
        assert file_entries[0].hash == file_entries[1].hash

        # Only one file should be in cache (content-addressable storage)
        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 1

    def test_hash_upload_with_hash_cache(self, tmp_path: Path) -> None:
        """Test that hash cache is used and updated correctly."""
        cache_root = tmp_path / "cache"
        hash_cache_dir = tmp_path / "hash_cache"
        hash_cache_dir.mkdir()

        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content for caching")
        file_stat = test_file.stat()
        file_size = int(file_stat.st_size)

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=file_size,
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=file_size,
        )

        with HashCache(str(hash_cache_dir)) as hash_cache:
            data_cache = self._create_filesystem_data_cache(cache_root)
            result = hash_upload_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=hash_cache,
            )

            # Verify hash was computed and cached
            cache_key = str(test_file.resolve())
            cached_entry = hash_cache.get_entry(
                cache_key, HashAlgorithm.XXH128, range_start=0, range_end=file_size
            )
            assert cached_entry is not None
            assert cached_entry.file_hash == result.files[0].hash

    def test_hash_matches_direct_hash(self, tmp_path: Path) -> None:
        """Test that computed hash matches direct hash_file() result."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content for hash verification")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        # Compute hash directly
        expected_hash = hash_file(str(test_file), HashAlgorithm.XXH128)
        assert result.files[0].hash == expected_hash

    def test_preserves_metadata(self, tmp_path: Path) -> None:
        """Test that file metadata is preserved in the result manifest."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "test.txt"
        test_file.write_text("Content")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")
        original_size = int(file_stat.st_size)
        original_mtime = int(file_stat.st_mtime_ns // 1000)

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=original_size,
                    mtime=original_mtime,
                )
            ],
            total_size=original_size,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert result.files[0].size == original_size
        assert result.files[0].mtime == original_mtime
        assert result.files[0].path == abs_path

    def test_symlinks_pass_through_unchanged(self, tmp_path: Path) -> None:
        """Test that symlinks are passed through without writing to cache."""
        cache_root = tmp_path / "cache"

        # Create a target file and symlink
        target = tmp_path / "target.txt"
        target.write_text("Target content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        # Filter out cache directory
        filtered_files = [f for f in collected.files if "cache" not in f.path]
        filtered_manifest = AbsSnapshotManifest(
            hash_alg=collected.hashAlg,
            files=filtered_files,
            dirs=[d for d in collected.dirs if "cache" not in d.path],
            total_size=sum(f.size or 0 for f in filtered_files if f.size),
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=filtered_manifest,
            data_cache=data_cache,
        )

        # Find symlink and file entries
        symlink_entries = [p for p in result.files if p.symlink_target is not None]
        file_entries = [p for p in result.files if p.symlink_target is None]

        assert len(symlink_entries) == 1
        assert len(file_entries) == 1

        # Symlink should have no hash
        assert symlink_entries[0].hash is None
        # File should have hash
        assert file_entries[0].hash != ""

        # Only the target file should be written (not the symlink)
        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 1

    def test_deleted_entries_pass_through(self, tmp_path: Path) -> None:
        """Test that deleted entries in diff manifests pass through unchanged."""
        cache_root = tmp_path / "cache"

        # Create a diff manifest with a deleted entry
        manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path="/some/deleted/file.txt",
                    deleted=True,
                )
            ],
            total_size=0,
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert len(result.files) == 1
        assert result.files[0].deleted is True
        assert result.files[0].hash is None

        # No files written for deleted entries
        cache_files = self._get_cache_files(cache_root)
        assert len(cache_files) == 0

    @pytest.mark.parametrize("runnable", [True, False])
    def test_preserves_runnable_flag(self, tmp_path: Path, runnable: bool) -> None:
        """Test that runnable flag is preserved when set directly in manifest."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "script.sh"
        test_file.write_text("#!/bin/bash\necho hello")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                    runnable=runnable,
                )
            ],
            total_size=int(file_stat.st_size),
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert len(result.files) == 1
        assert result.files[0].runnable is runnable
        assert result.files[0].hash != ""

    def test_returns_abs_snapshot_manifest(self, tmp_path: Path) -> None:
        """Test that AbsSnapshotManifest input returns AbsSnapshotManifest."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        file_stat = test_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=str(test_file).replace("\\", "/"),
                    hash="",
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsSnapshotManifest)

    def test_returns_abs_diff_manifest(self, tmp_path: Path) -> None:
        """Test that AbsDiffManifest input returns AbsDiffManifest."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
            parent_manifest_hash="abc123",
        )

        data_cache = self._create_filesystem_data_cache(cache_root)
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        assert isinstance(result, AbsDiffManifest)
        assert result.parentManifestHash == "abc123"

    def test_skips_existing_files_in_cache(self, tmp_path: Path) -> None:
        """Test that files already in cache are not re-written."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        # First upload
        data_cache = self._create_filesystem_data_cache(cache_root)
        result1 = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        # Get the cached file's mtime
        cached_filename = f"{result1.files[0].hash}.xxh128"
        cached_file = cache_root / cached_filename
        original_mtime = cached_file.stat().st_mtime

        # Second upload - should skip since file exists
        result2 = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )

        # Both results should have the same hash
        assert result1.files[0].hash == result2.files[0].hash

        # File mtime should be unchanged (not re-written)
        assert cached_file.stat().st_mtime == original_mtime


class TestFileSystemDataCacheValidation:
    """Tests for FileSystemDataCache validation."""

    def test_rejects_relative_root_path(self) -> None:
        """Test that FileSystemDataCache rejects relative root_path."""
        with pytest.raises(ValueError, match="root_path must be absolute"):
            FileSystemDataCache(root_path=Path("relative/path"))

    def test_accepts_absolute_root_path(self, tmp_path: Path) -> None:
        """Test that FileSystemDataCache accepts absolute root_path."""
        cache = FileSystemDataCache(root_path=tmp_path)
        assert cache.root_path == tmp_path

    def test_object_exists_returns_false_for_missing(self, tmp_path: Path) -> None:
        """Test that object_exists returns False for non-existent files."""
        cache = FileSystemDataCache(root_path=tmp_path)
        assert cache.object_exists("nonexistent", "xxh128") is False

    def test_object_exists_returns_true_for_existing(self, tmp_path: Path) -> None:
        """Test that object_exists returns True for existing files."""
        cache = FileSystemDataCache(root_path=tmp_path)
        # Create a file in the cache
        (tmp_path / "somehash.xxh128").write_text("content")
        assert cache.object_exists("somehash", "xxh128") is True

    def test_get_object_key_format(self, tmp_path: Path) -> None:
        """Test that get_object_key returns correct path format."""
        cache = FileSystemDataCache(root_path=tmp_path)
        key = cache.get_object_key("abc123", "xxh128")
        expected = str(tmp_path / "abc123.xxh128")
        assert key == expected


class TestHashUploadFileSystemInputValidation:
    """Tests for input validation in hash_upload_manifest with FileSystemDataCache."""

    def test_rejects_relative_paths(self, tmp_path: Path) -> None:
        """Test that manifest with relative paths raises ValueError."""
        cache_root = tmp_path / "cache"
        cache_root.mkdir()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path="relative/path/file.txt",  # Relative path
                    hash="",
                    size=100,
                    mtime=12345,
                )
            ],
            total_size=100,
        )

        data_cache = FileSystemDataCache(root_path=cache_root)
        with pytest.raises(ValueError, match="requires absolute paths"):
            hash_upload_manifest(
                manifest=manifest,
                data_cache=data_cache,
            )

    def test_accepts_absolute_paths(self, tmp_path: Path) -> None:
        """Test that manifest with absolute paths is accepted."""
        cache_root = tmp_path / "cache"

        test_file = tmp_path / "test.txt"
        test_file.write_text("Content")
        file_stat = test_file.stat()

        abs_path = str(test_file).replace("\\", "/")

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
        )

        data_cache = FileSystemDataCache(root_path=cache_root)
        # Should not raise
        result = hash_upload_manifest(
            manifest=manifest,
            data_cache=data_cache,
        )
        assert result.files[0].hash != ""
