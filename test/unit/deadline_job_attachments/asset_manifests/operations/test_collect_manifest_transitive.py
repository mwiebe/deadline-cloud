# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for collect_manifest TRANSITIVE_INCLUDE_TARGETS symlink policy.

These tests cover:
- Transitive file targets
- Transitive directory targets with nested content
- Symlinks within transitive targets (skipped)
- Broken transitive targets
- Multiple transitive targets
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
)
from deadline.job_attachments.asset_manifests.versions import (
    SymlinkPolicy,
)


class TestTransitiveFileTargets:
    """Tests for TRANSITIVE_INCLUDE_TARGETS with file targets."""

    def test_escaping_file_target_collected(self, tmp_path: Path) -> None:
        """Escaping symlink's file target is collected transitively."""
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

        paths = {p.path for p in manifest.files}

        # Symlink should be preserved
        assert link.as_posix() in paths
        link_entry = [p for p in manifest.files if p.path == link.as_posix()][0]
        assert link_entry.symlink_target == outside.as_posix()

        # Target should be collected
        assert outside.as_posix() in paths
        target_entry = [p for p in manifest.files if p.path == outside.as_posix()][0]
        assert target_entry.symlink_target is None
        assert target_entry.size == len("outside content")

    def test_multiple_symlinks_to_same_target(self, tmp_path: Path) -> None:
        """Multiple symlinks to same target only collect target once."""
        outside = tmp_path / "outside.txt"
        outside.write_text("content")

        root = tmp_path / "root"
        root.mkdir()
        link1 = root / "link1.txt"
        link1.symlink_to(outside)
        link2 = root / "link2.txt"
        link2.symlink_to(outside)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
        )

        paths = [p.path for p in manifest.files]

        # Both symlinks should be preserved
        assert link1.as_posix() in paths
        assert link2.as_posix() in paths

        # Target should only appear once
        assert paths.count(outside.as_posix()) == 1

    def test_non_escaping_symlink_target_not_duplicated(self, tmp_path: Path) -> None:
        """Non-escaping symlink doesn't duplicate its target."""
        root = tmp_path / "root"
        root.mkdir()
        target = root / "target.txt"
        target.write_text("content")
        link = root / "link.txt"
        link.symlink_to(target)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
        )

        paths = [p.path for p in manifest.files]

        # Target should only appear once (already in collected set)
        assert paths.count(target.as_posix()) == 1
        assert link.as_posix() in paths


class TestTransitiveDirectoryTargets:
    """Tests for TRANSITIVE_INCLUDE_TARGETS with directory targets."""

    def test_escaping_dir_target_contents_collected(self, tmp_path: Path) -> None:
        """Escaping symlink's directory target contents are collected."""
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "file1.txt").write_text("content1")
        (outside_dir / "file2.txt").write_text("content2")

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
        assert (outside_dir / "file1.txt").as_posix() in paths
        assert (outside_dir / "file2.txt").as_posix() in paths

    def test_deeply_nested_transitive_dir(self, tmp_path: Path) -> None:
        """Deeply nested directory target contents are collected."""
        outside_dir = tmp_path / "outside_dir"
        level1 = outside_dir / "level1"
        level2 = level1 / "level2"
        level2.mkdir(parents=True)
        (level2 / "deep_file.txt").write_text("deep content")
        (outside_dir / "root_file.txt").write_text("root content")

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
        dir_paths = {d.path for d in manifest.dirs}

        # All nested content should be collected under original paths
        assert (outside_dir / "root_file.txt").as_posix() in paths
        assert (level2 / "deep_file.txt").as_posix() in paths

        # Nested directories should be collected
        assert level1.as_posix() in dir_paths
        assert level2.as_posix() in dir_paths

    def test_empty_transitive_dir(self, tmp_path: Path) -> None:
        """Empty directory target is handled correctly."""
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()

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

        # No files from empty directory
        assert not any(outside_dir.as_posix() in p for p in paths if p != link.as_posix())


