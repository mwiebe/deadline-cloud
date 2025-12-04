# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Contains the base asset manifest and entities that are part of the Asset Manifest"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Optional

from .hash_algorithms import HashAlgorithm
from .versions import ManifestType, ManifestVersion


@dataclass
class BaseManifestDirectory(ABC):
    """
    Data class for directories in the Asset Manifest.
    Supports empty directories and directory deletion markers (for diff manifests).
    """

    path: str
    deleted: bool
    manifest_version: ClassVar[ManifestVersion]

    def __init__(self, *, path: str, deleted: bool = False) -> None:
        self.path = path
        self.deleted = deleted

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BaseManifestDirectory):
            return NotImplemented
        return fields(self) == fields(other)


@dataclass
class BaseManifestPath(ABC):
    """
    Data class for paths in the Asset Manifest.

    Fields:
        path: Relative file path within the manifest.
        hash: File content hash (None for symlinks or chunked files).
        size: File size in bytes.
        mtime: Modification time as Unix timestamp.
        runnable: POSIX execute bit (v2025_12_04+).
        chunkhashes: List of chunk hashes for files > 256MB (v2025_12_04+).
        symlink_target: Target path for symlinks (v2025_12_04+).
        deleted: Deletion marker for diff manifests (v2025_12_04+).
    """

    path: str
    hash: Optional[str]
    size: int
    mtime: int
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
        size: int,
        mtime: int,
        runnable: bool = False,
        chunkhashes: Optional[list[str]] = None,
        symlink_target: Optional[str] = None,
        deleted: bool = False,
    ) -> None:
        self.path = path
        self.hash = hash
        self.size = size
        self.mtime = mtime
        self.runnable = runnable
        self.chunkhashes = chunkhashes
        self.symlink_target = symlink_target
        self.deleted = deleted

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
        manifestType: Whether this is a snapshot or diff manifest (v2025_12_04+).
        paths: List of file entries (v2023_03_03 compatibility).
        dirs: List of directory entries (v2025_12_04+).
        files: List of file entries (v2025_12_04+, replaces paths).
        parentManifestHash: Hash of parent snapshot for diff manifests (v2025_12_04+).
    """

    hashAlg: HashAlgorithm
    manifestVersion: ManifestVersion
    # v2023_03_03 compatibility
    paths: list[BaseManifestPath]
    # v2025_12_04+ fields
    manifestType: ManifestType
    dirs: list[BaseManifestDirectory]
    files: list[BaseManifestPath]
    parentManifestHash: Optional[str]

    def __init__(
        self,
        *,
        hash_alg: HashAlgorithm,
        paths: Optional[list[BaseManifestPath]] = None,
        manifest_type: ManifestType = ManifestType.SNAPSHOT,
        dirs: Optional[list[BaseManifestDirectory]] = None,
        files: Optional[list[BaseManifestPath]] = None,
        parent_manifest_hash: Optional[str] = None,
    ):
        self.hashAlg = hash_alg
        self.manifestType = manifest_type
        self.dirs = dirs if dirs is not None else []
        self.files = files if files is not None else []
        self.parentManifestHash = parent_manifest_hash

        # v2023_03_03 compatibility: use paths if provided, otherwise use files
        self.paths = paths if paths is not None else self.files

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
