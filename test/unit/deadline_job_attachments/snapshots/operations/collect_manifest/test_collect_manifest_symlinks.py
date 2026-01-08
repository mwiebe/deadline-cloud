# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for collect_manifest symlink handling.

These tests cover:
- Symlink chain handling with PRESERVE policy
- COLLAPSE policy with file and directory symlinks
- EXCLUDE policy
- TRANSITIVE_INCLUDE_TARGETS policy basics
- Symlink target resolution
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

import pytest

from deadline.job_attachments._snapshots import (
    collect_manifest,
    SymlinkPolicy,
)
from deadline.job_attachments._snapshots import AbsSnapshotManifest


class TestPreservePolicy:
    """Tests for PRESERVE symlink policy."""

    def test_symlink_preserved_with_absolute_target(self, tmp_path: Path) -> None:
        """Symlink is preserved with absolute target path."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to("target.txt")

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        link_entry = [p for p in manifest.files if "link.txt" in p.path][0]
        assert link_entry.symlink_target == target.as_posix()

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

        assert len(manifest.files) == 2
        link_entry = [p for p in manifest.files if "link.txt" in p.path][0]
        assert link_entry.symlink_target == target.as_posix()

    def test_directory_symlink_preserved(self, tmp_path: Path) -> None:
        """Directory symlink is preserved with PRESERVE policy."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")
        link = tmp_path / "link_dir"
        link.symlink_to(subdir)

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        # Link should be in files as a symlink entry
        link_entry = [p for p in manifest.files if p.path == link.as_posix()][0]
        assert link_entry.symlink_target == subdir.as_posix()

        # Contents of link_dir should NOT be collected (symlink not followed)
        paths = {p.path for p in manifest.files}
        assert f"{link.as_posix()}/file.txt" not in paths


class TestSymlinkChains:
    """Tests for symlink chain handling."""

    def test_symlink_chain_preserved_with_preserve_policy(self, tmp_path: Path) -> None:
        """Symlink chain of length 3 is preserved with PRESERVE policy."""
        # Create: link3 -> link2 -> link1 -> target.txt
        target = tmp_path / "target.txt"
        target.write_text("content")
        link1 = tmp_path / "link1.txt"
        link2 = tmp_path / "link2.txt"
        link3 = tmp_path / "link3.txt"

        link1.symlink_to("target.txt")
        link2.symlink_to("link1.txt")
        link3.symlink_to("link2.txt")

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        paths_by_name = {p.path: p for p in manifest.files}
        # Each symlink should point to its immediate target (absolute)
        assert paths_by_name[link1.as_posix()].symlink_target == target.as_posix()
        assert paths_by_name[link2.as_posix()].symlink_target == link1.as_posix()
        assert paths_by_name[link3.as_posix()].symlink_target == link2.as_posix()

    def test_preserve_keeps_all_symlinks_in_chain(self, tmp_path: Path) -> None:
        """PRESERVE policy keeps all symlinks with absolute targets."""
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

        assert target.as_posix() in paths_by_name
        assert link1.as_posix() in paths_by_name
        assert link2.as_posix() in paths_by_name

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
                    reason="Windows cannot follow relative symlinks with '..' components",
                ),
            ),
            pytest.param(True, id="absolute"),
        ],
    )
    def test_preserve_keeps_escaping_symlink_chain(
        self, tmp_path: Path, use_absolute_symlink: bool
    ) -> None:
        """PRESERVE policy keeps escaping symlinks with absolute targets."""
        outside = tmp_path / "outside.txt"
        outside.write_text("outside content")

        root = tmp_path / "root"
        root.mkdir()
        link = root / "link.txt"

        if use_absolute_symlink:
            link.symlink_to(outside)
        else:
            link.symlink_to("../outside.txt")

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        assert link.as_posix() in paths_by_name
        assert paths_by_name[link.as_posix()].symlink_target == outside.as_posix()


class TestCollapsePolicy:
    """Tests for COLLAPSE symlink policy."""

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

        assert isinstance(manifest, AbsSnapshotManifest)
        paths = {p.path for p in manifest.files}
        assert target.as_posix() in paths
        assert link.as_posix() in paths

        # Link should be a regular file entry, not a symlink
        link_entry = [p for p in manifest.files if p.path == link.as_posix()][0]
        assert link_entry.symlink_target is None
        assert link_entry.size == len("content")

    def test_collapse_follows_directory_symlinks(self, tmp_path: Path) -> None:
        """COLLAPSE policy follows directory symlinks."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")
        link = tmp_path / "link_dir"
        link.symlink_to(subdir)

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        paths = {p.path for p in manifest.files}
        # Both the original and the symlink path should have the file
        assert (subdir / "file.txt").as_posix() in paths

    def test_collapse_nested_directory_symlinks(self, tmp_path: Path) -> None:
        """COLLAPSE policy follows nested directory symlinks."""
        # Create: root/link1 -> subdir1, subdir1/link2 -> subdir2, subdir2/file.txt
        subdir2 = tmp_path / "subdir2"
        subdir2.mkdir()
        (subdir2 / "file.txt").write_text("content")

        subdir1 = tmp_path / "subdir1"
        subdir1.mkdir()
        link2 = subdir1 / "link2"
        link2.symlink_to(subdir2)

        root = tmp_path / "root"
        root.mkdir()
        link1 = root / "link1"
        link1.symlink_to(subdir1)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # The file should be accessible through the symlink chain
        paths = {p.path for p in manifest.files}
        # With COLLAPSE, os.walk follows symlinks, so we should see the file
        assert any("file.txt" in p for p in paths)

    def test_collapse_symlink_chain(self, tmp_path: Path) -> None:
        """COLLAPSE policy follows symlink chains to final target."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link1 = tmp_path / "link1.txt"
        link2 = tmp_path / "link2.txt"

        link1.symlink_to(target)
        link2.symlink_to(link1)

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        # All three should be collected as regular files
        paths = {p.path for p in manifest.files}
        assert target.as_posix() in paths
        assert link1.as_posix() in paths
        assert link2.as_posix() in paths

        # All should have the same size (content)
        for entry in manifest.files:
            if entry.path in [target.as_posix(), link1.as_posix(), link2.as_posix()]:
                assert entry.symlink_target is None
                assert entry.size == len("content")