class TestTransitiveSymlinksInTargets:
    """Tests for symlinks within transitive targets."""

    def test_symlinks_in_transitive_dir_transitively_included(self, tmp_path: Path) -> None:
        """Symlinks within transitive directory targets are transitively included."""
        # Create a file that the nested symlink will point to
        somewhere_else = tmp_path / "somewhere_else.txt"
        somewhere_else.write_text("somewhere else content")

        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "file.txt").write_text("content")
        nested_link = outside_dir / "nested_link.txt"
        nested_link.symlink_to(somewhere_else)

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
        paths_by_name = {p.path: p for p in manifest.files}

        # Regular file should be collected
        assert (outside_dir / "file.txt").as_posix() in paths

        # Nested symlink should be preserved
        assert nested_link.as_posix() in paths
        nested_entry = paths_by_name[nested_link.as_posix()]
        assert nested_entry.symlink_target == somewhere_else.as_posix()

        # Nested symlink's target should be transitively included
        assert somewhere_else.as_posix() in paths
        target_entry = paths_by_name[somewhere_else.as_posix()]
        assert target_entry.symlink_target is None
        assert target_entry.size == len("somewhere else content")

    def test_dir_symlinks_in_transitive_dir_transitively_included(self, tmp_path: Path) -> None:
        """Directory symlinks within transitive targets are transitively included."""
        deep_dir = tmp_path / "deep_dir"
        deep_dir.mkdir()
        (deep_dir / "deep_file.txt").write_text("deep content")

        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "file.txt").write_text("content")
        nested_dir_link = outside_dir / "nested_dir_link"
        nested_dir_link.symlink_to(deep_dir)

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
        paths_by_name = {p.path: p for p in manifest.files}

        # Regular file should be collected
        assert (outside_dir / "file.txt").as_posix() in paths

        # Nested directory symlink should be preserved
        assert nested_dir_link.as_posix() in paths
        nested_entry = paths_by_name[nested_dir_link.as_posix()]
        assert nested_entry.symlink_target == deep_dir.as_posix()

        # Nested directory symlink's target contents should be transitively included
        assert (deep_dir / "deep_file.txt").as_posix() in paths


class TestTransitiveBrokenTargets:
    """Tests for broken transitive targets."""

    def test_broken_transitive_file_target_skipped(self, tmp_path: Path) -> None:
        """Broken symlink's target is skipped with callback message."""
        root = tmp_path / "root"
        root.mkdir()
        link = root / "broken_link.txt"
        nonexistent = tmp_path / "nonexistent.txt"
        link.symlink_to(nonexistent)

        messages: List[str] = []

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
            print_function_callback=lambda msg: messages.append(str(msg)),
        )

        paths = {p.path for p in manifest.files}

        # Symlink should still be preserved (it exists)
        assert link.as_posix() in paths
        link_entry = [p for p in manifest.files if p.path == link.as_posix()][0]
        assert link_entry.symlink_target == nonexistent.as_posix()

        # Nonexistent target should not be in manifest
        assert nonexistent.as_posix() not in paths

        # Should have logged about broken target
        assert any("broken" in msg.lower() or "skipping" in msg.lower() for msg in messages)

    def test_broken_transitive_dir_target_skipped(self, tmp_path: Path) -> None:
        """Broken directory symlink's target is skipped."""
        root = tmp_path / "root"
        root.mkdir()
        link = root / "broken_dir_link"
        nonexistent_dir = tmp_path / "nonexistent_dir"
        link.symlink_to(nonexistent_dir)

        messages: List[str] = []

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
            print_function_callback=lambda msg: messages.append(str(msg)),
        )

        paths = {p.path for p in manifest.files}
        dir_paths = {d.path for d in manifest.dirs}

        # Symlink should still be preserved
        assert link.as_posix() in paths

        # Nonexistent directory should not be in manifest
        assert nonexistent_dir.as_posix() not in dir_paths


