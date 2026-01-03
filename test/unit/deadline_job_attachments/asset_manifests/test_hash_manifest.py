# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_manifest and related functions.

These tests cover:
- Basic file hashing
- Hash cache integration (cache hits, cache misses, cache updates)
- Force rehash behavior
- Large file chunking
- Symlink handling (symlinks pass through without hashing)
- Directory handling (directories pass through unchanged)
- Validation that input manifest has absolute paths
- Diff manifest handling (deleted entries pass through)
"""

import os
import pytest
from pathlib import Path
from typing import List
from unittest.mock import patch

from deadline.job_attachments.asset_manifests._operations import (
    hash_manifest,
    collect_manifest,
)
from deadline.job_attachments.asset_manifests._operations._hash_manifest import (
    _get_or_compute_hash,
    _hash_file_chunked,
)
from deadline.job_attachments.asset_manifests.versions import (
    SymlinkPolicy,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import (
    HashAlgorithm,
    hash_file,
)
from deadline.job_attachments.asset_manifests.manifest import (
    FILE_CHUNK_SIZE_BYTES,
    AbsDiffManifest,
    AbsSnapshotManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
)
from deadline.job_attachments.caches.hash_cache import HashCache, HashCacheEntry


class TestHashManifestBasic:
    """Tests for basic manifest hashing."""

    def test_hash_single_file(self, tmp_path: Path) -> None:
        """Hashes a single file correctly."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        # Collect first (hash="")
        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        assert collected.paths[0].hash == ""

        # Hash the manifest
        hashed = hash_manifest(collected)

        assert len(hashed.paths) == 1
        assert hashed.paths[0].hash is not None
        assert hashed.paths[0].hash != ""
        assert len(hashed.paths[0].hash) == 32  # XXH128 produces 32 hex chars

    def test_hash_matches_direct_hash(self, tmp_path: Path) -> None:
        """Hash matches direct hash_file() result."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content for hashing")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected)

        expected_hash = hash_file(str(test_file), HashAlgorithm.XXH128)
        assert hashed.paths[0].hash == expected_hash

    def test_hash_multiple_files(self, tmp_path: Path) -> None:
        """Hashes multiple files."""
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bbbbb")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected)

        assert len(hashed.paths) == 2
        for entry in hashed.paths:
            assert entry.hash is not None
            assert entry.hash != ""
            assert len(entry.hash) == 32

    def test_preserves_metadata(self, tmp_path: Path) -> None:
        """Preserves size and mtime from collected manifest."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected)

        assert hashed.paths[0].size == collected.paths[0].size
        assert hashed.paths[0].mtime == collected.paths[0].mtime
        assert hashed.paths[0].path == collected.paths[0].path

    def test_total_size_calculated(self, tmp_path: Path) -> None:
        """Total size is sum of all file sizes."""
        (tmp_path / "a.txt").write_text("aaa")  # 3 bytes
        (tmp_path / "b.txt").write_text("bbbbb")  # 5 bytes

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected)

        assert hashed.totalSize == 8

    def test_hash_algorithm_preserved(self, tmp_path: Path) -> None:
        """Hash algorithm is preserved."""
        (tmp_path / "test.txt").write_text("test")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected)

        assert hashed.hashAlg == HashAlgorithm.XXH128

    def test_preserves_runnable_flag(self, tmp_path: Path) -> None:
        """Preserves runnable flag from collected manifest."""
        test_file = tmp_path / "script.sh"
        test_file.write_text("#!/bin/bash")
        if os.name != "nt":
            test_file.chmod(0o755)

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        hashed = hash_manifest(collected)

        assert hashed.paths[0].runnable == collected.paths[0].runnable

    def test_symlinks_pass_through_unchanged(self, tmp_path: Path) -> None:
        """Symlinks are not hashed, just passed through."""
        target = tmp_path / "target.txt"
        target.write_text("target content")
        link = tmp_path / "link.txt"

        link.symlink_to("target.txt")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        hashed = hash_manifest(collected)

        # Find the symlink entry (paths are absolute)
        link_path = str(link).replace("\\", "/")
        target_path = str(target).replace("\\", "/")
        symlink_entry = next(p for p in hashed.paths if p.path == link_path)
        assert symlink_entry.symlink_target is not None
        assert symlink_entry.hash is None

        # Target file should be hashed
        target_entry = next(p for p in hashed.paths if p.path == target_path)
        assert target_entry.hash is not None
        assert target_entry.symlink_target is None

    def test_directories_pass_through_unchanged(self, tmp_path: Path) -> None:
        """Directories are passed through unchanged."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        hashed = hash_manifest(collected)

        # collect_manifest includes both the root and subdirectories
        assert len(hashed.dirs) >= 1
        # Find the subdir entry (paths are absolute)
        subdir_path = str(subdir).replace("\\", "/")
        subdir_entries = [d for d in hashed.dirs if d.path == subdir_path]
        assert len(subdir_entries) == 1
        assert subdir_entries[0].deleted is False

    def test_returns_abs_snapshot_manifest(self, tmp_path: Path) -> None:
        """Hashing AbsSnapshotManifest returns AbsSnapshotManifest."""
        (tmp_path / "test.txt").write_text("test")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        hashed = hash_manifest(collected)

        assert isinstance(hashed, AbsSnapshotManifest)


class TestHashManifestWithCache:
    """Tests for hash cache integration."""

    def test_cache_miss_computes_hash(self, tmp_path: Path) -> None:
        """On cache miss, hash is computed and cached."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        # Cache key is the resolved path
        cache_key = str(Path(collected.paths[0].path).resolve())

        with HashCache(str(cache_dir)) as hash_cache:
            hashed = hash_manifest(collected, hash_cache=hash_cache)

            # Verify hash was computed
            assert hashed.paths[0].hash != ""

            # Verify hash was cached (using resolved path as cache key)
            cached_entry = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128)
            assert cached_entry is not None
            assert cached_entry.file_hash == hashed.paths[0].hash

    def test_cache_hit_uses_cached_hash(self, tmp_path: Path) -> None:
        """On cache hit, cached hash is used without recomputing."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        mtime_str = str(collected.paths[0].mtime)
        # Cache key is the resolved path
        cache_key = str(Path(collected.paths[0].path).resolve())

        with HashCache(str(cache_dir)) as hash_cache:
            # Pre-populate cache with a fake hash (using resolved path as key)
            fake_hash = "a" * 32
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path=cache_key,
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash=fake_hash,
                    last_modified_time=mtime_str,
                )
            )

            hashed = hash_manifest(collected, hash_cache=hash_cache)

            # Should use cached hash, not compute new one
            assert hashed.paths[0].hash == fake_hash

    def test_cache_miss_on_mtime_change(self, tmp_path: Path) -> None:
        """Cache miss when mtime doesn't match."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        # Cache key is the resolved path
        cache_key = str(Path(collected.paths[0].path).resolve())

        with HashCache(str(cache_dir)) as hash_cache:
            # Pre-populate cache with old mtime
            fake_hash = "a" * 32
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path=cache_key,
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash=fake_hash,
                    last_modified_time="0",  # Old mtime
                )
            )

            hashed = hash_manifest(collected, hash_cache=hash_cache)

            # Should compute new hash since mtime doesn't match
            assert hashed.paths[0].hash != fake_hash

    def test_force_rehash_ignores_cache(self, tmp_path: Path) -> None:
        """Force rehash ignores cache and recomputes."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        mtime_str = str(collected.paths[0].mtime)
        # Cache key is the resolved path
        cache_key = str(Path(collected.paths[0].path).resolve())

        with HashCache(str(cache_dir)) as hash_cache:
            # Pre-populate cache with a fake hash
            fake_hash = "a" * 32
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path=cache_key,
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash=fake_hash,
                    last_modified_time=mtime_str,
                )
            )

            hashed = hash_manifest(collected, hash_cache=hash_cache, force_rehash=True)

            # Should compute new hash despite cache hit
            assert hashed.paths[0].hash != fake_hash

            # Cache should be updated with new hash
            cached_entry = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128)
            assert cached_entry.file_hash == hashed.paths[0].hash

    def test_no_cache_always_computes(self, tmp_path: Path) -> None:
        """Without cache, hash is always computed."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected, hash_cache=None)

        expected_hash = hash_file(str(test_file), HashAlgorithm.XXH128)
        assert hashed.paths[0].hash == expected_hash


