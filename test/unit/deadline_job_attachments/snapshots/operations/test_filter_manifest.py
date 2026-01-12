# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for filter_manifest and related functions.

These tests cover:
- Basic filtering with include patterns
- Basic filtering with exclude patterns
- Combined include/exclude patterns
- Filtering for unified manifest classes
- Directory filtering
- Symlink filtering
- Preservation of manifest metadata
- Edge cases (empty patterns, no matches, etc.)
- Custom filter callables
"""

from typing import List

from deadline.job_attachments._snapshots import (
    filter_manifest,
    ManifestFilePath,
    ManifestDirectoryPath,
    AbsSnapshot,
    AbsSnapshotDiff,
    Snapshot,
    SnapshotDiff,
    IncludeExcludePathsFilter,
)
from deadline.job_attachments._snapshots._operations._filter_manifest import (
    _matches_patterns,
    ManifestEntry,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm


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


class TestIncludeExcludePathsFilter:
    """Tests for the IncludeExcludePathsFilter class."""

    def test_empty_patterns_matches_all(self) -> None:
        """Empty patterns match everything."""
        filter_obj = IncludeExcludePathsFilter()
        entry = ManifestFilePath(path="any/path.txt", hash="h1", size=10, mtime=1000)
        assert filter_obj(entry) is True

    def test_include_pattern_matches(self) -> None:
        """Include pattern filters correctly."""
        filter_obj = IncludeExcludePathsFilter(include=["*.blend"])
        entry1 = ManifestFilePath(path="model.blend", hash="h1", size=10, mtime=1000)
        entry2 = ManifestFilePath(path="texture.png", hash="h2", size=20, mtime=2000)
        assert filter_obj(entry1) is True
        assert filter_obj(entry2) is False

    def test_exclude_pattern_matches(self) -> None:
        """Exclude pattern filters correctly."""
        filter_obj = IncludeExcludePathsFilter(exclude=["backup/*"])
        entry1 = ManifestFilePath(path="src/main.py", hash="h1", size=10, mtime=1000)
        entry2 = ManifestFilePath(path="backup/old.py", hash="h2", size=20, mtime=2000)
        assert filter_obj(entry1) is True
        assert filter_obj(entry2) is False

    def test_combined_patterns(self) -> None:
        """Include and exclude patterns work together."""
        filter_obj = IncludeExcludePathsFilter(include=["*.blend"], exclude=["backup/*"])
        entry1 = ManifestFilePath(path="model.blend", hash="h1", size=10, mtime=1000)
        entry2 = ManifestFilePath(path="backup/old.blend", hash="h2", size=20, mtime=2000)
        entry3 = ManifestFilePath(path="texture.png", hash="h3", size=30, mtime=3000)
        assert filter_obj(entry1) is True
        assert filter_obj(entry2) is False
        assert filter_obj(entry3) is False

    def test_filters_directory_entries(self) -> None:
        """Filter works with directory entries."""
        filter_obj = IncludeExcludePathsFilter(include=["src*"])
        dir1 = ManifestDirectoryPath(path="src")
        dir2 = ManifestDirectoryPath(path="backup")
        assert filter_obj(dir1) is True
        assert filter_obj(dir2) is False

    def test_repr(self) -> None:
        """Filter has useful repr."""
        filter_obj = IncludeExcludePathsFilter(include=["*.blend"], exclude=["backup/*"])
        repr_str = repr(filter_obj)
        assert "IncludeExcludePathsFilter" in repr_str
        assert "*.blend" in repr_str
        assert "backup/*" in repr_str


class TestFilterManifestAbsSnapshot:
    """Tests for filtering AbsSnapshot."""

    def _create_manifest(self, files: List[dict], dirs: List[dict] | None = None) -> AbsSnapshot:
        """Helper to create an AbsSnapshot."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            files=file_entries,
            total_size=total_size,
        )

    def test_filter_with_include_pattern(self) -> None:
        """Include pattern filters to matching files only."""
        manifest = self._create_manifest(
            [
                {"path": "/project/model.blend", "hash": "hash1", "size": 100, "mtime": 1000},
                {"path": "/project/texture.png", "hash": "hash2", "size": 200, "mtime": 2000},
                {"path": "/project/notes.txt", "hash": "hash3", "size": 50, "mtime": 3000},
            ]
        )

        filter_obj = IncludeExcludePathsFilter(include=["*.blend"])
        filtered = filter_manifest(manifest, filter_obj)

        assert len(filtered.files) == 1
        assert filtered.files[0].path == "/project/model.blend"
        assert filtered.totalSize == 100

    def test_filter_with_exclude_pattern(self) -> None:
        """Exclude pattern removes matching files."""
        manifest = self._create_manifest(
            [
                {"path": "/src/main.py", "hash": "hash1", "size": 100, "mtime": 1000},
                {"path": "/backup/old.py", "hash": "hash2", "size": 200, "mtime": 2000},
                {"path": "/src/utils.py", "hash": "hash3", "size": 50, "mtime": 3000},
            ]
        )

        filter_obj = IncludeExcludePathsFilter(exclude=["/backup/*"])
        filtered = filter_manifest(manifest, filter_obj)

        assert len(filtered.files) == 2
        paths = {p.path for p in filtered.files}
        assert paths == {"/src/main.py", "/src/utils.py"}
        assert filtered.totalSize == 150

    def test_filter_with_both_patterns(self) -> None:
        """Include and exclude patterns work together."""
        manifest = self._create_manifest(
            [
                {"path": "/project/model.blend", "hash": "hash1", "size": 100, "mtime": 1000},
                {"path": "/backup/old.blend", "hash": "hash2", "size": 200, "mtime": 2000},
                {"path": "/project/texture.png", "hash": "hash3", "size": 50, "mtime": 3000},
            ]
        )

        filter_obj = IncludeExcludePathsFilter(include=["*.blend"], exclude=["/backup/*"])
        filtered = filter_manifest(manifest, filter_obj)

        assert len(filtered.files) == 1
        assert filtered.files[0].path == "/project/model.blend"

    def test_filter_empty_patterns_returns_all(self) -> None:
        """Empty patterns return all entries."""
        manifest = self._create_manifest(
            [
                {"path": "/a.txt", "hash": "hash1", "size": 10, "mtime": 1000},
                {"path": "/b.txt", "hash": "hash2", "size": 20, "mtime": 2000},
            ]
        )

        filter_obj = IncludeExcludePathsFilter()
        filtered = filter_manifest(manifest, filter_obj)

        assert len(filtered.files) == 2

    def test_filter_no_matches_returns_empty(self) -> None:
        """No matches returns empty manifest."""
        manifest = self._create_manifest(
            [
                {"path": "/a.txt", "hash": "hash1", "size": 10, "mtime": 1000},
                {"path": "/b.txt", "hash": "hash2", "size": 20, "mtime": 2000},
            ]
        )

        filter_obj = IncludeExcludePathsFilter(include=["*.blend"])
        filtered = filter_manifest(manifest, filter_obj)

        assert len(filtered.files) == 0
        assert filtered.totalSize == 0

    def test_filter_preserves_hash_algorithm(self) -> None:
        """Filtered manifest preserves hash algorithm."""
        manifest = self._create_manifest(
            [{"path": "/a.txt", "hash": "hash1", "size": 10, "mtime": 1000}]
        )

        filter_obj = IncludeExcludePathsFilter()
        filtered = filter_manifest(manifest, filter_obj)

        assert filtered.hashAlg == HashAlgorithm.XXH128

    def test_filter_preserves_entry_metadata(self) -> None:
        """Filtered entries preserve all metadata."""
        manifest = self._create_manifest(
            [{"path": "/test.txt", "hash": "abc123", "size": 42, "mtime": 1234567890}]
        )

        filter_obj = IncludeExcludePathsFilter()
        filtered = filter_manifest(manifest, filter_obj)

        entry = filtered.files[0]
        assert entry.path == "/test.txt"
        assert entry.hash == "abc123"
        assert entry.size == 42
        assert entry.mtime == 1234567890

    def test_filter_does_not_mutate_original(self) -> None:
        """Filtering creates new manifest, doesn't mutate original."""
        manifest = self._create_manifest(
            [
                {"path": "/keep.txt", "hash": "hash1", "size": 10, "mtime": 1000},
                {"path": "/remove.txt", "hash": "hash2", "size": 20, "mtime": 2000},
            ]
        )
        original_count = len(manifest.files)

        filter_obj = IncludeExcludePathsFilter(include=["/keep.txt"])
        filter_manifest(manifest, filter_obj)

        assert len(manifest.files) == original_count

    def test_returns_abs_snapshot_manifest(self) -> None:
        """Filtering AbsSnapshot returns AbsSnapshot."""
        manifest = self._create_manifest(
            [{"path": "/a.txt", "hash": "h1", "size": 10, "mtime": 1000}]
        )

        filter_obj = IncludeExcludePathsFilter()
        filtered = filter_manifest(manifest, filter_obj)

        assert isinstance(filtered, AbsSnapshot)


