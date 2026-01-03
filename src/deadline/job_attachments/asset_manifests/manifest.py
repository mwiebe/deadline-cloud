# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Unified manifest classes for job attachments.

This module provides the canonical in-memory representation for manifests,
independent of on-disk format versions. The classes here support all features
(directories, symlinks, chunked files, diff manifests) and use mixins for
validation of path style (absolute/relative) and manifest type (snapshot/diff).

On-disk serialization is handled by version-specific modules (v2023_03_03, v2025_12_04).
"""

from __future__ import annotations

import math
import os
import posixpath
from dataclasses import dataclass, fields
from typing import List, Optional, Union

from .hash_algorithms import HashAlgorithm
from .versions import ManifestType
from ..exceptions import ManifestDecodeValidationError


def _normalize_path_in_manifest(path: str) -> str:
    """
    Normalize path in manifest to POSIX format.

    On Windows, backslashes are converted to forward slashes (they are directory separators).
    On POSIX, backslashes are preserved (they are valid filename characters).

    Also collapses '..' and '.' components using posixpath.normpath.
    """
    if os.name == "nt":
        path = path.replace("\\", "/")
    normalized = posixpath.normpath(path)
    return normalized


def _is_absolute_path(path: str) -> bool:
    """Check if a path string represents an absolute path."""
    return (
        path.startswith("/")
        or (len(path) >= 3 and path[1] == ":" and path[2] == "/")
        or path.startswith("//")
    )


# 256MB chunk size for large files (256 * 2^20 bytes)
FILE_CHUNK_SIZE_BYTES = 256 * 1024 * 1024


# =============================================================================
# Path Classes
# =============================================================================


@dataclass
class ManifestDirectoryPath:
    """
    Directory entry in a manifest.

    Supports empty directories and directory deletion markers (for diff manifests).
    """

    path: str
    deleted: bool

    def __init__(self, *, path: str, deleted: bool = False) -> None:
        self.path = _normalize_path_in_manifest(path)
        self.deleted = deleted

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ManifestDirectoryPath):
            return NotImplemented
        return self.path == other.path and self.deleted == other.deleted


@dataclass
class ManifestFilePath:
    """
    File entry in a manifest.

    Supports regular files, large chunked files, symlinks, and deletion markers.

    Fields:
        path: File path within the manifest.
        hash: File content hash (None for symlinks, chunked files, or deleted entries).
        size: File size in bytes (None for deleted entries or symlinks).
        mtime: Modification time in microseconds (None for deleted entries or symlinks).
        runnable: POSIX execute bit.
        chunkhashes: List of chunk hashes for files > 256MB.
        symlink_target: Target path for symlinks.
        deleted: Deletion marker for diff manifests.

    Validation rules:
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
    chunkhashes: Optional[List[str]]
    symlink_target: Optional[str]
    deleted: bool

    def __init__(
        self,
        *,
        path: str,
        hash: Optional[str] = None,
        size: Optional[int] = None,
        mtime: Optional[int] = None,
        runnable: bool = False,
        chunkhashes: Optional[List[str]] = None,
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
            self._validate_deleted_entry()
        else:
            self._validate_non_deleted_entry()

    def _validate_deleted_entry(self) -> None:
        """Validate a deleted entry - only path may be set."""
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

    def _validate_non_deleted_entry(self) -> None:
        """Validate a non-deleted entry."""
        # Must have exactly one of hash, chunkhashes, or symlink_target
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
                raise ManifestDecodeValidationError(f"File '{self.path}' must have 'size' field")
            if self.mtime is None:
                raise ManifestDecodeValidationError(f"File '{self.path}' must have 'mtime' field")

        # Validate chunkhashes relationship with size
        if self.chunkhashes is not None:
            self._validate_chunkhashes()

    def _validate_chunkhashes(self) -> None:
        """Validate chunkhashes field constraints."""
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
        if self.chunkhashes is not None and len(self.chunkhashes) != expected_chunks:
            raise ManifestDecodeValidationError(
                f"File '{self.path}' with size {self.size} should have {expected_chunks} "
                f"chunks, got {len(self.chunkhashes)}"
            )

    def _validate_symlink_target_relative(self) -> None:
        """
        Validate that symlink_target is a valid relative path.

        This is called during encode() for relative-path manifests.
        """
        target = self.symlink_target
        if target is None:
            return

        if posixpath.isabs(target):
            raise ManifestDecodeValidationError(
                f"Symlink '{self.path}' target must be a relative path, got absolute: '{target}'"
            )

        # Check Windows-style absolute paths
        if len(target) >= 2 and target[1] == ":" and target[0].isalpha():
            raise ManifestDecodeValidationError(
                f"Symlink '{self.path}' target must be a relative path, got absolute: '{target}'"
            )
        if target.startswith("\\\\") or target.startswith("//"):
            raise ManifestDecodeValidationError(
                f"Symlink '{self.path}' target must be a relative path, got absolute: '{target}'"
            )

        # Check if target would escape from root when symlink is at root level
        if "/" not in self.path and target.startswith(".."):
            raise ManifestDecodeValidationError(
                f"Symlink '{self.path}' target escapes manifest root: '{target}'"
            )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ManifestFilePath):
            return NotImplemented
        return fields(self) == fields(other)