class TestHashFileChunked:
    """Tests for _hash_file_chunked function."""

    def test_single_chunk_for_small_file(self, tmp_path: Path) -> None:
        """Small file produces single chunk hash."""
        test_file = tmp_path / "small.txt"
        test_file.write_text("small content")
        file_size = test_file.stat().st_size

        chunk_hashes = _hash_file_chunked(
            file_path=test_file,
            cache_key=str(test_file),
            file_size=file_size,
            mtime=12345,
            hash_alg=HashAlgorithm.XXH128,
            chunk_size=1024,
        )

        assert len(chunk_hashes) == 1
        assert len(chunk_hashes[0]) == 32

    def test_multiple_chunks_for_large_file(self, tmp_path: Path) -> None:
        """Large file produces multiple chunk hashes."""
        test_file = tmp_path / "large.bin"
        # Create file larger than chunk size (use small chunk for testing)
        chunk_size = 100
        content = b"x" * 350  # 3.5 chunks worth
        test_file.write_bytes(content)

        chunk_hashes = _hash_file_chunked(
            file_path=test_file,
            cache_key=str(test_file),
            file_size=350,
            mtime=12345,
            hash_alg=HashAlgorithm.XXH128,
            chunk_size=chunk_size,
        )

        assert len(chunk_hashes) == 4  # ceil(350/100) = 4

    def test_chunk_hashes_are_different(self, tmp_path: Path) -> None:
        """Different chunks produce different hashes."""
        test_file = tmp_path / "varied.bin"
        # Create file with different content in each chunk
        chunk_size = 100
        content = b"a" * 100 + b"b" * 100 + b"c" * 100
        test_file.write_bytes(content)

        chunk_hashes = _hash_file_chunked(
            file_path=test_file,
            cache_key=str(test_file),
            file_size=300,
            mtime=12345,
            hash_alg=HashAlgorithm.XXH128,
            chunk_size=chunk_size,
        )

        assert len(chunk_hashes) == 3
        # All hashes should be different
        assert len(set(chunk_hashes)) == 3

    def test_identical_chunks_same_hash(self, tmp_path: Path) -> None:
        """Identical chunks produce same hash."""
        test_file = tmp_path / "repeated.bin"
        chunk_size = 100
        content = b"x" * 100 + b"x" * 100  # Two identical chunks
        test_file.write_bytes(content)

        chunk_hashes = _hash_file_chunked(
            file_path=test_file,
            cache_key=str(test_file),
            file_size=200,
            mtime=12345,
            hash_alg=HashAlgorithm.XXH128,
            chunk_size=chunk_size,
        )

        assert len(chunk_hashes) == 2
        assert chunk_hashes[0] == chunk_hashes[1]

    def test_uses_cache_for_chunks(self, tmp_path: Path) -> None:
        """Chunk hashes are cached and retrieved from cache."""
        test_file = tmp_path / "cached.bin"
        chunk_size = 100
        content = b"a" * 100 + b"b" * 100
        test_file.write_bytes(content)
        cache_key = str(test_file)

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        with HashCache(str(cache_dir)) as hash_cache:
            # First call - computes and caches
            chunk_hashes_1 = _hash_file_chunked(
                file_path=test_file,
                cache_key=cache_key,
                file_size=200,
                mtime=12345,
                hash_alg=HashAlgorithm.XXH128,
                chunk_size=chunk_size,
                hash_cache=hash_cache,
            )

            # Verify chunks were cached
            entry_0 = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128, 0, 100)
            entry_1 = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128, 100, 200)
            assert entry_0 is not None
            assert entry_1 is not None
            assert entry_0.file_hash == chunk_hashes_1[0]
            assert entry_1.file_hash == chunk_hashes_1[1]

            # Second call - should use cache
            chunk_hashes_2 = _hash_file_chunked(
                file_path=test_file,
                cache_key=cache_key,
                file_size=200,
                mtime=12345,
                hash_alg=HashAlgorithm.XXH128,
                chunk_size=chunk_size,
                hash_cache=hash_cache,
            )

            assert chunk_hashes_1 == chunk_hashes_2

    def test_force_rehash_ignores_chunk_cache(self, tmp_path: Path) -> None:
        """Force rehash recomputes chunk hashes even with cache."""
        test_file = tmp_path / "force.bin"
        chunk_size = 100
        content = b"x" * 100
        test_file.write_bytes(content)
        cache_key = str(test_file)

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        with HashCache(str(cache_dir)) as hash_cache:
            # Pre-populate cache with fake hash
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path=cache_key,
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash="fakehash" + "0" * 24,
                    last_modified_time="12345",
                    range_start=0,
                    range_end=100,
                )
            )

            # With force_rehash, should compute real hash
            chunk_hashes = _hash_file_chunked(
                file_path=test_file,
                cache_key=cache_key,
                file_size=100,
                mtime=12345,
                hash_alg=HashAlgorithm.XXH128,
                chunk_size=chunk_size,
                hash_cache=hash_cache,
                force_rehash=True,
            )

            assert chunk_hashes[0] != "fakehash" + "0" * 24
            assert len(chunk_hashes[0]) == 32


