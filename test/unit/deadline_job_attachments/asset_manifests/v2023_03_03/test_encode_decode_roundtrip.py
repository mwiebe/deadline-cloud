# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Roundtrip tests for v2023-03-03 encode/decode functions.

These tests verify that:
1. The new encode_v2023/decode_v2023 functions produce identical JSON to the old AssetManifest
2. Roundtrips through both formats preserve data correctly
3. Both implementations handle the same sample manifest sets identically
"""

import json
import pytest
from typing import Any, Dict, List

from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.base_manifest import BaseManifestPath
from deadline.job_attachments._snapshots import (
    ManifestFilePath,
    RelSnapshotManifest,
    WHOLE_FILE_CHUNK_SIZE,
)
from deadline.job_attachments.asset_manifests.v2023_03_03._encode import encode_v2023
from deadline.job_attachments.asset_manifests.v2023_03_03._decode import decode_v2023
from deadline.job_attachments.asset_manifests.v2023_03_03.asset_manifest import (
    AssetManifest,
    ManifestPath,
)


# Sample manifest data sets for testing
SAMPLE_MANIFESTS: List[Dict[str, Any]] = [
    # Basic manifest with simple files
    {
        "hashAlg": "xxh128",
        "manifestVersion": "2023-03-03",
        "paths": [
            {"hash": "abc123", "mtime": 1000, "path": "file.txt", "size": 100},
        ],
        "totalSize": 100,
    },
    # Multiple files
    {
        "hashAlg": "xxh128",
        "manifestVersion": "2023-03-03",
        "paths": [
            {"hash": "h1", "mtime": 1000, "path": "a.txt", "size": 100},
            {"hash": "h2", "mtime": 2000, "path": "b.txt", "size": 200},
            {"hash": "h3", "mtime": 3000, "path": "dir/c.txt", "size": 300},
        ],
        "totalSize": 600,
    },
    # Files with special characters (UTF-16 BE sort order)
    {
        "hashAlg": "xxh128",
        "manifestVersion": "2023-03-03",
        "paths": [
            {"hash": "one", "mtime": 100, "path": "1", "size": 1},
            {"hash": "a", "mtime": 100, "path": "a", "size": 1},
            {"hash": "euro", "mtime": 100, "path": "\u20ac", "size": 1},  # €
        ],
        "totalSize": 3,
    },
    # Deep directory structure
    {
        "hashAlg": "xxh128",
        "manifestVersion": "2023-03-03",
        "paths": [
            {"hash": "h1", "mtime": 1000, "path": "a/b/c/file1.txt", "size": 10},
            {"hash": "h2", "mtime": 2000, "path": "a/b/d/file2.txt", "size": 20},
            {"hash": "h3", "mtime": 3000, "path": "x/y/file3.txt", "size": 30},
        ],
        "totalSize": 60,
    },
]


class TestRoundtripNewFormat:
    """Tests for encode/decode roundtrip using new memory format (RelSnapshotManifest)."""

    @pytest.mark.parametrize("manifest_data", SAMPLE_MANIFESTS)
    def test_decode_encode_roundtrip(self, manifest_data: Dict[str, Any]) -> None:
        """Decode then encode produces identical bytes."""
        # Create canonical JSON (sorted keys, compact separators)
        json_str = json.dumps(manifest_data, sort_keys=True, separators=(",", ":"))

        # Decode to new format
        decoded = decode_v2023(json_str)
        assert isinstance(decoded, RelSnapshotManifest)

        # Encode back to JSON
        encoded = encode_v2023(decoded)

        # Should produce identical bytes
        assert encoded == json_str

    @pytest.mark.parametrize("manifest_data", SAMPLE_MANIFESTS)
    def test_encode_decode_roundtrip(self, manifest_data: Dict[str, Any]) -> None:
        """Create manifest, encode, decode, encode again - verify identical bytes."""
        # Create RelSnapshotManifest from test data
        files = [
            ManifestFilePath(
                path=p["path"],
                hash=p["hash"],
                size=p["size"],
                mtime=p["mtime"],
            )
            for p in manifest_data["paths"]
        ]
        manifest = RelSnapshotManifest(
            hash_alg=HashAlgorithm(manifest_data["hashAlg"]),
            files=files,
            total_size=manifest_data["totalSize"],
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        # Encode to JSON
        encoded1 = encode_v2023(manifest)

        # Decode back to manifest
        decoded = decode_v2023(encoded1)

        # Encode again
        encoded2 = encode_v2023(decoded)

        # Should produce identical bytes
        assert encoded1 == encoded2


class TestRoundtripOldFormat:
    """Tests for encode/decode roundtrip using old memory format (AssetManifest)."""

    @pytest.mark.parametrize("manifest_data", SAMPLE_MANIFESTS)
    def test_decode_encode_roundtrip(self, manifest_data: Dict[str, Any]) -> None:
        """Decode then encode produces identical bytes using old format."""
        # Create canonical JSON (sorted keys, compact separators)
        json_str = json.dumps(manifest_data, sort_keys=True, separators=(",", ":"))

        # Decode using old format
        decoded = AssetManifest.decode(manifest_data=manifest_data)

        # Encode back to JSON
        encoded = decoded.encode()

        # Should produce identical bytes
        assert encoded == json_str

    @pytest.mark.parametrize("manifest_data", SAMPLE_MANIFESTS)
    def test_encode_decode_roundtrip(self, manifest_data: Dict[str, Any]) -> None:
        """Create manifest, encode, decode, encode again - verify identical bytes using old format."""
        # Create AssetManifest from test data
        paths: List[BaseManifestPath] = [
            ManifestPath(
                path=p["path"],
                hash=p["hash"],
                size=p["size"],
                mtime=p["mtime"],
            )
            for p in manifest_data["paths"]
        ]
        manifest = AssetManifest(
            hash_alg=HashAlgorithm(manifest_data["hashAlg"]),
            paths=paths,
            total_size=manifest_data["totalSize"],
        )

        # Encode to JSON
        encoded1 = manifest.encode()

        # Decode back to manifest
        decoded = AssetManifest.decode(manifest_data=json.loads(encoded1))

        # Encode again
        encoded2 = decoded.encode()

        # Should produce identical bytes
        assert encoded1 == encoded2


class TestCrossFormatEquivalence:
    """Tests that new and old formats produce identical JSON output."""

    @pytest.mark.parametrize("manifest_data", SAMPLE_MANIFESTS)
    def test_new_and_old_encode_produce_same_json(self, manifest_data: Dict[str, Any]) -> None:
        """New encode_v2023 and old AssetManifest.encode produce identical JSON."""
        # Create old format manifest
        old_paths: List[BaseManifestPath] = [
            ManifestPath(
                path=p["path"],
                hash=p["hash"],
                size=p["size"],
                mtime=p["mtime"],
            )
            for p in manifest_data["paths"]
        ]
        old_manifest = AssetManifest(
            hash_alg=HashAlgorithm(manifest_data["hashAlg"]),
            paths=old_paths,
            total_size=manifest_data["totalSize"],
        )

        # Create new format manifest
        new_files = [
            ManifestFilePath(
                path=p["path"],
                hash=p["hash"],
                size=p["size"],
                mtime=p["mtime"],
            )
            for p in manifest_data["paths"]
        ]
        new_manifest = RelSnapshotManifest(
            hash_alg=HashAlgorithm(manifest_data["hashAlg"]),
            files=new_files,
            total_size=manifest_data["totalSize"],
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        # Encode both
        old_encoded = old_manifest.encode()
        new_encoded = encode_v2023(new_manifest)

        # Should produce identical JSON
        assert old_encoded == new_encoded

    @pytest.mark.parametrize("manifest_data", SAMPLE_MANIFESTS)
    def test_decode_to_both_formats_equivalent(self, manifest_data: Dict[str, Any]) -> None:
        """Decoding same JSON to both formats produces equivalent data."""
        json_str = json.dumps(manifest_data, sort_keys=True, separators=(",", ":"))

        # Decode to old format
        old_decoded = AssetManifest.decode(manifest_data=manifest_data)

        # Decode to new format
        new_decoded = decode_v2023(json_str)

        # Compare hash algorithm
        assert old_decoded.hashAlg == new_decoded.hashAlg

        # Compare total size
        assert old_decoded.totalSize == new_decoded.totalSize

        # Compare paths
        old_paths = {(p.path, p.hash, p.size, p.mtime) for p in old_decoded.paths}
        new_paths = {(f.path, f.hash, f.size, f.mtime) for f in new_decoded.files}
        assert old_paths == new_paths


class TestSampleManifestFile:
    """Tests using the sample manifest file from test data."""

    def test_roundtrip_sample_manifest_new_format(
        self, default_manifest_str_v2023_03_03: str
    ) -> None:
        """Roundtrip sample manifest through new format produces identical bytes."""
        # Decode
        decoded = decode_v2023(default_manifest_str_v2023_03_03)
        assert isinstance(decoded, RelSnapshotManifest)

        # Encode
        encoded1 = encode_v2023(decoded)

        # Decode again
        decoded2 = decode_v2023(encoded1)

        # Encode again
        encoded2 = encode_v2023(decoded2)

        # Should produce identical bytes
        assert encoded1 == encoded2

    def test_roundtrip_sample_manifest_old_format(
        self, default_manifest_str_v2023_03_03: str
    ) -> None:
        """Roundtrip sample manifest through old format produces identical bytes."""
        manifest_data = json.loads(default_manifest_str_v2023_03_03)

        # Decode
        decoded = AssetManifest.decode(manifest_data=manifest_data)

        # Encode
        encoded1 = decoded.encode()

        # Decode again
        decoded2 = AssetManifest.decode(manifest_data=json.loads(encoded1))

        # Encode again
        encoded2 = decoded2.encode()

        # Should produce identical bytes
        assert encoded1 == encoded2

    def test_sample_manifest_cross_format_equivalence(
        self, default_manifest_str_v2023_03_03: str
    ) -> None:
        """Sample manifest produces identical bytes from both old and new formats."""
        manifest_data = json.loads(default_manifest_str_v2023_03_03)

        # Decode to old format and encode
        old_decoded = AssetManifest.decode(manifest_data=manifest_data)
        old_encoded = old_decoded.encode()

        # Decode to new format and encode
        new_decoded = decode_v2023(default_manifest_str_v2023_03_03)
        new_encoded = encode_v2023(new_decoded)

        # Should produce identical bytes
        assert old_encoded == new_encoded


class TestConfTestFixtures:
    """Tests using conftest.py manifest fixtures."""

    def test_manifest_one_cross_format(self, test_manifest_one: Dict[str, Any]) -> None:
        """test_manifest_one produces same output from both formats."""
        # Old format
        old_paths: List[BaseManifestPath] = [
            ManifestPath(path=p["path"], hash=p["hash"], size=p["size"], mtime=p["mtime"])
            for p in test_manifest_one["paths"]
        ]
        old_manifest = AssetManifest(
            hash_alg=HashAlgorithm(test_manifest_one["hashAlg"]),
            paths=old_paths,
            total_size=test_manifest_one["totalSize"],
        )
        old_encoded = old_manifest.encode()

        # New format
        new_files = [
            ManifestFilePath(path=p["path"], hash=p["hash"], size=p["size"], mtime=p["mtime"])
            for p in test_manifest_one["paths"]
        ]
        new_manifest = RelSnapshotManifest(
            hash_alg=HashAlgorithm(test_manifest_one["hashAlg"]),
            files=new_files,
            total_size=test_manifest_one["totalSize"],
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )
        new_encoded = encode_v2023(new_manifest)

        assert old_encoded == new_encoded

    def test_manifest_two_cross_format(self, test_manifest_two: Dict[str, Any]) -> None:
        """test_manifest_two produces same output from both formats."""
        # Old format
        old_paths: List[BaseManifestPath] = [
            ManifestPath(path=p["path"], hash=p["hash"], size=p["size"], mtime=p["mtime"])
            for p in test_manifest_two["paths"]
        ]
        old_manifest = AssetManifest(
            hash_alg=HashAlgorithm(test_manifest_two["hashAlg"]),
            paths=old_paths,
            total_size=test_manifest_two["totalSize"],
        )
        old_encoded = old_manifest.encode()

        # New format
        new_files = [
            ManifestFilePath(path=p["path"], hash=p["hash"], size=p["size"], mtime=p["mtime"])
            for p in test_manifest_two["paths"]
        ]
        new_manifest = RelSnapshotManifest(
            hash_alg=HashAlgorithm(test_manifest_two["hashAlg"]),
            files=new_files,
            total_size=test_manifest_two["totalSize"],
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )
        new_encoded = encode_v2023(new_manifest)

        assert old_encoded == new_encoded

    def test_merged_manifest_cross_format(self, merged_manifest: Dict[str, Any]) -> None:
        """merged_manifest produces same output from both formats."""
        # Old format
        old_paths: List[BaseManifestPath] = [
            ManifestPath(path=p["path"], hash=p["hash"], size=p["size"], mtime=p["mtime"])
            for p in merged_manifest["paths"]
        ]
        old_manifest = AssetManifest(
            hash_alg=HashAlgorithm(merged_manifest["hashAlg"]),
            paths=old_paths,
            total_size=merged_manifest["totalSize"],
        )
        old_encoded = old_manifest.encode()

        # New format
        new_files = [
            ManifestFilePath(path=p["path"], hash=p["hash"], size=p["size"], mtime=p["mtime"])
            for p in merged_manifest["paths"]
        ]
        new_manifest = RelSnapshotManifest(
            hash_alg=HashAlgorithm(merged_manifest["hashAlg"]),
            files=new_files,
            total_size=merged_manifest["totalSize"],
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )
        new_encoded = encode_v2023(new_manifest)

        assert old_encoded == new_encoded

    def test_really_big_manifest_cross_format(self, really_big_manifest: Dict[str, Any]) -> None:
        """really_big_manifest produces same output from both formats."""
        # Old format
        old_paths: List[BaseManifestPath] = [
            ManifestPath(path=p["path"], hash=p["hash"], size=p["size"], mtime=p["mtime"])
            for p in really_big_manifest["paths"]
        ]
        old_manifest = AssetManifest(
            hash_alg=HashAlgorithm(really_big_manifest["hashAlg"]),
            paths=old_paths,
            total_size=really_big_manifest["totalSize"],
        )
        old_encoded = old_manifest.encode()

        # New format
        new_files = [
            ManifestFilePath(path=p["path"], hash=p["hash"], size=p["size"], mtime=p["mtime"])
            for p in really_big_manifest["paths"]
        ]
        new_manifest = RelSnapshotManifest(
            hash_alg=HashAlgorithm(really_big_manifest["hashAlg"]),
            files=new_files,
            total_size=really_big_manifest["totalSize"],
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )
        new_encoded = encode_v2023(new_manifest)

        assert old_encoded == new_encoded
