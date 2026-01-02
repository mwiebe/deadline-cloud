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
    ManifestVersion,
    SymlinkPolicy,
)
from deadline.job_attachments.asset_manifests.v2023_03_03.asset_manifest import (
    AssetManifest as AssetManifest2023,
)
from deadline.job_attachments.asset_manifests.v2025_12_04.asset_manifest import (
    AssetManifest as AssetManifest2025,
)


class TestCollectManifest:
    """Tests for collect_manifest function."""

    def test_v2023_absolute_paths(self, tmp_path: Path) -> None:
        """When using collect_manifest, paths are absolute."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2023_03_03,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # Path should be absolute (POSIX format)
        assert manifest.paths[0].path == tmp_path.as_posix() + "/file.txt"

    def test_v2023_nested_absolute_paths(self, tmp_path: Path) -> None:
        """Nested files have full absolute paths."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("content")

        manifest = collect_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2023_03_03,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        assert manifest.paths[0].path == (subdir / "nested.txt").as_posix()

    def test_v2025_absolute_paths(self, tmp_path: Path) -> None:
        """When using collect_manifest, paths are absolute."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        manifest = collect_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2025_12_04_beta,
        )

        # Check file path is absolute
        file_entry = [p for p in manifest.paths if "file.txt" in p.path][0]
        assert file_entry.path == (subdir / "file.txt").as_posix()
        # Check dir paths are absolute (includes root and subdir)
        dir_paths = {d.path for d in manifest.dirs}
        assert tmp_path.as_posix() in dir_paths
        assert subdir.as_posix() in dir_paths

    def test_v2025_symlink_absolute_paths(self, tmp_path: Path) -> None:
        """Symlink entries use absolute paths for both path and target with collect_manifest and PRESERVE policy."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        link.symlink_to("target.txt")

        manifest = collect_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2025_12_04_beta,
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        # Find the symlink entry
        link_entry = [p for p in manifest.paths if "link.txt" in p.path][0]
        assert link_entry.path == link.as_posix()
        # symlink_target should also be absolute with collect_manifest and PRESERVE
        assert link_entry.symlink_target == target.resolve().as_posix()

    def test_produces_absolute_paths(self, tmp_path: Path) -> None:
        """collect_manifest produces absolute paths."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2025_12_04_beta,
        )

        assert manifest.paths[0].path == (tmp_path / "file.txt").as_posix()


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
            version=ManifestVersion.v2025_12_04_beta,
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        paths_by_name = {p.path: p for p in manifest.paths}
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
            version=ManifestVersion.v2025_12_04_beta,
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        # Both entries should exist
        assert len(manifest.paths) == 2
        link_entry = [p for p in manifest.paths if "link.txt" in p.path][0]
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
            version=ManifestVersion.v2025_12_04_beta,
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        paths_by_name = {p.path: p for p in manifest.paths}

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
            version=ManifestVersion.v2025_12_04_beta,
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        paths_by_name = {p.path: p for p in manifest.paths}

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
                version=ManifestVersion.v2025_12_04_beta,
            )

    def test_raises_on_file_as_directory(self, tmp_path: Path) -> None:
        """Raises ValueError when a file is passed as a directory."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")

        with pytest.raises(ValueError, match="Path is not a directory"):
            collect_manifest(
                [file_path],
                [],
                version=ManifestVersion.v2025_12_04_beta,
            )

    def test_raises_on_nonexistent_filename(self, tmp_path: Path) -> None:
        """Raises FileNotFoundError when required filename doesn't exist."""
        nonexistent = tmp_path / "nonexistent.txt"

        with pytest.raises(FileNotFoundError, match="File does not exist"):
            collect_manifest(
                [],
                [nonexistent],
                version=ManifestVersion.v2025_12_04_beta,
            )

    def test_raises_on_directory_as_filename(self, tmp_path: Path) -> None:
        """Raises ValueError when a directory is passed as a filename."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        with pytest.raises(ValueError, match="Path is not a file or symlink"):
            collect_manifest(
                [],
                [subdir],
                version=ManifestVersion.v2025_12_04_beta,
            )

    def test_raises_on_collapse_escaping_policy(self, tmp_path: Path) -> None:
        """Raises ValueError when COLLAPSE_ESCAPING policy is used."""
        (tmp_path / "file.txt").write_text("content")

        with pytest.raises(ValueError, match="COLLAPSE_ESCAPING is not supported"):
            collect_manifest(
                [tmp_path],
                [],
                version=ManifestVersion.v2025_12_04_beta,
                symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
            )

    def test_raises_on_unsupported_v2023_symlink_policy(self, tmp_path: Path) -> None:
        """Raises ValueError when v2023 is used with unsupported symlink policy."""
        (tmp_path / "file.txt").write_text("content")

        with pytest.raises(ValueError, match="v2023-03-03 manifest format only supports"):
            collect_manifest(
                [tmp_path],
                [],
                version=ManifestVersion.v2023_03_03,
                symlink_policy=SymlinkPolicy.PRESERVE,
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
            version=ManifestVersion.v2025_12_04_beta,
        )

        paths = {p.path for p in manifest.paths}
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
            version=ManifestVersion.v2025_12_04_beta,
        )

        paths = {p.path for p in manifest.paths}
        assert file1.as_posix() in paths
        assert missing.as_posix() not in paths


class TestCollectManifestV2023SymlinkPolicies:
    """Tests for v2023 symlink policy handling."""

    def test_collapse_follows_file_symlinks(self, tmp_path: Path) -> None:
        """COLLAPSE policy follows file symlinks and collects target content."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2023_03_03,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # Both target and link should be collected as files
        assert isinstance(manifest, AssetManifest2023)
        paths = {p.path for p in manifest.paths}
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
            version=ManifestVersion.v2023_03_03,
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        # Only target should be collected, not the symlink
        assert isinstance(manifest, AssetManifest2023)
        paths = {p.path for p in manifest.paths}
        assert target.as_posix() in paths
        assert link.as_posix() not in paths


