# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for collecting directory structure into manifest objects WITHOUT computing hashes.

This module implements the COLLECT operation from the composable manifest operations design:
    COLLECT: Directory → Manifest (with hash="" for files)

The separation of collection from hashing enables:
- Fast diff comparison by mtime/size without hashing unchanged files
- Hash cache integration - only hash files with cache misses
- Deferred hashing - collect structure first, hash only what's needed
- Remove redundant reads - Can read file to memory, then hash + upload instead of separate reads for hash and upload.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any, Callable, List, Optional, Set

from ..base_manifest import BaseAssetManifest
from ..hash_algorithms import HashAlgorithm
from ..versions import ManifestType, ManifestVersion, SymlinkPolicy
from ..v2023_03_03.asset_manifest import (
    AssetManifest as AssetManifest2023,
    ManifestPath as ManifestPath2023,
)
from ..v2025_12_04.asset_manifest import (
    AssetManifest as AssetManifest2025,
    ManifestDirectoryPath as ManifestDirectoryPath2025,
    ManifestFilePath as ManifestFilePath2025,
)


def _collect_manifest_directory_tree(
    root: Path | str,
    version: ManifestVersion,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
    *,
    absolute_paths: bool = False,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
) -> BaseAssetManifest:
    """
    Scan a directory tree and create a manifest WITHOUT hashes.

    This function:
    1. Walks the directory tree
    2. Collects all files, symlinks, and directories
    3. Captures metadata (mtime, size, permissions)
    4. Sets hash="" (empty string) for all file entries
    5. Returns a manifest ready for hashing

    Args:
        root: Root directory path to scan
        version: Manifest version to create (determines features)
        print_function_callback: Progress callback
        absolute_paths: If True, store absolute paths in manifest entries instead
            of paths relative to root. Useful for intermediate in-memory processing.
            Default False produces standard relative paths for on-disk storage.
        symlink_policy: How to handle symlinks during collection:
            - COLLAPSE: Follow all symlinks, treating them as files/directories.
            - COLLAPSE_ESCAPING: Follow only symlinks that escape root; preserve others.
              (v2025 only)
            - PRESERVE: Keep all symlinks (requires absolute_paths=True). (v2025 only)
            - TRANSITIVE_INCLUDE_TARGETS: Keep symlinks and add their targets
              (requires absolute_paths=True). (v2025 only)
            - EXCLUDE: Skip all symlinks entirely.

    Returns:
        A manifest with all entries but hash="" for files

    Raises:
        ValueError: If symlink_policy requires absolute_paths=True but it's False.
        ValueError: If v2023 version is used with symlink_policy other than COLLAPSE or EXCLUDE.

    Note:
        - For v2023-03-03: Only COLLAPSE and EXCLUDE policies are supported.
          Only files are collected (symlinks and directories cannot be represented
          in the manifest format, but directories are still traversed to find files).
        - For v2025-12-04-beta: Files, symlinks, and directories are collected.
        - Symlinks have symlink_target set (no hash needed)
        - Use _hash_manifest() to fill in file hashes
    """
    # Validate symlink_policy constraints
    if symlink_policy in (SymlinkPolicy.PRESERVE, SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS):
        if not absolute_paths:
            raise ValueError(
                f"symlink_policy={symlink_policy.value} requires absolute_paths=True "
                "because escaping symlinks cannot be represented with relative paths."
            )

    root_path = Path(os.path.normpath(os.path.abspath(root)))
    if version == ManifestVersion.v2023_03_03:
        return _collect_manifest_directory_tree_v2023(
            root_path,
            print_function_callback,
            absolute_paths=absolute_paths,
            symlink_policy=symlink_policy,
        )
    elif version == ManifestVersion.v2025_12_04_beta:
        return _collect_manifest_directory_tree_v2025(
            root_path,
            print_function_callback,
            absolute_paths=absolute_paths,
            symlink_policy=symlink_policy,
        )
    else:
        raise ValueError(f"Unsupported manifest version: {version}")


