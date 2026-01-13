# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Symlink handling functions for the collect_abs_snapshot operation.

This module contains all symlink-related logic for the COLLECT operation:
- Symlink target resolution
- Policy-based symlink handling (PRESERVE, COLLAPSE_*, EXCLUDE_*, TRANSITIVE_*)
- Escaping symlink detection and collapsing
- Transitive target collection

These functions are used by _collect_abs_snapshot.py to handle symlinks
according to the specified SymlinkPolicy.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, List, Optional, Set, Tuple

from .._manifest import (
    ManifestDirectoryPath,
    ManifestFilePath,
    SymlinkPolicy,
)

logger = logging.getLogger(__name__)

# Type alias for the file entry creator function
FileEntryCreator = Callable[[Path, str, Optional[os.stat_result]], ManifestFilePath]


def _remove_longpath_prefix(path: Path) -> Path:
    """Returns a copy with '\\\\?\\' longpath prefix removed if the path has it."""
    if os.name == "nt" and path.parts[0].startswith("\\\\?\\"):
        return Path(path.parts[0][4:], *path.parts[1:])
    return path


def _get_symlink_absolute_target(full_path: Path) -> Path:
    """Get the absolute path of a symlink's target without resolving symlink chains."""
    target = full_path.parent / os.readlink(full_path)
    target_clean = _remove_longpath_prefix(target)
    return Path(os.path.normpath(target_clean.absolute()))


def _symlink_target_is_directory(full_path: Path) -> bool:
    """Check if a symlink's target is a directory."""
    try:
        target = full_path.parent / os.readlink(full_path)
        return target.is_dir()
    except (OSError, ValueError):
        return False


def _handle_symlink(
    full_path: Path,
    symlink_policy: SymlinkPolicy,
    is_directory: bool,
) -> Optional[Tuple[Optional[ManifestFilePath], bool, Optional[Path]]]:
    """Handle a symlink according to the symlink policy.

    Returns:
        None if symlink should be skipped entirely.
        Otherwise a tuple of:
        - entry: ManifestFilePath to add (or None if no entry)
        - should_follow: Whether to follow the symlink (for COLLAPSE_ALL)
        - transitive_target: Path to add transitively (for TRANSITIVE_INCLUDE_TARGETS)
    """
    entry_path = full_path.absolute().as_posix()

    if symlink_policy == SymlinkPolicy.EXCLUDE_ALL:
        logger.debug("Excluding symlink: %s", entry_path)
        return None

    elif symlink_policy == SymlinkPolicy.COLLAPSE_ALL:
        if is_directory:
            logger.debug("Following symlink dir: %s", entry_path)
            return (None, True, None)
        return (None, True, None)

    elif symlink_policy == SymlinkPolicy.PRESERVE:
        target = _get_symlink_absolute_target(full_path)
        entry = ManifestFilePath(
            path=entry_path,
            symlink_target=target.as_posix(),
        )
        kind = "symlink dir" if is_directory else "symlink"
        logger.debug("Collected %s: %s", kind, entry_path)
        return (entry, False, None)

    elif symlink_policy == SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS:
        target = _get_symlink_absolute_target(full_path)
        entry = ManifestFilePath(
            path=entry_path,
            symlink_target=target.as_posix(),
        )
        kind = "symlink dir" if is_directory else "symlink"
        logger.debug("Collected %s: %s", kind, entry_path)
        return (entry, False, target)

    elif symlink_policy == SymlinkPolicy.COLLAPSE_ESCAPING:
        # This should not be called for COLLAPSE_ESCAPING - it uses two-pass
        raise ValueError("COLLAPSE_ESCAPING should use two-pass processing, not _handle_symlink")

    elif symlink_policy == SymlinkPolicy.EXCLUDE_ESCAPING:
        # This should not be called for EXCLUDE_ESCAPING - it uses two-pass
        raise ValueError("EXCLUDE_ESCAPING should use two-pass processing, not _handle_symlink")

    else:
        raise ValueError(f"Unknown symlink policy: {symlink_policy}")


