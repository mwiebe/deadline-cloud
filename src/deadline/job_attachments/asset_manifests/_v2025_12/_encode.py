# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
EXPERIMENTAL: Encode unified manifest classes to v2025-12 JSON format.

This module provides the encode_v2025() function that serializes unified manifest
classes (AbsSnapshotManifest, RelSnapshotManifest, etc.) to canonical JSON.

This format is under development and subject to change. Do not use in production.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ..._snapshots import (
    AbsDiffManifest,
    AbsSnapshotManifest,
    Manifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    RelDiffManifest,
    RelSnapshotManifest,
)
from ...exceptions import ManifestDecodeValidationError


# Specification version strings
SPEC_ABS_SNAPSHOT = "absolute-manifest-snapshot-beta-2025-12"
SPEC_ABS_DIFF = "absolute-manifest-diff-beta-2025-12"
SPEC_REL_SNAPSHOT = "relative-manifest-snapshot-beta-2025-12"
SPEC_REL_DIFF = "relative-manifest-diff-beta-2025-12"


def encode_v2025(manifest: Manifest) -> str:
    """
    Encode a unified manifest to v2025-12 JSON format.

    Args:
        manifest: Any unified manifest (AbsSnapshotManifest, RelSnapshotManifest, etc.)

    Returns:
        Canonical JSON string with specificationVersion field

    Raises:
        ManifestDecodeValidationError: If manifest contains invalid data

    Note:
        Automatically adds all parent directories needed for $N/ compression,
        even if they weren't explicitly included in manifest.dirs.
    """
    # Determine specificationVersion from manifest type
    spec_version = _get_specification_version(manifest)

    # Validate symlink targets for relative manifests
    if isinstance(manifest, (RelSnapshotManifest, RelDiffManifest)):
        for f in manifest.files:
            if f.symlink_target is not None:
                f._validate_symlink_target_relative()

    # Collect all directories needed for encoding (explicit + inferred from paths)
    all_dirs = _collect_all_directories(manifest.dirs, manifest.files)

    # Sort and deduplicate directories
    sorted_dirs = _sort_and_dedupe_dirs(all_dirs)

    # Build directory index for $N/ compression
    dir_index: Dict[str, int] = {d.path: i for i, d in enumerate(sorted_dirs)}

    # Encode directories
    dirs_json = _encode_dirs(sorted_dirs, dir_index)

    # Sort and deduplicate files
    sorted_files = _sort_and_dedupe_files(manifest.files)

    # Encode files
    files_json = _encode_files(sorted_files, dir_index)

    # Build manifest dictionary with canonical key order
    manifest_dict: Dict[str, Any] = {
        "dirs": dirs_json,
        "files": files_json,
        "hashAlg": manifest.hashAlg.value,
        "specificationVersion": spec_version,
        "totalSize": manifest.totalSize,
    }

    # Add parentManifestHash if present
    if manifest.parentManifestHash is not None:
        manifest_dict["parentManifestHash"] = manifest.parentManifestHash

    return json.dumps(manifest_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _get_specification_version(manifest: Manifest) -> str:
    """Get the specificationVersion string for a manifest type."""
    if isinstance(manifest, AbsSnapshotManifest):
        return SPEC_ABS_SNAPSHOT
    elif isinstance(manifest, AbsDiffManifest):
        return SPEC_ABS_DIFF
    elif isinstance(manifest, RelSnapshotManifest):
        return SPEC_REL_SNAPSHOT
    elif isinstance(manifest, RelDiffManifest):
        return SPEC_REL_DIFF
    else:
        raise ManifestDecodeValidationError(f"Unknown manifest type: {type(manifest).__name__}")


def _sort_and_dedupe_dirs(dirs: List[ManifestDirectoryPath]) -> List[ManifestDirectoryPath]:
    """Sort and deduplicate directories by path, validating duplicates are identical."""
    seen: Dict[str, ManifestDirectoryPath] = {}
    unique: List[ManifestDirectoryPath] = []

    for d in dirs:
        if d.path in seen:
            existing = seen[d.path]
            if d.deleted != existing.deleted:
                raise ManifestDecodeValidationError(
                    f"Duplicate directory '{d.path}' has conflicting 'deleted' values"
                )
        else:
            seen[d.path] = d
            unique.append(d)

    return sorted(unique, key=lambda d: d.path)


def _sort_and_dedupe_files(files: List[ManifestFilePath]) -> List[ManifestFilePath]:
    """Sort and deduplicate files by path, validating duplicates are identical."""
    seen: Dict[str, ManifestFilePath] = {}
    unique: List[ManifestFilePath] = []

    for f in files:
        if f.path in seen:
            _validate_duplicate_file(f, seen[f.path])
        else:
            seen[f.path] = f
            unique.append(f)

    # Sort by UTF-16 BE encoding for canonical ordering
    return sorted(unique, key=lambda f: f.path.encode("utf-16_be", errors="surrogatepass"))


def _validate_duplicate_file(file: ManifestFilePath, existing: ManifestFilePath) -> None:
    """Validate that a duplicate file entry is identical to the existing one."""
    path = file.path
    if file.deleted != existing.deleted:
        raise ManifestDecodeValidationError(
            f"Duplicate file '{path}' has conflicting 'deleted' values"
        )
    if file.hash != existing.hash:
        raise ManifestDecodeValidationError(
            f"Duplicate file '{path}' has conflicting 'hash' values"
        )
    if file.size != existing.size:
        raise ManifestDecodeValidationError(
            f"Duplicate file '{path}' has conflicting 'size' values"
        )
    if file.mtime != existing.mtime:
        raise ManifestDecodeValidationError(
            f"Duplicate file '{path}' has conflicting 'mtime' values"
        )
    if file.runnable != existing.runnable:
        raise ManifestDecodeValidationError(
            f"Duplicate file '{path}' has conflicting 'runnable' values"
        )
    if file.chunkhashes != existing.chunkhashes:
        raise ManifestDecodeValidationError(
            f"Duplicate file '{path}' has conflicting 'chunkhashes' values"
        )
    if file.symlink_target != existing.symlink_target:
        raise ManifestDecodeValidationError(
            f"Duplicate file '{path}' has conflicting 'symlink_target' values"
        )


def _encode_dirs(
    dirs: List[ManifestDirectoryPath], dir_index: Dict[str, int]
) -> List[Dict[str, Any]]:
    """Encode directories with $N/ compression."""
    result: List[Dict[str, Any]] = []

    for d in dirs:
        encoded_name = _encode_path_with_dir_index(d.path, dir_index)
        entry: Dict[str, Any] = {"name": encoded_name}
        if d.deleted:
            entry["delete"] = True
        result.append(entry)

    return result


def _encode_files(files: List[ManifestFilePath], dir_index: Dict[str, int]) -> List[Dict[str, Any]]:
    """Encode files with $N/ compression."""
    result: List[Dict[str, Any]] = []

    for f in files:
        encoded_name = _encode_path_with_dir_index(f.path, dir_index)
        entry: Dict[str, Any] = {"name": encoded_name}

        # Add content field (exactly one of: hash, chunkhashes, symlink, or none for deleted)
        if f.hash is not None:
            entry["hash"] = f.hash
        elif f.chunkhashes is not None:
            entry["chunkhashes"] = f.chunkhashes
        elif f.symlink_target is not None:
            encoded_target = _encode_path_with_dir_index(f.symlink_target, dir_index)
            entry["symlink"] = {"name": encoded_target}

        # Add metadata fields (only for non-deleted, non-symlink entries)
        if not f.deleted and f.symlink_target is None:
            if f.size is not None:
                entry["size"] = f.size
            if f.mtime is not None:
                entry["mtime"] = f.mtime
            if f.runnable:
                entry["runnable"] = True

        # Deletion marker
        if f.deleted:
            entry["delete"] = True

        result.append(entry)

    return result


def _encode_path_with_dir_index(path: str, dir_index: Dict[str, int]) -> str:
    """
    Encode a path using directory index compression ($N/ references).

    For a path like "env_sandbox/RaceCarToy/file.blend":
    - If "env_sandbox/RaceCarToy" is at index 1, returns "$1/file.blend"
    - If no parent directory in index, returns the original path
    """
    last_slash = path.rfind("/")

    if last_slash == -1:
        return path

    dir_path = path[:last_slash]
    name = path[last_slash + 1 :]

    if dir_path in dir_index:
        return f"${dir_index[dir_path]}/{name}"

    return path


def _collect_all_directories(
    explicit_dirs: List[ManifestDirectoryPath],
    files: List[ManifestFilePath],
) -> List[ManifestDirectoryPath]:
    """
    Collect all directories needed for $N/ compression.

    This includes:
    - All explicitly provided directories
    - All parent directories of file paths (recursively)
    - All parent directories of directory paths (recursively)
    - All parent directories of symlink targets (recursively)

    When a directory appears both explicitly and is inferred, the explicit
    entry takes precedence (preserving its deleted flag).

    Args:
        explicit_dirs: Directories explicitly provided in the manifest
        files: File entries in the manifest

    Returns:
        Complete list of ManifestDirectoryPath objects for encoding
    """
    # Build map of explicit dirs (path -> ManifestDirectoryPath)
    explicit_map: Dict[str, ManifestDirectoryPath] = {d.path: d for d in explicit_dirs}

    # Collect all paths that need parent directories extracted
    all_paths: List[str] = []

    # Add file paths
    for f in files:
        all_paths.append(f.path)
        if f.symlink_target is not None:
            all_paths.append(f.symlink_target)

    # Add directory paths (to get their parents)
    for d in explicit_dirs:
        all_paths.append(d.path)

    # Extract all parent directories recursively
    inferred_dirs: set[str] = set()
    for path in all_paths:
        _extract_parent_dirs(path, inferred_dirs)

    # Merge: explicit dirs take precedence, add inferred dirs that aren't explicit
    result: List[ManifestDirectoryPath] = list(explicit_dirs)
    for dir_path in inferred_dirs:
        if dir_path not in explicit_map:
            result.append(ManifestDirectoryPath(path=dir_path, deleted=False))

    return result


def _extract_parent_dirs(path: str, result: set[str]) -> None:
    """
    Extract all parent directories from a path recursively.

    For "a/b/c/file.txt", adds "a/b/c", "a/b", and "a" to result.
    For "a/b/c" (directory), adds "a/b" and "a" to result.
    """
    last_slash = path.rfind("/")
    while last_slash > 0:
        parent = path[:last_slash]
        if parent in result:
            # Already processed this parent and its ancestors
            break
        result.add(parent)
        last_slash = parent.rfind("/")