def _collect_manifest_directory_tree_v2023(
    root_path: Path,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
    *,
    absolute_paths: bool = False,
    symlink_policy: SymlinkPolicy,
) -> AssetManifest2023:
    """
    Scan directory tree and create a v2023-03-03 manifest WITHOUT hashes.

    This function:
    1. Walks the directory tree
    2. Collects regular files (symlinks are handled according to symlink_policy)
    3. Captures metadata (mtime, size)
    4. Sets hash="" for all file entries (to be filled by _hash_manifest)

    Note: v2023 format does not support symlink entries. Only COLLAPSE and EXCLUDE
    policies are allowed. COLLAPSE follows symlinks during directory walk (so symlink
    targets are collected as files), EXCLUDE skips symlinks entirely.

    Args:
        root_path: Root directory path to scan
        print_function_callback: Progress callback
        absolute_paths: If True, store absolute paths
        symlink_policy: Required. Must be COLLAPSE or EXCLUDE.

    Raises:
        ValueError: If symlink_policy is not COLLAPSE or EXCLUDE.
    """
    # v2023 only supports COLLAPSE and EXCLUDE - both result in no symlinks in manifest
    if symlink_policy not in (SymlinkPolicy.COLLAPSE, SymlinkPolicy.EXCLUDE):
        raise ValueError(
            f"v2023-03-03 manifest format only supports symlink_policy COLLAPSE or EXCLUDE, "
            f"got {symlink_policy.value}. Other policies require symlink entries which v2023 "
            "cannot represent."
        )

    # COLLAPSE means follow symlinks during walk, EXCLUDE means skip them
    followlinks = symlink_policy == SymlinkPolicy.COLLAPSE

    file_entries: List[ManifestPath2023] = []
    total_size = 0

    # On Windows, directory symlinks may appear in filenames instead of being followed
    # by os.walk. Track them for manual walking.
    # Each entry is (symlink_path_relative_to_root, target_absolute_path)
    dir_symlinks_to_walk: List[tuple[str, Path]] = []

    for dirpath, _, filenames in os.walk(root_path, followlinks=followlinks):
        for name in filenames:
            full_path = Path(dirpath) / name

            # Handle symlinks according to policy
            if full_path.is_symlink():
                if symlink_policy == SymlinkPolicy.EXCLUDE:
                    print_function_callback(f"Excluding symlink: {full_path}")
                    continue
                # COLLAPSE mode: check if it's a directory symlink (Windows edge case)
                if _symlink_target_is_directory(full_path):
                    # Directory symlink appeared in filenames (Windows behavior)
                    # Queue it for manual walking
                    rel_path = full_path.relative_to(root_path).as_posix()
                    target = _get_symlink_absolute_target(full_path)
                    dir_symlinks_to_walk.append((rel_path, target))
                    continue
                # File symlink - fall through to collect as a file

            # Skip if not a regular file (after following symlinks if COLLAPSE)
            if not full_path.is_file():
                continue

            if absolute_paths:
                entry_path = full_path.absolute().as_posix()
            else:
                entry_path = full_path.relative_to(root_path).as_posix()

            try:
                stat_info = full_path.stat()
            except OSError as e:
                print_function_callback(f"Skipping inaccessible file {entry_path}: {e}")
                continue

            file_size = stat_info.st_size
            mtime_us = stat_info.st_mtime_ns // 1000  # nanoseconds to microseconds

            file_entries.append(
                ManifestPath2023(
                    path=entry_path,
                    hash="",  # Empty string - to be filled by _hash_manifest()
                    size=file_size,
                    mtime=mtime_us,
                )
            )
            total_size += file_size
            print_function_callback(f"Collected: {entry_path}")

    # Walk directory symlinks that appeared in filenames (Windows edge case)
    for symlink_rel_path, target_abs_path in dir_symlinks_to_walk:
        entries, size = _collect_dir_symlink_v2023(
            symlink_rel_path=symlink_rel_path,
            target_abs_path=target_abs_path,
            absolute_paths=absolute_paths,
            root_path=root_path,
            print_function_callback=print_function_callback,
        )
        file_entries.extend(entries)
        total_size += size

    return AssetManifest2023(
        hash_alg=HashAlgorithm.XXH128,
        paths=file_entries,
        total_size=total_size,
    )


