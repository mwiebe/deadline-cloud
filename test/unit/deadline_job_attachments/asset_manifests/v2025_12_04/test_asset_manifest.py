# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the v2025-12-04-beta version of the manifest file."""

import json

import pytest

from deadline.job_attachments.asset_manifests.v2025_12_04.asset_manifest import (
    AssetManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
)
from deadline.job_attachments.asset_manifests import HashAlgorithm
from deadline.job_attachments.asset_manifests.versions import ManifestType


class TestEncode:
    """Tests for the encode() function."""

    def test_encode_basic_snapshot(self) -> None:
        """Test encoding a basic snapshot manifest with directories and files."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            total_size=100,
            dirs=[
                ManifestDirectoryPath(path="textures"),
            ],
            paths=[
                ManifestFilePath(
                    path="textures/tex.png", hash="abc123", size=100, mtime=1234567890
                ),
            ],
        )

        encoded = manifest.encode()
        decoded = json.loads(encoded)

        assert decoded["hashAlg"] == "xxh128"
        assert decoded["manifestVersion"] == "2025-12-04-beta"
        assert decoded["totalSize"] == 100
        assert "parentManifestHash" not in decoded
        assert len(decoded["dirs"]) == 1
        assert decoded["dirs"][0]["name"] == "textures"
        assert len(decoded["files"]) == 1
        assert decoded["files"][0]["name"] == "$0/tex.png"
        assert decoded["files"][0]["hash"] == "abc123"

    def test_encode_directory_compression(self) -> None:
        """Test that directory paths use $N/ compression correctly."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            total_size=300,
            dirs=[
                ManifestDirectoryPath(path="env_sandbox"),
                ManifestDirectoryPath(path="env_sandbox/RaceCarToy"),
                ManifestDirectoryPath(path="env_sandbox/RaceCarToy/textures"),
            ],
            paths=[
                ManifestFilePath(
                    path="env_sandbox/env.blend", hash="hash1", size=100, mtime=1234567890
                ),
                ManifestFilePath(
                    path="env_sandbox/RaceCarToy/car.blend",
                    hash="hash2",
                    size=100,
                    mtime=1234567890,
                ),
                ManifestFilePath(
                    path="env_sandbox/RaceCarToy/textures/tex.png",
                    hash="hash3",
                    size=100,
                    mtime=1234567890,
                ),
            ],
        )

        encoded = manifest.encode()
        decoded = json.loads(encoded)

        # Directories should use $N/ compression
        assert decoded["dirs"][0]["name"] == "env_sandbox"
        assert decoded["dirs"][1]["name"] == "$0/RaceCarToy"
        assert decoded["dirs"][2]["name"] == "$1/textures"

        # Files should reference directory indices
        file_names = [f["name"] for f in decoded["files"]]
        assert "$0/env.blend" in file_names
        assert "$1/car.blend" in file_names
        assert "$2/tex.png" in file_names

    def test_encode_chunked_file(self) -> None:
        """Test encoding a file with chunkhashes (>256MB file)."""
        # 512MB + 1 byte = 3 chunks (256MB + 256MB + 1 byte)
        size = 536870913
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            total_size=size,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path="large_file.bin",
                    chunkhashes=["chunk1hash", "chunk2hash", "chunk3hash"],
                    size=size,
                    mtime=1234567890,
                ),
            ],
        )

        encoded = manifest.encode()
        decoded = json.loads(encoded)

        file_entry = decoded["files"][0]
        assert file_entry["name"] == "large_file.bin"
        assert "hash" not in file_entry
        assert file_entry["chunkhashes"] == ["chunk1hash", "chunk2hash", "chunk3hash"]
        assert file_entry["size"] == size
        assert file_entry["mtime"] == 1234567890

    def test_encode_symlink(self) -> None:
        """Test encoding a symlink entry."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            total_size=100,
            dirs=[
                ManifestDirectoryPath(path="data"),
            ],
            paths=[
                ManifestFilePath(
                    path="data/target.txt", hash="targethash", size=100, mtime=1234567890
                ),
                ManifestFilePath(path="data/link.txt", symlink_target="data/target.txt"),
            ],
        )

        encoded = manifest.encode()
        decoded = json.loads(encoded)

        # Find the symlink entry
        symlink_entry = next(f for f in decoded["files"] if "symlink" in f)
        assert symlink_entry["name"] == "$0/link.txt"
        assert symlink_entry["symlink"]["name"] == "$0/target.txt"
        assert "hash" not in symlink_entry
        assert "size" not in symlink_entry
        assert "mtime" not in symlink_entry

    def test_encode_runnable(self) -> None:
        """Test encoding a file with runnable=true."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            total_size=100,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path="script.sh", hash="scripthash", size=100, mtime=1234567890, runnable=True
                ),
                ManifestFilePath(
                    path="data.txt", hash="datahash", size=100, mtime=1234567890, runnable=False
                ),
            ],
        )

        encoded = manifest.encode()
        decoded = json.loads(encoded)

        script_entry = next(f for f in decoded["files"] if f["name"] == "script.sh")
        data_entry = next(f for f in decoded["files"] if f["name"] == "data.txt")

        assert script_entry["runnable"] is True
        assert "runnable" not in data_entry  # runnable: false should be omitted

    def test_encode_diff_manifest(self) -> None:
        """Test encoding a diff manifest with parentManifestHash."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            total_size=100,
            dirs=[],
            paths=[
                ManifestFilePath(path="new_file.txt", hash="newhash", size=100, mtime=1234567890),
            ],
            manifest_type=ManifestType.DIFF,
            parent_manifest_hash="parenthash123",
        )

        encoded = manifest.encode()
        decoded = json.loads(encoded)

        assert decoded["parentManifestHash"] == "parenthash123"

    def test_encode_deleted_entries(self) -> None:
        """Test encoding deleted files and directories in a diff manifest."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            total_size=0,
            dirs=[
                ManifestDirectoryPath(path="deleted_dir", deleted=True),
            ],
            paths=[
                ManifestFilePath(path="deleted_file.txt", deleted=True),
            ],
            manifest_type=ManifestType.DIFF,
            parent_manifest_hash="parenthash123",
        )

        encoded = manifest.encode()
        decoded = json.loads(encoded)

        assert decoded["dirs"][0]["name"] == "deleted_dir"
        assert decoded["dirs"][0]["delete"] is True

        assert decoded["files"][0]["name"] == "deleted_file.txt"
        assert decoded["files"][0]["delete"] is True
        assert "hash" not in decoded["files"][0]
        assert "size" not in decoded["files"][0]

    def test_encode_unicode_paths(self) -> None:
        """Test that paths are sorted by UTF-16 BE encoding for canonical output."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            total_size=10,
            dirs=[],
            paths=[
                ManifestFilePath(path="€", hash="EuroSign", size=1, mtime=1234567890),
                ManifestFilePath(path="\r", hash="CarriageReturn", size=1, mtime=1234567890),
                ManifestFilePath(path="1", hash="One", size=1, mtime=1234567890),
                ManifestFilePath(path="😀", hash="EmojiGrinningFace", size=1, mtime=1234567890),
                ManifestFilePath(
                    path="ö", hash="LatinSmallLetterOWithDiaeresis", size=1, mtime=1234567890
                ),
            ],
        )

        encoded = manifest.encode()
        decoded = json.loads(encoded)

        # Verify files are sorted by UTF-16 BE encoding
        file_names = [f["name"] for f in decoded["files"]]
        # Expected order based on UTF-16 BE: \r (0x000D), 1 (0x0031), ö (0x00F6), € (0x20AC), 😀 (0xD83D 0xDE00)
        assert file_names == ["\r", "1", "ö", "€", "😀"]

    def test_encode_canonical_json(self) -> None:
        """Test that the output is canonical JSON (sorted keys, no whitespace)."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            total_size=100,
            dirs=[],
            paths=[
                ManifestFilePath(path="file.txt", hash="hash123", size=100, mtime=1234567890),
            ],
        )

        encoded = manifest.encode()

        # Should have no whitespace
        assert " " not in encoded
        assert "\n" not in encoded
        assert "\t" not in encoded

        # Keys should be sorted alphabetically
        decoded = json.loads(encoded)
        assert list(decoded.keys()) == sorted(decoded.keys())


