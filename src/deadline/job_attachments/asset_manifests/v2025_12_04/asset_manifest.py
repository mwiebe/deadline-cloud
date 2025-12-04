# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Module that defines the v2025-12-04 version of the asset manifest"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Type

import math

from ..base_manifest import (
    CHUNK_SIZE_BYTES,
    BaseAssetManifest,
    BaseManifestDirectoryPath,
    BaseManifestPath,
)
from ..hash_algorithms import HashAlgorithm
from ..manifest_model import BaseManifestModel
from ..versions import ManifestType, ManifestVersion
from ...exceptions import ManifestDecodeValidationError


SUPPORTED_HASH_ALGS: set[HashAlgorithm] = {HashAlgorithm.XXH128}
DEFAULT_HASH_ALG: HashAlgorithm = HashAlgorithm.XXH128


@dataclass
class ManifestDirectoryPath(BaseManifestDirectoryPath):
    """
    Directory entry for version v2025-12-04 of the asset manifest.
    """

    manifest_version = ManifestVersion.v2025_12_04

    def __init__(self, *, path: str, deleted: bool = False) -> None:
        super().__init__(path=path, deleted=deleted)


@dataclass
class ManifestFilePath(BaseManifestPath):
    """
    File entry for version v2025-12-04 of the asset manifest.

    Validation rules:
        - If deleted is True, only path may be set; all other fields must be None/False.
        - Otherwise, exactly one of hash, chunkhashes, or symlink_target must be provided.
        - If chunkhashes is provided, size must be > 256MB and len(chunkhashes) must match
          ceil(size / CHUNK_SIZE).
    """

    manifest_version = ManifestVersion.v2025_12_04

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
        # Validate based on deleted status
        if deleted:
            # Deleted entries can only have path set
            if hash is not None:
                raise ManifestDecodeValidationError(
                    f"Deleted file '{path}' cannot have 'hash' field"
                )
            if chunkhashes is not None:
                raise ManifestDecodeValidationError(
                    f"Deleted file '{path}' cannot have 'chunkhashes' field"
                )
            if symlink_target is not None:
                raise ManifestDecodeValidationError(
                    f"Deleted file '{path}' cannot have 'symlink_target' field"
                )
            if runnable:
                raise ManifestDecodeValidationError(
                    f"Deleted file '{path}' cannot have 'runnable' set to True"
                )
            if size is not None:
                raise ManifestDecodeValidationError(
                    f"Deleted file '{path}' cannot have 'size' field"
                )
            if mtime is not None:
                raise ManifestDecodeValidationError(
                    f"Deleted file '{path}' cannot have 'mtime' field"
                )
        else:
            # Non-deleted entries must have exactly one of hash, chunkhashes, or symlink_target
            content_fields = [
                hash is not None,
                chunkhashes is not None,
                symlink_target is not None,
            ]
            if sum(content_fields) != 1:
                raise ManifestDecodeValidationError(
                    f"File '{path}' must have exactly one of 'hash', 'chunkhashes', "
                    f"or 'symlink_target'"
                )

            # Symlinks don't need size/mtime, but regular files do
            if symlink_target is None:
                if size is None:
                    raise ManifestDecodeValidationError(
                        f"File '{path}' must have 'size' field"
                    )
                if mtime is None:
                    raise ManifestDecodeValidationError(
                        f"File '{path}' must have 'mtime' field"
                    )

            # Validate chunkhashes relationship with size
            if chunkhashes is not None:
                if size is None:
                    raise ManifestDecodeValidationError(
                        f"File '{path}' with chunkhashes must have 'size' field"
                    )
                if size <= CHUNK_SIZE_BYTES:
                    raise ManifestDecodeValidationError(
                        f"File '{path}' with chunkhashes must have size > {CHUNK_SIZE_BYTES} "
                        f"(256MB), got {size}"
                    )
                expected_chunks = math.ceil(size / CHUNK_SIZE_BYTES)
                if len(chunkhashes) != expected_chunks:
                    raise ManifestDecodeValidationError(
                        f"File '{path}' with size {size} should have {expected_chunks} chunks, "
                        f"got {len(chunkhashes)}"
                    )

        super().__init__(
            path=path,
            hash=hash,
            size=size,
            mtime=mtime,
            runnable=runnable,
            chunkhashes=chunkhashes,
            symlink_target=symlink_target,
            deleted=deleted,
        )


