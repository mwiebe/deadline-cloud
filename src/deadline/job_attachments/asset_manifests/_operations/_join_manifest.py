# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for joining a prefix to all paths in a manifest.

This module implements the JOIN operation from the composable manifest operations design:
    JOIN: (Manifest, prefix) → Manifest

The JOIN operation adds a prefix to all paths in a manifest, producing a new manifest
with prefixed paths. This is the inverse of the SUBTREE operation.

Key behaviors:
- Joins prefix to all file paths, directory paths, and symlink targets
- If prefix is absolute, output paths are absolute
- If prefix is relative, output paths remain relative (but prefixed)
"""

from __future__ import annotations

import os
import posixpath
from typing import Any, Callable, List

from ..base_manifest import BaseAssetManifest
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


def join_manifest(
    manifest: BaseAssetManifest,
    prefix: str,
    *,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> BaseAssetManifest:
    """
    Join a prefix to all paths in a manifest.

    Args:
        manifest: The source manifest to transform
        prefix: Path prefix to join to all paths (relative or absolute)
        print_function_callback: Progress callback for status messages

    Returns:
        A new manifest with:
        - All file paths prefixed
        - All directory paths prefixed
        - All symlink targets prefixed

    Raises:
        ValueError: If prefix is empty
    """
    # Normalize prefix path
    prefix = _normalize_prefix(prefix)
    if not prefix:
        raise ValueError("prefix cannot be empty")

    version = manifest.manifestVersion

    if version == ManifestVersion.v2023_03_03:
        if not isinstance(manifest, AssetManifest2023):
            raise TypeError(
                f"Expected AssetManifest2023 for version {version}, got {type(manifest).__name__}"
            )
        return _join_manifest_v2023(
            manifest=manifest,
            prefix=prefix,
            print_function_callback=print_function_callback,
        )
    elif version == ManifestVersion.v2025_12_04_beta:
        if not isinstance(manifest, AssetManifest2025):
            raise TypeError(
                f"Expected AssetManifest2025 for version {version}, got {type(manifest).__name__}"
            )
        return _join_manifest_v2025(
            manifest=manifest,
            prefix=prefix,
            print_function_callback=print_function_callback,
        )
    else:
        raise ValueError(f"Unsupported manifest version: {version}")


def _normalize_prefix(prefix: str) -> str:
    """Normalize the prefix path, removing trailing slashes and normalizing separators.

    On Windows, backslashes are converted to forward slashes (they are directory separators).
    On POSIX, backslashes are preserved (they are valid filename characters).
    """
    # Only convert backslashes to forward slashes on Windows
    if os.name == "nt":
        prefix = prefix.replace("\\", "/")
    # Remove trailing slash (but preserve leading slash for absolute paths)
    prefix = prefix.rstrip("/")
    return prefix


def _join_path(prefix: str, path: str) -> str:
    """Join prefix to a path using posixpath."""
    return posixpath.join(prefix, path)


def _join_manifest_v2023(
    manifest: AssetManifest2023,
    prefix: str,
    print_function_callback: Callable[[Any], None],
) -> AssetManifest2023:
    """
    Join prefix for v2023-03-03 manifests.

    v2023 format doesn't support symlinks or directories, so only file paths are prefixed.
    """
    result_paths: List[ManifestPath2023] = []

    for entry in manifest.paths:
        joined_path = _join_path(prefix, entry.path)
        result_paths.append(
            ManifestPath2023(
                path=joined_path,
                hash=entry.hash,
                size=entry.size,
                mtime=entry.mtime,
            )
        )
        print_function_callback(f"Joined: {entry.path} -> {joined_path}")

    return AssetManifest2023(
        hash_alg=manifest.hashAlg,
        paths=result_paths,
        total_size=manifest.totalSize,
    )


def _join_manifest_v2025(
    manifest: AssetManifest2025,
    prefix: str,
    print_function_callback: Callable[[Any], None],
) -> AssetManifest2025:
    """
    Join prefix for v2025-12-04-beta manifests.

    Handles:
    - File paths: prefixed
    - Directory paths: prefixed
    - Symlink targets: prefixed
    - Deleted markers: prefixed
    """
    result_paths: List[ManifestFilePath2025] = []
    result_dirs: List[ManifestDirectoryPath2025] = []

    # Process directories
    for dir_entry in manifest.dirs:
        joined_path = _join_path(prefix, dir_entry.path)
        result_dirs.append(
            ManifestDirectoryPath2025(
                path=joined_path,
                deleted=dir_entry.deleted,
            )
        )

    # Process files and symlinks
    for entry in manifest.paths:
        joined_path = _join_path(prefix, entry.path)

        # Handle symlink targets
        joined_target = None
        if entry.symlink_target is not None:
            joined_target = _join_path(prefix, entry.symlink_target)

        result_paths.append(
            ManifestFilePath2025(
                path=joined_path,
                hash=entry.hash,
                size=entry.size,
                mtime=entry.mtime,
                runnable=entry.runnable,
                chunkhashes=entry.chunkhashes,
                symlink_target=joined_target,
                deleted=entry.deleted,
            )
        )
        if entry.symlink_target is not None:
            print_function_callback(
                f"Joined symlink: {entry.path} -> {joined_path} (target: {joined_target})"
            )
        else:
            print_function_callback(f"Joined: {entry.path} -> {joined_path}")

    return AssetManifest2025(
        hash_alg=manifest.hashAlg,
        dirs=result_dirs,
        paths=result_paths,
        total_size=manifest.totalSize,
        manifest_type=manifest.manifestType,
        parent_manifest_hash=manifest.parentManifestHash,
    )
