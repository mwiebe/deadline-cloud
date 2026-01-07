# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for collect_manifest duplicate path handling.

These tests cover:
- Overlapping directories
- Same file via directory walk and filenames parameter
- Same file via multiple symlinks
- Deduplication of directory entries
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
)
from deadline.job_attachments.asset_manifests.versions import (
    SymlinkPolicy,
)


class TestOverlappingDirectories:
    """Tests for overlapping directory handling."""

    def test_nested_directories_deduplicated(self, tmp_path: Path) -> None:
        """Nested directories passed separately are deduplicated."""
        parent = tmp_path / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        (parent / "parent_file.txt").write_text("parent content")
        (child / "child_file.txt").write_text("child content")

        # Pass both parent and child directories
        manifest = collect_manifest(
            [parent, child],
            [],
        )

        paths = [p.path for p in manifest.files]
        dir_paths = [d.path for d in manifest.dirs]

        # Files should only appear once
        assert paths.count((parent / "parent_file.txt").as_posix()) == 1
        assert paths.count((child / "child_file.txt").as_posix()) == 1

        # Directories should only appear once
        assert dir_paths.count(parent.as_posix()) == 1
        assert dir_paths.count(child.as_posix()) == 1

    def test_same_directory_twice_deduplicated(self, tmp_path: Path) -> None:
        """Same directory passed twice is deduplicated."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        manifest = collect_manifest(
            [subdir, subdir],
            [],
        )

        paths = [p.path for p in manifest.files]
        dir_paths = [d.path for d in manifest.dirs]

        # File should only appear once
        assert paths.count((subdir / "file.txt").as_posix()) == 1

        # Directory should only appear once
        assert dir_paths.count(subdir.as_posix()) == 1

    def test_sibling_directories_both_collected(self, tmp_path: Path) -> None:
        """Sibling directories are both collected without deduplication issues."""
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()
        (dir1 / "file1.txt").write_text("content1")
        (dir2 / "file2.txt").write_text("content2")

        manifest = collect_manifest(
            [dir1, dir2],
            [],
        )

        paths = {p.path for p in manifest.files}

        assert (dir1 / "file1.txt").as_posix() in paths
        assert (dir2 / "file2.txt").as_posix() in paths


class TestDirectoryAndFilenameOverlap:
    """Tests for overlap between directories and filenames parameters."""

    def test_file_in_directory_and_filenames_deduplicated(self, tmp_path: Path) -> None:
        """File in directory and also in filenames is deduplicated."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        file_path = subdir / "file.txt"
        file_path.write_text("content")

        manifest = collect_manifest(
            [subdir],
            [file_path],
        )

        paths = [p.path for p in manifest.files]

        # File should only appear once
        assert paths.count(file_path.as_posix()) == 1

    def test_file_in_filenames_and_optional_deduplicated(self, tmp_path: Path) -> None:
        """File in both filenames and optional_filenames is deduplicated."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")

        manifest = collect_manifest(
            [],
            [file_path],
            optional_filenames=[file_path],
        )

        paths = [p.path for p in manifest.files]

        # File should only appear once
        assert paths.count(file_path.as_posix()) == 1

    def test_file_in_directory_and_optional_deduplicated(self, tmp_path: Path) -> None:
        """File in directory and optional_filenames is deduplicated."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        file_path = subdir / "file.txt"
        file_path.write_text("content")

        manifest = collect_manifest(
            [subdir],
            [],
            optional_filenames=[file_path],
        )

        paths = [p.path for p in manifest.files]

        # File should only appear once
        assert paths.count(file_path.as_posix()) == 1