class TestExcludePolicy:
    """Tests for EXCLUDE symlink policy."""

    def test_exclude_skips_file_symlinks(self, tmp_path: Path) -> None:
        """EXCLUDE policy skips file symlinks entirely."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        assert isinstance(manifest, AbsSnapshotManifest)
        paths = {p.path for p in manifest.files}
        assert target.as_posix() in paths
        assert link.as_posix() not in paths

    def test_exclude_skips_directory_symlinks(self, tmp_path: Path) -> None:
        """EXCLUDE policy skips directory symlinks."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")
        link = tmp_path / "link_dir"
        link.symlink_to(subdir)

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.EXCLUDE,
        )

        paths = {p.path for p in manifest.files}
        dir_paths = {d.path for d in manifest.dirs}

        # Original directory and file should be collected
        assert (subdir / "file.txt").as_posix() in paths
        assert subdir.as_posix() in dir_paths

        # Symlink should be excluded
        assert link.as_posix() not in paths
        assert link.as_posix() not in dir_paths

    def test_exclude_logs_callback(self, tmp_path: Path) -> None:
        """EXCLUDE policy logs excluded symlinks via callback."""
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


class TestTransitiveIncludeTargets:
    """Tests for TRANSITIVE_INCLUDE_TARGETS symlink policy."""

    def test_transitive_adds_escaping_file_target(self, tmp_path: Path) -> None:
        """TRANSITIVE_INCLUDE_TARGETS adds escaping symlink file targets."""
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

    def test_transitive_adds_escaping_directory_target(self, tmp_path: Path) -> None:
        """TRANSITIVE_INCLUDE_TARGETS adds escaping symlink directory targets."""
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "file.txt").write_text("content")

        root = tmp_path / "root"
        root.mkdir()
        link = root / "link_dir"
        link.symlink_to(outside_dir)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
        )

        paths = {p.path for p in manifest.files}

        # Symlink should be preserved
        assert link.as_posix() in paths
        link_entry = [p for p in manifest.files if p.path == link.as_posix()][0]
        assert link_entry.symlink_target == outside_dir.as_posix()

        # Target directory contents should be collected
        assert (outside_dir / "file.txt").as_posix() in paths

    def test_transitive_non_escaping_symlink_not_duplicated(self, tmp_path: Path) -> None:
        """Non-escaping symlinks don't duplicate their targets."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
        )

        # Both should be collected, but target only once
        paths = [p.path for p in manifest.files]
        assert paths.count(target.as_posix()) == 1
        assert link.as_posix() in paths


class TestSymlinkTargetIsDirectory:
    """Tests for symlink target type detection."""

    def test_symlink_to_file_detected(self, tmp_path: Path) -> None:
        """Symlink to file is correctly detected as file symlink."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        # Should be in files, not dirs
        file_paths = {p.path for p in manifest.files}
        dir_paths = {d.path for d in manifest.dirs}

        assert link.as_posix() in file_paths
        assert link.as_posix() not in dir_paths

    def test_symlink_to_directory_detected(self, tmp_path: Path) -> None:
        """Symlink to directory is correctly detected as directory symlink."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        link = tmp_path / "link_dir"
        link.symlink_to(subdir)

        manifest = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        # Directory symlinks are stored in files with symlink_target set
        file_paths = {p.path for p in manifest.files}
        assert link.as_posix() in file_paths

        link_entry = [p for p in manifest.files if p.path == link.as_posix()][0]
        assert link_entry.symlink_target == subdir.as_posix()


class TestSymlinkInFilenames:
    """Tests for symlinks passed via filenames parameter."""

    def test_symlink_in_filenames_preserved(self, tmp_path: Path) -> None:
        """Symlink passed via filenames is preserved with PRESERVE policy."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [],
            [link],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        assert len(manifest.files) == 1
        assert manifest.files[0].path == link.as_posix()
        assert manifest.files[0].symlink_target == target.as_posix()

    def test_symlink_in_filenames_collapsed(self, tmp_path: Path) -> None:
        """Symlink passed via filenames is collapsed with COLLAPSE policy."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [],
            [link],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        assert len(manifest.files) == 1
        assert manifest.files[0].path == link.as_posix()
        assert manifest.files[0].symlink_target is None
        assert manifest.files[0].size == len("content")

    def test_symlink_in_optional_filenames(self, tmp_path: Path) -> None:
        """Symlink in optional_filenames is handled correctly."""
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

        assert len(manifest.files) == 1
        assert manifest.files[0].symlink_target == target.as_posix()
