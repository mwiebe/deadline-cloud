# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
EXPERIMENTAL: Contains functions validating Asset Manifests for v2025-12 format.

Supports both the legacy format (manifestVersion + manifestType) and
the new format (specificationVersion).

This format is under development and subject to change. Do not use in production.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

# New specificationVersion values
_SPEC_VERSIONS_2025_12: set[str] = {
    "absolute-manifest-snapshot-2025-12",
    "absolute-manifest-diff-2025-12",
    "relative-manifest-snapshot-2025-12",
    "relative-manifest-diff-2025-12",
}

_DIFF_SPEC_VERSIONS: set[str] = {
    "absolute-manifest-diff-2025-12",
    "relative-manifest-diff-2025-12",
}

# Required fields for new format
_REQUIRED_FIELDS_2025_12: list[str] = [
    "hashAlg",
    "dirs",
    "files",
    "specificationVersion",
    "totalSize",
]

# Required fields for legacy format
_REQUIRED_FIELDS_2025_12_04_LEGACY: list[str] = [
    "hashAlg",
    "dirs",
    "files",
    "manifestVersion",
    "totalSize",
]

_HASH_ALGS_2025_12: set[str] = {"xxh128"}

_DIR_REQUIRED_FIELDS: list[str] = ["name"]

_FILE_REQUIRED_FIELDS: list[str] = ["name"]


def _get_missing_fields(obj: dict[str, Any], required: list[str]) -> list[str]:
    missing = []
    for field in required:
        if field not in obj:
            missing.append(field)
    return missing


def _validate_dir(dir_object: dict[str, Any], is_diff: bool) -> Tuple[bool, Optional[str]]:
    missing = _get_missing_fields(dir_object, _DIR_REQUIRED_FIELDS)
    if len(missing) > 0:
        return False, f"directory is missing required field(s) {missing}"

    name = dir_object["name"]
    if not isinstance(name, str):
        return False, "directory name must be a string"

    is_deleted = dir_object.get("delete", False)
    if is_deleted and not is_diff:
        return False, "snapshot manifests cannot have directory deletions"

    return True, None


def _validate_file(file_object: dict[str, Any], is_diff: bool) -> Tuple[bool, Optional[str]]:
    missing = _get_missing_fields(file_object, _FILE_REQUIRED_FIELDS)
    if len(missing) > 0:
        return False, f"file is missing required field(s) {missing}"

    name = file_object["name"]
    if not isinstance(name, str):
        return False, "file name must be a string"

    is_deleted = file_object.get("delete", False)
    is_symlink = "symlink" in file_object
    has_hash = "hash" in file_object
    has_chunkhashes = "chunkhashes" in file_object

    # Deletion validation
    if is_deleted and not is_diff:
        return False, "snapshot manifests cannot have file deletions"

    # Hash validation (not required for deleted files or symlinks)
    if not is_deleted and not is_symlink:
        if not has_hash and not has_chunkhashes:
            return False, f"file '{name}' must have hash or chunkhashes"
        if has_hash and has_chunkhashes:
            return False, f"file '{name}' cannot have both hash and chunkhashes"

    # Type validation for optional fields
    if has_hash and not isinstance(file_object["hash"], str):
        return False, "hash must be a string"

    if has_chunkhashes:
        chunkhashes = file_object["chunkhashes"]
        if not isinstance(chunkhashes, list):
            return False, "chunkhashes must be a list"
        if not all(isinstance(h, str) for h in chunkhashes):
            return False, "all chunkhashes must be strings"

    if "size" in file_object and not isinstance(file_object["size"], int):
        return False, "size must be an integer"

    if "mtime" in file_object and not isinstance(file_object["mtime"], int):
        return False, "mtime must be an integer"

    if "runnable" in file_object:
        if file_object["runnable"] is not True:
            return False, "runnable field must be true or omitted"

    if is_symlink:
        symlink = file_object["symlink"]
        if not isinstance(symlink, dict) or "name" not in symlink:
            return False, "symlink must be an object with 'name' field"

    return True, None


def validate_manifest_2025_12(manifest: dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Validate a v2025-12 manifest with specificationVersion field.

    Args:
        manifest: Parsed JSON manifest dictionary

    Returns:
        Tuple of (is_valid, error_message)
    """
    missing = _get_missing_fields(manifest, _REQUIRED_FIELDS_2025_12)
    if len(missing) > 0:
        return False, f"manifest is missing required field(s) {missing}"

    spec_version = manifest["specificationVersion"]
    if not isinstance(spec_version, str) or spec_version not in _SPEC_VERSIONS_2025_12:
        return False, (
            f"specificationVersion must be one of {sorted(_SPEC_VERSIONS_2025_12)}, "
            f"got '{spec_version}'"
        )

    hash_alg = manifest["hashAlg"]
    if not isinstance(hash_alg, str) or hash_alg not in _HASH_ALGS_2025_12:
        return False, f"hashAlg must be one of {_HASH_ALGS_2025_12}"

    total_size = manifest["totalSize"]
    if not isinstance(total_size, int):
        return False, "totalSize must be an integer"

    # Determine if this is a diff manifest from specificationVersion
    is_diff = spec_version in _DIFF_SPEC_VERSIONS

    # Validate parentManifestHash
    if "parentManifestHash" in manifest:
        parent_hash = manifest["parentManifestHash"]
        if not isinstance(parent_hash, str):
            return False, "parentManifestHash must be a string"

    # Validate dirs
    dirs = manifest["dirs"]
    if not isinstance(dirs, list):
        return False, "dirs must be a list"
    for dir_object in dirs:
        ok, message = _validate_dir(dir_object, is_diff)
        if not ok:
            return False, message

    # Validate files
    files = manifest["files"]
    if not isinstance(files, list):
        return False, "files must be a list"
    for file_object in files:
        ok, message = _validate_file(file_object, is_diff)
        if not ok:
            return False, message

    return True, None


def validate_manifest_2025_12_04(manifest: dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Validate a legacy v2025-12-04-beta manifest with manifestVersion field.

    This function is kept for backwards compatibility with the old format.
    """
    missing = _get_missing_fields(manifest, _REQUIRED_FIELDS_2025_12_04_LEGACY)
    if len(missing) > 0:
        return False, f"manifest is missing required field(s) {missing}"

    manifest_version = manifest["manifestVersion"]
    if not isinstance(manifest_version, str) or manifest_version != "2025-12-04-beta":
        return False, 'manifestVersion must be "2025-12-04-beta"'

    hash_alg = manifest["hashAlg"]
    if not isinstance(hash_alg, str) or hash_alg not in _HASH_ALGS_2025_12:
        return False, f"hashAlg must be one of {_HASH_ALGS_2025_12}"

    total_size = manifest["totalSize"]
    if not isinstance(total_size, int):
        return False, "totalSize must be an integer"

    # Determine if this is a diff manifest
    is_diff = "parentManifestHash" in manifest
    if is_diff:
        parent_hash = manifest["parentManifestHash"]
        if not isinstance(parent_hash, str):
            return False, "parentManifestHash must be a string"

    # Validate dirs
    dirs = manifest["dirs"]
    if not isinstance(dirs, list):
        return False, "dirs must be a list"
    for dir_object in dirs:
        ok, message = _validate_dir(dir_object, is_diff)
        if not ok:
            return False, message

    # Validate files
    files = manifest["files"]
    if not isinstance(files, list):
        return False, "files must be a list"
    for file_object in files:
        ok, message = _validate_file(file_object, is_diff)
        if not ok:
            return False, message

    return True, None
