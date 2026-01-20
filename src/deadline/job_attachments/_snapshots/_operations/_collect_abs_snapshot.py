# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Module for collecting directory structure into manifest objects WITHOUT computing hashes.

This module implements the COLLECT operation from the composable manifest operations design:
    COLLECT: Paths → AbsSnapshot (with hash=None for files, absolute paths)

The separation of collection from hashing enables:
- Fast diff comparison by mtime/size without hashing unchanged files
- Hash cache integration - only hash files with cache misses
- Deferred hashing - collect structure first, hash only what's needed
- Remove redundant reads - Can read file to memory, then hash + upload instead of
  separate reads for hash and upload.

All composable operations use v2025 structure and semantics internally. Support for
v2023 on-disk format is provided via lossy conversion functions that drop symlinks,
deletions, and other v2025-only features.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import stat
from typing import List, Optional, Set, Tuple

from ...asset_manifests.hash_algorithms import HashAlgorithm
from .._manifest import (
    AbsSnapshot,
    ManifestDirectoryPath,
    ManifestFilePath,
    SymlinkPolicy,
    DEFAULT_FILE_CHUNK_SIZE,
)
from ._collect_abs_snapshot_symlinks import (
    _get_symlink_absolute_target,
    _symlink_target_is_directory,
    _handle_symlink,
    process_deferred_symlink,
    _collect_transitive_target,
    _collect_escaping_dir_symlink,
)

logger = logging.getLogger(__name__)


def collect_abs_snapshot(
    directories: List[Path | str],
    filenames: List[Path | str],
    *,
    optional_filenames: Optional[List[Path | str]] = None,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
    file_chunk_size_bytes: Optional[int] = None,
) -> AbsSnapshot:
    """
    Collect provided lists of paths into a manifest with absolute paths.

    This function:
    1. Collects the specified directories and filenames
    2. Captures metadata (mtime, size, permissions)
    3. Sets hash=None for all file entries (hashes not yet computed)
    4. Returns an AbsSnapshot with absolute paths

    Args:
        directories: List of directory paths whose full contents are collected.
            All paths must exist and be directories. Empty directories are
            included in the manifest.
        filenames: List of file/symlink paths that must exist. Raises an
            exception if any file from this list does not exist on the filesystem.
        optional_filenames: List of file/symlink paths to include if they exist.
            Missing files are silently ignored.
        symlink_policy: How to handle symlinks during collection:
            - COLLAPSE_ESCAPING: Preserve symlinks whose targets are within the
              collected paths; collapse symlinks whose targets are outside
              (escaping symlinks) to files/directories. (default)
            - COLLAPSE_ALL: Follow all symlinks, treating them as files/directories.
            - PRESERVE: Keep all symlinks with absolute targets.
            - TRANSITIVE_INCLUDE_TARGETS: Keep symlinks and add their targets.
            - EXCLUDE_ALL: Skip all symlinks entirely.
            - EXCLUDE_ESCAPING: Preserve symlinks whose targets are within the
              collected paths; exclude symlinks whose targets are outside.
        file_chunk_size_bytes: Chunk size for large file hashing.
            - None: Use DEFAULT_FILE_CHUNK_SIZE (256MB) (default)
            - WHOLE_FILE_CHUNK_SIZE (-1): Hash files as a whole, no chunking
            - Positive int: Chunk size in bytes for large files

    Returns:
        An AbsSnapshot with absolute paths and hash=None for files

    Raises:
        FileNotFoundError: If any directory does not exist.
        FileNotFoundError: If any file in filenames does not exist.
        ValueError: If any path in directories is not a directory.
        ValueError: If any path in filenames is not a file or symlink.

    Note:
        - Use hash_abs_manifest() to fill in file hashes
        - All composable operations use v2025 structure internally
        - For v2023 on-disk format, use lossy conversion after processing
    """
    # Validate and normalize input paths
    validated_directories: List[Path] = []
    validated_filenames: List[Path] = []
    validated_optional: List[Path] = []

    for p in directories:
        abs_path = Path(os.path.normpath(os.path.abspath(p)))
        if not abs_path.exists():
            raise FileNotFoundError(f"Directory does not exist: {abs_path}")
        if not abs_path.is_dir():
            raise ValueError(f"Path is not a directory: {abs_path}")
        validated_directories.append(abs_path)

    for p in filenames:
        abs_path = Path(os.path.normpath(os.path.abspath(p)))
        if not abs_path.exists():
            raise FileNotFoundError(f"File does not exist: {abs_path}")
        if not abs_path.is_file() and not abs_path.is_symlink():
            raise ValueError(f"Path is not a file or symlink: {abs_path}")
        validated_filenames.append(abs_path)

    if optional_filenames:
        for p in optional_filenames:
            abs_path = Path(os.path.normpath(os.path.abspath(p)))
            # Only add if it exists and is a file/symlink
            if abs_path.exists() and (abs_path.is_file() or abs_path.is_symlink()):
                validated_optional.append(abs_path)

    return _collect_abs_snapshot_impl(
        symlink_policy=symlink_policy,
        filenames=validated_filenames,
        optional_filenames=validated_optional,
        directories=validated_directories,
        file_chunk_size_bytes=file_chunk_size_bytes,
    )


