# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_manifest and related functions.

These tests cover:
- Basic file hashing for both v2023 and v2025 formats
- Hash cache integration (cache hits, cache misses, cache updates)
- Force rehash behavior
- Large file chunking (v2025 only)
- Symlink handling (v2025 only - symlinks pass through without hashing)
- Directory handling (v2025 only - directories pass through unchanged)
- Version-specific behavior differences
"""

import os
import pytest
from pathlib import Path
from typing import List
from unittest.mock import patch

from deadline.job_attachments.asset_manifests._operations import (
    hash_manifest,
    collect_manifest as collect_manifest_abs,
    subtree_manifest,
)
from deadline.job_attachments.asset_manifests._operations._hash_manifest import (
    _get_or_compute_hash,
    _hash_file_chunked,
)
from deadline.job_attachments.asset_manifests.versions import (
    ManifestType,
    ManifestVersion,
    SymlinkPolicy,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import (
    HashAlgorithm,
    hash_file,
)
from deadline.job_attachments.asset_manifests.base_manifest import FILE_CHUNK_SIZE_BYTES, BaseAssetManifest
from deadline.job_attachments.caches.hash_cache import HashCache, HashCacheEntry


def collect_manifest(
    *,
    root: Path,
    version: ManifestVersion,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE,
) -> BaseAssetManifest:
    """
    Helper function that collects a directory into a relative-path manifest
    using collect_manifest + subtree_manifest.

    Note: For v2025 with symlink preservation, use symlink_policy=PRESERVE.
    """
    # For v2025, default to PRESERVE to maintain symlink entries
    # For v2023, COLLAPSE is the only option that makes sense
    if version == ManifestVersion.v2025_12_04_beta and symlink_policy == SymlinkPolicy.COLLAPSE:
        # Use PRESERVE by default for v2025 to maintain symlink entries
        symlink_policy = SymlinkPolicy.PRESERVE

    abs_manifest = collect_manifest_abs(
        [root],
        [],
        version=version,
        symlink_policy=symlink_policy,
    )
    return subtree_manifest(abs_manifest, str(root))


class TestHashManifestV2023:
    """Tests for v2023-03-03 manifest hashing."""

    def test_hash_single_file(self, tmp_path: Path) -> None:
        """Hashes a single file correctly."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        # Collect first (hash="")
        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        assert collected.paths[0].hash == ""

        # Hash the manifest
        hashed = hash_manifest(collected, tmp_path)

        assert len(hashed.paths) == 1
        assert hashed.paths[0].hash != ""
        assert len(hashed.paths[0].hash) == 32  # XXH128 produces 32 hex chars

    def test_hash_matches_direct_hash(self, tmp_path: Path) -> None:
        """Hash matches direct hash_file() result."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content for hashing")

        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected, tmp_path)

        expected_hash = hash_file(str(test_file), HashAlgorithm.XXH128)
        assert hashed.paths[0].hash == expected_hash

    def test_hash_multiple_files(self, tmp_path: Path) -> None:
        """Hashes multiple files."""
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bbbbb")

        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected, tmp_path)

        assert len(hashed.paths) == 2
        for entry in hashed.paths:
            assert entry.hash != ""
            assert len(entry.hash) == 32

    def test_preserves_metadata(self, tmp_path: Path) -> None:
        """Preserves size and mtime from collected manifest."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected, tmp_path)

        assert hashed.paths[0].size == collected.paths[0].size
        assert hashed.paths[0].mtime == collected.paths[0].mtime
        assert hashed.paths[0].path == collected.paths[0].path

    def test_total_size_calculated(self, tmp_path: Path) -> None:
        """Total size is sum of all file sizes."""
        (tmp_path / "a.txt").write_text("aaa")  # 3 bytes
        (tmp_path / "b.txt").write_text("bbbbb")  # 5 bytes

        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected, tmp_path)

        assert hashed.totalSize == 8

    def test_manifest_version_preserved(self, tmp_path: Path) -> None:
        """Manifest version is preserved."""
        (tmp_path / "test.txt").write_text("test")

        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected, tmp_path)

        assert hashed.manifestVersion == ManifestVersion.v2023_03_03

    def test_hash_algorithm_preserved(self, tmp_path: Path) -> None:
        """Hash algorithm is preserved."""
        (tmp_path / "test.txt").write_text("test")

        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected, tmp_path)

        assert hashed.hashAlg == HashAlgorithm.XXH128


