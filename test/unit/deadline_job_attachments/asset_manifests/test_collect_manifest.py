# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for collect_manifest function.

These tests cover:
- Basic file collection for both v2023 and v2025 formats
- Directory collection (v2025 only)
- Empty directory handling (v2025 only)
- Symlink handling (v2025 only)
- Symlink validation (absolute paths, escaping root)
- POSIX execute bit capture (v2025 only)
- Version-specific behavior differences

For tests of collect_abs_manifest, see test_collect_abs_manifest.py.
"""

import os
import stat
import pytest
from pathlib import Path
from typing import List

from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
)
from deadline.job_attachments.asset_manifests._operations._collect_manifest import (
    _create_unhashed_file_entry,
    _create_symlink_entry,
)
from deadline.job_attachments.asset_manifests.versions import (
    ManifestType,
    ManifestVersion,
    SymlinkPolicy,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm


class TestCollectManifestDirectoryTreeV2023:
    """Tests for v2023-03-03 manifest collection."""

    def test_collect_single_file(self, tmp_path: Path) -> None:
        """Collects a single file with correct metadata."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        manifest = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

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

        manifest = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        assert len(manifest.paths) == 2
        paths = {p.path for p in manifest.paths}
        assert paths == {"a.txt", "b.txt"}

    def test_collect_nested_files(self, tmp_path: Path) -> None:
        """Collects files in nested directories."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("nested content")

        manifest = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        assert len(manifest.paths) == 1
        assert manifest.paths[0].path == "subdir/nested.txt"

    def test_skip_symlinks(self, tmp_path: Path) -> None:
        """Symlinks to files are collected as files in COLLAPSE mode (v2023 format)."""
        target = tmp_path / "target.txt"
        target.write_text("target content")
        link = tmp_path / "link.txt"

        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        messages: List[str] = []
        manifest = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            print_function_callback=messages.append,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # Both the target file and the symlink (as a file) should be collected
        assert len(manifest.paths) == 2
        paths = {p.path for p in manifest.paths}
        assert paths == {"target.txt", "link.txt"}

    def test_total_size_calculated(self, tmp_path: Path) -> None:
        """Total size is sum of all file sizes."""
        (tmp_path / "a.txt").write_text("aaa")  # 3 bytes
        (tmp_path / "b.txt").write_text("bbbbb")  # 5 bytes

        manifest = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        assert manifest.totalSize == 8

    def test_empty_directory(self, tmp_path: Path) -> None:
        """Empty directory results in empty manifest."""
        manifest = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        assert len(manifest.paths) == 0
        assert manifest.totalSize == 0

    def test_hash_algorithm_is_xxh128(self, tmp_path: Path) -> None:
        """Manifest uses XXH128 hash algorithm."""
        (tmp_path / "test.txt").write_text("test")

        manifest = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        assert manifest.hashAlg == HashAlgorithm.XXH128

    def test_no_dirs_field(self, tmp_path: Path) -> None:
        """v2023 manifest has empty dirs list."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        manifest = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # v2023 doesn't track directories
        assert manifest.dirs == []


class TestCollectManifestDirectoryTreeV2025:
    """Tests for v2025-12-04-beta manifest collection."""

    def test_collect_single_file(self, tmp_path: Path) -> None:
        """Collects a single file with correct metadata."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

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

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

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

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        dir_paths = {d.path for d in manifest.dirs}
        assert dir_paths == {"level1", "level1/level2", "level1/level2/level3"}

    def test_collect_empty_directory(self, tmp_path: Path) -> None:
        """Empty directories are collected in v2025 format."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        assert len(manifest.dirs) == 1
        assert manifest.dirs[0].path == "empty"
        assert len(manifest.paths) == 0

    def test_collect_multiple_empty_directories(self, tmp_path: Path) -> None:
        """Multiple empty directories are all collected."""
        (tmp_path / "empty1").mkdir()
        (tmp_path / "empty2").mkdir()
        (tmp_path / "empty3").mkdir()

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        dir_paths = {d.path for d in manifest.dirs}
        assert dir_paths == {"empty1", "empty2", "empty3"}

    def test_manifest_type_is_snapshot(self, tmp_path: Path) -> None:
        """Collected manifest is a SNAPSHOT type."""
        (tmp_path / "test.txt").write_text("test")

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        assert manifest.manifestType == ManifestType.SNAPSHOT

    def test_hash_algorithm_is_xxh128(self, tmp_path: Path) -> None:
        """Manifest uses XXH128 hash algorithm."""
        (tmp_path / "test.txt").write_text("test")

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        assert manifest.hashAlg == HashAlgorithm.XXH128


