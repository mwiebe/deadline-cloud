# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Contains the base asset manifest and entities that are part of the Asset Manifest"""

from __future__ import annotations

import math
import os
import posixpath
from abc import ABC, abstractmethod
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Optional

from .hash_algorithms import HashAlgorithm
from .versions import ManifestType, ManifestVersion
from ..exceptions import ManifestDecodeValidationError

# Re-export unified manifest classes for operations that don't need serialization
from .manifest import (
    FILE_CHUNK_SIZE_BYTES,
    Manifest,
    ManifestFilePath,
    ManifestDirectoryPath,
    AbsSnapshotManifest,
    AbsDiffManifest,
    RelSnapshotManifest,
    RelDiffManifest,
    AbsManifestMixin,
    RelManifestMixin,
    SnapshotManifestMixin,
    DiffManifestMixin,
)

__all__ = [
    # Abstract base classes for version-specific serialization
    "BaseAssetManifest",
    "BaseManifestPath",
    "BaseManifestDirectoryPath",
    "FILE_CHUNK_SIZE_BYTES",
    # Unified manifest classes (re-exported from manifest.py)
    "Manifest",
    "ManifestFilePath",
    "ManifestDirectoryPath",
    "AbsSnapshotManifest",
    "AbsDiffManifest",
    "RelSnapshotManifest",
    "RelDiffManifest",
    "AbsManifestMixin",
    "RelManifestMixin",
    "SnapshotManifestMixin",
    "DiffManifestMixin",
]


def _normalize_path_in_manifest(path: str) -> str:
    """
    Normalize path in manifest to POSIX format.

    On Windows, backslashes are converted to forward slashes (they are directory separators).
    On POSIX, backslashes are preserved (they are valid filename characters).

    Also collapses '..' and '.' components using posixpath.normpath.
    """
    # Only convert Windows path separators to POSIX on Windows
    if os.name == "nt":
        path = path.replace("\\", "/")

    # Normalize the path (collapse .., ., etc.)
    normalized = posixpath.normpath(path)

    return normalized


@dataclass
class BaseManifestDirectoryPath(ABC):
    """
    Data class for directories in the Asset Manifest.
    Supports empty directories and directory deletion markers (for diff manifests).
    """

    path: str
    deleted: bool
    manifest_version: ClassVar[ManifestVersion]

    def __init__(self, *, path: str, deleted: bool = False) -> None:
        self.path = _normalize_path_in_manifest(path)
        self.deleted = deleted

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BaseManifestDirectoryPath):
            return NotImplemented
        return fields(self) == fields(other)