class TestFilterManifestRelSnapshot:
    """Tests for filtering Snapshot."""

    def _create_manifest(self, files: List[dict], dirs: List[dict] | None = None) -> Snapshot:
        """Helper to create a Snapshot."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return Snapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            files=file_entries,
            total_size=total_size,
        )

    def test_filter_files_with_include_pattern(self) -> None:
        """Include pattern filters files."""
        manifest = self._create_manifest(
            [
                {"path": "model.blend", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "texture.png", "hash": "h2", "size": 200, "mtime": 2000},
            ]
        )

        filter_obj = IncludeExcludePathsFilter(include=["*.blend"])
        filtered = filter_manifest(manifest, filter_obj)

        assert len(filtered.files) == 1
        assert filtered.files[0].path == "model.blend"

    def test_filter_directories(self) -> None:
        """Directories are filtered by pattern."""
        manifest = self._create_manifest(
            files=[{"path": "src/main.py", "hash": "h1", "size": 100, "mtime": 1000}],
            dirs=[
                {"path": "src"},
                {"path": "backup"},
                {"path": "cache"},
            ],
        )

        filter_obj = IncludeExcludePathsFilter(include=["src*"])
        filtered = filter_manifest(manifest, filter_obj)

        dir_paths = {d.path for d in filtered.dirs}
        assert dir_paths == {"src"}

    def test_filter_symlinks(self) -> None:
        """Symlinks are filtered by their path."""
        manifest = self._create_manifest(
            [
                {"path": "link.blend", "symlink_target": "target.blend"},
                {"path": "link.png", "symlink_target": "target.png"},
            ]
        )

        filter_obj = IncludeExcludePathsFilter(include=["*.blend"])
        filtered = filter_manifest(manifest, filter_obj)

        assert len(filtered.files) == 1
        assert filtered.files[0].path == "link.blend"
        assert filtered.files[0].symlink_target == "target.blend"

    def test_filter_preserves_runnable(self) -> None:
        """Filtered entries preserve runnable flag."""
        manifest = self._create_manifest(
            [{"path": "script.sh", "hash": "h1", "size": 100, "mtime": 1000, "runnable": True}]
        )

        filter_obj = IncludeExcludePathsFilter()
        filtered = filter_manifest(manifest, filter_obj)

        assert filtered.files[0].runnable is True

    def test_filter_preserves_chunkhashes(self) -> None:
        """Filtered entries preserve chunkhashes for large files."""
        manifest = self._create_manifest(
            [
                {
                    "path": "large.bin",
                    "chunkhashes": ["chunk1", "chunk2"],
                    "size": 512 * 1024 * 1024,  # 512MB
                    "mtime": 1000,
                }
            ]
        )

        filter_obj = IncludeExcludePathsFilter()
        filtered = filter_manifest(manifest, filter_obj)

        assert filtered.files[0].chunkhashes == ["chunk1", "chunk2"]

    def test_filter_recalculates_total_size(self) -> None:
        """Total size is recalculated for filtered entries."""
        manifest = self._create_manifest(
            [
                {"path": "keep.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "remove.txt", "hash": "h2", "size": 200, "mtime": 2000},
            ]
        )

        filter_obj = IncludeExcludePathsFilter(include=["keep.txt"])
        filtered = filter_manifest(manifest, filter_obj)

        assert filtered.totalSize == 100

    def test_filter_excludes_symlinks_from_total_size(self) -> None:
        """Symlinks don't contribute to total size."""
        manifest = self._create_manifest(
            [
                {"path": "file.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "link.txt", "symlink_target": "file.txt"},
            ]
        )

        filter_obj = IncludeExcludePathsFilter()
        filtered = filter_manifest(manifest, filter_obj)

        assert filtered.totalSize == 100

    def test_returns_rel_snapshot_manifest(self) -> None:
        """Filtering Snapshot returns Snapshot."""
        manifest = self._create_manifest(
            [{"path": "a.txt", "hash": "h1", "size": 10, "mtime": 1000}]
        )

        filter_obj = IncludeExcludePathsFilter()
        filtered = filter_manifest(manifest, filter_obj)

        assert isinstance(filtered, Snapshot)


class TestFilterManifestAbsDiff:
    """Tests for filtering AbsSnapshotDiff."""

    def _create_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
        parent_hash: str | None = None,
    ) -> AbsSnapshotDiff:
        """Helper to create an AbsSnapshotDiff."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return AbsSnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            files=file_entries,
            total_size=total_size,
            parent_manifest_hash=parent_hash,
        )

    def test_filter_deleted_entries(self) -> None:
        """Deleted entries are filtered by path."""
        manifest = self._create_manifest(
            files=[
                {"path": "/keep.blend", "deleted": True},
                {"path": "/remove.txt", "deleted": True},
            ]
        )

        filter_obj = IncludeExcludePathsFilter(include=["*.blend"])
        filtered = filter_manifest(manifest, filter_obj)

        assert len(filtered.files) == 1
        assert filtered.files[0].path == "/keep.blend"
        assert filtered.files[0].deleted is True

    def test_filter_preserves_parent_hash(self) -> None:
        """Filtered manifest preserves parent manifest hash."""
        manifest = self._create_manifest(
            files=[{"path": "/a.txt", "hash": "h1", "size": 10, "mtime": 1000}],
            parent_hash="parent123",
        )

        filter_obj = IncludeExcludePathsFilter()
        filtered = filter_manifest(manifest, filter_obj)

        assert filtered.parentManifestHash == "parent123"

    def test_filter_excludes_deleted_from_total_size(self) -> None:
        """Deleted entries don't contribute to total size."""
        manifest = self._create_manifest(
            files=[
                {"path": "/existing.txt", "hash": "h1", "size": 100, "mtime": 1000},
                {"path": "/deleted.txt", "deleted": True},
            ]
        )

        filter_obj = IncludeExcludePathsFilter()
        filtered = filter_manifest(manifest, filter_obj)

        assert filtered.totalSize == 100

    def test_returns_abs_diff_snapshots(self) -> None:
        """Filtering AbsSnapshotDiff returns AbsSnapshotDiff."""
        manifest = self._create_manifest(
            files=[{"path": "/a.txt", "hash": "h1", "size": 10, "mtime": 1000}]
        )

        filter_obj = IncludeExcludePathsFilter()
        filtered = filter_manifest(manifest, filter_obj)

        assert isinstance(filtered, AbsSnapshotDiff)


