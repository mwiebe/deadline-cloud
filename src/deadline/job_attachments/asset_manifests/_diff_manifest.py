# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for computing diff manifests between two snapshot manifests.

This module implements the DIFF operation from the composable manifest operations design:
    DIFF: (Parent Snapshot, Current Snapshot) → Diff Manifest

The diff manifest contains:
- New entries (in current but not parent)
- Modified entries (in both, but different hash/content)
- Deleted entries (in parent but not current) with deleted=True markers

CRITICAL PRECONDITIONS:
1. Both parent and current manifests MUST be filtered with the SAME filter
   before calling _compute_diff_manifest(). This ensures deletions are computed
   correctly within the filtered view.

2. Both manifests should already have hashes computed (via _hash_manifest()).
   This function does NOT compute hashes - it only compares existing hashes.

The composable operations flow for diff:
    Parent File ──[load]──► Parent ──[filter]──► Filtered Parent ──┐
                                                                   ├──[diff]──► Diff Manifest
    Directory ──[collect]──► Unhashed ──[hash]──► Hashed ──[filter]──► Filtered Current ─┘
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set

from .base_manifest import BaseAssetManifest, BaseManifestPath
from .versions import ManifestType, ManifestVersion
from .v2023_03_03.asset_manifest import (
    AssetManifest as AssetManifest2023,
    ManifestPath as ManifestPath2023,
)
from .v2025_12_04.asset_manifest import (
    AssetManifest as AssetManifest2025,
    ManifestDirectoryPath as ManifestDirectoryPath2025,
    ManifestFilePath as ManifestFilePath2025,
)