def _collect_dir_symlink_v2023(
    symlink_rel_path: str,
    target_abs_path: Path,
    absolute_paths: bool,
    root_path: Path,
    print_function_callback: Callable[[Any], None],
) -> tuple[List[ManifestPath2023], int]:
    """
    Collect contents of a directory symlink for v2023 COLLAPSE mode.

    This handles the Windows edge case where directory symlinks appear in
    os.walk's filenames list instead of being followed automatically.

    Args:
        symlink_rel_path: Relative path of the symlink from root (e.g., "link_dir")
        target_abs_path: Absolute path to the symlink target directory
        absolute_paths: Whether to use absolute paths in manifest
        root_path: Root directory path of the manifest
        print_function_callback: Progress callback

    Returns:
        Tuple of (file_entries, total_size)
    """
    file_entries: List[ManifestPath2023] = []
    total_size = 0

    if not target_abs_path.exists() or not target_abs_path.is_dir():
        print_function_callback(f"Skipping broken or non-directory symlink: {symlink_rel_path}")
        return (file_entries, total_size)

    # Walk the target directory with followlinks=True to continue following symlinks
    for dirpath, _, filenames in os.walk(target_abs_path, followlinks=True):
        # Calculate the relative path within the target
        rel_within_target = Path(dirpath).relative_to(target_abs_path)

        for name in filenames:
            full_path = Path(dirpath) / name

            # Skip if not a regular file
            if not full_path.is_file():
                continue

            # Build manifest path
            if rel_within_target == Path("."):
                manifest_file_rel = f"{symlink_rel_path}/{name}"
            else:
                manifest_file_rel = f"{symlink_rel_path}/{rel_within_target.as_posix()}/{name}"

            if absolute_paths:
                entry_path = (root_path / manifest_file_rel).as_posix()
            else:
                entry_path = manifest_file_rel

            try:
                stat_info = full_path.stat()
            except OSError as e:
                print_function_callback(f"Skipping inaccessible file {entry_path}: {e}")
                continue

            file_size = stat_info.st_size
            mtime_us = stat_info.st_mtime_ns // 1000

            file_entries.append(
                ManifestPath2023(
                    path=entry_path,
                    hash="",
                    size=file_size,
                    mtime=mtime_us,
                )
            )
            total_size += file_size
            print_function_callback(f"Collected: {entry_path}")

    return (file_entries, total_size)


