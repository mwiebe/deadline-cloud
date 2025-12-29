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
from typing import Any, Callable, List, Optional

from ..base_manifest import BaseAssetManifest
from ..hash_algorithms import HashAlgorithm
from ..versions import ManifestType, ManifestVersion
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

    Returns:
        A manifest with all entries but hash="" for files

    Note:
        - For v2023-03-03: Only files are collected (symlinks skipped, no dirs)
        - For v2025-12-04-beta: Files, symlinks, and directories are collected
        - Symlinks have symlink_target set (no hash needed)
        - Use _hash_manifest() to fill in file hashes
    """
    root_path = Path(os.path.normpath(os.path.abspath(root)))
    if version == ManifestVersion.v2023_03_03:
        return _collect_manifest_directory_tree_v2023(root_path, print_function_callback, absolute_paths=absolute_paths)
    elif version == ManifestVersion.v2025_12_04_beta:
        return _collect_manifest_directory_tree_v2025(root_path, print_function_callback, absolute_paths=absolute_paths)
    else:
        raise ValueError(f"Unsupported manifest version: {version}")


def _collect_manifest_directory_tree_v2023(
    root_path: Path,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
    *,
    absolute_paths: bool = False,
) -> AssetManifest2023:
    """
    Scan directory tree and create a v2023-03-03 manifest WITHOUT hashes.

    This function:
    1. Walks the directory tree
    2. Collects only regular files (symlinks are skipped)
    3. Captures metadata (mtime, size)
    4. Sets hash="" for all file entries (to be filled by _hash_manifest)

    Note: Does NOT compute file hashes. Use _hash_manifest() to fill in hashes.
    """

    file_entries: List[ManifestPath2023] = []
    total_size = 0

    for dirpath, _, filenames in os.walk(root_path, followlinks=False):
        for name in filenames:
            full_path = Path(dirpath) / name

            # Skip symlinks in v2023 format
            if full_path.is_symlink():
                print_function_callback(f"Skipping symlink: {full_path}")
                continue

            # Skip if not a regular file
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

    return AssetManifest2023(
        hash_alg=HashAlgorithm.XXH128,
        paths=file_entries,
        total_size=total_size,
    )


def _collect_manifest_directory_tree_v2025(
    root_path: Path,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
    *,
    absolute_paths: bool = False,
) -> AssetManifest2025:
    """
    Scan directory tree and create a v2025-12-04-beta manifest WITHOUT hashes.

    This function:
    1. Walks the directory tree
    2. Collects files, symlinks, and directories
    3. Captures metadata (mtime, size, permissions)
    4. Sets hash="" for all file entries (to be filled by _hash_manifest)
    5. Validates symlink targets

    Note: Does NOT compute file hashes. Use _hash_manifest() to fill in hashes.
    """

    file_entries: List[ManifestFilePath2025] = []
    dir_entries: List[ManifestDirectoryPath2025] = []
    total_size = 0

    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
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
                if absolute_paths:
                    entry_path = full_path.absolute().as_posix()
                else:
                    entry_path = full_path.relative_to(root_path).as_posix()
                try:
                    entry = _create_symlink_entry(full_path, entry_path, root_path, absolute_paths=absolute_paths)
                    file_entries.append(entry)
                    print_function_callback(f"Collected symlink dir: {entry_path}")
                    # Remove from dirnames to prevent os.walk from following it
                    dirnames.remove(name)
                except ValueError as e:
                    print_function_callback(f"Skipping invalid symlink {entry_path}: {e}")
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
                # Create symlink entry (no hash needed)
                try:
                    entry = _create_symlink_entry(full_path, entry_path, root_path, absolute_paths=absolute_paths)
                    file_entries.append(entry)
                    print_function_callback(f"Collected symlink: {entry_path}")
                except ValueError as e:
                    print_function_callback(f"Skipping invalid symlink {entry_path}: {e}")
            else:
                # Create file entry WITHOUT hash
                try:
                    entry = _create_unhashed_file_entry(full_path, entry_path, stat_info)
                    file_entries.append(entry)
                    total_size += entry.size or 0
                    print_function_callback(f"Collected: {entry_path}")
                except OSError as e:
                    print_function_callback(f"Skipping inaccessible file {entry_path}: {e}")

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
