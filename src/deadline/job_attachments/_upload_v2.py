# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Refactored upload functions using the snapshots composable operations.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from ._snapshots import (
    AbsSnapshot,
    Snapshot,
    SymlinkPolicy,
    filter_manifest,
    partition_manifest,
)
from ._snapshots._operations._partition_manifest import _is_path_under_root
from .models import FileSystemLocationType, StorageProfile


@dataclass
class PartitionedAssetGroup:
    """Result of partitioning an AbsSnapshot by storage profile locations."""

    root_path: str
    manifest: Snapshot  # Relative-path manifest for this root
    outputs: List[str]  # Output paths relative to root
    file_system_location_name: Optional[str] = None


def _normalize_to_forward_slashes(path: str) -> str:
    """Normalize a path to use forward slashes (for internal comparisons)."""
    if os.name == "nt":
        return path.replace("\\", "/")
    return path


def _get_file_system_locations_by_type(
    storage_profile: StorageProfile,
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Extracts LOCAL and SHARED file system locations from a storage profile.
    Returns (local_locations, shared_locations) where each is a dict of path -> name.
    """
    local_locations: dict[str, str] = {}
    shared_locations: dict[str, str] = {}
    for fs_loc in storage_profile.fileSystemLocations:
        if fs_loc.type == FileSystemLocationType.LOCAL:
            local_locations[fs_loc.path] = fs_loc.name
        elif fs_loc.type == FileSystemLocationType.SHARED:
            shared_locations[fs_loc.path] = fs_loc.name
    return local_locations, shared_locations


def _get_relative_path(path: str, root: str) -> str:
    """Get path relative to root using string operations (cross-platform safe).

    Both path and root should use forward slashes for comparison.
    """
    if path == root:
        return "."
    if not root.endswith("/"):
        root = root + "/"
    if path.startswith(root):
        return path[len(root) :]
    raise ValueError(f"Path {path} is not under root {root}")


def partition_snapshot_by_storage_profile(
    manifest: AbsSnapshot,
    output_paths: List[str],
    referenced_paths: List[str],
    storage_profile: Optional[StorageProfile] = None,
) -> List[PartitionedAssetGroup]:
    """
    Partitions an AbsSnapshot by storage profile locations.

    1. Filters out entries under SHARED locations
    2. Partitions remaining entries by LOCAL locations (or auto-determines roots)
    3. Associates output paths with their respective roots

    Args:
        manifest: An AbsSnapshot containing the collected input files
        output_paths: List of output directory paths
        referenced_paths: List of referenced paths (affects root determination)
        storage_profile: Optional storage profile defining LOCAL/SHARED locations

    Returns:
        List of PartitionedAssetGroup, one per root path
    """
    # Get storage profile locations
    local_locations: dict[str, str] = {}
    shared_locations: dict[str, str] = {}
    if storage_profile:
        local_locations, shared_locations = _get_file_system_locations_by_type(storage_profile)

    # Filter out entries under SHARED locations
    filtered_manifest = manifest
    if shared_locations:

        def not_under_shared(entry) -> bool:
            return not any(_is_path_under_root(entry.path, shared) for shared in shared_locations)

        filtered_manifest = filter_manifest(manifest, not_under_shared)

    # Filter output_paths and referenced_paths
    filtered_outputs = [
        p for p in output_paths if not any(_is_path_under_root(p, s) for s in shared_locations)
    ]
    filtered_refs = [
        p for p in referenced_paths if not any(_is_path_under_root(p, s) for s in shared_locations)
    ]

    # Partition by LOCAL locations
    partitions = partition_manifest(
        filtered_manifest,
        roots=list(local_locations.keys()) if local_locations else None,
        referenced_paths=filtered_outputs + filtered_refs,
        symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
    )

    # Build result with outputs associated to each root
    result = []
    for root, rel_manifest in partitions:
        # Normalize root to forward slashes for comparisons
        # (partition_manifest returns native separators on Windows)
        root_normalized = _normalize_to_forward_slashes(root)

        # Find outputs under this root and make them relative
        root_outputs = []
        for p in filtered_outputs:
            # Normalize output path to forward slashes for comparison
            p_normalized = _normalize_to_forward_slashes(p)
            if _is_path_under_root(p_normalized, root_normalized):
                try:
                    root_outputs.append(_get_relative_path(p_normalized, root_normalized))
                except ValueError:
                    pass

        # partition_manifest returns Snapshot when input is AbsSnapshot
        assert isinstance(rel_manifest, Snapshot)
        result.append(
            PartitionedAssetGroup(
                root_path=root,
                manifest=rel_manifest,
                outputs=sorted(root_outputs),
                file_system_location_name=local_locations.get(root_normalized),
            )
        )

    return result