class TestCollectManifestV2025SymlinkPolicies:
    """Tests for v2025 symlink policy handling."""

    def test_exclude_skips_symlinks(self, tmp_path: Path) -> None:
        """EXCLUDE policy skips symlinks entirely."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2025_12_04_beta,
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        # Only target should be collected, not the symlink
        assert isinstance(manifest, AssetManifest2025)
        paths = {p.path for p in manifest.paths}
        assert target.as_posix() in paths
        assert link.as_posix() not in paths

    def test_collapse_follows_file_symlinks(self, tmp_path: Path) -> None:
        """COLLAPSE policy follows file symlinks."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2025_12_04_beta,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        assert isinstance(manifest, AssetManifest2025)
        # Both should be collected as files (symlink followed)
        paths = {p.path for p in manifest.paths}
        assert target.as_posix() in paths
        assert link.as_posix() in paths

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
            version=ManifestVersion.v2025_12_04_beta,
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
        )

        assert isinstance(manifest, AssetManifest2025)
        paths = {p.path for p in manifest.paths}

        # Symlink should be preserved
        assert link.as_posix() in paths
        link_entry = [p for p in manifest.paths if p.path == link.as_posix()][0]
        assert link_entry.symlink_target == outside.as_posix()

        # Target should also be collected (transitive)
        assert outside.as_posix() in paths


class TestCollectManifestDirectoryHandling:
    """Tests for directory handling in v2025 manifests."""

    def test_empty_directory_included(self, tmp_path: Path) -> None:
        """Empty directories are included in v2025 manifests."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        manifest = collect_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2025_12_04_beta,
        )

        assert isinstance(manifest, AssetManifest2025)
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
            version=ManifestVersion.v2025_12_04_beta,
        )

        assert isinstance(manifest, AssetManifest2025)
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
            version=ManifestVersion.v2025_12_04_beta,
        )

        paths = {p.path for p in manifest.paths}
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
            version=ManifestVersion.v2025_12_04_beta,
        )

        entry = manifest.paths[0]
        assert entry.size == len(content)

    def test_mtime_captured(self, tmp_path: Path) -> None:
        """File mtime is captured correctly."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")
        stat_info = file_path.stat()
        expected_mtime = stat_info.st_mtime_ns // 1000

        manifest = collect_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2025_12_04_beta,
        )

        entry = manifest.paths[0]
        assert entry.mtime == expected_mtime

    def test_hash_is_empty_string(self, tmp_path: Path) -> None:
        """Hash is set to empty string (to be filled by hash_manifest)."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")

        manifest = collect_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2025_12_04_beta,
        )

        entry = manifest.paths[0]
        assert entry.hash == ""

    @pytest.mark.skipif(os.name == "nt", reason="Execute bit not meaningful on Windows")
    def test_runnable_flag_captured(self, tmp_path: Path) -> None:
        """Runnable flag is captured for executable files."""
        script = tmp_path / "script.sh"
        script.write_text("#!/bin/bash\necho hello")
        script.chmod(0o755)

        manifest = collect_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2025_12_04_beta,
        )

        entry = manifest.paths[0]
        assert entry.runnable is True

    def test_total_size_calculated(self, tmp_path: Path) -> None:
        """Total size is sum of all file sizes."""
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file1.write_text("12345")  # 5 bytes
        file2.write_text("1234567890")  # 10 bytes

        manifest = collect_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2025_12_04_beta,
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
            version=ManifestVersion.v2025_12_04_beta,
        )

        paths = {p.path for p in manifest.paths}
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
            version=ManifestVersion.v2025_12_04_beta,
        )

        paths = {p.path for p in manifest.paths}
        assert (subdir / "in_dir.txt").as_posix() in paths
        assert extra_file.as_posix() in paths