# =============================================================================
# Validation Mixins
# =============================================================================


class AbsManifestMixin:
    """Mixin that validates all paths in the manifest are absolute."""

    def _validate_absolute_paths(self) -> None:
        """Validate that all paths in the manifest are absolute."""
        manifest: Manifest = self  # type: ignore[assignment]

        for entry in manifest.paths:
            if not _is_absolute_path(entry.path):
                raise ManifestDecodeValidationError(
                    f"AbsManifest requires absolute paths. Found relative path: '{entry.path}'"
                )
            if entry.symlink_target is not None and not _is_absolute_path(entry.symlink_target):
                raise ManifestDecodeValidationError(
                    f"AbsManifest requires absolute symlink targets. "
                    f"Found relative target: '{entry.symlink_target}' for '{entry.path}'"
                )

        for d in manifest.dirs:
            if not _is_absolute_path(d.path):
                raise ManifestDecodeValidationError(
                    f"AbsManifest requires absolute paths. Found relative directory: '{d.path}'"
                )


class RelManifestMixin:
    """Mixin that validates all paths in the manifest are relative."""

    def _validate_relative_paths(self) -> None:
        """Validate that all paths in the manifest are relative."""
        manifest: Manifest = self  # type: ignore[assignment]

        for entry in manifest.paths:
            if _is_absolute_path(entry.path):
                raise ManifestDecodeValidationError(
                    f"RelManifest requires relative paths. Found absolute path: '{entry.path}'"
                )
            if entry.symlink_target is not None and _is_absolute_path(entry.symlink_target):
                raise ManifestDecodeValidationError(
                    f"RelManifest requires relative symlink targets. "
                    f"Found absolute target: '{entry.symlink_target}' for '{entry.path}'"
                )

        for d in manifest.dirs:
            if _is_absolute_path(d.path):
                raise ManifestDecodeValidationError(
                    f"RelManifest requires relative paths. Found absolute directory: '{d.path}'"
                )


class SnapshotManifestMixin:
    """Mixin that validates the manifest is a valid snapshot."""

    def _validate_snapshot(self) -> None:
        """Validate snapshot-specific constraints."""
        manifest: Manifest = self  # type: ignore[assignment]

        if manifest.manifestType != ManifestType.SNAPSHOT:
            raise ManifestDecodeValidationError(
                f"SnapshotManifest must have manifestType=SNAPSHOT, got {manifest.manifestType}"
            )

        # Snapshots should not have deleted entries
        for entry in manifest.paths:
            if entry.deleted:
                raise ManifestDecodeValidationError(
                    f"Snapshot manifest cannot have deleted entries. Found: '{entry.path}'"
                )

        for d in manifest.dirs:
            if d.deleted:
                raise ManifestDecodeValidationError(
                    f"Snapshot manifest cannot have deleted directories. Found: '{d.path}'"
                )


class DiffManifestMixin:
    """Mixin that validates the manifest is a valid diff."""

    def _validate_diff(self) -> None:
        """Validate diff-specific constraints."""
        manifest: Manifest = self  # type: ignore[assignment]

        if manifest.manifestType != ManifestType.DIFF:
            raise ManifestDecodeValidationError(
                f"DiffManifest must have manifestType=DIFF, got {manifest.manifestType}"
            )


# =============================================================================
# Manifest Base Class
# =============================================================================


