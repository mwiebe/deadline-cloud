# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_manifest core functionality.

These tests cover:
- Basic file hashing
- Metadata preservation (size, mtime, runnable)
- End-to-end with collect_manifest
- Manifest type preservation (snapshot vs diff)
- Progress callbacks
"""

import os
from pathlib import Path
from typing import List

from deadline.job_attachments.asset_manifests._operations import (
    hash_manifest,
    collect_manifest,
)
from deadline.job_attachments.asset_manifests._operations._hash_manifest import (
    _get_or_compute_hash,
)
from deadline.job_attachments.asset_manifests.versions import (
    SymlinkPolicy,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import (
    HashAlgorithm,
    hash_file,
)
from deadline.job_attachments.asset_manifests.manifest import (
    AbsDiffManifest,
    AbsSnapshotManifest,
    ManifestDirectoryPath,
    ManifestFilePath,
)


class TestHashManifestBasic:
    """Tests for basic manifest hashing."""

    def test_hash_single_file(self, tmp_path: Path) -> None:
        """Hashes a single file correctly."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        assert collected.files[0].hash is None

        hashed = hash_manifest(collected)

        assert len(hashed.files) == 1
        assert hashed.files[0].hash is not None
        assert hashed.files[0].hash != ""
        assert len(hashed.files[0].hash) == 32  # XXH128 produces 32 hex chars

    def test_hash_matches_direct_hash(self, tmp_path: Path) -> None:
        """Hash matches direct hash_file() result."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content for hashing")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected)

        expected_hash = hash_file(str(test_file), HashAlgorithm.XXH128)
        assert hashed.files[0].hash == expected_hash

    def test_hash_multiple_files(self, tmp_path: Path) -> None:
        """Hashes multiple files."""
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bbbbb")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected)

        assert len(hashed.files) == 2
        for entry in hashed.files:
            assert entry.hash is not None
            assert entry.hash != ""
            assert len(entry.hash) == 32

    def test_preserves_metadata(self, tmp_path: Path) -> None:
        """Preserves size and mtime from collected manifest."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected)

        assert hashed.files[0].size == collected.files[0].size
        assert hashed.files[0].mtime == collected.files[0].mtime
        assert hashed.files[0].path == collected.files[0].path

    def test_total_size_calculated(self, tmp_path: Path) -> None:
        """Total size is sum of all file sizes."""
        (tmp_path / "a.txt").write_text("aaa")  # 3 bytes
        (tmp_path / "b.txt").write_text("bbbbb")  # 5 bytes

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected)

        assert hashed.totalSize == 8

    def test_hash_algorithm_preserved(self, tmp_path: Path) -> None:
        """Hash algorithm is preserved."""
        (tmp_path / "test.txt").write_text("test")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )
        hashed = hash_manifest(collected)

        assert hashed.hashAlg == HashAlgorithm.XXH128

    def test_preserves_runnable_flag(self, tmp_path: Path) -> None:
        """Preserves runnable flag from collected manifest."""
        test_file = tmp_path / "script.sh"
        test_file.write_text("#!/bin/bash")
        if os.name != "nt":
            test_file.chmod(0o755)

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        hashed = hash_manifest(collected)

        assert hashed.files[0].runnable == collected.files[0].runnable

    def test_returns_abs_snapshot_manifest(self, tmp_path: Path) -> None:
        """Hashing AbsSnapshotManifest returns AbsSnapshotManifest."""
        (tmp_path / "test.txt").write_text("test")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        hashed = hash_manifest(collected)

        assert isinstance(hashed, AbsSnapshotManifest)


class TestGetOrComputeHash:
    """Tests for _get_or_compute_hash helper function."""

    def test_computes_hash_without_cache(self, tmp_path: Path) -> None:
        """Computes hash when no cache provided."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        result = _get_or_compute_hash(
            file_path=test_file,
            cache_key=str(test_file),
            mtime=12345,
            hash_alg=HashAlgorithm.XXH128,
            hash_cache=None,
            force_rehash=False,
        )

        expected = hash_file(str(test_file), HashAlgorithm.XXH128)
        assert result == expected


class TestProgressCallback:
    """Tests for progress callback functionality."""

    def test_callback_called_for_each_file(self, tmp_path: Path) -> None:
        """Progress callback is called for each file."""
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bbb")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE,
        )

        messages: List[str] = []
        hash_manifest(collected, print_function_callback=messages.append)

        assert len(messages) == 2
        assert all("Hashed:" in msg for msg in messages)

    def test_callback_for_symlinks(self, tmp_path: Path) -> None:
        """Progress callback indicates symlinks are not hashed."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"

        link.symlink_to("target.txt")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        messages: List[str] = []
        hash_manifest(collected, print_function_callback=messages.append)

        symlink_msg = [m for m in messages if "link.txt" in m][0]
        assert "Symlink" in symlink_msg or "no hash" in symlink_msg


