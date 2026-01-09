# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
EXPERIMENTAL: Decode v2025-12 JSON format to unified manifest classes.

This module provides the decode_v2025() function that deserializes JSON
to unified manifest classes (AbsSnapshotManifest, RelSnapshotManifest, etc.).

This format is under development and subject to change. Do not use in production.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Type

from ..hash_algorithms import HashAlgorithm
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
from ._validate import validate_manifest_2025_12


# Specification version to manifest class mapping
SPEC_VERSION_MAP: Dict[str, Type[Manifest]] = {
    "absolute-manifest-snapshot-beta-2025-12": AbsSnapshotManifest,
    "absolute-manifest-diff-beta-2025-12": AbsDiffManifest,
    "relative-manifest-snapshot-beta-2025-12": RelSnapshotManifest,
    "relative-manifest-diff-beta-2025-12": RelDiffManifest,
}

SUPPORTED_SPEC_VERSIONS = set(SPEC_VERSION_MAP.keys())


def decode_v2025(manifest_str: str) -> Manifest:
    """
    Decode a v2025-12 JSON string to a unified manifest.

    Args:
        manifest_str: JSON string with specificationVersion field

    Returns:
        Appropriate unified manifest type based on specificationVersion

    Raises:
        ManifestDecodeValidationError: If JSON is invalid or specificationVersion unknown
    """
    try:
        data: Dict[str, Any] = json.loads(manifest_str)
    except json.JSONDecodeError as e:
        raise ManifestDecodeValidationError(f"Invalid JSON: {e}")

    # Validate structure
    is_valid, error = validate_manifest_2025_12(data)
    if not is_valid:
        raise ManifestDecodeValidationError(error)

    # Get specificationVersion and determine manifest class
    spec_version = data["specificationVersion"]
    if spec_version not in SPEC_VERSION_MAP:
        raise ManifestDecodeValidationError(
            f"Unknown specificationVersion: {spec_version}. "
            f"Supported versions: {sorted(SUPPORTED_SPEC_VERSIONS)}"
        )

    manifest_class = SPEC_VERSION_MAP[spec_version]

    # Parse hash algorithm
    try:
        hash_alg = HashAlgorithm(data["hashAlg"])
    except ValueError:
        raise ManifestDecodeValidationError(f"Unsupported hash algorithm: {data['hashAlg']}")

    # Build directory index for $N/ expansion
    raw_dirs = data.get("dirs", [])
    dir_index = _build_dir_index(raw_dirs)

    # Decode directories
    dirs = _decode_dirs(raw_dirs, dir_index)

    # Decode files
    files = _decode_files(data.get("files", []), dir_index)

    # Get optional fields
    total_size = data["totalSize"]
    parent_hash: Optional[str] = data.get("parentManifestHash")

    # Create manifest instance
    return manifest_class(
        hash_alg=hash_alg,
        files=files,
        total_size=total_size,
        dirs=dirs,
        parent_manifest_hash=parent_hash,
    )


def _build_dir_index(raw_dirs: List[Dict[str, Any]]) -> Dict[int, str]:
    """
    Build directory index for $N/ expansion.

    Returns a mapping from index to full directory path.
    Handles nested $N/ references by expanding them in order.
    """
    index: Dict[int, str] = {}

    for i, d in enumerate(raw_dirs):
        name = d["name"]
        expanded = _expand_path_reference(name, index)
        index[i] = expanded

    return index


def _decode_dirs(
    raw_dirs: List[Dict[str, Any]], dir_index: Dict[int, str]
) -> List[ManifestDirectoryPath]:
    """Decode directory entries."""
    result: List[ManifestDirectoryPath] = []

    for i, d in enumerate(raw_dirs):
        path = dir_index[i]  # Already expanded
        deleted = d.get("delete", False)
        result.append(ManifestDirectoryPath(path=path, deleted=deleted))

    return result


def _decode_files(
    raw_files: List[Dict[str, Any]], dir_index: Dict[int, str]
) -> List[ManifestFilePath]:
    """Decode file entries."""
    result: List[ManifestFilePath] = []

    for f in raw_files:
        name = f["name"]
        path = _expand_path_reference(name, dir_index)

        # Determine content type
        file_hash: Optional[str] = f.get("hash")
        chunkhashes: Optional[List[str]] = f.get("chunkhashes")
        symlink_target: Optional[str] = None

        if "symlink" in f:
            symlink_name = f["symlink"]["name"]
            symlink_target = _expand_path_reference(symlink_name, dir_index)

        # Get metadata
        size: Optional[int] = f.get("size")
        mtime: Optional[int] = f.get("mtime")
        runnable: bool = f.get("runnable", False)
        deleted: bool = f.get("delete", False)

        result.append(
            ManifestFilePath(
                path=path,
                hash=file_hash,
                size=size,
                mtime=mtime,
                runnable=runnable,
                chunkhashes=chunkhashes,
                symlink_target=symlink_target,
                deleted=deleted,
            )
        )

    return result


def _expand_path_reference(name: str, dir_index: Dict[int, str]) -> str:
    """
    Expand $N/ references in a path (directory or file).

    The encoded format guarantees at most one "/" in each name due to
    parent directory auto-collection during encoding.

    Args:
        name: Encoded path, either plain or with $N/ prefix
        dir_index: Mapping from directory index to full path

    Returns:
        Expanded full path

    Raises:
        ManifestDecodeValidationError: If path has invalid format or references invalid index

    Examples:
        - "file.txt" -> "file.txt"
        - "$0/file.txt" -> "{dir_index[0]}/file.txt"
        - "$5/subdir" -> "{dir_index[5]}/subdir"
    """
    parts = name.split("/")

    if len(parts) == 1:
        # No "/" - plain filename (returned as-is)
        return name

    if len(parts) == 2:
        ref, component = parts
        # Handle absolute root paths like "/project" -> ["", "project"]
        if ref == "":
            return name
        # Must be "$N/component" format
        if not ref.startswith("$"):
            raise ManifestDecodeValidationError(
                f"Invalid path format '{name}': paths with '/' must use $N/ reference"
            )
        try:
            idx = int(ref[1:])
        except ValueError:
            raise ManifestDecodeValidationError(
                f"Invalid directory reference '{name}': expected $N/component format"
            )
        if idx not in dir_index:
            raise ManifestDecodeValidationError(
                f"Invalid directory reference '{name}': index {idx} not found"
            )
        return f"{dir_index[idx]}/{component}"

    # len(parts) >= 3 means invalid format
    raise ManifestDecodeValidationError(
        f"Invalid path format '{name}': expected at most one '/' in encoded path"
    )
