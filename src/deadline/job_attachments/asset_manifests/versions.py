# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Module that defines the asset manifest versions."""

from enum import Enum


class ManifestVersion(str, Enum):
    """
    Enumerant of all Asset Manifest versions supported by this library.

    Special values:
      UNDEFINED -- Purely for internal testing.

    Versions:
      v2023_03_03 - First version.
      v2025_12_04_beta - EXPERIMENTAL: Second version with directory compression,
                         chunked files, diff manifests, empty directories, symlinks,
                         and execute bit. Format is subject to change.
    """

    UNDEFINED = "UNDEFINED"
    v2023_03_03 = "2023-03-03"
    v2025_12_04_beta = "2025-12-04-beta"


class ManifestType(str, Enum):
    """
    Enumerant of manifest types.

    Types:
      SNAPSHOT - A full directory tree representation.
      DIFF - A set of changes relative to a parent snapshot manifest.
    """

    SNAPSHOT = "snapshot"
    DIFF = "diff"


class ManifestContentType(str, Enum):
    """
    Content-Type values for manifest storage in S3.
    """

    SNAPSHOT_2025_12_04_BETA = "application/x-deadline-manifest-2025-12-04-beta"
    DIFF_2025_12_04_BETA = "application/x-deadline-manifest-diff-2025-12-04-beta"