def _collect_manifest_directory_tree_v2025(
    root_path: Path,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
    *,
    absolute_paths: bool = False,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
) -> AssetManifest2025:
    """
    Scan directory tree and create a v2025-12-04-beta manifest WITHOUT hashes.

    This function:
    1. Walks the directory tree
    2. Collects files, symlinks, and directories
    3. Captures metadata (mtime, size, permissions)
    4. Sets hash="" for all file entries (to be filled by _hash_manifest)
    5. Handles symlinks according to symlink_policy

    Note: Does NOT compute file hashes. Use _hash_manifest() to fill in hashes.
    """
    file_entries: List[ManifestFilePath2025] = []
    dir_entries: List[ManifestDirectoryPath2025] = []
    total_size = 0

    # For COLLAPSE policy, follow all symlinks during walk
    followlinks = symlink_policy == SymlinkPolicy.COLLAPSE

    # Track paths we've already collected (for TRANSITIVE_INCLUDE_TARGETS)
    collected_paths: Set[str] = set()

    # Paths to collect transitively (symlink targets outside root)
    transitive_targets: List[Path] = []

    # Escaping directory symlinks to manually walk (for COLLAPSE_ESCAPING)
    # Each entry is (symlink_path_relative_to_root, target_absolute_path)
    escaping_dir_symlinks: List[tuple[str, Path]] = []

    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=followlinks):
        rel_dir = Path(dirpath).relative_to(root_path)

        # Record directory (except root)
        if rel_dir != Path("."):
            if absolute_paths:
                dir_path = Path(dirpath).absolute().as_posix()
            else:
                dir_path = rel_dir.as_posix()
            dir_entries.append(ManifestDirectoryPath2025(path=dir_path))
            print_function_callback(f"Collected dir: {dir_path}")

        # Check for symlinks to directories (they appear in dirnames)
        for name in list(dirnames):
            full_path = Path(dirpath) / name
            if full_path.is_symlink():
                result = _handle_symlink_v2025(
                    full_path=full_path,
                    root_path=root_path,
                    absolute_paths=absolute_paths,
                    symlink_policy=symlink_policy,
                    print_function_callback=print_function_callback,
                    is_directory=True,
                )
                if result is not None:
                    entry, should_follow, transitive_target = result
                    if entry is not None:
                        file_entries.append(entry)
                        collected_paths.add(entry.path)
                    if transitive_target is not None:
                        transitive_targets.append(transitive_target)
                    if should_follow:
                        # For COLLAPSE mode, let os.walk follow the symlink (don't remove from dirnames)
                        if symlink_policy == SymlinkPolicy.COLLAPSE:
                            pass  # Let os.walk handle it with followlinks=True
                        elif symlink_policy == SymlinkPolicy.COLLAPSE_ESCAPING:
                            # For COLLAPSE_ESCAPING, we need to manually walk escaping dir symlinks
                            # since os.walk won't follow them (followlinks=False)
                            rel_path = full_path.relative_to(root_path).as_posix()
                            target = _get_symlink_absolute_target(full_path)
                            escaping_dir_symlinks.append((rel_path, target))
                            dirnames.remove(name)
                        else:
                            # Other policies that want to follow - remove and handle manually
                            dirnames.remove(name)
                    else:
                        # Remove from dirnames to prevent os.walk from following it
                        dirnames.remove(name)
                else:
                    # Symlink was excluded or invalid
                    dirnames.remove(name)

        # Process files
        for name in filenames:
            full_path = Path(dirpath) / name
            stat_info = full_path.stat(follow_symlinks=False)
            if absolute_paths:
                entry_path = full_path.absolute().as_posix()
            else:
                entry_path = full_path.relative_to(root_path).as_posix()

            if stat.S_ISLNK(stat_info.st_mode):
                # Check if symlink points to a directory (on Windows, directory symlinks
                # may appear in filenames rather than dirnames when followlinks=False)
                symlink_target_is_dir = _symlink_target_is_directory(full_path)

                result = _handle_symlink_v2025(
                    full_path=full_path,
                    root_path=root_path,
                    absolute_paths=absolute_paths,
                    symlink_policy=symlink_policy,
                    print_function_callback=print_function_callback,
                    is_directory=symlink_target_is_dir,
                )
                if result is not None:
                    entry, should_follow, transitive_target = result
                    if entry is not None:
                        file_entries.append(entry)
                        collected_paths.add(entry.path)
                    if transitive_target is not None:
                        transitive_targets.append(transitive_target)
                    if should_follow:
                        if symlink_target_is_dir:
                            # Directory symlink - need to manually walk it
                            # (for COLLAPSE_ESCAPING, add to escaping_dir_symlinks)
                            if symlink_policy == SymlinkPolicy.COLLAPSE_ESCAPING:
                                rel_path = full_path.relative_to(root_path).as_posix()
                                target = _get_symlink_absolute_target(full_path)
                                escaping_dir_symlinks.append((rel_path, target))
                            elif symlink_policy == SymlinkPolicy.COLLAPSE:
                                # For COLLAPSE, we need to walk the directory manually
                                # since os.walk won't see it (it's in filenames, not dirnames)
                                rel_path = full_path.relative_to(root_path).as_posix()
                                target = _get_symlink_absolute_target(full_path)
                                escaping_dir_symlinks.append((rel_path, target))
                        else:
                            # For file symlinks with COLLAPSE, collect the target as a file
                            try:
                                target_stat = full_path.stat(follow_symlinks=True)
                                file_entry = _create_unhashed_file_entry(
                                    full_path, entry_path, target_stat
                                )
                                file_entries.append(file_entry)
                                collected_paths.add(entry_path)
                                total_size += file_entry.size or 0
                                print_function_callback(
                                    f"Collected (collapsed symlink): {entry_path}"
                                )
                            except OSError as e:
                                print_function_callback(
                                    f"Skipping broken symlink {entry_path}: {e}"
                                )
            else:
                # Create file entry WITHOUT hash
                try:
                    entry = _create_unhashed_file_entry(full_path, entry_path, stat_info)
                    file_entries.append(entry)
                    collected_paths.add(entry_path)
                    total_size += entry.size or 0
                    print_function_callback(f"Collected: {entry_path}")
                except OSError as e:
                    print_function_callback(f"Skipping inaccessible file {entry_path}: {e}")

    # Handle escaping directory symlinks (for COLLAPSE_ESCAPING policy)
    # These need to be walked manually since os.walk doesn't follow them
    for symlink_rel_path, target_abs_path in escaping_dir_symlinks:
        entries, dirs, size = _collect_escaping_dir_symlink(
            symlink_rel_path=symlink_rel_path,
            target_abs_path=target_abs_path,
            root_path=root_path,
            absolute_paths=absolute_paths,
            collected_paths=collected_paths,
            print_function_callback=print_function_callback,
        )
        file_entries.extend(entries)
        dir_entries.extend(dirs)
        total_size += size

    # Handle transitive targets (for TRANSITIVE_INCLUDE_TARGETS policy)
    for target_path in transitive_targets:
        entries, dirs, size = _collect_transitive_target(
            target_path=target_path,
            collected_paths=collected_paths,
            print_function_callback=print_function_callback,
        )
        file_entries.extend(entries)
        dir_entries.extend(dirs)
        total_size += size

    return AssetManifest2025(
        hash_alg=HashAlgorithm.XXH128,
        dirs=dir_entries,
        paths=file_entries,
        total_size=total_size,
        manifest_type=ManifestType.SNAPSHOT,
    )


