# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Encode unified manifest classes to v2023-03-03 JSON format.

This module provides the encode_v2023() function that serializes RelManifest
(either snapshot or diff) to canonical JSON. The v2023 format has limited
feature support compared to v2025, so some manifest features must be handled
via parameters or will raise errors in strict mode.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ..._snapshots import (
    ManifestFilePath,
    RelManifest,
    SymlinkPolicy,
    WHOLE_FILE_CHUNK_SIZE,
    subtree_manifest,
)
from ...exceptions import ManifestDecodeValidationError


# Manifest version string for v2023-03-03
MANIFEST_VERSION = "2023-03-03"


def encode_v2023(
    manifest: RelManifest,
    *,
    strict: bool = True,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ALL,
) -> str:
    """
    Encode a RelManifest to v2023-03-03 JSON format.

    The v2023 format only supports relative paths with basic file metadata.
    It does not support directories, symlinks, chunked files, deletion markers,
    or the runnable flag. This function handles these limitations based on
    the provided parameters.

    Args:
        manifest: RelManifest (either Snapshot or SnapshotDiff)
        strict: If True (default), raises an error if the manifest has features
            not supported by the format (directories, chunkhashes, runnable,
            deleted entries, or unhashed files). If False, these are silently
            skipped or ignored.
        symlink_policy: How to handle symlinks:
            - COLLAPSE_ALL (default): Collapse symlinks to their target content using
              subtree_manifest with symlink_policy=COLLAPSE.
            - EXCLUDE_ALL: Silently skip symlink entries.

    Returns:
        Canonical JSON string with manifestVersion "2023-03-03"

    Raises:
        ManifestDecodeValidationError: If manifest contains unsupported features
            and strict=True, or if symlink_policy is not COLLAPSE_ALL or EXCLUDE_ALL.
        AssertionError: If manifest.fileChunkSizeBytes is not WHOLE_FILE_CHUNK_SIZE.
    """
    # v2023 format doesn't support chunking - manifest must use whole-file hashing
    assert manifest.fileChunkSizeBytes == WHOLE_FILE_CHUNK_SIZE, (
        f"v2023 format requires fileChunkSizeBytes={WHOLE_FILE_CHUNK_SIZE} (no chunking), "
        f"got {manifest.fileChunkSizeBytes}"
    )

    # Validate symlink_policy
    if symlink_policy not in (SymlinkPolicy.COLLAPSE_ALL, SymlinkPolicy.EXCLUDE_ALL):
        raise ManifestDecodeValidationError(
            f"symlink_policy must be COLLAPSE_ALL or EXCLUDE_ALL for v2023 encoding, "
            f"got {symlink_policy.value}"
        )

    # Apply symlink_policy using subtree_manifest with "." (identity subtree)
    # This collapses or excludes symlinks as needed
    processed_manifest = subtree_manifest(manifest, ".", symlink_policy=symlink_policy)

    # Validate directories (v2023 doesn't support them)
    if processed_manifest.dirs and strict:
        raise ManifestDecodeValidationError(
            f"v2023 format does not support directories. "
            f"Found {len(processed_manifest.dirs)} directory entries. Use strict=False to ignore."
        )

    # Process files
    paths_json = _encode_files(
        processed_manifest.files,
        strict=strict,
    )

    # v2023 requires at least one path
    if not paths_json:
        raise ManifestDecodeValidationError(
            "v2023 format requires at least one file entry after filtering"
        )

    # Sort paths by UTF-16 BE encoding for canonical ordering
    paths_json.sort(key=lambda p: p["path"].encode("utf-16_be", errors="surrogatepass"))

    # Build manifest dictionary with canonical key order
    manifest_dict: Dict[str, Any] = {
        "hashAlg": processed_manifest.hashAlg.value,
        "manifestVersion": MANIFEST_VERSION,
        "paths": paths_json,
        "totalSize": processed_manifest.totalSize,
    }

    return json.dumps(manifest_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _encode_files(
    files: List[ManifestFilePath],
    *,
    strict: bool,
) -> List[Dict[str, Any]]:
    """
    Encode file entries to v2023 paths format.

    Args:
        files: List of ManifestFilePath entries (symlinks already processed)
        strict: If True, raise errors for unsupported features

    Returns:
        List of path dictionaries for v2023 format
    """
    result: List[Dict[str, Any]] = []

    for f in files:
        # Symlinks should have been collapsed/excluded by subtree_manifest
        # but handle any remaining ones as an error
        if f.symlink_target is not None:
            if strict:
                raise ManifestDecodeValidationError(
                    f"v2023 format does not support symlinks. "
                    f"File '{f.path}' is a symlink to '{f.symlink_target}'. "
                    f"This should have been handled by symlink_policy."
                )
            continue

        # Handle deleted entries (v2023 doesn't support diff manifests with deletions)
        if f.deleted:
            if strict:
                raise ManifestDecodeValidationError(
                    f"v2023 format does not support deletion markers. "
                    f"File '{f.path}' is marked as deleted. Use strict=False to skip."
                )
            continue

        # Handle chunked files (v2023 doesn't support chunkhashes)
        if f.chunkhashes is not None:
            if strict:
                raise ManifestDecodeValidationError(
                    f"v2023 format does not support chunked files. "
                    f"File '{f.path}' has {len(f.chunkhashes)} chunk hashes. "
                    f"Use strict=False to skip."
                )
            continue

        # Handle runnable flag (v2023 doesn't support it)
        if f.runnable and strict:
            raise ManifestDecodeValidationError(
                f"v2023 format does not support the runnable flag. "
                f"File '{f.path}' has runnable=True. Use strict=False to ignore."
            )

        # Handle unhashed files (v2023 requires hash)
        if f.hash is None:
            if strict:
                raise ManifestDecodeValidationError(
                    f"v2023 format requires all files to have a hash. "
                    f"File '{f.path}' has no hash. Use strict=False to skip."
                )
            continue

        # Validate required fields
        if f.size is None:
            if strict:
                raise ManifestDecodeValidationError(
                    f"v2023 format requires all files to have a size. File '{f.path}' has no size."
                )
            continue

        if f.mtime is None:
            if strict:
                raise ManifestDecodeValidationError(
                    f"v2023 format requires all files to have an mtime. "
                    f"File '{f.path}' has no mtime."
                )
            continue

        # Build path entry
        result.append(
            {
                "hash": f.hash,
                "mtime": f.mtime,
                "path": f.path,
                "size": f.size,
            }
        )

    return result
