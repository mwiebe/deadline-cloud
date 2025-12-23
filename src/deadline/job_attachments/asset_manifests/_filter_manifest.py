# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for filtering manifest entries by include/exclude glob patterns.

This module implements the FILTER operation from the composable manifest operations design:
    FILTER: Manifest → Manifest (with only matching entries)

The FILTER operation is critical for diff computation:
- Both parent and current manifests must be filtered with the SAME patterns
- This ensures deletions are computed correctly within the filtered view
"""

from __future__ import annotations

import fnmatch
from typing import List, Optional

from .base_manifest import BaseAssetManifest
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


def _filter_manifest(
    manifest: BaseAssetManifest,
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
) -> BaseAssetManifest:
    """
    Apply include/exclude glob patterns to a manifest's entries.

    This operation:
    - Filters file/symlink entries by path
    - Filters directory entries by path (v2025+)
    - Returns a NEW manifest with only matching entries
    - Preserves manifest version and type

    Args:
        manifest: The manifest to filter
        include: Glob patterns for paths to include (empty/None = include all)
        exclude: Glob patterns for paths to exclude (empty/None = exclude none)

    Returns:
        A new manifest with only entries matching the patterns

    Pattern Matching:
        - Uses fnmatch for glob-style pattern matching
        - Patterns are matched against the full relative path
        - If include patterns are specified, path must match at least one
        - Path must not match any exclude pattern

    Example:
        manifest = _collect_manifest_structure(root, version)
        filtered = _filter_manifest(manifest, include=["*.blend"], exclude=["backup/*"])

    Critical for Diff:
        When computing a diff manifest, BOTH parent and current must be filtered
        with the SAME patterns before comparison. This ensures deletions are
        computed correctly within the filtered view.
    """
    include_patterns = include if include else []
    exclude_patterns = exclude if exclude else []

    if manifest.manifestVersion == ManifestVersion.v2023_03_03:
        if not isinstance(manifest, AssetManifest2023):
            raise TypeError(
                f"Expected AssetManifest2023 for version {manifest.manifestVersion}, "
                f"got {type(manifest).__name__}"
            )
        return _filter_manifest_v2023(manifest, include_patterns, exclude_patterns)
    elif manifest.manifestVersion == ManifestVersion.v2025_12_04_beta:
        if not isinstance(manifest, AssetManifest2025):
            raise TypeError(
                f"Expected AssetManifest2025 for version {manifest.manifestVersion}, "
                f"got {type(manifest).__name__}"
            )
        return _filter_manifest_v2025(manifest, include_patterns, exclude_patterns)
    else:
        raise ValueError(f"Unsupported manifest version: {manifest.manifestVersion}")


def _matches_patterns(path: str, include: List[str], exclude: List[str]) -> bool:
    """
    Check if a path matches the include/exclude pattern rules.

    Args:
        path: The path to check
        include: Glob patterns for paths to include (empty = include all)
        exclude: Glob patterns for paths to exclude

    Returns:
        True if the path should be included, False otherwise
    """
    # If include patterns specified, path must match at least one
    if include:
        if not any(fnmatch.fnmatch(path, pattern) for pattern in include):
            return False

    # Path must not match any exclude pattern
    if any(fnmatch.fnmatch(path, pattern) for pattern in exclude):
        return False

    return True


def _filter_manifest_v2023(
    manifest: AssetManifest2023,
    include: List[str],
    exclude: List[str],
) -> AssetManifest2023:
    """
    Filter a v2023-03-03 manifest by include/exclude patterns.

    v2023 format only has file entries (no directories or symlinks).
    """
    filtered_paths: List[ManifestPath2023] = []
    total_size = 0

    for entry in manifest.paths:
        if _matches_patterns(entry.path, include, exclude):
            # Create a new entry (don't mutate original)
            filtered_paths.append(
                ManifestPath2023(
                    path=entry.path,
                    hash=entry.hash,
                    size=entry.size,
                    mtime=entry.mtime,
                )
            )
            total_size += entry.size

    return AssetManifest2023(
        hash_alg=manifest.hashAlg,
        paths=filtered_paths,
        total_size=total_size,
    )


def _filter_manifest_v2025(
    manifest: AssetManifest2025,
    include: List[str],
    exclude: List[str],
) -> AssetManifest2025:
    """
    Filter a v2025-12-04-beta manifest by include/exclude patterns.

    Filters:
    - File entries (regular files, symlinks, deleted markers)
    - Directory entries

    Preserves:
    - Manifest type (snapshot/diff)
    - Parent manifest hash (for diff manifests)
    - Hash algorithm
    """
    filtered_paths: List[ManifestFilePath2025] = []
    filtered_dirs: List[ManifestDirectoryPath2025] = []
    total_size = 0

    # Filter file entries
    for entry in manifest.paths:
        if _matches_patterns(entry.path, include, exclude):
            # Create a new entry (don't mutate original)
            filtered_paths.append(
                ManifestFilePath2025(
                    path=entry.path,
                    hash=entry.hash,
                    size=entry.size,
                    mtime=entry.mtime,
                    runnable=entry.runnable,
                    chunkhashes=entry.chunkhashes,
                    symlink_target=entry.symlink_target,
                    deleted=entry.deleted,
                )
            )
            # Only count size for non-deleted, non-symlink entries
            if not entry.deleted and entry.symlink_target is None and entry.size is not None:
                total_size += entry.size

    # Filter directory entries
    for dir_entry in manifest.dirs:
        if _matches_patterns(dir_entry.path, include, exclude):
            filtered_dirs.append(
                ManifestDirectoryPath2025(
                    path=dir_entry.path,
                    deleted=dir_entry.deleted,
                )
            )

    return AssetManifest2025(
        hash_alg=manifest.hashAlg,
        dirs=filtered_dirs,
        paths=filtered_paths,
        total_size=total_size,
        manifest_type=manifest.manifestType,
        parent_manifest_hash=manifest.parentManifestHash,
    )