class TestLargeFileChunking:
    """Tests for large file chunking."""

    def test_large_file_uses_chunkhashes(self, tmp_path: Path) -> None:
        """Files larger than 256MB use chunkhashes instead of hash."""
        # We can't create a real 256MB+ file in tests, so we mock the size
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)
        abs_path = str(test_file).replace("\\", "/")
        stat_info = test_file.stat()

        # Create manifest with large file size manually
        # For size = FILE_CHUNK_SIZE_BYTES + 1000, we need 2 chunks
        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path=abs_path,
                    chunkhashes=["", ""],  # Placeholder for 2 chunks
                    size=FILE_CHUNK_SIZE_BYTES + 1000,
                    mtime=int(stat_info.st_mtime_ns // 1000),
                )
            ],
            total_size=FILE_CHUNK_SIZE_BYTES + 1000,
        )

        # Mock _hash_file_chunked to avoid creating huge file
        with patch(
            "deadline.job_attachments.asset_manifests._operations._hash_manifest._hash_file_chunked"
        ) as mock_chunk:
            mock_chunk.return_value = ["hash1", "hash2"]

            hashed = hash_manifest(manifest)

            assert hashed.paths[0].chunkhashes == ["hash1", "hash2"]
            assert hashed.paths[0].hash is None
            mock_chunk.assert_called_once()

    def test_small_file_uses_single_hash(self, tmp_path: Path) -> None:
        """Files smaller than 256MB use single hash."""
        test_file = tmp_path / "small.txt"
        test_file.write_text("small content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        hashed = hash_manifest(collected)

        assert hashed.paths[0].hash is not None
        assert hashed.paths[0].chunkhashes is None


class TestInputValidation:
    """Tests for input validation in hash_manifest."""

    def test_rejects_relative_paths(self, tmp_path: Path) -> None:
        """Manifest with relative paths raises ValueError."""
        # Create a manifest with relative paths manually
        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            paths=[
                ManifestFilePath(
                    path="/absolute/path/file.txt",  # Start with absolute
                    hash="",
                    size=100,
                    mtime=12345,
                )
            ],
            total_size=100,
        )
        # Manually override path to relative (bypassing validation)
        manifest.paths[0].path = "relative/path/file.txt"

        with pytest.raises(ValueError, match="requires absolute paths"):
            hash_manifest(manifest)

    def test_accepts_absolute_paths(self, tmp_path: Path) -> None:
        """Manifest with absolute paths is accepted."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # Should not raise
        hashed = hash_manifest(collected)
        assert hashed.paths[0].hash != ""

    def test_large_file_rejects_non_none_hash(self, tmp_path: Path) -> None:
        """Large file with hash set (not None) raises ValueError."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)
        abs_path = str(test_file).replace("\\", "/")
        stat_info = test_file.stat()

        # Create manifest with large file that has hash set (invalid)
        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path=abs_path,
                    chunkhashes=["a", "b"],  # Correct count for 2 chunks
                    size=FILE_CHUNK_SIZE_BYTES + 1000,
                    mtime=int(stat_info.st_mtime_ns // 1000),
                )
            ],
            total_size=FILE_CHUNK_SIZE_BYTES + 1000,
        )
        # Manually set hash (invalid for large file)
        manifest.paths[0].hash = "somehash"

        with pytest.raises(ValueError, match="should have hash=None"):
            hash_manifest(manifest)

    def test_large_file_rejects_wrong_chunk_count(self, tmp_path: Path) -> None:
        """Large file with wrong chunkhashes count raises ValueError."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)
        abs_path = str(test_file).replace("\\", "/")
        stat_info = test_file.stat()

        # Create manifest with valid chunk count first
        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path=abs_path,
                    chunkhashes=["a", "b"],  # Correct count for 2 chunks
                    size=FILE_CHUNK_SIZE_BYTES + 1000,
                    mtime=int(stat_info.st_mtime_ns // 1000),
                )
            ],
            total_size=FILE_CHUNK_SIZE_BYTES + 1000,
        )
        # Manually set wrong chunk count (bypassing validation)
        manifest.paths[0].chunkhashes = ["a"]  # Should be 2 chunks

        with pytest.raises(ValueError, match="should have 2 chunkhashes"):
            hash_manifest(manifest)

    def test_large_file_rejects_none_chunkhashes(self, tmp_path: Path) -> None:
        """Large file with chunkhashes=None raises ValueError."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)
        abs_path = str(test_file).replace("\\", "/")
        stat_info = test_file.stat()

        # Create manifest - need to bypass validation
        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path=abs_path,
                    chunkhashes=["", ""],  # Valid initially
                    size=FILE_CHUNK_SIZE_BYTES + 1000,
                    mtime=int(stat_info.st_mtime_ns // 1000),
                )
            ],
            total_size=FILE_CHUNK_SIZE_BYTES + 1000,
        )
        # Manually set to None (invalid)
        manifest.paths[0].chunkhashes = None

        with pytest.raises(ValueError, match="should have 2 chunkhashes"):
            hash_manifest(manifest)

    def test_large_file_valid_input_passes(self, tmp_path: Path) -> None:
        """Large file with valid input (hash=None, correct chunkhashes count) passes."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)
        abs_path = str(test_file).replace("\\", "/")
        stat_info = test_file.stat()

        # Create manifest with valid large file
        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path=abs_path,
                    chunkhashes=["", "", ""],  # Correct count for 3 chunks
                    size=FILE_CHUNK_SIZE_BYTES * 2 + 1000,
                    mtime=int(stat_info.st_mtime_ns // 1000),
                )
            ],
            total_size=FILE_CHUNK_SIZE_BYTES * 2 + 1000,
        )

        with patch(
            "deadline.job_attachments.asset_manifests._operations._hash_manifest._hash_file_chunked"
        ) as mock_chunk:
            mock_chunk.return_value = ["hash1", "hash2", "hash3"]

            hashed = hash_manifest(manifest)

            assert hashed.paths[0].chunkhashes is not None
            assert len(hashed.paths[0].chunkhashes) == 3

    def test_small_file_rejects_non_string_hash(self, tmp_path: Path) -> None:
        """Small file with hash=None raises ValueError."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        # Manually set hash to None (invalid for small file)
        collected.paths[0].hash = None

        with pytest.raises(ValueError, match="should have hash as a string"):
            hash_manifest(collected)

    def test_small_file_rejects_non_none_chunkhashes(self, tmp_path: Path) -> None:
        """Small file with chunkhashes set raises ValueError."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        # Manually set chunkhashes (invalid for small file)
        collected.paths[0].chunkhashes = ["a", "b"]

        with pytest.raises(ValueError, match="should have chunkhashes=None"):
            hash_manifest(collected)

    def test_small_file_valid_input_passes(self, tmp_path: Path) -> None:
        """Small file with valid input (hash is string, chunkhashes=None) passes."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        # Default from collect_manifest should be valid
        assert isinstance(collected.paths[0].hash, str)
        assert collected.paths[0].chunkhashes is None

        hashed = hash_manifest(collected)

        assert isinstance(hashed.paths[0].hash, str)
        assert len(hashed.paths[0].hash) > 0
        assert hashed.paths[0].chunkhashes is None


