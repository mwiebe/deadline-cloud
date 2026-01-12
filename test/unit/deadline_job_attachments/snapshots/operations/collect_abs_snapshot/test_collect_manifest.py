# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for collect_abs_snapshot core functionality.

These tests cover:
- Absolute path generation for files and directories
- Metadata collection (size, mtime, hash, runnable)
- Directory handling (empty, nested, multiple)
- Basic file collection via filenames parameter
- File chunk size parameter

For symlink-related tests, see:
- test_collect_abs_snapshot_symlinks.py - Basic symlink policies
- test_collect_abs_snapshot_collapse_escaping.py - COLLAPSE_ESCAPING policy
- test_collect_abs_snapshot_transitive.py - TRANSITIVE_INCLUDE_TARGETS policy

For error handling tests, see:
- test_collect_abs_snapshot_errors.py

For deduplication tests, see:
- test_collect_abs_snapshot_deduplication.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deadline.job_attachments._snapshots import (
    collect_abs_snapshot,
    SymlinkPolicy,
)
from deadline.job_attachments._snapshots import (
    AbsSnapshot,
    DEFAULT_FILE_CHUNK_SIZE,
    WHOLE_FILE_CHUNK_SIZE,
)


class TestCollectManifestAbsolutePaths:
    """Tests for absolute path generation in collect_abs_snapshot."""

    def test_absolute_paths(self, tmp_path: Path) -> None:
        """When using collect_abs_snapshot, paths are absolute."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ALL,
        )

        # Path should be absolute (POSIX format)
        assert manifest.files[0].path == tmp_path.as_posix() + "/file.txt"

    def test_nested_absolute_paths(self, tmp_path: Path) -> None:
        """Nested files have full absolute paths."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ALL,
        )

        # Find the file entry (not directory)
        file_entries = [p for p in manifest.files if "nested.txt" in p.path]
        assert len(file_entries) == 1
        assert file_entries[0].path == (subdir / "nested.txt").as_posix()

    def test_absolute_paths_with_subdir(self, tmp_path: Path) -> None:
        """Paths include subdirectory structure."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        # Check file path is absolute
        file_entry = [p for p in manifest.files if "file.txt" in p.path][0]
        assert file_entry.path == (subdir / "file.txt").as_posix()

        # Check dir paths are absolute (includes root and subdir)
        dir_paths = {d.path for d in manifest.dirs}
        assert tmp_path.as_posix() in dir_paths
        assert subdir.as_posix() in dir_paths

    def test_produces_absolute_paths(self, tmp_path: Path) -> None:
        """collect_abs_snapshot produces absolute paths."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        file_entries = [p for p in manifest.files if "file.txt" in p.path]
        assert file_entries[0].path == (tmp_path / "file.txt").as_posix()

    def test_returns_abs_snapshot_manifest(self, tmp_path: Path) -> None:
        """collect_abs_snapshot returns AbsSnapshot type."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        assert isinstance(manifest, AbsSnapshot)


class TestCollectManifestMetadata:
    """Tests for metadata collection."""

    def test_file_size_captured(self, tmp_path: Path) -> None:
        """File size is captured correctly."""
        file_path = tmp_path / "file.txt"
        content = "Hello, World!"
        file_path.write_text(content)

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        file_entries = [p for p in manifest.files if "file.txt" in p.path]
        assert file_entries[0].size == len(content)

    def test_mtime_captured(self, tmp_path: Path) -> None:
        """File mtime is captured correctly."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")
        stat_info = file_path.stat()
        expected_mtime = stat_info.st_mtime_ns // 1000

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        file_entries = [p for p in manifest.files if "file.txt" in p.path]
        assert file_entries[0].mtime == expected_mtime

    def test_hash_is_none_for_unhashed(self, tmp_path: Path) -> None:
        """Hash is set to None for unhashed files (to be filled by hash_abs_manifest)."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        file_entries = [p for p in manifest.files if "file.txt" in p.path]
        assert file_entries[0].hash is None

    @pytest.mark.skipif(os.name == "nt", reason="Execute bit not meaningful on Windows")
    def test_runnable_flag_captured_true(self, tmp_path: Path) -> None:
        """Runnable flag is captured for executable files."""
        script = tmp_path / "script.sh"
        script.write_text("#!/bin/bash\necho hello")
        script.chmod(0o755)

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        file_entries = [p for p in manifest.files if "script.sh" in p.path]
        assert file_entries[0].runnable is True

    def test_runnable_flag_captured_false(self, tmp_path: Path) -> None:
        """Runnable flag is False for non-executable files."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")
        # On POSIX, explicitly set non-executable; on Windows this is a no-op
        # but the file will still have runnable=False
        if os.name != "nt":
            file_path.chmod(0o644)

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        file_entries = [p for p in manifest.files if "file.txt" in p.path]
        assert file_entries[0].runnable is False

    def test_total_size_calculated(self, tmp_path: Path) -> None:
        """Total size is sum of all file sizes."""
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file1.write_text("12345")  # 5 bytes
        file2.write_text("1234567890")  # 10 bytes

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        assert manifest.totalSize == 15

    def test_hash_algorithm_is_xxh128(self, tmp_path: Path) -> None:
        """Hash algorithm is set to XXH128."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm

        assert manifest.hashAlg == HashAlgorithm.XXH128


class TestCollectManifestDirectoryHandling:
    """Tests for directory handling in manifests."""

    def test_empty_directory_included(self, tmp_path: Path) -> None:
        """Empty directories are included in manifests."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        assert isinstance(manifest, AbsSnapshot)
        dir_paths = {d.path for d in manifest.dirs}
        assert empty_dir.as_posix() in dir_paths

    def test_nested_directories_included(self, tmp_path: Path) -> None:
        """Nested directories are all included."""
        level1 = tmp_path / "level1"
        level2 = level1 / "level2"
        level3 = level2 / "level3"
        level3.mkdir(parents=True)
        (level3 / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        assert isinstance(manifest, AbsSnapshot)
        dir_paths = {d.path for d in manifest.dirs}
        assert level1.as_posix() in dir_paths
        assert level2.as_posix() in dir_paths
        assert level3.as_posix() in dir_paths

    def test_multiple_directories_collected(self, tmp_path: Path) -> None:
        """Multiple directories can be collected into one manifest."""
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()
        (dir1 / "file1.txt").write_text("content1")
        (dir2 / "file2.txt").write_text("content2")

        manifest = collect_abs_snapshot(
            [dir1, dir2],
            [],
        )

        paths = {p.path for p in manifest.files}
        assert (dir1 / "file1.txt").as_posix() in paths
        assert (dir2 / "file2.txt").as_posix() in paths

    def test_root_directory_included_in_dirs(self, tmp_path: Path) -> None:
        """The root directory passed to collect is included in dirs."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [subdir],
            [],
        )

        dir_paths = {d.path for d in manifest.dirs}
        assert subdir.as_posix() in dir_paths


class TestCollectManifestFilenamesParameter:
    """Tests for the filenames parameter."""

    def test_collect_specific_files(self, tmp_path: Path) -> None:
        """Can collect specific files without walking directories."""
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file3 = tmp_path / "file3.txt"
        file1.write_text("content1")
        file2.write_text("content2")
        file3.write_text("content3")

        # Only collect file1 and file2
        manifest = collect_abs_snapshot(
            [],
            [file1, file2],
        )

        paths = {p.path for p in manifest.files}
        assert file1.as_posix() in paths
        assert file2.as_posix() in paths
        assert file3.as_posix() not in paths

    def test_combine_directories_and_filenames(self, tmp_path: Path) -> None:
        """Can combine directories and specific filenames."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "in_dir.txt").write_text("in dir")

        extra_file = tmp_path / "extra.txt"
        extra_file.write_text("extra")

        manifest = collect_abs_snapshot(
            [subdir],
            [extra_file],
        )

        paths = {p.path for p in manifest.files}
        assert (subdir / "in_dir.txt").as_posix() in paths
        assert extra_file.as_posix() in paths

    def test_filenames_only_no_directories(self, tmp_path: Path) -> None:
        """Can collect only specific files with no directory walk."""
        file1 = tmp_path / "file1.txt"
        file1.write_text("content1")

        manifest = collect_abs_snapshot(
            [],
            [file1],
        )

        assert len(manifest.files) == 1
        assert manifest.files[0].path == file1.as_posix()

        # No directories should be collected when only filenames provided
        assert len(manifest.dirs) == 0

    def test_string_paths_accepted(self, tmp_path: Path) -> None:
        """String paths are accepted in addition to Path objects."""
        file1 = tmp_path / "file1.txt"
        file1.write_text("content1")

        manifest = collect_abs_snapshot(
            [str(tmp_path)],
            [str(file1)],
        )

        paths = {p.path for p in manifest.files}
        assert file1.as_posix() in paths


class TestCollectManifestChunkSize:
    """Tests for file_chunk_size_bytes parameter."""

    def test_default_chunk_size(self, tmp_path: Path) -> None:
        """Default chunk size is DEFAULT_FILE_CHUNK_SIZE."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        assert manifest.fileChunkSizeBytes == DEFAULT_FILE_CHUNK_SIZE

    def test_custom_chunk_size(self, tmp_path: Path) -> None:
        """Custom chunk size is preserved in manifest."""
        (tmp_path / "file.txt").write_text("content")

        custom_size = 64 * 1024 * 1024  # 64MB
        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
            file_chunk_size_bytes=custom_size,
        )

        assert manifest.fileChunkSizeBytes == custom_size

    def test_whole_file_chunk_size(self, tmp_path: Path) -> None:
        """WHOLE_FILE_CHUNK_SIZE disables chunking."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
            file_chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE,
        )

        assert manifest.fileChunkSizeBytes == WHOLE_FILE_CHUNK_SIZE