class TestFilterManifestRelDiff:
    """Tests for filtering SnapshotDiff."""

    def _create_manifest(
        self,
        files: List[dict],
        dirs: List[dict] | None = None,
        parent_hash: str | None = None,
    ) -> SnapshotDiff:
        """Helper to create a SnapshotDiff."""
        file_entries = [ManifestFilePath(**f) for f in files]
        dir_entries = [ManifestDirectoryPath(**d) for d in (dirs or [])]
        total_size = sum(
            f.get("size", 0) or 0
            for f in files
            if not f.get("deleted") and not f.get("symlink_target")
        )
        return SnapshotDiff(
            hash_alg=HashAlgorithm.XXH128,
            dirs=dir_entries,
            files=file_entries,
            total_size=total_size,
            parent_manifest_hash=parent_hash,
        )

    def test_filter_deleted_entries(self) -> None:
        """Deleted entries are filtered by path."""
        manifest = self._create_manifest(
            files=[
                {"path": "keep.blend", "deleted": True},
                {"path": "remove.txt", "deleted": True},
            ]
        )

        filter_obj = IncludeExcludePathsFilter(include=["*.blend"])
        filtered = filter_manifest(manifest, filter_obj)

        assert len(filtered.files) == 1
        assert filtered.files[0].path == "keep.blend"
        assert filtered.files[0].deleted is True

    def test_returns_rel_diff_snapshots(self) -> None:
        """Filtering SnapshotDiff returns SnapshotDiff."""
        manifest = self._create_manifest(
            files=[{"path": "a.txt", "hash": "h1", "size": 10, "mtime": 1000}]
        )

        filter_obj = IncludeExcludePathsFilter()
        filtered = filter_manifest(manifest, filter_obj)

        assert isinstance(filtered, SnapshotDiff)


