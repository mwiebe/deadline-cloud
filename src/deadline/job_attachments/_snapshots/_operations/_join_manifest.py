# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for joining a prefix to all paths in a manifest.

This module implements the JOIN operation from the composable manifest operations design:
    JOIN: (RelManifest, prefix) → Manifest

The JOIN operation adds a prefix to all paths in a manifest, producing a new manifest
with prefixed paths. This is the inverse of the SUBTREE operation.

Key behaviors:
- Input must be a relative-path manifest (RelSnapshotManifest or RelDiffManifest)
- Joins prefix to all file paths, directory paths, and symlink targets
- If prefix is absolute, output is an absolute-path manifest (AbsSnapshotManifest or AbsDiffManifest)
- If prefix is relative, output is a relative-path manifest (RelSnapshotManifest or RelDiffManifest)

All composable operations use v2025 structure and semantics internally. Support for
v2023 on-disk format is provided via lossy conversion functions that drop symlinks,
deletions, and other v2025-only features.
"""

from __future__ import annotations

import os
import posixpath
from typing import Any, Callable, List

from .._manifest import (
    AbsDiffManifest,
    AbsSnapshotManifest,
    Manifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    RelDiffManifest,
    RelManifest,
    RelSnapshotManifest,
)


def join_manifest(
    manifest: RelManifest,
    prefix: str,
    *,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> Manifest:
    """
    Join a prefix to all paths in a manifest.

    Args:
        manifest: The source manifest to transform (must be RelSnapshotManifest or RelDiffManifest)
        prefix: Path prefix to join to all paths (relative or absolute)
        print_function_callback: Progress callback for status messages

    Returns:
        A new manifest with all paths prefixed:
        - If prefix is absolute: AbsSnapshotManifest or AbsDiffManifest
        - If prefix is relative: RelSnapshotManifest or RelDiffManifest
        The snapshot/diff type is preserved from the input.

    Raises:
        ValueError: If prefix is empty
    """
    # Normalize prefix path
    prefix = _normalize_prefix(prefix)
    if not prefix:
        raise ValueError("prefix cannot be empty")

    result_paths: List[ManifestFilePath] = []
    result_dirs: List[ManifestDirectoryPath] = []

    # Process directories
    for dir_entry in manifest.dirs:
        joined_path = _join_path(prefix, dir_entry.path)
        result_dirs.append(
            ManifestDirectoryPath(
                path=joined_path,
                deleted=dir_entry.deleted,
            )
        )

    # Process files and symlinks
    for entry in manifest.files:
        joined_path = _join_path(prefix, entry.path)

        # Handle symlink targets
        joined_target = None
        if entry.symlink_target is not None:
            joined_target = _join_path(prefix, entry.symlink_target)

        result_paths.append(
            ManifestFilePath(
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

    # Determine output type based on prefix (absolute vs relative) and input type (snapshot vs diff)
    output_type = _get_output_manifest_type(manifest, prefix)
    return output_type(
        hash_alg=manifest.hashAlg,
        dirs=result_dirs,
        files=result_paths,
        total_size=manifest.totalSize,
        parent_manifest_hash=manifest.parentManifestHash,
    )


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


def _is_absolute_path(path: str) -> bool:
    """Check if a path is absolute.

    Handles both POSIX and Windows-style absolute paths.
    """
    # POSIX absolute
    if path.startswith("/"):
        return True
    # Windows drive letter (e.g., C:/)
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return True
    # Windows UNC path (e.g., //server/share)
    if path.startswith("//"):
        return True
    return False


def _get_output_manifest_type(
    manifest: RelManifest, prefix: str
) -> (
    type[AbsSnapshotManifest]
    | type[AbsDiffManifest]
    | type[RelSnapshotManifest]
    | type[RelDiffManifest]
):
    """Determine the output manifest type based on input type and prefix.

    - If prefix is absolute: output is Abs*Manifest
    - If prefix is relative: output is Rel*Manifest
    - Snapshot/Diff type is preserved from input
    """
    prefix_is_absolute = _is_absolute_path(prefix)
    is_snapshot = isinstance(manifest, RelSnapshotManifest)

    if prefix_is_absolute:
        return AbsSnapshotManifest if is_snapshot else AbsDiffManifest
    else:
        return RelSnapshotManifest if is_snapshot else RelDiffManifest


def _join_path(prefix: str, path: str) -> str:
    """Join prefix to a path using posixpath."""
    return posixpath.join(prefix, path)
