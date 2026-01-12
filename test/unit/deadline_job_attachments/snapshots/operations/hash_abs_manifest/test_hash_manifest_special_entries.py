# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_abs_manifest special entry handling.

These tests cover:
- Symlink entries (pass through unchanged)
- Deleted entries (pass through unchanged)
- Directory entries (pass through unchanged)
- Mixed entry types
"""

from pathlib import Path

from deadline.job_attachments._snapshots import (
    hash_abs_manifest,
    collect_abs_snapshot,
    SymlinkPolicy,
    AbsSnapshotDiff,
    AbsSnapshot,
    ManifestDirectoryPath,
    ManifestFilePath,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm


class TestSymlinkPassthrough:
    """Tests for symlink entry pass-through."""

    def test_symlinks_pass_through_unchanged(self, tmp_path: Path) -> None:
        """Symlinks are not hashed, just passed through."""
        target = tmp_path / "target.txt"
        target.write_text("target content")
        link = tmp_path / "link.txt"

        link.symlink_to("target.txt")

        collected = collect_abs_snapshot(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        hashed = hash_abs_manifest(collected)

        link_path = str(link).replace("\\", "/")
        target_path = str(target).replace("\\", "/")
        symlink_entry = next(p for p in hashed.files if p.path == link_path)
        assert symlink_entry.symlink_target is not None
        assert symlink_entry.hash is None

        target_entry = next(p for p in hashed.files if p.path == target_path)
        assert target_entry.hash is not None
        assert target_entry.symlink_target is None

    def test_symlink_entry_passed_through_unchanged(self, tmp_path: Path) -> None:
        """Symlink entries are passed through without hashing (direct manifest)."""
        target_file = tmp_path / "target.txt"
        target_file.write_text("target content")

        symlink_path = str(tmp_path / "link.txt").replace("\\", "/")
        target_path = str(target_file).replace("\\", "/")

        input_manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=symlink_path,
                    symlink_target=target_path,
                )
            ],
            total_size=0,
        )

        result = hash_abs_manifest(manifest=input_manifest)

        assert len(result.files) == 1
        assert result.files[0].path == symlink_path
        assert result.files[0].symlink_target == target_path
        assert result.files[0].hash is None
        assert result.files[0].size is None

    def test_symlink_with_callback_reports_no_hash(self, tmp_path: Path) -> None:
        """Symlink entries report 'no hash' via callback."""
        symlink_path = str(tmp_path / "link.txt").replace("\\", "/")
        target_path = str(tmp_path / "target.txt").replace("\\", "/")

        input_manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=symlink_path,
                    symlink_target=target_path,
                )
            ],
            total_size=0,
        )

        hashed = hash_abs_manifest(manifest=input_manifest)

        # Symlink should be passed through unchanged
        assert len(hashed.files) == 1
        assert hashed.files[0].symlink_target == target_path
        assert hashed.files[0].hash is None

    def test_diff_manifest_with_symlinks(self, tmp_path: Path) -> None:
        """Diff manifest with symlink entries passes them through unchanged."""
        diff_manifest = AbsSnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path="/some/new/link.txt",
                    symlink_target="/some/absolute/target.txt",
                )
            ],
            total_size=0,
        )

        hashed = hash_abs_manifest(diff_manifest)

        assert len(hashed.files) == 1
        assert hashed.files[0].symlink_target == "/some/absolute/target.txt"
        assert hashed.files[0].hash is None
        assert isinstance(hashed, AbsSnapshotDiff)


class TestDeletedEntryPassthrough:
    """Tests for deleted entry pass-through."""

    def test_deleted_entry_passed_through_unchanged(self, tmp_path: Path) -> None:
        """Deleted entries are passed through without hashing."""
        deleted_path = str(tmp_path / "deleted.txt").replace("\\", "/")

        input_manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=deleted_path,
                    deleted=True,
                )
            ],
            total_size=0,
        )

        result = hash_abs_manifest(manifest=input_manifest)

        assert len(result.files) == 1
        assert result.files[0].path == deleted_path
        assert result.files[0].deleted is True
        assert result.files[0].hash is None
        assert result.files[0].size is None

    def test_diff_manifest_preserves_deleted_entries(self, tmp_path: Path) -> None:
        """Diff manifest deleted entries are passed through unchanged."""
        diff_manifest = AbsSnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path="/some/deleted/file.txt",
                    deleted=True,
                )
            ],
            total_size=0,
            parent_manifest_hash="parent456",
        )

        hashed = hash_abs_manifest(diff_manifest)

        assert len(hashed.files) == 1
        assert hashed.files[0].deleted is True
        assert hashed.files[0].path == "/some/deleted/file.txt"
        assert isinstance(hashed, AbsSnapshotDiff)

    def test_diff_manifest_preserves_deleted_directories(self, tmp_path: Path) -> None:
        """Diff manifest deleted directory entries are passed through unchanged."""
        diff_manifest = AbsSnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(
                    path="/some/deleted/dir",
                    deleted=True,
                )
            ],
            files=[],
            total_size=0,
        )

        hashed = hash_abs_manifest(diff_manifest)

        assert len(hashed.dirs) == 1
        assert hashed.dirs[0].deleted is True
        assert hashed.dirs[0].path == "/some/deleted/dir"
        assert isinstance(hashed, AbsSnapshotDiff)


class TestDirectoryEntryHandling:
    """Tests for directory entry handling."""

    def test_directories_pass_through_unchanged(self, tmp_path: Path) -> None:
        """Directories are passed through unchanged."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        collected = collect_abs_snapshot(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        hashed = hash_abs_manifest(collected)

        assert len(hashed.dirs) >= 1
        subdir_path = str(subdir).replace("\\", "/")
        subdir_entries = [d for d in hashed.dirs if d.path == subdir_path]
        assert len(subdir_entries) == 1
        assert subdir_entries[0].deleted is False

    def test_directory_entries_copied_unchanged(self, tmp_path: Path) -> None:
        """Directory entries are copied to output unchanged."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")
        dir_path = str(tmp_path).replace("\\", "/")

        input_manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=7,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            dirs=[
                ManifestDirectoryPath(path=dir_path),
            ],
            total_size=7,
        )

        result = hash_abs_manifest(manifest=input_manifest)

        assert len(result.dirs) == 1
        assert result.dirs[0].path == dir_path
        assert result.dirs[0].deleted is False

    def test_deleted_directory_entries_preserved(self, tmp_path: Path) -> None:
        """Deleted directory entries are preserved in output."""
        dir_path = str(tmp_path / "deleted_dir").replace("\\", "/")

        input_manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            dirs=[
                ManifestDirectoryPath(path=dir_path, deleted=True),
            ],
            total_size=0,
        )

        result = hash_abs_manifest(manifest=input_manifest)

        assert len(result.dirs) == 1
        assert result.dirs[0].path == dir_path
        assert result.dirs[0].deleted is True


class TestMixedEntryTypes:
    """Tests for handling mixed entry types."""

    def test_mixed_entries_symlink_deleted_and_regular(self, tmp_path: Path) -> None:
        """Mix of symlink, deleted, and regular file entries are handled correctly."""
        regular_file = tmp_path / "regular.txt"
        regular_file.write_text("content")
        regular_path = str(regular_file).replace("\\", "/")

        symlink_path = str(tmp_path / "link.txt").replace("\\", "/")
        deleted_path = str(tmp_path / "deleted.txt").replace("\\", "/")

        input_manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=symlink_path,
                    symlink_target="/some/target",
                ),
                ManifestFilePath(
                    path=deleted_path,
                    deleted=True,
                ),
                ManifestFilePath(
                    path=regular_path,
                    hash=None,
                    size=7,
                    mtime=int(regular_file.stat().st_mtime_ns // 1000),
                ),
            ],
            total_size=7,
        )

        result = hash_abs_manifest(manifest=input_manifest)

        assert len(result.files) == 3

        symlink_entry = next(e for e in result.files if e.path == symlink_path)
        deleted_entry = next(e for e in result.files if e.path == deleted_path)
        regular_entry = next(e for e in result.files if e.path == regular_path)

        # Symlink unchanged
        assert symlink_entry.symlink_target == "/some/target"
        assert symlink_entry.hash is None

        # Deleted unchanged
        assert deleted_entry.deleted is True
        assert deleted_entry.hash is None

        # Regular file hashed
        assert regular_entry.hash is not None
        assert len(regular_entry.hash) == 32  # XXH128 hex
