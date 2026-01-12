# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for computing diff manifests between two snapshot manifests.

This module implements the DIFF operation from the composable manifest operations design:
    DIFF: (Parent AnySnapshot, Current AnySnapshot) → AnyDiff

The diff manifest contains:
- New entries (in current but not parent)
- Modified entries (in both, but different hash/content)
- Deleted entries (in parent but not current) with deleted=True markers

CRITICAL PRECONDITIONS:
1. Both parent and current manifests MUST be filtered with the SAME filter
   before calling compute_diff_manifest(). This ensures deletions are computed
   correctly within the filtered view.

2. If ignore_hashes=False (default), both manifests must have hashes computed
   (via hash_abs_manifest()). If ignore_hashes=True, hashes are not required—
   comparison is done by metadata only (size, mtime, runnable).

All composable operations use v2025 structure and semantics internally. Support for
v2023 on-disk format is provided via lossy conversion functions that drop symlinks,
deletions, and other v2025-only features.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set

from .._manifest import (
    AbsSnapshot,
    AbsSnapshotDiff,
    AnyDiff,
    AnySnapshot,
    ManifestDirectoryPath,
    ManifestFilePath,
    SnapshotDiff,
)

logger = logging.getLogger(__name__)


def _entries_differ(
    parent: ManifestFilePath,
    current: ManifestFilePath,
    *,
    ignore_hashes: bool = False,
    ignore_runnable: bool = False,
) -> bool:
    """
    Compare two manifest file entries to determine if they differ.

    Comparison is done by:
    1. Entry type (regular file vs chunked file vs symlink)
    2. Content hash (hash or chunkhashes, unless ignore_hashes=True)
    3. Metadata (mtime, size, runnable)

    Args:
        parent: The parent manifest entry
        current: The current manifest entry
        ignore_hashes: If True, skip hash/chunkhashes comparison
        ignore_runnable: If True, skip runnable comparison (used with preserve_runnable)

    Returns:
        True if entries differ, False if they are the same
    """
    # Check for type transitions (regular <-> chunked <-> symlink)
    parent_is_symlink = parent.symlink_target is not None
    current_is_symlink = current.symlink_target is not None
    parent_is_chunked = parent.chunkhashes is not None
    current_is_chunked = current.chunkhashes is not None

    # Type transition = different
    if parent_is_symlink != current_is_symlink:
        return True
    if parent_is_chunked != current_is_chunked:
        return True

    # Symlink comparison
    if parent_is_symlink and current_is_symlink:
        return parent.symlink_target != current.symlink_target

    # Chunked file comparison
    if parent_is_chunked and current_is_chunked:
        if not ignore_hashes and parent.chunkhashes != current.chunkhashes:
            return True
        if parent.mtime != current.mtime:
            return True
        if parent.size != current.size:
            return True
        if not ignore_runnable and parent.runnable != current.runnable:
            return True
        return False

    # Regular file comparison
    if not ignore_hashes and parent.hash != current.hash:
        return True
    if parent.mtime != current.mtime:
        return True
    if parent.size != current.size:
        return True
    if not ignore_runnable and parent.runnable != current.runnable:
        return True

    return False


def compute_diff_manifest(
    parent: AnySnapshot,
    current: AnySnapshot,
    parent_manifest_hash: Optional[str] = None,
    ignore_hashes: bool = False,
    *,
    preserve_runnable: bool = False,
) -> AnyDiff:
    """
    Compute the difference between two snapshot manifests.

    PRECONDITIONS:
    1. Both manifests should be filtered with the same patterns before calling
       this function. This ensures deletions are computed correctly within the
       filtered view.
    2. If ignore_hashes=False (default), both manifests must have hashes computed
       (via hash_abs_manifest()). If ignore_hashes=True, hashes are not required—
       comparison is done by metadata only (size, mtime, runnable).

    Args:
        parent: The parent snapshot manifest
        current: The current snapshot manifest
        parent_manifest_hash: Optional hash of the parent manifest. The caller is
                             responsible for computing this hash from the original
                             parent manifest string if needed.
        ignore_hashes: If True, ignore hash/chunkhashes when comparing entries.
                      Useful for fast diff mode where only metadata (mtime, size, runnable)
                      is compared without hashing files.
        preserve_runnable: If True, copy the 'runnable' field from the parent entry
                          when a file is modified. This is useful on Windows where the
                          execute bit is not supported by the filesystem—if the parent
                          manifest came from POSIX with runnable=True, we want to preserve
                          that value rather than losing it when collecting on Windows.
                          Default is False.

    Returns:
        A diff manifest (AbsSnapshotDiff or SnapshotDiff) with:
        - parentManifestHash if provided
        - New/modified entries with full content
        - Deleted entries with deleted=True markers

    Raises:
        ValueError: If path types don't match (both absolute or both relative)
    """
    # Validate path types match
    parent_is_abs = isinstance(parent, AbsSnapshot)
    current_is_abs = isinstance(current, AbsSnapshot)
    if parent_is_abs != current_is_abs:
        raise ValueError(
            "Parent and current manifests must have the same path type "
            "(both absolute or both relative)"
        )

    return _compute_diff_manifest(
        parent=parent,
        current=current,
        parent_manifest_hash=parent_manifest_hash,
        ignore_hashes=ignore_hashes,
        preserve_runnable=preserve_runnable,
        is_absolute=parent_is_abs,
    )