def process_deferred_symlink(
    full_path: Path,
    entry_path: str,
    is_directory: bool,
    symlink_policy: SymlinkPolicy,
    collected_paths: Set[str],
    is_path_in_collected_set: Callable[[Path], bool],
    file_entries: List[ManifestFilePath],
    escaping_dir_symlinks: List[Tuple[str, Path]],
    create_file_entry: FileEntryCreator,
) -> int:
    """Process a deferred symlink for COLLAPSE_ESCAPING or EXCLUDE_ESCAPING policy.

    Args:
        full_path: Full path to the symlink
        entry_path: Entry path string for the manifest
        is_directory: Whether the symlink target is a directory
        symlink_policy: The symlink policy being applied
        collected_paths: Set of already collected paths (modified in place)
        is_path_in_collected_set: Function to check if a path is in the collected set
        file_entries: List of file entries (modified in place)
        escaping_dir_symlinks: List of escaping dir symlinks to process later (modified in place)
        create_file_entry: Function to create an unhashed file entry

    Returns:
        Size added to total (0 for symlinks, file size for collapsed files)
    """
    if entry_path in collected_paths:
        return 0

    target = _get_symlink_absolute_target(full_path)
    target_in_set = is_path_in_collected_set(target)

    if target_in_set:
        # Target is within collected paths - preserve as symlink
        entry = ManifestFilePath(
            path=entry_path,
            symlink_target=target.as_posix(),
        )
        file_entries.append(entry)
        collected_paths.add(entry_path)
        kind = "symlink dir" if is_directory else "symlink"
        logger.debug("Collected %s (non-escaping): %s", kind, entry_path)
        return 0

    # Target is outside collected paths - handle based on policy
    if symlink_policy == SymlinkPolicy.EXCLUDE_ESCAPING:
        # Exclude escaping symlinks entirely
        kind = "dir symlink" if is_directory else "symlink"
        logger.debug("Excluding escaping %s: %s", kind, entry_path)
        return 0

    # COLLAPSE_ESCAPING: Collapse escaping symlinks
    if is_directory:
        # Queue for directory symlink collection
        escaping_dir_symlinks.append((entry_path, target))
        logger.debug("Collapsing escaping dir symlink: %s", entry_path)
        return 0

    # Collapse file symlink
    try:
        target_stat = full_path.stat(follow_symlinks=True)
        file_entry = create_file_entry(full_path, entry_path, target_stat)
        file_entries.append(file_entry)
        collected_paths.add(entry_path)
        size = file_entry.size or 0
        logger.debug("Collected (collapsed escaping symlink): %s", entry_path)
        return size
    except OSError as e:
        logger.debug("Skipping broken symlink %s: %s", entry_path, e)
        return 0


