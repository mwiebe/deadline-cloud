# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for _collect_manifest_structure and related functions.

These tests cover:
- Basic file collection for both v2023 and v2025 formats
- Directory collection (v2025 only)
- Empty directory handling (v2025 only)
- Symlink handling (v2025 only)
- Symlink validation (absolute paths, escaping root)
- POSIX execute bit capture (v2025 only)
- Version-specific behavior differences
"""

import os
import stat
import pytest
from pathlib import Path
from typing import List

from deadline.job_attachments.asset_manifests._operations._collect_manifest import (
    _collect_manifest_structure,
    _collect_manifest_structure_v2023,
    _collect_manifest_structure_v2025,
    _create_unhashed_file_entry,
    _create_symlink_entry,
)
from deadline.job_attachments.asset_manifests.versions import (
    ManifestType,
    ManifestVersion,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm


class TestCollectManifestStructureV2023:
    """Tests for v2023-03-03 manifest collection."""

    def test_collect_single_file(self, tmp_path: Path) -> None:
        """Collects a single file with correct metadata."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        manifest = _collect_manifest_structure_v2023(tmp_path)

        assert len(manifest.paths) == 1
        entry = manifest.paths[0]
        assert entry.path == "test.txt"
        assert entry.hash == ""  # Not computed yet
        assert entry.size == 11
        assert entry.mtime > 0

    def test_collect_multiple_files(self, tmp_path: Path) -> None:
        """Collects multiple files."""
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bbbbb")

        manifest = _collect_manifest_structure_v2023(tmp_path)

        assert len(manifest.paths) == 2
        paths = {p.path for p in manifest.paths}
        assert paths == {"a.txt", "b.txt"}

    def test_collect_nested_files(self, tmp_path: Path) -> None:
        """Collects files in nested directories."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("nested content")

        manifest = _collect_manifest_structure_v2023(tmp_path)

        assert len(manifest.paths) == 1
        assert manifest.paths[0].path == "subdir/nested.txt"

    def test_skip_symlinks(self, tmp_path: Path) -> None:
        """Symlinks are skipped in v2023 format."""
        target = tmp_path / "target.txt"
        target.write_text("target content")
        link = tmp_path / "link.txt"

        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        messages: List[str] = []
        manifest = _collect_manifest_structure_v2023(
            tmp_path, print_function_callback=messages.append
        )

        # Only the target file should be collected, not the symlink
        assert len(manifest.paths) == 1
        assert manifest.paths[0].path == "target.txt"
        assert any("Skipping symlink" in msg for msg in messages)

    def test_total_size_calculated(self, tmp_path: Path) -> None:
        """Total size is sum of all file sizes."""
        (tmp_path / "a.txt").write_text("aaa")  # 3 bytes
        (tmp_path / "b.txt").write_text("bbbbb")  # 5 bytes

        manifest = _collect_manifest_structure_v2023(tmp_path)

        assert manifest.totalSize == 8

    def test_empty_directory(self, tmp_path: Path) -> None:
        """Empty directory results in empty manifest."""
        manifest = _collect_manifest_structure_v2023(tmp_path)

        assert len(manifest.paths) == 0
        assert manifest.totalSize == 0

    def test_hash_algorithm_is_xxh128(self, tmp_path: Path) -> None:
        """Manifest uses XXH128 hash algorithm."""
        (tmp_path / "test.txt").write_text("test")

        manifest = _collect_manifest_structure_v2023(tmp_path)

        assert manifest.hashAlg == HashAlgorithm.XXH128

    def test_no_dirs_field(self, tmp_path: Path) -> None:
        """v2023 manifest has empty dirs list."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        manifest = _collect_manifest_structure_v2023(tmp_path)

        # v2023 doesn't track directories
        assert manifest.dirs == []


