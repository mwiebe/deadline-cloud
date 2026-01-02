# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for filling in hashes for manifest objects that were created by collect_manifest
or compute_diff_manifest.

This module implements the HASH operation from the composable manifest operations design:
    HASH: AbsManifest (with hash="") → AbsManifest (with hashes filled in)

Where AbsManifest can be either:
    - AbsSnapshot: A full directory tree snapshot with absolute paths
    - AbsDiff: A diff manifest with absolute paths (contains new/modified/deleted entries)

The separation of collection from hashing enables:
- Fast diff comparison by mtime/size without hashing unchanged files
- Hash cache integration - only hash files with cache misses
- Deferred hashing - collect structure first, hash only what's needed
- Force rehash option - recalculate all hashes when needed
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, List, Optional

from ..base_manifest import BaseAssetManifest, FILE_CHUNK_SIZE_BYTES
from ..hash_algorithms import HashAlgorithm, hash_file
from ..versions import ManifestVersion
from ..v2023_03_03.asset_manifest import (
    AssetManifest as AssetManifest2023,
    ManifestPath as ManifestPath2023,
)
from ..v2025_12_04.asset_manifest import (
    AssetManifest as AssetManifest2025,
    ManifestDirectoryPath as ManifestDirectoryPath2025,
    ManifestFilePath as ManifestFilePath2025,
)
from ...caches.hash_cache import HashCache, HashCacheEntry, WHOLE_FILE_RANGE_END


def _is_absolute_path(path: str) -> bool:
    """Check if a path string represents an absolute path."""
    # POSIX absolute paths start with /
    # Windows absolute paths start with drive letter (e.g., C:/) or UNC (//server)
    return (
        path.startswith("/")
        or (len(path) >= 3 and path[1] == ":" and path[2] == "/")
        or path.startswith("//")
    )


