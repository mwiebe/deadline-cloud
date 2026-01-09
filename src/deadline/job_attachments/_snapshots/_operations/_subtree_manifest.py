# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for extracting a subtree from a manifest.

This module implements the SUBTREE operation from the composable manifest operations design:
    SUBTREE: (Manifest, subtree_path) → RelManifest

The SUBTREE operation extracts a portion of a manifest rooted at a subdirectory,
producing a new manifest with paths relative to the new root.

Key behaviors:
- Filters to entries within the subtree
- Rebases paths relative to the new root (strips the subtree prefix)
- Handles symlinks according to symlink_policy
- Output always uses relative paths (RelSnapshotManifest or RelDiffManifest)

Path Style Requirements:
- The subtree path must match the manifest's path style (both relative or both absolute)
- Mismatches raise ValueError

Symlink Handling:
- Symlinks that were "within root" may now "escape" the new subtree root
- symlink_policy controls how escaping symlinks are handled
- PRESERVE and TRANSITIVE_INCLUDE_TARGETS are not supported (output must be relative)

All composable operations use unified manifest classes from manifest.py internally.
"""

from __future__ import annotations

import os
import posixpath
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .._manifest import (
    AbsSnapshotManifest,
    Manifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    RelDiffManifest,
    RelManifest,
    RelSnapshotManifest,
    SymlinkPolicy,
    _is_absolute_path,
)


def subtree_manifest(
    manifest: Manifest,
    subtree: str,
    *,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> RelManifest:
    """
    Extract a subtree from a manifest, producing a new manifest rooted at the subdirectory.

    Args:
        manifest: The source manifest to extract from
        subtree: Path to the subtree root (relative or absolute, must match manifest path style).
                 Use "." or "" to apply symlink_policy without rebasing paths (identity subtree).
        symlink_policy: How to handle symlinks in the subtree.
                       - COLLAPSE_ALL: Collapse every symlink in the result.
                       - COLLAPSE_ESCAPING: Collapse only symlinks whose targets escape the subtree.
                       - EXCLUDE_ALL: Exclude every symlink from the result.
                       - EXCLUDE_ESCAPING: Exclude only symlinks whose targets escape the subtree.
                       PRESERVE and TRANSITIVE_INCLUDE_TARGETS are not supported.
        print_function_callback: Progress callback for status messages

    Returns:
        A new manifest with:
        - Only entries within the subtree (all entries if subtree="." or "")
        - Paths rebased relative to the new root (unchanged if subtree="." or "")
        - Symlinks handled according to symlink_policy
        - Always returns RelSnapshotManifest or RelDiffManifest (relative paths)

    Raises:
        ValueError: If subtree path style doesn't match manifest path style
        ValueError: If symlink_policy is PRESERVE or TRANSITIVE_INCLUDE_TARGETS

    Note:
        When subtree="." or "", this operation acts as an identity transformation that
        only applies the symlink_policy. This is useful for collapsing or excluding
        symlinks without changing the path structure.
    """
    # Validate symlink_policy
    if symlink_policy in (SymlinkPolicy.PRESERVE, SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS):
        raise ValueError(
            f"symlink_policy={symlink_policy.value} is not supported for SUBTREE operation. "
            f"Output must use relative paths, so escaping symlinks cannot be preserved. "
            f"Use COLLAPSE_ALL, COLLAPSE_ESCAPING, EXCLUDE_ALL, or EXCLUDE_ESCAPING instead."
        )

    # Normalize subtree path
    subtree = _normalize_subtree_path(subtree)

    # Special case: "" or "." means identity subtree (apply symlink_policy only)
    if not subtree or subtree == ".":
        return _identity_subtree_manifest(
            manifest=manifest,
            symlink_policy=symlink_policy,
            print_function_callback=print_function_callback,
        )

    # Validate path style consistency
    _validate_path_style_consistency(manifest, subtree)

    return _subtree_manifest(
        manifest=manifest,
        subtree=subtree,
        symlink_policy=symlink_policy,
        print_function_callback=print_function_callback,
    )


def _normalize_subtree_path(subtree: str) -> str:
    """Normalize the subtree path, removing trailing slashes and normalizing separators.

    On Windows, backslashes are converted to forward slashes (they are directory separators).
    On POSIX, backslashes are preserved (they are valid filename characters).
    """
    # Only convert backslashes to forward slashes on Windows
    if os.name == "nt":
        subtree = subtree.replace("\\", "/")
    # Normalize path components (collapse .., ., etc.)
    subtree = posixpath.normpath(subtree)
    # Remove trailing slash, but preserve root "/"
    if subtree != "/":
        subtree = subtree.rstrip("/")
    return subtree


def _validate_path_style_consistency(manifest: Manifest, subtree: str) -> None:
    """
    Validate that the subtree path style matches the manifest's path style.

    Raises ValueError if there's a mismatch.
    """
    subtree_is_absolute = _is_absolute_path(subtree)

    # Check first file path to determine manifest style
    manifest_is_absolute: Optional[bool] = None

    for entry in manifest.files:
        manifest_is_absolute = _is_absolute_path(entry.path)
        break

    # If no paths, check directories
    if manifest_is_absolute is None:
        for dir_entry in manifest.dirs:
            manifest_is_absolute = _is_absolute_path(dir_entry.path)
            break

    # If manifest is empty, we can't validate - allow any subtree style
    if manifest_is_absolute is None:
        return

    if subtree_is_absolute and not manifest_is_absolute:
        raise ValueError(
            f"subtree path is absolute ('{subtree}') but manifest uses relative paths. "
            f"Both must use the same path style."
        )

    if not subtree_is_absolute and manifest_is_absolute:
        raise ValueError(
            f"subtree path is relative ('{subtree}') but manifest uses absolute paths. "
            f"Both must use the same path style."
        )


def _is_within_subtree(path: str, subtree: str) -> bool:
    """Check if a path is within the subtree (starts with subtree prefix)."""
    # Exact match (the subtree directory itself)
    if path == subtree:
        return True
    # Special case: root "/" contains all absolute paths
    if subtree == "/":
        return path.startswith("/")
    # Path is under subtree
    return path.startswith(subtree + "/")


def _rebase_path(path: str, subtree: str) -> str:
    """
    Rebase a path relative to the new subtree root.

    Example: _rebase_path("assets/textures/wood.png", "assets/textures") -> "wood.png"
    Example: _rebase_path("/home/user/file.txt", "/") -> "home/user/file.txt"
    """
    if path == subtree:
        # This shouldn't happen for files, but handle it
        return ""
    # Special case: root "/" - just strip the leading slash
    if subtree == "/":
        return path[1:]  # Remove leading "/"
    # Strip the subtree prefix and the following slash
    return path[len(subtree) + 1 :]


def _subtree_manifest(
    manifest: Manifest,
    subtree: str,
    symlink_policy: SymlinkPolicy,
    print_function_callback: Callable[[Any], None],
) -> RelManifest:
    """
    Extract subtree from a manifest using unified manifest classes.

    Handles:
    - Regular files: rebased if within subtree
    - Directories: rebased if within subtree
    - Symlinks: handled according to symlink_policy
    - Deleted markers: rebased if within subtree
    """
    # Build lookup tables for collapse operations
    file_lookup: Dict[str, ManifestFilePath] = {e.path: e for e in manifest.files}

    # Build dir_lookup from explicit dirs AND implicit parent directories of files
    dir_lookup: Set[str] = {d.path for d in manifest.dirs}
    for entry in manifest.files:
        # Add all parent directories of this file
        parent = posixpath.dirname(entry.path)
        seen_parents: Set[str] = set()  # Prevent infinite loops with UNC paths
        while parent and parent != "/" and parent not in seen_parents:
            seen_parents.add(parent)
            dir_lookup.add(parent)
            new_parent = posixpath.dirname(parent)
            if new_parent == parent:
                # posixpath.dirname returns same value (e.g., "//server" -> "//server")
                break
            parent = new_parent

    result_paths: List[ManifestFilePath] = []
    result_dirs: List[ManifestDirectoryPath] = []
    total_size = 0

    # Process directories
    for dir_entry in manifest.dirs:
        if not _is_within_subtree(dir_entry.path, subtree):
            continue

        rebased_path = _rebase_path(dir_entry.path, subtree)
        if not rebased_path:
            # Skip the subtree directory itself
            continue

        result_dirs.append(
            ManifestDirectoryPath(
                path=rebased_path,
                deleted=dir_entry.deleted,
            )
        )

    # Process files and symlinks
    for entry in manifest.files:
        if not _is_within_subtree(entry.path, subtree):
            continue

        rebased_path = _rebase_path(entry.path, subtree)
        if not rebased_path:
            continue

        # Handle symlinks
        if entry.symlink_target is not None:
            symlink_target = entry.symlink_target  # Capture for type narrowing
            new_entries, new_size = _handle_symlink_in_subtree(
                entry=entry,
                symlink_target=symlink_target,
                rebased_path=rebased_path,
                subtree=subtree,
                symlink_policy=symlink_policy,
                file_lookup=file_lookup,
                dir_lookup=dir_lookup,
                print_function_callback=print_function_callback,
            )
            result_paths.extend(new_entries)
            total_size += new_size
        else:
            # Regular file or deleted marker
            result_paths.append(
                ManifestFilePath(
                    path=rebased_path,
                    hash=entry.hash,
                    size=entry.size,
                    mtime=entry.mtime,
                    runnable=entry.runnable,
                    chunkhashes=entry.chunkhashes,
                    symlink_target=None,
                    deleted=entry.deleted,
                )
            )
            if not entry.deleted and entry.size is not None:
                total_size += entry.size
            print_function_callback(f"Included: {rebased_path}")

    # Determine output type: always relative, preserve snapshot/diff
    # Note: parentManifestHash is NOT preserved because the subtree operation
    # changes the root path, making the original parent manifest hash invalid.
    is_snapshot = isinstance(manifest, (AbsSnapshotManifest, RelSnapshotManifest))

    if is_snapshot:
        return RelSnapshotManifest(
            hash_alg=manifest.hashAlg,
            dirs=result_dirs,
            files=result_paths,
            total_size=total_size,
        )
    else:
        return RelDiffManifest(
            hash_alg=manifest.hashAlg,
            dirs=result_dirs,
            files=result_paths,
            total_size=total_size,
        )


def _identity_subtree_manifest(
    manifest: Manifest,
    symlink_policy: SymlinkPolicy,
    print_function_callback: Callable[[Any], None],
) -> RelManifest:
    """
    Apply symlink_policy to a manifest without rebasing paths.

    This is the special case when subtree="." - it acts as an identity
    transformation that only processes symlinks according to the policy.

    For relative-path manifests, paths are unchanged.
    For absolute-path manifests, this raises an error since the output
    must be a RelManifest.
    """
    # Check if manifest uses absolute paths
    for entry in manifest.files:
        if _is_absolute_path(entry.path):
            raise ValueError(
                "subtree='.' requires a manifest with relative paths. "
                "Use a specific subtree path for absolute-path manifests."
            )
        break
    for dir_entry in manifest.dirs:
        if _is_absolute_path(dir_entry.path):
            raise ValueError(
                "subtree='.' requires a manifest with relative paths. "
                "Use a specific subtree path for absolute-path manifests."
            )
        break

    # Build lookup tables for collapse operations
    file_lookup: Dict[str, ManifestFilePath] = {e.path: e for e in manifest.files}

    # Build dir_lookup from explicit dirs AND implicit parent directories of files
    dir_lookup: Set[str] = {d.path for d in manifest.dirs}
    for entry in manifest.files:
        parent = posixpath.dirname(entry.path)
        seen_parents: Set[str] = set()
        while parent and parent != "." and parent not in seen_parents:
            seen_parents.add(parent)
            dir_lookup.add(parent)
            new_parent = posixpath.dirname(parent)
            if new_parent == parent:
                break
            parent = new_parent

    result_paths: List[ManifestFilePath] = []
    result_dirs: List[ManifestDirectoryPath] = []
    total_size = 0

    # Copy directories unchanged
    for dir_entry in manifest.dirs:
        result_dirs.append(
            ManifestDirectoryPath(
                path=dir_entry.path,
                deleted=dir_entry.deleted,
            )
        )

    # Process files and symlinks
    for entry in manifest.files:
        # Handle symlinks
        if entry.symlink_target is not None:
            symlink_target = entry.symlink_target

            if symlink_policy == SymlinkPolicy.COLLAPSE_ALL:
                # Collapse all symlinks
                new_entries, new_size = _collapse_symlink(
                    rebased_path=entry.path,
                    target=symlink_target,
                    file_lookup=file_lookup,
                    dir_lookup=dir_lookup,
                    print_function_callback=print_function_callback,
                )
                result_paths.extend(new_entries)
                total_size += new_size
            elif symlink_policy == SymlinkPolicy.EXCLUDE_ALL:
                # Exclude all symlinks
                print_function_callback(f"Excluded symlink: {entry.path}")
            else:
                # COLLAPSE_ESCAPING: For identity subtree, no symlinks "escape"
                # since there's no subtree boundary. Preserve all symlinks.
                result_paths.append(
                    ManifestFilePath(
                        path=entry.path,
                        symlink_target=symlink_target,
                    )
                )
                print_function_callback(f"Preserved symlink: {entry.path} -> {symlink_target}")
        else:
            # Regular file or deleted marker - copy unchanged
            result_paths.append(
                ManifestFilePath(
                    path=entry.path,
                    hash=entry.hash,
                    size=entry.size,
                    mtime=entry.mtime,
                    runnable=entry.runnable,
                    chunkhashes=entry.chunkhashes,
                    symlink_target=None,
                    deleted=entry.deleted,
                )
            )
            if not entry.deleted and entry.size is not None:
                total_size += entry.size
            print_function_callback(f"Included: {entry.path}")

    # Determine output type: preserve snapshot/diff
    is_snapshot = isinstance(manifest, (AbsSnapshotManifest, RelSnapshotManifest))

    if is_snapshot:
        return RelSnapshotManifest(
            hash_alg=manifest.hashAlg,
            dirs=result_dirs,
            files=result_paths,
            total_size=total_size,
            parent_manifest_hash=manifest.parentManifestHash,
            file_chunk_size_bytes=manifest.fileChunkSizeBytes,
        )
    else:
        return RelDiffManifest(
            hash_alg=manifest.hashAlg,
            dirs=result_dirs,
            files=result_paths,
            total_size=total_size,
            parent_manifest_hash=manifest.parentManifestHash,
            file_chunk_size_bytes=manifest.fileChunkSizeBytes,
        )


def _handle_symlink_in_subtree(
    entry: ManifestFilePath,
    symlink_target: str,
    rebased_path: str,
    subtree: str,
    symlink_policy: SymlinkPolicy,
    file_lookup: Dict[str, ManifestFilePath],
    dir_lookup: Set[str],
    print_function_callback: Callable[[Any], None],
) -> Tuple[List[ManifestFilePath], int]:
    """
    Handle a symlink entry when extracting a subtree.

    Returns a tuple of (list of entries to add, total size added).

    Policy behavior:
    - COLLAPSE_ALL: Collapse every symlink regardless of target location.
    - COLLAPSE_ESCAPING: Collapse only symlinks whose targets escape the subtree;
                         preserve symlinks whose targets are within the subtree.
    - EXCLUDE_ALL: Exclude every symlink regardless of target location.
    - EXCLUDE_ESCAPING: Exclude only symlinks whose targets escape the subtree;
                        preserve symlinks whose targets are within the subtree.
    """
    # Check if the target is within the new subtree
    # (symlink_target is already relative to manifest root)
    target_in_subtree = _is_within_subtree(symlink_target, subtree)

    # COLLAPSE_ALL policy: collapse ALL symlinks regardless of whether they escape
    if symlink_policy == SymlinkPolicy.COLLAPSE_ALL:
        return _collapse_symlink(
            rebased_path=rebased_path,
            target=symlink_target,
            file_lookup=file_lookup,
            dir_lookup=dir_lookup,
            print_function_callback=print_function_callback,
        )

    # EXCLUDE_ALL policy: exclude ALL symlinks regardless of whether they escape
    if symlink_policy == SymlinkPolicy.EXCLUDE_ALL:
        print_function_callback(f"Excluded symlink: {rebased_path}")
        return ([], 0)

    # For COLLAPSE_ESCAPING and EXCLUDE_ESCAPING, preserve symlinks within subtree
    if target_in_subtree:
        # Target is within subtree - symlink doesn't escape, preserve it
        # Rebase the symlink target relative to the new root
        rebased_target = _rebase_path(symlink_target, subtree)
        print_function_callback(f"Preserved symlink: {rebased_path} -> {rebased_target}")
        return (
            [
                ManifestFilePath(
                    path=rebased_path,
                    symlink_target=rebased_target,
                )
            ],
            0,
        )

    # Target escapes the subtree - handle according to policy
    if symlink_policy == SymlinkPolicy.EXCLUDE_ESCAPING:
        print_function_callback(f"Excluded escaping symlink: {rebased_path}")
        return ([], 0)

    # COLLAPSE_ESCAPING - collapse the escaping symlink
    return _collapse_symlink(
        rebased_path=rebased_path,
        target=symlink_target,
        file_lookup=file_lookup,
        dir_lookup=dir_lookup,
        print_function_callback=print_function_callback,
    )


def _collapse_symlink(
    rebased_path: str,
    target: str,
    file_lookup: Dict[str, ManifestFilePath],
    dir_lookup: Set[str],
    print_function_callback: Callable[[Any], None],
) -> Tuple[List[ManifestFilePath], int]:
    """
    Collapse a symlink by replacing it with its target's content.

    Args:
        rebased_path: The symlink's path after rebasing (for the output entry)
        target: The symlink target path (relative to original manifest root)
        file_lookup: Lookup table of file entries by path
        dir_lookup: Set of directory paths
        print_function_callback: Progress callback

    Returns:
        A tuple of (list of entries to add, total size added).
    """
    # Check if target is a file
    if target in file_lookup:
        target_entry = file_lookup[target]

        # If target is itself a symlink, we need to follow the chain
        if target_entry.symlink_target is not None:
            return _collapse_symlink(
                rebased_path=rebased_path,
                target=target_entry.symlink_target,
                file_lookup=file_lookup,
                dir_lookup=dir_lookup,
                print_function_callback=print_function_callback,
            )

        # Target is a regular file - copy its content
        print_function_callback(f"Collapsed symlink to file: {rebased_path}")
        size = target_entry.size if target_entry.size is not None else 0
        return (
            [
                ManifestFilePath(
                    path=rebased_path,
                    hash=target_entry.hash,
                    size=target_entry.size,
                    mtime=target_entry.mtime,
                    runnable=target_entry.runnable,
                    chunkhashes=target_entry.chunkhashes,
                    symlink_target=None,
                    deleted=False,
                )
            ],
            size,
        )

    # Check if target is a directory
    if target in dir_lookup:
        # Collect all entries under this directory
        result_entries: List[ManifestFilePath] = []
        total_size = 0
        target_prefix = target + "/"

        for path, entry in file_lookup.items():
            if path.startswith(target_prefix):
                # Compute the path relative to the target directory
                relative_to_target = path[len(target_prefix) :]
                # New path is symlink path + relative path
                new_path = rebased_path + "/" + relative_to_target

                if entry.symlink_target is not None:
                    # Nested symlink - recursively collapse it
                    nested_entries, nested_size = _collapse_symlink(
                        rebased_path=new_path,
                        target=entry.symlink_target,
                        file_lookup=file_lookup,
                        dir_lookup=dir_lookup,
                        print_function_callback=print_function_callback,
                    )
                    result_entries.extend(nested_entries)
                    total_size += nested_size
                else:
                    result_entries.append(
                        ManifestFilePath(
                            path=new_path,
                            hash=entry.hash,
                            size=entry.size,
                            mtime=entry.mtime,
                            runnable=entry.runnable,
                            chunkhashes=entry.chunkhashes,
                            symlink_target=None,
                            deleted=entry.deleted,
                        )
                    )
                    if not entry.deleted and entry.size is not None:
                        total_size += entry.size

        print_function_callback(
            f"Collapsed symlink to directory: {rebased_path} ({len(result_entries)} entries)"
        )
        return (result_entries, total_size)

    # Target doesn't exist in manifest - exclude with warning
    print_function_callback(
        f"Warning: Excluded symlink '{rebased_path}' - target '{target}' not in manifest"
    )
    return ([], 0)
