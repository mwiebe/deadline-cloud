# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for _filter_manifest and related functions.

These tests cover:
- Basic filtering with include patterns
- Basic filtering with exclude patterns
- Combined include/exclude patterns
- Filtering for both v2023 and v2025 formats
- Directory filtering (v2025 only)
- Symlink filtering (v2025 only)
- Preservation of manifest metadata
- Edge cases (empty patterns, no matches, etc.)
"""

import pytest
from typing import List

from deadline.job_attachments.asset_manifests._filter_manifest import (
    _filter_manifest,
    _filter_manifest_v2023,
    _filter_manifest_v2025,
    _matches_patterns,
)
from deadline.job_attachments.asset_manifests.versions import (
    ManifestType,
    ManifestVersion,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
from deadline.job_attachments.asset_manifests.v2023_03_03.asset_manifest import (
    AssetManifest as AssetManifest2023,
    ManifestPath as ManifestPath2023,
)
from deadline.job_attachments.asset_manifests.v2025_12_04.asset_manifest import (
    AssetManifest as AssetManifest2025,
    ManifestDirectoryPath as ManifestDirectoryPath2025,
    ManifestFilePath as ManifestFilePath2025,
)


class TestMatchesPatterns:
    """Tests for the _matches_patterns helper function."""

    def test_empty_patterns_matches_all(self) -> None:
        """Empty include and exclude patterns match everything."""
        assert _matches_patterns("any/path.txt", [], []) is True
        assert _matches_patterns("another.blend", [], []) is True

    def test_include_pattern_matches(self) -> None:
        """Path matching include pattern is included."""
        assert _matches_patterns("model.blend", ["*.blend"], []) is True
        assert _matches_patterns("scene.blend", ["*.blend"], []) is True

    def test_include_pattern_no_match(self) -> None:
        """Path not matching include pattern is excluded."""
        assert _matches_patterns("texture.png", ["*.blend"], []) is False
        assert _matches_patterns("notes.txt", ["*.blend"], []) is False

    def test_multiple_include_patterns(self) -> None:
        """Path matching any include pattern is included."""
        patterns = ["*.blend", "*.png"]
        assert _matches_patterns("model.blend", patterns, []) is True
        assert _matches_patterns("texture.png", patterns, []) is True
        assert _matches_patterns("notes.txt", patterns, []) is False

    def test_exclude_pattern_matches(self) -> None:
        """Path matching exclude pattern is excluded."""
        assert _matches_patterns("backup/file.txt", [], ["backup/*"]) is False
        assert _matches_patterns("cache/data.bin", [], ["cache/*"]) is False

    def test_exclude_pattern_no_match(self) -> None:
        """Path not matching exclude pattern is included."""
        assert _matches_patterns("src/file.txt", [], ["backup/*"]) is True

    def test_include_and_exclude_combined(self) -> None:
        """Include and exclude patterns work together."""
        include = ["*.blend"]
        exclude = ["backup/*"]

        # Matches include, not excluded
        assert _matches_patterns("model.blend", include, exclude) is True

        # Matches include, but excluded
        assert _matches_patterns("backup/old.blend", include, exclude) is False

        # Doesn't match include
        assert _matches_patterns("texture.png", include, exclude) is False

    def test_wildcard_patterns(self) -> None:
        """Wildcard patterns work correctly."""
        assert _matches_patterns("subdir/file.txt", ["*/*.txt"], []) is True
        assert _matches_patterns("file.txt", ["*/*.txt"], []) is False

    def test_recursive_wildcard_pattern(self) -> None:
        """Double wildcard patterns for recursive matching."""
        # Note: fnmatch doesn't support ** like glob, so we test single *
        assert _matches_patterns("a/b/c.txt", ["*/*/*.txt"], []) is True


class TestFilterManifestV2023:
    """Tests for v2023-03-03 manifest filtering."""

    def _create_v2023_manifest(
        self, paths: List[tuple[str, str, int, int]]
    ) -> AssetManifest2023:
        """Helper to create a v2023 manifest with given paths.

        Args:
            paths: List of (path, hash, size, mtime) tuples
        """
        entries = [
            ManifestPath2023(path=p, hash=h, size=s, mtime=m) for p, h, s, m in paths
        ]
        total_size = sum(s for _, _, s, _ in paths)
        return AssetManifest2023(
            hash_alg=HashAlgorithm.XXH128,
            paths=entries,
            total_size=total_size,
        )

    def test_filter_with_include_pattern(self) -> None:
        """Include pattern filters to matching files only."""
        manifest = self._create_v2023_manifest([
            ("model.blend", "hash1", 100, 1000),
            ("texture.png", "hash2", 200, 2000),
            ("notes.txt", "hash3", 50, 3000),
        ])

        filtered = _filter_manifest_v2023(manifest, ["*.blend"], [])

        assert len(filtered.paths) == 1
        assert filtered.paths[0].path == "model.blend"
        assert filtered.totalSize == 100

    def test_filter_with_exclude_pattern(self) -> None:
        """Exclude pattern removes matching files."""
        manifest = self._create_v2023_manifest([
            ("src/main.py", "hash1", 100, 1000),
            ("backup/old.py", "hash2", 200, 2000),
            ("src/utils.py", "hash3", 50, 3000),
        ])

        filtered = _filter_manifest_v2023(manifest, [], ["backup/*"])

        assert len(filtered.paths) == 2
        paths = {p.path for p in filtered.paths}
        assert paths == {"src/main.py", "src/utils.py"}
        assert filtered.totalSize == 150

    def test_filter_with_both_patterns(self) -> None:
        """Include and exclude patterns work together."""
        manifest = self._create_v2023_manifest([
            ("model.blend", "hash1", 100, 1000),
            ("backup/old.blend", "hash2", 200, 2000),
            ("texture.png", "hash3", 50, 3000),
        ])

        filtered = _filter_manifest_v2023(manifest, ["*.blend"], ["backup/*"])

        assert len(filtered.paths) == 1
        assert filtered.paths[0].path == "model.blend"

    def test_filter_empty_patterns_returns_all(self) -> None:
        """Empty patterns return all entries."""
        manifest = self._create_v2023_manifest([
            ("a.txt", "hash1", 10, 1000),
            ("b.txt", "hash2", 20, 2000),
        ])

        filtered = _filter_manifest_v2023(manifest, [], [])

        assert len(filtered.paths) == 2

    def test_filter_no_matches_returns_empty(self) -> None:
        """No matches returns empty manifest."""
        manifest = self._create_v2023_manifest([
            ("a.txt", "hash1", 10, 1000),
            ("b.txt", "hash2", 20, 2000),
        ])

        filtered = _filter_manifest_v2023(manifest, ["*.blend"], [])

        assert len(filtered.paths) == 0
        assert filtered.totalSize == 0

    def test_filter_preserves_hash_algorithm(self) -> None:
        """Filtered manifest preserves hash algorithm."""
        manifest = self._create_v2023_manifest([
            ("a.txt", "hash1", 10, 1000),
        ])

        filtered = _filter_manifest_v2023(manifest, [], [])

        assert filtered.hashAlg == HashAlgorithm.XXH128

    def test_filter_preserves_entry_metadata(self) -> None:
        """Filtered entries preserve all metadata."""
        manifest = self._create_v2023_manifest([
            ("test.txt", "abc123", 42, 1234567890),
        ])

        filtered = _filter_manifest_v2023(manifest, [], [])

        entry = filtered.paths[0]
        assert entry.path == "test.txt"
        assert entry.hash == "abc123"
        assert entry.size == 42
        assert entry.mtime == 1234567890

    def test_filter_does_not_mutate_original(self) -> None:
        """Filtering creates new manifest, doesn't mutate original."""
        manifest = self._create_v2023_manifest([
            ("keep.txt", "hash1", 10, 1000),
            ("remove.txt", "hash2", 20, 2000),
        ])
        original_count = len(manifest.paths)

        _filter_manifest_v2023(manifest, ["keep.txt"], [])

        assert len(manifest.paths) == original_count