class TestHashManifestV2025:
    """Tests for v2025-12-04-beta manifest hashing."""

    def test_hash_single_file(self, tmp_path: Path) -> None:
        """Hashes a single file correctly."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        assert collected.paths[0].hash == ""

        hashed = hash_manifest(collected, tmp_path)

        assert len(hashed.paths) == 1
        assert hashed.paths[0].hash != ""
        assert len(hashed.paths[0].hash) == 32

    def test_preserves_runnable_flag(self, tmp_path: Path) -> None:
        """Preserves runnable flag from collected manifest."""
        test_file = tmp_path / "script.sh"
        test_file.write_text("#!/bin/bash")
        if os.name != "nt":
            test_file.chmod(0o755)

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        hashed = hash_manifest(collected, tmp_path)

        assert hashed.paths[0].runnable == collected.paths[0].runnable

    def test_symlinks_pass_through_unchanged(self, tmp_path: Path) -> None:
        """Symlinks are not hashed, just passed through."""
        target = tmp_path / "target.txt"
        target.write_text("target content")
        link = tmp_path / "link.txt"

        link.symlink_to("target.txt")

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        hashed = hash_manifest(collected, tmp_path)

        # Find the symlink entry
        symlink_entry = next(p for p in hashed.paths if p.path == "link.txt")
        assert symlink_entry.symlink_target == "target.txt"
        assert symlink_entry.hash is None

        # Target file should be hashed
        target_entry = next(p for p in hashed.paths if p.path == "target.txt")
        assert target_entry.hash is not None
        assert target_entry.symlink_target is None

    def test_directories_pass_through_unchanged(self, tmp_path: Path) -> None:
        """Directories are passed through unchanged."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        hashed = hash_manifest(collected, tmp_path)

        assert len(hashed.dirs) == 1
        assert hashed.dirs[0].path == "subdir"
        assert hashed.dirs[0].deleted is False

    def test_manifest_type_preserved(self, tmp_path: Path) -> None:
        """Manifest type is preserved."""
        (tmp_path / "test.txt").write_text("test")

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        hashed = hash_manifest(collected, tmp_path)

        assert hashed.manifestType == ManifestType.SNAPSHOT


