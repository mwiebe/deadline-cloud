# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for composing multiple manifests into a single manifest.

This module implements the COMPOSE operation from the composable manifest operations design:
    COMPOSE: (AnyManifest, AnyManifest, ...) → AnyManifest

The compose operation layers manifests together, as if applying each manifest as a set of
changes in order. Later entries override earlier ones for the same path.

Supported compositions:
- (AnySnapshot, AnyDiff, AnyDiff, ...) → AnySnapshot
- (AnyDiff, AnyDiff, ...) → AnyDiff

All composable operations use v2025 structure and semantics internally. Support for
v2023 on-disk format is provided via lossy conversion functions that drop symlinks,
deletions, and other v2025-only features.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .._manifest import (
    AbsSnapshot,
    AbsSnapshotDiff,
    AnyManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    Snapshot,
    SnapshotDiff,
)


@dataclass
class _ManifestTrieNode:
    """
    A node in the manifest trie structure.

    Each node represents a path component in the directory tree. Nodes can hold:
    - A file/symlink entry (for leaf nodes representing files)
    - Child nodes (for subdirectories)
    - A deleted flag (for diff composition, to track deletion markers)

    Any node without a file_entry and without the deleted flag is implicitly a directory.
    This enables efficient:
    - Insertion/update of entries by path
    - Deletion of entire subtrees when a directory is deleted
    - Traversal to collect all entries for the final manifest
    """

    children: Dict[str, "_ManifestTrieNode"] = field(default_factory=dict)
    """Child nodes keyed by path component"""

    file_entry: Optional[ManifestFilePath] = None
    """File or symlink entry at this node"""

    deleted: bool = False
    """True if this path was deleted (for diff composition)"""

    def insert_path(self, path_components: List[str]) -> "_ManifestTrieNode":
        """
        Navigate to or create the node at the given path.

        Args:
            path_components: List of path components (e.g., ["dir", "subdir", "file.txt"])

        Returns:
            The node at the specified path

        Raises:
            ValueError: If an intermediate path component is a file (has a file_entry)
        """
        node = self
        for i, component in enumerate(path_components):
            if node.file_entry is not None:
                # This node is a file, can't have children
                current_path = "/".join(path_components[:i])
                full_path = "/".join(path_components)
                raise ValueError(
                    f"Cannot insert path '{full_path}': '{current_path}' is a file, not a directory"
                )
            if component not in node.children:
                node.children[component] = _ManifestTrieNode()
            node = node.children[component]
        return node

    def delete_subtree(self, path_components: List[str]) -> bool:
        """
        Delete the node at the given path and all its children.

        This removes the entire subtree rooted at the specified path.

        Args:
            path_components: List of path components to the node to delete

        Returns:
            True if the node was found and deleted, False otherwise
        """
        if not path_components:
            # Can't delete root
            return False

        # Navigate to parent
        parent = self
        for component in path_components[:-1]:
            if component not in parent.children:
                return False
            parent = parent.children[component]

        # Delete the target node
        target = path_components[-1]
        if target in parent.children:
            del parent.children[target]
            return True
        return False

    def delete_if_empty(self, path_components: List[str]) -> bool:
        """
        Delete the node at the given path only if it has no children.

        This implements the "delete empty directory" semantics from the
        manifest design: a directory deletion marker only deletes an
        empty directory.

        Args:
            path_components: List of path components to the node to delete

        Returns:
            True if the node was found, empty, and deleted; False otherwise
        """
        if not path_components:
            # Can't delete root
            return False

        # Navigate to parent
        parent = self
        for component in path_components[:-1]:
            if component not in parent.children:
                return False
            parent = parent.children[component]

        # Check if target exists and is empty
        target = path_components[-1]
        if target in parent.children:
            node = parent.children[target]
            if not node.children and node.file_entry is None:
                del parent.children[target]
                return True
        return False

    def mark_deleted(self, path_components: List[str]) -> "_ManifestTrieNode":
        """
        Mark a path as deleted (for diff composition).

        Creates the node if it doesn't exist, clears any file_entry,
        and sets the deleted flag. Does NOT clear children - directory
        deletion markers only represent empty directory deletion.

        Args:
            path_components: List of path components to the node to mark deleted

        Returns:
            The node that was marked deleted
        """
        node = self.insert_path(path_components)
        node.file_entry = None
        node.deleted = True
        return node

    def iter_files(
        self, path_prefix: Optional[List[str]] = None
    ) -> Iterator[Tuple[str, ManifestFilePath]]:
        """
        Iterate over all file entries in the trie (excludes deleted nodes).

        Yields:
            Tuples of (path, file_entry)
        """
        if path_prefix is None:
            path_prefix = []

        if self.file_entry is not None and not self.deleted:
            yield ("/".join(path_prefix), self.file_entry)

        for name, child in self.children.items():
            yield from child.iter_files(path_prefix + [name])

    def iter_deleted_files(self, path_prefix: Optional[List[str]] = None) -> Iterator[str]:
        """
        Iterate over all deleted file paths in the trie.

        A deleted file is a node with deleted=True and no children (leaf deletion).

        Yields:
            Deleted file paths as strings
        """
        if path_prefix is None:
            path_prefix = []

        if self.deleted and not self.children:
            yield "/".join(path_prefix)

        for name, child in self.children.items():
            yield from child.iter_deleted_files(path_prefix + [name])

    def iter_dirs(self, path_prefix: Optional[List[str]] = None) -> Iterator[str]:
        """
        Iterate over all directory paths in the trie (excludes deleted nodes).

        A directory is any node without a file_entry and not deleted.

        Yields:
            Directory paths as strings
        """
        if path_prefix is None:
            path_prefix = []

        for name, child in self.children.items():
            child_path = path_prefix + [name]
            # If this child has no file_entry and is not deleted, it's a directory
            if child.file_entry is None and not child.deleted:
                yield "/".join(child_path)
            yield from child.iter_dirs(child_path)

    def iter_deleted_dirs(self, path_prefix: Optional[List[str]] = None) -> Iterator[str]:
        """
        Iterate over all deleted directory paths in the trie.

        A deleted directory is a node with deleted=True and has children
        (or was explicitly a directory before deletion).

        Yields:
            Deleted directory paths as strings
        """
        if path_prefix is None:
            path_prefix = []

        # A deleted node with no file_entry represents a deleted directory
        # (deleted files would have had a file_entry before deletion)
        if self.deleted and self.file_entry is None:
            yield "/".join(path_prefix)

        for name, child in self.children.items():
            yield from child.iter_deleted_dirs(path_prefix + [name])

    def reconcile_deleted_flags(self) -> bool:
        """
        Reconcile deleted flags after all diffs have been applied.

        A directory that was marked deleted but now has non-deleted children
        must have its deleted flag cleared. This handles the case where:
        1. diff1 deletes /dir/ and all its contents
        2. diff2 adds /dir/newfile.txt

        After diff2, /dir/ should not be marked as deleted because it has
        a non-deleted child.

        Returns:
            True if this node has any non-deleted content (file or children),
            False if this node and all descendants are deleted or empty.
        """
        # Recursively reconcile children first (depth-first)
        has_non_deleted_children = False
        for child in self.children.values():
            if child.reconcile_deleted_flags():
                has_non_deleted_children = True

        # This node has non-deleted content if:
        # 1. It has a non-deleted file entry, OR
        # 2. Any child has non-deleted content
        has_non_deleted_file = self.file_entry is not None and not self.deleted
        has_non_deleted_content = has_non_deleted_file or has_non_deleted_children

        # If this node was marked deleted but has non-deleted children,
        # clear the deleted flag (the directory must exist for its children)
        if self.deleted and has_non_deleted_children:
            self.deleted = False

        return has_non_deleted_content


