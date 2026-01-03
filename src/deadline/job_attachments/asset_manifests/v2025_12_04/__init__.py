# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
EXPERIMENTAL: Asset manifest format v2025-12.

This module provides encode/decode functions for the v2025-12 manifest format,
which uses a specificationVersion field to identify the manifest type.

This format is under development and subject to change. Do not use in production.
"""

from .asset_manifest import AssetManifest, ManifestDirectoryPath, ManifestFilePath, ManifestModel
from .decode import decode_v2025
from .encode import encode_v2025

__all__ = [
    # New standalone functions
    "encode_v2025",
    "decode_v2025",
    # Legacy classes (for backwards compatibility)
    "ManifestModel",
    "ManifestDirectoryPath",
    "ManifestFilePath",
    "AssetManifest",
]