class TestHashManifestWithCache:
    """Tests for hash cache integration."""

    def test_cache_miss_computes_hash(self, tmp_path: Path) -> None:
        """On cache miss, hash is computed and cached."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        with HashCache(str(cache_dir)) as hash_cache:
            hashed = hash_manifest(collected, tmp_path, hash_cache=hash_cache)

            # Verify hash was computed
            assert hashed.paths[0].hash != ""

            # Verify hash was cached
            cached_entry = hash_cache.get_entry("test.txt", HashAlgorithm.XXH128)
            assert cached_entry is not None
            assert cached_entry.file_hash == hashed.paths[0].hash

    def test_cache_hit_uses_cached_hash(self, tmp_path: Path) -> None:
        """On cache hit, cached hash is used without recomputing."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        mtime_str = str(collected.paths[0].mtime)

        with HashCache(str(cache_dir)) as hash_cache:
            # Pre-populate cache with a fake hash
            fake_hash = "a" * 32
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path="test.txt",
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash=fake_hash,
                    last_modified_time=mtime_str,
                )
            )

            hashed = hash_manifest(collected, tmp_path, hash_cache=hash_cache)

            # Should use cached hash, not compute new one
            assert hashed.paths[0].hash == fake_hash

    def test_cache_miss_on_mtime_change(self, tmp_path: Path) -> None:
        """Cache miss when mtime doesn't match."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        with HashCache(str(cache_dir)) as hash_cache:
            # Pre-populate cache with old mtime
            fake_hash = "a" * 32
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path="test.txt",
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash=fake_hash,
                    last_modified_time="0",  # Old mtime
                )
            )

            hashed = hash_manifest(collected, tmp_path, hash_cache=hash_cache)

            # Should compute new hash since mtime doesn't match
            assert hashed.paths[0].hash != fake_hash

    def test_force_rehash_ignores_cache(self, tmp_path: Path) -> None:
        """Force rehash ignores cache and recomputes."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        mtime_str = str(collected.paths[0].mtime)

        with HashCache(str(cache_dir)) as hash_cache:
            # Pre-populate cache with a fake hash
            fake_hash = "a" * 32
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path="test.txt",
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash=fake_hash,
                    last_modified_time=mtime_str,
                )
            )

            hashed = hash_manifest(collected, tmp_path, hash_cache=hash_cache, force_rehash=True)

            # Should compute new hash despite cache hit
            assert hashed.paths[0].hash != fake_hash

            # Cache should be updated with new hash
            cached_entry = hash_cache.get_entry("test.txt", HashAlgorithm.XXH128)
            assert cached_entry.file_hash == hashed.paths[0].hash

    def test_no_cache_always_computes(self, tmp_path: Path) -> None:
        """Without cache, hash is always computed."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected, tmp_path, hash_cache=None)

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
            rel_path="small.txt",
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
            rel_path="large.bin",
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
            rel_path="varied.bin",
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
            rel_path="repeated.bin",
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

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        with HashCache(str(cache_dir)) as hash_cache:
            # First call - computes and caches
            chunk_hashes_1 = _hash_file_chunked(
                file_path=test_file,
                rel_path="cached.bin",
                file_size=200,
                mtime=12345,
                hash_alg=HashAlgorithm.XXH128,
                chunk_size=chunk_size,
                hash_cache=hash_cache,
            )

            # Verify chunks were cached
            entry_0 = hash_cache.get_entry("cached.bin", HashAlgorithm.XXH128, 0, 100)
            entry_1 = hash_cache.get_entry("cached.bin", HashAlgorithm.XXH128, 100, 200)
            assert entry_0 is not None
            assert entry_1 is not None
            assert entry_0.file_hash == chunk_hashes_1[0]
            assert entry_1.file_hash == chunk_hashes_1[1]

            # Second call - should use cache
            chunk_hashes_2 = _hash_file_chunked(
                file_path=test_file,
                rel_path="cached.bin",
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

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        with HashCache(str(cache_dir)) as hash_cache:
            # Pre-populate cache with fake hash
            from deadline.job_attachments.caches.hash_cache import HashCacheEntry

            hash_cache.put_entry(
                HashCacheEntry(
                    file_path="force.bin",
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
                rel_path="force.bin",
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
    """Tests for large file chunking in v2025 format."""

    def test_large_file_uses_chunkhashes(self, tmp_path: Path) -> None:
        """Files larger than 256MB use chunkhashes instead of hash."""
        # We can't create a real 256MB+ file in tests, so we mock the size
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        # Manually set size to trigger chunking and set up valid input
        # For size = FILE_CHUNK_SIZE_BYTES + 1000, we need 2 chunks
        collected.paths[0].size = FILE_CHUNK_SIZE_BYTES + 1000
        collected.paths[0].hash = None  # Large files have hash=None
        collected.paths[0].chunkhashes = ["", ""]  # Placeholder for 2 chunks

        # Mock _hash_file_chunked to avoid creating huge file
        with patch(
            "deadline.job_attachments.asset_manifests._operations._hash_manifest._hash_file_chunked"
        ) as mock_chunk:
            mock_chunk.return_value = ["hash1", "hash2"]

            hashed = hash_manifest(collected, tmp_path)

            assert hashed.paths[0].chunkhashes == ["hash1", "hash2"]
            assert hashed.paths[0].hash is None
            mock_chunk.assert_called_once()

    def test_small_file_uses_single_hash(self, tmp_path: Path) -> None:
        """Files smaller than 256MB use single hash."""
        test_file = tmp_path / "small.txt"
        test_file.write_text("small content")

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        hashed = hash_manifest(collected, tmp_path)

        assert hashed.paths[0].hash is not None
        assert hashed.paths[0].chunkhashes is None


class TestInputValidation:
    """Tests for input validation in _hash_manifest_v2025."""

    def test_large_file_rejects_non_none_hash(self, tmp_path: Path) -> None:
        """Large file with hash set (not None) raises ValueError."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        # Set size to trigger large file path, but set hash (should be None)
        collected.paths[0].size = FILE_CHUNK_SIZE_BYTES + 1000
        collected.paths[0].hash = "somehash"
        collected.paths[0].chunkhashes = ["a", "b"]  # Correct count for 2 chunks

        with pytest.raises(ValueError, match="should have hash=None"):
            hash_manifest(collected, tmp_path)

    def test_large_file_rejects_wrong_chunk_count(self, tmp_path: Path) -> None:
        """Large file with wrong chunkhashes count raises ValueError."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        # Set size to require 2 chunks
        collected.paths[0].size = FILE_CHUNK_SIZE_BYTES + 1000
        collected.paths[0].hash = None
        collected.paths[0].chunkhashes = ["a"]  # Should be 2 chunks

        with pytest.raises(ValueError, match="should have 2 chunkhashes"):
            hash_manifest(collected, tmp_path)

    def test_large_file_rejects_none_chunkhashes(self, tmp_path: Path) -> None:
        """Large file with chunkhashes=None raises ValueError."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        collected.paths[0].size = FILE_CHUNK_SIZE_BYTES + 1000
        collected.paths[0].hash = None
        collected.paths[0].chunkhashes = None

        with pytest.raises(ValueError, match="should have 2 chunkhashes"):
            hash_manifest(collected, tmp_path)

    def test_large_file_valid_input_passes(self, tmp_path: Path) -> None:
        """Large file with valid input (hash=None, correct chunkhashes count) passes."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        # Set size to require exactly 3 chunks
        collected.paths[0].size = FILE_CHUNK_SIZE_BYTES * 2 + 1000
        collected.paths[0].hash = None
        collected.paths[0].chunkhashes = ["", "", ""]  # Correct count, empty placeholders

        with patch(
            "deadline.job_attachments.asset_manifests._operations._hash_manifest._hash_file_chunked"
        ) as mock_chunk:
            mock_chunk.return_value = ["hash1", "hash2", "hash3"]

            hashed = hash_manifest(collected, tmp_path)

            assert len(hashed.paths[0].chunkhashes) == 3

    def test_small_file_rejects_non_string_hash(self, tmp_path: Path) -> None:
        """Small file with hash=None raises ValueError."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        collected.paths[0].hash = None  # Should be a string

        with pytest.raises(ValueError, match="should have hash as a string"):
            hash_manifest(collected, tmp_path)

    def test_small_file_rejects_non_none_chunkhashes(self, tmp_path: Path) -> None:
        """Small file with chunkhashes set raises ValueError."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        collected.paths[0].hash = ""
        collected.paths[0].chunkhashes = ["a", "b"]  # Should be None

        with pytest.raises(ValueError, match="should have chunkhashes=None"):
            hash_manifest(collected, tmp_path)

    def test_small_file_valid_input_passes(self, tmp_path: Path) -> None:
        """Small file with valid input (hash is string, chunkhashes=None) passes."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        # Default from _collect_manifest should be valid
        assert isinstance(collected.paths[0].hash, str)
        assert collected.paths[0].chunkhashes is None

        hashed = hash_manifest(collected, tmp_path)

        assert isinstance(hashed.paths[0].hash, str)
        assert len(hashed.paths[0].hash) > 0
        assert hashed.paths[0].chunkhashes is None