class TestFilterManifestDiffScenarios:
    """Tests for filtering in diff manifest scenarios.

    These tests verify the critical requirement that both parent and current
    manifests must be filtered with the same filter for correct diff computation.
    """

    def test_filter_both_manifests_same_patterns(self) -> None:
        """Filtering both manifests with same filter gives consistent view.

        This is the key scenario for diff computation:
        - Parent has: [model.blend, texture.png, notes.txt]
        - Current has: [model.blend, texture.png, new.blend]
        - Filter: *.blend

        After filtering:
        - Parent: [model.blend]
        - Current: [model.blend, new.blend]

        Diff should show: new.blend added (not texture.png/notes.txt deleted)
        """
        parent = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(path="/model.blend", hash="h1", size=100, mtime=1000),
                ManifestFilePath(path="/texture.png", hash="h2", size=200, mtime=2000),
                ManifestFilePath(path="/notes.txt", hash="h3", size=50, mtime=3000),
            ],
            total_size=350,
        )

        current = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(path="/model.blend", hash="h1", size=100, mtime=1000),
                ManifestFilePath(path="/texture.png", hash="h2", size=200, mtime=2000),
                ManifestFilePath(path="/new.blend", hash="h4", size=150, mtime=4000),
            ],
            total_size=450,
        )

        # Use the same filter for both
        filter_obj = IncludeExcludePathsFilter(include=["*.blend"])
        filtered_parent = filter_manifest(parent, filter_obj)
        filtered_current = filter_manifest(current, filter_obj)

        # Parent should only have model.blend
        parent_paths = {p.path for p in filtered_parent.files}
        assert parent_paths == {"/model.blend"}

        # Current should have model.blend and new.blend
        current_paths = {p.path for p in filtered_current.files}
        assert current_paths == {"/model.blend", "/new.blend"}

    def test_filter_directories_for_diff(self) -> None:
        """Directory filtering works correctly for diff scenarios."""
        parent = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="/src"),
                ManifestDirectoryPath(path="/backup"),
            ],
            files=[],
            total_size=0,
        )

        current = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[
                ManifestDirectoryPath(path="/src"),
                ManifestDirectoryPath(path="/new_dir"),
            ],
            files=[],
            total_size=0,
        )

        filter_obj = IncludeExcludePathsFilter(exclude=["/backup*"])
        filtered_parent = filter_manifest(parent, filter_obj)
        filtered_current = filter_manifest(current, filter_obj)

        parent_dirs = {d.path for d in filtered_parent.dirs}
        current_dirs = {d.path for d in filtered_current.dirs}

        assert parent_dirs == {"/src"}
        assert current_dirs == {"/src", "/new_dir"}


