# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
EXPERIMENTAL: Module that defines the v2025-12-04-beta version of the asset manifest.

This format is under development and subject to change. Do not use in production.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional, Type

from ..base_manifest import (
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
    EXPERIMENTAL: Directory entry for version v2025-12-04-beta of the asset manifest.
    """

    manifest_version = ManifestVersion.v2025_12_04_beta

    def __init__(self, *, path: str, deleted: bool = False) -> None:
        super().__init__(path=path, deleted=deleted)


@dataclass
class ManifestFilePath(BaseManifestPath):
    """
    EXPERIMENTAL: File entry for version v2025-12-04-beta of the asset manifest.
    Validation is performed in the base class.
    """

    manifest_version = ManifestVersion.v2025_12_04_beta

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
    """EXPERIMENTAL: Version v2025-12-04-beta of the asset manifest."""

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
        self.manifestVersion = ManifestVersion.v2025_12_04_beta

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
        Implements directory index compression ($N/ references) per the spec.

        The format is canonical:
        - Directories sorted lexicographically by full path
        - Files sorted lexicographically by full path (UTF-16 BE encoding)
        - Keys sorted alphabetically within each object
        - No whitespace between JSON tokens
        """
        # Sort and deduplicate directories by full path for canonical output
        seen_dirs: set[str] = set()
        unique_dirs: list[BaseManifestDirectoryPath] = []
        for d in self.dirs:
            if d.path not in seen_dirs:
                seen_dirs.add(d.path)
                unique_dirs.append(d)
        sorted_dirs = sorted(unique_dirs, key=lambda d: d.path)

        # Build directory index mapping: full_path -> index
        dir_index: dict[str, int] = {}
        for i, d in enumerate(sorted_dirs):
            dir_index[d.path] = i

        # Encode directories with $N/ compression
        dirs_json: list[dict[str, Any]] = []
        for d in sorted_dirs:
            encoded_name = self._encode_path_with_dir_index(d.path, dir_index)
            dir_entry: dict[str, Any] = {"name": encoded_name}
            if d.deleted:
                dir_entry["delete"] = True
            dirs_json.append(dir_entry)

        # Sort and deduplicate files by full path (UTF-16 BE for canonical ordering)
        seen_files: set[str] = set()
        unique_files: list[BaseManifestPath] = []
        for f in self.paths:
            if f.path not in seen_files:
                seen_files.add(f.path)
                unique_files.append(f)
        sorted_files = sorted(
            unique_files,
            key=lambda f: f.path.encode("utf-16_be", errors="surrogatepass"),
        )

        # Encode files with $N/ compression
        files_json: list[dict[str, Any]] = []
        for f in sorted_files:
            encoded_name = self._encode_path_with_dir_index(f.path, dir_index)
            file_entry: dict[str, Any] = {"name": encoded_name}

            # Add content field (exactly one of: hash, chunkhashes, symlink, or none for deleted)
            if f.hash is not None:
                file_entry["hash"] = f.hash
            elif f.chunkhashes is not None:
                file_entry["chunkhashes"] = f.chunkhashes
            elif f.symlink_target is not None:
                # Symlink target also uses $N/ compression
                encoded_target = self._encode_path_with_dir_index(f.symlink_target, dir_index)
                file_entry["symlink"] = {"name": encoded_target}

            # Add metadata fields (only for non-deleted, non-symlink entries)
            if not f.deleted and f.symlink_target is None:
                if f.size is not None:
                    file_entry["size"] = f.size
                if f.mtime is not None:
                    file_entry["mtime"] = f.mtime
                # runnable: true only appears when True (never runnable: false)
                if f.runnable:
                    file_entry["runnable"] = True

            # Deletion marker
            if f.deleted:
                file_entry["delete"] = True

            files_json.append(file_entry)

        # Build manifest dictionary with canonical key order
        manifest_dict: dict[str, Any] = {
            "dirs": dirs_json,
            "files": files_json,
            "hashAlg": self.hashAlg.value,
            "manifestVersion": self.manifestVersion.value,
            "totalSize": self.totalSize,
        }

        # Add parentManifestHash for diff manifests
        if self.manifestType == ManifestType.DIFF and self.parentManifestHash:
            manifest_dict["parentManifestHash"] = self.parentManifestHash

        return json.dumps(manifest_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def _encode_path_with_dir_index(self, path: str, dir_index: dict[str, int]) -> str:
        """
        Encode a path using directory index compression ($N/ references).

        For a path like "env_sandbox/RaceCarToy/file.blend":
        - If "env_sandbox/RaceCarToy" is at index 1, returns "$1/file.blend"
        - If no parent directory in index, returns the original path

        Args:
            path: The full path to encode
            dir_index: Mapping of directory paths to their indices

        Returns:
            The encoded path with $N/ reference if applicable
        """
        # Find the last slash to split directory from filename
        last_slash = path.rfind("/")

        if last_slash == -1:
            # No directory component, return as-is
            return path

        dir_path = path[:last_slash]
        name = path[last_slash + 1 :]

        if dir_path in dir_index:
            return f"${dir_index[dir_path]}/{name}"

        # Directory not in index, return original path
        return path


class ManifestModel(BaseManifestModel):
    """
    EXPERIMENTAL: The asset manifest model for v2025-12-04-beta.
    """

    manifest_version: ManifestVersion = ManifestVersion.v2025_12_04_beta
    AssetManifest: Type[AssetManifest] = AssetManifest
    FilePath: Type[ManifestFilePath] = ManifestFilePath
    DirectoryPath: Type[ManifestDirectoryPath] = ManifestDirectoryPath