class TestTransitiveMultipleTargets:
    """Tests for multiple transitive targets."""

    def test_multiple_escaping_symlinks_different_targets(self, tmp_path: Path) -> None:
        """Multiple escaping symlinks to different targets."""
        outside1 = tmp_path / "outside1.txt"
        outside1.write_text("content1")
        outside2 = tmp_path / "outside2.txt"
        outside2.write_text("content2")

        root = tmp_path / "root"
        root.mkdir()
        link1 = root / "link1.txt"
        link1.symlink_to(outside1)
        link2 = root / "link2.txt"
        link2.symlink_to(outside2)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
        )

        paths = {p.path for p in manifest.files}

        # Both symlinks should be preserved
        assert link1.as_posix() in paths
        assert link2.as_posix() in paths

        # Both targets should be collected
        assert outside1.as_posix() in paths
        assert outside2.as_posix() in paths

    def test_chain_of_escaping_symlinks(self, tmp_path: Path) -> None:
        """Chain of escaping symlinks - all targets collected transitively.

        When a symlink points to another symlink outside the root, the
        immediate target (which is itself a symlink) is preserved and its
        target is also transitively collected.
        """
        outside1 = tmp_path / "outside1.txt"
        outside1.write_text("content1")
        outside2 = tmp_path / "outside2.txt"
        outside2.symlink_to(outside1)

        root = tmp_path / "root"
        root.mkdir()
        link = root / "link.txt"
        link.symlink_to(outside2)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
        )

        paths = {p.path for p in manifest.files}
        paths_by_name = {p.path: p for p in manifest.files}

        # Link should be preserved
        assert link.as_posix() in paths
        link_entry = paths_by_name[link.as_posix()]
        assert link_entry.symlink_target == outside2.as_posix()

        # outside2 (a symlink) should be preserved with its target
        assert outside2.as_posix() in paths
        outside2_entry = paths_by_name[outside2.as_posix()]
        assert outside2_entry.symlink_target == outside1.as_posix()

        # outside1 (the final target) should be collected as a regular file
        assert outside1.as_posix() in paths
        outside1_entry = paths_by_name[outside1.as_posix()]
        assert outside1_entry.symlink_target is None
        assert outside1_entry.size == len("content1")

    def test_deep_chain_of_escaping_symlinks(self, tmp_path: Path) -> None:
        """Deep chain of escaping symlinks - full chain collected recursively.

        Tests that transitive collection works for chains longer than 2 links:
        link -> sym4 -> sym3 -> sym2 -> sym1 -> file.txt

        All symlinks should be preserved and the final file collected.
        """
        # Create the final target file
        final_file = tmp_path / "final_file.txt"
        final_file.write_text("final content")

        # Create a chain of symlinks: sym1 -> sym2 -> sym3 -> sym4 -> final_file
        sym1 = tmp_path / "sym1.txt"
        sym1.symlink_to(final_file)
        sym2 = tmp_path / "sym2.txt"
        sym2.symlink_to(sym1)
        sym3 = tmp_path / "sym3.txt"
        sym3.symlink_to(sym2)
        sym4 = tmp_path / "sym4.txt"
        sym4.symlink_to(sym3)

        root = tmp_path / "root"
        root.mkdir()
        link = root / "link.txt"
        link.symlink_to(sym4)

        manifest = collect_manifest(
            [root],
            [],
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
        )

        paths = {p.path for p in manifest.files}
        paths_by_name = {p.path: p for p in manifest.files}

        # All symlinks in the chain should be preserved
        assert link.as_posix() in paths
        assert paths_by_name[link.as_posix()].symlink_target == sym4.as_posix()

        assert sym4.as_posix() in paths
        assert paths_by_name[sym4.as_posix()].symlink_target == sym3.as_posix()

        assert sym3.as_posix() in paths
        assert paths_by_name[sym3.as_posix()].symlink_target == sym2.as_posix()

        assert sym2.as_posix() in paths
        assert paths_by_name[sym2.as_posix()].symlink_target == sym1.as_posix()

        assert sym1.as_posix() in paths
        assert paths_by_name[sym1.as_posix()].symlink_target == final_file.as_posix()

        # Final file should be collected as a regular file
        assert final_file.as_posix() in paths
        final_entry = paths_by_name[final_file.as_posix()]
        assert final_entry.symlink_target is None
        assert final_entry.size == len("final content")


class TestTransitiveWithFilenames:
    """Tests for TRANSITIVE_INCLUDE_TARGETS with filenames parameter."""

    def test_symlink_in_filenames_transitive(self, tmp_path: Path) -> None:
        """Symlink passed via filenames has target collected transitively."""
        outside = tmp_path / "outside.txt"
        outside.write_text("outside content")

        link = tmp_path / "link.txt"
        link.symlink_to(outside)

        manifest = collect_manifest(
            [],
            [link],
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
        )

        paths = {p.path for p in manifest.files}

        # Symlink should be preserved
        assert link.as_posix() in paths

        # Target should be collected
        assert outside.as_posix() in paths

    def test_dir_symlink_in_filenames_transitive(self, tmp_path: Path) -> None:
        """Directory symlink passed via filenames has contents collected."""
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "file.txt").write_text("content")

        link = tmp_path / "link_dir"
        link.symlink_to(outside_dir)

        manifest = collect_manifest(
            [],
            [link],
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
        )

        paths = {p.path for p in manifest.files}

        # Directory symlink should be preserved and target contents collected
        assert link.as_posix() in paths
        link_entry = [p for p in manifest.files if p.path == link.as_posix()][0]
        assert link_entry.symlink_target == outside_dir.as_posix()

        assert (outside_dir / "file.txt").as_posix() in paths
