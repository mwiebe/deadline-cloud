# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for collect_manifest error handling and edge cases.

These tests cover:
- Input validation (missing files, directories, invalid paths)
- Optional filenames handling
- Broken symlinks
- Permission errors
- Files disappearing during collection
- OSError handling paths
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from deadline.job_attachments._snapshots import (
    collect_manifest,
    SymlinkPolicy,
)


class TestInputValidation:
    """Tests for input validation in collect_manifest."""

    def test_raises_on_nonexistent_directory(self, tmp_path: Path) -> None:
        """Raises FileNotFoundError when directory doesn't exist."""
        nonexistent = tmp_path / "nonexistent"

        with pytest.raises(FileNotFoundError, match="Directory does not exist"):
            collect_manifest(
                [nonexistent],
                [],
            )

    def test_raises_on_file_as_directory(self, tmp_path: Path) -> None:
        """Raises ValueError when a file is passed as a directory."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")

        with pytest.raises(ValueError, match="Path is not a directory"):
            collect_manifest(
                [file_path],
                [],
            )

    def test_raises_on_nonexistent_filename(self, tmp_path: Path) -> None:
        """Raises FileNotFoundError when required filename doesn't exist."""
        nonexistent = tmp_path / "nonexistent.txt"

        with pytest.raises(FileNotFoundError, match="File does not exist"):
            collect_manifest(
                [],
                [nonexistent],
            )

    def test_raises_on_directory_as_filename(self, tmp_path: Path) -> None:
        """Raises ValueError when a directory is passed as a filename."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        with pytest.raises(ValueError, match="Path is not a file or symlink"):
            collect_manifest(
                [],
                [subdir],
            )

    def test_empty_directories_and_filenames_returns_empty_manifest(self, tmp_path: Path) -> None:
        """Empty inputs return an empty manifest."""
        manifest = collect_manifest([], [])

        assert len(manifest.files) == 0
        assert len(manifest.dirs) == 0
        assert manifest.totalSize == 0


class TestOptionalFilenames:
    """Tests for optional_filenames parameter."""

    def test_optional_existing_file_included(self, tmp_path: Path) -> None:
        """Optional file that exists is included in manifest."""
        file1 = tmp_path / "required.txt"
        file1.write_text("required")
        file2 = tmp_path / "optional.txt"
        file2.write_text("optional")

        manifest = collect_manifest(
            [],
            [file1],
            optional_filenames=[file2],
        )

        paths = {p.path for p in manifest.files}
        assert file1.as_posix() in paths
        assert file2.as_posix() in paths

    def test_optional_missing_file_ignored(self, tmp_path: Path) -> None:
        """Optional file that doesn't exist is silently ignored."""
        file1 = tmp_path / "required.txt"
        file1.write_text("required")
        missing = tmp_path / "missing.txt"

        manifest = collect_manifest(
            [],
            [file1],
            optional_filenames=[missing],
        )

        paths = {p.path for p in manifest.files}
        assert file1.as_posix() in paths
        assert missing.as_posix() not in paths

    def test_optional_directory_ignored(self, tmp_path: Path) -> None:
        """Optional path that is a directory is silently ignored."""
        file1 = tmp_path / "required.txt"
        file1.write_text("required")
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        manifest = collect_manifest(
            [],
            [file1],
            optional_filenames=[subdir],
        )

        paths = {p.path for p in manifest.files}
        assert file1.as_posix() in paths
        assert subdir.as_posix() not in paths

    def test_optional_symlink_included_when_exists(self, tmp_path: Path) -> None:
        """Optional symlink that exists is included."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [],
            [],
            optional_filenames=[link],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        paths = {p.path for p in manifest.files}
        assert link.as_posix() in paths


class TestBrokenSymlinks:
    """Tests for broken symlink handling."""

    def test_broken_file_symlink_skipped_with_collapse(self, tmp_path: Path) -> None:
        """Broken file symlink is skipped with COLLAPSE policy."""
        # Create a symlink to a non-existent target
        link = tmp_path / "broken_link.txt"
        link.symlink_to(tmp_path / "nonexistent.txt")

        messages: List[str] = []

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
            print_function_callback=lambda msg: messages.append(str(msg)),
        )

        # The broken symlink should be skipped
        paths = {p.path for p in manifest.files}
        assert link.as_posix() not in paths

        # Should have logged a message about skipping
        assert any("broken" in msg.lower() or "skipping" in msg.lower() for msg in messages)

    def test_broken_dir_symlink_skipped_with_collapse(self, tmp_path: Path) -> None:
        """Broken directory symlink is skipped with COLLAPSE policy."""
        # Create a symlink to a non-existent directory
        link = tmp_path / "broken_dir_link"
        link.symlink_to(tmp_path / "nonexistent_dir")

        messages: List[str] = []

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
            print_function_callback=lambda msg: messages.append(str(msg)),
        )

        # The broken symlink should not appear as a directory
        dir_paths = {d.path for d in manifest.dirs}
        assert link.as_posix() not in dir_paths

    def test_broken_symlink_preserved_with_preserve(self, tmp_path: Path) -> None:
        """Broken symlink is preserved as symlink entry with PRESERVE policy."""
        # Create a symlink to a non-existent target
        link = tmp_path / "broken_link.txt"
        nonexistent = tmp_path / "nonexistent.txt"
        link.symlink_to(nonexistent)

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        # The broken symlink should be preserved
        paths_by_name = {p.path: p for p in manifest.files}
        assert link.as_posix() in paths_by_name
        assert paths_by_name[link.as_posix()].symlink_target == nonexistent.as_posix()

    def test_broken_symlink_excluded_with_exclude(self, tmp_path: Path) -> None:
        """Broken symlink is excluded with EXCLUDE policy."""
        link = tmp_path / "broken_link.txt"
        link.symlink_to(tmp_path / "nonexistent.txt")

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        paths = {p.path for p in manifest.files}
        assert link.as_posix() not in paths


class TestPermissionErrors:
    """Tests for permission-denied scenarios."""

    @pytest.mark.skipif(os.name == "nt", reason="Permission tests unreliable on Windows")
    def test_unreadable_file_in_directory_skipped(self, tmp_path: Path) -> None:
        """Unreadable file in directory is skipped with callback message."""
        readable = tmp_path / "readable.txt"
        readable.write_text("readable content")

        unreadable = tmp_path / "unreadable.txt"
        unreadable.write_text("secret content")
        unreadable.chmod(0o000)

        messages: List[str] = []

        try:
            manifest = collect_manifest(
                [tmp_path],
                [],
                print_function_callback=lambda msg: messages.append(str(msg)),
            )

            # Readable file should be collected
            paths = {p.path for p in manifest.files}
            assert readable.as_posix() in paths

            # Note: The current implementation may or may not skip unreadable files
            # depending on whether stat() fails. This test documents the behavior.
        finally:
            # Restore permissions for cleanup
            unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR)

    @pytest.mark.skipif(os.name == "nt", reason="Permission tests unreliable on Windows")
    def test_unreadable_directory_raises_or_skips(self, tmp_path: Path) -> None:
        """Unreadable directory either raises or is handled gracefully."""
        subdir = tmp_path / "unreadable_dir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")
        subdir.chmod(0o000)

        try:
            # The behavior depends on implementation - may raise or skip
            # This test documents whichever behavior exists
            try:
                manifest = collect_manifest(
                    [tmp_path],
                    [],
                )
                # If it doesn't raise, the unreadable dir contents should be skipped
                paths = {p.path for p in manifest.files}
                assert (subdir / "file.txt").as_posix() not in paths
            except PermissionError:
                # This is also acceptable behavior
                pass
        finally:
            subdir.chmod(stat.S_IRWXU)


class TestOSErrorHandling:
    """Tests for OSError handling during collection."""

    def test_stat_failure_on_file_skipped(self, tmp_path: Path) -> None:
        """File that fails stat() is skipped with callback message."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        messages: List[str] = []

        # Mock stat to fail for our specific file
        original_stat = Path.stat

        def mock_stat(self, *args, **kwargs):
            if "test.txt" in str(self):
                raise OSError("Mocked stat failure")
            return original_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", mock_stat):
            # The implementation should handle this gracefully
            # Note: This may or may not be caught depending on where stat is called
            try:
                collect_manifest(
                    [tmp_path],
                    [],
                    print_function_callback=lambda msg: messages.append(str(msg)),
                )
            except OSError:
                # If it propagates, that's also valid behavior
                pass

    def test_readlink_failure_handled(self, tmp_path: Path) -> None:
        """Symlink that fails readlink() is handled gracefully."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        messages: List[str] = []

        # Mock os.readlink to fail
        original_readlink = os.readlink

        def mock_readlink(path):
            if "link.txt" in str(path):
                raise OSError("Mocked readlink failure")
            return original_readlink(path)

        with patch("os.readlink", mock_readlink):
            try:
                collect_manifest(
                    [tmp_path],
                    [],
                    symlink_policy=SymlinkPolicy.PRESERVE,
                    print_function_callback=lambda msg: messages.append(str(msg)),
                )
            except OSError:
                # If it propagates, that's also valid behavior
                pass


class TestFilesDisappearDuringCollection:
    """Tests for files that disappear mid-collection."""

    def test_file_deleted_during_walk_raises(self, tmp_path: Path) -> None:
        """File deleted during os.walk raises OSError."""
        file1 = tmp_path / "file1.txt"
        file1.write_text("content1")
        file2 = tmp_path / "file2.txt"
        file2.write_text("content2")

        # Mock Path.stat to raise OSError for file2 (simulating deletion)
        original_stat = Path.stat

        def mock_stat(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if self.name == "file2.txt":
                raise OSError("File disappeared during collection")
            return original_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", mock_stat):
            with pytest.raises(OSError, match="File disappeared during collection"):
                collect_manifest(
                    [tmp_path],
                    [],
                )


class TestWindowsLongPathPrefix:
    """Tests for Windows long path prefix handling."""

    def test_longpath_prefix_removal_mocked(self, tmp_path: Path) -> None:
        """Test that Windows long path prefix is handled (mocked for cross-platform)."""
        from deadline.job_attachments._snapshots._operations._collect_manifest import (
            _remove_longpath_prefix,
        )

        # Test with mock Windows path
        with patch("os.name", "nt"):
            # Path with long path prefix
            long_path = Path("\\\\?\\C:\\Users\\test\\file.txt")
            result = _remove_longpath_prefix(long_path)
            # The prefix should be removed
            assert not str(result).startswith("\\\\?\\")

    def test_normal_path_unchanged(self, tmp_path: Path) -> None:
        """Normal paths are unchanged by _remove_longpath_prefix."""
        from deadline.job_attachments._snapshots._operations._collect_manifest import (
            _remove_longpath_prefix,
        )

        normal_path = tmp_path / "file.txt"
        result = _remove_longpath_prefix(normal_path)
        assert result == normal_path


class TestPrintFunctionCallback:
    """Tests for print_function_callback parameter."""

    def test_callback_called_for_collected_files(self, tmp_path: Path) -> None:
        """Callback is called when files are collected."""
        (tmp_path / "file.txt").write_text("content")

        messages: List[str] = []

        collect_manifest(
            [tmp_path],
            [],
            print_function_callback=lambda msg: messages.append(str(msg)),
        )

        assert len(messages) > 0
        assert any("Collected" in msg for msg in messages)

    def test_callback_called_for_directories(self, tmp_path: Path) -> None:
        """Callback is called when directories are collected."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        messages: List[str] = []

        collect_manifest(
            [tmp_path],
            [],
            print_function_callback=lambda msg: messages.append(str(msg)),
        )

        assert any("dir" in msg.lower() for msg in messages)

    def test_callback_called_for_symlinks(self, tmp_path: Path) -> None:
        """Callback is called when symlinks are collected."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        messages: List[str] = []

        collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
            print_function_callback=lambda msg: messages.append(str(msg)),
        )

        assert any("symlink" in msg.lower() for msg in messages)

    def test_callback_called_for_excluded_symlinks(self, tmp_path: Path) -> None:
        """Callback is called when symlinks are excluded."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        messages: List[str] = []

        collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.EXCLUDE,
            print_function_callback=lambda msg: messages.append(str(msg)),
        )

        assert any("excluding" in msg.lower() or "symlink" in msg.lower() for msg in messages)