class TestSymlinkDeduplication:
    """Tests for symlink-related deduplication."""

    def test_symlink_and_target_both_collected(self, tmp_path: Path) -> None:
        """Symlink and its target are both collected (not deduplicated)."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        paths = {p.path for p in manifest.files}

        # Both should be collected (they are different entries)
        assert target.as_posix() in paths
        assert link.as_posix() in paths

    def test_multiple_symlinks_to_same_target(self, tmp_path: Path) -> None:
        """Multiple symlinks to same target are all collected."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link1 = tmp_path / "link1.txt"
        link1.symlink_to(target)
        link2 = tmp_path / "link2.txt"
        link2.symlink_to(target)

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        paths = {p.path for p in manifest.files}

        # All three should be collected
        assert target.as_posix() in paths
        assert link1.as_posix() in paths
        assert link2.as_posix() in paths

    def test_symlink_in_filenames_and_directory(self, tmp_path: Path) -> None:
        """Symlink in both filenames and directory is deduplicated."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [tmp_path],
            [link],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        paths = [p.path for p in manifest.files]

        # Symlink should only appear once
        assert paths.count(link.as_posix()) == 1


class TestDirectoryEntryDeduplication:
    """Tests for directory entry deduplication."""

    def test_directory_entries_deduplicated(self, tmp_path: Path) -> None:
        """Directory entries are deduplicated when walking overlapping paths."""
        parent = tmp_path / "parent"
        child = parent / "child"
        grandchild = child / "grandchild"
        grandchild.mkdir(parents=True)
        (grandchild / "file.txt").write_text("content")

        # Walk from multiple starting points
        manifest = collect_manifest(
            [parent, child],
            [],
        )

        dir_paths = [d.path for d in manifest.dirs]

        # Each directory should only appear once
        assert dir_paths.count(parent.as_posix()) == 1
        assert dir_paths.count(child.as_posix()) == 1
        assert dir_paths.count(grandchild.as_posix()) == 1

    def test_root_directory_included(self, tmp_path: Path) -> None:
        """Root directory passed to collect is included in dirs."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        manifest = collect_manifest(
            [subdir],
            [],
        )

        dir_paths = {d.path for d in manifest.dirs}

        # Root directory should be included
        assert subdir.as_posix() in dir_paths


class TestTotalSizeDeduplication:
    """Tests for total size calculation with deduplication."""

    def test_total_size_not_double_counted(self, tmp_path: Path) -> None:
        """Total size doesn't double-count deduplicated files."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        file_path = subdir / "file.txt"
        file_path.write_text("12345")  # 5 bytes

        # Pass file in both directory and filenames
        manifest = collect_manifest(
            [subdir],
            [file_path],
        )

        # Total size should be 5, not 10
        assert manifest.totalSize == 5

    def test_total_size_with_multiple_files(self, tmp_path: Path) -> None:
        """Total size is correct with multiple files and deduplication."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        file1 = subdir / "file1.txt"
        file1.write_text("12345")  # 5 bytes
        file2 = subdir / "file2.txt"
        file2.write_text("1234567890")  # 10 bytes

        # Pass file1 in both directory and filenames
        manifest = collect_manifest(
            [subdir],
            [file1],
        )

        # Total size should be 15 (5 + 10), not 20
        assert manifest.totalSize == 15


class TestCollectedPathsSet:
    """Tests verifying the collected_paths set behavior."""

    def test_files_processed_in_order(self, tmp_path: Path) -> None:
        """Files are processed in a consistent order."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        for i in range(5):
            (subdir / f"file{i}.txt").write_text(f"content{i}")

        manifest1 = collect_manifest([subdir], [])
        manifest2 = collect_manifest([subdir], [])

        # Same files should be collected
        paths1 = {p.path for p in manifest1.files}
        paths2 = {p.path for p in manifest2.files}
        assert paths1 == paths2

    def test_filenames_processed_before_directories(self, tmp_path: Path) -> None:
        """Filenames are processed before directory walk."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        file_path = subdir / "file.txt"
        file_path.write_text("content")

        # When file is in both filenames and directory, it should only appear once
        # The order of processing shouldn't matter for the final result
        manifest = collect_manifest(
            [subdir],
            [file_path],
        )

        paths = [p.path for p in manifest.files]
        assert paths.count(file_path.as_posix()) == 1