class TestHashManifestDispatch:
    """Tests for the main _hash_manifest dispatch function."""

    def test_dispatch_to_v2023(self, tmp_path: Path) -> None:
        """Version v2023-03-03 dispatches to v2023 implementation."""
        (tmp_path / "test.txt").write_text("test")

        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected, tmp_path)

        assert hashed.manifestVersion == ManifestVersion.v2023_03_03

    def test_dispatch_to_v2025(self, tmp_path: Path) -> None:
        """Version v2025-12-04-beta dispatches to v2025 implementation."""
        (tmp_path / "test.txt").write_text("test")

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)
        hashed = hash_manifest(collected, tmp_path)

        assert hashed.manifestVersion == ManifestVersion.v2025_12_04_beta

    def test_unsupported_version_raises(self, tmp_path: Path) -> None:
        """Unsupported version raises ValueError."""
        (tmp_path / "test.txt").write_text("test")

        collected = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        # Manually change version to unsupported
        collected.manifestVersion = ManifestVersion.UNDEFINED

        with pytest.raises(ValueError, match="Unsupported manifest version"):
            hash_manifest(collected, tmp_path)


class TestGetOrComputeHash:
    """Tests for _get_or_compute_hash helper function."""

    def test_computes_hash_without_cache(self, tmp_path: Path) -> None:
        """Computes hash when no cache provided."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        result = _get_or_compute_hash(
            file_path=test_file,
            rel_path="test.txt",
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

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        with HashCache(str(cache_dir)) as hash_cache:
            fake_hash = "b" * 32
            hash_cache.put_entry(
                HashCacheEntry(
                    file_path="test.txt",
                    hash_algorithm=HashAlgorithm.XXH128,
                    file_hash=fake_hash,
                    last_modified_time="12345",
                )
            )

            result = _get_or_compute_hash(
                file_path=test_file,
                rel_path="test.txt",
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
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        messages: List[str] = []
        hash_manifest(collected, tmp_path, print_function_callback=messages.append)

        assert len(messages) == 2
        assert all("Hashed:" in msg for msg in messages)

    def test_callback_for_symlinks(self, tmp_path: Path) -> None:
        """Progress callback indicates symlinks are not hashed."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        link.symlink_to("target.txt")

        collected = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        messages: List[str] = []
        hash_manifest(collected, tmp_path, print_function_callback=messages.append)

        symlink_msg = [m for m in messages if "link.txt" in m][0]
        assert "Symlink" in symlink_msg or "no hash" in symlink_msg


class TestVersionDifferences:
    """Tests comparing behavior differences between v2023 and v2025."""

    def test_v2023_no_runnable_v2025_has_runnable(self, tmp_path: Path) -> None:
        """v2023 doesn't preserve runnable, v2025 does."""
        test_file = tmp_path / "script.sh"
        test_file.write_text("#!/bin/bash")
        if os.name != "nt":
            test_file.chmod(0o755)

        collected_v2023 = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        collected_v2025 = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        hashed_v2023 = hash_manifest(collected_v2023, tmp_path)
        hashed_v2025 = hash_manifest(collected_v2025, tmp_path)

        # v2023 doesn't track runnable
        assert hashed_v2023.paths[0].runnable is False
        # v2025 preserves runnable from collection
        if os.name != "nt":
            assert hashed_v2025.paths[0].runnable is True

    def test_both_versions_produce_same_hash(self, tmp_path: Path) -> None:
        """Both versions produce the same hash for the same file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("identical content")

        collected_v2023 = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        collected_v2025 = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        hashed_v2023 = hash_manifest(collected_v2023, tmp_path)
        hashed_v2025 = hash_manifest(collected_v2025, tmp_path)

        assert hashed_v2023.paths[0].hash == hashed_v2025.paths[0].hash
