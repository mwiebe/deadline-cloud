# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_manifest hash cache integration.

These tests cover:
- Cache hits and misses with real HashCache
- Cache updates on miss
- Force rehash behavior
- Mtime-based cache invalidation
- Mock-based cache behavior verification
"""

from pathlib import Path
from unittest.mock import MagicMock

from deadline.job_attachments.asset_manifests._operations import (
    hash_manifest,
    collect_manifest,
)
from deadline.job_attachments.asset_manifests._operations._hash_manifest import (
    _get_or_compute_hash,
)
from deadline.job_attachments.asset_manifests.versions import (
    SymlinkPolicy,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import (
    HashAlgorithm,
    hash_file,
)
from deadline.job_attachments.asset_manifests.manifest import (
    AbsSnapshotManifest,
    ManifestFilePath,
    WHOLE_FILE_CHUNK_SIZE,
)
from deadline.job_attachments.caches.hash_cache import HashCache, HashCacheEntry


class TestHashManifestWithCache:
    """Tests for hash cache integration with real HashCache."""

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
        cache_key = str(Path(collected.files[0].path).resolve())

        with HashCache(str(cache_dir)) as hash_cache:
            hashed = hash_manifest(collected, hash_cache=hash_cache)

            assert hashed.files[0].hash != ""

            cached_entry = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128)
            assert cached_entry is not None
            assert cached_entry.file_hash == hashed.files[0].hash

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
        mtime_str = str(collected.files[0].mtime)
        cache_key = str(Path(collected.files[0].path).resolve())

        with HashCache(str(cache_dir)) as hash_cache:
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

            assert hashed.files[0].hash == fake_hash

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
        cache_key = str(Path(collected.files[0].path).resolve())

        with HashCache(str(cache_dir)) as hash_cache:
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

            assert hashed.files[0].hash != fake_hash

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
        mtime_str = str(collected.files[0].mtime)
        cache_key = str(Path(collected.files[0].path).resolve())

        with HashCache(str(cache_dir)) as hash_cache:
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

            assert hashed.files[0].hash != fake_hash

            cached_entry = hash_cache.get_entry(cache_key, HashAlgorithm.XXH128)
            assert cached_entry.file_hash == hashed.files[0].hash

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
        assert hashed.files[0].hash == expected_hash


class TestGetOrComputeHashWithCache:
    """Tests for _get_or_compute_hash with cache."""

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


class TestHashManifestCacheMocked:
    """Tests for hash cache integration using mocks."""

    def test_hash_cache_hit_uses_cached_hash(self, tmp_path: Path) -> None:
        """When hash cache has matching entry, cached hash is used."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")
        resolved_path = str(test_file.resolve()).replace("\\", "/")
        mtime = int(test_file.stat().st_mtime_ns // 1000)
        mtime_str = str(mtime)

        mock_cache = MagicMock(spec=HashCache)
        cached_hash = "cachedcachedcachedcachedcached00"
        mock_cache.get_entry.return_value = HashCacheEntry(
            file_path=resolved_path,
            hash_algorithm=HashAlgorithm.XXH128,
            file_hash=cached_hash,
            last_modified_time=mtime_str,
            range_start=0,
            range_end=-1,
        )

        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=7,
                    mtime=mtime,
                )
            ],
            total_size=7,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = hash_manifest(
            manifest=input_manifest,
            hash_cache=mock_cache,
        )

        assert result.files[0].hash == cached_hash
        mock_cache.get_entry.assert_called()
        mock_cache.put_entry.assert_not_called()

    def test_hash_cache_miss_computes_and_stores_hash(self, tmp_path: Path) -> None:
        """When hash cache misses, hash is computed and stored."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")
        mtime = int(test_file.stat().st_mtime_ns // 1000)

        mock_cache = MagicMock(spec=HashCache)
        mock_cache.get_entry.return_value = None

        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=7,
                    mtime=mtime,
                )
            ],
            total_size=7,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = hash_manifest(
            manifest=input_manifest,
            hash_cache=mock_cache,
        )

        assert result.files[0].hash is not None
        assert len(result.files[0].hash) == 32

        mock_cache.put_entry.assert_called_once()
        put_call = mock_cache.put_entry.call_args[0][0]
        assert put_call.file_hash == result.files[0].hash
        assert put_call.last_modified_time == str(mtime)

    def test_hash_cache_stale_entry_recomputes_hash(self, tmp_path: Path) -> None:
        """When cache entry has different mtime, hash is recomputed."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")
        resolved_path = str(test_file.resolve()).replace("\\", "/")
        mtime = int(test_file.stat().st_mtime_ns // 1000)

        mock_cache = MagicMock(spec=HashCache)
        mock_cache.get_entry.return_value = HashCacheEntry(
            file_path=resolved_path,
            hash_algorithm=HashAlgorithm.XXH128,
            file_hash="stale_hash_should_not_be_used",
            last_modified_time="99999",  # Different mtime!
            range_start=0,
            range_end=-1,
        )

        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=7,
                    mtime=mtime,
                )
            ],
            total_size=7,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = hash_manifest(
            manifest=input_manifest,
            hash_cache=mock_cache,
        )

        assert result.files[0].hash is not None
        assert result.files[0].hash != "stale_hash_should_not_be_used"
        assert len(result.files[0].hash) == 32

        mock_cache.put_entry.assert_called_once()

    def test_force_rehash_ignores_cache(self, tmp_path: Path) -> None:
        """When force_rehash=True, cache is ignored and hash is recomputed."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")
        mtime = int(test_file.stat().st_mtime_ns // 1000)

        mock_cache = MagicMock(spec=HashCache)

        input_manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=7,
                    mtime=mtime,
                )
            ],
            total_size=7,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = hash_manifest(
            manifest=input_manifest,
            hash_cache=mock_cache,
            force_rehash=True,
        )

        assert result.files[0].hash is not None

        mock_cache.get_entry.assert_not_called()
        mock_cache.put_entry.assert_called_once()