class TestFilterManifestV2025:
    """Tests for v2025-12-04-beta manifest filtering."""

    def _create_v2025_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
        manifest_type: ManifestType = ManifestType.SNAPSHOT,
        parent_hash: str | None = None,
    ) -> AssetManifest2025:
        """Helper to create a v2025 manifest.

        Args:
            files: List of dicts with file entry fields
            dirs: List of dicts with directory entry fields
            manifest_type: SNAPSHOT or DIFF
            parent_hash: Parent manifest hash for diff manifests
        """
        file_entries = [ManifestFilePath2025(**f) for f in files]
        dir_entries = [ManifestDirectoryPath2025(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            paths=file_entries,
            total_size=total_size,
            manifest_type=manifest_type,
            parent_manifest_hash=parent_hash,
        )

    def test_filter_files_with_include_pattern(self) -> None:
        """Include pattern filters files."""
        manifest = self._create_v2025_manifest([
            {"path": "model.blend", "hash": "h1", "size": 100, "mtime": 1000},
            {"path": "texture.png", "hash": "h2", "size": 200, "mtime": 2000},
        ])

        filtered = _filter_manifest_v2025(manifest, ["*.blend"], [])

        assert len(filtered.paths) == 1
        assert filtered.paths[0].path == "model.blend"

    def test_filter_directories(self) -> None:
        """Directories are filtered by pattern."""
        manifest = self._create_v2025_manifest(
            files=[{"path": "src/main.py", "hash": "h1", "size": 100, "mtime": 1000}],
            dirs=[
                {"path": "src"},
                {"path": "backup"},
                {"path": "cache"},
            ],
        )

        filtered = _filter_manifest_v2025(manifest, ["src*"], [])

        dir_paths = {d.path for d in filtered.dirs}
        assert dir_paths == {"src"}

    def test_filter_symlinks(self) -> None:
        """Symlinks are filtered by their path."""
        manifest = self._create_v2025_manifest([
            {"path": "link.blend", "symlink_target": "target.blend"},
            {"path": "link.png", "symlink_target": "target.png"},
        ])

        filtered = _filter_manifest_v2025(manifest, ["*.blend"], [])

        assert len(filtered.paths) == 1
        assert filtered.paths[0].path == "link.blend"
        assert filtered.paths[0].symlink_target == "target.blend"

    def test_filter_deleted_entries(self) -> None:
        """Deleted entries are filtered by path."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "keep.blend", "deleted": True},
                {"path": "remove.txt", "deleted": True},
            ],
            manifest_type=ManifestType.DIFF,
        )

        filtered = _filter_manifest_v2025(manifest, ["*.blend"], [])

        assert len(filtered.paths) == 1
        assert filtered.paths[0].path == "keep.blend"
        assert filtered.paths[0].deleted is True

    def test_filter_preserves_manifest_type(self) -> None:
        """Filtered manifest preserves manifest type."""
        manifest = self._create_v2025_manifest(
            files=[{"path": "a.txt", "hash": "h1", "size": 10, "mtime": 1000}],
            manifest_type=ManifestType.DIFF,
        )

        filtered = _filter_manifest_v2025(manifest, [], [])

        assert filtered.manifestType == ManifestType.DIFF

    def test_filter_preserves_parent_hash(self) -> None:
        """Filtered manifest preserves parent manifest hash."""
        manifest = self._create_v2025_manifest(
            files=[{"path": "a.txt", "hash": "h1", "size": 10, "mtime": 1000}],
            manifest_type=ManifestType.DIFF,
            parent_hash="parent123",
        )

        filtered = _filter_manifest_v2025(manifest, [], [])

        assert filtered.parentManifestHash == "parent123"

    def test_filter_preserves_runnable(self) -> None:
        """Filtered entries preserve runnable flag."""
        manifest = self._create_v2025_manifest([
            {"path": "script.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": True},
        ])

        filtered = _filter_manifest_v2025(manifest, [], [])

        assert filtered.paths[0].runnable is True

    def test_filter_preserves_chunkhashes(self) -> None:
        """Filtered entries preserve chunkhashes for large files."""
        manifest = self._create_v2025_manifest([
            {
                "path": "large.bin",
                "chunkhashes": ["chunk1", "chunk2"],
                "size": 512 * 1024 * 1024,  # 512MB
                "mtime": 1000,
            },
        ])

        filtered = _filter_manifest_v2025(manifest, [], [])

        assert filtered.paths[0].chunkhashes == ["chunk1", "chunk2"]

    def test_filter_recalculates_total_size(self) -> None:
        """Total size is recalculated for filtered entries."""
        manifest = self._create_v2025_manifest([
            {"path": "keep.txt", "hash": "h1", "size": 100, "mtime": 1000},
            {"path": "remove.txt", "hash": "h2", "size": 200, "mtime": 2000},
        ])

        filtered = _filter_manifest_v2025(manifest, ["keep.txt"], [])

        assert filtered.totalSize == 100

    def test_filter_excludes_symlinks_from_total_size(self) -> None:
        """Symlinks don't contribute to total size."""
        manifest = self._create_v2025_manifest([
            {"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000},
            {"path": "link.txt", "symlink_target": "file.txt"},
        ])

        filtered = _filter_manifest_v2025(manifest, [], [])

        assert filtered.totalSize == 100

    def test_filter_excludes_deleted_from_total_size(self) -> None:
        """Deleted entries don't contribute to total size."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "existing.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "deleted.txt", "deleted": True},
            ],
            manifest_type=ManifestType.DIFF,
        )

        filtered = _filter_manifest_v2025(manifest, [], [])

        assert filtered.totalSize == 100

    def test_filter_does_not_mutate_original(self) -> None:
        """Filtering creates new manifest, doesn't mutate original."""
        manifest = self._create_v2025_manifest(
            files=[
                {"path": "keep.txt", "hash": "h1", "size": 10, "mtime": 1000},
                {"path": "remove.txt", "hash": "h2", "size": 20, "mtime": 2000},
            ],
            dirs=[{"path": "keep"}, {"path": "remove"}],
        )
        original_file_count = len(manifest.paths)
        original_dir_count = len(manifest.dirs)

        _filter_manifest_v2025(manifest, ["keep*"], [])

        assert len(manifest.paths) == original_file_count
        assert len(manifest.dirs) == original_dir_count


class TestFilterManifestDispatch:
    """Tests for the main _filter_manifest dispatch function."""

    def test_dispatch_to_v2023(self) -> None:
        """Version v2023-03-03 dispatches to v2023 implementation."""
        manifest = AssetManifest2023(
            hash_alg=HashAlgorithm.XXH128,
            paths=[ManifestPath2023(path="test.txt", hash="h1", size=10, mtime=1000)],
            total_size=10,
        )

        filtered = _filter_manifest(manifest, ["*.txt"], [])

        assert filtered.manifestVersion == ManifestVersion.v2023_03_03
        assert len(filtered.paths) == 1

    def test_dispatch_to_v2025(self) -> None:
        """Version v2025-12-04-beta dispatches to v2025 implementation."""
        manifest = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[ManifestFilePath2025(path="test.txt", hash="h1", size=10, mtime=1000)],
            total_size=10,
        )

        filtered = _filter_manifest(manifest, ["*.txt"], [])

        assert filtered.manifestVersion == ManifestVersion.v2025_12_04_beta
        assert len(filtered.paths) == 1

    def test_none_patterns_treated_as_empty(self) -> None:
        """None patterns are treated as empty lists."""
        manifest = AssetManifest2023(
            hash_alg=HashAlgorithm.XXH128,
            paths=[ManifestPath2023(path="test.txt", hash="h1", size=10, mtime=1000)],
            total_size=10,
        )

        # Should not raise, and should return all entries
        filtered = _filter_manifest(manifest, None, None)

        assert len(filtered.paths) == 1

    def test_type_error_for_wrong_manifest_type_v2023(self) -> None:
        """TypeError raised if manifest type doesn't match version for v2023."""
        # Create a v2025 manifest but lie about its version
        manifest = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[],
            total_size=0,
        )
        # Manually override version to trigger type check
        manifest.manifestVersion = ManifestVersion.v2023_03_03

        with pytest.raises(TypeError, match="Expected AssetManifest2023"):
            _filter_manifest(manifest, [], [])

    def test_type_error_for_wrong_manifest_type_v2025(self) -> None:
        """TypeError raised if manifest type doesn't match version for v2025."""
        # Create a v2023 manifest but lie about its version
        manifest = AssetManifest2023(
            hash_alg=HashAlgorithm.XXH128,
            paths=[],
            total_size=0,
        )
        # Manually override version to trigger type check
        manifest.manifestVersion = ManifestVersion.v2025_12_04_beta

        with pytest.raises(TypeError, match="Expected AssetManifest2025"):
            _filter_manifest(manifest, [], [])

    def test_unsupported_version_raises(self) -> None:
        """Unsupported version raises ValueError."""
        manifest = AssetManifest2023(
            hash_alg=HashAlgorithm.XXH128,
            paths=[],
            total_size=0,
        )
        manifest.manifestVersion = ManifestVersion.UNDEFINED

        with pytest.raises(ValueError, match="Unsupported manifest version"):
            _filter_manifest(manifest, [], [])


