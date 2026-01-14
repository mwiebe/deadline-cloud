# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for manifest class methods.
"""

from deadline.job_attachments._snapshots import (
    ManifestFilePath,
    AbsSnapshot,
    AbsSnapshotDiff,
    Snapshot,
    SnapshotDiff,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm


class TestClearHashes:
    """Tests for the clear_hashes() method."""

    def test_clears_hash_from_regular_files(self) -> None:
        """clear_hashes() sets hash to None for regular files."""
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="/a/file1.txt", hash="abc123", size=100, mtime=1000),
                ManifestFilePath(path="/a/file2.txt", hash="def456", size=200, mtime=2000),
            ],
        )

        manifest.clear_hashes()

        assert manifest.files[0].hash is None
        assert manifest.files[1].hash is None

    def test_clears_chunkhashes_from_large_files(self) -> None:
        """clear_hashes() sets chunkhashes to None for large files."""
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path="/a/large.bin",
                    chunkhashes=["chunk1", "chunk2"],
                    size=512 * 1024 * 1024,
                    mtime=1000,
                ),
            ],
        )

        manifest.clear_hashes()

        assert manifest.files[0].chunkhashes is None

    def test_preserves_symlinks(self) -> None:
        """clear_hashes() does not modify symlink entries."""
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="/a/link", symlink_target="/a/target"),
            ],
        )

        manifest.clear_hashes()

        assert manifest.files[0].symlink_target == "/a/target"

    def test_preserves_deleted_entries(self) -> None:
        """clear_hashes() does not modify deleted entries."""
        manifest = AbsSnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="/a/deleted.txt", deleted=True),
            ],
        )

        manifest.clear_hashes()

        assert manifest.files[0].deleted is True

    def test_works_on_all_manifest_types(self) -> None:
        """clear_hashes() works on all four manifest types."""
        for manifest_cls, path in [
            (AbsSnapshot, "/a/file.txt"),
            (AbsSnapshotDiff, "/a/file.txt"),
            (Snapshot, "file.txt"),
            (SnapshotDiff, "file.txt"),
        ]:
            manifest = manifest_cls(
                hash_alg=HashAlgorithm.XXH128,
                files=[ManifestFilePath(path=path, hash="abc", size=100, mtime=1000)],
            )
            manifest.clear_hashes()
            assert manifest.files[0].hash is None

    def test_preserves_other_file_metadata(self) -> None:
        """clear_hashes() preserves size, mtime, runnable, and path."""
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path="/a/file.txt", hash="abc123", size=100, mtime=1000, runnable=True
                ),
            ],
        )

        manifest.clear_hashes()

        entry = manifest.files[0]
        assert entry.path == "/a/file.txt"
        assert entry.size == 100
        assert entry.mtime == 1000
        assert entry.runnable is True

    def test_mixed_entries(self) -> None:
        """clear_hashes() handles a mix of entry types correctly."""
        manifest = AbsSnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(path="/a/hashed.txt", hash="abc", size=100, mtime=1000),
                ManifestFilePath(
                    path="/a/chunked.bin",
                    chunkhashes=["c1", "c2"],
                    size=512 * 1024 * 1024,
                    mtime=2000,
                ),
                ManifestFilePath(path="/a/link", symlink_target="/a/target"),
                ManifestFilePath(path="/a/deleted.txt", deleted=True),
                ManifestFilePath(path="/a/unhashed.txt", size=50, mtime=3000),
            ],
        )

        manifest.clear_hashes()

        assert manifest.files[0].hash is None
        assert manifest.files[1].chunkhashes is None
        assert manifest.files[2].symlink_target == "/a/target"
        assert manifest.files[3].deleted is True
        assert manifest.files[4].hash is None