class TestDecode:
    """Tests for the decode() function."""

    def test_decode_basic_snapshot(self, default_manifest_str_v2025_12_04_beta: str) -> None:
        """Test decoding a basic snapshot manifest."""
        manifest = AssetManifest.decode(
            manifest_data=json.loads(default_manifest_str_v2025_12_04_beta)
        )

        assert manifest.hashAlg == HashAlgorithm.XXH128
        assert manifest.manifestVersion.value == "2025-12-04-beta"
        assert manifest.totalSize == 536932693
        assert manifest.manifestType == ManifestType.SNAPSHOT
        assert manifest.parentManifestHash is None

        # Check directories
        assert len(manifest.dirs) == 4
        dir_paths = [d.path for d in manifest.dirs]
        assert "env_sandbox" in dir_paths
        assert "$0/RaceCarToy" in dir_paths  # Note: decode doesn't expand $N/ references yet

        # Check files
        assert len(manifest.paths) == 6

    def test_decode_chunked_file(self) -> None:
        """Test decoding a manifest with chunked files."""
        # 512MB + 1 byte = 3 chunks
        size = 536870913
        manifest_data = {
            "hashAlg": "xxh128",
            "manifestVersion": "2025-12-04-beta",
            "dirs": [],
            "files": [
                {
                    "name": "large.bin",
                    "chunkhashes": ["chunk1", "chunk2", "chunk3"],
                    "size": size,
                    "mtime": 1234567890,
                }
            ],
            "totalSize": size,
        }

        manifest = AssetManifest.decode(manifest_data=manifest_data)

        assert len(manifest.paths) == 1
        file_entry = manifest.paths[0]
        assert file_entry.path == "large.bin"
        assert file_entry.hash is None
        assert file_entry.chunkhashes == ["chunk1", "chunk2", "chunk3"]
        assert file_entry.size == size

    def test_decode_symlink(self) -> None:
        """Test decoding a manifest with symlinks."""
        manifest_data = {
            "hashAlg": "xxh128",
            "manifestVersion": "2025-12-04-beta",
            "dirs": [],
            "files": [
                {
                    "name": "link.txt",
                    "symlink": {"name": "target.txt"},
                }
            ],
            "totalSize": 0,
        }

        manifest = AssetManifest.decode(manifest_data=manifest_data)

        file_entry = manifest.paths[0]
        assert file_entry.path == "link.txt"
        assert file_entry.symlink_target == "target.txt"
        assert file_entry.hash is None

    def test_decode_runnable(self) -> None:
        """Test decoding a manifest with runnable files."""
        manifest_data = {
            "hashAlg": "xxh128",
            "manifestVersion": "2025-12-04-beta",
            "dirs": [],
            "files": [
                {
                    "name": "script.sh",
                    "hash": "scripthash",
                    "size": 100,
                    "mtime": 1234567890,
                    "runnable": True,
                },
                {
                    "name": "data.txt",
                    "hash": "datahash",
                    "size": 100,
                    "mtime": 1234567890,
                },
            ],
            "totalSize": 200,
        }

        manifest = AssetManifest.decode(manifest_data=manifest_data)

        script = next(f for f in manifest.paths if f.path == "script.sh")
        data = next(f for f in manifest.paths if f.path == "data.txt")

        assert script.runnable is True
        assert data.runnable is False

    def test_decode_diff_manifest(self) -> None:
        """Test decoding a diff manifest."""
        manifest_data = {
            "hashAlg": "xxh128",
            "manifestVersion": "2025-12-04-beta",
            "parentManifestHash": "parenthash123",
            "dirs": [{"name": "deleted_dir", "delete": True}],
            "files": [
                {"name": "deleted.txt", "delete": True},
                {"name": "new.txt", "hash": "newhash", "size": 100, "mtime": 1234567890},
            ],
            "totalSize": 100,
        }

        manifest = AssetManifest.decode(manifest_data=manifest_data)

        assert manifest.manifestType == ManifestType.DIFF
        assert manifest.parentManifestHash == "parenthash123"

        # Check deleted directory
        assert manifest.dirs[0].path == "deleted_dir"
        assert manifest.dirs[0].deleted is True

        # Check deleted file
        deleted_file = next(f for f in manifest.paths if f.path == "deleted.txt")
        assert deleted_file.deleted is True

        # Check new file
        new_file = next(f for f in manifest.paths if f.path == "new.txt")
        assert new_file.deleted is False
        assert new_file.hash == "newhash"