def _compute_diff_manifest(
    parent: AnySnapshot,
    current: AnySnapshot,
    parent_manifest_hash: Optional[str],
    ignore_hashes: bool,
    *,
    preserve_runnable: bool = False,
    is_absolute: bool = True,
) -> AnyDiff:
    """
    Compute diff between two snapshot manifests.

    Creates a diff manifest (AbsSnapshotDiff or SnapshotDiff) with:
    - parentManifestHash (if provided)
    - New/modified entries with full content
    - Deleted entries with deleted=True markers

    Comparison is done by hash (unless ignore_hashes=True) and metadata.
    When ignore_hashes=False, both manifests must already have hashes computed.

    Directory Deletion Semantics:
    A directory deletion marker means "delete this empty directory". To delete
    a non-empty directory, all its contents must be explicitly deleted first:
    - All files and symlinks within the directory
    - All subdirectories (recursively, following the same rule)
    - Finally, the directory itself

    This ensures diff manifests are fully composable—each deletion is
    self-contained and doesn't depend on knowing the parent snapshot's contents.

    Args:
        parent: The parent snapshot manifest
        current: The current snapshot manifest
        parent_manifest_hash: Optional hash of the parent manifest
        ignore_hashes: If True, ignore hash/chunkhashes comparison
        preserve_runnable: If True, copy 'runnable' from parent for modified files.
                          This preserves POSIX execute bits when diffing on Windows.
        is_absolute: Whether the manifests use absolute paths
    """
    # Build path lookups for files
    parent_file_paths: Dict[str, ManifestFilePath] = {p.path: p for p in parent.files}
    current_file_paths: Dict[str, ManifestFilePath] = {p.path: p for p in current.files}

    # Build path sets for directories
    parent_dir_paths: Set[str] = {d.path for d in parent.dirs}
    current_dir_paths: Set[str] = {d.path for d in current.dirs}

    # Find file changes
    parent_file_set = set(parent_file_paths.keys())
    current_file_set = set(current_file_paths.keys())

    new_files = current_file_set - parent_file_set
    deleted_files = parent_file_set - current_file_set
    common_files = parent_file_set & current_file_set

    # Find directory changes
    new_dirs = current_dir_paths - parent_dir_paths
    deleted_dirs = parent_dir_paths - current_dir_paths

    # Find modified files (by content comparison)
    modified_files: Set[str] = set()
    for path in common_files:
        parent_entry = parent_file_paths[path]
        current_entry = current_file_paths[path]

        if _entries_differ(
            parent_entry,
            current_entry,
            ignore_hashes=ignore_hashes,
            ignore_runnable=preserve_runnable,
        ):
            modified_files.add(path)

    # Ensure deleted directories have all their contents deleted too.
    # For each deleted directory, find all files and subdirectories that were
    # contained within it in the parent manifest and add them to the deleted sets.
    for deleted_dir in list(deleted_dirs):
        dir_prefix = deleted_dir + "/"

        # Find all files within this deleted directory
        for file_path in parent_file_set:
            if file_path.startswith(dir_prefix) and file_path not in current_file_set:
                deleted_files.add(file_path)

        # Find all subdirectories within this deleted directory
        for dir_path in parent_dir_paths:
            if dir_path.startswith(dir_prefix) and dir_path not in current_dir_paths:
                deleted_dirs.add(dir_path)

    # Build file entries for diff manifest
    file_entries: List[ManifestFilePath] = []
    total_size = 0

    # Process new and modified files
    changed_files = new_files | modified_files
    for path in sorted(changed_files):
        entry = current_file_paths[path]

        # Determine the runnable value to use
        runnable_value = entry.runnable
        if preserve_runnable and path in modified_files:
            # For modified files, preserve runnable from parent when requested
            parent_entry = parent_file_paths[path]
            runnable_value = parent_entry.runnable

        # Copy the entry to the diff manifest
        file_entries.append(
            ManifestFilePath(
                path=entry.path,
                hash=entry.hash,
                size=entry.size,
                mtime=entry.mtime,
                runnable=runnable_value,
                chunkhashes=entry.chunkhashes,
                symlink_target=entry.symlink_target,
            )
        )

        # Count size for non-symlink entries
        if entry.symlink_target is None and entry.size is not None:
            total_size += entry.size

        if path in new_files:
            logger.debug("New: %s", path)
        else:
            logger.debug("Modified: %s", path)

    # Add deletion markers for deleted files
    for path in sorted(deleted_files):
        file_entries.append(ManifestFilePath(path=path, deleted=True))
        logger.debug("Deleted: %s", path)

    # Build directory entries
    dir_entries: List[ManifestDirectoryPath] = []

    # Add new directories
    for path in sorted(new_dirs):
        dir_entries.append(ManifestDirectoryPath(path=path))

    # Add deletion markers for deleted directories
    # Sort by path length descending so subdirectories come before parents
    for path in sorted(deleted_dirs, key=lambda p: (-len(p), p)):
        dir_entries.append(ManifestDirectoryPath(path=path, deleted=True))
        logger.debug("Deleted dir: %s", path)

    # Return the appropriate diff manifest type
    output_type = AbsSnapshotDiff if is_absolute else SnapshotDiff
    return output_type(
        hash_alg=parent.hashAlg,
        dirs=dir_entries,
        files=file_entries,
        total_size=total_size,
        parent_manifest_hash=parent_manifest_hash,
    )
