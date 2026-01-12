# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for v2023-03-03 encode function using unified manifest classes.
"""

import json
import pytest

from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments._snapshots import (
    ManifestDirectoryPath,
    ManifestFilePath,
    SnapshotDiff,
    Snapshot,
    SymlinkPolicy,
    WHOLE_FILE_CHUNK_SIZE,
)
from deadline.job_attachments.asset_manifests.v2023_03_03._encode import encode_v2023
from deadline.job_attachments.exceptions import ManifestDecodeValidationError


class TestEncodeV2023Basic:
    """Basic tests for encode_v2023 function."""

    def test_encode_basic_manifest(self) -> None:
        """Encodes a basic Snapshot to v2023 format."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="file.txt", hash="abc123", size=100, mtime=1000),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = encode_v2023(manifest)
        data = json.loads(result)

        assert data["manifestVersion"] == "2023-03-03"
        assert data["hashAlg"] == "xxh128"
        assert data["totalSize"] == 100
        assert len(data["paths"]) == 1
        assert data["paths"][0]["path"] == "file.txt"
        assert data["paths"][0]["hash"] == "abc123"
        assert data["paths"][0]["size"] == 100
        assert data["paths"][0]["mtime"] == 1000

    def test_encode_multiple_files(self) -> None:
        """Encodes manifest with multiple files."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="a.txt", hash="h1", size=100, mtime=1000),
                ManifestFilePath(path="b.txt", hash="h2", size=200, mtime=2000),
                ManifestFilePath(path="dir/c.txt", hash="h3", size=300, mtime=3000),
            ],
            total_size=600,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = encode_v2023(manifest)
        data = json.loads(result)

        assert len(data["paths"]) == 3
        paths = {p["path"] for p in data["paths"]}
        assert paths == {"a.txt", "b.txt", "dir/c.txt"}

    def test_encode_canonical_json(self) -> None:
        """Encodes to canonical JSON (sorted keys, no whitespace, ASCII)."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="file.txt", hash="abc", size=10, mtime=100),
            ],
            total_size=10,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = encode_v2023(manifest)

        # Should have no whitespace between tokens (except in string values)
        assert " " not in result.replace("file.txt", "")
        # Keys should be sorted
        data = json.loads(result)
        keys = list(data.keys())
        assert keys == sorted(keys)

    def test_encode_utf16_be_sort_order(self) -> None:
        """Encodes paths sorted by UTF-16 BE encoding."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="€", hash="euro", size=1, mtime=100),  # U+20AC
                ManifestFilePath(path="1", hash="one", size=1, mtime=100),  # U+0031
                ManifestFilePath(path="a", hash="a", size=1, mtime=100),  # U+0061
            ],
            total_size=3,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = encode_v2023(manifest)
        data = json.loads(result)

        # UTF-16 BE order: "1" (0x0031) < "a" (0x0061) < "€" (0x20AC)
        paths = [p["path"] for p in data["paths"]]
        assert paths == ["1", "a", "€"]

    def test_encode_rel_diff_snapshots(self) -> None:
        """Encodes SnapshotDiff (without deleted entries)."""
        manifest = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="file.txt", hash="abc123", size=100, mtime=1000),
            ],
            total_size=100,
            parent_manifest_hash="parent123",
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = encode_v2023(manifest)
        data = json.loads(result)

        # v2023 doesn't include parentManifestHash
        assert "parentManifestHash" not in data
        assert data["manifestVersion"] == "2023-03-03"

    def test_encode_wrong_chunk_size_raises_assertion(self) -> None:
        """Manifest with non-WHOLE_FILE_CHUNK_SIZE raises AssertionError."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="file.txt", hash="abc123", size=100, mtime=1000),
            ],
            total_size=100,
            file_chunk_size_bytes=256 * 1024 * 1024,  # 256MB chunk size
        )

        with pytest.raises(AssertionError, match="fileChunkSizeBytes"):
            encode_v2023(manifest)