class TestCollectManifestStructureV2025:
    """Tests for v2025-12-04-beta manifest collection."""

    def test_collect_single_file(self, tmp_path: Path) -> None:
        """Collects a single file with correct metadata."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        manifest = _collect_manifest_structure_v2025(tmp_path)

        assert len(manifest.paths) == 1
        entry = manifest.paths[0]
        assert entry.path == "test.txt"
        assert entry.hash == ""  # Not computed yet
        assert entry.size == 11
        assert entry.mtime > 0
        assert entry.runnable is False

    def test_collect_directories(self, tmp_path: Path) -> None:
        """Directories are collected in v2025 format."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        manifest = _collect_manifest_structure_v2025(tmp_path)

        assert len(manifest.dirs) == 1
        assert manifest.dirs[0].path == "subdir"
        assert manifest.dirs[0].deleted is False

    def test_collect_nested_directories(self, tmp_path: Path) -> None:
        """Nested directories are all collected."""
        level1 = tmp_path / "level1"
        level2 = level1 / "level2"
        level3 = level2 / "level3"
        level3.mkdir(parents=True)
        (level3 / "deep.txt").write_text("deep content")

        manifest = _collect_manifest_structure_v2025(tmp_path)

        dir_paths = {d.path for d in manifest.dirs}
        assert dir_paths == {"level1", "level1/level2", "level1/level2/level3"}

    def test_collect_empty_directory(self, tmp_path: Path) -> None:
        """Empty directories are collected in v2025 format."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        manifest = _collect_manifest_structure_v2025(tmp_path)

        assert len(manifest.dirs) == 1
        assert manifest.dirs[0].path == "empty"
        assert len(manifest.paths) == 0

    def test_collect_multiple_empty_directories(self, tmp_path: Path) -> None:
        """Multiple empty directories are all collected."""
        (tmp_path / "empty1").mkdir()
        (tmp_path / "empty2").mkdir()
        (tmp_path / "empty3").mkdir()

        manifest = _collect_manifest_structure_v2025(tmp_path)

        dir_paths = {d.path for d in manifest.dirs}
        assert dir_paths == {"empty1", "empty2", "empty3"}

    def test_manifest_type_is_snapshot(self, tmp_path: Path) -> None:
        """Collected manifest is a SNAPSHOT type."""
        (tmp_path / "test.txt").write_text("test")

        manifest = _collect_manifest_structure_v2025(tmp_path)

        assert manifest.manifestType == ManifestType.SNAPSHOT

    def test_hash_algorithm_is_xxh128(self, tmp_path: Path) -> None:
        """Manifest uses XXH128 hash algorithm."""
        (tmp_path / "test.txt").write_text("test")

        manifest = _collect_manifest_structure_v2025(tmp_path)

        assert manifest.hashAlg == HashAlgorithm.XXH128


class TestCollectManifestStructureV2025Symlinks:
    """Tests for symlink handling in v2025-12-04-beta manifest collection."""

    def test_collect_file_symlink(self, tmp_path: Path) -> None:
        """File symlinks are collected with target path."""
        target = tmp_path / "target.txt"
        target.write_text("target content")
        link = tmp_path / "link.txt"

        try:
            link.symlink_to("target.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = _collect_manifest_structure_v2025(tmp_path)

        # Both target and symlink should be collected
        assert len(manifest.paths) == 2
        paths_by_name = {p.path: p for p in manifest.paths}

        assert "target.txt" in paths_by_name
        assert paths_by_name["target.txt"].hash == ""
        assert paths_by_name["target.txt"].symlink_target is None

        assert "link.txt" in paths_by_name
        assert paths_by_name["link.txt"].symlink_target == "target.txt"
        assert paths_by_name["link.txt"].hash is None

    def test_collect_directory_symlink(self, tmp_path: Path) -> None:
        """Directory symlinks are collected as symlink entries."""
        target_dir = tmp_path / "target_dir"
        target_dir.mkdir()
        (target_dir / "file.txt").write_text("content")
        link_dir = tmp_path / "link_dir"

        try:
            link_dir.symlink_to("target_dir", target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = _collect_manifest_structure_v2025(tmp_path)

        # The symlink should be in paths, not dirs
        paths_by_name = {p.path: p for p in manifest.paths}
        assert "link_dir" in paths_by_name
        assert paths_by_name["link_dir"].symlink_target == "target_dir"

        # The target directory should be in dirs
        dir_paths = {d.path for d in manifest.dirs}
        assert "target_dir" in dir_paths

    def test_symlink_to_nested_target(self, tmp_path: Path) -> None:
        """Symlink to file in subdirectory is collected correctly."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        target = subdir / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        try:
            link.symlink_to("subdir/target.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = _collect_manifest_structure_v2025(tmp_path)

        paths_by_name = {p.path: p for p in manifest.paths}
        assert "link.txt" in paths_by_name
        assert paths_by_name["link.txt"].symlink_target == "subdir/target.txt"

    def test_skip_absolute_symlink(self, tmp_path: Path) -> None:
        """Symlinks with absolute targets are skipped with warning."""
        link = tmp_path / "absolute_link.txt"

        try:
            link.symlink_to("/absolute/path/target.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        messages: List[str] = []
        manifest = _collect_manifest_structure_v2025(
            tmp_path, print_function_callback=messages.append
        )

        assert len(manifest.paths) == 0
        assert any("Skipping invalid symlink" in msg for msg in messages)

    def test_skip_escaping_symlink(self, tmp_path: Path) -> None:
        """Symlinks that escape the root are skipped with warning."""
        link = tmp_path / "escaping_link.txt"

        try:
            link.symlink_to("../outside.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        messages: List[str] = []
        manifest = _collect_manifest_structure_v2025(
            tmp_path, print_function_callback=messages.append
        )

        assert len(manifest.paths) == 0
        assert any("Skipping invalid symlink" in msg for msg in messages)

    def test_symlink_with_dot_dot_within_root(self, tmp_path: Path) -> None:
        """Symlink with .. that stays within root is valid."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = subdir / "link.txt"

        try:
            link.symlink_to("../target.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = _collect_manifest_structure_v2025(tmp_path)

        paths_by_name = {p.path: p for p in manifest.paths}
        assert "subdir/link.txt" in paths_by_name
        # The target is relative to the root path within the manifest
        assert paths_by_name["subdir/link.txt"].symlink_target == "target.txt"


class TestCollectManifestStructureV2025Runnable:
    """Tests for POSIX execute bit capture in v2025-12-04-beta manifest."""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions not available on Windows")
    def test_executable_file_captured(self, tmp_path: Path) -> None:
        """Files with execute bit are marked as runnable."""
        script = tmp_path / "script.sh"
        script.write_text("#!/bin/bash\necho hello")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        manifest = _collect_manifest_structure_v2025(tmp_path)

        assert len(manifest.paths) == 1
        assert manifest.paths[0].runnable is True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions not available on Windows")
    def test_non_executable_file_not_runnable(self, tmp_path: Path) -> None:
        """Files without execute bit are not marked as runnable."""
        regular = tmp_path / "regular.txt"
        regular.write_text("regular content")
        # Ensure no execute bits
        regular.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

        manifest = _collect_manifest_structure_v2025(tmp_path)

        assert len(manifest.paths) == 1
        assert manifest.paths[0].runnable is False

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions not available on Windows")
    def test_group_execute_bit_captured(self, tmp_path: Path) -> None:
        """Group execute bit also marks file as runnable."""
        script = tmp_path / "group_exec.sh"
        script.write_text("#!/bin/bash")
        script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXGRP)

        manifest = _collect_manifest_structure_v2025(tmp_path)

        assert manifest.paths[0].runnable is True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions not available on Windows")
    def test_other_execute_bit_captured(self, tmp_path: Path) -> None:
        """Other execute bit also marks file as runnable."""
        script = tmp_path / "other_exec.sh"
        script.write_text("#!/bin/bash")
        script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXOTH)

        manifest = _collect_manifest_structure_v2025(tmp_path)

        assert manifest.paths[0].runnable is True


class TestCollectManifestStructureDispatch:
    """Tests for the main _collect_manifest_structure dispatch function."""

    def test_dispatch_to_v2023(self, tmp_path: Path) -> None:
        """Version v2023-03-03 dispatches to v2023 implementation."""
        (tmp_path / "test.txt").write_text("test")

        manifest = _collect_manifest_structure(tmp_path, ManifestVersion.v2023_03_03)

        assert manifest.manifestVersion == ManifestVersion.v2023_03_03

    def test_dispatch_to_v2025(self, tmp_path: Path) -> None:
        """Version v2025-12-04-beta dispatches to v2025 implementation."""
        (tmp_path / "test.txt").write_text("test")

        manifest = _collect_manifest_structure(tmp_path, ManifestVersion.v2025_12_04_beta)

        assert manifest.manifestVersion == ManifestVersion.v2025_12_04_beta

    def test_unsupported_version_raises(self, tmp_path: Path) -> None:
        """Unsupported version raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported manifest version"):
            _collect_manifest_structure(tmp_path, ManifestVersion.UNDEFINED)


class TestCreateUnhashedFileEntry:
    """Tests for _create_unhashed_file_entry helper function."""

    def test_creates_entry_with_empty_hash(self, tmp_path: Path) -> None:
        """Entry is created with hash='' (empty string)."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        entry = _create_unhashed_file_entry(test_file, "test.txt")

        assert entry.hash == ""

    def test_captures_size(self, tmp_path: Path) -> None:
        """File size is captured correctly."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("12345")  # 5 bytes

        entry = _create_unhashed_file_entry(test_file, "test.txt")

        assert entry.size == 5

    def test_captures_mtime_in_microseconds(self, tmp_path: Path) -> None:
        """Modification time is captured in microseconds."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        entry = _create_unhashed_file_entry(test_file, "test.txt")

        # mtime should be a large number (microseconds since epoch)
        assert entry.mtime is not None
        assert entry.mtime > 1_000_000_000_000_000  # After year 2001 in microseconds

    def test_raises_on_inaccessible_file(self, tmp_path: Path) -> None:
        """OSError is raised for inaccessible files."""
        nonexistent = tmp_path / "nonexistent.txt"

        with pytest.raises(OSError):
            _create_unhashed_file_entry(nonexistent, "nonexistent.txt")


class TestCreateSymlinkEntry:
    """Tests for _create_symlink_entry helper function."""

    def test_creates_entry_with_target(self, tmp_path: Path) -> None:
        """Symlink entry has symlink_target set."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        try:
            link.symlink_to("target.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        entry = _create_symlink_entry(link, "link.txt", tmp_path)

        assert entry.symlink_target == "target.txt"
        assert entry.hash is None

    def test_rejects_escaping_target(self, tmp_path: Path) -> None:
        """Absolute symlink targets are rejected."""
        tmp_subdir = tmp_path / "subdir"
        tmp_subdir.mkdir()
        link = tmp_subdir / "link.txt"
        target = tmp_path / "target.txt"
        target.touch()

        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        with pytest.raises(ValueError, match="is not in the subpath of"):
            _create_symlink_entry(link, "subdir/link.txt", tmp_subdir)

    def test_accepts_absolute_within_root(self, tmp_path: Path) -> None:
        """Absolute symlink targets are rejected."""
        tmp_subdir = tmp_path / "subdir"
        tmp_subdir.mkdir()
        link = tmp_subdir / "link.txt"
        target = tmp_path / "target.txt"
        target.touch()

        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        _create_symlink_entry(link, "subdir/link.txt", tmp_path)

    def test_accepts_dot_dot_within_root(self, tmp_path: Path) -> None:
        """Symlink with .. that stays within root is accepted."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = subdir / "link.txt"

        try:
            link.symlink_to("../target.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        entry = _create_symlink_entry(link, "subdir/link.txt", tmp_path)

        assert entry.symlink_target == "target.txt"


class TestVersionDifferences:
    """Tests comparing behavior differences between v2023 and v2025."""

    def test_v2023_skips_symlinks_v2025_collects(self, tmp_path: Path) -> None:
        """v2023 skips symlinks while v2025 collects them."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        try:
            link.symlink_to("target.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest_v2023 = _collect_manifest_structure(tmp_path, ManifestVersion.v2023_03_03)
        manifest_v2025 = _collect_manifest_structure(tmp_path, ManifestVersion.v2025_12_04_beta)

        # v2023: only target file
        assert len(manifest_v2023.paths) == 1
        assert manifest_v2023.paths[0].path == "target.txt"

        # v2025: both target and symlink
        assert len(manifest_v2025.paths) == 2

    def test_v2023_no_dirs_v2025_has_dirs(self, tmp_path: Path) -> None:
        """v2023 doesn't track directories, v2025 does."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        manifest_v2023 = _collect_manifest_structure(tmp_path, ManifestVersion.v2023_03_03)
        manifest_v2025 = _collect_manifest_structure(tmp_path, ManifestVersion.v2025_12_04_beta)

        assert len(manifest_v2023.dirs) == 0
        assert len(manifest_v2025.dirs) == 1
        assert manifest_v2025.dirs[0].path == "subdir"

    @pytest.mark.skipif(os.name == "nt", reason="Windows doesn't support execute bit")
    def test_v2023_no_runnable_v2025_has_runnable(self, tmp_path: Path) -> None:
        """v2023 doesn't track runnable, v2025 does."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        test_file.chmod(0o700)

        manifest_v2023 = _collect_manifest_structure(tmp_path, ManifestVersion.v2023_03_03)
        manifest_v2025 = _collect_manifest_structure(tmp_path, ManifestVersion.v2025_12_04_beta)

        # v2023 ManifestPath doesn't track runnable
        assert manifest_v2023.paths[0].runnable is False
        # v2025 ManifestFilePath has runnable
        assert manifest_v2025.paths[0].runnable is True
