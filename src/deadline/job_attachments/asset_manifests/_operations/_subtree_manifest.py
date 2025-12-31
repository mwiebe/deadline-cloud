# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for extracting a subtree from a manifest.

This module implements the SUBTREE operation from the composable manifest operations design:
    SUBTREE: (Manifest, subtree_path) → Manifest

The SUBTREE operation extracts a portion of a manifest rooted at a subdirectory,
producing a new manifest with paths relative to the new root.

Key behaviors:
- Filters to entries within the subtree
- Rebases paths relative to the new root (strips the subtree prefix)
- Handles symlinks according to symlink_policy
- Output always uses relative paths

Path Style Requirements:
- The subtree path must match the manifest's path style (both relative or both absolute)
- Mismatches raise ValueError

Symlink Handling:
- Symlinks that were "within root" may now "escape" the new subtree root
- symlink_policy controls how escaping symlinks are handled
- PRESERVE and TRANSITIVE_INCLUDE_TARGETS are not supported (output must be relative)
"""

from __future__ import annotations

import os
import posixpath
from typing import Any, Callable, Dict, List, Optional, Set

from ..base_manifest import BaseAssetManifest
from ..versions import ManifestVersion, SymlinkPolicy
from ..v2023_03_03.asset_manifest import (
    AssetManifest as AssetManifest2023,
    ManifestPath as ManifestPath2023,
)
from ..v2025_12_04.asset_manifest import (
    AssetManifest as AssetManifest2025,
    ManifestDirectoryPath as ManifestDirectoryPath2025,
    ManifestFilePath as ManifestFilePath2025,
)


def subtree_manifest(
    manifest: BaseAssetManifest,
    subtree: str,
    *,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> BaseAssetManifest:
    """
    Extract a subtree from a manifest, producing a new manifest rooted at the subdirectory.

    Args:
        manifest: The source manifest to extract from
        subtree: Path to the subtree root (relative or absolute, must match manifest path style)
        symlink_policy: How to handle symlinks that escape the new subtree root.
                       Only COLLAPSE, COLLAPSE_ESCAPING, and EXCLUDE are supported.
        print_function_callback: Progress callback for status messages

    Returns:
        A new manifest with:
        - Only entries within the subtree
        - Paths rebased relative to the new root
        - Symlinks handled according to symlink_policy

    Raises:
        ValueError: If subtree path style doesn't match manifest path style
        ValueError: If symlink_policy is PRESERVE or TRANSITIVE_INCLUDE_TARGETS
        ValueError: If subtree path is empty
    """
    # Validate symlink_policy
    if symlink_policy in (SymlinkPolicy.PRESERVE, SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS):
        raise ValueError(
            f"symlink_policy={symlink_policy.value} is not supported for SUBTREE operation. "
            f"Output must use relative paths, so escaping symlinks cannot be preserved. "
            f"Use COLLAPSE, COLLAPSE_ESCAPING, or EXCLUDE instead."
        )

    # Normalize subtree path
    subtree = _normalize_subtree_path(subtree)
    if not subtree or subtree == ".":
        raise ValueError("subtree path cannot be empty or '.'")

    # Validate path style consistency
    _validate_path_style_consistency(manifest, subtree)

    version = manifest.manifestVersion

    if version == ManifestVersion.v2023_03_03:
        if not isinstance(manifest, AssetManifest2023):
            raise TypeError(
                f"Expected AssetManifest2023 for version {version}, got {type(manifest).__name__}"
            )
        return _subtree_manifest_v2023(
            manifest=manifest,
            subtree=subtree,
            symlink_policy=symlink_policy,
            print_function_callback=print_function_callback,
        )
    elif version == ManifestVersion.v2025_12_04_beta:
        if not isinstance(manifest, AssetManifest2025):
            raise TypeError(
                f"Expected AssetManifest2025 for version {version}, got {type(manifest).__name__}"
            )
        return _subtree_manifest_v2025(
            manifest=manifest,
            subtree=subtree,
            symlink_policy=symlink_policy,
            print_function_callback=print_function_callback,
        )
    else:
        raise ValueError(f"Unsupported manifest version: {version}")


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


def _is_absolute_path(path: str) -> bool:
    """Check if a path is absolute for the host OS."""
    if os.name == "nt":
        # Windows drive letter (e.g., C:/)
        if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
            return True
        # Windows UNC path
        if path.startswith("//") or path.startswith("\\\\"):
            return True
    # POSIX absolute (consider it absolute on Windows, even though it's half-absolute)
    if path.startswith("/"):
        return True
    return False


def _validate_path_style_consistency(manifest: BaseAssetManifest, subtree: str) -> None:
    """
    Validate that the subtree path style matches the manifest's path style.

    Raises ValueError if there's a mismatch.
    """
    subtree_is_absolute = _is_absolute_path(subtree)

    # Check first file path to determine manifest style
    manifest_is_absolute: Optional[bool] = None

    for entry in manifest.paths:
        manifest_is_absolute = _is_absolute_path(entry.path)
        break

    # If no paths, check directories (v2025+)
    if manifest_is_absolute is None and hasattr(manifest, "dirs"):
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


def _subtree_manifest_v2023(
    manifest: AssetManifest2023,
    subtree: str,
    symlink_policy: SymlinkPolicy,
    print_function_callback: Callable[[Any], None],
) -> AssetManifest2023:
    """
    Extract subtree for v2023-03-03 manifests.

    v2023 format doesn't support symlinks, so symlink_policy only affects
    validation (COLLAPSE and EXCLUDE are both no-ops since there are no symlinks).
    """
    result_paths: List[ManifestPath2023] = []
    total_size = 0

    for entry in manifest.paths:
        if not _is_within_subtree(entry.path, subtree):
            continue

        rebased_path = _rebase_path(entry.path, subtree)
        if not rebased_path:
            # Skip the subtree directory itself (shouldn't happen for files)
            continue

        result_paths.append(
            ManifestPath2023(
                path=rebased_path,
                hash=entry.hash,
                size=entry.size,
                mtime=entry.mtime,
            )
        )
        total_size += entry.size
        print_function_callback(f"Included: {rebased_path}")

    return AssetManifest2023(
        hash_alg=manifest.hashAlg,
        paths=result_paths,
        total_size=total_size,
    )


def _subtree_manifest_v2025(
    manifest: AssetManifest2025,
    subtree: str,
    symlink_policy: SymlinkPolicy,
    print_function_callback: Callable[[Any], None],
) -> AssetManifest2025:
    """
    Extract subtree for v2025-12-04-beta manifests.

    Handles:
    - Regular files: rebased if within subtree
    - Directories: rebased if within subtree
    - Symlinks: handled according to symlink_policy
    - Deleted markers: rebased if within subtree
    """
    # Build lookup tables for collapse operations
    file_lookup: Dict[str, ManifestFilePath2025] = {e.path: e for e in manifest.paths}

    # Build dir_lookup from explicit dirs AND implicit parent directories of files
    dir_lookup: Set[str] = {d.path for d in manifest.dirs}
    for entry in manifest.paths:
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

    result_paths: List[ManifestFilePath2025] = []
    result_dirs: List[ManifestDirectoryPath2025] = []
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
            ManifestDirectoryPath2025(
                path=rebased_path,
                deleted=dir_entry.deleted,
            )
        )

    # Process files and symlinks
    for entry in manifest.paths:
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
                ManifestFilePath2025(
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

    return AssetManifest2025(
        hash_alg=manifest.hashAlg,
        dirs=result_dirs,
        paths=result_paths,
        total_size=total_size,
        manifest_type=manifest.manifestType,
        parent_manifest_hash=manifest.parentManifestHash,
    )


def _handle_symlink_in_subtree(
    entry: ManifestFilePath2025,
    symlink_target: str,
    rebased_path: str,
    subtree: str,
    symlink_policy: SymlinkPolicy,
    file_lookup: Dict[str, ManifestFilePath2025],
    dir_lookup: Set[str],
    print_function_callback: Callable[[Any], None],
) -> tuple[List[ManifestFilePath2025], int]:
    """
    Handle a symlink entry when extracting a subtree.

    Returns a tuple of (list of entries to add, total size added).
    """
    # Check if the target is within the new subtree
    # (symlink_target is already relative to manifest root)
    target_in_subtree = _is_within_subtree(symlink_target, subtree)

    # COLLAPSE policy: collapse ALL symlinks regardless of whether they escape
    if symlink_policy == SymlinkPolicy.COLLAPSE:
        return _collapse_symlink(
            rebased_path=rebased_path,
            target=symlink_target,
            file_lookup=file_lookup,
            dir_lookup=dir_lookup,
            print_function_callback=print_function_callback,
        )

    if target_in_subtree:
        # Target is within subtree - symlink doesn't escape
        # Rebase the symlink target relative to the new root
        rebased_target = _rebase_path(symlink_target, subtree)
        print_function_callback(f"Preserved symlink: {rebased_path} -> {rebased_target}")
        return (
            [
                ManifestFilePath2025(
                    path=rebased_path,
                    symlink_target=rebased_target,
                )
            ],
            0,
        )

    # Target escapes the subtree - handle according to policy
    if symlink_policy == SymlinkPolicy.EXCLUDE:
        print_function_callback(f"Excluded escaping symlink: {rebased_path}")
        return ([], 0)

    # COLLAPSE_ESCAPING - collapse the symlink
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
    file_lookup: Dict[str, ManifestFilePath2025],
    dir_lookup: Set[str],
    print_function_callback: Callable[[Any], None],
) -> tuple[List[ManifestFilePath2025], int]:
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
                ManifestFilePath2025(
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
        result_entries: List[ManifestFilePath2025] = []
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
                        ManifestFilePath2025(
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
