# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for collect_manifest function.

These tests cover:
- Absolute path generation for files and directories
- Absolute path symlink handling with PRESERVE policy
- Symlink chains with absolute paths
- Input validation (missing files, directories, invalid paths)
- Different symlink policies (COLLAPSE, PRESERVE, EXCLUDE, TRANSITIVE_INCLUDE_TARGETS)
- Optional filenames handling
- Empty directories
- Nested directory structures
"""

import os
import pytest
from pathlib import Path

from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
)
from deadline.job_attachments.asset_manifests.versions import (
    SymlinkPolicy,
)
from deadline.job_attachments.asset_manifests.manifest import AbsSnapshotManifest


class TestCollectManifest:
    """Tests for collect_manifest function."""

    def test_absolute_paths(self, tmp_path: Path) -> None:
        """When using collect_manifest, paths are absolute."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # Path should be absolute (POSIX format)
        assert manifest.files[0].path == tmp_path.as_posix() + "/file.txt"

    def test_nested_absolute_paths(self, tmp_path: Path) -> None:
        """Nested files have full absolute paths."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("content")

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # Find the file entry (not directory)
        file_entries = [p for p in manifest.files if "nested.txt" in p.path]
        assert len(file_entries) == 1
        assert file_entries[0].path == (subdir / "nested.txt").as_posix()

    def test_absolute_paths_with_subdir(self, tmp_path: Path) -> None:
        """When using collect_manifest, paths are absolute."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        manifest = collect_manifest(
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

    def test_symlink_absolute_paths(self, tmp_path: Path) -> None:
        """Symlink entries use absolute paths for both path and target with collect_manifest and PRESERVE policy."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        link.symlink_to("target.txt")

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        # Find the symlink entry
        link_entry = [p for p in manifest.files if "link.txt" in p.path][0]
        assert link_entry.path == link.as_posix()
        # symlink_target should also be absolute with collect_manifest and PRESERVE
        assert link_entry.symlink_target == target.resolve().as_posix()

    def test_produces_absolute_paths(self, tmp_path: Path) -> None:
        """collect_manifest produces absolute paths."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_manifest(
            [tmp_path],
            [],
        )

        file_entries = [p for p in manifest.files if "file.txt" in p.path]
        assert file_entries[0].path == (tmp_path / "file.txt").as_posix()