class TestEncodeV2023SymlinkHandling:
    """Tests for symlink handling in encode_v2023."""

    def test_encode_collapse_symlinks_default(self) -> None:
        """Default behavior collapses symlinks to target content."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="target.txt", hash="h1", size=100, mtime=1000),
                ManifestFilePath(path="link.txt", symlink_target="target.txt"),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = encode_v2023(manifest)
        data = json.loads(result)

        # Both entries should be regular files with same content
        assert len(data["paths"]) == 2
        paths_by_name = {p["path"]: p for p in data["paths"]}

        assert paths_by_name["target.txt"]["hash"] == "h1"
        assert paths_by_name["link.txt"]["hash"] == "h1"
        assert paths_by_name["link.txt"]["size"] == 100

    def test_encode_collapse_symlinks_explicit(self) -> None:
        """Explicit COLLAPSE_ALL policy collapses symlinks."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="target.txt", hash="h1", size=100, mtime=1000),
                ManifestFilePath(path="link.txt", symlink_target="target.txt"),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = encode_v2023(manifest, symlink_policy=SymlinkPolicy.COLLAPSE_ALL)
        data = json.loads(result)

        paths_by_name = {p["path"]: p for p in data["paths"]}
        assert paths_by_name["link.txt"]["hash"] == "h1"

    def test_encode_exclude_symlinks(self) -> None:
        """EXCLUDE_ALL policy removes symlinks from output."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="target.txt", hash="h1", size=100, mtime=1000),
                ManifestFilePath(path="link.txt", symlink_target="target.txt"),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = encode_v2023(manifest, symlink_policy=SymlinkPolicy.EXCLUDE_ALL)
        data = json.loads(result)

        # Only target.txt should remain
        assert len(data["paths"]) == 1
        assert data["paths"][0]["path"] == "target.txt"

    def test_encode_collapse_directory_symlink(self) -> None:
        """Collapses directory symlinks to include all contents."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[ManifestDirectoryPath(path="actual_dir")],
            files=[
                ManifestFilePath(path="link_dir", symlink_target="actual_dir"),
                ManifestFilePath(path="actual_dir/file1.txt", hash="h1", size=100, mtime=1000),
                ManifestFilePath(path="actual_dir/file2.txt", hash="h2", size=200, mtime=2000),
            ],
            total_size=300,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = encode_v2023(manifest, strict=False)  # strict=False to ignore dirs
        data = json.loads(result)

        paths = {p["path"] for p in data["paths"]}
        # Directory symlink should be expanded
        assert "link_dir/file1.txt" in paths
        assert "link_dir/file2.txt" in paths
        # Original files should still be there
        assert "actual_dir/file1.txt" in paths
        assert "actual_dir/file2.txt" in paths

    def test_encode_invalid_symlink_policy_raises_error(self) -> None:
        """Invalid symlink_policy raises error."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="file.txt", hash="h1", size=100, mtime=1000),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        with pytest.raises(ManifestDecodeValidationError, match="symlink_policy"):
            encode_v2023(manifest, symlink_policy=SymlinkPolicy.PRESERVE)


class TestEncodeV2023StrictMode:
    """Tests for strict mode validation in encode_v2023."""

    def test_encode_directories_strict_raises_error(self) -> None:
        """Directories raise error in strict mode."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[ManifestDirectoryPath(path="mydir")],
            files=[
                ManifestFilePath(path="mydir/file.txt", hash="h1", size=100, mtime=1000),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        with pytest.raises(ManifestDecodeValidationError, match="directories"):
            encode_v2023(manifest, strict=True)

    def test_encode_directories_non_strict_ignores(self) -> None:
        """Directories are ignored in non-strict mode."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[ManifestDirectoryPath(path="mydir")],
            files=[
                ManifestFilePath(path="mydir/file.txt", hash="h1", size=100, mtime=1000),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = encode_v2023(manifest, strict=False)
        data = json.loads(result)

        # Should succeed and include the file
        assert len(data["paths"]) == 1
        assert data["paths"][0]["path"] == "mydir/file.txt"

    def test_encode_deleted_strict_raises_error(self) -> None:
        """Deleted entries raise error in strict mode."""
        manifest = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="deleted.txt", deleted=True),
            ],
            total_size=0,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        with pytest.raises(ManifestDecodeValidationError, match="deletion"):
            encode_v2023(manifest, strict=True)

    def test_encode_deleted_non_strict_skips(self) -> None:
        """Deleted entries are skipped in non-strict mode."""
        manifest = SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="file.txt", hash="h1", size=100, mtime=1000),
                ManifestFilePath(path="deleted.txt", deleted=True),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = encode_v2023(manifest, strict=False)
        data = json.loads(result)

        # Only non-deleted file should be included
        assert len(data["paths"]) == 1
        assert data["paths"][0]["path"] == "file.txt"

    def test_encode_runnable_strict_raises_error(self) -> None:
        """Runnable flag raises error in strict mode."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="script.sh", hash="h1", size=100, mtime=1000, runnable=True),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        with pytest.raises(ManifestDecodeValidationError, match="runnable"):
            encode_v2023(manifest, strict=True)

    def test_encode_runnable_non_strict_ignores(self) -> None:
        """Runnable flag is ignored in non-strict mode."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="script.sh", hash="h1", size=100, mtime=1000, runnable=True),
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        result = encode_v2023(manifest, strict=False)
        data = json.loads(result)

        # File should be included without runnable flag
        assert len(data["paths"]) == 1
        assert data["paths"][0]["path"] == "script.sh"
        assert "runnable" not in data["paths"][0]

    def test_encode_unhashed_strict_raises_error(self) -> None:
        """Unhashed files raise error in strict mode."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="unhashed.txt", size=100, mtime=1000),  # No hash
            ],
            total_size=100,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        with pytest.raises(ManifestDecodeValidationError, match="hash"):
            encode_v2023(manifest, strict=True)

    def test_encode_empty_after_filtering_raises_error(self) -> None:
        """Empty manifest after filtering raises error."""
        manifest = Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="link.txt", symlink_target="missing.txt"),
            ],
            total_size=0,
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        # Symlink to missing target will be excluded, leaving empty manifest
        with pytest.raises(ManifestDecodeValidationError, match="at least one"):
            encode_v2023(manifest, symlink_policy=SymlinkPolicy.EXCLUDE_ALL)