def _split_path(path: str) -> List[str]:
    """Split a POSIX path into components.

    For absolute paths (starting with '/'), the first component will be
    an empty string to preserve the absolute path information.
    """
    if not path:
        return []
    components = path.split("/")
    # Keep leading empty string for absolute paths, filter trailing empties
    if components and components[0] == "":
        return [""] + [c for c in components[1:] if c]
    return [c for c in components if c]


def compose_manifests(
    manifests: List[AnyManifest],
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> AnyManifest:
    """
    Compose multiple manifests into a single manifest by layering them together.

    The result represents the directory tree you would get by:
    1. Starting with the first manifest's directory tree
    2. Applying each subsequent manifest as a "patch"—adding new entries,
       updating modified entries, and removing deleted entries

    Args:
        manifests: List of manifests to compose. Must all be the same type
                   (all absolute or all relative). The first manifest should
                   be a snapshot, followed by zero or more diff manifests,
                   OR all manifests should be diffs.
        print_function_callback: Progress callback

    Returns:
        A single composed manifest representing the final state.
        - (AnySnapshot, AnyDiff, ...) → AnySnapshot (same path type as input)
        - (AnyDiff, AnyDiff, ...) → AnyDiff (same path type as input)

    Raises:
        ValueError: If manifests list is empty or invalid manifest type sequence.
    """
    if not manifests:
        raise ValueError("Cannot compose empty list of manifests")

    # Single manifest - return as-is
    if len(manifests) == 1:
        return manifests[0]

    # Determine composition type based on first manifest
    first = manifests[0]
    if isinstance(first, (AbsSnapshot, Snapshot)):
        return _compose_snapshot_diffs(manifests, print_function_callback)
    else:
        return _compose_diffs(manifests, print_function_callback)


def _compose_snapshot_diffs(
    manifests: List[AnyManifest],
    print_function_callback: Callable[[Any], None],
) -> AnyManifest:
    """
    Compose manifests: (snapshot, diff, diff, ...) → snapshot.

    The first manifest must be a snapshot. Subsequent manifests must be diffs.
    Diff manifests can add, modify, or delete entries. Deleted entries are
    removed from the result.

    Uses a trie structure to efficiently manage the directory tree and handle
    cascading deletions when a directory is deleted.
    """
    first = manifests[0]

    # Validate first manifest is a snapshot
    if not isinstance(first, (AbsSnapshot, Snapshot)):
        raise ValueError(
            f"First manifest must be a SNAPSHOT for snapshot+diffs composition, "
            f"got {type(first).__name__}."
        )

    # Validate remaining manifests are diffs
    for i, manifest in enumerate(manifests[1:], start=1):
        if not isinstance(manifest, (AbsSnapshotDiff, SnapshotDiff)):
            raise ValueError(
                f"Manifest {i} must be a DIFF for snapshot+diffs composition, "
                f"got {type(manifest).__name__}"
            )

    # Build trie from base snapshot
    root = _ManifestTrieNode()

    for entry in first.files:
        components = _split_path(entry.path)
        node = root.insert_path(components)
        node.file_entry = entry

    # Directories are implicitly created by insert_path when adding files
    # We also need to add explicit empty directories from the manifest
    for dir_entry in first.dirs:
        components = _split_path(dir_entry.path)
        root.insert_path(components)

    print_function_callback("Base snapshot loaded into trie")

    # Apply each diff in order
    for diff_index, diff_manifest in enumerate(manifests[1:], start=1):
        # Apply file deletions first
        for entry in diff_manifest.files:
            components = _split_path(entry.path)
            if entry.deleted:
                # Delete the file node
                if root.delete_subtree(components):
                    print_function_callback(f"Diff {diff_index}: deleted {entry.path}")

        # Apply directory deletions (empty directories only)
        # Sort by path length descending so subdirectories are deleted before parents
        deleted_dirs = [dir_entry for dir_entry in diff_manifest.dirs if dir_entry.deleted]
        deleted_dirs.sort(key=lambda d: len(d.path), reverse=True)
        for dir_entry in deleted_dirs:
            components = _split_path(dir_entry.path)
            if root.delete_if_empty(components):
                print_function_callback(f"Diff {diff_index}: deleted empty dir {dir_entry.path}")

        # Apply file additions/modifications
        for entry in diff_manifest.files:
            components = _split_path(entry.path)
            if not entry.deleted:
                # Add or update entry
                node = root.insert_path(components)
                node.file_entry = entry
                print_function_callback(f"Diff {diff_index}: added/updated {entry.path}")

        # Apply directory additions
        for dir_entry in diff_manifest.dirs:
            components = _split_path(dir_entry.path)
            if not dir_entry.deleted:
                # Add directory (creates the path in the trie)
                root.insert_path(components)

    # Collect results from trie - create new entries without deleted flag
    result_paths: List[ManifestFilePath] = []
    for _, entry in root.iter_files():
        result_paths.append(
            ManifestFilePath(
                path=entry.path,
                hash=entry.hash,
                size=entry.size,
                mtime=entry.mtime,
                runnable=entry.runnable,
                chunkhashes=entry.chunkhashes,
                symlink_target=entry.symlink_target,
            )
        )

    result_dirs: List[ManifestDirectoryPath] = []
    for dir_path in root.iter_dirs():
        result_dirs.append(ManifestDirectoryPath(path=dir_path))

    # Calculate total size (non-symlink entries only)
    total_size = sum(entry.size or 0 for entry in result_paths if entry.symlink_target is None)

    # Return the same manifest type as input (preserving absolute/relative)
    return type(first)(
        hash_alg=first.hashAlg,
        dirs=result_dirs,
        files=result_paths,
        total_size=total_size,
    )


def _compose_diffs(
    manifests: List[AnyManifest],
    print_function_callback: Callable[[Any], None],
) -> AnyManifest:
    """
    Compose diff manifests: (diff, diff, ...) → diff.

    All manifests must be diffs. The result is a single diff manifest that is
    equivalent to applying all the input diffs in order.

    The resulting diff:
    - Has parentManifestHash from the first diff (the original parent)
    - Contains all additions/modifications from the input diffs (later overrides earlier)
    - Contains deletion markers for anything deleted (even if later re-added then deleted again)

    Note: We cannot determine if a deletion "cancels out" an addition because we don't
    have access to the original parent snapshot. A deletion marker in any input diff
    means the path might exist in the parent, so we must preserve it unless the path
    ends up with a non-deleted entry in the final state.

    Uses a trie structure to track the cumulative state and determine what
    changed relative to the original parent.
    """
    # Validate all manifests are diffs
    for i, manifest in enumerate(manifests):
        if not isinstance(manifest, (AbsSnapshotDiff, SnapshotDiff)):
            raise ValueError(
                f"Manifest {i} must be a DIFF for diff composition, got {type(manifest).__name__}"
            )

    first = manifests[0]

    # Track changes using a trie with deleted flags
    root = _ManifestTrieNode()

    # Apply each diff in order
    for diff_index, diff_manifest in enumerate(manifests):
        # Apply file deletions first
        for entry in diff_manifest.files:
            components = _split_path(entry.path)
            if entry.deleted:
                # Mark as deleted
                root.mark_deleted(components)
                print_function_callback(f"Diff {diff_index}: deleted {entry.path}")

        # Apply directory deletions (empty directories only)
        for dir_entry in diff_manifest.dirs:
            components = _split_path(dir_entry.path)
            if dir_entry.deleted:
                # Mark directory as deleted
                root.mark_deleted(components)
                print_function_callback(f"Diff {diff_index}: deleted empty dir {dir_entry.path}")

        # Apply file additions/modifications
        for entry in diff_manifest.files:
            components = _split_path(entry.path)
            if not entry.deleted:
                # Add or update - clear deleted flag and set file entry
                node = root.insert_path(components)
                node.deleted = False
                node.file_entry = entry
                print_function_callback(f"Diff {diff_index}: added/updated {entry.path}")

        # Apply directory additions
        for dir_entry in diff_manifest.dirs:
            components = _split_path(dir_entry.path)
            if not dir_entry.deleted:
                # Directory added - create node and clear deleted flag
                node = root.insert_path(components)
                node.deleted = False

    # Reconcile deleted flags: a directory marked deleted but with non-deleted
    # children must have its deleted flag cleared
    root.reconcile_deleted_flags()

    # Build result diff manifest
    result_paths: List[ManifestFilePath] = []

    # Add all current file entries (additions/modifications)
    for _, entry in root.iter_files():
        result_paths.append(
            ManifestFilePath(
                path=entry.path,
                hash=entry.hash,
                size=entry.size,
                mtime=entry.mtime,
                runnable=entry.runnable,
                chunkhashes=entry.chunkhashes,
                symlink_target=entry.symlink_target,
            )
        )

    # Add deletion markers for deleted files
    for path in root.iter_deleted_files():
        result_paths.append(ManifestFilePath(path=path, deleted=True))

    # Build directory entries
    result_dirs: List[ManifestDirectoryPath] = []

    # Add current directories
    for dir_path in root.iter_dirs():
        result_dirs.append(ManifestDirectoryPath(path=dir_path))

    # Add deletion markers for deleted directories
    for dir_path in root.iter_deleted_dirs():
        result_dirs.append(ManifestDirectoryPath(path=dir_path, deleted=True))

    # Calculate total size (non-symlink, non-deleted entries only)
    total_size = sum(
        entry.size or 0
        for entry in result_paths
        if entry.symlink_target is None and not entry.deleted
    )

    # Return the same manifest type as input (preserving absolute/relative)
    return type(first)(
        hash_alg=first.hashAlg,
        dirs=result_dirs,
        files=result_paths,
        total_size=total_size,
        parent_manifest_hash=first.parentManifestHash,
    )