def hash_manifest(
    manifest: BaseAssetManifest,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> BaseAssetManifest:
    """
    Fill in hashes for a manifest structure with absolute paths.

    Given a manifest with hash="" for file entries (from collect_manifest or
    compute_diff_manifest), computes and fills in the actual hashes.

    Args:
        manifest: Manifest with absolute paths and empty hashes. Can be either:
            - A snapshot manifest (from collect_manifest)
            - A diff manifest (from compute_diff_manifest with ignore_hashes=True)
        hash_cache: Optional hash cache for efficiency
        force_rehash: If True, ignore cache and recalculate all hashes
        print_function_callback: Progress callback

    Returns:
        A NEW manifest with all hashes filled in. The manifest type (snapshot/diff)
        and parentManifestHash are preserved from the input.

    Raises:
        ValueError: If the manifest contains relative paths (paths must be absolute)

    Hash Cache Behavior:
        - If hash_cache is provided and force_rehash=False:
          - Check cache using (path, mtime) as key
          - On cache hit: use cached hash
          - On cache miss: compute hash and update cache
        - If force_rehash=True: always compute hash, update cache
        - If hash_cache is None: always compute hash

    Manifest Type Handling:
        - Snapshot manifests: All file entries are hashed
        - Diff manifests: Only new/modified file entries are hashed;
          deleted entries are passed through unchanged (no hash needed)

    Note:
        - Input manifest must have absolute paths (from collect_manifest or join_manifest)
        - Symlink entries are unchanged (they have symlink_target, not hash)
        - Directory entries are unchanged (they have no hash)
        - Deleted entries are unchanged (they mark deletions, no hash needed)
        - For v2025 large files (>256MB): computes chunkhashes
        - Returns a NEW manifest (does not mutate input)
    """
    # Validate that manifest has absolute paths
    _validate_absolute_paths(manifest)

    if manifest.manifestVersion == ManifestVersion.v2023_03_03:
        if not isinstance(manifest, AssetManifest2023):
            raise TypeError(
                f"Expected AssetManifest2023 for version {manifest.manifestVersion}, "
                f"got {type(manifest).__name__}"
            )
        return _hash_manifest_v2023(manifest, hash_cache, force_rehash, print_function_callback)
    elif manifest.manifestVersion == ManifestVersion.v2025_12_04_beta:
        if not isinstance(manifest, AssetManifest2025):
            raise TypeError(
                f"Expected AssetManifest2025 for version {manifest.manifestVersion}, "
                f"got {type(manifest).__name__}"
            )
        return _hash_manifest_v2025(manifest, hash_cache, force_rehash, print_function_callback)
    else:
        raise ValueError(f"Unsupported manifest version: {manifest.manifestVersion}")


def _validate_absolute_paths(manifest: BaseAssetManifest) -> None:
    """Validate that all paths in the manifest are absolute."""
    for entry in manifest.paths:
        if not _is_absolute_path(entry.path):
            raise ValueError(
                f"HASH operation requires absolute paths. "
                f"Found relative path: '{entry.path}'. "
                f"Use collect_manifest() or join_manifest() to create a manifest with absolute paths."
            )

    # Also check directory paths for v2025
    if hasattr(manifest, "dirs"):
        for d in manifest.dirs:
            if not _is_absolute_path(d.path):
                raise ValueError(
                    f"HASH operation requires absolute paths. "
                    f"Found relative directory path: '{d.path}'. "
                    f"Use collect_manifest() or join_manifest() to create a manifest with absolute paths."
                )


def _hash_manifest_v2023(
    manifest: AssetManifest2023,
    hash_cache: Optional[HashCache],
    force_rehash: bool,
    print_function_callback: Callable[[Any], None],
) -> AssetManifest2023:
    """
    Fill in hashes for a v2023-03-03 manifest.

    v2023 format only supports regular files with single hashes.
    """
    hashed_paths: List[ManifestPath2023] = []
    total_size = 0

    for entry in manifest.paths:
        abs_path = Path(entry.path)
        # Use resolved path as cache key for consistency
        cache_key = str(abs_path.resolve())

        # Get hash (from cache or compute)
        file_hash = _get_or_compute_hash(
            file_path=abs_path,
            cache_key=cache_key,
            mtime=entry.mtime,
            hash_alg=manifest.hashAlg,
            hash_cache=hash_cache,
            force_rehash=force_rehash,
        )

        hashed_paths.append(
            ManifestPath2023(
                path=entry.path,
                hash=file_hash,
                size=entry.size,
                mtime=entry.mtime,
            )
        )
        total_size += entry.size
        print_function_callback(f"Hashed: {entry.path}")

    return AssetManifest2023(
        hash_alg=manifest.hashAlg,
        paths=hashed_paths,
        total_size=total_size,
    )


def _hash_manifest_v2025(
    manifest: AssetManifest2025,
    hash_cache: Optional[HashCache],
    force_rehash: bool,
    print_function_callback: Callable[[Any], None],
) -> AssetManifest2025:
    """
    Fill in hashes for a v2025-12-04-beta manifest.

    Handles:
    - Regular files: single hash or chunkhashes (>256MB)
    - Symlinks: unchanged (no hash needed)
    - Directories: unchanged (no hash needed)

    Input validation for file entries (non-symlink, non-deleted):
    - For small files (<=256MB): hash should be a string (empty from collect),
      chunkhashes should be None
    - For large files (>256MB): hash should be None, chunkhashes should be a list
      of strings with length == ceil(size / 256MB)
    """
    hashed_paths: List[ManifestFilePath2025] = []
    total_size = 0

    for entry in manifest.paths:
        # Symlinks don't need hashing - pass through unchanged
        if entry.symlink_target is not None:
            hashed_paths.append(
                ManifestFilePath2025(
                    path=entry.path,
                    symlink_target=entry.symlink_target,
                )
            )
            print_function_callback(f"Symlink (no hash): {entry.path}")
            continue

        # Deleted entries don't need hashing - pass through unchanged
        if entry.deleted:
            hashed_paths.append(
                ManifestFilePath2025(
                    path=entry.path,
                    deleted=True,
                )
            )
            continue

        abs_path = Path(entry.path)
        # Use resolved path as cache key for consistency
        cache_key = str(abs_path.resolve())

        # Check if file needs chunking (>256MB)
        if entry.size is not None and entry.size > FILE_CHUNK_SIZE_BYTES:
            # Large file: validate input - hash should be None, chunkhashes should be
            # a list with correct length for the file size
            expected_chunks = (entry.size + FILE_CHUNK_SIZE_BYTES - 1) // FILE_CHUNK_SIZE_BYTES
            if entry.hash is not None:
                raise ValueError(
                    f"Large file '{entry.path}' (size={entry.size}) should have hash=None, "
                    f"got hash={entry.hash!r}"
                )
            if not isinstance(entry.chunkhashes, list) or len(entry.chunkhashes) != expected_chunks:
                raise ValueError(
                    f"Large file '{entry.path}' (size={entry.size}) should have "
                    f"{expected_chunks} chunkhashes, got {entry.chunkhashes!r}"
                )

            # Compute chunk hashes
            chunk_hashes = _hash_file_chunked(
                file_path=abs_path,
                cache_key=cache_key,
                file_size=entry.size,
                mtime=entry.mtime,
                hash_alg=manifest.hashAlg,
                chunk_size=FILE_CHUNK_SIZE_BYTES,
                hash_cache=hash_cache,
                force_rehash=force_rehash,
            )

            hashed_paths.append(
                ManifestFilePath2025(
                    path=entry.path,
                    chunkhashes=chunk_hashes,
                    size=entry.size,
                    mtime=entry.mtime,
                    runnable=getattr(entry, "runnable", False),
                )
            )
            print_function_callback(f"Hashed (chunked, {len(chunk_hashes)} chunks): {entry.path}")
        else:
            # Small file: validate input - hash should be a string (empty from collect),
            # chunkhashes should be None
            if not isinstance(entry.hash, str):
                raise ValueError(
                    f"Small file '{entry.path}' should have hash as a string, "
                    f"got hash={entry.hash!r}"
                )
            if entry.chunkhashes is not None:
                raise ValueError(
                    f"Small file '{entry.path}' should have chunkhashes=None, "
                    f"got chunkhashes={entry.chunkhashes!r}"
                )

            # Compute hash
            file_hash = _get_or_compute_hash(
                file_path=abs_path,
                cache_key=cache_key,
                mtime=entry.mtime,
                hash_alg=manifest.hashAlg,
                hash_cache=hash_cache,
                force_rehash=force_rehash,
            )

            hashed_paths.append(
                ManifestFilePath2025(
                    path=entry.path,
                    hash=file_hash,
                    size=entry.size,
                    mtime=entry.mtime,
                    runnable=getattr(entry, "runnable", False),
                )
            )
            print_function_callback(f"Hashed: {entry.path}")

        if entry.size is not None:
            total_size += entry.size

    # Copy directory entries unchanged
    dir_entries: List[ManifestDirectoryPath2025] = []
    for d in manifest.dirs:
        dir_entries.append(
            ManifestDirectoryPath2025(
                path=d.path,
                deleted=d.deleted,
            )
        )

    return AssetManifest2025(
        hash_alg=manifest.hashAlg,
        dirs=dir_entries,
        paths=hashed_paths,
        total_size=total_size,
        manifest_type=manifest.manifestType,
        parent_manifest_hash=manifest.parentManifestHash,
    )


def _get_or_compute_hash(
    file_path: Path,
    cache_key: str,
    mtime: Optional[int],
    hash_alg: HashAlgorithm,
    hash_cache: Optional[HashCache],
    force_rehash: bool,
    range_start: int = 0,
    range_end: int = WHOLE_FILE_RANGE_END,
) -> str:
    """
    Get hash from cache or compute it.

    Supports both whole-file hashes and byte-range hashes for chunked files.

    Args:
        file_path: Absolute path to the file
        cache_key: Key for cache lookup (the absolute path string)
        mtime: File modification time in microseconds
        hash_alg: Hash algorithm to use
        hash_cache: Optional hash cache
        force_rehash: If True, ignore cache
        range_start: Start byte offset (0 for whole-file or chunk start)
        range_end: End byte offset (-1 for whole-file, or chunk end exclusive)

    Returns:
        The file hash as a hex string
    """
    # Convert mtime to string for cache lookup (cache stores as timestamp string)
    mtime_str = str(mtime) if mtime is not None else ""

    # Try cache first (unless force_rehash)
    if hash_cache is not None and not force_rehash:
        cache_entry = hash_cache.get_entry(cache_key, hash_alg, range_start, range_end)
        if cache_entry is not None and cache_entry.last_modified_time == mtime_str:
            return cache_entry.file_hash

    # Compute hash (works for both whole-file and byte-range)
    file_hash = hash_file(str(file_path), hash_alg, range_start, range_end)

    # Update cache
    if hash_cache is not None:
        hash_cache.put_entry(
            HashCacheEntry(
                file_path=cache_key,
                hash_algorithm=hash_alg,
                file_hash=file_hash,
                last_modified_time=mtime_str,
                range_start=range_start,
                range_end=range_end,
            )
        )

    return file_hash


def _hash_file_chunked(
    file_path: Path,
    cache_key: str,
    file_size: int,
    mtime: Optional[int],
    hash_alg: HashAlgorithm,
    chunk_size: int,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
) -> List[str]:
    """
    Hash a file in chunks, returning a list of chunk hashes.

    Uses the hash cache for each chunk when available, allowing it to
    resume part-way through a file without recomputing hashes when
    hashing is interrupted.

    Args:
        file_path: Absolute path to the file
        cache_key: Key for cache lookup (the absolute path string)
        file_size: Size of the file in bytes
        mtime: File modification time in microseconds
        hash_alg: Hash algorithm to use
        chunk_size: Size of each chunk in bytes
        hash_cache: Optional hash cache for efficiency
        force_rehash: If True, ignore cache and recalculate all hashes

    Returns:
        List of hash strings, one per chunk
    """
    chunk_hashes: List[str] = []
    offset = 0

    while offset < file_size:
        range_start = offset
        range_end = min(offset + chunk_size, file_size)

        chunk_hash = _get_or_compute_hash(
            file_path=file_path,
            cache_key=cache_key,
            mtime=mtime,
            hash_alg=hash_alg,
            hash_cache=hash_cache,
            force_rehash=force_rehash,
            range_start=range_start,
            range_end=range_end,
        )
        chunk_hashes.append(chunk_hash)
        offset = range_end

    return chunk_hashes
