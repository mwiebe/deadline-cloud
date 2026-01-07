# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for collect_manifest COLLAPSE_ESCAPING symlink policy.

These tests cover:
- Non-escaping symlinks (preserved)
- Escaping file symlinks (collapsed)
- Escaping directory symlinks (collapsed with contents inlined)
- Mixed escaping and non-escaping scenarios
- Symlink chains with partial escaping
- Edge cases (broken symlinks, nested symlinks in collapsed dirs)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

import pytest

from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
)
from deadline.job_attachments.asset_manifests.versions import (
    SymlinkPolicy,
)


class TestCollapseEscapingBasics:
    """Basic tests for COLLAPSE_ESCAPING symlink policy."""

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

        assert target.as_posix() in paths_by_name
        assert link.as_posix() in paths_by_name

        # Link should be preserved as symlink (target is in collected set)
        link_entry = paths_by_name[link.as_posix()]
        assert link_entry.symlink_target == target.as_posix()

    def test_escaping_file_symlink_collapsed(self, tmp_path: Path) -> None:
        """Symlink to file outside collected paths is collapsed to file."""
        outside = tmp_path / "outside.txt"
        outside.write_text("outside content")

        root = tmp_path / "root"
        root.mkdir()
        link = root / "link.txt"
        link.symlink_to(outside)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # Link should be collapsed (no symlink_target)
        assert link.as_posix() in paths_by_name
        link_entry = paths_by_name[link.as_posix()]
        assert link_entry.symlink_target is None
        assert link_entry.size == len("outside content")

        # outside.txt should NOT be in manifest
        assert outside.as_posix() not in paths_by_name

    def test_escaping_dir_symlink_collapsed(self, tmp_path: Path) -> None:
        """Symlink to directory outside collected paths is collapsed."""
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "file.txt").write_text("content")

        root = tmp_path / "root"
        root.mkdir()
        link_dir = root / "link_dir"
        link_dir.symlink_to(outside_dir)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths = {p.path for p in manifest.files}
        dir_paths = {d.path for d in manifest.dirs}

        # The symlink dir should be collapsed - contents appear under link_dir path
        assert f"{link_dir.as_posix()}/file.txt" in paths

        # The directory entry should exist
        assert link_dir.as_posix() in dir_paths

        # outside_dir should NOT be in manifest
        assert outside_dir.as_posix() not in dir_paths
        assert f"{outside_dir.as_posix()}/file.txt" not in paths

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


