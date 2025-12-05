# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the v2023-03-03 version of the manifest file."""

import json

from deadline.job_attachments.asset_manifests.v2023_03_03.asset_manifest import (
    AssetManifest,
    ManifestPath,
)
from deadline.job_attachments.asset_manifests import HashAlgorithm


def test_encode():
    """
    Ensure the expected JSON string is returned from the encode function.
    """
    manifest = AssetManifest(
        hash_alg=HashAlgorithm("xxh128"),
        total_size=10,
        paths=[
            ManifestPath(path="test_file", hash="a", size=1, mtime=167907934333848),
            ManifestPath(path="test_dir/test_file", hash="b", size=1, mtime=1479079344833848),
            ManifestPath(path="another_test_file", hash="c", size=1, mtime=1675079344833848),
            ManifestPath(path="€", hash="EuroSign", size=1, mtime=1679079344836848),
            ManifestPath(path="\r", hash="CarriageReturn", size=1, mtime=1679079744833848),
            ManifestPath(
                path="דּ", hash="HebrewLetterDaletWithDagesh", size=1, mtime=1679039344833848
            ),
            ManifestPath(path="1", hash="One", size=1, mtime=1679079344833868),
            ManifestPath(path="😀", hash="EmojiGrinningFace", size=1, mtime=1679579344833848),
            ManifestPath(path="\u0080", hash="Control", size=1, mtime=1679079344833348),
            ManifestPath(
                path="ö", hash="LatinSmallLetterOWithDiaeresis", size=1, mtime=1679079344833848
            ),
        ],
    )

    expected = (
        "{"
        '"hashAlg":"xxh128",'
        '"manifestVersion":"2023-03-03",'
        '"paths":['
        r'{"hash":"CarriageReturn","mtime":1679079744833848,"path":"\r","size":1},'
        '{"hash":"One","mtime":1679079344833868,"path":"1","size":1},'
        '{"hash":"c","mtime":1675079344833848,"path":"another_test_file","size":1},'
        '{"hash":"b","mtime":1479079344833848,"path":"test_dir/test_file","size":1},'
        '{"hash":"a","mtime":167907934333848,"path":"test_file","size":1},'
        r'{"hash":"Control","mtime":1679079344833348,"path":"\u0080","size":1},'
        r'{"hash":"LatinSmallLetterOWithDiaeresis","mtime":1679079344833848,"path":"\u00f6","size":1},'
        r'{"hash":"EuroSign","mtime":1679079344836848,"path":"\u20ac","size":1},'
        r'{"hash":"EmojiGrinningFace","mtime":1679579344833848,"path":"\ud83d\ude00","size":1},'
        r'{"hash":"HebrewLetterDaletWithDagesh","mtime":1679039344833848,"path":"\ufb33","size":1}'
        "],"
        '"totalSize":10'
        "}"
    )

    a = manifest.encode()
    assert a == expected


def test_decode(default_manifest_str_v2023_03_03: str):
    """
    Ensure the expected AssetManifest is returned from the decode function.
    """
    expected = AssetManifest(
        hash_alg=HashAlgorithm("xxh128"),
        total_size=10,
        paths=[
            ManifestPath(path="\r", hash="CarriageReturn", size=1, mtime=1679079744833848),
            ManifestPath(path="1", hash="One", size=1, mtime=1679079344833868),
            ManifestPath(path="another_test_file", hash="c", size=1, mtime=1675079344833848),
            ManifestPath(path="test_dir/test_file", hash="b", size=1, mtime=1479079344833848),
            ManifestPath(path="test_file", hash="a", size=1, mtime=167907934333848),
            ManifestPath(path="\u0080", hash="Control", size=1, mtime=1679079344833348),
            ManifestPath(path="\u00c3\u00b1", hash="UserTestCase", size=1, mtime=1679579344833848),
            ManifestPath(
                path="ö", hash="LatinSmallLetterOWithDiaeresis", size=1, mtime=1679079344833848
            ),
            ManifestPath(path="€", hash="EuroSign", size=1, mtime=1679079344836848),
            ManifestPath(path="😀", hash="EmojiGrinningFace", size=1, mtime=1679579344833848),
            ManifestPath(path="\ude0a", hash="EmojiTestCase", size=1, mtime=1679579344833848),
            ManifestPath(
                path="דּ", hash="HebrewLetterDaletWithDagesh", size=1, mtime=1679039344833848
            ),
        ],
    )
    assert (
        AssetManifest.decode(manifest_data=json.loads(default_manifest_str_v2023_03_03)) == expected
    )


import pytest

from deadline.job_attachments.asset_manifests.base_manifest import BaseManifestPath
from deadline.job_attachments.exceptions import ManifestDecodeValidationError


class TestToDictValidation:
    """Tests for to_dict() validation of v2025_12_04-specific fields."""

    def test_to_dict_rejects_runnable_field(self) -> None:
        """to_dict() should reject paths with runnable=True."""
        # Create a path with runnable set via base class
        path = BaseManifestPath.__new__(ManifestPath)
        path.path = "script.sh"
        path.hash = "abc123"
        path.size = 100
        path.mtime = 1234567890
        path.runnable = True
        path.chunkhashes = None
        path.symlink_target = None
        path.deleted = False

        with pytest.raises(
            ManifestDecodeValidationError, match="does not support 'runnable' field"
        ):
            path.to_dict()

    def test_to_dict_rejects_chunkhashes_field(self) -> None:
        """to_dict() should reject paths with chunkhashes set."""
        path = BaseManifestPath.__new__(ManifestPath)
        path.path = "large.bin"
        path.hash = None
        path.size = 536870913
        path.mtime = 1234567890
        path.runnable = False
        path.chunkhashes = ["chunk1", "chunk2", "chunk3"]
        path.symlink_target = None
        path.deleted = False

        with pytest.raises(
            ManifestDecodeValidationError, match="does not support 'chunkhashes' field"
        ):
            path.to_dict()

    def test_to_dict_rejects_symlink_target_field(self) -> None:
        """to_dict() should reject paths with symlink_target set."""
        path = BaseManifestPath.__new__(ManifestPath)
        path.path = "link.txt"
        path.hash = None
        path.size = None
        path.mtime = None
        path.runnable = False
        path.chunkhashes = None
        path.symlink_target = "target.txt"
        path.deleted = False

        with pytest.raises(
            ManifestDecodeValidationError, match="does not support 'symlink_target' field"
        ):
            path.to_dict()

    def test_to_dict_rejects_deleted_field(self) -> None:
        """to_dict() should reject paths with deleted=True."""
        path = BaseManifestPath.__new__(ManifestPath)
        path.path = "deleted.txt"
        path.hash = None
        path.size = None
        path.mtime = None
        path.runnable = False
        path.chunkhashes = None
        path.symlink_target = None
        path.deleted = True

        with pytest.raises(
            ManifestDecodeValidationError, match="does not support 'deleted' field"
        ):
            path.to_dict()

    def test_to_dict_accepts_valid_path(self) -> None:
        """to_dict() should accept paths with only v2023-03-03 fields."""
        path = ManifestPath(path="file.txt", hash="abc123", size=100, mtime=1234567890)

        result = path.to_dict()

        assert result == {
            "hash": "abc123",
            "mtime": 1234567890,
            "path": "file.txt",
            "size": 100,
        }