def _collect_transitive_target(
    target_path: Path,
    collected_paths: Set[str],
    create_file_entry: FileEntryCreator,
    _visiting: Optional[Set[str]] = None,
) -> Tuple[List[ManifestFilePath], List[ManifestDirectoryPath], int]:
    """Collect a symlink target that is outside the root path.

    For TRANSITIVE_INCLUDE_TARGETS policy, symlinks found within transitive
    targets are also preserved and their targets are transitively included.

    Args:
        target_path: The symlink target path to collect.
        collected_paths: Set of already collected paths (modified in place).
        create_file_entry: Function to create an unhashed file entry.
        _visiting: Internal parameter for cycle detection - paths currently being visited.

    Symlink Cycle Handling:
        Symlink cycles (e.g., A -> B -> A) are detected and logged as warnings.
        When a cycle is detected, the cyclic symlink is skipped to prevent
        infinite recursion.
    """
    # Initialize visiting set for cycle detection
    if _visiting is None:
        _visiting = set()

    file_entries: List[ManifestFilePath] = []
    dir_entries: List[ManifestDirectoryPath] = []
    total_size = 0
    # Track additional transitive targets discovered from nested symlinks
    nested_transitive_targets: List[Path] = []

    if not target_path.exists():
        logger.debug("Skipping broken symlink target: %s", target_path)
        return (file_entries, dir_entries, total_size)

    # Normalize path for cycle detection
    target_posix = target_path.as_posix()

    # Check for cycle
    if target_posix in _visiting:
        logger.warning("Symlink cycle detected, skipping: %s", target_posix)
        return (file_entries, dir_entries, total_size)

    # Mark as visiting
    _visiting.add(target_posix)

    try:
        if target_path.is_symlink():
            # Target is itself a symlink - preserve it and transitively include its target
            entry_path = target_path.as_posix()
            if entry_path not in collected_paths:
                symlink_target = _get_symlink_absolute_target(target_path)
                entry = ManifestFilePath(
                    path=entry_path,
                    symlink_target=symlink_target.as_posix(),
                )
                file_entries.append(entry)
                collected_paths.add(entry_path)
                logger.debug("Collected transitive symlink target: %s", entry_path)
                # Queue the symlink's target for transitive collection
                nested_transitive_targets.append(symlink_target)

        elif target_path.is_file():
            entry_path = target_path.as_posix()
            if entry_path not in collected_paths:
                try:
                    entry = create_file_entry(target_path, entry_path, None)
                    file_entries.append(entry)
                    collected_paths.add(entry_path)
                    total_size += entry.size or 0
                    logger.debug("Collected transitive target: %s", entry_path)
                except OSError as e:
                    logger.debug("Skipping inaccessible transitive target %s: %s", entry_path, e)

        elif target_path.is_dir():
            for dirpath, dirnames, filenames in os.walk(target_path, followlinks=False):
                dir_abs = Path(dirpath).absolute()
                if dir_abs != target_path:
                    dir_path = dir_abs.as_posix()
                    if dir_path not in collected_paths:
                        dir_entries.append(ManifestDirectoryPath(path=dir_path))
                        collected_paths.add(dir_path)
                        logger.debug("Collected transitive dir: %s", dir_path)

                for name in list(dirnames):
                    full_path = Path(dirpath) / name
                    if full_path.is_symlink():
                        # Preserve the symlink and queue its target for transitive collection
                        entry_path = full_path.absolute().as_posix()
                        if entry_path not in collected_paths:
                            symlink_target = _get_symlink_absolute_target(full_path)
                            entry = ManifestFilePath(
                                path=entry_path,
                                symlink_target=symlink_target.as_posix(),
                            )
                            file_entries.append(entry)
                            collected_paths.add(entry_path)
                            logger.debug("Collected transitive symlink dir: %s", entry_path)
                            # Queue the target for transitive collection
                            nested_transitive_targets.append(symlink_target)
                        # Don't follow directory symlinks in os.walk
                        dirnames.remove(name)

                for name in filenames:
                    full_path = Path(dirpath) / name
                    entry_path = full_path.absolute().as_posix()
                    if entry_path in collected_paths:
                        continue
                    if full_path.is_symlink():
                        # Preserve the symlink and queue its target for transitive collection
                        symlink_target = _get_symlink_absolute_target(full_path)
                        entry = ManifestFilePath(
                            path=entry_path,
                            symlink_target=symlink_target.as_posix(),
                        )
                        file_entries.append(entry)
                        collected_paths.add(entry_path)
                        logger.debug("Collected transitive symlink: %s", entry_path)
                        # Queue the target for transitive collection
                        nested_transitive_targets.append(symlink_target)
                        continue
                    try:
                        stat_info = full_path.stat(follow_symlinks=False)
                        entry = create_file_entry(full_path, entry_path, stat_info)
                        file_entries.append(entry)
                        collected_paths.add(entry_path)
                        total_size += entry.size or 0
                        logger.debug("Collected transitive: %s", entry_path)
                    except OSError as e:
                        logger.debug("Skipping inaccessible transitive file %s: %s", entry_path, e)

        # Recursively collect nested transitive targets
        for nested_target in nested_transitive_targets:
            nested_files, nested_dirs, nested_size = _collect_transitive_target(
                target_path=nested_target,
                collected_paths=collected_paths,
                create_file_entry=create_file_entry,
                _visiting=_visiting,
            )
            file_entries.extend(nested_files)
            dir_entries.extend(nested_dirs)
            total_size += nested_size

    finally:
        # Remove from visiting set when done
        _visiting.discard(target_posix)

    return (file_entries, dir_entries, total_size)