@dataclass
class Manifest:
    """
    Base class for the unified in-memory manifest representation.

    This class holds all manifest data and delegates validation to mixins
    in concrete subclasses.

    Fields:
        hashAlg: Hashing algorithm used for file content hashes.
        paths: List of file entries.
        totalSize: Total size of all files in the manifest (in bytes).
        manifestType: Whether this is a snapshot or diff manifest.
        dirs: List of directory entries.
        parentManifestHash: Hash of parent snapshot for diff manifests.
    """

    hashAlg: HashAlgorithm
    paths: List[ManifestFilePath]
    totalSize: int
    manifestType: ManifestType
    dirs: List[ManifestDirectoryPath]
    parentManifestHash: Optional[str]

    def __init__(
        self,
        *,
        hash_alg: HashAlgorithm,
        paths: List[ManifestFilePath],
        total_size: int = 0,
        manifest_type: ManifestType = ManifestType.SNAPSHOT,
        dirs: Optional[List[ManifestDirectoryPath]] = None,
        parent_manifest_hash: Optional[str] = None,
    ) -> None:
        self.hashAlg = hash_alg
        self.manifestType = manifest_type
        self.totalSize = total_size
        self.dirs = dirs if dirs is not None else []
        self.paths = paths if paths is not None else []
        self.parentManifestHash = parent_manifest_hash

    def validate(self) -> None:
        """
        Run all validations from mixins.

        Subclasses should override this to call their mixin validation methods.
        """
        pass

    @classmethod
    def get_default_hash_alg(cls) -> HashAlgorithm:
        """Returns the default hashing algorithm for manifests."""
        return HashAlgorithm.XXH128


# =============================================================================
# Concrete Manifest Classes
# =============================================================================


class AbsSnapshotManifest(Manifest, AbsManifestMixin, SnapshotManifestMixin):
    """Manifest with absolute paths representing a full directory snapshot."""

    def __init__(
        self,
        *,
        hash_alg: HashAlgorithm,
        paths: List[ManifestFilePath],
        total_size: int = 0,
        dirs: Optional[List[ManifestDirectoryPath]] = None,
        parent_manifest_hash: Optional[str] = None,
    ) -> None:
        super().__init__(
            hash_alg=hash_alg,
            paths=paths,
            total_size=total_size,
            manifest_type=ManifestType.SNAPSHOT,
            dirs=dirs,
            parent_manifest_hash=parent_manifest_hash,
        )

    def validate(self) -> None:
        """Validate absolute paths and snapshot constraints."""
        self._validate_absolute_paths()
        self._validate_snapshot()


class AbsDiffManifest(Manifest, AbsManifestMixin, DiffManifestMixin):
    """Manifest with absolute paths representing changes relative to a parent."""

    def __init__(
        self,
        *,
        hash_alg: HashAlgorithm,
        paths: List[ManifestFilePath],
        total_size: int = 0,
        dirs: Optional[List[ManifestDirectoryPath]] = None,
        parent_manifest_hash: Optional[str] = None,
    ) -> None:
        super().__init__(
            hash_alg=hash_alg,
            paths=paths,
            total_size=total_size,
            manifest_type=ManifestType.DIFF,
            dirs=dirs,
            parent_manifest_hash=parent_manifest_hash,
        )

    def validate(self) -> None:
        """Validate absolute paths and diff constraints."""
        self._validate_absolute_paths()
        self._validate_diff()


class RelSnapshotManifest(Manifest, RelManifestMixin, SnapshotManifestMixin):
    """Manifest with relative paths representing a full directory snapshot."""

    def __init__(
        self,
        *,
        hash_alg: HashAlgorithm,
        paths: List[ManifestFilePath],
        total_size: int = 0,
        dirs: Optional[List[ManifestDirectoryPath]] = None,
        parent_manifest_hash: Optional[str] = None,
    ) -> None:
        super().__init__(
            hash_alg=hash_alg,
            paths=paths,
            total_size=total_size,
            manifest_type=ManifestType.SNAPSHOT,
            dirs=dirs,
            parent_manifest_hash=parent_manifest_hash,
        )

    def validate(self) -> None:
        """Validate relative paths and snapshot constraints."""
        self._validate_relative_paths()
        self._validate_snapshot()


class RelDiffManifest(Manifest, RelManifestMixin, DiffManifestMixin):
    """Manifest with relative paths representing changes relative to a parent."""

    def __init__(
        self,
        *,
        hash_alg: HashAlgorithm,
        paths: List[ManifestFilePath],
        total_size: int = 0,
        dirs: Optional[List[ManifestDirectoryPath]] = None,
        parent_manifest_hash: Optional[str] = None,
    ) -> None:
        super().__init__(
            hash_alg=hash_alg,
            paths=paths,
            total_size=total_size,
            manifest_type=ManifestType.DIFF,
            dirs=dirs,
            parent_manifest_hash=parent_manifest_hash,
        )

    def validate(self) -> None:
        """Validate relative paths and diff constraints."""
        self._validate_relative_paths()
        self._validate_diff()

# =============================================================================
# Type Aliases
# =============================================================================

# Type aliases for clearer function signatures
SnapshotManifest = Union[AbsSnapshotManifest, RelSnapshotManifest]
DiffManifest = Union[AbsDiffManifest, RelDiffManifest]