class TestCollectManifestDirectoryTreeV2025Symlinks:
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

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

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

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

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

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        paths_by_name = {p.path: p for p in manifest.paths}
        assert "link.txt" in paths_by_name
        assert paths_by_name["link.txt"].symlink_target == "subdir/target.txt"

    def test_skip_absolute_symlink(self, tmp_path: Path) -> None:
        """Symlinks with absolute targets are excluded with EXCLUDE policy."""
        link = tmp_path / "absolute_link.txt"

        try:
            link.symlink_to("/absolute/path/target.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        messages: List[str] = []
        manifest = collect_manifest(
            version=ManifestVersion.v2025_12_04_beta,
            root=tmp_path,
            print_function_callback=messages.append,
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        assert len(manifest.paths) == 0
        assert any("Excluding symlink" in msg for msg in messages)

    def test_skip_escaping_symlink(self, tmp_path: Path) -> None:
        """Symlinks that escape the root are excluded with EXCLUDE policy."""
        link = tmp_path / "escaping_link.txt"

        try:
            link.symlink_to("../outside.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        messages: List[str] = []
        manifest = collect_manifest(
            version=ManifestVersion.v2025_12_04_beta,
            root=tmp_path,
            print_function_callback=messages.append,
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        assert len(manifest.paths) == 0
        assert any("Excluding symlink" in msg for msg in messages)

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

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        paths_by_name = {p.path: p for p in manifest.paths}
        assert "subdir/link.txt" in paths_by_name
        # The target is relative to the root path within the manifest
        assert paths_by_name["subdir/link.txt"].symlink_target == "target.txt"

    def test_symlink_chain_preserved(self, tmp_path: Path) -> None:
        """Symlink chain of length 3 is preserved (not collapsed by resolve())."""
        # Create: link3 -> link2 -> link1 -> target.txt
        target = tmp_path / "target.txt"
        target.write_text("content")
        link1 = tmp_path / "link1.txt"
        link2 = tmp_path / "link2.txt"
        link3 = tmp_path / "link3.txt"

        try:
            link1.symlink_to("target.txt")
            link2.symlink_to("link1.txt")
            link3.symlink_to("link2.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        # Test with relative paths (default)
        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        paths_by_name = {p.path: p for p in manifest.paths}
        # Each symlink should point to its immediate target, not the final target
        assert paths_by_name["link1.txt"].symlink_target == "target.txt"
        assert paths_by_name["link2.txt"].symlink_target == "link1.txt"
        assert paths_by_name["link3.txt"].symlink_target == "link2.txt"


class TestCollectManifestDirectoryTreeV2025Runnable:
    """Tests for POSIX execute bit capture in v2025-12-04-beta manifest."""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions not available on Windows")
    def test_executable_file_captured(self, tmp_path: Path) -> None:
        """Files with execute bit are marked as runnable."""
        script = tmp_path / "script.sh"
        script.write_text("#!/bin/bash\necho hello")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        assert len(manifest.paths) == 1
        assert manifest.paths[0].runnable is True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions not available on Windows")
    def test_non_executable_file_not_runnable(self, tmp_path: Path) -> None:
        """Files without execute bit are not marked as runnable."""
        regular = tmp_path / "regular.txt"
        regular.write_text("regular content")
        # Ensure no execute bits
        regular.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        assert len(manifest.paths) == 1
        assert manifest.paths[0].runnable is False

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions not available on Windows")
    def test_group_execute_bit_captured(self, tmp_path: Path) -> None:
        """Group execute bit also marks file as runnable."""
        script = tmp_path / "group_exec.sh"
        script.write_text("#!/bin/bash")
        script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXGRP)

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        assert manifest.paths[0].runnable is True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions not available on Windows")
    def test_other_execute_bit_captured(self, tmp_path: Path) -> None:
        """Other execute bit also marks file as runnable."""
        script = tmp_path / "other_exec.sh"
        script.write_text("#!/bin/bash")
        script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXOTH)

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        assert manifest.paths[0].runnable is True


class TestCollectManifestDirectoryTreeDispatch:
    """Tests for the main collect_manifest dispatch function."""

    def test_dispatch_to_v2023(self, tmp_path: Path) -> None:
        """Version v2023-03-03 dispatches to v2023 implementation."""
        (tmp_path / "test.txt").write_text("test")

        manifest = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        assert manifest.manifestVersion == ManifestVersion.v2023_03_03

    def test_dispatch_to_v2025(self, tmp_path: Path) -> None:
        """Version v2025-12-04-beta dispatches to v2025 implementation."""
        (tmp_path / "test.txt").write_text("test")

        manifest = collect_manifest(
            version=ManifestVersion.v2025_12_04_beta,
            root=tmp_path,
        )

        assert manifest.manifestVersion == ManifestVersion.v2025_12_04_beta

    def test_unsupported_version_raises(self, tmp_path: Path) -> None:
        """Unsupported version raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported manifest version"):
            collect_manifest(version=ManifestVersion.UNDEFINED, root=tmp_path)


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


class TestRelativePaths:
    """Tests for relative paths (default behavior)."""

    def test_v2023_relative_paths_by_default(self, tmp_path: Path) -> None:
        """By default, paths are relative to root."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        assert manifest.paths[0].path == "file.txt"

    def test_v2025_relative_paths_by_default(self, tmp_path: Path) -> None:
        """By default, paths are relative to root."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        # Check file path
        file_entry = [p for p in manifest.paths if p.path.endswith("file.txt")][0]
        assert file_entry.path == "subdir/file.txt"
        # Check dir path
        assert manifest.dirs[0].path == "subdir"


class TestVersionDifferences:
    """Tests comparing behavior differences between v2023 and v2025."""

    def test_v2023_skips_symlinks_v2025_collects(self, tmp_path: Path) -> None:
        """v2023 COLLAPSE collects symlinks as files, v2025 collects them as symlink entries."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        try:
            link.symlink_to("target.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest_v2023 = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        manifest_v2025 = collect_manifest(
            version=ManifestVersion.v2025_12_04_beta,
            root=tmp_path,
        )

        # v2023 COLLAPSE: both target and symlink collected as files
        assert len(manifest_v2023.paths) == 2
        v2023_paths = {p.path for p in manifest_v2023.paths}
        assert v2023_paths == {"target.txt", "link.txt"}
        # v2023 doesn't have symlink_target field populated (all are regular files)
        for p in manifest_v2023.paths:
            assert p.symlink_target is None

        # v2025: both target and symlink, but symlink has symlink_target set
        assert len(manifest_v2025.paths) == 2
        v2025_link = next(p for p in manifest_v2025.paths if p.path == "link.txt")
        assert v2025_link.symlink_target == "target.txt"

    def test_v2023_no_dirs_v2025_has_dirs(self, tmp_path: Path) -> None:
        """v2023 doesn't track directories, v2025 does."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        manifest_v2023 = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        manifest_v2025 = collect_manifest(
            version=ManifestVersion.v2025_12_04_beta,
            root=tmp_path,
        )

        assert len(manifest_v2023.dirs) == 0
        assert len(manifest_v2025.dirs) == 1
        assert manifest_v2025.dirs[0].path == "subdir"

    @pytest.mark.skipif(os.name == "nt", reason="Windows doesn't support execute bit")
    def test_v2023_no_runnable_v2025_has_runnable(self, tmp_path: Path) -> None:
        """v2023 doesn't track runnable, v2025 does."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        test_file.chmod(0o700)

        manifest_v2023 = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        manifest_v2025 = collect_manifest(
            version=ManifestVersion.v2025_12_04_beta,
            root=tmp_path,
        )

        # v2023 ManifestPath doesn't track runnable
        assert manifest_v2023.paths[0].runnable is False
        # v2025 ManifestFilePath has runnable
        assert manifest_v2025.paths[0].runnable is True


class TestSymlinkPolicyV2023:
    """Tests for symlink_policy parameter with v2023 format."""

    def test_v2023_collapse_follows_symlinks(self, tmp_path: Path) -> None:
        """COLLAPSE policy follows directory symlinks during directory walk.

        When a symlink points to a directory, os.walk with followlinks=True
        will traverse into that directory, collecting files within it.
        """
        target_dir = tmp_path / "target_dir"
        target_dir.mkdir()
        (target_dir / "file.txt").write_text("content")
        link_dir = tmp_path / "link_dir"

        try:
            link_dir.symlink_to("target_dir", target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # Both the original file and the file via the directory symlink should be collected
        paths = {p.path for p in manifest.paths}
        assert "target_dir/file.txt" in paths
        assert "link_dir/file.txt" in paths
        # Total of 2 file entries (same content, different paths)
        assert len(manifest.paths) == 2

    def test_v2023_exclude_skips_symlinks(self, tmp_path: Path) -> None:
        """EXCLUDE policy skips symlinks entirely."""
        target = tmp_path / "target.txt"
        target.write_text("target content")
        link = tmp_path / "link.txt"

        try:
            link.symlink_to("target.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        messages: List[str] = []
        manifest = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            print_function_callback=messages.append,
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        # Only the target file should be collected
        assert len(manifest.paths) == 1
        assert manifest.paths[0].path == "target.txt"
        assert any("Excluding symlink" in msg for msg in messages)

    def test_v2023_exclude_skips_directory_symlinks(self, tmp_path: Path) -> None:
        """EXCLUDE policy skips directory symlinks."""
        target_dir = tmp_path / "target_dir"
        target_dir.mkdir()
        (target_dir / "file.txt").write_text("content")
        link_dir = tmp_path / "link_dir"

        try:
            link_dir.symlink_to("target_dir", target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = collect_manifest(
            version=ManifestVersion.v2023_03_03,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        # Only the original file should be collected, not via symlink
        paths = {p.path for p in manifest.paths}
        assert paths == {"target_dir/file.txt"}

    def test_v2023_rejects_collapse_escaping(self, tmp_path: Path) -> None:
        """v2023 rejects COLLAPSE_ESCAPING policy."""
        with pytest.raises(ValueError, match="only supports symlink_policy COLLAPSE or EXCLUDE"):
            collect_manifest(
                version=ManifestVersion.v2023_03_03,
                root=tmp_path,
                symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
            )

    def test_v2023_rejects_preserve(self, tmp_path: Path) -> None:
        """v2023 rejects PRESERVE policy."""
        # PRESERVE requires absolute paths, so we get that error first
        with pytest.raises(ValueError, match="Use collect_abs_manifest\\(\\) instead"):
            collect_manifest(
                version=ManifestVersion.v2023_03_03,
                root=tmp_path,
                symlink_policy=SymlinkPolicy.PRESERVE,
            )

    def test_v2023_rejects_transitive_include_targets(self, tmp_path: Path) -> None:
        """v2023 rejects TRANSITIVE_INCLUDE_TARGETS policy."""
        # TRANSITIVE_INCLUDE_TARGETS requires absolute paths, so we get that error first
        with pytest.raises(ValueError, match="Use collect_abs_manifest\\(\\) instead"):
            collect_manifest(
                version=ManifestVersion.v2023_03_03,
                root=tmp_path,
                symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
            )


class TestSymlinkPolicyV2025:
    """Tests for symlink_policy parameter with v2025 format."""

    def test_v2025_collapse_follows_all_symlinks(self, tmp_path: Path) -> None:
        """COLLAPSE policy follows all symlinks during directory walk."""
        target_dir = tmp_path / "target_dir"
        target_dir.mkdir()
        (target_dir / "file.txt").write_text("content")
        link_dir = tmp_path / "link_dir"

        try:
            link_dir.symlink_to("target_dir", target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = collect_manifest(
            version=ManifestVersion.v2025_12_04_beta,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # Both paths should be collected as files (symlink followed)
        file_paths = {p.path for p in manifest.paths}
        assert "target_dir/file.txt" in file_paths
        assert "link_dir/file.txt" in file_paths

        # No symlink entries (all collapsed)
        symlinks = [p for p in manifest.paths if p.symlink_target is not None]
        assert len(symlinks) == 0

    def test_v2025_collapse_escaping_preserves_internal_symlinks(self, tmp_path: Path) -> None:
        """COLLAPSE_ESCAPING preserves symlinks within root."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        try:
            link.symlink_to("target.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = collect_manifest(
            version=ManifestVersion.v2025_12_04_beta,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.paths}
        # Target is a regular file
        assert paths_by_name["target.txt"].symlink_target is None
        # Link is preserved as symlink
        assert paths_by_name["link.txt"].symlink_target == "target.txt"

    def test_v2025_exclude_skips_all_symlinks(self, tmp_path: Path) -> None:
        """EXCLUDE policy skips all symlinks."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        try:
            link.symlink_to("target.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        messages: List[str] = []
        manifest = collect_manifest(
            version=ManifestVersion.v2025_12_04_beta,
            root=tmp_path,
            print_function_callback=messages.append,
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        # Only target file collected
        assert len(manifest.paths) == 1
        assert manifest.paths[0].path == "target.txt"
        assert any("Excluding symlink" in msg for msg in messages)

    def test_v2025_preserve_requires_absolute_paths(self, tmp_path: Path) -> None:
        """PRESERVE policy requires collect_abs_manifest."""
        with pytest.raises(ValueError, match="Use collect_abs_manifest\\(\\) instead"):
            collect_manifest(
                version=ManifestVersion.v2025_12_04_beta,
                root=tmp_path,
                symlink_policy=SymlinkPolicy.PRESERVE,
            )

    def test_v2025_transitive_requires_absolute_paths(self, tmp_path: Path) -> None:
        """TRANSITIVE_INCLUDE_TARGETS policy requires collect_abs_manifest."""
        with pytest.raises(ValueError, match="Use collect_abs_manifest\\(\\) instead"):
            collect_manifest(
                version=ManifestVersion.v2025_12_04_beta,
                root=tmp_path,
                symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
            )

    def test_v2025_default_is_collapse_escaping(self, tmp_path: Path) -> None:
        """Default symlink_policy is COLLAPSE_ESCAPING."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        try:
            link.symlink_to("target.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        # Call without specifying symlink_policy
        manifest = collect_manifest(version=ManifestVersion.v2025_12_04_beta, root=tmp_path)

        # Internal symlink should be preserved (COLLAPSE_ESCAPING behavior)
        paths_by_name = {p.path: p for p in manifest.paths}
        assert paths_by_name["link.txt"].symlink_target == "target.txt"

    @pytest.mark.parametrize(
        "version,symlink_policy",
        [
            (ManifestVersion.v2023_03_03, SymlinkPolicy.COLLAPSE),
            (ManifestVersion.v2025_12_04_beta, SymlinkPolicy.COLLAPSE),
            (ManifestVersion.v2025_12_04_beta, SymlinkPolicy.COLLAPSE_ESCAPING),
        ],
        ids=["v2023-COLLAPSE", "v2025-COLLAPSE", "v2025-COLLAPSE_ESCAPING"],
    )
    def test_collapse_follows_escaping_dir_symlink_with_nested_content(
        self, tmp_path: Path, version: ManifestVersion, symlink_policy: SymlinkPolicy
    ) -> None:
        """COLLAPSE and COLLAPSE_ESCAPING follow escaping directory symlinks and collect nested content.

        Structure:
            tmp_path/
                root/           <- manifest root
                    link_dir/   <- symlink to ../outside_dir (escaping)
                outside_dir/    <- outside the root
                    subdir/
                        file.txt

        Expected: link_dir/subdir/file.txt should be collected as a regular file.
        """
        # Create structure outside the root
        outside_dir = tmp_path / "outside_dir"
        subdir = outside_dir / "subdir"
        subdir.mkdir(parents=True)
        (subdir / "file.txt").write_text("nested content")

        # Create the manifest root with a symlink escaping to outside_dir
        root = tmp_path / "root"
        root.mkdir()
        link_dir = root / "link_dir"

        try:
            link_dir.symlink_to("../outside_dir", target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = collect_manifest(
            version=version,
            root=root,
            symlink_policy=symlink_policy,
        )

        # The escaping symlink should be followed, and nested content collected
        file_paths = {p.path for p in manifest.paths}
        assert "link_dir/subdir/file.txt" in file_paths

        # The file should be collected as a regular file (not a symlink entry)
        file_entry = next(p for p in manifest.paths if p.path == "link_dir/subdir/file.txt")
        assert file_entry.symlink_target is None
        assert file_entry.hash == ""  # Unhashed, ready for _hash_manifest
        assert file_entry.size == len("nested content")

        # v2025 collects directories, v2023 does not
        if version == ManifestVersion.v2025_12_04_beta:
            dir_paths = {d.path for d in manifest.dirs}
            assert "link_dir" in dir_paths or "link_dir/subdir" in dir_paths


class TestSymlinkChains:
    """Tests for symlink chain handling across all symlink policies.

    Tests cover:
    - symlink -> symlink -> file (all within root)
    - symlink -> symlink -> dir (all within root)
    - symlink chain where intermediate link escapes root
    - symlink chain where final target escapes root
    """

    # ==================== File symlink chains (all within root) ====================

    @pytest.mark.parametrize(
        "version,symlink_policy,expect_chain_preserved",
        [
            # COLLAPSE follows all symlinks, so chain is collapsed to files
            (ManifestVersion.v2023_03_03, SymlinkPolicy.COLLAPSE, False),
            (ManifestVersion.v2025_12_04_beta, SymlinkPolicy.COLLAPSE, False),
            # COLLAPSE_ESCAPING preserves internal symlinks
            (ManifestVersion.v2025_12_04_beta, SymlinkPolicy.COLLAPSE_ESCAPING, True),
            # EXCLUDE skips all symlinks
            (ManifestVersion.v2023_03_03, SymlinkPolicy.EXCLUDE, None),  # None = symlinks excluded
            (ManifestVersion.v2025_12_04_beta, SymlinkPolicy.EXCLUDE, None),
        ],
        ids=[
            "v2023-COLLAPSE",
            "v2025-COLLAPSE",
            "v2025-COLLAPSE_ESCAPING",
            "v2023-EXCLUDE",
            "v2025-EXCLUDE",
        ],
    )
    def test_file_symlink_chain_within_root(
        self,
        tmp_path: Path,
        version: ManifestVersion,
        symlink_policy: SymlinkPolicy,
        expect_chain_preserved: bool | None,
    ) -> None:
        """Test symlink -> symlink -> file chain where all are within root.

        Structure:
            root/
                target.txt      <- actual file
                link1.txt       <- symlink to target.txt
                link2.txt       <- symlink to link1.txt
        """
        target = tmp_path / "target.txt"
        target.write_text("content")
        link1 = tmp_path / "link1.txt"
        link2 = tmp_path / "link2.txt"

        try:
            link1.symlink_to("target.txt")
            link2.symlink_to("link1.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = collect_manifest(
            version=version,
            root=tmp_path,
            symlink_policy=symlink_policy,
        )

        paths_by_name = {p.path: p for p in manifest.paths}

        if expect_chain_preserved is None:
            # EXCLUDE: only target file collected
            assert "target.txt" in paths_by_name
            assert "link1.txt" not in paths_by_name
            assert "link2.txt" not in paths_by_name
        elif expect_chain_preserved:
            # COLLAPSE_ESCAPING: symlinks preserved with their immediate targets
            assert "target.txt" in paths_by_name
            assert paths_by_name["target.txt"].symlink_target is None
            assert "link1.txt" in paths_by_name
            assert paths_by_name["link1.txt"].symlink_target == "target.txt"
            assert "link2.txt" in paths_by_name
            assert paths_by_name["link2.txt"].symlink_target == "link1.txt"
        else:
            # COLLAPSE: all collected as files (symlinks followed)
            assert "target.txt" in paths_by_name
            assert paths_by_name["target.txt"].symlink_target is None
            assert "link1.txt" in paths_by_name
            assert paths_by_name["link1.txt"].symlink_target is None
            assert "link2.txt" in paths_by_name
            assert paths_by_name["link2.txt"].symlink_target is None

    # ==================== Directory symlink chains (all within root) ====================

    @pytest.mark.parametrize(
        "version,symlink_policy,expect_chain_preserved",
        [
            # COLLAPSE follows all symlinks
            (ManifestVersion.v2023_03_03, SymlinkPolicy.COLLAPSE, False),
            (ManifestVersion.v2025_12_04_beta, SymlinkPolicy.COLLAPSE, False),
            # COLLAPSE_ESCAPING preserves internal symlinks
            (ManifestVersion.v2025_12_04_beta, SymlinkPolicy.COLLAPSE_ESCAPING, True),
            # EXCLUDE skips all symlinks
            (ManifestVersion.v2023_03_03, SymlinkPolicy.EXCLUDE, None),
            (ManifestVersion.v2025_12_04_beta, SymlinkPolicy.EXCLUDE, None),
        ],
        ids=[
            "v2023-COLLAPSE",
            "v2025-COLLAPSE",
            "v2025-COLLAPSE_ESCAPING",
            "v2023-EXCLUDE",
            "v2025-EXCLUDE",
        ],
    )
    def test_dir_symlink_chain_within_root(
        self,
        tmp_path: Path,
        version: ManifestVersion,
        symlink_policy: SymlinkPolicy,
        expect_chain_preserved: bool | None,
    ) -> None:
        """Test symlink -> symlink -> dir chain where all are within root.

        Structure:
            root/
                target_dir/     <- actual directory
                    file.txt
                link1_dir/      <- symlink to target_dir
                link2_dir/      <- symlink to link1_dir
        """
        target_dir = tmp_path / "target_dir"
        target_dir.mkdir()
        (target_dir / "file.txt").write_text("content")
        link1_dir = tmp_path / "link1_dir"
        link2_dir = tmp_path / "link2_dir"

        try:
            link1_dir.symlink_to("target_dir", target_is_directory=True)
            link2_dir.symlink_to("link1_dir", target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = collect_manifest(
            version=version,
            root=tmp_path,
            symlink_policy=symlink_policy,
        )

        file_paths = {p.path for p in manifest.paths}

        if expect_chain_preserved is None:
            # EXCLUDE: only files in target_dir collected
            assert "target_dir/file.txt" in file_paths
            assert "link1_dir/file.txt" not in file_paths
            assert "link2_dir/file.txt" not in file_paths
        elif expect_chain_preserved:
            # COLLAPSE_ESCAPING: dir symlinks preserved as symlink entries
            assert "target_dir/file.txt" in file_paths
            # Dir symlinks should be in paths as symlink entries (v2025)
            symlink_entries = {p.path: p for p in manifest.paths if p.symlink_target is not None}
            assert "link1_dir" in symlink_entries
            assert symlink_entries["link1_dir"].symlink_target == "target_dir"
            assert "link2_dir" in symlink_entries
            assert symlink_entries["link2_dir"].symlink_target == "link1_dir"
        else:
            # COLLAPSE: symlinks followed, files collected via all paths
            assert "target_dir/file.txt" in file_paths
            assert "link1_dir/file.txt" in file_paths
            assert "link2_dir/file.txt" in file_paths

    # ==================== Symlink chain with escaping final target ====================

    @pytest.mark.parametrize(
        "version,symlink_policy,use_absolute_symlink",
        [
            pytest.param(
                ManifestVersion.v2023_03_03,
                SymlinkPolicy.COLLAPSE,
                False,
                id="v2023-COLLAPSE-relative",
                marks=pytest.mark.skipif(
                    os.name == "nt",
                    reason="Windows cannot follow relative symlinks with '..' components (WinError 123)",
                ),
            ),
            pytest.param(
                ManifestVersion.v2023_03_03,
                SymlinkPolicy.COLLAPSE,
                True,
                id="v2023-COLLAPSE-absolute",
            ),
            pytest.param(
                ManifestVersion.v2025_12_04_beta,
                SymlinkPolicy.COLLAPSE,
                False,
                id="v2025-COLLAPSE-relative",
                marks=pytest.mark.skipif(
                    os.name == "nt",
                    reason="Windows cannot follow relative symlinks with '..' components (WinError 123)",
                ),
            ),
            pytest.param(
                ManifestVersion.v2025_12_04_beta,
                SymlinkPolicy.COLLAPSE,
                True,
                id="v2025-COLLAPSE-absolute",
            ),
            pytest.param(
                ManifestVersion.v2025_12_04_beta,
                SymlinkPolicy.COLLAPSE_ESCAPING,
                False,
                id="v2025-COLLAPSE_ESCAPING-relative",
                marks=pytest.mark.skipif(
                    os.name == "nt",
                    reason="Windows cannot follow relative symlinks with '..' components (WinError 123)",
                ),
            ),
            pytest.param(
                ManifestVersion.v2025_12_04_beta,
                SymlinkPolicy.COLLAPSE_ESCAPING,
                True,
                id="v2025-COLLAPSE_ESCAPING-absolute",
            ),
        ],
    )
    def test_file_symlink_chain_final_target_escapes(
        self,
        tmp_path: Path,
        version: ManifestVersion,
        symlink_policy: SymlinkPolicy,
        use_absolute_symlink: bool,
    ) -> None:
        """Test symlink chain where final target is outside root.

        Structure:
            tmp_path/
                outside.txt     <- actual file (outside root)
                root/           <- manifest root
                    link1.txt   <- symlink to outside.txt (escaping, relative or absolute)
                    link2.txt   <- symlink to link1.txt (within root)

        Behavior:
            - COLLAPSE: Both symlinks are followed and collected as files
            - COLLAPSE_ESCAPING with relative symlink: link1 escapes via "..", so both
              are collapsed (link2 -> link1 -> ../outside.txt, the chain escapes)
            - COLLAPSE_ESCAPING with absolute symlink: link1 has absolute target outside
              root so it's collapsed, but link2 points to link1 which is within root,
              so link2 is preserved as a symlink entry
        """
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("outside content")

        root = tmp_path / "root"
        root.mkdir()
        link1 = root / "link1.txt"
        link2 = root / "link2.txt"

        try:
            if use_absolute_symlink:
                link1.symlink_to(outside_file)  # Absolute path
            else:
                link1.symlink_to("../outside.txt")  # Relative path with ..
            link2.symlink_to("link1.txt")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = collect_manifest(
            version=version,
            root=root,
            symlink_policy=symlink_policy,
        )

        file_paths = {p.path for p in manifest.paths}
        paths_by_name = {p.path: p for p in manifest.paths}

        # link1 should always be collected (either as file or symlink entry)
        assert "link1.txt" in file_paths
        # link1 should be collapsed to a file (escaping symlink)
        assert paths_by_name["link1.txt"].symlink_target is None

        # link2 -> link1 is within root, so behavior depends on policy
        # but NOT on whether link1 uses relative or absolute target
        assert "link2.txt" in file_paths
        if symlink_policy == SymlinkPolicy.COLLAPSE:
            # COLLAPSE: all symlinks followed
            assert paths_by_name["link2.txt"].symlink_target is None
        elif symlink_policy == SymlinkPolicy.COLLAPSE_ESCAPING:
            # COLLAPSE_ESCAPING: link2 is within root, so preserved as symlink
            assert paths_by_name["link2.txt"].symlink_target == "link1.txt"

    @pytest.mark.parametrize(
        "version,symlink_policy,use_absolute_symlink",
        [
            pytest.param(
                ManifestVersion.v2023_03_03,
                SymlinkPolicy.COLLAPSE,
                False,
                id="v2023-COLLAPSE-relative",
                marks=pytest.mark.skipif(
                    os.name == "nt",
                    reason="Windows cannot follow relative symlinks with '..' components (WinError 123)",
                ),
            ),
            pytest.param(
                ManifestVersion.v2023_03_03,
                SymlinkPolicy.COLLAPSE,
                True,
                id="v2023-COLLAPSE-absolute",
            ),
            pytest.param(
                ManifestVersion.v2025_12_04_beta,
                SymlinkPolicy.COLLAPSE,
                False,
                id="v2025-COLLAPSE-relative",
                marks=pytest.mark.skipif(
                    os.name == "nt",
                    reason="Windows cannot follow relative symlinks with '..' components (WinError 123)",
                ),
            ),
            pytest.param(
                ManifestVersion.v2025_12_04_beta,
                SymlinkPolicy.COLLAPSE,
                True,
                id="v2025-COLLAPSE-absolute",
            ),
            pytest.param(
                ManifestVersion.v2025_12_04_beta,
                SymlinkPolicy.COLLAPSE_ESCAPING,
                False,
                id="v2025-COLLAPSE_ESCAPING-relative",
                marks=pytest.mark.skipif(
                    os.name == "nt",
                    reason="Windows cannot follow relative symlinks with '..' components (WinError 123)",
                ),
            ),
            pytest.param(
                ManifestVersion.v2025_12_04_beta,
                SymlinkPolicy.COLLAPSE_ESCAPING,
                True,
                id="v2025-COLLAPSE_ESCAPING-absolute",
            ),
        ],
    )
    def test_dir_symlink_chain_final_target_escapes(
        self,
        tmp_path: Path,
        version: ManifestVersion,
        symlink_policy: SymlinkPolicy,
        use_absolute_symlink: bool,
    ) -> None:
        """Test dir symlink chain where final target directory is outside root.

        Structure:
            tmp_path/
                outside_dir/    <- actual directory (outside root)
                    file.txt
                root/           <- manifest root
                    link1_dir/  <- symlink to outside_dir (escaping, relative or absolute)
                    link2_dir/  <- symlink to link1_dir (within root)

        Behavior:
            - COLLAPSE: Both symlinks are followed, files collected via both paths
            - COLLAPSE_ESCAPING with relative symlink: link1 escapes via "..", so both
              are collapsed (link2 -> link1 -> ../outside_dir, the chain escapes)
            - COLLAPSE_ESCAPING with absolute symlink: link1 has absolute target outside
              root so it's collapsed, but link2 points to link1 which is within root,
              so link2 is preserved as a symlink entry
        """
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "file.txt").write_text("outside content")

        root = tmp_path / "root"
        root.mkdir()
        link1_dir = root / "link1_dir"
        link2_dir = root / "link2_dir"

        try:
            if use_absolute_symlink:
                link1_dir.symlink_to(outside_dir, target_is_directory=True)  # Absolute path
            else:
                link1_dir.symlink_to(
                    "../outside_dir", target_is_directory=True
                )  # Relative path with ..
            link2_dir.symlink_to("link1_dir", target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = collect_manifest(
            version=version,
            root=root,
            symlink_policy=symlink_policy,
        )

        file_paths = {p.path for p in manifest.paths}

        # link1_dir should always be followed (escaping symlink)
        assert "link1_dir/file.txt" in file_paths

        # link2_dir -> link1_dir is within root, so it should be preserved as symlink
        # regardless of whether link1_dir uses relative or absolute target
        if symlink_policy == SymlinkPolicy.COLLAPSE:
            # COLLAPSE: all symlinks followed
            assert "link2_dir/file.txt" in file_paths
        elif symlink_policy == SymlinkPolicy.COLLAPSE_ESCAPING:
            # COLLAPSE_ESCAPING: link2_dir is within root, so preserved as symlink
            assert "link2_dir" in file_paths
            link2_entry = next(p for p in manifest.paths if p.path == "link2_dir")
            assert link2_entry.symlink_target == "link1_dir"

    # ==================== Symlink chain with escaping intermediate link ====================

    @pytest.mark.parametrize(
        "version,symlink_policy,use_absolute_symlinks",
        [
            pytest.param(
                ManifestVersion.v2023_03_03,
                SymlinkPolicy.COLLAPSE,
                False,
                id="v2023-COLLAPSE-relative",
                marks=pytest.mark.skipif(
                    os.name == "nt",
                    reason="Windows cannot follow relative symlinks reliably (WinError 123)",
                ),
            ),
            pytest.param(
                ManifestVersion.v2023_03_03,
                SymlinkPolicy.COLLAPSE,
                True,
                id="v2023-COLLAPSE-absolute",
            ),
            pytest.param(
                ManifestVersion.v2025_12_04_beta,
                SymlinkPolicy.COLLAPSE,
                False,
                id="v2025-COLLAPSE-relative",
                marks=pytest.mark.skipif(
                    os.name == "nt",
                    reason="Windows cannot follow relative symlinks reliably (WinError 123)",
                ),
            ),
            pytest.param(
                ManifestVersion.v2025_12_04_beta,
                SymlinkPolicy.COLLAPSE,
                True,
                id="v2025-COLLAPSE-absolute",
            ),
            pytest.param(
                ManifestVersion.v2025_12_04_beta,
                SymlinkPolicy.COLLAPSE_ESCAPING,
                False,
                id="v2025-COLLAPSE_ESCAPING-relative",
                marks=pytest.mark.skipif(
                    os.name == "nt",
                    reason="Windows cannot follow relative symlinks reliably (WinError 123)",
                ),
            ),
            pytest.param(
                ManifestVersion.v2025_12_04_beta,
                SymlinkPolicy.COLLAPSE_ESCAPING,
                True,
                id="v2025-COLLAPSE_ESCAPING-absolute",
            ),
        ],
    )
    def test_file_symlink_chain_intermediate_escapes(
        self,
        tmp_path: Path,
        version: ManifestVersion,
        symlink_policy: SymlinkPolicy,
        use_absolute_symlinks: bool,
    ) -> None:
        """Test symlink chain where intermediate link is outside root.

        Structure:
            tmp_path/
                outside_link.txt  <- symlink to root/target.txt (outside root, points back in)
                root/             <- manifest root
                    target.txt    <- actual file
                    link.txt      <- symlink to outside_link.txt (escaping)

        With relative symlinks:
            outside_link.txt -> root/target.txt (relative)
            link.txt -> ../outside_link.txt (relative)

        With absolute symlinks:
            outside_link.txt -> /abs/path/root/target.txt (absolute)
            link.txt -> /abs/path/outside_link.txt (absolute)
        """
        root = tmp_path / "root"
        root.mkdir()
        target = root / "target.txt"
        target.write_text("content")

        outside_link = tmp_path / "outside_link.txt"
        link = root / "link.txt"

        try:
            if use_absolute_symlinks:
                outside_link.symlink_to(target)  # Absolute path
                link.symlink_to(outside_link)  # Absolute path
            else:
                outside_link.symlink_to("root/target.txt")  # Relative path
                link.symlink_to("../outside_link.txt")  # Relative path
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = collect_manifest(
            version=version,
            root=root,
            symlink_policy=symlink_policy,
        )

        file_paths = {p.path for p in manifest.paths}
        paths_by_name = {p.path: p for p in manifest.paths}

        # target.txt should always be collected
        assert "target.txt" in file_paths
        assert paths_by_name["target.txt"].symlink_target is None

        # link.txt escapes to outside_link.txt which points back to target.txt
        # COLLAPSE modes should follow and collect as a file
        assert "link.txt" in file_paths
        assert paths_by_name["link.txt"].symlink_target is None

    # ==================== EXCLUDE policy with chains ====================

    @pytest.mark.parametrize(
        "version",
        [ManifestVersion.v2023_03_03, ManifestVersion.v2025_12_04_beta],
        ids=["v2023", "v2025"],
    )
    def test_exclude_skips_all_symlinks_in_chain(
        self,
        tmp_path: Path,
        version: ManifestVersion,
    ) -> None:
        """EXCLUDE policy skips all symlinks regardless of chain structure.

        Structure:
            root/
                target.txt
                link1.txt -> target.txt
                link2.txt -> link1.txt
                target_dir/
                    file.txt
                link_dir -> target_dir
        """
        target = tmp_path / "target.txt"
        target.write_text("content")
        link1 = tmp_path / "link1.txt"
        link2 = tmp_path / "link2.txt"
        target_dir = tmp_path / "target_dir"
        target_dir.mkdir()
        (target_dir / "file.txt").write_text("dir content")
        link_dir = tmp_path / "link_dir"

        try:
            link1.symlink_to("target.txt")
            link2.symlink_to("link1.txt")
            link_dir.symlink_to("target_dir", target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        manifest = collect_manifest(
            version=version,
            root=tmp_path,
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        file_paths = {p.path for p in manifest.paths}

        # Only actual files should be collected
        assert "target.txt" in file_paths
        assert "target_dir/file.txt" in file_paths
        # All symlinks should be excluded
        assert "link1.txt" not in file_paths
        assert "link2.txt" not in file_paths
        assert "link_dir/file.txt" not in file_paths
