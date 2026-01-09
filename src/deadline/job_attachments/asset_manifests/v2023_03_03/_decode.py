# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Decode v2023-03-03 JSON format to unified manifest classes.

This module provides the decode_v2023() function that deserializes JSON
to Snapshot. The v2023 format only supports relative paths
and snapshot manifests, so this always returns Snapshot.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ..hash_algorithms import HashAlgorithm
from ..._snapshots import (
    ManifestFilePath,
    Snapshot,
    WHOLE_FILE_CHUNK_SIZE,
)
from ...exceptions import ManifestDecodeValidationError
from .validate import validate_manifest_2023_03_03


def decode_v2023(manifest_str: str) -> Snapshot:
    """
    Decode a v2023-03-03 JSON string to a unified Snapshot.

    The v2023 format only supports relative paths and snapshot manifests,
    so this always returns Snapshot.

    Args:
        manifest_str: JSON string with manifestVersion "2023-03-03"

    Returns:
        Snapshot with the decoded manifest data

    Raises:
        ManifestDecodeValidationError: If JSON is invalid or manifestVersion is wrong
    """
    try:
        data: Dict[str, Any] = json.loads(manifest_str)
    except json.JSONDecodeError as e:
        raise ManifestDecodeValidationError(f"Invalid JSON: {e}")

    # Validate structure
    is_valid, error = validate_manifest_2023_03_03(data)
    if not is_valid:
        raise ManifestDecodeValidationError(error)

    # Parse hash algorithm
    try:
        hash_alg = HashAlgorithm(data["hashAlg"])
    except ValueError:
        raise ManifestDecodeValidationError(f"Unsupported hash algorithm: {data['hashAlg']}")

    # Decode files from paths array
    files = _decode_paths(data["paths"])

    # Get total size
    total_size = data["totalSize"]

    # Create manifest instance
    # v2023 doesn't support chunking, so use WHOLE_FILE_CHUNK_SIZE
    return Snapshot(
        hash_alg=hash_alg,
        files=files,
        total_size=total_size,
        dirs=None,
        parent_manifest_hash=None,
        file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
    )


def _decode_paths(raw_paths: List[Dict[str, Any]]) -> List[ManifestFilePath]:
    """Decode path entries from v2023 format."""
    result: List[ManifestFilePath] = []

    for p in raw_paths:
        result.append(
            ManifestFilePath(
                path=p["path"],
                hash=p["hash"],
                size=p["size"],
                mtime=p["mtime"],
                # v2023 doesn't support these features
                runnable=False,
                chunkhashes=None,
                symlink_target=None,
                deleted=False,
            )
        )

    return result
