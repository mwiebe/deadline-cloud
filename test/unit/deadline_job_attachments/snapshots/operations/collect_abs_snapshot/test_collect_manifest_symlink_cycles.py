# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for symlink cycle handling in collect_abs_snapshot.

These tests verify that symlink cycles are detected and handled gracefully
without causing infinite recursion. Cycles can occur in various scenarios:
- Direct cycle: A -> B -> A (length 2)
- Self-referential: A -> A (length 1)
- Longer cycles: A -> B -> C -> A (length 3+)

The expected behavior is:
- Cycles are detected and logged as warnings
- The cyclic symlink is skipped to prevent infinite recursion
- Non-cyclic parts of the directory tree are still collected
"""

import os
from pathlib import Path

import pytest

from deadline.job_attachments._snapshots import (
    collect_abs_snapshot,
    SymlinkPolicy,
)


@pytest.fixture
def symlink_cycle_dir(tmp_path: Path) -> Path:
    """Create a directory with various symlink cycle configurations."""
    root = tmp_path / "root"
    root.mkdir()
    return root


class TestSymlinkCycleDetection:
    """Tests for symlink cycle detection during collection."""

    @pytest.mark.skipif(os.name == "nt", reason="Symlinks require special permissions on Windows")
    def test_self_referential_symlink_cycle(self, symlink_cycle_dir: Path) -> None:
        """A symlink pointing to itself (A -> A) is detected and skipped."""
        # Create: link_a -> link_a (self-referential)
        link_a = symlink_cycle_dir / "link_a"
        link_a.symlink_to(link_a)

        # Also create a regular file to ensure collection works
        (symlink_cycle_dir / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [symlink_cycle_dir],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ALL,
        )

        # The regular file should be collected
        file_paths = {e.path for e in manifest.files}
        assert (symlink_cycle_dir / "file.txt").as_posix() in file_paths

        # The self-referential symlink should NOT cause infinite recursion
        # and should be skipped (not in manifest as a file entry)
        # Note: With COLLAPSE_ALL, broken/cyclic symlinks are skipped

    @pytest.mark.skipif(os.name == "nt", reason="Symlinks require special permissions on Windows")
    def test_two_symlink_cycle(self, symlink_cycle_dir: Path) -> None:
        """A cycle of length 2 (A -> B -> A) is detected and skipped."""
        # Create two directories that will form a cycle
        dir_a = symlink_cycle_dir / "dir_a"
        dir_b = symlink_cycle_dir / "dir_b"
        dir_a.mkdir()
        dir_b.mkdir()

        # Create files in each directory
        (dir_a / "file_a.txt").write_text("content a")
        (dir_b / "file_b.txt").write_text("content b")

        # Create cycle: dir_a/link_to_b -> dir_b, dir_b/link_to_a -> dir_a
        (dir_a / "link_to_b").symlink_to(dir_b)
        (dir_b / "link_to_a").symlink_to(dir_a)

        manifest = collect_abs_snapshot(
            [symlink_cycle_dir],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ALL,
        )

        # Both regular files should be collected
        file_paths = {e.path for e in manifest.files}
        assert (dir_a / "file_a.txt").as_posix() in file_paths
        assert (dir_b / "file_b.txt").as_posix() in file_paths

        # The cycle should be detected and not cause infinite recursion

    @pytest.mark.skipif(os.name == "nt", reason="Symlinks require special permissions on Windows")
    def test_three_symlink_cycle(self, symlink_cycle_dir: Path) -> None:
        """A cycle of length 3 (A -> B -> C -> A) is detected and skipped."""
        # Create three directories
        dir_a = symlink_cycle_dir / "dir_a"
        dir_b = symlink_cycle_dir / "dir_b"
        dir_c = symlink_cycle_dir / "dir_c"
        dir_a.mkdir()
        dir_b.mkdir()
        dir_c.mkdir()

        # Create files in each
        (dir_a / "file_a.txt").write_text("content a")
        (dir_b / "file_b.txt").write_text("content b")
        (dir_c / "file_c.txt").write_text("content c")

        # Create cycle: A -> B -> C -> A
        (dir_a / "link_to_b").symlink_to(dir_b)
        (dir_b / "link_to_c").symlink_to(dir_c)
        (dir_c / "link_to_a").symlink_to(dir_a)

        manifest = collect_abs_snapshot(
            [symlink_cycle_dir],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ALL,
        )

        # All regular files should be collected
        file_paths = {e.path for e in manifest.files}
        assert (dir_a / "file_a.txt").as_posix() in file_paths
        assert (dir_b / "file_b.txt").as_posix() in file_paths
        assert (dir_c / "file_c.txt").as_posix() in file_paths

    @pytest.mark.skipif(os.name == "nt", reason="Symlinks require special permissions on Windows")
    def test_cycle_with_collapse_escaping(self, symlink_cycle_dir: Path) -> None:
        """Cycles are detected with COLLAPSE_ESCAPING policy."""
        # Create an external directory that will form a cycle back
        external = symlink_cycle_dir.parent / "external"
        external.mkdir()
        (external / "external_file.txt").write_text("external content")

        # Create cycle: root/link_to_external -> external, external/link_to_root -> root
        (symlink_cycle_dir / "link_to_external").symlink_to(external)
        (external / "link_to_root").symlink_to(symlink_cycle_dir)

        # Also create a regular file
        (symlink_cycle_dir / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [symlink_cycle_dir],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
        )

        # The regular file should be collected
        file_paths = {e.path for e in manifest.files}
        assert (symlink_cycle_dir / "file.txt").as_posix() in file_paths

        # The external file should be collected (via collapsed escaping symlink)
        # but the cycle back should be detected and skipped

    @pytest.mark.skipif(os.name == "nt", reason="Symlinks require special permissions on Windows")
    def test_cycle_with_transitive_include(self, symlink_cycle_dir: Path) -> None:
        """Cycles are detected with TRANSITIVE_INCLUDE_TARGETS policy."""
        # Create an external directory
        external = symlink_cycle_dir.parent / "external"
        external.mkdir()
        (external / "external_file.txt").write_text("external content")

        # Create cycle via transitive targets
        (symlink_cycle_dir / "link_to_external").symlink_to(external)
        (external / "link_back").symlink_to(symlink_cycle_dir / "link_to_external")

        # Also create a regular file
        (symlink_cycle_dir / "file.txt").write_text("content")

        manifest = collect_abs_snapshot(
            [symlink_cycle_dir],
            [],
            symlink_policy=SymlinkPolicy.TRANSITIVE_INCLUDE_TARGETS,
        )

        # The regular file should be collected
        file_paths = {e.path for e in manifest.files}
        assert (symlink_cycle_dir / "file.txt").as_posix() in file_paths

        # The symlink should be preserved
        symlink_entries = [e for e in manifest.files if e.symlink_target is not None]
        assert len(symlink_entries) >= 1

    @pytest.mark.skipif(os.name == "nt", reason="Symlinks require special permissions on Windows")
    def test_long_cycle_chain(self, symlink_cycle_dir: Path) -> None:
        """A longer cycle (5 directories) is detected and skipped."""
        # Create 5 directories forming a cycle
        dirs = []
        for i in range(5):
            d = symlink_cycle_dir / f"dir_{i}"
            d.mkdir()
            (d / f"file_{i}.txt").write_text(f"content {i}")
            dirs.append(d)

        # Create cycle: 0 -> 1 -> 2 -> 3 -> 4 -> 0
        for i in range(5):
            next_i = (i + 1) % 5
            (dirs[i] / f"link_to_{next_i}").symlink_to(dirs[next_i])

        manifest = collect_abs_snapshot(
            [symlink_cycle_dir],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ALL,
        )

        # All regular files should be collected
        file_paths = {e.path for e in manifest.files}
        for i in range(5):
            assert (dirs[i] / f"file_{i}.txt").as_posix() in file_paths


class TestSymlinkCycleWithPreserve:
    """Tests for cycle handling with PRESERVE policy (no recursion needed)."""

    @pytest.mark.skipif(os.name == "nt", reason="Symlinks require special permissions on Windows")
    def test_cycle_preserved_as_symlinks(self, symlink_cycle_dir: Path) -> None:
        """With PRESERVE policy, cyclic symlinks are kept as symlink entries."""
        dir_a = symlink_cycle_dir / "dir_a"
        dir_b = symlink_cycle_dir / "dir_b"
        dir_a.mkdir()
        dir_b.mkdir()

        # Create cycle
        (dir_a / "link_to_b").symlink_to(dir_b)
        (dir_b / "link_to_a").symlink_to(dir_a)

        manifest = collect_abs_snapshot(
            [symlink_cycle_dir],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        # Both symlinks should be preserved (no recursion with PRESERVE)
        symlink_entries = {e.path: e.symlink_target for e in manifest.files if e.symlink_target}
        assert (dir_a / "link_to_b").as_posix() in symlink_entries
        assert (dir_b / "link_to_a").as_posix() in symlink_entries