class TestHashDiffManifest:
    """Tests for hashing diff manifests."""

    def test_diff_manifest_hashes_new_files(self, tmp_path: Path) -> None:
        """Diff manifest with new files gets hashes computed."""
        test_file = tmp_path / "new_file.txt"
        test_file.write_text("new content")
        file_stat = test_file.stat()
        abs_path = str(test_file).replace("\\", "/")

        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
            parent_manifest_hash="parent123",
        )

        hashed = hash_manifest(diff_manifest)

        assert hashed.files[0].hash is not None
        assert hashed.files[0].hash != ""
        assert len(hashed.files[0].hash) == 32
        assert isinstance(hashed, AbsDiffManifest)
        assert hashed.parentManifestHash == "parent123"

    def test_diff_manifest_mixed_entries(self, tmp_path: Path) -> None:
        """Diff manifest with new, modified, and deleted entries."""
        new_file = tmp_path / "new.txt"
        new_file.write_text("new content")
        new_stat = new_file.stat()
        new_path = str(new_file).replace("\\", "/")

        modified_file = tmp_path / "modified.txt"
        modified_file.write_text("modified content")
        mod_stat = modified_file.stat()
        mod_path = str(modified_file).replace("\\", "/")

        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="/new/dir", deleted=False),
                ManifestDirectoryPath(path="/deleted/dir", deleted=True),
            ],
            files=[
                ManifestFilePath(
                    path=new_path,
                    hash=None,
                    size=int(new_stat.st_size),
                    mtime=int(new_stat.st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path=mod_path,
                    hash=None,
                    size=int(mod_stat.st_size),
                    mtime=int(mod_stat.st_mtime_ns // 1000),
                ),
                ManifestFilePath(
                    path="/old/deleted.txt",
                    deleted=True,
                ),
            ],
            total_size=int(new_stat.st_size) + int(mod_stat.st_size),
            parent_manifest_hash="parent789",
        )

        hashed = hash_manifest(diff_manifest)

        assert isinstance(hashed, AbsDiffManifest)
        assert hashed.parentManifestHash == "parent789"

        assert len(hashed.dirs) == 2
        new_dir = next(d for d in hashed.dirs if d.path == "/new/dir")
        deleted_dir = next(d for d in hashed.dirs if d.path == "/deleted/dir")
        assert new_dir.deleted is False
        assert deleted_dir.deleted is True

        assert len(hashed.files) == 3
        new_entry = next(p for p in hashed.files if p.path == new_path)
        assert new_entry.hash != ""
        mod_entry = next(p for p in hashed.files if p.path == mod_path)
        assert mod_entry.hash != ""
        del_entry = next(p for p in hashed.files if p.path == "/old/deleted.txt")
        assert del_entry.deleted is True

    def test_diff_manifest_parent_hash_none_preserved(self, tmp_path: Path) -> None:
        """Diff manifest with no parent hash preserves None."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")
        file_stat = test_file.stat()
        abs_path = str(test_file).replace("\\", "/")

        diff_manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=int(file_stat.st_size),
                    mtime=int(file_stat.st_mtime_ns // 1000),
                )
            ],
            total_size=int(file_stat.st_size),
            parent_manifest_hash=None,
        )

        hashed = hash_manifest(diff_manifest)

        assert isinstance(hashed, AbsDiffManifest)
        assert hashed.parentManifestHash is None


class TestManifestTypePreservation:
    """Tests verifying that manifest types are preserved through hashing."""

    def test_abs_snapshot_returns_abs_snapshot(self, tmp_path: Path) -> None:
        """AbsSnapshotManifest input returns AbsSnapshotManifest output."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")
        stat_info = test_file.stat()

        manifest = AbsSnapshotManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=stat_info.st_size,
                    mtime=int(stat_info.st_mtime_ns // 1000),
                )
            ],
            total_size=stat_info.st_size,
        )

        hashed = hash_manifest(manifest)

        assert isinstance(hashed, AbsSnapshotManifest)

    def test_abs_diff_returns_abs_diff(self, tmp_path: Path) -> None:
        """AbsDiffManifest input returns AbsDiffManifest output."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        abs_path = str(test_file).replace("\\", "/")
        stat_info = test_file.stat()

        manifest = AbsDiffManifest(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=stat_info.st_size,
                    mtime=int(stat_info.st_mtime_ns // 1000),
                )
            ],
            total_size=stat_info.st_size,
            parent_manifest_hash="parent123",
        )

        hashed = hash_manifest(manifest)

        assert isinstance(hashed, AbsDiffManifest)
        assert hashed.parentManifestHash == "parent123"

    def test_snapshot_manifest_type_preserved(self, tmp_path: Path) -> None:
        """Snapshot manifest type is preserved after hashing."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_manifest(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )

        assert isinstance(collected, AbsSnapshotManifest)

        hashed = hash_manifest(collected)

        assert isinstance(hashed, AbsSnapshotManifest)