class TestCollectManifestSymlinkChains:
    """Tests for symlink chain handling with collect_manifest."""

    def test_symlink_chain_preserved_with_preserve_policy(self, tmp_path: Path) -> None:
        """Symlink chain of length 3 is preserved with collect_manifest and PRESERVE policy."""
        # Create: link3 -> link2 -> link1 -> target.txt
        target = tmp_path / "target.txt"
        target.write_text("content")
        link1 = tmp_path / "link1.txt"
        link2 = tmp_path / "link2.txt"
        link3 = tmp_path / "link3.txt"

        link1.symlink_to("target.txt")
        link2.symlink_to("link1.txt")
        link3.symlink_to("link2.txt")

        # Test with absolute paths using collect_manifest with PRESERVE policy
        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        paths_by_name = {p.path: p for p in manifest.files}
        # Each symlink should point to its immediate target (absolute), not the final target
        assert paths_by_name[link1.as_posix()].symlink_target == target.as_posix()
        assert paths_by_name[link2.as_posix()].symlink_target == link1.as_posix()
        assert paths_by_name[link3.as_posix()].symlink_target == link2.as_posix()

    def test_preserve_keeps_all_symlinks(self, tmp_path: Path) -> None:
        """PRESERVE policy keeps all symlinks including escaping ones."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        link.symlink_to("target.txt")

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        # Both entries should exist
        assert len(manifest.files) == 2
        link_entry = [p for p in manifest.files if "link.txt" in p.path][0]
        assert link_entry.symlink_target == target.as_posix()

    def test_preserve_keeps_all_symlinks_in_chain(self, tmp_path: Path) -> None:
        """PRESERVE policy keeps all symlinks with absolute targets.

        Structure:
            root/
                target.txt
                link1.txt -> target.txt
                link2.txt -> link1.txt
        """
        target = tmp_path / "target.txt"
        target.write_text("content")
        link1 = tmp_path / "link1.txt"
        link2 = tmp_path / "link2.txt"

        link1.symlink_to("target.txt")
        link2.symlink_to("link1.txt")

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # All entries should exist
        assert target.as_posix() in paths_by_name
        assert link1.as_posix() in paths_by_name
        assert link2.as_posix() in paths_by_name

        # Symlinks should have absolute targets
        assert paths_by_name[link1.as_posix()].symlink_target == target.as_posix()
        assert paths_by_name[link2.as_posix()].symlink_target == link1.as_posix()

    @pytest.mark.parametrize(
        "use_absolute_symlink",
        [
            pytest.param(
                False,
                id="relative",
                marks=pytest.mark.skipif(
                    os.name == "nt",
                    reason="Windows cannot follow relative symlinks with '..' components (WinError 123)",
                ),
            ),
            pytest.param(True, id="absolute"),
        ],
    )
    def test_preserve_keeps_escaping_symlink_chain(
        self, tmp_path: Path, use_absolute_symlink: bool
    ) -> None:
        """PRESERVE policy keeps escaping symlinks with absolute targets.

        Structure:
            tmp_path/
                outside.txt     <- outside root
                root/
                    link.txt    <- symlink to outside.txt (relative or absolute)
        """
        outside = tmp_path / "outside.txt"
        outside.write_text("outside content")

        root = tmp_path / "root"
        root.mkdir()
        link = root / "link.txt"

        if use_absolute_symlink:
            link.symlink_to(outside)  # Absolute path
        else:
            link.symlink_to("../outside.txt")  # Relative path

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # Escaping symlink should be preserved with absolute target
        assert link.as_posix() in paths_by_name
        assert paths_by_name[link.as_posix()].symlink_target == outside.as_posix()


class TestCollectManifestInputValidation:
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


class TestCollectManifestOptionalFilenames:
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


class TestCollectManifestSymlinkPolicies:
    """Tests for symlink policy handling."""

    def test_collapse_follows_file_symlinks(self, tmp_path: Path) -> None:
        """COLLAPSE policy follows file symlinks and collects target content."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # Both target and link should be collected as files
        assert isinstance(manifest, AbsSnapshotManifest)
        paths = {p.path for p in manifest.files}
        assert target.as_posix() in paths
        assert link.as_posix() in paths

    def test_exclude_skips_symlinks(self, tmp_path: Path) -> None:
        """EXCLUDE policy skips symlinks entirely."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        # Only target should be collected, not the symlink
        assert isinstance(manifest, AbsSnapshotManifest)
        paths = {p.path for p in manifest.files}
        assert target.as_posix() in paths
        assert link.as_posix() not in paths

    def test_transitive_include_targets_adds_escaping_target(self, tmp_path: Path) -> None:
        """TRANSITIVE_INCLUDE_TARGETS adds escaping symlink targets to manifest."""
        # Create structure: root/link.txt -> ../outside.txt
        outside = tmp_path / "outside.txt"
        outside.write_text("outside content")

        root = tmp_path / "root"
        root.mkdir()
        link = root / "link.txt"
        link.symlink_to(outside)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
        )

        assert isinstance(manifest, AbsSnapshotManifest)
        paths = {p.path for p in manifest.files}

        # Symlink should be preserved
        assert link.as_posix() in paths
        link_entry = [p for p in manifest.files if p.path == link.as_posix()][0]
        assert link_entry.symlink_target == outside.as_posix()

        # Target should also be collected (transitive)
        assert outside.as_posix() in paths


class TestCollectManifestDirectoryHandling:
    """Tests for directory handling in manifests."""

    def test_empty_directory_included(self, tmp_path: Path) -> None:
        """Empty directories are included in manifests."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        manifest = collect_manifest(
            [tmp_path],
            [],
        )

        assert isinstance(manifest, AbsSnapshotManifest)
        dir_paths = {d.path for d in manifest.dirs}
        assert empty_dir.as_posix() in dir_paths

    def test_nested_directories_included(self, tmp_path: Path) -> None:
        """Nested directories are all included."""
        level1 = tmp_path / "level1"
        level2 = level1 / "level2"
        level3 = level2 / "level3"
        level3.mkdir(parents=True)
        (level3 / "file.txt").write_text("content")

        manifest = collect_manifest(
            [tmp_path],
            [],
        )

        assert isinstance(manifest, AbsSnapshotManifest)
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

        manifest = collect_manifest(
            [dir1, dir2],
            [],
        )

        paths = {p.path for p in manifest.files}
        assert (dir1 / "file1.txt").as_posix() in paths
        assert (dir2 / "file2.txt").as_posix() in paths


