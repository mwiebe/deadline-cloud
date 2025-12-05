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


def _normalize_path_in_manifest(path: str) -> str:
    """
    Normalize symlink target path to POSIX format.

    - Converts Windows backslashes to forward slashes
    - Collapses '..' and '.' components using posixpath.normpath
    """
    # Convert Windows path separators to POSIX
    if os.name == "nt":
        normalized = path.replace("\\", "/")

    # Normalize the path (collapse .., ., etc.)
    normalized = posixpath.normpath(normalized)

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


# 256MB chunk size for large files (256 * 2^20 bytes)
FILE_CHUNK_SIZE_BYTES = 256 * 1024 * 1024


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

            # Validate symlink_target
            if self.symlink_target is not None:
                self._validate_symlink_target()

    def _validate_symlink_target(self) -> None:
        """Validate that symlink_target is a valid relative path within the manifest."""
        target = self.symlink_target
        if target is None:
            return

        # Must be a relative path (not absolute)
        if posixpath.isabs(target):
            raise ManifestDecodeValidationError(
                f"Symlink '{self.path}' target must be a relative path, got absolute: '{target}'"
            )

        # Must not escape outside the manifest root (start with '..')
        # No need to check the rest of the path because
        # the path is normalized before validation.
        if target.startswith(".."):
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
        manifestType: Whether this is a snapshot or diff manifest (v2025_12_04+).
        dirs: List of directory entries (v2025_12_04+).
        parentManifestHash: Hash of parent snapshot for diff manifests (v2025_12_04+).
    """

    hashAlg: HashAlgorithm
    manifestVersion: ManifestVersion
    paths: list[BaseManifestPath]
    # v2025_12_04+ fields
    manifestType: ManifestType
    dirs: list[BaseManifestDirectoryPath]
    parentManifestHash: Optional[str]

    def __init__(
        self,
        *,
        hash_alg: HashAlgorithm,
        paths: Optional[list[BaseManifestPath]] = None,
        manifest_type: ManifestType = ManifestType.SNAPSHOT,
        dirs: Optional[list[BaseManifestDirectoryPath]] = None,
        parent_manifest_hash: Optional[str] = None,
    ):
        self.hashAlg = hash_alg
        self.manifestType = manifest_type
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
