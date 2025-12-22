# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for filling in hashes for manifest objects that were created by _collect_manifest.

This module implements the HASH operation from the composable manifest operations design:
    HASH: Manifest (with hash="") → Manifest (with hashes filled in)

The separation of collection from hashing enables:
- Fast diff comparison by mtime/size without hashing unchanged files
- Hash cache integration - only hash files with cache misses
- Deferred hashing - collect structure first, hash only what's needed
- Force rehash option - recalculate all hashes when needed
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, List, Optional

from .base_manifest import BaseAssetManifest, FILE_CHUNK_SIZE_BYTES
from .hash_algorithms import HashAlgorithm, hash_file, hash_data
from .versions import ManifestVersion
from .v2023_03_03.asset_manifest import (
    AssetManifest as AssetManifest2023,
    ManifestPath as ManifestPath2023,
)
from .v2025_12_04.asset_manifest import (
    AssetManifest as AssetManifest2025,
    ManifestDirectoryPath as ManifestDirectoryPath2025,
    ManifestFilePath as ManifestFilePath2025,
)
from ..caches.hash_cache import HashCache, HashCacheEntry


def _hash_manifest(
    manifest: BaseAssetManifest,
    root: Path | str,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> BaseAssetManifest:
    """
    Fill in hashes for a manifest structure.

    Given a manifest with hash="" for file entries (from _collect_manifest_structure),
    computes and fills in the actual hashes.

    Args:
        manifest: Manifest with empty hashes (from _collect_manifest_structure)
        root: Root directory path (needed to read files for hashing)
        hash_cache: Optional hash cache for efficiency
        force_rehash: If True, ignore cache and recalculate all hashes
        print_function_callback: Progress callback

    Returns:
        A NEW manifest with all hashes filled in

    Hash Cache Behavior:
        - If hash_cache is provided and force_rehash=False:
          - Check cache using (path, mtime) as key
          - On cache hit: use cached hash
          - On cache miss: compute hash and update cache
        - If force_rehash=True: always compute hash, update cache
        - If hash_cache is None: always compute hash

    Note:
        - Symlink entries are unchanged (they have symlink_target, not hash)
        - Directory entries are unchanged (they have no hash)
        - For v2025 large files (>256MB): computes chunkhashes
        - Returns a NEW manifest (does not mutate input)
    """
    root_path = Path(os.path.normpath(os.path.abspath(root)))

    if manifest.manifestVersion == ManifestVersion.v2023_03_03:
        if not isinstance(manifest, AssetManifest2023):
            raise TypeError(
                f"Expected AssetManifest2023 for version {manifest.manifestVersion}, "
                f"got {type(manifest).__name__}"
            )
        return _hash_manifest_v2023(
            manifest, root_path, hash_cache, force_rehash, print_function_callback
        )
    elif manifest.manifestVersion == ManifestVersion.v2025_12_04_beta:
        if not isinstance(manifest, AssetManifest2025):
            raise TypeError(
                f"Expected AssetManifest2025 for version {manifest.manifestVersion}, "
                f"got {type(manifest).__name__}"
            )
        return _hash_manifest_v2025(
            manifest, root_path, hash_cache, force_rehash, print_function_callback
        )
    else:
        raise ValueError(f"Unsupported manifest version: {manifest.manifestVersion}")


def _hash_manifest_v2023(
    manifest: AssetManifest2023,
    root_path: Path,
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
        abs_path = root_path / entry.path

        # Get hash (from cache or compute)
        file_hash = _get_or_compute_hash(
            file_path=abs_path,
            rel_path=entry.path,
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
    root_path: Path,
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

        abs_path = root_path / entry.path

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
                abs_path, manifest.hashAlg, FILE_CHUNK_SIZE_BYTES
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
                rel_path=entry.path,
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
    rel_path: str,
    mtime: Optional[int],
    hash_alg: HashAlgorithm,
    hash_cache: Optional[HashCache],
    force_rehash: bool,
) -> str:
    """
    Get hash from cache or compute it.

    Args:
        file_path: Absolute path to the file
        rel_path: Relative path (used as cache key)
        mtime: File modification time in microseconds
        hash_alg: Hash algorithm to use
        hash_cache: Optional hash cache
        force_rehash: If True, ignore cache

    Returns:
        The file hash as a hex string
    """
    # Convert mtime to string for cache lookup (cache stores as timestamp string)
    mtime_str = str(mtime) if mtime is not None else ""

    # Try cache first (unless force_rehash)
    if hash_cache is not None and not force_rehash:
        cache_entry = hash_cache.get_entry(rel_path, hash_alg)
        if cache_entry is not None and cache_entry.last_modified_time == mtime_str:
            return cache_entry.file_hash

    # Compute hash
    file_hash = hash_file(str(file_path), hash_alg)

    # Update cache
    if hash_cache is not None:
        hash_cache.put_entry(
            HashCacheEntry(
                file_path=rel_path,
                hash_algorithm=hash_alg,
                file_hash=file_hash,
                last_modified_time=mtime_str,
            )
        )

    return file_hash


def _hash_file_chunked(
    file_path: Path | str,
    hash_alg: HashAlgorithm,
    chunk_size: int,
) -> List[str]:
    """
    Hash a file in chunks, returning a list of chunk hashes.

    Args:
        file_path: Path to the file
        hash_alg: Hash algorithm to use
        chunk_size: Size of each chunk in bytes

    Returns:
        List of hash strings, one per chunk
    """
    chunk_hashes: List[str] = []

    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            chunk_hashes.append(hash_data(chunk, hash_alg))

    return chunk_hashes