def _create_unhashed_file_entry(
    full_path: Path,
    rel_path: str,
    stat_info: Optional[os.stat_result] = None,
) -> ManifestFilePath2025:
    """
    Create a ManifestFilePath entry for a regular file WITHOUT computing hash.

    Sets hash="" (empty string) to indicate hash needs to be computed.
    Captures mtime, size, and runnable (POSIX execute bit).

    Args:
        full_path: Absolute path to the file
        rel_path: Relative path from root (for manifest entry)
        stat_info: If the stat() of the path was already collected, provide it here to
            avoid redundant retrieval.

    Returns:
        ManifestFilePath with hash="" and metadata populated

    Raises:
        OSError: If file cannot be accessed
    """
    if stat_info is None:
        stat_info = full_path.stat()
    file_size = stat_info.st_size
    mtime_us = stat_info.st_mtime_ns // 1000  # microseconds
    # Check if any execute bit is set (owner, group, or other)
    runnable = bool(stat_info.st_mode & 0o111)

    return ManifestFilePath2025(
        path=rel_path,
        hash="",  # Empty string - to be filled by _hash_manifest()
        size=file_size,
        mtime=mtime_us,
        runnable=runnable if runnable else False,
    )


def _remove_longpath_prefix(path: Path) -> Path:
    """Returns a copy with '\\?\' longpath prefix removed if the path has it."""
    if os.name == "nt" and path.parts[0].startswith("\\\\?\\"):
        return Path(path.parts[0][4:], *path.parts[1:])
    else:
        return path


def _create_symlink_entry(
    full_path: Path,
    entry_path: str,
    root_path: Path,
    *,
    absolute_paths: bool = False,
) -> ManifestFilePath2025:
    """
    Create a ManifestFilePath entry for a symlink.

    Args:
        full_path: Absolute path to the symlink
        entry_path: Path for the manifest entry (relative or absolute depending on mode)
        root_path: Root directory path (for validation)
        absolute_paths: If True, store absolute symlink target. If False, store
            target relative to root_path. Note: original symlink relative paths cannot
            be preserved because they are rooted at the symlink location, not at the
            manifest root.

    Returns:
        ManifestFilePath with symlink_target set

    Raises:
        ValueError: If the symlink target is not a subpath of root_path
    """
    # Get the symlink target as an absolute path
    target = full_path.parent / os.readlink(full_path)

    # absolute() doesn't remove Windows "\\?" prefixes, remove them manually if needed
    root_path_clean = _remove_longpath_prefix(root_path)
    target_clean = _remove_longpath_prefix(target)

    # Convert to absolute path and normalize it (collapse .. components) without
    # resolving symlinks. We use os.path.normpath() to collapse .. while keeping
    # symlink chains intact, unlike resolve() which would follow symlinks.
    absolute_target = Path(os.path.normpath(target_clean.absolute()))
    absolute_root = Path(os.path.normpath(root_path_clean.absolute()))

    # Validate that the target is within root_path.
    # This will raise a ValueError if target is not a subpath of root_path.
    absolute_target.relative_to(absolute_root)

    if absolute_paths:
        # Store absolute path to the target
        symlink_target = absolute_target.as_posix()
    else:
        # Store path relative to manifest root
        symlink_target = absolute_target.relative_to(absolute_root).as_posix()

    return ManifestFilePath2025(
        path=entry_path,
        symlink_target=symlink_target,
    )