class TestRoundtrip:
    """Tests for encode/decode roundtrip."""

    def test_roundtrip_basic(self) -> None:
        """Test that encode -> decode produces equivalent manifest."""
        original = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            total_size=300,
            dirs=[
                ManifestDirectoryPath(path="data"),
                ManifestDirectoryPath(path="data/subdir"),
            ],
            paths=[
                ManifestFilePath(path="data/file1.txt", hash="hash1", size=100, mtime=1234567890),
                ManifestFilePath(
                    path="data/subdir/file2.txt", hash="hash2", size=100, mtime=1234567891
                ),
                ManifestFilePath(path="root.txt", hash="hash3", size=100, mtime=1234567892),
            ],
        )

        encoded = original.encode()
        decoded = AssetManifest.decode(manifest_data=json.loads(encoded))

        assert decoded.hashAlg == original.hashAlg
        assert decoded.totalSize == original.totalSize
        assert decoded.manifestType == original.manifestType
        assert len(decoded.dirs) == len(original.dirs)
        assert len(decoded.paths) == len(original.paths)

    def test_roundtrip_all_features(self) -> None:
        """Test roundtrip with all v2025-12-04-beta features."""
        # 512MB + 1 byte = 3 chunks
        large_size = 536870913
        original = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            total_size=large_size + 200,
            dirs=[
                ManifestDirectoryPath(path="scripts"),
                ManifestDirectoryPath(path="data"),
            ],
            paths=[
                # Regular file
                ManifestFilePath(path="data/file.txt", hash="hash1", size=100, mtime=1234567890),
                # Chunked file (3 chunks for 512MB + 1 byte)
                ManifestFilePath(
                    path="data/large.bin",
                    chunkhashes=["chunk1", "chunk2", "chunk3"],
                    size=large_size,
                    mtime=1234567891,
                ),
                # Symlink
                ManifestFilePath(path="data/link.txt", symlink_target="data/file.txt"),
                # Runnable file
                ManifestFilePath(
                    path="scripts/run.sh", hash="hash2", size=100, mtime=1234567892, runnable=True
                ),
            ],
        )

        encoded = original.encode()
        decoded = AssetManifest.decode(manifest_data=json.loads(encoded))

        # Verify all features preserved
        assert decoded.totalSize == original.totalSize

        # Find specific files by checking their properties
        chunked = next((f for f in decoded.paths if f.chunkhashes is not None), None)
        assert chunked is not None
        assert chunked.chunkhashes == ["chunk1", "chunk2", "chunk3"]

        symlink = next((f for f in decoded.paths if f.symlink_target is not None), None)
        assert symlink is not None

        runnable = next((f for f in decoded.paths if f.runnable), None)
        assert runnable is not None

    def test_encode_is_deterministic(self) -> None:
        """Test that encoding the same manifest twice produces identical output."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            total_size=200,
            dirs=[
                ManifestDirectoryPath(path="b_dir"),
                ManifestDirectoryPath(path="a_dir"),
            ],
            paths=[
                ManifestFilePath(path="b_dir/file.txt", hash="hash1", size=100, mtime=1234567890),
                ManifestFilePath(path="a_dir/file.txt", hash="hash2", size=100, mtime=1234567891),
            ],
        )

        encoded1 = manifest.encode()
        encoded2 = manifest.encode()

        assert encoded1 == encoded2