@dataclass
class BaseManifestPath(ABC):
    """
    Data class for paths in the Asset Manifest.

    Fields:
        path: Relative file path within the manifest.
        hash: File content hash (None for symlinks, chunked files, or deleted entries).
        size: File size in bytes (None for deleted entries).
        mtime: Modification time as Unix timestamp (None for deleted entries).
        runnable: POSIX execute bit (v2025_12_04+).
        chunkhashes: List of chunk hashes for files > 256MB (v2025_12_04+).
        symlink_target: Target path for symlinks (v2025_12_04+).
        deleted: Deletion marker for diff manifests (v2025_12_04+).

    Validation rules (v2025_12_04+):
        - If deleted is True, only path may be set; all other fields must be None/False.
        - Otherwise, exactly one of hash, chunkhashes, or symlink_target must be provided.
        - If chunkhashes is provided, size must be > 256MB and len(chunkhashes) must match
          ceil(size / CHUNK_SIZE).
    """

    path: str
    hash: Optional[str]
    size: Optional[int]
    mtime: Optional[int]
    runnable: bool
    chunkhashes: Optional[list[str]]
    symlink_target: Optional[str]
    deleted: bool
    manifest_version: ClassVar[ManifestVersion]

    def __init__(
        self,
        *,
        path: str,
        hash: Optional[str] = None,
        size: Optional[int] = None,
        mtime: Optional[int] = None,
        runnable: bool = False,
        chunkhashes: Optional[list[str]] = None,
        symlink_target: Optional[str] = None,
        deleted: bool = False,
    ) -> None:
        self.path = _normalize_path_in_manifest(path)
        self.hash = hash
        self.size = size
        self.mtime = mtime
        self.runnable = runnable
        self.chunkhashes = chunkhashes
        self.symlink_target = (
            _normalize_path_in_manifest(symlink_target) if symlink_target else None
        )
        self.deleted = deleted

        self._validate()

    def _validate(self) -> None:
        """Validate the manifest path entry according to format rules."""
        if self.deleted:
            # Deleted entries can only have path set
            if self.hash is not None:
                raise ManifestDecodeValidationError(
                    f"Deleted file '{self.path}' cannot have 'hash' field"
                )
            if self.chunkhashes is not None:
                raise ManifestDecodeValidationError(
                    f"Deleted file '{self.path}' cannot have 'chunkhashes' field"
                )
            if self.symlink_target is not None:
                raise ManifestDecodeValidationError(
                    f"Deleted file '{self.path}' cannot have 'symlink_target' field"
                )
            if self.runnable:
                raise ManifestDecodeValidationError(
                    f"Deleted file '{self.path}' cannot have 'runnable' set to True"
                )
            if self.size is not None:
                raise ManifestDecodeValidationError(
                    f"Deleted file '{self.path}' cannot have 'size' field"
                )
            if self.mtime is not None:
                raise ManifestDecodeValidationError(
                    f"Deleted file '{self.path}' cannot have 'mtime' field"
                )
        else:
            # Non-deleted entries must have exactly one of hash, chunkhashes, or symlink_target
            content_fields = [
                self.hash is not None,
                self.chunkhashes is not None,
                self.symlink_target is not None,
            ]
            if sum(content_fields) != 1:
                raise ManifestDecodeValidationError(
                    f"File '{self.path}' must have exactly one of 'hash', 'chunkhashes', "
                    f"or 'symlink_target'"
                )

            # Symlinks don't need size/mtime, but regular files do
            if self.symlink_target is None:
                if self.size is None:
                    raise ManifestDecodeValidationError(
                        f"File '{self.path}' must have 'size' field"
                    )
                if self.mtime is None:
                    raise ManifestDecodeValidationError(
                        f"File '{self.path}' must have 'mtime' field"
                    )

            # Validate chunkhashes relationship with size
            if self.chunkhashes is not None:
                if self.size is None:
                    raise ManifestDecodeValidationError(
                        f"File '{self.path}' with chunkhashes must have 'size' field"
                    )
                if self.size <= FILE_CHUNK_SIZE_BYTES:
                    raise ManifestDecodeValidationError(
                        f"File '{self.path}' with chunkhashes must have size > {FILE_CHUNK_SIZE_BYTES} "
                        f"(256MB), got {self.size}"
                    )
                expected_chunks = math.ceil(self.size / FILE_CHUNK_SIZE_BYTES)
                if len(self.chunkhashes) != expected_chunks:
                    raise ManifestDecodeValidationError(
                        f"File '{self.path}' with size {self.size} should have {expected_chunks} "
                        f"chunks, got {len(self.chunkhashes)}"
                    )

            # Note: symlink_target validation (relative path check) is deferred to
            # encode() time to allow intermediate in-memory manifests with absolute paths.

    def _validate_symlink_target(self) -> None:
        """Validate that symlink_target is a valid relative path within the manifest.

        Note: This validation is intentionally permissive about '..' in targets.
        A symlink in a subdirectory (e.g., 'subdir/link.txt') can legitimately
        point to '../target.txt' if the target is within the manifest root.
        Full validation with path context happens at collection time in
        _create_symlink_entry().
        """
        target = self.symlink_target
        if target is None:
            return

        # Must be a relative path (not absolute)
        # Check both POSIX and Windows absolute path formats
        if posixpath.isabs(target):
            raise ManifestDecodeValidationError(
                f"Symlink '{self.path}' target must be a relative path, got absolute: '{target}'"
            )

        # Check Windows-style absolute paths (drive letter or UNC)
        if len(target) >= 2 and target[1] == ":" and target[0].isalpha():
            raise ManifestDecodeValidationError(
                f"Symlink '{self.path}' target must be a relative path, got absolute: '{target}'"
            )
        if target.startswith("\\\\") or target.startswith("//"):
            raise ManifestDecodeValidationError(
                f"Symlink '{self.path}' target must be a relative path, got absolute: '{target}'"
            )

        # Check if target would escape from root when symlink is at root level
        # A symlink at root level (no '/' in path) cannot have target starting with '..'
        if "/" not in self.path and target.startswith(".."):
            raise ManifestDecodeValidationError(
                f"Symlink '{self.path}' target escapes manifest root: '{target}'"
            )

    def __eq__(self, other: object) -> bool:
        """
        By default dataclasses still check ClassVars for equality.
        We only want to compare fields.
        :param other:
        :return: True if all fields are equal, False otherwise.
        """
        if not isinstance(other, BaseManifestPath):
            return NotImplemented
        return fields(self) == fields(other)


@dataclass
class BaseAssetManifest(ABC):
    """
    Base class for the Asset Manifest.

    Supports both snapshot manifests (full directory tree) and diff manifests
    (changes relative to a parent snapshot).

    Fields:
        hashAlg: Hashing algorithm used for file content hashes.
        manifestVersion: Version of the manifest format.
        paths: List of file entries.
        totalSize: Total size of all files in the manifest (in bytes).
        manifestType: Whether this is a snapshot or diff manifest (v2025_12_04+).
        dirs: List of directory entries (v2025_12_04+).
        parentManifestHash: Hash of parent snapshot for diff manifests (v2025_12_04+).
    """

    hashAlg: HashAlgorithm
    manifestVersion: ManifestVersion
    paths: list
    totalSize: int
    # v2025_12_04+ fields
    manifestType: ManifestType
    dirs: list
    parentManifestHash: Optional[str]

    def __init__(
        self,
        *,
        hash_alg: HashAlgorithm,
        paths: list,
        total_size: int = 0,
        manifest_type: ManifestType = ManifestType.SNAPSHOT,
        dirs: Optional[list] = None,
        parent_manifest_hash: Optional[str] = None,
    ):
        self.hashAlg = hash_alg
        self.manifestType = manifest_type
        self.totalSize = total_size
        self.dirs = dirs if dirs is not None else []
        self.paths = paths if paths is not None else []
        self.parentManifestHash = parent_manifest_hash

    @classmethod
    @abstractmethod
    def get_default_hash_alg(cls) -> HashAlgorithm:  # pragma: no cover
        """Returns the default hashing algorithm for the Asset Manifest"""
        raise NotImplementedError(
            "Asset Manifest base class does not implement get_default_hash_alg"
        )

    @classmethod
    @abstractmethod
    def decode(cls, *, manifest_data: dict[str, Any]) -> BaseAssetManifest:  # pragma: no cover
        """Turn a dictionary for a manifest into an AssetManifest object"""
        raise NotImplementedError("Asset Manifest base class does not implement decode")

    @abstractmethod
    def encode(self) -> str:  # pragma: no cover
        """
        Recursively encode the Asset Manifest into a string according to
        whatever format the Asset Manifest was written for.
        """
        raise NotImplementedError("Asset Manifest base class does not implement encode")