class TestFilterManifestDiffScenarios:
    """Tests for filtering in diff manifest scenarios.

    These tests verify the critical requirement that both parent and current
    manifests must be filtered with the same patterns for correct diff computation.
    """

    def test_filter_both_manifests_same_patterns(self) -> None:
        """Filtering both manifests with same patterns gives consistent view.

        This is the key scenario for diff computation:
        - Parent has: [model.blend, texture.png, notes.txt]
        - Current has: [model.blend, texture.png, new.blend]
        - Filter: *.blend

        After filtering:
        - Parent: [model.blend]
        - Current: [model.blend, new.blend]

        Diff should show: new.blend added (not texture.png/notes.txt deleted)
        """
        parent = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath2025(path="model.blend", hash="h1", size=100, mtime=1000),
                ManifestFilePath2025(path="texture.png", hash="h2", size=200, mtime=2000),
                ManifestFilePath2025(path="notes.txt", hash="h3", size=50, mtime=3000),
            ],
            total_size=350,
        )

        current = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            paths=[
                ManifestFilePath2025(path="model.blend", hash="h1", size=100, mtime=1000),
                ManifestFilePath2025(path="texture.png", hash="h2", size=200, mtime=2000),
                ManifestFilePath2025(path="new.blend", hash="h4", size=150, mtime=4000),
            ],
            total_size=450,
        )

        include = ["*.blend"]
        filtered_parent = _filter_manifest(parent, include, [])
        filtered_current = _filter_manifest(current, include, [])

        # Parent should only have model.blend
        parent_paths = {p.path for p in filtered_parent.paths}
        assert parent_paths == {"model.blend"}

        # Current should have model.blend and new.blend
        current_paths = {p.path for p in filtered_current.paths}
        assert current_paths == {"model.blend", "new.blend"}

        # The diff would correctly show new.blend as added
        # (texture.png and notes.txt are not considered deleted because
        # they were filtered out of both manifests)

    def test_filter_directories_for_diff(self) -> None:
        """Directory filtering works correctly for diff scenarios."""
        parent = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath2025(path="src"),
                ManifestDirectoryPath2025(path="backup"),
            ],
            paths=[],
            total_size=0,
        )

        current = AssetManifest2025(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath2025(path="src"),
                ManifestDirectoryPath2025(path="new_dir"),
            ],
            paths=[],
            total_size=0,
        )

        exclude = ["backup*"]
        filtered_parent = _filter_manifest(parent, [], exclude)
        filtered_current = _filter_manifest(current, [], exclude)

        parent_dirs = {d.path for d in filtered_parent.dirs}
        current_dirs = {d.path for d in filtered_current.dirs}

        assert parent_dirs == {"src"}
        assert current_dirs == {"src", "new_dir"}