class TestGetOrComputeHash:
    """Tests for _get_or_compute_hash helper function."""

    def test_computes_hash_without_cache(self, tmp_path: Path) -> None:
        """Computes hash when no cache provided."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        result = _get_or_compute_hash(
            file_path=test_file,
            cache_key=str(test_file),
            mtime=12345,
            hash_alg=HashAlgorithm.XXH128,
            hash_cache=None,
            force_rehash=False,
        )

        expected = hash_file(str(test_file), HashAlgorithm.XXH128)
        assert result == expected

    def test_uses_cache_on_hit(self, tmp_path: Path) -> None:
        """Uses cached hash on cache hit."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        cache_key = str(test_file)

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        with HashCache(str(cache_dir)) as hash_cache:
            fake_hash = "b" * 32
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path=cache_key,
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash=fake_hash,
                    last_modified_time="12345",
                )
            )

            result = _get_or_compute_hash(
                file_path=test_file,
                cache_key=cache_key,
                mtime=12345,
                hash_alg=HashAlgorithm.XXH128,
                hash_cache=hash_cache,
                force_rehash=False,
            )

            assert result == fake_hash


class TestProgressCallback:
    """Tests for progress callback functionality."""

    def test_callback_called_for_each_file(self, tmp_path: Path) -> None:
        """Progress callback is called for each file."""
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bbb")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        messages: List[str] = []
        hash_manifest(collected, print_function_callback=messages.append)

        assert len(messages) == 2
        assert all("Hashed:" in msg for msg in messages)

    def test_callback_for_symlinks(self, tmp_path: Path) -> None:
        """Progress callback indicates symlinks are not hashed."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        link.symlink_to("target.txt")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        messages: List[str] = []
        hash_manifest(collected, print_function_callback=messages.append)

        symlink_msg = [m for m in messages if "link.txt" in m][0]
        assert "Symlink" in symlink_msg or "no hash" in symlink_msg


class TestHashDiffManifest:
    """Tests for hashing diff manifests."""

    def test_diff_manifest_hashes_new_files(self, tmp_path: Path) -> None:
        """Diff manifest with new files gets hashes computed."""
        # Create a test file
        test_file = tmp_path / "new_file.txt"
        test_file.write_text("new content")
        file_stat = test_file.stat()
        abs_path = str(test_file).replace("\\", "/")

        # Create a diff manifest with a new file (hash="")
        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",  # Empty hash to be filled
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
            parent_manifest_hash="parent123",
        )

        hashed = hash_manifest(diff_manifest)

        # Verify hash was computed
        assert hashed.paths[0].hash is not None
        assert hashed.paths[0].hash != ""
        assert len(hashed.paths[0].hash) == 32
        # Verify manifest type is preserved
        assert isinstance(hashed, AbsDiffManifest)
        # Verify parent hash is preserved
        assert hashed.parentManifestHash == "parent123"

    def test_diff_manifest_preserves_deleted_entries(self, tmp_path: Path) -> None:
        """Diff manifest deleted entries are passed through unchanged."""
        # Create a diff manifest with a deleted file entry
        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path="/some/deleted/file.txt",
                    deleted=True,
                )
            ],
            total_size=0,
            parent_manifest_hash="parent456",
        )

        hashed = hash_manifest(diff_manifest)

        # Verify deleted entry is preserved
        assert len(hashed.paths) == 1
        assert hashed.paths[0].deleted is True
        assert hashed.paths[0].path == "/some/deleted/file.txt"
        # Verify manifest type is preserved
        assert isinstance(hashed, AbsDiffManifest)

    def test_diff_manifest_preserves_deleted_directories(self, tmp_path: Path) -> None:
        """Diff manifest deleted directory entries are passed through unchanged."""
        # Create a diff manifest with a deleted directory entry
        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(
                    path="/some/deleted/dir",
                    deleted=True,
                )
            ],
            paths=[],
            total_size=0,
        )

        hashed = hash_manifest(diff_manifest)

        # Verify deleted directory is preserved
        assert len(hashed.dirs) == 1
        assert hashed.dirs[0].deleted is True
        assert hashed.dirs[0].path == "/some/deleted/dir"
        # Verify manifest type is preserved
        assert isinstance(hashed, AbsDiffManifest)

    def test_diff_manifest_mixed_entries(self, tmp_path: Path) -> None:
        """Diff manifest with new, modified, and deleted entries."""
        # Create test files
        new_file = tmp_path / "new.txt"
        new_file.write_text("new content")
        new_stat = new_file.stat()
        new_path = str(new_file).replace("\\", "/")

        modified_file = tmp_path / "modified.txt"
        modified_file.write_text("modified content")
        mod_stat = modified_file.stat()
        mod_path = str(modified_file).replace("\\", "/")

        # Create a diff manifest with mixed entries
        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="/new/dir", deleted=False),
                ManifestDirectoryPath(path="/deleted/dir", deleted=True),
            ],
            paths=[
                # New file
                ManifestFilePath(
                    path=new_path,
                    hash="",
                    size=int(new_stat.st_size),
                    mtime=int(new_stat.st_mtime_ns // 1000),
                ),
                # Modified file
                ManifestFilePath(
                    path=mod_path,
                    hash="",
                    size=int(mod_stat.st_size),
                    mtime=int(mod_stat.st_mtime_ns // 1000),
                ),
                # Deleted file
                ManifestFilePath(
                    path="/old/deleted.txt",
                    deleted=True,
                ),
            ],
            total_size=int(new_stat.st_size) + int(mod_stat.st_size),
            parent_manifest_hash="parent789",
        )

        hashed = hash_manifest(diff_manifest)

        # Verify manifest type preserved
        assert isinstance(hashed, AbsDiffManifest)
        assert hashed.parentManifestHash == "parent789"

        # Verify directories
        assert len(hashed.dirs) == 2
        new_dir = next(d for d in hashed.dirs if d.path == "/new/dir")
        deleted_dir = next(d for d in hashed.dirs if d.path == "/deleted/dir")
        assert new_dir.deleted is False
        assert deleted_dir.deleted is True

        # Verify files
        assert len(hashed.paths) == 3

        # New file should be hashed
        new_entry = next(p for p in hashed.paths if p.path == new_path)
        assert new_entry.hash != ""
        assert new_entry.deleted is False

        # Modified file should be hashed
        mod_entry = next(p for p in hashed.paths if p.path == mod_path)
        assert mod_entry.hash != ""
        assert mod_entry.deleted is False

        # Deleted file should be unchanged
        del_entry = next(p for p in hashed.paths if p.path == "/old/deleted.txt")
        assert del_entry.deleted is True

    def test_diff_manifest_with_symlinks(self, tmp_path: Path) -> None:
        """Diff manifest with symlink entries passes them through unchanged."""
        # Create a diff manifest with a symlink entry
        # Note: In absolute-path manifests, symlink targets must also be absolute
        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path="/some/new/link.txt",
                    symlink_target="/some/absolute/target.txt",
                )
            ],
            total_size=0,
        )

        hashed = hash_manifest(diff_manifest)

        # Verify symlink is preserved with absolute target
        assert len(hashed.paths) == 1
        assert hashed.paths[0].symlink_target == "/some/absolute/target.txt"
        assert hashed.paths[0].hash is None
        # Verify manifest type is preserved
        assert isinstance(hashed, AbsDiffManifest)

    def test_snapshot_manifest_type_preserved(self, tmp_path: Path) -> None:
        """Snapshot manifest type is preserved after hashing."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        # collect_manifest creates AbsSnapshotManifest
        assert isinstance(collected, AbsSnapshotManifest)

        hashed = hash_manifest(collected)

        # Verify snapshot type is preserved
        assert isinstance(hashed, AbsSnapshotManifest)

    def test_diff_manifest_parent_hash_none_preserved(self, tmp_path: Path) -> None:
        """Diff manifest with no parent hash preserves None."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")
        file_stat = test_file.stat()
        abs_path = str(test_file).replace("\\", "/")

        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
            parent_manifest_hash=None,  # No parent hash
        )

        hashed = hash_manifest(diff_manifest)

        assert isinstance(hashed, AbsDiffManifest)
        assert hashed.parentManifestHash is None


class TestManifestTypePreservation:
    """Tests verifying that manifest types are preserved through hashing."""

    def test_abs_snapshot_returns_abs_snapshot(self, tmp_path: Path) -> None:
        """AbsSnapshotManifest input returns AbsSnapshotManifest output."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")
        stat_info = test_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=stat_info.st_size,
                    mtime=int(stat_info.st_mtime_ns // 1000),
                )
            ],
            total_size=stat_info.st_size,
        )

        hashed = hash_manifest(manifest)

        assert isinstance(hashed, AbsSnapshotManifest)

    def test_abs_diff_returns_abs_diff(self, tmp_path: Path) -> None:
        """AbsDiffManifest input returns AbsDiffManifest output."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")
        stat_info = test_file.stat()

        manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path=abs_path,
                    hash="",
                    size=stat_info.st_size,
                    mtime=int(stat_info.st_mtime_ns // 1000),
                )
            ],
            total_size=stat_info.st_size,
            parent_manifest_hash="parent123",
        )

        hashed = hash_manifest(manifest)

        assert isinstance(hashed, AbsDiffManifest)
        assert hashed.parentManifestHash == "parent123"