def _is_symlink_within_root(full_path: Path, root_path: Path) -> bool:
    """
    Check if a symlink's target is within the root path.

    Args:
        full_path: Absolute path to the symlink
        root_path: Root directory path

    Returns:
        True if the symlink target is within root_path, False otherwise
    """
    try:
        target = full_path.parent / os.readlink(full_path)
        root_path_clean = _remove_longpath_prefix(root_path)
        target_clean = _remove_longpath_prefix(target)

        absolute_target = Path(os.path.normpath(target_clean.absolute()))
        absolute_root = Path(os.path.normpath(root_path_clean.absolute()))

        absolute_target.relative_to(absolute_root)
        return True
    except ValueError:
        return False


def _get_symlink_absolute_target(full_path: Path) -> Path:
    """
    Get the absolute path of a symlink's target without resolving symlink chains.

    Args:
        full_path: Absolute path to the symlink

    Returns:
        Absolute path to the symlink target (normalized but not resolved)
    """
    target = full_path.parent / os.readlink(full_path)
    target_clean = _remove_longpath_prefix(target)
    return Path(os.path.normpath(target_clean.absolute()))


def _symlink_target_is_directory(full_path: Path) -> bool:
    """
    Check if a symlink's target is a directory.

    This is needed because on Windows, directory symlinks may appear in
    os.walk's filenames list rather than dirnames when followlinks=False.

    Args:
        full_path: Absolute path to the symlink

    Returns:
        True if the symlink target is a directory, False otherwise
    """
    try:
        target = full_path.parent / os.readlink(full_path)
        return target.is_dir()
    except (OSError, ValueError):
        return False


def _handle_symlink_v2025(
    full_path: Path,
    root_path: Path,
    absolute_paths: bool,
    symlink_policy: SymlinkPolicy,
    print_function_callback: Callable[[Any], None],
    is_directory: bool,
) -> Optional[tuple[Optional[ManifestFilePath2025], bool, Optional[Path]]]:
    """
    Handle a symlink according to the symlink policy.

    Args:
        full_path: Absolute path to the symlink
        root_path: Root directory path
        absolute_paths: Whether to use absolute paths in manifest
        symlink_policy: The symlink handling policy
        print_function_callback: Progress callback
        is_directory: Whether the symlink points to a directory

    Returns:
        A tuple of (entry, should_follow, transitive_target) where:
        - entry: The manifest entry to add (or None if no entry)
        - should_follow: Whether os.walk should follow this symlink
        - transitive_target: Path to collect transitively (for TRANSITIVE_INCLUDE_TARGETS)
        Returns None if the symlink should be skipped entirely.
    """
    if absolute_paths:
        entry_path = full_path.absolute().as_posix()
    else:
        entry_path = full_path.relative_to(root_path).as_posix()

    is_within_root = _is_symlink_within_root(full_path, root_path)

    if symlink_policy == SymlinkPolicy.EXCLUDE:
        print_function_callback(f"Excluding symlink: {entry_path}")
        return None

    elif symlink_policy == SymlinkPolicy.COLLAPSE:
        # Follow all symlinks - collect them as files/directories
        if is_directory:
            print_function_callback(f"Following symlink dir: {entry_path}")
            return (None, True, None)  # Let os.walk follow it or caller will walk manually
        else:
            # File symlink - signal to caller to collect as a file
            # Return should_follow=True to indicate the symlink should be followed
            return (None, True, None)

    elif symlink_policy == SymlinkPolicy.COLLAPSE_ESCAPING:
        if is_within_root:
            # Preserve as symlink entry
            try:
                entry = _create_symlink_entry(
                    full_path, entry_path, root_path, absolute_paths=absolute_paths
                )
                kind = "symlink dir" if is_directory else "symlink"
                print_function_callback(f"Collected {kind}: {entry_path}")
                return (entry, False, None)
            except ValueError as e:
                print_function_callback(f"Skipping invalid symlink {entry_path}: {e}")
                return None
        else:
            # Escaping symlink - follow it (collapse)
            if is_directory:
                print_function_callback(f"Following escaping symlink dir: {entry_path}")
                return (None, True, None)
            else:
                # File symlink - will be collected as a file by caller
                return (None, True, None)

    elif symlink_policy == SymlinkPolicy.PRESERVE:
        # Keep all symlinks (absolute_paths must be True, validated earlier)
        target = _get_symlink_absolute_target(full_path)
        entry = ManifestFilePath2025(
            path=entry_path,
            symlink_target=target.as_posix(),
        )
        kind = "symlink dir" if is_directory else "symlink"
        print_function_callback(f"Collected {kind}: {entry_path}")
        return (entry, False, None)

    elif symlink_policy == SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS:
        # Keep symlink and add target to manifest
        target = _get_symlink_absolute_target(full_path)
        entry = ManifestFilePath2025(
            path=entry_path,
            symlink_target=target.as_posix(),
        )
        kind = "symlink dir" if is_directory else "symlink"
        print_function_callback(f"Collected {kind}: {entry_path}")

        # Only add transitive target if it's outside root (inside root is already collected)
        transitive_target = None if is_within_root else target
        return (entry, False, transitive_target)

    else:
        raise ValueError(f"Unknown symlink policy: {symlink_policy}")