class TestCustomFilterCallables:
    """Tests for custom filter callables beyond IncludeExcludePathsFilter."""

    def test_filter_by_size(self) -> None:
        """Filter files by size threshold."""
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(path="/small.txt", hash="h1", size=100, mtime=1000),
                ManifestFilePath(path="/medium.txt", hash="h2", size=1000, mtime=2000),
                ManifestFilePath(path="/large.txt", hash="h3", size=10000, mtime=3000),
            ],
            total_size=11100,
        )

        def size_filter(entry: ManifestEntry) -> bool:
            if isinstance(entry, ManifestFilePath) and entry.size is not None:
                return entry.size >= 1000
            return True  # Keep directories

        filtered = filter_manifest(manifest, size_filter)

        paths = {p.path for p in filtered.files}
        assert paths == {"/medium.txt", "/large.txt"}

    def test_filter_by_extension_case_insensitive(self) -> None:
        """Custom filter for case-insensitive extension matching."""
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(path="/model.BLEND", hash="h1", size=100, mtime=1000),
                ManifestFilePath(path="/scene.blend", hash="h2", size=200, mtime=2000),
                ManifestFilePath(path="/texture.PNG", hash="h3", size=300, mtime=3000),
            ],
            total_size=600,
        )

        def blend_filter(entry: ManifestEntry) -> bool:
            return entry.path.lower().endswith(".blend")

        filtered = filter_manifest(manifest, blend_filter)

        paths = {p.path for p in filtered.files}
        assert paths == {"/model.BLEND", "/scene.blend"}

    def test_filter_exclude_runnable(self) -> None:
        """Filter to exclude executable files."""
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(path="/script.sh", hash="h1", size=100, mtime=1000, runnable=True),
                ManifestFilePath(path="/data.txt", hash="h2", size=200, mtime=2000),
            ],
            total_size=300,
        )

        def non_executable_filter(entry: ManifestEntry) -> bool:
            if isinstance(entry, ManifestFilePath):
                return not entry.runnable
            return True

        filtered = filter_manifest(manifest, non_executable_filter)

        assert len(filtered.files) == 1
        assert filtered.files[0].path == "/data.txt"

    def test_always_true_filter(self) -> None:
        """Filter that accepts everything."""
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[ManifestDirectoryPath(path="/dir1")],
            files=[ManifestFilePath(path="/file.txt", hash="h1", size=100, mtime=1000)],
            total_size=100,
        )

        def accept_all(entry: ManifestEntry) -> bool:
            return True

        filtered = filter_manifest(manifest, accept_all)

        assert len(filtered.files) == 1
        assert len(filtered.dirs) == 1

    def test_always_false_filter(self) -> None:
        """Filter that rejects everything."""
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[ManifestDirectoryPath(path="/dir1")],
            files=[ManifestFilePath(path="/file.txt", hash="h1", size=100, mtime=1000)],
            total_size=100,
        )

        def reject_all(entry: ManifestEntry) -> bool:
            return False

        filtered = filter_manifest(manifest, reject_all)

        assert len(filtered.files) == 0
        assert len(filtered.dirs) == 0
        assert filtered.totalSize == 0