class TestCollectManifestEmptyInputs:
    """Tests for empty input handling."""

    def test_empty_directory(self, tmp_path: Path) -> None:
        """Empty directory results in manifest with only directory entry."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        manifest = collect_abs_snapshot(
            [empty_dir],
            [],
        )

        assert len(manifest.files) == 0
        assert len(manifest.dirs) == 1
        assert manifest.dirs[0].path == empty_dir.as_posix()
        assert manifest.totalSize == 0

    def test_no_inputs(self) -> None:
        """No inputs results in empty manifest."""
        manifest = collect_abs_snapshot(
            [],
            [],
        )

        assert len(manifest.files) == 0
        assert len(manifest.dirs) == 0
        assert manifest.totalSize == 0


class TestCollectManifestSpecialFiles:
    """Tests for special file handling."""

    def test_hidden_files_collected(self, tmp_path: Path) -> None:
        """Hidden files (starting with .) are collected."""
        hidden = tmp_path / ".hidden"
        hidden.write_text("hidden content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        paths = {p.path for p in manifest.files}
        assert hidden.as_posix() in paths

    def test_hidden_directories_collected(self, tmp_path: Path) -> None:
        """Hidden directories are collected."""
        hidden_dir = tmp_path / ".hidden_dir"
        hidden_dir.mkdir()
        (hidden_dir / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        dir_paths = {d.path for d in manifest.dirs}
        paths = {p.path for p in manifest.files}

        assert hidden_dir.as_posix() in dir_paths
        assert (hidden_dir / "file.txt").as_posix() in paths

    def test_files_with_spaces_in_name(self, tmp_path: Path) -> None:
        """Files with spaces in names are collected correctly."""
        file_with_spaces = tmp_path / "file with spaces.txt"
        file_with_spaces.write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        paths = {p.path for p in manifest.files}
        assert file_with_spaces.as_posix() in paths

    def test_files_with_unicode_names(self, tmp_path: Path) -> None:
        """Files with unicode characters in names are collected."""
        unicode_file = tmp_path / "файл_文件_αρχείο.txt"
        unicode_file.write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        paths = {p.path for p in manifest.files}
        assert unicode_file.as_posix() in paths

    def test_empty_file(self, tmp_path: Path) -> None:
        """Empty files are collected with size 0."""
        empty_file = tmp_path / "empty.txt"
        empty_file.write_text("")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        file_entries = [p for p in manifest.files if "empty.txt" in p.path]
        assert len(file_entries) == 1
        assert file_entries[0].size == 0

    @pytest.mark.skipif(os.name == "nt", reason="Backslash is path separator on Windows")
    def test_backslash_in_filename_posix(self, tmp_path: Path) -> None:
        """Backslash in filename is preserved on POSIX (not a path separator)."""
        # On POSIX, backslash is a valid filename character
        file_with_backslash = tmp_path / "file\\with\\backslash.txt"
        file_with_backslash.write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        paths = {p.path for p in manifest.files}
        assert file_with_backslash.as_posix() in paths

    @pytest.mark.skipif(os.name == "nt", reason="Backslash is path separator on Windows")
    def test_backslash_in_directory_name_posix(self, tmp_path: Path) -> None:
        """Backslash in directory name is preserved on POSIX."""
        # On POSIX, backslash is a valid directory name character
        dir_with_backslash = tmp_path / "dir\\with\\backslash"
        dir_with_backslash.mkdir()
        (dir_with_backslash / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [tmp_path],
            [],
        )

        dir_paths = {d.path for d in manifest.dirs}
        paths = {p.path for p in manifest.files}

        assert dir_with_backslash.as_posix() in dir_paths
        assert (dir_with_backslash / "file.txt").as_posix() in paths