def _collect_transitive_target(
    target_path: Path,
    collected_paths: Set[str],
    print_function_callback: Callable[[Any], None],
) -> tuple[List[ManifestFilePath2025], List[ManifestDirectoryPath2025], int]:
    """
    Collect a symlink target that is outside the root path.

    This is used for TRANSITIVE_INCLUDE_TARGETS policy to add the actual
    content that escaping symlinks point to.

    Args:
        target_path: Absolute path to the target (file or directory)
        collected_paths: Set of paths already collected (to avoid duplicates)
        print_function_callback: Progress callback

    Returns:
        Tuple of (file_entries, dir_entries, total_size)
    """
    file_entries: List[ManifestFilePath2025] = []
    dir_entries: List[ManifestDirectoryPath2025] = []
    total_size = 0

    if not target_path.exists():
        print_function_callback(f"Skipping broken symlink target: {target_path}")
        return (file_entries, dir_entries, total_size)

    if target_path.is_file() and not target_path.is_symlink():
        # Single file target
        entry_path = target_path.as_posix()
        if entry_path not in collected_paths:
            try:
                entry = _create_unhashed_file_entry(target_path, entry_path)
                file_entries.append(entry)
                collected_paths.add(entry_path)
                total_size += entry.size or 0
                print_function_callback(f"Collected transitive target: {entry_path}")
            except OSError as e:
                print_function_callback(
                    f"Skipping inaccessible transitive target {entry_path}: {e}"
                )

    elif target_path.is_dir():
        # Directory target - collect entire subtree
        for dirpath, dirnames, filenames in os.walk(target_path, followlinks=False):
            dir_abs = Path(dirpath).absolute()

            # Record directory (except the target root itself, which is the symlink)
            if dir_abs != target_path:
                dir_path = dir_abs.as_posix()
                if dir_path not in collected_paths:
                    dir_entries.append(ManifestDirectoryPath2025(path=dir_path))
                    collected_paths.add(dir_path)
                    print_function_callback(f"Collected transitive dir: {dir_path}")

            # Skip symlinks in dirnames (don't follow nested symlinks transitively)
            for name in list(dirnames):
                full_path = Path(dirpath) / name
                if full_path.is_symlink():
                    dirnames.remove(name)

            # Process files
            for name in filenames:
                full_path = Path(dirpath) / name
                entry_path = full_path.absolute().as_posix()

                if entry_path in collected_paths:
                    continue

                if full_path.is_symlink():
                    # Skip symlinks in transitive collection
                    continue

                try:
                    stat_info = full_path.stat(follow_symlinks=False)
                    entry = _create_unhashed_file_entry(full_path, entry_path, stat_info)
                    file_entries.append(entry)
                    collected_paths.add(entry_path)
                    total_size += entry.size or 0
                    print_function_callback(f"Collected transitive: {entry_path}")
                except OSError as e:
                    print_function_callback(
                        f"Skipping inaccessible transitive file {entry_path}: {e}"
                    )

    return (file_entries, dir_entries, total_size)


