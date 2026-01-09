# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for joining a prefix to all paths in a manifest.

This module implements the JOIN operation from the composable manifest operations design:
    JOIN: (RelManifest, prefix) → AnyManifest

The JOIN operation adds a prefix to all paths in a manifest, producing a new manifest
with prefixed paths. This is the inverse of the SUBTREE operation.

Key behaviors:
- Input must be a relative-path manifest (Snapshot or SnapshotDiff)
- Joins prefix to all file paths, directory paths, and symlink targets
- If prefix is absolute, output is an absolute-path manifest (AbsSnapshot or AbsSnapshotDiff)
- If prefix is relative, output is a relative-path manifest (Snapshot or SnapshotDiff)

All composable operations use v2025 structure and semantics internally. Support for
v2023 on-disk format is provided via lossy conversion functions that drop symlinks,
deletions, and other v2025-only features.
"""

from __future__ import annotations

import os
import posixpath
from typing import Any, Callable, List

from .._manifest import (
    AbsSnapshot,
    AbsSnapshotDiff,
    AnyManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    Snapshot,
    SnapshotDiff,
    RelManifest,
    _is_absolute_path,
)


def join_manifest(
    manifest: RelManifest,
    prefix: str,
    *,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> AnyManifest:
    """
    Join a prefix to all paths in a manifest.

    Args:
        manifest: The source manifest to transform (must be Snapshot or SnapshotDiff)
        prefix: Path prefix to join to all paths (relative or absolute)
        print_function_callback: Progress callback for status messages

    Returns:
        A new manifest with all paths prefixed:
        - If prefix is absolute: AbsSnapshot or AbsSnapshotDiff
        - If prefix is relative: Snapshot or SnapshotDiff
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


def _get_output_manifest_type(
    manifest: RelManifest, prefix: str
) -> type[AbsSnapshot] | type[AbsSnapshotDiff] | type[Snapshot] | type[SnapshotDiff]:
    """Determine the output manifest type based on input type and prefix.

    - If prefix is absolute: output is AbsSnapshot or AbsSnapshotDiff
    - If prefix is relative: output is Snapshot or SnapshotDiff
    - Snapshot/Diff type is preserved from input
    """
    prefix_is_absolute = _is_absolute_path(prefix)
    is_snapshot = isinstance(manifest, Snapshot)

    if prefix_is_absolute:
        return AbsSnapshot if is_snapshot else AbsSnapshotDiff
    else:
        return Snapshot if is_snapshot else SnapshotDiff


def _join_path(prefix: str, path: str) -> str:
    """Join prefix to a path using posixpath."""
    return posixpath.join(prefix, path)