# =============================================================================
# Internal implementation functions
# =============================================================================


def _create_unhashed_file_entry(
    full_path: Path,
    entry_path: str,
    stat_info: Optional[os.stat_result] = None,
) -> ManifestFilePath:
    """Create a ManifestFilePath entry for a regular file WITHOUT computing hash.

    The returned entry has hash=None and chunkhashes=None to indicate that
    hashes have not been computed yet.
    """
    if stat_info is None:
        stat_info = full_path.stat()
    runnable = bool(stat_info.st_mode & 0o111)
    return ManifestFilePath(
        path=entry_path,
        hash=None,
        size=stat_info.st_size,
        mtime=stat_info.st_mtime_ns // 1000,
        runnable=runnable if runnable else False,
    )


def _collect_abs_snapshot_impl(
    *,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.PRESERVE,
    filenames: List[Path],
    optional_filenames: List[Path],
    directories: List[Path],
    file_chunk_size_bytes: Optional[int] = None,
) -> AbsSnapshot:
    """Collect files and directories into a manifest WITHOUT hashes.

    For COLLAPSE_ESCAPING policy, this uses a two-pass approach:
    1. First pass: Collect all non-symlink paths to build the "collected set"
    2. Second pass: Process symlinks - preserve if target is in collected set,
       collapse if target is outside (escaping)

    Files are collected with hash=None to indicate hashes have not been computed.
    Use hash_abs_manifest() or hash_upload_abs_manifest() to fill in hashes.

    Args:
        file_chunk_size_bytes: Chunk size for large file hashing.
            None means use DEFAULT_FILE_CHUNK_SIZE (256MB).
    """
    file_entries: List[ManifestFilePath] = []
    dir_entries: List[ManifestDirectoryPath] = []
    total_size = 0
    followlinks = symlink_policy == SymlinkPolicy.COLLAPSE_ALL
    collected_paths: Set[str] = set()
    transitive_targets: List[Path] = []

    # For COLLAPSE_ESCAPING, we need to track symlinks for deferred processing
    deferred_symlinks: List[Tuple[Path, str, bool]] = []  # (full_path, entry_path, is_dir)
    escaping_dir_symlinks: List[Tuple[str, Path]] = []

    def is_path_in_collected_set(target_path: Path) -> bool:
        """Check if a path or any of its parents is in the collected set."""
        target_posix = target_path.as_posix()
        # Direct match
        if target_posix in collected_paths:
            return True
        # Check if target is under any collected directory
        for collected in collected_paths:
            if target_posix.startswith(collected + "/"):
                return True
        return False

    def collect_file(full_path: Path, defer_symlinks: bool = False) -> None:
        """Collect a single file entry."""
        nonlocal total_size
        stat_info = full_path.stat(follow_symlinks=False)
        entry_path = full_path.absolute().as_posix()

        if entry_path in collected_paths:
            return

        if stat.S_ISLNK(stat_info.st_mode):
            if defer_symlinks:
                # For COLLAPSE_ESCAPING, defer symlink processing
                symlink_target_is_dir = _symlink_target_is_directory(full_path)
                deferred_symlinks.append((full_path, entry_path, symlink_target_is_dir))
            else:
                symlink_target_is_dir = _symlink_target_is_directory(full_path)
                result = _handle_symlink(
                    full_path=full_path,
                    symlink_policy=symlink_policy,
                    is_directory=symlink_target_is_dir,
                )
                if result is not None:
                    entry, should_follow, transitive_target = result
                    if entry is not None:
                        file_entries.append(entry)
                        collected_paths.add(entry.path)
                    if transitive_target is not None:
                        transitive_targets.append(transitive_target)
                    if should_follow and not symlink_target_is_dir:
                        try:
                            target_stat = full_path.stat(follow_symlinks=True)
                            file_entry = _create_unhashed_file_entry(
                                full_path, entry_path, target_stat
                            )
                            file_entries.append(file_entry)
                            collected_paths.add(entry_path)
                            total_size += file_entry.size or 0
                            logger.debug("Collected (collapsed symlink): %s", entry_path)
                        except OSError as e:
                            logger.debug("Skipping broken symlink %s: %s", entry_path, e)
        elif stat.S_ISREG(stat_info.st_mode):
            # Only collect regular files, skip device files (NUL, CON, etc. on Windows)
            try:
                entry = _create_unhashed_file_entry(full_path, entry_path, stat_info)
                file_entries.append(entry)
                collected_paths.add(entry_path)
                total_size += entry.size or 0
                logger.debug("Collected: %s", entry_path)
            except OSError as e:
                logger.debug("Skipping inaccessible file %s: %s", entry_path, e)
        else:
            # Skip non-regular files (device files, sockets, etc.)
            logger.debug("Skipping non-regular file %s (mode: %o)", entry_path, stat_info.st_mode)

    # Determine if we need two-pass processing
    use_two_pass = symlink_policy in (
        SymlinkPolicy.COLLAPSE_ESCAPING,
        SymlinkPolicy.EXCLUDE_ESCAPING,
    )

    # =========================================================================
    # Pass 1: Collect all paths (defer symlinks for COLLAPSE_ESCAPING)
    # =========================================================================

    for full_path in filenames:
        collect_file(full_path, defer_symlinks=use_two_pass)
    for full_path in optional_filenames:
        collect_file(full_path, defer_symlinks=use_two_pass)

    for dir_to_walk in directories:
        for dirpath, dirnames, walk_filenames in os.walk(dir_to_walk, followlinks=followlinks):
            current_dir = Path(dirpath)
            dir_path = current_dir.absolute().as_posix()
            if dir_path not in collected_paths:
                dir_entries.append(ManifestDirectoryPath(path=dir_path))
                collected_paths.add(dir_path)
                logger.debug("Collected dir: %s", dir_path)

            for name in list(dirnames):
                full_path = Path(dirpath) / name
                if full_path.is_symlink():
                    if use_two_pass:
                        # Defer symlink processing, but don't follow it in os.walk
                        entry_path = full_path.absolute().as_posix()
                        deferred_symlinks.append((full_path, entry_path, True))
                        dirnames.remove(name)
                    else:
                        result = _handle_symlink(
                            full_path=full_path,
                            symlink_policy=symlink_policy,
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
                                if symlink_policy == SymlinkPolicy.COLLAPSE_ALL:
                                    pass  # os.walk will follow it
                                else:
                                    dirnames.remove(name)
                            else:
                                dirnames.remove(name)
                        else:
                            dirnames.remove(name)

            for name in walk_filenames:
                full_path = Path(dirpath) / name
                stat_info = full_path.stat(follow_symlinks=False)
                entry_path = full_path.absolute().as_posix()

                if entry_path in collected_paths:
                    continue

                if stat.S_ISLNK(stat_info.st_mode):
                    if use_two_pass:
                        symlink_target_is_dir = _symlink_target_is_directory(full_path)
                        deferred_symlinks.append((full_path, entry_path, symlink_target_is_dir))
                    else:
                        symlink_target_is_dir = _symlink_target_is_directory(full_path)
                        result = _handle_symlink(
                            full_path=full_path,
                            symlink_policy=symlink_policy,
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
                                    if symlink_policy == SymlinkPolicy.COLLAPSE_ALL:
                                        rel_path = full_path.absolute().as_posix()
                                        target = _get_symlink_absolute_target(full_path)
                                        escaping_dir_symlinks.append((rel_path, target))
                                else:
                                    try:
                                        target_stat = full_path.stat(follow_symlinks=True)
                                        file_entry = _create_unhashed_file_entry(
                                            full_path, entry_path, target_stat
                                        )
                                        file_entries.append(file_entry)
                                        collected_paths.add(entry_path)
                                        total_size += file_entry.size or 0
                                        logger.debug(
                                            "Collected (collapsed symlink): %s", entry_path
                                        )
                                    except OSError as e:
                                        logger.debug(
                                            "Skipping broken symlink %s: %s", entry_path, e
                                        )
                elif stat.S_ISREG(stat_info.st_mode):
                    # Only collect regular files, skip device files (NUL, CON, etc. on Windows)
                    try:
                        entry = _create_unhashed_file_entry(full_path, entry_path, stat_info)
                        file_entries.append(entry)
                        collected_paths.add(entry_path)
                        total_size += entry.size or 0
                        logger.debug("Collected: %s", entry_path)
                    except OSError as e:
                        logger.debug("Skipping inaccessible file %s: %s", entry_path, e)
                else:
                    # Skip non-regular files (device files, sockets, etc.)
                    logger.debug(
                        "Skipping non-regular file %s (mode: %o)", entry_path, stat_info.st_mode
                    )

    # =========================================================================
    # Pass 2: Process deferred symlinks (COLLAPSE_ESCAPING only)
    # =========================================================================

    if use_two_pass:
        for full_path, entry_path, is_dir in deferred_symlinks:
            size_added = process_deferred_symlink(
                full_path=full_path,
                entry_path=entry_path,
                is_directory=is_dir,
                symlink_policy=symlink_policy,
                collected_paths=collected_paths,
                is_path_in_collected_set=is_path_in_collected_set,
                file_entries=file_entries,
                escaping_dir_symlinks=escaping_dir_symlinks,
                create_file_entry=_create_unhashed_file_entry,
            )
            total_size += size_added

    # =========================================================================
    # Post-processing: Handle escaping directory symlinks and transitive targets
    # =========================================================================

    for symlink_path, target_abs_path in escaping_dir_symlinks:
        entries, dirs, size = _collect_escaping_dir_symlink(
            symlink_path=symlink_path,
            target_abs_path=target_abs_path,
            collected_paths=collected_paths,
            create_file_entry=_create_unhashed_file_entry,
        )
        file_entries.extend(entries)
        dir_entries.extend(dirs)
        total_size += size

    for target_path in transitive_targets:
        entries, dirs, size = _collect_transitive_target(
            target_path=target_path,
            collected_paths=collected_paths,
            create_file_entry=_create_unhashed_file_entry,
        )
        file_entries.extend(entries)
        dir_entries.extend(dirs)
        total_size += size

    # Apply default chunk size if not specified
    output_chunk_size = (
        file_chunk_size_bytes if file_chunk_size_bytes is not None else DEFAULT_FILE_CHUNK_SIZE
    )

    return AbsSnapshot(
        hash_alg=HashAlgorithm.XXH128,
        dirs=dir_entries,
        files=file_entries,
        total_size=total_size,
        file_chunk_size_bytes=output_chunk_size,
    )
