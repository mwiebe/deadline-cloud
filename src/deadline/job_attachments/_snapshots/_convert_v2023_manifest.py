# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Conversion functions between snapshot manifests and v2023-03-03 BaseAssetManifest.

The v2023 format has limitations compared to the snapshot format:
- No symlinks (must be collapsed)
- No empty directories (dropped with warning)
- No deletions (dropped with warning for diffs)
- No chunked files (must use WHOLE_FILE_CHUNK_SIZE)

These conversions are lossy when going from snapshot to v2023.
"""

from __future__ import annotations

import logging
from typing import cast, List

from ._manifest import (
    Snapshot,
    SnapshotDiff,
    ManifestFilePath,
    SymlinkPolicy,
    WHOLE_FILE_CHUNK_SIZE,
)
from ._operations._subtree_manifest import subtree_manifest
from ..asset_manifests.base_manifest import BaseManifestPath
from ..asset_manifests.v2023_03_03 import AssetManifest, ManifestPath

logger = logging.getLogger(__name__)


def get_implied_directories(files: List[ManifestFilePath]) -> set[str]:
    """
    Get all directories implied by file paths.

    For each non-deleted file/symlink, extracts all parent directories recursively.
    E.g., "a/b/c/file.txt" implies directories "a/b/c", "a/b", and "a".
    """
    implied: set[str] = set()
    for entry in files:
        if entry.deleted:
            continue
        path = entry.path
        last_slash = path.rfind("/")
        while last_slash > 0:
            parent = path[:last_slash]
            if parent in implied:
                break
            implied.add(parent)
            last_slash = parent.rfind("/")
    return implied


def snapshot_to_v2023_manifest(snapshot: Snapshot) -> AssetManifest:
    """
    Convert a Snapshot to a v2023-03-03 AssetManifest.

    Uses COLLAPSE_ALL symlink policy via subtree_manifest with "." to collapse
    all symlinks. Empty directories are dropped with a warning.

    Args:
        snapshot: A Snapshot with relative paths and hashes computed.
                  Must have fileChunkSizeBytes=WHOLE_FILE_CHUNK_SIZE.

    Returns:
        A v2023-03-03 AssetManifest.

    Raises:
        ValueError: If fileChunkSizeBytes is not WHOLE_FILE_CHUNK_SIZE.
        ValueError: If any file is missing a hash.
    """
    if snapshot.fileChunkSizeBytes != WHOLE_FILE_CHUNK_SIZE:
        raise ValueError(
            f"v2023 format requires fileChunkSizeBytes={WHOLE_FILE_CHUNK_SIZE}, "
            f"got {snapshot.fileChunkSizeBytes}."
        )

    # Use subtree_manifest with "." to apply COLLAPSE_ALL without rebasing paths
    collapsed = subtree_manifest(snapshot, ".", symlink_policy=SymlinkPolicy.COLLAPSE_ALL)

    # Warn about truly empty directories being dropped (those not implied by file paths)
    if collapsed.dirs:
        implied_dirs = get_implied_directories(collapsed.files)
        empty_dirs = [d.path for d in collapsed.dirs if d.path not in implied_dirs]
        if empty_dirs:
            logger.warning(
                "Dropping %d empty directories (not supported in v2023 format): %s",
                len(empty_dirs),
                empty_dirs[:5] if len(empty_dirs) > 5 else empty_dirs,
            )

    # Convert files to v2023 ManifestPath entries
    paths: List[ManifestPath] = []
    for entry in collapsed.files:
        if entry.hash is None:
            raise ValueError(f"File '{entry.path}' is missing a hash.")
        if entry.size is None or entry.mtime is None:
            raise ValueError(f"File '{entry.path}' is missing size or mtime.")

        paths.append(
            ManifestPath(
                path=entry.path,
                hash=entry.hash,
                size=entry.size,
                mtime=entry.mtime,
            )
        )

    return AssetManifest(
        hash_alg=collapsed.hashAlg,
        paths=cast(List[BaseManifestPath], paths),
        total_size=collapsed.totalSize,
    )


def snapshot_diff_to_v2023_manifest(snapshot_diff: SnapshotDiff) -> AssetManifest:
    """
    Convert a SnapshotDiff to a v2023-03-03 AssetManifest.

    Uses COLLAPSE_ALL symlink policy via subtree_manifest with "." to collapse
    all symlinks. Empty directories and deletions are dropped with warnings.

    Args:
        snapshot_diff: A SnapshotDiff with relative paths and hashes computed.
                       Must have fileChunkSizeBytes=WHOLE_FILE_CHUNK_SIZE.

    Returns:
        A v2023-03-03 AssetManifest containing only new/modified files.

    Raises:
        ValueError: If fileChunkSizeBytes is not WHOLE_FILE_CHUNK_SIZE.
        ValueError: If any non-deleted file is missing a hash.
    """
    if snapshot_diff.fileChunkSizeBytes != WHOLE_FILE_CHUNK_SIZE:
        raise ValueError(
            f"v2023 format requires fileChunkSizeBytes={WHOLE_FILE_CHUNK_SIZE}, "
            f"got {snapshot_diff.fileChunkSizeBytes}."
        )

    # Use subtree_manifest with "." to apply COLLAPSE_ALL without rebasing paths
    collapsed = subtree_manifest(snapshot_diff, ".", symlink_policy=SymlinkPolicy.COLLAPSE_ALL)

    # Warn about truly empty directories being dropped (those not implied by file paths)
    implied_dirs = get_implied_directories(collapsed.files)
    non_deleted_dirs = [d for d in collapsed.dirs if not d.deleted]
    if non_deleted_dirs:
        empty_dirs = [d.path for d in non_deleted_dirs if d.path not in implied_dirs]
        if empty_dirs:
            logger.warning(
                "Dropping %d empty directories (not supported in v2023 format): %s",
                len(empty_dirs),
                empty_dirs[:5] if len(empty_dirs) > 5 else empty_dirs,
            )

    # Warn about deletions being dropped
    deleted_files = [e for e in collapsed.files if e.deleted]
    deleted_dirs = [d for d in collapsed.dirs if d.deleted]
    if deleted_files or deleted_dirs:
        deleted_paths = [e.path for e in deleted_files] + [d.path for d in deleted_dirs]
        logger.warning(
            "Dropping %d deletions (not supported in v2023 format): %s",
            len(deleted_paths),
            deleted_paths[:5] if len(deleted_paths) > 5 else deleted_paths,
        )

    # Convert non-deleted files to v2023 ManifestPath entries
    paths: List[ManifestPath] = []
    total_size = 0
    for entry in collapsed.files:
        if entry.deleted:
            continue
        if entry.hash is None:
            raise ValueError(f"File '{entry.path}' is missing a hash.")
        if entry.size is None or entry.mtime is None:
            raise ValueError(f"File '{entry.path}' is missing size or mtime.")

        paths.append(
            ManifestPath(
                path=entry.path,
                hash=entry.hash,
                size=entry.size,
                mtime=entry.mtime,
            )
        )
        total_size += entry.size

    return AssetManifest(
        hash_alg=collapsed.hashAlg,
        paths=cast(List[BaseManifestPath], paths),
        total_size=total_size,
    )


def v2023_manifest_to_snapshot(manifest: AssetManifest) -> Snapshot:
    """
    Convert a v2023-03-03 AssetManifest to a Snapshot.

    Args:
        manifest: A v2023-03-03 AssetManifest.

    Returns:
        A Snapshot with relative paths.
    """
    files: List[ManifestFilePath] = []
    for path_entry in manifest.paths:
        files.append(
            ManifestFilePath(
                path=path_entry.path,
                hash=path_entry.hash,
                size=path_entry.size,
                mtime=path_entry.mtime,
            )
        )

    return Snapshot(
        hash_alg=manifest.hashAlg,
        files=files,
        total_size=manifest.totalSize,
        file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
    )


def v2023_manifest_to_snapshot_diff(
    manifest: AssetManifest,
    parent_manifest_hash: str | None = None,
) -> SnapshotDiff:
    """
    Convert a v2023-03-03 AssetManifest to a SnapshotDiff.

    Note: The v2023 format cannot represent deletions, so the resulting
    SnapshotDiff will only contain additions/modifications.

    Args:
        manifest: A v2023-03-03 AssetManifest.
        parent_manifest_hash: Optional hash of the parent manifest.

    Returns:
        A SnapshotDiff with relative paths.
    """
    files: List[ManifestFilePath] = []
    for path_entry in manifest.paths:
        files.append(
            ManifestFilePath(
                path=path_entry.path,
                hash=path_entry.hash,
                size=path_entry.size,
                mtime=path_entry.mtime,
            )
        )

    return SnapshotDiff(
        hash_alg=manifest.hashAlg,
        files=files,
        total_size=manifest.totalSize,
        parent_manifest_hash=parent_manifest_hash,
        file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
    )
