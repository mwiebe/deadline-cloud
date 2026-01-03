# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Contains methods for decoding and validating Asset Manifests."""

from __future__ import annotations

import json
import re
from typing import Any, Optional, Tuple, Union

from ..exceptions import ManifestDecodeValidationError
from .base_manifest import BaseAssetManifest
from .manifest import Manifest
from .manifest_model import ManifestModelRegistry
from .versions import ManifestVersion
from .v2023_03_03.validate import validate_manifest_2023_03_03
from .v2025_12_04.decode import decode_v2025, SUPPORTED_SPEC_VERSIONS

alphanum_regex = re.compile("[a-zA-Z0-9]+")


def validate_manifest(
    manifest: dict[str, Any], version: ManifestVersion
) -> Tuple[bool, Optional[str]]:
    """
    Checks if the given manifest is valid for the given manifest version. Returns True if the manifest
    is valid for the given version. Returns False and a string explaining the error if the manifest is not valid.
    """
    if version == ManifestVersion.v2023_03_03:
        return validate_manifest_2023_03_03(manifest)
    else:
        return False, f"Version {version} is not supported"


def decode_manifest(manifest: str) -> Union[BaseAssetManifest, Manifest]:
    """
    Takes in a manifest string and returns an Asset Manifest object.
    A ManifestDecodeValidationError will be raised if the manifest version is unknown or
    the manifest is not valid.

    For v2025 manifests with specificationVersion, returns a unified Manifest class.
    For v2023 manifests with manifestVersion, returns a BaseAssetManifest subclass.
    """
    document: dict[str, Any] = json.loads(manifest)

    # Check for new v2025 format with specificationVersion
    if "specificationVersion" in document:
        decoded = decode_v2025(manifest)
        _validate_hashes(decoded)
        return decoded

    # Legacy format with manifestVersion
    try:
        version = ManifestVersion(document["manifestVersion"])
    except ValueError:
        # Value of the manifest version is not one we know.
        supported_versions = ", ".join(
            [v.value for v in ManifestVersion if v != ManifestVersion.UNDEFINED]
        )
        raise ManifestDecodeValidationError(
            f"Unknown manifest version: {document['manifestVersion']} "
            f"(Currently supported Manifest versions: {supported_versions})"
        )
    except KeyError:
        raise ManifestDecodeValidationError(
            'Manifest is missing the required "manifestVersion" or "specificationVersion" field'
        )

    manifest_valid, error_string = validate_manifest(document, version)

    if not manifest_valid:
        raise ManifestDecodeValidationError(error_string)

    manifest_model = ManifestModelRegistry.get_manifest_model(version=version)
    decoded_manifest = manifest_model.AssetManifest.decode(manifest_data=document)

    _validate_hashes(decoded_manifest)

    return decoded_manifest


def _validate_hashes(decoded_manifest: Union[BaseAssetManifest, Manifest]) -> None:
    """Validate that all hashes in the manifest are alphanumeric."""
    for path in decoded_manifest.paths:
        # Skip validation for entries without hash (symlinks, chunked files, deleted)
        if path.hash is not None and alphanum_regex.fullmatch(path.hash) is None:
            raise ManifestDecodeValidationError(
                f"The hash {path.hash} for path {path.path} is not alphanumeric"
            )
        # Also validate chunkhashes if present
        if path.chunkhashes is not None:
            for chunk_hash in path.chunkhashes:
                if alphanum_regex.fullmatch(chunk_hash) is None:
                    raise ManifestDecodeValidationError(
                        f"The chunk hash {chunk_hash} for path {path.path} is not alphanumeric"
                    )
