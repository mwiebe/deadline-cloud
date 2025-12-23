# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for filtering manifest entries using a flexible filter interface.

This module implements the FILTER operation from the composable manifest operations design:
    FILTER: Manifest → Manifest (with only matching entries)

The FILTER operation is critical for diff computation:
- Both parent and current manifests must be filtered with the SAME filter
- This ensures deletions are computed correctly within the filtered view

Filter Interface:
    The filter is a Callable that takes a manifest entry (file or directory) and
    returns True to keep the entry, False to exclude it. This allows for flexible
    filtering strategies beyond simple include/exclude patterns.

Built-in Filters:
    - IncludeExcludePathsFilter: Glob-based include/exclude pattern matching
"""

from __future__ import annotations

import fnmatch
from typing import Callable, List, Optional, Union

from .base_manifest import BaseAssetManifest, BaseManifestDirectoryPath, BaseManifestPath
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


class IncludeExcludePathsFilter:
    """
    Filter manifest entries using include/exclude glob patterns.

    This filter implements the classic include/exclude pattern matching:
    - If include patterns are specified, the path must match at least one
    - The path must not match any exclude pattern

    Pattern Matching:
        - Uses fnmatch for glob-style pattern matching
        - Patterns are matched against the full relative path
        - Common patterns: "*.blend", "backup/*", "**/*.tmp"

    Example:
        filter = IncludeExcludePathsFilter(
            include=["*.blend", "textures/*"],
            exclude=["backup/*", "*.tmp"]
        )
        filtered = _filter_manifest(manifest, filter)
    """

    def __init__(
        self,
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
    ) -> None:
        """
        Initialize the filter with include/exclude patterns.

        Args:
            include: Glob patterns for paths to include (empty/None = include all)
            exclude: Glob patterns for paths to exclude (empty/None = exclude none)
        """
        self.include_patterns: List[str] = include if include else []
        self.exclude_patterns: List[str] = exclude if exclude else []

    def __call__(self, entry: Union[BaseManifestPath, BaseManifestDirectoryPath]) -> bool:
        """
        Check if a manifest entry matches the include/exclude patterns.

        Args:
            entry: A file path entry or directory path entry from a manifest.

        Returns:
            True if the entry should be included, False otherwise.
        """
        return _matches_patterns(entry.path, self.include_patterns, self.exclude_patterns)

    def __repr__(self) -> str:
        return (
            f"IncludeExcludePathsFilter(include={self.include_patterns!r}, "
            f"exclude={self.exclude_patterns!r})"
        )


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


def _filter_manifest(
    manifest: BaseAssetManifest,
    entry_filter: Callable[[Union[BaseManifestPath, BaseManifestDirectoryPath]], bool],
) -> BaseAssetManifest:
    """
    Apply a filter to a manifest's entries.

    This operation:
    - Filters file/symlink entries using the provided filter
    - Filters directory entries using the provided filter (v2025+)
    - Returns a NEW manifest with only matching entries
    - Preserves manifest version and type

    Args:
        manifest: The manifest to filter
        entry_filter: A callable that takes a manifest entry and returns True to keep it

    Returns:
        A new manifest with only entries that pass the filter

    Example:
        # Using IncludeExcludePathsFilter
        filter = IncludeExcludePathsFilter(include=["*.blend"], exclude=["backup/*"])
        filtered = _filter_manifest(manifest, filter)

        # Using a custom filter
        def large_files_only(entry: Union[BaseManifestPath, BaseManifestDirectoryPath]) -> bool:
            if isinstance(entry, BaseManifestPath) and entry.size is not None:
                return entry.size > 1_000_000  # > 1MB
            return False
        filtered = _filter_manifest(manifest, large_files_only)

    Critical for Diff:
        When computing a diff manifest, BOTH parent and current must be filtered
        with the SAME filter before comparison. This ensures deletions are
        computed correctly within the filtered view.
    """
    if manifest.manifestVersion == ManifestVersion.v2023_03_03:
        if not isinstance(manifest, AssetManifest2023):
            raise TypeError(
                f"Expected AssetManifest2023 for version {manifest.manifestVersion}, "
                f"got {type(manifest).__name__}"
            )
        return _filter_manifest_v2023(manifest, entry_filter)
    elif manifest.manifestVersion == ManifestVersion.v2025_12_04_beta:
        if not isinstance(manifest, AssetManifest2025):
            raise TypeError(
                f"Expected AssetManifest2025 for version {manifest.manifestVersion}, "
                f"got {type(manifest).__name__}"
            )
        return _filter_manifest_v2025(manifest, entry_filter)
    else:
        raise ValueError(f"Unsupported manifest version: {manifest.manifestVersion}")


def _filter_manifest_v2023(
    manifest: AssetManifest2023,
    entry_filter: Callable[[Union[BaseManifestPath, BaseManifestDirectoryPath]], bool],
) -> AssetManifest2023:
    """
    Filter a v2023-03-03 manifest using the provided filter.

    v2023 format only has file entries (no directories or symlinks).
    """
    filtered_paths: List[ManifestPath2023] = []
    total_size = 0

    for entry in manifest.paths:
        if entry_filter(entry):
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
    entry_filter: Callable[[Union[BaseManifestPath, BaseManifestDirectoryPath]], bool],
) -> AssetManifest2025:
    """
    Filter a v2025-12-04-beta manifest using the provided filter.

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
        if entry_filter(entry):
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
        if entry_filter(dir_entry):
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
