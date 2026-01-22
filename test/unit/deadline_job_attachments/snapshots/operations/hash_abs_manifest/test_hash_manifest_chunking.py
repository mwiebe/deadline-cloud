# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_abs_manifest chunking behavior.

These tests cover:
- _hash_file_chunked function
- Large file chunking (files > chunk size)
- Small file single hash
- Chunk cache integration
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from deadline.job_attachments._snapshots import (
    hash_abs_manifest,
    collect_abs_snapshot,
    SymlinkPolicy,
    DEFAULT_FILE_CHUNK_SIZE,
    AbsSnapshot,
    ManifestFilePath,
)
from deadline.job_attachments._snapshots._operations._hash_abs_manifest import (
    _hash_file_chunked,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.caches.hash_cache import HashCache, HashCacheEntry


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
            chunk_hashes_1 = _hash_file_chunked(
                file_path=test_file,
                cache_key=cache_key,
                file_size=200,
                mtime=12345,
                hash_alg=HashAlgorithm.XXH128,
                chunk_size=chunk_size,
                hash_cache=hash_cache,
            )

            entry_0 = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128, 0, 100)
            entry_1 = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128, 100, 200)
            assert entry_0 is not None
            assert entry_1 is not None
            assert entry_0.file_hash == chunk_hashes_1[0]
            assert entry_1.file_hash == chunk_hashes_1[1]

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
    """Tests for large file chunking in hash_abs_manifest."""

    def test_large_file_uses_chunkhashes(self, tmp_path: Path) -> None:
        """Files larger than chunk size use chunkhashes instead of hash."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)
        abs_path = str(test_file).replace("\\", "/")
        stat_info = test_file.stat()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=DEFAULT_FILE_CHUNK_SIZE + 1000,
                    mtime=int(stat_info.st_mtime_ns // 1000),
                )
            ],
            total_size=DEFAULT_FILE_CHUNK_SIZE + 1000,
        )

        with patch(
            "deadline.job_attachments._snapshots._operations._hash_abs_manifest._hash_file_chunked"
        ) as mock_chunk:
            mock_chunk.return_value = ["hash1", "hash2"]

            result = hash_abs_manifest(manifest)

            assert result.manifest.files[0].chunkhashes == ["hash1", "hash2"]
            assert result.manifest.files[0].hash is None
            mock_chunk.assert_called_once()

    def test_small_file_uses_single_hash(self, tmp_path: Path) -> None:
        """Files smaller than chunk size use single hash."""
        test_file = tmp_path / "small.txt"
        test_file.write_text("small content")

        collected = collect_abs_snapshot(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        result = hash_abs_manifest(collected)

        assert result.manifest.files[0].hash is not None
        assert result.manifest.files[0].chunkhashes is None


class TestChunkedFileCacheMocked:
    """Tests for chunked file cache behavior using mocks."""

    def test_hash_cache_with_chunked_file(self, tmp_path: Path) -> None:
        """Hash cache works correctly with chunked files."""
        test_file = tmp_path / "file.bin"
        content = bytes(range(64))
        test_file.write_bytes(content)
        abs_path = str(test_file).replace("\\", "/")
        mtime = int(test_file.stat().st_mtime_ns // 1000)

        mock_cache = MagicMock(spec=HashCache)
        mock_cache.get_entry.return_value = None

        chunk_size = 16  # 64 bytes / 16 = 4 chunks
        input_manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=64,
                    mtime=mtime,
                )
            ],
            total_size=64,
            file_chunk_size_bytes=chunk_size,
        )

        result = hash_abs_manifest(
            manifest=input_manifest,
            hash_cache=mock_cache,
        )

        assert result.manifest.files[0].chunkhashes is not None
        assert len(result.manifest.files[0].chunkhashes) == 4

        assert mock_cache.put_entry.call_count == 4

        calls = mock_cache.put_entry.call_args_list
        for i, call in enumerate(calls):
            entry = call[0][0]
            assert entry.range_start == i * chunk_size
            assert entry.range_end == (i + 1) * chunk_size
