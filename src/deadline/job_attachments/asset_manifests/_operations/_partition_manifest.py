# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for partitioning a manifest into multiple (root, RelSnapshot) pairs.

This module implements the PARTITION operation from the composable manifest operations design:
    PARTITION: (Snapshot, roots?) → List[(root, RelSnapshot)]

The PARTITION operation divides a manifest into multiple subtrees, each with paths
relative to its root. Each RelSnapshot is an extracted subtree per the SUBTREE operation.

Key behaviors:
- Partitions entries by root paths
- Each output manifest has paths relative to its root
- Handles symlinks according to symlink_policy
- Explicit roots appear first in output (in order), then auto-determined roots (sorted)
- Empty manifests are returned for explicit roots with no entries

Path Style Requirements:
- If manifest uses absolute paths, all provided roots must be absolute
- If manifest uses relative paths, all provided roots must be relative
- Mismatches raise ValueError

Auto-Root Determination:
- When roots is None/empty on POSIX: single root (longest common path prefix)
- When roots is None/empty on Windows: one root per drive letter or UNC root
- When roots provided: additional roots auto-determined for remaining entries
"""

from __future__ import annotations

import os
import posixpath
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..base_manifest import BaseAssetManifest
from ..versions import SymlinkPolicy
from ._subtree_manifest import subtree_manifest


def partition_manifest(
    manifest: BaseAssetManifest,
    roots: Optional[List[str]] = None,
    *,
    referenced_paths: Optional[List[str]] = None,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> List[Tuple[str, BaseAssetManifest]]:
    """
    Partition a manifest into multiple (root, RelSnapshot) pairs.

    Args:
        manifest: The source manifest to partition (absolute or relative paths)
        roots: Optional list of root paths to partition by. No root may be a subpath
               of another. Path style must match manifest path style.
        referenced_paths: Optional list of paths referenced by the workload. These paths
                         must be within one of the resulting roots, affecting auto-root
                         determination even if no files exist under them.
        symlink_policy: How to handle symlinks that escape their partition root.
                       Only COLLAPSE, COLLAPSE_ESCAPING, and EXCLUDE are supported.
        print_function_callback: Progress callback for status messages

    Returns:
        A list of (root, RelSnapshot) tuples where:
        - Each root is a path string
        - Each RelSnapshot is a manifest with paths relative to that root
        - Explicit roots appear first (in order), then auto-determined roots (sorted)

    Raises:
        ValueError: If a root is a subpath of another root
        ValueError: If root path style doesn't match manifest path style
        ValueError: If symlink_policy is PRESERVE or TRANSITIVE_INCLUDE_TARGETS
    """
    # Validate symlink_policy
    if symlink_policy in (SymlinkPolicy.PRESERVE, SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS):
        raise ValueError(
            f"symlink_policy={symlink_policy.value} is not supported for PARTITION operation. "
            f"Output must use relative paths, so escaping symlinks cannot be preserved. "
            f"Use COLLAPSE, COLLAPSE_ESCAPING, or EXCLUDE instead."
        )

    # Normalize roots
    if roots is None:
        roots = []
    roots = [_normalize_path(r) for r in roots]

    # Normalize referenced_paths
    if referenced_paths is None:
        referenced_paths = []
    referenced_paths = [_normalize_path(p) for p in referenced_paths]

    # Validate no root is a subpath of another
    _validate_roots_no_overlap(roots)

    # Determine manifest path style from first entry
    manifest_is_absolute = _get_manifest_path_style(manifest)

    # Validate root path styles match manifest
    if manifest_is_absolute is not None:
        for root in roots:
            root_is_absolute = _is_absolute_path(root)
            if root_is_absolute != manifest_is_absolute:
                style = "absolute" if manifest_is_absolute else "relative"
                root_style = "absolute" if root_is_absolute else "relative"
                raise ValueError(
                    f"Root path '{root}' is {root_style} but manifest uses {style} paths. "
                    f"All roots must match the manifest path style."
                )
        for ref_path in referenced_paths:
            ref_is_absolute = _is_absolute_path(ref_path)
            if ref_is_absolute != manifest_is_absolute:
                style = "absolute" if manifest_is_absolute else "relative"
                ref_style = "absolute" if ref_is_absolute else "relative"
                raise ValueError(
                    f"Referenced path '{ref_path}' is {ref_style} but manifest uses {style} paths. "
                    f"All referenced_paths must match the manifest path style."
                )

    # Collect all directories from manifest (for root determination)
    all_dirs = _collect_all_dirs(manifest)

    # Determine all roots (explicit + auto-determined)
    all_roots = _determine_all_roots(
        explicit_roots=roots,
        manifest_dirs=all_dirs,
        referenced_paths=referenced_paths,
        manifest_is_absolute=manifest_is_absolute,
        print_function_callback=print_function_callback,
    )

    # Build result: extract subtree for each root
    result: List[Tuple[str, BaseAssetManifest]] = []

    for root in all_roots:
        if root == "." or root == "":
            # Special case: root-level relative paths - return manifest as-is
            # (subtree_manifest doesn't accept "." as a subtree path)
            result.append((root, manifest))
            print_function_callback(f"Partitioned root '{root}' with {len(manifest.paths)} entries")
        else:
            # Use subtree_manifest to extract the subtree (returns empty manifest if no entries)
            subtree = subtree_manifest(
                manifest=manifest,
                subtree=root,
                symlink_policy=symlink_policy,
                print_function_callback=print_function_callback,
            )
            result.append((root, subtree))
            print_function_callback(f"Partitioned root '{root}' with {len(subtree.paths)} entries")

    return result


def _normalize_path(path: str) -> str:
    """Normalize a path, removing trailing slashes and normalizing separators.

    On Windows, backslashes are converted to forward slashes (they are directory separators).
    On POSIX, backslashes are preserved (they are valid filename characters).
    """
    # Only convert backslashes to forward slashes on Windows
    if os.name == "nt":
        path = path.replace("\\", "/")
    # Normalize path components (collapse .., ., etc.)
    path = posixpath.normpath(path)
    # Remove trailing slash (but preserve root slash for absolute paths)
    path = path.rstrip("/")
    # Handle edge case where path becomes empty
    if not path:
        path = "."
    return path


def _is_absolute_path(path: str) -> bool:
    """Check if a path is absolute."""
    if os.name == "nt":
        # Windows drive letter (e.g., C:/)
        if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
            return True
        # Windows UNC path
        if path.startswith("//"):
            return True
    # POSIX absolute (consider it absolute on Windows, even though it's half-absolute)
    if path.startswith("/"):
        return True
    return False


def _get_manifest_path_style(manifest: BaseAssetManifest) -> Optional[bool]:
    """
    Determine if manifest uses absolute or relative paths.

    Returns:
        True if absolute, False if relative, None if manifest is empty
    """
    # Check first file path
    for entry in manifest.paths:
        return _is_absolute_path(entry.path)

    # If no paths, check directories (v2025+)
    if hasattr(manifest, "dirs"):
        for dir_entry in manifest.dirs:
            return _is_absolute_path(dir_entry.path)

    # Empty manifest
    return None


def _validate_roots_no_overlap(roots: List[str]) -> None:
    """
    Validate that no root is a subpath of another root.

    Raises ValueError if overlap is detected.
    """
    for i, root1 in enumerate(roots):
        for j, root2 in enumerate(roots):
            if i != j and len(root1) > len(root2):
                # Only a longer path can be under a shorter path
                if _is_path_under_root(root1, root2):
                    raise ValueError(
                        f"Root '{root1}' is a subpath of root '{root2}'. "
                        f"No root may be a subpath of another root."
                    )


def _is_path_under_root(path: str, root: str) -> bool:
    """Check if a path is under the given root (or is the root itself)."""
    if path == root:
        return True
    # Special case: root "/" contains all absolute paths
    if root == "/":
        return path.startswith("/")
    # Special case: root "." contains all relative paths
    if root == ".":
        return not _is_absolute_path(path)
    # Path is under root if it starts with root + "/"
    return path.startswith(root + "/")


def _collect_all_dirs(manifest: BaseAssetManifest) -> Set[str]:
    """
    Collect all directory paths from a manifest.

    For files and symlinks, collects the parent directory.
    For directories, collects the path directly.
    Using a set deduplicates entries, reducing work for root determination.

    For files at root level (no parent directory), adds "." for relative paths
    or "/" for absolute POSIX paths to ensure root detection works.
    """
    dirs: Set[str] = set()

    for entry in manifest.paths:
        # For files/symlinks, take the parent directory
        parent = posixpath.dirname(entry.path)
        if parent:
            dirs.add(parent)
        else:
            # File at root level (e.g., "file.txt" or "/file.txt")
            # Add appropriate root marker
            if _is_absolute_path(entry.path):
                # For absolute paths like "/file.txt", parent is "/"
                dirs.add("/")
            else:
                # For relative paths like "file.txt", use "." as the root
                dirs.add(".")

    if hasattr(manifest, "dirs"):
        for dir_entry in manifest.dirs:
            dirs.add(dir_entry.path)

    return dirs


def _determine_all_roots(
    explicit_roots: List[str],
    manifest_dirs: Set[str],
    referenced_paths: List[str],
    manifest_is_absolute: Optional[bool],
    print_function_callback: Callable[[Any], None],
) -> List[str]:
    """
    Determine all roots for partitioning.

    Returns explicit roots first (in order), then auto-determined roots (sorted).
    """
    # Combine manifest dirs and referenced paths for root determination
    all_paths_for_roots = list(manifest_dirs) + referenced_paths

    if not explicit_roots:
        # No explicit roots - auto-determine all roots
        if not all_paths_for_roots:
            return []

        if manifest_is_absolute is None:
            # Determine from paths
            if all_paths_for_roots:
                manifest_is_absolute = _is_absolute_path(all_paths_for_roots[0])
            else:
                return []

        if manifest_is_absolute and os.name == "nt":
            # Windows: one root per drive letter or UNC root
            return _determine_windows_roots(all_paths_for_roots, print_function_callback)
        else:
            # POSIX or relative paths: single root (longest common prefix)
            return _determine_common_root(all_paths_for_roots, print_function_callback)

    # Explicit roots provided - find remaining paths not under any explicit root
    remaining_paths: List[str] = []
    for path in all_paths_for_roots:
        under_explicit = any(_is_path_under_root(path, root) for root in explicit_roots)
        if not under_explicit:
            remaining_paths.append(path)

    if not remaining_paths:
        # All paths covered by explicit roots
        return explicit_roots

    # Determine additional roots for remaining paths
    # These must not include any explicit root as a subpath
    additional_roots = _determine_additional_roots(
        remaining_paths=remaining_paths,
        explicit_roots=explicit_roots,
        manifest_is_absolute=manifest_is_absolute if manifest_is_absolute is not None else False,
        print_function_callback=print_function_callback,
    )

    return explicit_roots + sorted(additional_roots)


def _determine_windows_roots(
    paths: List[str],
    print_function_callback: Callable[[Any], None],
) -> List[str]:
    """
    Determine roots for Windows paths - one per drive letter or UNC root.

    For each drive/UNC root, finds the longest common prefix of all paths on that drive.
    """
    # Group paths by drive/UNC root
    paths_by_drive: Dict[str, List[str]] = {}

    for path in paths:
        drive_root = _get_windows_drive_root(path)
        if drive_root not in paths_by_drive:
            paths_by_drive[drive_root] = []
        paths_by_drive[drive_root].append(path)

    # For each drive, find the longest common prefix
    roots: List[str] = []
    for drive_root, drive_paths in sorted(paths_by_drive.items()):
        common = _longest_common_path_prefix(drive_paths)
        roots.append(common)
        print_function_callback(f"Auto-determined root for {drive_root}: {common}")

    return roots


def _get_windows_drive_root(path: str) -> str:
    """Extract the drive letter or UNC root from a Windows path."""
    # Drive letter (e.g., "C:/...")
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return path[:2].upper()

    # UNC path (e.g., "//server/share/...")
    if path.startswith("//"):
        parts = path[2:].split("/", 2)
        if len(parts) >= 2:
            return f"//{parts[0]}/{parts[1]}"
        elif len(parts) == 1:
            return f"//{parts[0]}"

    # Fallback - shouldn't happen for valid absolute Windows paths
    return path


def _determine_common_root(
    paths: List[str],
    print_function_callback: Callable[[Any], None],
) -> List[str]:
    """
    Determine a single root as the longest common path prefix.
    """
    if not paths:
        return []

    common = _longest_common_path_prefix(paths)
    print_function_callback(f"Auto-determined common root: {common}")
    return [common]


def _longest_common_path_prefix(paths: List[str]) -> str:
    """
    Find the longest common path prefix of a list of directory paths.

    This finds the longest directory path that is a prefix of all paths.
    Since the input is directories (from _collect_all_dirs), a single directory
    returns itself as the common prefix.

    For example:
        ["/a/b/c", "/a/b/d"] -> "/a/b"
        ["/a/b/c", "/a/b/c/d"] -> "/a/b/c"
        ["a/b/c"] -> "a/b/c"
        ["project/src"] -> "project/src"
    """
    if not paths:
        return ""

    if len(paths) == 1:
        # Single directory - return it as-is (it's already a directory path)
        return paths[0]

    # Split all paths into components
    split_paths = [p.split("/") for p in paths]

    # Find common prefix components
    common_parts: List[str] = []
    for parts in zip(*split_paths):
        if len(set(parts)) == 1:
            common_parts.append(parts[0])
        else:
            break

    if not common_parts:
        # No common prefix - for absolute paths, return "/"
        if paths[0].startswith("/"):
            return "/"
        # For relative paths, this shouldn't happen in practice
        return "."

    result = "/".join(common_parts)

    # Handle absolute paths - ensure we don't lose the leading slash
    if paths[0].startswith("/") and not result.startswith("/"):
        result = "/" + result

    return result


def _determine_additional_roots(
    remaining_paths: List[str],
    explicit_roots: List[str],
    manifest_is_absolute: bool,
    print_function_callback: Callable[[Any], None],
) -> List[str]:
    """
    Determine additional roots for paths not covered by explicit roots.

    The additional roots must not include any explicit root as a subpath.
    """
    if not remaining_paths:
        return []

    if manifest_is_absolute and os.name == "nt":
        # Windows: group by drive, then find common prefix per drive
        paths_by_drive: Dict[str, List[str]] = {}
        for path in remaining_paths:
            drive_root = _get_windows_drive_root(path)
            if drive_root not in paths_by_drive:
                paths_by_drive[drive_root] = []
            paths_by_drive[drive_root].append(path)

        additional_roots: List[str] = []
        for drive_root, drive_paths in paths_by_drive.items():
            roots = _find_valid_roots_for_paths(
                paths=drive_paths,
                explicit_roots=explicit_roots,
                print_function_callback=print_function_callback,
            )
            additional_roots.extend(roots)

        return additional_roots
    else:
        # POSIX or relative: find valid roots
        return _find_valid_roots_for_paths(
            paths=remaining_paths,
            explicit_roots=explicit_roots,
            print_function_callback=print_function_callback,
        )


def _find_valid_roots_for_paths(
    paths: List[str],
    explicit_roots: List[str],
    print_function_callback: Callable[[Any], None],
) -> List[str]:
    """
    Find valid roots for the given paths that don't include any explicit root as a subpath.

    Tries to find a single common root first. If that would include an explicit root,
    groups paths by their top-level directory and finds the deepest valid root for each group.
    """
    if not paths:
        return []

    candidate = _longest_common_path_prefix(paths)

    # Check if candidate includes any explicit root as a subpath
    includes_explicit = any(_is_path_under_root(root, candidate) for root in explicit_roots)
    if not includes_explicit:
        print_function_callback(f"Auto-determined additional root: {candidate}")
        return [candidate]

    # The common root would include an explicit root - need to split into multiple roots
    # Group paths by their first differing component after the problematic prefix

    # Find which explicit root is causing the problem
    problematic_root = next(
        (root for root in explicit_roots if _is_path_under_root(root, candidate)), None
    )

    if problematic_root is None:
        # Shouldn't happen, but fallback
        print_function_callback(f"Auto-determined additional root: {candidate}")
        return [candidate]

    # We need to group paths by their top-level directory relative to candidate
    # For "/" candidate, group by first path component after /
    # For "." candidate, group by first path component
    # For other candidates, we need to find paths that don't go through the problematic root

    if candidate == "/":
        # Group by top-level directory (e.g., /data, /home)
        paths_by_toplevel: Dict[str, List[str]] = {}
        for path in paths:
            # Extract top-level directory: "/data/foo" -> "/data"
            parts = path.split("/")
            if len(parts) >= 2 and parts[0] == "":
                toplevel = "/" + parts[1]
            else:
                toplevel = path
            if toplevel not in paths_by_toplevel:
                paths_by_toplevel[toplevel] = []
            paths_by_toplevel[toplevel].append(path)

        # For each top-level group, find the deepest valid root
        result: List[str] = []
        for toplevel, group_paths in paths_by_toplevel.items():
            # Find the longest common prefix within this group
            group_common = _longest_common_path_prefix(group_paths)
            # Check if it's valid
            group_includes_explicit = any(
                _is_path_under_root(root, group_common) for root in explicit_roots
            )
            if not group_includes_explicit:
                print_function_callback(f"Auto-determined additional root: {group_common}")
                result.append(group_common)
            else:
                # Need to recurse further within this group
                group_roots = _find_valid_roots_for_paths(
                    paths=group_paths,
                    explicit_roots=explicit_roots,
                    print_function_callback=print_function_callback,
                )
                result.extend(group_roots)
        return result
    elif candidate == ".":
        # Group by top-level directory for relative paths
        paths_by_toplevel = {}
        for path in paths:
            parts = path.split("/")
            toplevel = parts[0]
            if toplevel not in paths_by_toplevel:
                paths_by_toplevel[toplevel] = []
            paths_by_toplevel[toplevel].append(path)

        result = []
        for toplevel, group_paths in paths_by_toplevel.items():
            # Find the longest common prefix within this group
            group_common = _longest_common_path_prefix(group_paths)
            # Check if it's valid
            group_includes_explicit = any(
                _is_path_under_root(root, group_common) for root in explicit_roots
            )
            if not group_includes_explicit:
                print_function_callback(f"Auto-determined additional root: {group_common}")
                result.append(group_common)
            else:
                # Need to recurse further within this group
                group_roots = _find_valid_roots_for_paths(
                    paths=group_paths,
                    explicit_roots=explicit_roots,
                    print_function_callback=print_function_callback,
                )
                result.extend(group_roots)
        return result
    else:
        # candidate is a specific path like "/data" or "C:/projects"
        # The problematic root is under candidate, so we need to split paths
        # into those that go through the problematic root's parent vs others

        # Group paths by their next component after candidate
        candidate_depth = len(candidate.rstrip("/").split("/"))
        paths_by_next: Dict[str, List[str]] = {}

        for path in paths:
            parts = path.split("/")
            # Handle paths that start with empty string (absolute paths)
            if parts and parts[0] == "":
                # Absolute path - adjust for leading empty part
                if len(parts) > candidate_depth:
                    next_component = "/".join(parts[: candidate_depth + 1])
                else:
                    next_component = path
            else:
                # Relative path
                if len(parts) > candidate_depth:
                    next_component = "/".join(parts[: candidate_depth + 1])
                else:
                    next_component = path

            if next_component not in paths_by_next:
                paths_by_next[next_component] = []
            paths_by_next[next_component].append(path)

        result = []
        for next_comp, group_paths in paths_by_next.items():
            group_common = _longest_common_path_prefix(group_paths)
            group_includes_explicit = any(
                _is_path_under_root(root, group_common) for root in explicit_roots
            )
            if not group_includes_explicit:
                print_function_callback(f"Auto-determined additional root: {group_common}")
                result.append(group_common)
            else:
                # Still problematic - recurse
                group_roots = _find_valid_roots_for_paths(
                    paths=group_paths,
                    explicit_roots=explicit_roots,
                    print_function_callback=print_function_callback,
                )
                result.extend(group_roots)
        return result