class TestCollapseEscapingMixed:
    """Tests for mixed escaping and non-escaping scenarios."""

    def test_mixed_escaping_and_non_escaping(self, tmp_path: Path) -> None:
        """Mix of escaping and non-escaping symlinks handled correctly."""
        outside = tmp_path / "outside.txt"
        outside.write_text("outside")

        root = tmp_path / "root"
        root.mkdir()
        internal = root / "internal.txt"
        internal.write_text("internal")
        link_internal = root / "link_internal.txt"
        link_internal.symlink_to("internal.txt")
        link_outside = root / "link_outside.txt"
        link_outside.symlink_to(outside)

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
        """Symlink to sibling directory (both collected) is preserved."""
        dir1 = tmp_path / "dir1"
        dir1.mkdir()
        (dir1 / "file1.txt").write_text("content1")

        dir2 = tmp_path / "dir2"
        dir2.mkdir()
        link_to_dir1 = dir2 / "link_to_dir1"
        link_to_dir1.symlink_to(dir1)

        manifest = collect_manifest(
            [dir1, dir2],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # Symlink should be preserved (target dir1 is in collected set)
        assert link_to_dir1.as_posix() in paths_by_name
        assert paths_by_name[link_to_dir1.as_posix()].symlink_target == dir1.as_posix()


class TestCollapseEscapingChains:
    """Tests for COLLAPSE_ESCAPING with symlink chains."""

    def test_chain_in_in_file_in(self, tmp_path: Path) -> None:
        """Chain: symlink [IN] -> symlink [IN] -> file [IN] - all preserved."""
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

        assert target_file.as_posix() in paths_by_name
        assert link1.as_posix() in paths_by_name
        assert link2.as_posix() in paths_by_name

        # Both symlinks should be preserved (targets are IN)
        assert paths_by_name[link1.as_posix()].symlink_target == target_file.as_posix()
        assert paths_by_name[link2.as_posix()].symlink_target == link1.as_posix()

    def test_chain_in_in_dir_in(self, tmp_path: Path) -> None:
        """Chain: symlink [IN] -> symlink [IN] -> directory [IN] - all preserved."""
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
        """Chain: symlink [IN] -> symlink [IN] -> file [OUT]."""
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

        # link1 points to OUT -> collapsed
        assert link1.as_posix() in paths_by_name
        assert paths_by_name[link1.as_posix()].symlink_target is None
        assert paths_by_name[link1.as_posix()].size == len("outside content")

        # link2 points to link1 (IN) -> preserved as symlink
        assert link2.as_posix() in paths_by_name
        assert paths_by_name[link2.as_posix()].symlink_target == link1.as_posix()

    def test_chain_in_in_dir_out(self, tmp_path: Path) -> None:
        """Chain: symlink [IN] -> symlink [IN] -> directory [OUT]."""
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
        """Chain: symlink [IN] -> symlink [OUT] -> file [IN]."""
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
        assert link2.as_posix() in paths_by_name
        assert paths_by_name[link2.as_posix()].symlink_target is None
        assert paths_by_name[link2.as_posix()].size == len("content")

        # outside_link should NOT be in manifest
        assert outside_link.as_posix() not in paths_by_name

    def test_chain_in_out_dir_in(self, tmp_path: Path) -> None:
        """Chain: symlink [IN] -> symlink [OUT] -> directory [IN]."""
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
        assert link2.as_posix() in dir_paths
        assert f"{link2.as_posix()}/file.txt" in paths_by_name

        # outside_link should NOT be in manifest
        assert outside_link.as_posix() not in paths_by_name
        assert outside_link.as_posix() not in dir_paths

    def test_symlink_chain_partial_escape(self, tmp_path: Path) -> None:
        """Symlink chain where intermediate link is non-escaping but final target escapes."""
        outside = tmp_path / "outside.txt"
        outside.write_text("outside")

        root = tmp_path / "root"
        root.mkdir()
        link2 = root / "link2.txt"
        link2.symlink_to(outside)
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


class TestCollapseEscapingEdgeCases:
    """Edge cases for COLLAPSE_ESCAPING policy."""

    def test_broken_escaping_file_symlink_skipped(self, tmp_path: Path) -> None:
        """Broken escaping file symlink is skipped."""
        root = tmp_path / "root"
        root.mkdir()
        link = root / "broken_link.txt"
        link.symlink_to(tmp_path / "nonexistent.txt")

        messages: List[str] = []

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
            print_function_callback=lambda msg: messages.append(str(msg)),
        )

        paths = {p.path for p in manifest.files}
        # Broken symlink should be skipped when trying to collapse
        # (can't read content from nonexistent target)
        assert link.as_posix() not in paths or any(
            "broken" in msg.lower() or "skipping" in msg.lower() for msg in messages
        )

    def test_broken_escaping_dir_symlink_skipped(self, tmp_path: Path) -> None:
        """Broken escaping directory symlink is skipped."""
        root = tmp_path / "root"
        root.mkdir()
        link = root / "broken_dir_link"
        link.symlink_to(tmp_path / "nonexistent_dir")

        messages: List[str] = []

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
            print_function_callback=lambda msg: messages.append(str(msg)),
        )

        dir_paths = {d.path for d in manifest.dirs}
        paths = {p.path for p in manifest.files}

        # Broken symlink should be skipped
        assert link.as_posix() not in dir_paths
        assert link.as_posix() not in paths

    def test_nested_symlinks_in_collapsed_dir_escaping_collapsed(self, tmp_path: Path) -> None:
        """Nested symlinks within collapsed directory that escape are collapsed.

        When collapsing an escaping directory symlink, any nested symlinks inside
        that directory which point outside the collapsed directory should also be
        collapsed (their target content inlined), not skipped.
        """
        # Create a file outside the collapsed directory that the nested symlink points to
        somewhere_else = tmp_path / "somewhere_else.txt"
        somewhere_else.write_text("external content")

        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "file.txt").write_text("content")
        # This symlink points OUTSIDE the collapsed directory
        nested_link = outside_dir / "nested_link.txt"
        nested_link.symlink_to(somewhere_else)

        root = tmp_path / "root"
        root.mkdir()
        link_dir = root / "link_dir"
        link_dir.symlink_to(outside_dir)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # Regular file should be collected
        assert f"{link_dir.as_posix()}/file.txt" in paths_by_name

        # Nested escaping symlink should be collapsed (not skipped)
        collapsed_nested = f"{link_dir.as_posix()}/nested_link.txt"
        assert collapsed_nested in paths_by_name
        entry = paths_by_name[collapsed_nested]
        # Should be collapsed - no symlink_target, has size
        assert entry.symlink_target is None
        assert entry.size == len("external content")

    def test_nested_symlinks_in_collapsed_dir_internal_preserved(self, tmp_path: Path) -> None:
        """Nested symlinks within collapsed directory that stay internal are preserved.

        When collapsing an escaping directory symlink, any symlinks inside that
        directory which point to other files within the same directory should be
        preserved with their targets translated to the collapsed location.
        """
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        target_file = outside_dir / "target.txt"
        target_file.write_text("target content")
        # This symlink points to a file WITHIN the same directory being collapsed
        internal_link = outside_dir / "internal_link.txt"
        internal_link.symlink_to("target.txt")

        root = tmp_path / "root"
        root.mkdir()
        link_dir = root / "link_dir"
        link_dir.symlink_to(outside_dir)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # Target file should be collected under collapsed path
        collapsed_target = f"{link_dir.as_posix()}/target.txt"
        assert collapsed_target in paths_by_name
        assert paths_by_name[collapsed_target].symlink_target is None
        assert paths_by_name[collapsed_target].size == len("target content")

        # Internal symlink should be preserved with translated target
        collapsed_link = f"{link_dir.as_posix()}/internal_link.txt"
        assert collapsed_link in paths_by_name
        link_entry = paths_by_name[collapsed_link]
        # The symlink target should be translated to the collapsed location
        assert link_entry.symlink_target == collapsed_target

    def test_escaping_symlink_to_file_not_dir(self, tmp_path: Path) -> None:
        """Escaping symlink that looks like dir but points to file."""
        outside_file = tmp_path / "outside_file.txt"
        outside_file.write_text("content")

        root = tmp_path / "root"
        root.mkdir()
        # Create symlink without extension (might look like dir)
        link = root / "link_to_file"
        link.symlink_to(outside_file)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # Should be collapsed as a file
        assert link.as_posix() in paths_by_name
        assert paths_by_name[link.as_posix()].symlink_target is None
        assert paths_by_name[link.as_posix()].size == len("content")

    def test_deeply_nested_escaping_dir(self, tmp_path: Path) -> None:
        """Escaping directory symlink with deeply nested content."""
        outside_dir = tmp_path / "outside_dir"
        level1 = outside_dir / "level1"
        level2 = level1 / "level2"
        level2.mkdir(parents=True)
        (level2 / "deep_file.txt").write_text("deep content")

        root = tmp_path / "root"
        root.mkdir()
        link_dir = root / "link_dir"
        link_dir.symlink_to(outside_dir)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths = {p.path for p in manifest.files}
        dir_paths = {d.path for d in manifest.dirs}

        # All nested content should be inlined under link_dir
        assert f"{link_dir.as_posix()}/level1/level2/deep_file.txt" in paths
        assert f"{link_dir.as_posix()}/level1" in dir_paths
        assert f"{link_dir.as_posix()}/level1/level2" in dir_paths

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
    def test_relative_vs_absolute_escaping_symlink(
        self, tmp_path: Path, use_absolute_symlink: bool
    ) -> None:
        """Both relative and absolute escaping symlinks are collapsed."""
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
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        paths_by_name = {p.path: p for p in manifest.files}

        # Should be collapsed regardless of relative/absolute
        assert link.as_posix() in paths_by_name
        assert paths_by_name[link.as_posix()].symlink_target is None
        assert paths_by_name[link.as_posix()].size == len("outside content")