@dataclass
class AssetManifest(BaseAssetManifest):
    """Version v2025-12-04 of the asset manifest"""

    totalSize: int

    def __init__(
        self,
        *,
        hash_alg: HashAlgorithm,
        dirs: list[BaseManifestDirectoryPath],
        paths: list[BaseManifestPath],
        total_size: int,
        manifest_type: ManifestType = ManifestType.SNAPSHOT,
        parent_manifest_hash: Optional[str] = None,
    ) -> None:
        if hash_alg not in SUPPORTED_HASH_ALGS:
            raise ManifestDecodeValidationError(
                f"Unsupported hashing algorithm: {hash_alg}. "
                f"Must be one of: {[e.value for e in SUPPORTED_HASH_ALGS]}"
            )

        super().__init__(
            hash_alg=hash_alg,
            manifest_type=manifest_type,
            dirs=dirs,
            paths=paths,
            parent_manifest_hash=parent_manifest_hash,
        )
        self.totalSize = total_size
        self.manifestVersion = ManifestVersion.v2025_12_04

    @classmethod
    def decode(cls, *, manifest_data: dict[str, Any]) -> AssetManifest:
        """
        Return an instance of this class given a manifest dictionary.
        Assumes the manifest has been validated prior to calling.
        """
        try:
            hash_alg = HashAlgorithm(manifest_data["hashAlg"])
        except ValueError:
            raise ManifestDecodeValidationError(
                f"Unsupported hashing algorithm: {manifest_data['hashAlg']}. "
                f"Must be one of: {[e.value for e in SUPPORTED_HASH_ALGS]}"
            )

        # Determine manifest type from presence of parentManifestHash
        parent_hash = manifest_data.get("parentManifestHash")
        manifest_type = ManifestType.DIFF if parent_hash else ManifestType.SNAPSHOT

        # Decode directories
        dirs = [
            ManifestDirectoryPath(
                path=d["name"],
                deleted=d.get("delete", False),
            )
            for d in manifest_data.get("dirs", [])
        ]

        # Decode files
        paths = [
            ManifestFilePath(
                path=f["name"],
                hash=f.get("hash"),
                size=f.get("size"),
                mtime=f.get("mtime"),
                runnable=f.get("runnable", False),
                chunkhashes=f.get("chunkhashes"),
                symlink_target=f.get("symlink", {}).get("name") if "symlink" in f else None,
                deleted=f.get("delete", False),
            )
            for f in manifest_data.get("files", [])
        ]

        return cls(
            hash_alg=hash_alg,
            dirs=dirs,
            paths=paths,
            total_size=manifest_data["totalSize"],
            manifest_type=manifest_type,
            parent_manifest_hash=parent_hash,
        )

    @classmethod
    def get_default_hash_alg(cls) -> HashAlgorithm:
        """Returns the default hashing algorithm for the Asset Manifest"""
        return DEFAULT_HASH_ALG

    def encode(self) -> str:
        """
        Return a canonicalized JSON string of the manifest.
        TODO: Implement directory index compression ($N/ references).
        """
        raise NotImplementedError("encode() not yet implemented for v2025_12_04")


class ManifestModel(BaseManifestModel):
    """
    The asset manifest model for v2025-12-04
    """

    manifest_version: ManifestVersion = ManifestVersion.v2025_12_04
    AssetManifest: Type[AssetManifest] = AssetManifest
    FilePath: Type[ManifestFilePath] = ManifestFilePath
    DirectoryPath: Type[ManifestDirectoryPath] = ManifestDirectoryPath
