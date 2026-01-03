# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for v2025-12 encode/decode functions.
"""

import json
import pytest

from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.manifest import (
    AbsDiffManifest,
    AbsSnapshotManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
    RelDiffManifest,
    RelSnapshotManifest,
)
from deadline.job_attachments.asset_manifests.v2025_12_04 import (
    decode_v2025,
    encode_v2025,
)
from deadline.job_attachments.exceptions import ManifestDecodeValidationError


class TestEncodeV2025:
    """Tests for encode_v2025 function."""

    def test_encode_abs_snapshot(self) -> None:
        """Encodes AbsSnapshotManifest with correct specificationVersion."""
        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="/project/file.txt", hash="abc123", size=100, mtime=1000),
            ],
            total_size=100,
        )

        result = encode_v2025(manifest)
        data = json.loads(result)

        assert data["specificationVersion"] == "absolute-manifest-snapshot-beta-2025-12"
        assert data["hashAlg"] == "xxh128"
        assert data["totalSize"] == 100
        assert len(data["files"]) == 1
        # Parent directory is auto-collected, so file uses $N/ compression
        assert data["files"][0]["name"] == "$0/file.txt"
        assert data["dirs"][0]["name"] == "/project"

    def test_encode_rel_snapshot(self) -> None:
        """Encodes RelSnapshotManifest with correct specificationVersion."""
        manifest = RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="project/file.txt", hash="abc123", size=100, mtime=1000),
            ],
            total_size=100,
        )

        result = encode_v2025(manifest)
        data = json.loads(result)

        assert data["specificationVersion"] == "relative-manifest-snapshot-beta-2025-12"

    def test_encode_abs_diff(self) -> None:
        """Encodes AbsDiffManifest with correct specificationVersion."""
        manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="/project/file.txt", hash="abc123", size=100, mtime=1000),
            ],
            total_size=100,
            parent_manifest_hash="parent123",
        )

        result = encode_v2025(manifest)
        data = json.loads(result)

        assert data["specificationVersion"] == "absolute-manifest-diff-beta-2025-12"
        assert data["parentManifestHash"] == "parent123"

    def test_encode_rel_diff(self) -> None:
        """Encodes RelDiffManifest with correct specificationVersion."""
        manifest = RelDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="project/file.txt", hash="abc123", size=100, mtime=1000),
            ],
            total_size=100,
            parent_manifest_hash="parent123",
        )

        result = encode_v2025(manifest)
        data = json.loads(result)

        assert data["specificationVersion"] == "relative-manifest-diff-beta-2025-12"

    def test_encode_directory_index_compression(self) -> None:
        """Encodes paths with $N/ directory index compression."""
        manifest = RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="project"),
                ManifestDirectoryPath(path="project/src"),
            ],
            files=[
                ManifestFilePath(path="project/src/main.py", hash="abc123", size=100, mtime=1000),
                ManifestFilePath(path="project/src/utils.py", hash="def456", size=200, mtime=2000),
            ],
            total_size=300,
        )

        result = encode_v2025(manifest)
        data = json.loads(result)

        # Directories should be sorted and use $N/ compression
        assert data["dirs"][0]["name"] == "project"
        assert data["dirs"][1]["name"] == "$0/src"

        # Files should use $N/ compression referencing project/src (index 1)
        file_names = [f["name"] for f in data["files"]]
        assert "$1/main.py" in file_names
        assert "$1/utils.py" in file_names

    def test_encode_symlink(self) -> None:
        """Encodes symlinks correctly."""
        manifest = RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[ManifestDirectoryPath(path="project")],
            files=[
                ManifestFilePath(path="project/link.txt", symlink_target="project/target.txt"),
            ],
            total_size=0,
        )

        result = encode_v2025(manifest)
        data = json.loads(result)

        file_entry = data["files"][0]
        assert "symlink" in file_entry
        assert file_entry["symlink"]["name"] == "$0/target.txt"

    def test_encode_deleted_entry(self) -> None:
        """Encodes deleted entries in diff manifests."""
        manifest = RelDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="project/deleted.txt", deleted=True),
            ],
            total_size=0,
        )

        result = encode_v2025(manifest)
        data = json.loads(result)

        file_entry = data["files"][0]
        assert file_entry["delete"] is True
        assert "hash" not in file_entry

    def test_encode_canonical_json(self) -> None:
        """Encodes to canonical JSON (sorted keys, no whitespace)."""
        manifest = RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="file.txt", hash="abc", size=10, mtime=100),
            ],
            total_size=10,
        )

        result = encode_v2025(manifest)

        # Should have no whitespace between tokens
        assert " " not in result.replace("file.txt", "")
        # Keys should be sorted
        data = json.loads(result)
        keys = list(data.keys())
        assert keys == sorted(keys)

    def test_encode_auto_collects_parent_directories(self) -> None:
        """Encodes auto-collects all parent directories from file paths."""
        # Create manifest with deep file paths but no explicit directories
        manifest = RelSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],  # No explicit directories
            files=[
                ManifestFilePath(path="a/b/c/file1.txt", hash="abc", size=10, mtime=100),
                ManifestFilePath(path="a/b/d/file2.txt", hash="def", size=20, mtime=200),
                ManifestFilePath(path="x/y/file3.txt", hash="ghi", size=30, mtime=300),
            ],
            total_size=60,
        )

        result = encode_v2025(manifest)
        data = json.loads(result)

        # Should auto-collect all parent directories: a, a/b, a/b/c, a/b/d, x, x/y
        dir_paths = []
        dir_index: dict[int, str] = {}
        for i, d in enumerate(data["dirs"]):
            name = d["name"]
            # Expand $N/ references
            if name.startswith("$"):
                slash = name.find("/")
                if slash > 0:
                    idx = int(name[1:slash])
                    name = f"{dir_index[idx]}/{name[slash + 1 :]}"
            dir_index[i] = name
            dir_paths.append(name)

        assert "a" in dir_paths
        assert "a/b" in dir_paths
        assert "a/b/c" in dir_paths
        assert "a/b/d" in dir_paths
        assert "x" in dir_paths
        assert "x/y" in dir_paths

    def test_encode_preserves_explicit_dir_deleted_flag(self) -> None:
        """Encodes preserves deleted flag from explicit directories."""
        manifest = RelDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="a/b", deleted=True),  # Explicit with deleted=True
            ],
            files=[
                ManifestFilePath(path="a/b/c/file.txt", hash="abc", size=10, mtime=100),
            ],
            total_size=10,
            parent_manifest_hash="parent123",
        )

        result = encode_v2025(manifest)
        data = json.loads(result)

        # Find the a/b directory entry
        dir_index: dict[int, str] = {}
        ab_entry = None
        for i, d in enumerate(data["dirs"]):
            name = d["name"]
            if name.startswith("$"):
                slash = name.find("/")
                if slash > 0:
                    idx = int(name[1:slash])
                    name = f"{dir_index[idx]}/{name[slash + 1 :]}"
            dir_index[i] = name
            if name == "a/b":
                ab_entry = d

        assert ab_entry is not None
        assert ab_entry.get("delete") is True


class TestDecodeV2025:
    """Tests for decode_v2025 function."""

    def test_decode_abs_snapshot(self) -> None:
        """Decodes to AbsSnapshotManifest based on specificationVersion."""
        json_str = json.dumps(
            {
                "specificationVersion": "absolute-manifest-snapshot-beta-2025-12",
                "hashAlg": "xxh128",
                "totalSize": 100,
                "dirs": [
                    {"name": "/project"},
                ],
                "files": [{"name": "$0/file.txt", "hash": "abc123", "size": 100, "mtime": 1000}],
            }
        )

        result = decode_v2025(json_str)

        assert isinstance(result, AbsSnapshotManifest)
        assert result.hashAlg == HashAlgorithm.XXH128
        assert result.totalSize == 100
        assert len(result.files) == 1
        assert result.files[0].path == "/project/file.txt"

    def test_decode_rel_snapshot(self) -> None:
        """Decodes to RelSnapshotManifest based on specificationVersion."""
        json_str = json.dumps(
            {
                "specificationVersion": "relative-manifest-snapshot-beta-2025-12",
                "hashAlg": "xxh128",
                "totalSize": 100,
                "dirs": [
                    {"name": "project"},
                ],
                "files": [{"name": "$0/file.txt", "hash": "abc123", "size": 100, "mtime": 1000}],
            }
        )

        result = decode_v2025(json_str)

        assert isinstance(result, RelSnapshotManifest)

    def test_decode_abs_diff(self) -> None:
        """Decodes to AbsDiffManifest based on specificationVersion."""
        json_str = json.dumps(
            {
                "specificationVersion": "absolute-manifest-diff-beta-2025-12",
                "hashAlg": "xxh128",
                "totalSize": 100,
                "parentManifestHash": "parent123",
                "dirs": [
                    {"name": "/project"},
                ],
                "files": [{"name": "$0/file.txt", "hash": "abc123", "size": 100, "mtime": 1000}],
            }
        )

        result = decode_v2025(json_str)

        assert isinstance(result, AbsDiffManifest)
        assert result.parentManifestHash == "parent123"

    def test_decode_rel_diff(self) -> None:
        """Decodes to RelDiffManifest based on specificationVersion."""
        json_str = json.dumps(
            {
                "specificationVersion": "relative-manifest-diff-beta-2025-12",
                "hashAlg": "xxh128",
                "totalSize": 100,
                "parentManifestHash": "parent123",
                "dirs": [
                    {"name": "project"},
                ],
                "files": [{"name": "$0/file.txt", "hash": "abc123", "size": 100, "mtime": 1000}],
            }
        )

        result = decode_v2025(json_str)

        assert isinstance(result, RelDiffManifest)

    def test_decode_directory_index_expansion(self) -> None:
        """Decodes $N/ directory references to full paths."""
        json_str = json.dumps(
            {
                "specificationVersion": "relative-manifest-snapshot-beta-2025-12",
                "hashAlg": "xxh128",
                "totalSize": 100,
                "dirs": [
                    {"name": "project"},
                    {"name": "$0/src"},
                ],
                "files": [
                    {"name": "$1/main.py", "hash": "abc123", "size": 100, "mtime": 1000},
                ],
            }
        )

        result = decode_v2025(json_str)

        assert result.dirs[0].path == "project"
        assert result.dirs[1].path == "project/src"
        assert result.files[0].path == "project/src/main.py"

    def test_decode_symlink(self) -> None:
        """Decodes symlink entries correctly."""
        json_str = json.dumps(
            {
                "specificationVersion": "relative-manifest-snapshot-beta-2025-12",
                "hashAlg": "xxh128",
                "totalSize": 0,
                "dirs": [{"name": "project"}],
                "files": [
                    {"name": "$0/link.txt", "symlink": {"name": "$0/target.txt"}},
                ],
            }
        )

        result = decode_v2025(json_str)

        assert result.files[0].path == "project/link.txt"
        assert result.files[0].symlink_target == "project/target.txt"

    def test_decode_deleted_entry(self) -> None:
        """Decodes deleted entries in diff manifests."""
        json_str = json.dumps(
            {
                "specificationVersion": "relative-manifest-diff-beta-2025-12",
                "hashAlg": "xxh128",
                "totalSize": 0,
                "dirs": [],
                "files": [
                    {"name": "deleted.txt", "delete": True},
                ],
            }
        )

        result = decode_v2025(json_str)

        assert result.files[0].deleted is True

    def test_decode_invalid_spec_version(self) -> None:
        """Raises error for unknown specificationVersion."""
        json_str = json.dumps(
            {
                "specificationVersion": "unknown-version",
                "hashAlg": "xxh128",
                "totalSize": 0,
                "dirs": [],
                "files": [],
            }
        )

        with pytest.raises(ManifestDecodeValidationError, match="specificationVersion"):
            decode_v2025(json_str)

    def test_decode_invalid_json(self) -> None:
        """Raises error for invalid JSON."""
        with pytest.raises(ManifestDecodeValidationError, match="Invalid JSON"):
            decode_v2025("not valid json")

    def test_decode_missing_required_field(self) -> None:
        """Raises error for missing required fields."""
        json_str = json.dumps(
            {
                "specificationVersion": "relative-manifest-snapshot-beta-2025-12",
                "hashAlg": "xxh128",
                # Missing totalSize, dirs, files
            }
        )

        with pytest.raises(ManifestDecodeValidationError, match="missing required"):
            decode_v2025(json_str)


class TestRoundTrip:
    """Tests for encode/decode round-trip."""

    def test_roundtrip_abs_snapshot(self) -> None:
        """Round-trip preserves AbsSnapshotManifest data."""
        original = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="/project"),
                ManifestDirectoryPath(path="/project/src"),
            ],
            files=[
                ManifestFilePath(path="/project/src/main.py", hash="abc123", size=100, mtime=1000),
                ManifestFilePath(
                    path="/project/src/link.py", symlink_target="/project/src/main.py"
                ),
            ],
            total_size=100,
        )

        encoded = encode_v2025(original)
        decoded = decode_v2025(encoded)

        assert isinstance(decoded, AbsSnapshotManifest)
        assert decoded.hashAlg == original.hashAlg
        assert decoded.totalSize == original.totalSize
        assert len(decoded.dirs) == len(original.dirs)
        assert len(decoded.files) == len(original.files)

        # Check file paths are preserved
        original_paths = {p.path for p in original.files}
        decoded_paths = {p.path for p in decoded.files}
        assert original_paths == decoded_paths

    def test_roundtrip_rel_diff_with_deletions(self) -> None:
        """Round-trip preserves RelDiffManifest with deletions."""
        original = RelDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="project", deleted=True),
            ],
            files=[
                ManifestFilePath(path="project/new.txt", hash="abc123", size=100, mtime=1000),
                ManifestFilePath(path="project/deleted.txt", deleted=True),
            ],
            total_size=100,
            parent_manifest_hash="parent123",
        )

        encoded = encode_v2025(original)
        decoded = decode_v2025(encoded)

        assert isinstance(decoded, RelDiffManifest)
        assert decoded.parentManifestHash == "parent123"

        # Check deleted entries
        deleted_files = [p for p in decoded.files if p.deleted]
        assert len(deleted_files) == 1
        assert deleted_files[0].path == "project/deleted.txt"

        deleted_dirs = [d for d in decoded.dirs if d.deleted]
        assert len(deleted_dirs) == 1
        assert deleted_dirs[0].path == "project"
