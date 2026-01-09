# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for filling in hashes for manifest objects that were created by collect_manifest
or compute_diff_manifest.

This module implements the HASH operation from the composable manifest operations design:
    HASH: AbsManifest (with hash=None) → AbsManifest (with hashes filled in)

Where AbsManifest can be either:
    - AbsSnapshot: A full directory tree snapshot with absolute paths
    - AbsSnapshotDiff: A diff manifest with absolute paths (contains new/modified/deleted entries)

The separation of collection from hashing enables:
- Fast diff comparison by mtime/size without hashing unchanged files
- Hash cache integration - only hash files with cache misses
- Deferred hashing - collect structure first, hash only what's needed
- Force rehash option - recalculate all hashes when needed

All composable operations use v2025 structure and semantics internally. Support for
v2023 on-disk format is provided via lossy conversion functions that drop symlinks,
deletions, and other v2025-only features.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, List, Optional

from .._manifest import (
    AbsManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    _is_absolute_path,
)
from ...asset_manifests.hash_algorithms import hash_file, HashAlgorithm
from ...caches.hash_cache import HashCache, HashCacheEntry, WHOLE_FILE_RANGE_END


def hash_manifest(
    manifest: AbsManifest,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    file_chunk_size_bytes: Optional[int] = None,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> AbsManifest:
    """
    Fill in hashes for a manifest structure with absolute paths.

    Given a manifest with hash=None for file entries (from collect_manifest or
    compute_diff_manifest), computes and fills in the actual hashes.

    Args:
        manifest: Manifest with absolute paths and hash=None for unhashed files.
            Can be either:
            - AbsSnapshot (from collect_manifest)
            - AbsSnapshotDiff (from compute_diff_manifest with ignore_hashes=True)
        hash_cache: Optional hash cache for efficiency
        force_rehash: If True, ignore cache and recalculate all hashes
        file_chunk_size_bytes: Chunk size for output manifest.
            - None: Preserve the chunk size from the input manifest
            - WHOLE_FILE_CHUNK_SIZE (-1): Hash files as a whole, no chunking
            - Positive int: Chunk size in bytes for large files
        print_function_callback: Progress callback

    Returns:
        A NEW manifest of the same type with all hashes filled in. The manifest type
        (snapshot/diff) and parentManifestHash are preserved from the input.

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

    Chunking Behavior:
        - If effective chunk size is WHOLE_FILE_CHUNK_SIZE (-1): all files
          are hashed as a whole (no chunking regardless of file size)
        - If effective chunk size is a positive int: files larger than this size
          use chunked hashing with chunkhashes field

    Note:
        - Input manifest must have absolute paths (from collect_manifest or join_manifest)
        - Symlink entries are unchanged (they have symlink_target, not hash)
        - Directory entries are unchanged (they have no hash)
        - Deleted entries are unchanged (they mark deletions, no hash needed)
        - Returns a NEW manifest (does not mutate input)
    """
    # Validate that manifest has absolute paths
    _validate_absolute_paths(manifest)

    # Determine output chunk size: use parameter if provided, otherwise preserve from input manifest
    output_chunk_size = (
        file_chunk_size_bytes if file_chunk_size_bytes is not None else manifest.fileChunkSizeBytes
    )

    # WHOLE_FILE_CHUNK_SIZE (-1) means no chunking
    chunking_enabled = output_chunk_size > 0

    hashed_paths: List[ManifestFilePath] = []
    total_size = 0

    for entry in manifest.files:
        # Symlinks don't need hashing - pass through unchanged
        if entry.symlink_target is not None:
            hashed_paths.append(
                ManifestFilePath(
                    path=entry.path,
                    symlink_target=entry.symlink_target,
                )
            )
            print_function_callback(f"Symlink (no hash): {entry.path}")
            continue

        # Deleted entries don't need hashing - pass through unchanged
        if entry.deleted:
            hashed_paths.append(
                ManifestFilePath(
                    path=entry.path,
                    deleted=True,
                )
            )
            continue

        abs_path = Path(entry.path)
        # Use resolved path as cache key for consistency
        cache_key = str(abs_path.resolve())

        # Check if file needs chunking (only if chunking enabled and file is larger than chunk size)
        needs_chunking = (
            chunking_enabled and entry.size is not None and entry.size > output_chunk_size
        )

        if needs_chunking:
            # Large file: hash and chunkhashes should both be None (unhashed)
            if entry.hash is not None:
                raise ValueError(
                    f"Large file '{entry.path}' (size={entry.size}) should have hash=None, "
                    f"got hash={entry.hash!r}"
                )
            if entry.chunkhashes is not None:
                raise ValueError(
                    f"Large file '{entry.path}' (size={entry.size}) should have chunkhashes=None "
                    f"(unhashed), got chunkhashes={entry.chunkhashes!r}"
                )

            # Compute chunk hashes
            chunk_hashes = _hash_file_chunked(
                file_path=abs_path,
                cache_key=cache_key,
                file_size=entry.size,  # type: ignore[arg-type]
                mtime=entry.mtime,
                hash_alg=manifest.hashAlg,
                chunk_size=output_chunk_size,
                hash_cache=hash_cache,
                force_rehash=force_rehash,
            )

            hashed_paths.append(
                ManifestFilePath(
                    path=entry.path,
                    chunkhashes=chunk_hashes,
                    size=entry.size,
                    mtime=entry.mtime,
                    runnable=entry.runnable,
                )
            )
            print_function_callback(f"Hashed (chunked, {len(chunk_hashes)} chunks): {entry.path}")
        else:
            # Small file or no chunking: hash should be None (unhashed), chunkhashes should be None
            if entry.hash is not None:
                raise ValueError(
                    f"File '{entry.path}' should have hash=None (unhashed), got hash={entry.hash!r}"
                )
            if entry.chunkhashes is not None:
                raise ValueError(
                    f"File '{entry.path}' should have chunkhashes=None, "
                    f"got chunkhashes={entry.chunkhashes!r}"
                )

            # Compute hash (whole file)
            file_hash = _get_or_compute_hash(
                file_path=abs_path,
                cache_key=cache_key,
                mtime=entry.mtime,
                hash_alg=manifest.hashAlg,
                hash_cache=hash_cache,
                force_rehash=force_rehash,
            )

            hashed_paths.append(
                ManifestFilePath(
                    path=entry.path,
                    hash=file_hash,
                    size=entry.size,
                    mtime=entry.mtime,
                    runnable=entry.runnable,
                )
            )
            print_function_callback(f"Hashed: {entry.path}")

        if entry.size is not None:
            total_size += entry.size

    # Copy directory entries unchanged
    dir_entries: List[ManifestDirectoryPath] = []
    for d in manifest.dirs:
        dir_entries.append(
            ManifestDirectoryPath(
                path=d.path,
                deleted=d.deleted,
            )
        )

    # Return the same manifest type as input
    manifest_type = type(manifest)
    return manifest_type(
        hash_alg=manifest.hashAlg,
        dirs=dir_entries,
        files=hashed_paths,
        total_size=total_size,
        parent_manifest_hash=manifest.parentManifestHash,
        file_chunk_size_bytes=output_chunk_size,
    )


def _validate_absolute_paths(manifest: AbsManifest) -> None:
    """Validate that all paths in the manifest are absolute."""
    for entry in manifest.files:
        if not _is_absolute_path(entry.path):
            raise ValueError(
                f"HASH operation requires absolute paths. "
                f"Found relative path: '{entry.path}'. "
                f"Use collect_manifest() or join_manifest() to create a manifest with absolute paths."
            )

    for d in manifest.dirs:
        if not _is_absolute_path(d.path):
            raise ValueError(
                f"HASH operation requires absolute paths. "
                f"Found relative directory path: '{d.path}'. "
                f"Use collect_manifest() or join_manifest() to create a manifest with absolute paths."
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
