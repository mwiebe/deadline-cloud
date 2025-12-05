# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for manifest encoding invariants.

These tests verify that the encode() function produces canonical output regardless
of the order or duplication of input data. Two manifest objects representing the
same logical snapshot or diff must produce identical encoded output.
"""

import pytest

from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.versions import ManifestType
from deadline.job_attachments.asset_manifests.v2025_12_04 import (
    AssetManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
)


class TestFileOrderInvariance:
    """Tests that file order does not affect encoded output."""

    def test_files_in_different_order_produce_identical_encoding(self) -> None:
        """If files are in different order in A and B, encoded versions must be identical."""
        files_order_1 = [
            ManifestFilePath(path="a.txt", hash="hash_a", size=100, mtime=1000),
            ManifestFilePath(path="b.txt", hash="hash_b", size=200, mtime=2000),
            ManifestFilePath(path="c.txt", hash="hash_c", size=300, mtime=3000),
        ]
        files_order_2 = [
            ManifestFilePath(path="c.txt", hash="hash_c", size=300, mtime=3000),
            ManifestFilePath(path="a.txt", hash="hash_a", size=100, mtime=1000),
            ManifestFilePath(path="b.txt", hash="hash_b", size=200, mtime=2000),
        ]

        manifest_a = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=files_order_1,
            total_size=600,
        )
        manifest_b = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=files_order_2,
            total_size=600,
        )

        assert manifest_a.encode() == manifest_b.encode()

    def test_files_in_subdirs_different_order_produce_identical_encoding(self) -> None:
        """Files in subdirectories in different order produce identical encoding."""
        dirs = [
            ManifestDirectoryPath(path="dir1"),
            ManifestDirectoryPath(path="dir2"),
        ]

        files_order_1 = [
            ManifestFilePath(path="dir1/a.txt", hash="hash_a", size=100, mtime=1000),
            ManifestFilePath(path="dir2/b.txt", hash="hash_b", size=200, mtime=2000),
            ManifestFilePath(path="dir1/c.txt", hash="hash_c", size=300, mtime=3000),
        ]
        files_order_2 = [
            ManifestFilePath(path="dir2/b.txt", hash="hash_b", size=200, mtime=2000),
            ManifestFilePath(path="dir1/c.txt", hash="hash_c", size=300, mtime=3000),
            ManifestFilePath(path="dir1/a.txt", hash="hash_a", size=100, mtime=1000),
        ]

        manifest_a = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dirs.copy(),
            paths=files_order_1,
            total_size=600,
        )
        manifest_b = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dirs.copy(),
            paths=files_order_2,
            total_size=600,
        )

        assert manifest_a.encode() == manifest_b.encode()


class TestDirectoryOrderInvariance:
    """Tests that directory order does not affect encoded output."""

    def test_dirs_in_different_order_produce_identical_encoding(self) -> None:
        """If dirs are in different order in A and B, encoded versions must be identical."""
        dirs_order_1 = [
            ManifestDirectoryPath(path="alpha"),
            ManifestDirectoryPath(path="beta"),
            ManifestDirectoryPath(path="gamma"),
        ]
        dirs_order_2 = [
            ManifestDirectoryPath(path="gamma"),
            ManifestDirectoryPath(path="alpha"),
            ManifestDirectoryPath(path="beta"),
        ]

        manifest_a = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dirs_order_1,
            paths=[],
            total_size=0,
        )
        manifest_b = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dirs_order_2,
            paths=[],
            total_size=0,
        )

        assert manifest_a.encode() == manifest_b.encode()

    def test_nested_dirs_different_order_produce_identical_encoding(self) -> None:
        """Nested directories in different order produce identical encoding."""
        dirs_order_1 = [
            ManifestDirectoryPath(path="parent"),
            ManifestDirectoryPath(path="parent/child1"),
            ManifestDirectoryPath(path="parent/child2"),
        ]
        dirs_order_2 = [
            ManifestDirectoryPath(path="parent/child2"),
            ManifestDirectoryPath(path="parent"),
            ManifestDirectoryPath(path="parent/child1"),
        ]

        manifest_a = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dirs_order_1,
            paths=[],
            total_size=0,
        )
        manifest_b = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dirs_order_2,
            paths=[],
            total_size=0,
        )

        assert manifest_a.encode() == manifest_b.encode()


class TestDuplicateFileInvariance:
    """Tests that duplicate file entries are deduplicated in encoded output."""

    def test_duplicate_files_produce_identical_encoding_to_single(self) -> None:
        """If paths has duplicated entries in A but not in B, encoded versions must be identical."""
        file_entry = ManifestFilePath(path="file.txt", hash="hash_a", size=100, mtime=1000)

        # A has duplicates
        manifest_a = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(path="file.txt", hash="hash_a", size=100, mtime=1000),
                ManifestFilePath(path="file.txt", hash="hash_a", size=100, mtime=1000),
                ManifestFilePath(path="file.txt", hash="hash_a", size=100, mtime=1000),
            ],
            total_size=100,
        )

        # B has single entry
        manifest_b = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(path="file.txt", hash="hash_a", size=100, mtime=1000),
            ],
            total_size=100,
        )

        assert manifest_a.encode() == manifest_b.encode()

    def test_duplicate_files_mixed_with_unique_produce_identical_encoding(self) -> None:
        """Duplicates mixed with unique files produce identical encoding."""
        manifest_a = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(path="a.txt", hash="hash_a", size=100, mtime=1000),
                ManifestFilePath(path="b.txt", hash="hash_b", size=200, mtime=2000),
                ManifestFilePath(path="a.txt", hash="hash_a", size=100, mtime=1000),  # duplicate
                ManifestFilePath(path="c.txt", hash="hash_c", size=300, mtime=3000),
                ManifestFilePath(path="b.txt", hash="hash_b", size=200, mtime=2000),  # duplicate
            ],
            total_size=600,
        )

        manifest_b = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(path="a.txt", hash="hash_a", size=100, mtime=1000),
                ManifestFilePath(path="b.txt", hash="hash_b", size=200, mtime=2000),
                ManifestFilePath(path="c.txt", hash="hash_c", size=300, mtime=3000),
            ],
            total_size=600,
        )

        assert manifest_a.encode() == manifest_b.encode()

    def test_duplicate_deleted_files_produce_identical_encoding(self) -> None:
        """Duplicate deleted file entries in diff manifest produce identical encoding."""
        manifest_a = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(path="deleted.txt", deleted=True),
                ManifestFilePath(path="deleted.txt", deleted=True),  # duplicate
                ManifestFilePath(path="new.txt", hash="hash_new", size=100, mtime=1000),
            ],
            total_size=100,
            manifest_type=ManifestType.DIFF,
            parent_manifest_hash="parent123",
        )

        manifest_b = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(path="deleted.txt", deleted=True),
                ManifestFilePath(path="new.txt", hash="hash_new", size=100, mtime=1000),
            ],
            total_size=100,
            manifest_type=ManifestType.DIFF,
            parent_manifest_hash="parent123",
        )

        assert manifest_a.encode() == manifest_b.encode()


class TestDuplicateDirectoryInvariance:
    """Tests that duplicate directory entries are deduplicated in encoded output."""

    def test_duplicate_dirs_produce_identical_encoding_to_single(self) -> None:
        """Duplicate directory entries produce identical encoding to single entry."""
        manifest_a = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="mydir"),
                ManifestDirectoryPath(path="mydir"),  # duplicate
                ManifestDirectoryPath(path="mydir"),  # duplicate
            ],
            paths=[],
            total_size=0,
        )

        manifest_b = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="mydir"),
            ],
            paths=[],
            total_size=0,
        )

        assert manifest_a.encode() == manifest_b.encode()

    def test_duplicate_deleted_dirs_produce_identical_encoding(self) -> None:
        """Duplicate deleted directory entries in diff manifest produce identical encoding."""
        manifest_a = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="deleted_dir", deleted=True),
                ManifestDirectoryPath(path="deleted_dir", deleted=True),  # duplicate
            ],
            paths=[],
            total_size=0,
            manifest_type=ManifestType.DIFF,
            parent_manifest_hash="parent123",
        )

        manifest_b = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="deleted_dir", deleted=True),
            ],
            paths=[],
            total_size=0,
            manifest_type=ManifestType.DIFF,
            parent_manifest_hash="parent123",
        )

        assert manifest_a.encode() == manifest_b.encode()


class TestCombinedInvariance:
    """Tests combining multiple invariance properties."""

    def test_different_order_and_duplicates_produce_identical_encoding(self) -> None:
        """Different order AND duplicates in both dirs and files produce identical encoding."""
        manifest_a = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="dir2"),
                ManifestDirectoryPath(path="dir1"),
                ManifestDirectoryPath(path="dir1"),  # duplicate
            ],
            paths=[
                ManifestFilePath(path="dir2/b.txt", hash="hash_b", size=200, mtime=2000),
                ManifestFilePath(path="dir1/a.txt", hash="hash_a", size=100, mtime=1000),
                ManifestFilePath(path="dir1/a.txt", hash="hash_a", size=100, mtime=1000),  # dup
            ],
            total_size=300,
        )

        manifest_b = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="dir1"),
                ManifestDirectoryPath(path="dir2"),
            ],
            paths=[
                ManifestFilePath(path="dir1/a.txt", hash="hash_a", size=100, mtime=1000),
                ManifestFilePath(path="dir2/b.txt", hash="hash_b", size=200, mtime=2000),
            ],
            total_size=300,
        )

        assert manifest_a.encode() == manifest_b.encode()


class TestDuplicateValidation:
    """Tests that duplicate entries with conflicting values raise errors."""

    def test_duplicate_files_with_different_hash_raises_error(self) -> None:
        """Duplicate files with different hash values must raise an error."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(path="file.txt", hash="hash_a", size=100, mtime=1000),
                ManifestFilePath(path="file.txt", hash="hash_b", size=100, mtime=1000),
            ],
            total_size=100,
        )

        with pytest.raises(Exception, match="conflicting 'hash' values"):
            manifest.encode()

    def test_duplicate_files_with_different_size_raises_error(self) -> None:
        """Duplicate files with different size values must raise an error."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(path="file.txt", hash="hash_a", size=100, mtime=1000),
                ManifestFilePath(path="file.txt", hash="hash_a", size=200, mtime=1000),
            ],
            total_size=100,
        )

        with pytest.raises(Exception, match="conflicting 'size' values"):
            manifest.encode()

    def test_duplicate_files_with_different_mtime_raises_error(self) -> None:
        """Duplicate files with different mtime values must raise an error."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(path="file.txt", hash="hash_a", size=100, mtime=1000),
                ManifestFilePath(path="file.txt", hash="hash_a", size=100, mtime=2000),
            ],
            total_size=100,
        )

        with pytest.raises(Exception, match="conflicting 'mtime' values"):
            manifest.encode()

    def test_duplicate_files_with_different_runnable_raises_error(self) -> None:
        """Duplicate files with different runnable values must raise an error."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(
                    path="script.sh", hash="hash_a", size=100, mtime=1000, runnable=True
                ),
                ManifestFilePath(
                    path="script.sh", hash="hash_a", size=100, mtime=1000, runnable=False
                ),
            ],
            total_size=100,
        )

        with pytest.raises(Exception, match="conflicting 'runnable' values"):
            manifest.encode()

    def test_duplicate_files_with_different_deleted_raises_error(self) -> None:
        """Duplicate files with different deleted values must raise an error."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(path="file.txt", deleted=True),
                ManifestFilePath(path="file.txt", deleted=False, hash="hash_a", size=100, mtime=1000),
            ],
            total_size=100,
            manifest_type=ManifestType.DIFF,
            parent_manifest_hash="parent123",
        )

        with pytest.raises(Exception, match="conflicting 'deleted' values"):
            manifest.encode()

    def test_duplicate_dirs_with_different_deleted_raises_error(self) -> None:
        """Duplicate directories with different deleted values must raise an error."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="mydir", deleted=False),
                ManifestDirectoryPath(path="mydir", deleted=True),
            ],
            paths=[],
            total_size=0,
            manifest_type=ManifestType.DIFF,
            parent_manifest_hash="parent123",
        )

        with pytest.raises(Exception, match="conflicting 'deleted' values"):
            manifest.encode()

    def test_duplicate_symlinks_with_different_target_raises_error(self) -> None:
        """Duplicate symlinks with different targets must raise an error."""
        manifest = AssetManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath(path="link.txt", symlink_target="target_a.txt"),
                ManifestFilePath(path="link.txt", symlink_target="target_b.txt"),
            ],
            total_size=0,
        )

        with pytest.raises(Exception, match="conflicting 'symlink_target' values"):
            manifest.encode()