def _collect_escaping_dir_symlink(
    symlink_rel_path: str,
    target_abs_path: Path,
    root_path: Path,
    absolute_paths: bool,
    collected_paths: Set[str],
    print_function_callback: Callable[[Any], None],
) -> tuple[List[ManifestFilePath2025], List[ManifestDirectoryPath2025], int]:
    """
    Collect contents of an escaping directory symlink for COLLAPSE_ESCAPING policy.

    This walks the target directory and collects all files and directories,
    using paths relative to the symlink location within the manifest root.

    Args:
        symlink_rel_path: Relative path of the symlink from root (e.g., "link_dir")
        target_abs_path: Absolute path to the symlink target directory
        root_path: Root directory path of the manifest
        absolute_paths: Whether to use absolute paths in manifest
        collected_paths: Set of paths already collected (to avoid duplicates)
        print_function_callback: Progress callback

    Returns:
        Tuple of (file_entries, dir_entries, total_size)
    """
    file_entries: List[ManifestFilePath2025] = []
    dir_entries: List[ManifestDirectoryPath2025] = []
    total_size = 0

    if not target_abs_path.exists():
        print_function_callback(f"Skipping broken escaping symlink: {symlink_rel_path}")
        return (file_entries, dir_entries, total_size)

    if not target_abs_path.is_dir():
        print_function_callback(
            f"Skipping non-directory escaping symlink target: {symlink_rel_path}"
        )
        return (file_entries, dir_entries, total_size)

    # Walk the target directory
    for dirpath, dirnames, filenames in os.walk(target_abs_path, followlinks=False):
        # Calculate the relative path within the target
        rel_within_target = Path(dirpath).relative_to(target_abs_path)

        # Build the path as it appears in the manifest (under the symlink)
        if rel_within_target == Path("."):
            manifest_dir_rel = symlink_rel_path
        else:
            manifest_dir_rel = f"{symlink_rel_path}/{rel_within_target.as_posix()}"

        # Record directory (the symlink itself is recorded as a directory)
        if absolute_paths:
            dir_entry_path = (root_path / manifest_dir_rel).as_posix()
        else:
            dir_entry_path = manifest_dir_rel

        if dir_entry_path not in collected_paths:
            dir_entries.append(ManifestDirectoryPath2025(path=dir_entry_path))
            collected_paths.add(dir_entry_path)
            print_function_callback(f"Collected dir (escaping): {dir_entry_path}")

        # Skip symlinks in dirnames (don't follow nested symlinks within escaping target)
        for name in list(dirnames):
            full_path = Path(dirpath) / name
            if full_path.is_symlink():
                print_function_callback(
                    f"Skipping nested symlink in escaping target: {manifest_dir_rel}/{name}"
                )
                dirnames.remove(name)

        # Process files
        for name in filenames:
            full_path = Path(dirpath) / name

            # Build manifest path
            if rel_within_target == Path("."):
                manifest_file_rel = f"{symlink_rel_path}/{name}"
            else:
                manifest_file_rel = f"{symlink_rel_path}/{rel_within_target.as_posix()}/{name}"

            if absolute_paths:
                entry_path = (root_path / manifest_file_rel).as_posix()
            else:
                entry_path = manifest_file_rel

            if entry_path in collected_paths:
                continue

            # Skip symlinks
            if full_path.is_symlink():
                print_function_callback(f"Skipping nested symlink in escaping target: {entry_path}")
                continue

            try:
                stat_info = full_path.stat(follow_symlinks=False)
                entry = _create_unhashed_file_entry(full_path, entry_path, stat_info)
                file_entries.append(entry)
                collected_paths.add(entry_path)
                total_size += entry.size or 0
                print_function_callback(f"Collected (escaping): {entry_path}")
            except OSError as e:
                print_function_callback(
                    f"Skipping inaccessible file in escaping target {entry_path}: {e}"
                )

    return (file_entries, dir_entries, total_size)