def _compute_diff_manifest(
    parent: BaseAssetManifest,
    current: BaseAssetManifest,
    parent_manifest_hash: Optional[str] = None,
    ignore_hashes: bool = False,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> BaseAssetManifest:
    """
    Compute the difference between two snapshot manifests.

    PRECONDITIONS:
    1. Both manifests should be filtered with the same patterns before calling
       this function. This ensures deletions are computed correctly within the
       filtered view.
    2. Both manifests should already have hashes computed (via _hash_manifest())
       unless ignore_hashes=True. When ignore_hashes=True, comparison is done by
       metadata only (size, mtime, runnable).

    Args:
        parent: The parent snapshot manifest (filtered, with hashes)
        current: The current snapshot manifest (filtered, with hashes)
        parent_manifest_hash: Optional hash of the parent manifest (for v2025 diff manifests).
                             The caller is responsible for computing this hash from the
                             original parent manifest string if needed.
        ignore_hashes: If True, ignore hash/chunkhashes when comparing entries.
                      Useful for fast diff mode where only metadata (mtime, size, runnable)
                      is compared without hashing files.
        print_function_callback: Progress callback

    Returns:
        A diff manifest with:
        - manifestType=DIFF (v2025 only)
        - parentManifestHash if provided (v2025 only)
        - New/modified entries with full content
        - Deleted entries with deleted=True markers (v2025 only)

    Raises:
        ValueError: If versions don't match

    Example:
        # Composable operations flow:
        parent_str = load_manifest_string(path)
        parent = decode_manifest(parent_str)
        parent_hash = hash_data(parent_str.encode("utf-8"), HashAlgorithm.XXH128)
        filtered_parent = _filter_manifest(parent, filter_obj)

        current_unhashed = _collect_manifest_structure(root, version)
        current_hashed = _hash_manifest(current_unhashed, root)
        filtered_current = _filter_manifest(current_hashed, filter_obj)

        diff = _compute_diff_manifest(filtered_parent, filtered_current, parent_hash)
    """
    # Validate version match
    if parent.manifestVersion != current.manifestVersion:
        raise ValueError(
            f"Parent manifest version ({parent.manifestVersion.value}) does not match "
            f"current manifest version ({current.manifestVersion.value})"
        )

    version = parent.manifestVersion

    if version == ManifestVersion.v2023_03_03:
        if not isinstance(parent, AssetManifest2023) or not isinstance(current, AssetManifest2023):
            raise TypeError(
                f"Expected AssetManifest2023 for version {version}, "
                f"got parent={type(parent).__name__}, current={type(current).__name__}"
            )
        return _compute_diff_manifest_v2023(
            parent=parent,
            current=current,
            ignore_hashes=ignore_hashes,
            print_function_callback=print_function_callback,
        )
    elif version == ManifestVersion.v2025_12_04_beta:
        if not isinstance(parent, AssetManifest2025) or not isinstance(current, AssetManifest2025):
            raise TypeError(
                f"Expected AssetManifest2025 for version {version}, "
                f"got parent={type(parent).__name__}, current={type(current).__name__}"
            )
        return _compute_diff_manifest_v2025(
            parent=parent,
            current=current,
            parent_manifest_hash=parent_manifest_hash,
            ignore_hashes=ignore_hashes,
            print_function_callback=print_function_callback,
        )
    else:
        raise ValueError(f"Unsupported manifest version: {version}")


def _compute_diff_manifest_v2023(
    parent: AssetManifest2023,
    current: AssetManifest2023,
    ignore_hashes: bool,
    print_function_callback: Callable[[Any], None],
) -> AssetManifest2023:
    """
    Compute diff for v2023-03-03 manifests.

    Note: v2023 format doesn't have a diff manifest type with deletion markers.
    This returns a snapshot manifest containing only the changed files (new + modified).
    Deletions are NOT tracked in v2023 format.

    Comparison is done by hash (unless ignore_hashes=True), size, and mtime.
    When ignore_hashes=False, both manifests must already have hashes computed.
    """
    # Build path lookups
    parent_paths: Dict[str, ManifestPath2023] = {p.path: p for p in parent.paths}
    current_paths: Dict[str, ManifestPath2023] = {p.path: p for p in current.paths}

    # Find changes
    parent_path_set = set(parent_paths.keys())
    current_path_set = set(current_paths.keys())

    new_paths = current_path_set - parent_path_set
    common_paths = parent_path_set & current_path_set
    # Note: v2023 format doesn't support deletion markers, so no deleted_paths

    # Find modified entries (by hash, size, or mtime comparison)
    modified_paths: Set[str] = set()
    for path in common_paths:
        parent_entry = parent_paths[path]
        current_entry = current_paths[path]
        # Check if any content or metadata changed
        if ignore_hashes:
            # Fast mode: only compare metadata (size, mtime)
            if (parent_entry.size != current_entry.size or
                parent_entry.mtime != current_entry.mtime):
                modified_paths.add(path)
        else:
            # Full mode: compare hash and metadata
            if (parent_entry.hash != current_entry.hash or
                parent_entry.size != current_entry.size or
                parent_entry.mtime != current_entry.mtime):
                modified_paths.add(path)

    # Build result entries
    result_entries: List[ManifestPath2023] = []
    total_size = 0

    # Process new and modified files
    changed_paths = new_paths | modified_paths
    for path in sorted(changed_paths):
        entry = current_paths[path]

        # v2023 entries always have size and mtime (non-optional in this format)
        if entry.size is None:
            raise ValueError(f"v2023 entry {path} missing size")
        if entry.mtime is None:
            raise ValueError(f"v2023 entry {path} missing mtime")

        result_entries.append(entry)
        total_size += entry.size

        if path in new_paths:
            print_function_callback(f"New: {path}")
        else:
            print_function_callback(f"Modified: {path}")

    return AssetManifest2023(
        hash_alg=parent.hashAlg,
        paths=result_entries,
        total_size=total_size,
    )


def _compute_diff_manifest_v2025(
    parent: AssetManifest2025,
    current: AssetManifest2025,
    parent_manifest_hash: Optional[str],
    ignore_hashes: bool,
    print_function_callback: Callable[[Any], None],
) -> AssetManifest2025:
    """
    Compute diff for v2025-12-04-beta manifests.

    Creates a diff manifest with:
    - manifestType=DIFF
    - parentManifestHash (if provided)
    - New/modified entries with full content
    - Deleted entries with deleted=True markers

    Comparison is done by hash (unless ignore_hashes=True) and metadata.
    When ignore_hashes=False, both manifests must already have hashes computed.
    """

    # Build path lookups for files
    parent_file_paths: Dict[str, ManifestFilePath2025] = {p.path: p for p in parent.paths}
    current_file_paths: Dict[str, ManifestFilePath2025] = {p.path: p for p in current.paths}

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

        if _entries_differ(parent_entry, current_entry, ignore_hashes=ignore_hashes):
            modified_files.add(path)

    # Build file entries for diff manifest
    file_entries: List[ManifestFilePath2025] = []
    total_size = 0

    # Process new and modified files
    changed_files = new_files | modified_files
    for path in sorted(changed_files):
        entry = current_file_paths[path]

        # Copy the entry to the diff manifest
        file_entries.append(
            ManifestFilePath2025(
                path=entry.path,
                hash=entry.hash,
                size=entry.size,
                mtime=entry.mtime,
                runnable=entry.runnable,
                chunkhashes=entry.chunkhashes,
                symlink_target=entry.symlink_target,
            )
        )

        # Count size for non-symlink entries
        if entry.symlink_target is None and entry.size is not None:
            total_size += entry.size

        if path in new_files:
            print_function_callback(f"New: {path}")
        else:
            print_function_callback(f"Modified: {path}")

    # Add deletion markers for deleted files
    for path in sorted(deleted_files):
        file_entries.append(ManifestFilePath2025(path=path, deleted=True))
        print_function_callback(f"Deleted: {path}")

    # Build directory entries
    dir_entries: List[ManifestDirectoryPath2025] = []

    # Add new directories
    for path in sorted(new_dirs):
        dir_entries.append(ManifestDirectoryPath2025(path=path))

    # Add deletion markers for deleted directories
    for path in sorted(deleted_dirs):
        dir_entries.append(ManifestDirectoryPath2025(path=path, deleted=True))
        print_function_callback(f"Deleted dir: {path}")

    return AssetManifest2025(
        hash_alg=parent.hashAlg,
        dirs=dir_entries,
        paths=file_entries,
        total_size=total_size,
        manifest_type=ManifestType.DIFF,
        parent_manifest_hash=parent_manifest_hash,
    )


def _entries_differ(parent: BaseManifestPath, current: BaseManifestPath, ignore_hashes: bool = False) -> bool:
    """
    Check if two file entries differ in any meaningful way.

    Works for both v2023 and v2025 manifest formats.

    Compares:
    - Entry type (regular file vs symlink)
    - For regular/chunked files: hash/chunkhashes (unless ignore_hashes=True), size, mtime, runnable
    - For symlinks: symlink_target

    Type transitions are always considered different:
    - Regular file → symlink
    - Symlink → regular file

    Args:
        parent: Parent manifest entry
        current: Current manifest entry
        ignore_hashes: If True, ignore hash/chunkhashes comparison (fast mode)
    """
    # Determine entry types
    parent_is_symlink = parent.symlink_target is not None
    current_is_symlink = current.symlink_target is not None

    # Type transition: symlink status changed
    if parent_is_symlink != current_is_symlink:
        return True

    # Symlink comparison (only target matters)
    if parent_is_symlink and current_is_symlink:
        return parent.symlink_target != current.symlink_target

    # Regular/chunked file comparison
    # Note: A file can transition between regular (hash) and chunked (chunkhashes)
    # based on size crossing the 256MB threshold. We treat this as a content change.

    # Check metadata (always checked)
    if parent.size != current.size:
        return True
    if parent.mtime != current.mtime:
        return True
    if parent.runnable != current.runnable:
        return True

    # Check content (hash or chunkhashes) unless ignore_hashes is True
    if not ignore_hashes:
        if parent.hash != current.hash:
            return True
        if parent.chunkhashes != current.chunkhashes:
            return True

    return False
