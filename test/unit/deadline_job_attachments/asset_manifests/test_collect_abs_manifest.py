# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for collect_abs_manifest function.

These tests cover:
- Absolute path generation for files and directories
- Absolute path symlink handling with PRESERVE policy
- Symlink chains with absolute paths
"""

import os
import pytest
from pathlib import Path

from deadline.job_attachments.asset_manifests._operations import (
    collect_abs_manifest,
)
from deadline.job_attachments.asset_manifests.versions import (
    ManifestVersion,
    SymlinkPolicy,
)


class TestCollectAbsManifest:
    """Tests for collect_abs_manifest function."""

    def test_v2023_absolute_paths(self, tmp_path: Path) -> None:
        """When using collect_abs_manifest, paths are absolute."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_abs_manifest(
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

        manifest = collect_abs_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2023_03_03,
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        assert manifest.paths[0].path == (subdir / "nested.txt").as_posix()

    def test_v2025_absolute_paths(self, tmp_path: Path) -> None:
        """When using collect_abs_manifest, paths are absolute."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")

        manifest = collect_abs_manifest(
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
        """Symlink entries use absolute paths for both path and target with collect_abs_manifest and PRESERVE policy."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        link.symlink_to("target.txt")

        manifest = collect_abs_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2025_12_04_beta,
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        # Find the symlink entry
        link_entry = [p for p in manifest.paths if "link.txt" in p.path][0]
        assert link_entry.path == link.as_posix()
        # symlink_target should also be absolute with collect_abs_manifest and PRESERVE
        assert link_entry.symlink_target == target.resolve().as_posix()

    def test_produces_absolute_paths(self, tmp_path: Path) -> None:
        """collect_abs_manifest produces absolute paths."""
        (tmp_path / "file.txt").write_text("content")

        manifest = collect_abs_manifest(
            [tmp_path],
            [],
            version=ManifestVersion.v2025_12_04_beta,
        )

        assert manifest.paths[0].path == (tmp_path / "file.txt").as_posix()


class TestCollectAbsManifestSymlinkChains:
    """Tests for symlink chain handling with collect_abs_manifest."""

    def test_symlink_chain_preserved_with_preserve_policy(self, tmp_path: Path) -> None:
        """Symlink chain of length 3 is preserved with collect_abs_manifest and PRESERVE policy."""
        # Create: link3 -> link2 -> link1 -> target.txt
        target = tmp_path / "target.txt"
        target.write_text("content")
        link1 = tmp_path / "link1.txt"
        link2 = tmp_path / "link2.txt"
        link3 = tmp_path / "link3.txt"

        link1.symlink_to("target.txt")
        link2.symlink_to("link1.txt")
        link3.symlink_to("link2.txt")

        # Test with absolute paths using collect_abs_manifest with PRESERVE policy
        manifest = collect_abs_manifest(
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

        manifest = collect_abs_manifest(
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

        manifest = collect_abs_manifest(
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

        manifest = collect_abs_manifest(
            [root],
            [],
            version=ManifestVersion.v2025_12_04_beta,
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        paths_by_name = {p.path: p for p in manifest.paths}

        # Escaping symlink should be preserved with absolute target
        assert link.as_posix() in paths_by_name
        assert paths_by_name[link.as_posix()].symlink_target == outside.as_posix()