class TestCollectManifestMetadata:
    """Tests for metadata collection."""

    def test_file_size_captured(self, tmp_path: Path) -> None:
        """File size is captured correctly."""
        file_path = tmp_path / "file.txt"
        content = "Hello, World!"
        file_path.write_text(content)

        manifest = collect_manifest(
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

        manifest = collect_manifest(
            [tmp_path],
            [],
        )

        file_entries = [p for p in manifest.files if "file.txt" in p.path]
        assert file_entries[0].mtime == expected_mtime

    def test_hash_is_none_for_unhashed(self, tmp_path: Path) -> None:
        """Hash is set to None for unhashed files (to be filled by hash_manifest)."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")

        manifest = collect_manifest(
            [tmp_path],
            [],
        )

        file_entries = [p for p in manifest.files if "file.txt" in p.path]
        assert file_entries[0].hash is None

    @pytest.mark.skipif(os.name == "nt", reason="Execute bit not meaningful on Windows")
    def test_runnable_flag_captured(self, tmp_path: Path) -> None:
        """Runnable flag is captured for executable files."""
        script = tmp_path / "script.sh"
        script.write_text("#!/bin/bash\necho hello")
        script.chmod(0o755)

        manifest = collect_manifest(
            [tmp_path],
            [],
        )

        file_entries = [p for p in manifest.files if "script.sh" in p.path]
        assert file_entries[0].runnable is True

    def test_total_size_calculated(self, tmp_path: Path) -> None:
        """Total size is sum of all file sizes."""
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file1.write_text("12345")  # 5 bytes
        file2.write_text("1234567890")  # 10 bytes

        manifest = collect_manifest(
            [tmp_path],
            [],
        )

        assert manifest.totalSize == 15


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
        manifest = collect_manifest(
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

        manifest = collect_manifest(
            [subdir],
            [extra_file],
        )

        paths = {p.path for p in manifest.files}
        assert (subdir / "in_dir.txt").as_posix() in paths
        assert extra_file.as_posix() in paths


class TestCollectManifestCollapseEscaping:
    """Tests for COLLAPSE_ESCAPING symlink policy.

    COLLAPSE_ESCAPING preserves symlinks whose targets are within the collected
    paths, and collapses symlinks whose targets are outside (escaping symlinks).
    """

    def test_non_escaping_symlink_preserved(self, tmp_path: Path) -> None:
        """Symlink to file within collected paths is preserved as symlink."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to("target.txt")

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # Both should exist
        assert target.as_posix() in paths_by_name
        assert link.as_posix() in paths_by_name

        # Link should be preserved as symlink (target is in collected set)
        link_entry = paths_by_name[link.as_posix()]
        assert link_entry.symlink_target == target.as_posix()

    def test_escaping_file_symlink_collapsed(self, tmp_path: Path) -> None:
        """Symlink to file outside collected paths is collapsed to file.

        Structure:
            tmp_path/
                outside.txt     <- outside root (not collected)
                root/
                    link.txt    <- symlink to outside.txt (escaping, absolute path)
        """
        outside = tmp_path / "outside.txt"
        outside.write_text("outside content")

        root = tmp_path / "root"
        root.mkdir()
        link = root / "link.txt"
        link.symlink_to(outside)  # Absolute path symlink

        manifest = collect_manifest(
            [root],  # Only collect root, not outside.txt
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # Link should be collapsed (no symlink_target)
        assert link.as_posix() in paths_by_name
        link_entry = paths_by_name[link.as_posix()]
        assert link_entry.symlink_target is None
        assert link_entry.size == len("outside content")

        # outside.txt should NOT be in manifest (it's outside collected paths)
        assert outside.as_posix() not in paths_by_name

    def test_escaping_dir_symlink_collapsed(self, tmp_path: Path) -> None:
        """Symlink to directory outside collected paths is collapsed.

        Structure:
            tmp_path/
                outside_dir/
                    file.txt
                root/
                    link_dir    <- symlink to outside_dir (escaping, absolute path)
        """
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "file.txt").write_text("content")

        root = tmp_path / "root"
        root.mkdir()
        link_dir = root / "link_dir"
        link_dir.symlink_to(outside_dir)  # Absolute path symlink

        manifest = collect_manifest(
            [root],  # Only collect root
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths = {p.path for p in manifest.files}
        dir_paths = {d.path for d in manifest.dirs}

        # The symlink dir should be collapsed - its contents appear under link_dir path
        assert f"{link_dir.as_posix()}/file.txt" in paths

        # The directory entry should exist
        assert link_dir.as_posix() in dir_paths

        # outside_dir should NOT be in manifest
        assert outside_dir.as_posix() not in dir_paths
        assert f"{outside_dir.as_posix()}/file.txt" not in paths

    def test_mixed_escaping_and_non_escaping(self, tmp_path: Path) -> None:
        """Mix of escaping and non-escaping symlinks handled correctly.

        Structure:
            tmp_path/
                outside.txt         <- outside root
                root/
                    internal.txt    <- regular file
                    link_internal   <- symlink to internal.txt (non-escaping)
                    link_outside    <- symlink to outside.txt (escaping)
        """
        outside = tmp_path / "outside.txt"
        outside.write_text("outside")

        root = tmp_path / "root"
        root.mkdir()
        internal = root / "internal.txt"
        internal.write_text("internal")
        link_internal = root / "link_internal.txt"
        link_internal.symlink_to("internal.txt")
        link_outside = root / "link_outside.txt"
        link_outside.symlink_to(outside)  # Absolute path to outside

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # Internal file should exist
        assert internal.as_posix() in paths_by_name

        # Non-escaping symlink should be preserved
        assert link_internal.as_posix() in paths_by_name
        assert paths_by_name[link_internal.as_posix()].symlink_target == internal.as_posix()

        # Escaping symlink should be collapsed
        assert link_outside.as_posix() in paths_by_name
        assert paths_by_name[link_outside.as_posix()].symlink_target is None
        assert paths_by_name[link_outside.as_posix()].size == len("outside")

    def test_symlink_to_sibling_directory_preserved(self, tmp_path: Path) -> None:
        """Symlink to sibling directory (both collected) is preserved.

        Structure:
            tmp_path/
                dir1/
                    file1.txt
                dir2/
                    link_to_dir1    <- symlink to ../dir1 (non-escaping)
        """
        dir1 = tmp_path / "dir1"
        dir1.mkdir()
        (dir1 / "file1.txt").write_text("content1")

        dir2 = tmp_path / "dir2"
        dir2.mkdir()
        link_to_dir1 = dir2 / "link_to_dir1"
        link_to_dir1.symlink_to(dir1)  # Absolute symlink

        manifest = collect_manifest(
            [dir1, dir2],  # Both directories collected
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # Symlink should be preserved (target dir1 is in collected set)
        assert link_to_dir1.as_posix() in paths_by_name
        assert paths_by_name[link_to_dir1.as_posix()].symlink_target == dir1.as_posix()

    def test_symlink_chain_partial_escape(self, tmp_path: Path) -> None:
        """Symlink chain where intermediate link is non-escaping but final target escapes.

        Structure:
            tmp_path/
                outside.txt
                root/
                    link1.txt   <- symlink to link2.txt (non-escaping)
                    link2.txt   <- symlink to ../outside.txt (escaping)

        With COLLAPSE_ESCAPING:
        - link1 points to link2 (which is in collected set) -> preserved
        - link2 points to outside.txt (not in collected set) -> collapsed
        """
        outside = tmp_path / "outside.txt"
        outside.write_text("outside")

        root = tmp_path / "root"
        root.mkdir()
        link2 = root / "link2.txt"
        link2.symlink_to(outside)  # Absolute path
        link1 = root / "link1.txt"
        link1.symlink_to("link2.txt")

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # link1 points to link2 which IS in collected set -> preserved as symlink
        assert link1.as_posix() in paths_by_name
        assert paths_by_name[link1.as_posix()].symlink_target == link2.as_posix()

        # link2 points to outside which is NOT in collected set -> collapsed
        assert link2.as_posix() in paths_by_name
        assert paths_by_name[link2.as_posix()].symlink_target is None

    def test_no_symlinks_works(self, tmp_path: Path) -> None:
        """COLLAPSE_ESCAPING works correctly when there are no symlinks."""
        (tmp_path / "file.txt").write_text("content")
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("nested")

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths = {p.path for p in manifest.files}
        assert (tmp_path / "file.txt").as_posix() in paths
        assert (subdir / "nested.txt").as_posix() in paths


class TestCollectManifestCollapseEscapingChains:
    """Tests for COLLAPSE_ESCAPING with symlink chains.

    Legend:
    - IN = path is inside the collected dataset
    - OUT = path is outside the collected dataset (escaping)

    For COLLAPSE_ESCAPING:
    - Symlinks pointing to IN targets are preserved as symlinks
    - Symlinks pointing to OUT targets are collapsed
    """

    def test_chain_in_in_file_in(self, tmp_path: Path) -> None:
        """Chain: symlink [IN] -> symlink [IN] -> file [IN]

        All symlinks point to targets within the collected set.
        Both symlinks should be preserved.

        Structure:
            root/
                file.txt        <- target file [IN]
                link1.txt       <- symlink to file.txt [IN]
                link2.txt       <- symlink to link1.txt [IN]
        """
        root = tmp_path / "root"
        root.mkdir()
        target_file = root / "file.txt"
        target_file.write_text("content")
        link1 = root / "link1.txt"
        link1.symlink_to(target_file)
        link2 = root / "link2.txt"
        link2.symlink_to(link1)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # All three should exist
        assert target_file.as_posix() in paths_by_name
        assert link1.as_posix() in paths_by_name
        assert link2.as_posix() in paths_by_name

        # Both symlinks should be preserved (targets are IN)
        assert paths_by_name[link1.as_posix()].symlink_target == target_file.as_posix()
        assert paths_by_name[link2.as_posix()].symlink_target == link1.as_posix()

    def test_chain_in_in_dir_in(self, tmp_path: Path) -> None:
        """Chain: symlink [IN] -> symlink [IN] -> directory [IN]

        All symlinks point to targets within the collected set.
        Both symlinks should be preserved.

        Structure:
            root/
                subdir/
                    file.txt
                link1           <- symlink to subdir [IN]
                link2           <- symlink to link1 [IN]
        """
        root = tmp_path / "root"
        root.mkdir()
        subdir = root / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")
        link1 = root / "link1"
        link1.symlink_to(subdir)
        link2 = root / "link2"
        link2.symlink_to(link1)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # Both symlinks should be preserved (targets are IN)
        assert link1.as_posix() in paths_by_name
        assert link2.as_posix() in paths_by_name
        assert paths_by_name[link1.as_posix()].symlink_target == subdir.as_posix()
        assert paths_by_name[link2.as_posix()].symlink_target == link1.as_posix()

    def test_chain_in_in_file_out(self, tmp_path: Path) -> None:
        """Chain: symlink [IN] -> symlink [IN] -> file [OUT]

        link2 -> link1 -> outside_file
        - link1 points to outside_file (OUT) -> collapsed
        - link2 points to link1 (IN) -> preserved

        Structure:
            outside.txt         <- target file [OUT]
            root/
                link1.txt       <- symlink to outside.txt [OUT -> collapsed]
                link2.txt       <- symlink to link1.txt [IN -> preserved]
        """
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("outside content")
        root = tmp_path / "root"
        root.mkdir()
        link1 = root / "link1.txt"
        link1.symlink_to(outside_file)
        link2 = root / "link2.txt"
        link2.symlink_to(link1)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # link1 points to OUT -> collapsed (no symlink_target)
        assert link1.as_posix() in paths_by_name
        assert paths_by_name[link1.as_posix()].symlink_target is None
        assert paths_by_name[link1.as_posix()].size == len("outside content")

        # link2 points to link1 (IN) -> preserved as symlink
        assert link2.as_posix() in paths_by_name
        assert paths_by_name[link2.as_posix()].symlink_target == link1.as_posix()

    def test_chain_in_in_dir_out(self, tmp_path: Path) -> None:
        """Chain: symlink [IN] -> symlink [IN] -> directory [OUT]

        link2 -> link1 -> outside_dir
        - link1 points to outside_dir (OUT) -> collapsed
        - link2 points to link1 (IN) -> preserved

        Structure:
            outside_dir/
                file.txt
            root/
                link1           <- symlink to outside_dir [OUT -> collapsed]
                link2           <- symlink to link1 [IN -> preserved]
        """
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "file.txt").write_text("content")
        root = tmp_path / "root"
        root.mkdir()
        link1 = root / "link1"
        link1.symlink_to(outside_dir)
        link2 = root / "link2"
        link2.symlink_to(link1)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}
        dir_paths = {d.path for d in manifest.dirs}

        # link1 points to OUT dir -> collapsed (contents inlined)
        assert link1.as_posix() in dir_paths
        assert f"{link1.as_posix()}/file.txt" in paths_by_name

        # link2 points to link1 (IN) -> preserved as symlink
        assert link2.as_posix() in paths_by_name
        assert paths_by_name[link2.as_posix()].symlink_target == link1.as_posix()

    def test_chain_in_out_file_in(self, tmp_path: Path) -> None:
        """Chain: symlink [IN] -> symlink [OUT] -> file [IN]

        link2 -> outside_link -> target_file
        - outside_link is OUT, so link2 (pointing to it) is collapsed
        - target_file is IN and collected normally

        Structure:
            outside_link.txt    <- symlink to root/file.txt [OUT]
            root/
                file.txt        <- target file [IN]
                link2.txt       <- symlink to outside_link.txt [OUT -> collapsed]
        """
        root = tmp_path / "root"
        root.mkdir()
        target_file = root / "file.txt"
        target_file.write_text("content")
        outside_link = tmp_path / "outside_link.txt"
        outside_link.symlink_to(target_file)
        link2 = root / "link2.txt"
        link2.symlink_to(outside_link)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # target_file is IN -> collected normally
        assert target_file.as_posix() in paths_by_name
        assert paths_by_name[target_file.as_posix()].symlink_target is None

        # link2 points to outside_link (OUT) -> collapsed
        # When collapsed, it follows the symlink chain to get the actual content
        assert link2.as_posix() in paths_by_name
        assert paths_by_name[link2.as_posix()].symlink_target is None
        assert paths_by_name[link2.as_posix()].size == len("content")

        # outside_link should NOT be in manifest
        assert outside_link.as_posix() not in paths_by_name

    def test_chain_in_out_dir_in(self, tmp_path: Path) -> None:
        """Chain: symlink [IN] -> symlink [OUT] -> directory [IN]

        link2 -> outside_link -> subdir
        - outside_link is OUT, so link2 (pointing to it) is collapsed
        - subdir is IN and collected normally

        Structure:
            outside_link        <- symlink to root/subdir [OUT]
            root/
                subdir/
                    file.txt
                link2           <- symlink to outside_link [OUT -> collapsed]
        """
        root = tmp_path / "root"
        root.mkdir()
        subdir = root / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")
        outside_link = tmp_path / "outside_link"
        outside_link.symlink_to(subdir)
        link2 = root / "link2"
        link2.symlink_to(outside_link)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}
        dir_paths = {d.path for d in manifest.dirs}

        # subdir is IN -> collected normally
        assert subdir.as_posix() in dir_paths
        assert f"{subdir.as_posix()}/file.txt" in paths_by_name

        # link2 points to outside_link (OUT) -> collapsed
        # Contents should be inlined under link2 path
        assert link2.as_posix() in dir_paths
        assert f"{link2.as_posix()}/file.txt" in paths_by_name

        # outside_link should NOT be in manifest
        assert outside_link.as_posix() not in paths_by_name
        assert outside_link.as_posix() not in dir_paths