def _collect_escaping_dir_symlink(
    symlink_path: str,
    target_abs_path: Path,
    collected_paths: Set[str],
    create_file_entry: FileEntryCreator,
    _visiting: Optional[Set[str]] = None,
) -> Tuple[List[ManifestFilePath], List[ManifestDirectoryPath], int]:
    """Collect contents of an escaping directory symlink.

    The contents are collected with paths under the symlink path, not the target path.
    This effectively "inlines" the target directory contents at the symlink location.

    Nested symlinks within the collapsed directory are handled as follows:
    - If the symlink target is within the same directory being collapsed, the symlink
      is preserved with its target translated to the collapsed location.
    - If the symlink target escapes the directory being collapsed, it is collapsed
      recursively.

    Args:
        symlink_path: The path where the symlink appears in the manifest.
        target_abs_path: The absolute path of the symlink's target directory.
        collected_paths: Set of already collected paths (modified in place).
        create_file_entry: Function to create an unhashed file entry.
        _visiting: Internal parameter for cycle detection - target paths currently being visited.

    Symlink Cycle Handling:
        Symlink cycles (e.g., A -> B -> A) are detected and logged as warnings.
        When a cycle is detected, the cyclic symlink is skipped to prevent
        infinite recursion.
    """
    # Initialize visiting set for cycle detection
    if _visiting is None:
        _visiting = set()

    file_entries: List[ManifestFilePath] = []
    dir_entries: List[ManifestDirectoryPath] = []
    total_size = 0

    if not target_abs_path.exists():
        logger.debug("Skipping broken escaping symlink: %s", symlink_path)
        return (file_entries, dir_entries, total_size)

    if not target_abs_path.is_dir():
        logger.debug("Skipping non-directory escaping symlink target: %s", symlink_path)
        return (file_entries, dir_entries, total_size)

    # Normalize path for cycle detection
    target_posix = target_abs_path.as_posix()

    # Check for cycle
    if target_posix in _visiting:
        logger.warning("Symlink cycle detected, skipping: %s -> %s", symlink_path, target_posix)
        return (file_entries, dir_entries, total_size)

    # Mark as visiting
    _visiting.add(target_posix)

    try:

        def is_target_within_collapsed_dir(link_path: Path) -> Tuple[bool, Optional[str]]:
            """Check if a symlink's target is within the directory being collapsed."""
            try:
                raw_target = os.readlink(link_path)
                resolved_target = (link_path.parent / raw_target).resolve()
                try:
                    rel_to_target = resolved_target.relative_to(target_abs_path)
                    translated = f"{symlink_path}/{rel_to_target.as_posix()}"
                    return (True, translated)
                except ValueError:
                    return (False, None)
            except (OSError, ValueError):
                return (False, None)

        for dirpath, dirnames, filenames in os.walk(target_abs_path, followlinks=False):
            rel_within_target = Path(dirpath).relative_to(target_abs_path)

            if rel_within_target == Path("."):
                dir_entry_path = symlink_path
            else:
                dir_entry_path = f"{symlink_path}/{rel_within_target.as_posix()}"

            if dir_entry_path not in collected_paths:
                dir_entries.append(ManifestDirectoryPath(path=dir_entry_path))
                collected_paths.add(dir_entry_path)
                logger.debug("Collected dir (escaping): %s", dir_entry_path)

            for name in list(dirnames):
                full_path = Path(dirpath) / name
                if full_path.is_symlink():
                    is_internal, translated_target = is_target_within_collapsed_dir(full_path)
                    if rel_within_target == Path("."):
                        entry_path = f"{symlink_path}/{name}"
                    else:
                        entry_path = f"{symlink_path}/{rel_within_target.as_posix()}/{name}"

                    if is_internal and translated_target is not None:
                        if entry_path not in collected_paths:
                            entry = ManifestFilePath(
                                path=entry_path,
                                symlink_target=translated_target,
                            )
                            file_entries.append(entry)
                            collected_paths.add(entry_path)
                            logger.debug(
                                "Collected symlink dir (internal, translated): %s", entry_path
                            )
                    else:
                        try:
                            nested_target = full_path.resolve()
                            if nested_target.exists() and nested_target.is_dir():
                                nested_files, nested_dirs, nested_size = (
                                    _collect_escaping_dir_symlink(
                                        symlink_path=entry_path,
                                        target_abs_path=nested_target,
                                        collected_paths=collected_paths,
                                        create_file_entry=create_file_entry,
                                        _visiting=_visiting,
                                    )
                                )
                                file_entries.extend(nested_files)
                                dir_entries.extend(nested_dirs)
                                total_size += nested_size
                                logger.debug(
                                    "Collapsed nested escaping dir symlink: %s", entry_path
                                )
                            else:
                                logger.debug("Skipping broken nested dir symlink: %s", entry_path)
                        except OSError as e:
                            logger.debug(
                                "Skipping inaccessible nested dir symlink %s: %s", entry_path, e
                            )
                    dirnames.remove(name)

            for name in filenames:
                full_path = Path(dirpath) / name
                if rel_within_target == Path("."):
                    entry_path = f"{symlink_path}/{name}"
                else:
                    entry_path = f"{symlink_path}/{rel_within_target.as_posix()}/{name}"

                if entry_path in collected_paths:
                    continue

                if full_path.is_symlink():
                    is_internal, translated_target = is_target_within_collapsed_dir(full_path)
                    if is_internal and translated_target is not None:
                        entry = ManifestFilePath(
                            path=entry_path,
                            symlink_target=translated_target,
                        )
                        file_entries.append(entry)
                        collected_paths.add(entry_path)
                        logger.debug("Collected symlink (internal, translated): %s", entry_path)
                    else:
                        try:
                            target_stat = full_path.stat(follow_symlinks=True)
                            file_entry = create_file_entry(full_path, entry_path, target_stat)
                            file_entries.append(file_entry)
                            collected_paths.add(entry_path)
                            total_size += file_entry.size or 0
                            logger.debug("Collapsed nested escaping symlink: %s", entry_path)
                        except OSError as e:
                            logger.debug("Skipping broken nested symlink %s: %s", entry_path, e)
                    continue

                try:
                    stat_info = full_path.stat(follow_symlinks=False)
                    entry = create_file_entry(full_path, entry_path, stat_info)
                    file_entries.append(entry)
                    collected_paths.add(entry_path)
                    total_size += entry.size or 0
                    logger.debug("Collected (escaping): %s", entry_path)
                except OSError as e:
                    logger.debug(
                        "Skipping inaccessible file in escaping target %s: %s", entry_path, e
                    )

    finally:
        _visiting.discard(target_posix)

    return (file_entries, dir_entries, total_size)
