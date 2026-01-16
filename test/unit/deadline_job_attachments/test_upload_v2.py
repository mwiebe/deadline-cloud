# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for _upload_v2.py - refactored upload functions using snapshots operations.
"""

import os
from typing import List
from unittest.mock import patch

from deadline.job_attachments._upload_v2 import (
    partition_snapshot_by_storage_profile,
)
from deadline.job_attachments._snapshots import (
    AbsSnapshot,
    ManifestFilePath,
)
from deadline.job_attachments.asset_manifests import HashAlgorithm
from deadline.job_attachments.models import (
    FileSystemLocation,
    FileSystemLocationType,
    StorageProfile,
    StorageProfileOperatingSystemFamily,
)


def _make_abs_snapshot(file_paths: List[str]) -> AbsSnapshot:
    """Helper to create an AbsSnapshot from a list of absolute file paths."""
    files = [
        ManifestFilePath(
            path=p,
            hash="abc123",
            size=100,
            mtime=1000000,
        )
        for p in file_paths
    ]
    return AbsSnapshot(hash_alg=HashAlgorithm.XXH128, files=files, dirs=[])


@patch.object(os, "name", "posix")
class TestPartitionSnapshotByStorageProfilePosix:
    """Tests for partition_snapshot_by_storage_profile on POSIX systems."""

    def test_empty_manifest(self):
        """Empty manifest returns empty list."""
        manifest = AbsSnapshot(hash_alg=HashAlgorithm.XXH128, files=[], dirs=[])
        result = partition_snapshot_by_storage_profile(
            manifest=manifest,
            output_paths=[],
            referenced_paths=[],
        )
        assert result == []

    def test_single_root_no_storage_profile(self):
        """Files under common root without storage profile."""
        manifest = _make_abs_snapshot(
            [
                "/home/user/project/file1.txt",
                "/home/user/project/subdir/file2.txt",
            ]
        )
        result = partition_snapshot_by_storage_profile(
            manifest=manifest,
            output_paths=["/home/user/project/outputs"],
            referenced_paths=[],
        )

        assert len(result) == 1
        assert result[0].root_path == "/home/user/project"
        assert len(result[0].manifest.files) == 2
        assert result[0].outputs == ["outputs"]
        assert result[0].file_system_location_name is None

    def test_local_storage_profile_groups_files(self):
        """Files under LOCAL storage profile location are grouped together."""
        manifest = _make_abs_snapshot(
            [
                "/home/user/movie1/inputs/input1.txt",
                "/home/user/movie1/inputs/input2.txt",
            ]
        )
        storage_profile = StorageProfile(
            storageProfileId="sp-123",
            displayName="Test Profile",
            osFamily=StorageProfileOperatingSystemFamily.LINUX,
            fileSystemLocations=[
                FileSystemLocation(
                    name="Movie 1 - Local",
                    path="/home/user/movie1",
                    type=FileSystemLocationType.LOCAL,
                ),
            ],
        )

        result = partition_snapshot_by_storage_profile(
            manifest=manifest,
            output_paths=["/home/user/movie1/outputs"],
            referenced_paths=[],
            storage_profile=storage_profile,
        )

        assert len(result) == 1
        assert result[0].root_path == "/home/user/movie1"
        assert result[0].file_system_location_name == "Movie 1 - Local"
        assert len(result[0].manifest.files) == 2
        assert result[0].outputs == ["outputs"]

    def test_shared_storage_profile_filters_files(self):
        """Files under SHARED storage profile location are filtered out."""
        manifest = _make_abs_snapshot(
            [
                "/home/user/movie1/input.txt",
                "/mnt/shared/movie1/shared_input.txt",
            ]
        )
        storage_profile = StorageProfile(
            storageProfileId="sp-123",
            displayName="Test Profile",
            osFamily=StorageProfileOperatingSystemFamily.LINUX,
            fileSystemLocations=[
                FileSystemLocation(
                    name="Movie 1 - Local",
                    path="/home/user/movie1",
                    type=FileSystemLocationType.LOCAL,
                ),
                FileSystemLocation(
                    name="Movie 1 - Shared",
                    path="/mnt/shared/movie1",
                    type=FileSystemLocationType.SHARED,
                ),
            ],
        )

        result = partition_snapshot_by_storage_profile(
            manifest=manifest,
            output_paths=["/home/user/movie1/outputs"],
            referenced_paths=[],
            storage_profile=storage_profile,
        )

        assert len(result) == 1
        assert result[0].root_path == "/home/user/movie1"
        # Only the non-shared file should be in the manifest
        assert len(result[0].manifest.files) == 1
        assert result[0].manifest.files[0].path == "input.txt"

    def test_multiple_roots_with_local_and_auto(self):
        """Files split between LOCAL location and auto-determined root."""
        # Use paths where the explicit root is NOT a child of the common prefix
        # of remaining paths (to avoid a known partition_manifest edge case)
        manifest = _make_abs_snapshot(
            [
                "/home/user/movie1/inputs/input1.txt",
                "/data/docs/doc1.txt",
                "/data/extra.txt",
            ]
        )
        storage_profile = StorageProfile(
            storageProfileId="sp-123",
            displayName="Test Profile",
            osFamily=StorageProfileOperatingSystemFamily.LINUX,
            fileSystemLocations=[
                FileSystemLocation(
                    name="Movie 1 - Local",
                    path="/home/user/movie1",
                    type=FileSystemLocationType.LOCAL,
                ),
            ],
        )

        result = partition_snapshot_by_storage_profile(
            manifest=manifest,
            output_paths=["/home/user/movie1/outputs"],
            referenced_paths=[],
            storage_profile=storage_profile,
        )

        # Should have 2 groups: one for movie1 (LOCAL), one for the rest
        assert len(result) == 2

        # Find the movie1 group
        movie1_group = next(g for g in result if g.root_path == "/home/user/movie1")
        assert movie1_group.file_system_location_name == "Movie 1 - Local"
        assert len(movie1_group.manifest.files) == 1
        assert movie1_group.outputs == ["outputs"]

        # Find the auto-determined group
        other_group = next(g for g in result if g.root_path != "/home/user/movie1")
        assert other_group.file_system_location_name is None
        assert len(other_group.manifest.files) == 2

    def test_output_paths_filtered_by_shared(self):
        """Output paths under SHARED locations are filtered out."""
        manifest = _make_abs_snapshot(
            [
                "/home/user/movie1/input.txt",
            ]
        )
        storage_profile = StorageProfile(
            storageProfileId="sp-123",
            displayName="Test Profile",
            osFamily=StorageProfileOperatingSystemFamily.LINUX,
            fileSystemLocations=[
                FileSystemLocation(
                    name="Movie 1 - Local",
                    path="/home/user/movie1",
                    type=FileSystemLocationType.LOCAL,
                ),
                FileSystemLocation(
                    name="Shared Output",
                    path="/mnt/shared/outputs",
                    type=FileSystemLocationType.SHARED,
                ),
            ],
        )

        result = partition_snapshot_by_storage_profile(
            manifest=manifest,
            output_paths=[
                "/home/user/movie1/outputs",
                "/mnt/shared/outputs/render",  # Should be filtered
            ],
            referenced_paths=[],
            storage_profile=storage_profile,
        )

        assert len(result) == 1
        # Only the non-shared output should be included
        assert result[0].outputs == ["outputs"]


@patch.object(os, "name", "nt")
class TestPartitionSnapshotByStorageProfileWindows:
    """Tests for partition_snapshot_by_storage_profile on Windows systems."""

    def test_single_drive_letter_root(self):
        """Files on single drive letter without storage profile."""
        manifest = _make_abs_snapshot(
            [
                "C:/Users/artist/project/file1.txt",
                "C:/Users/artist/project/subdir/file2.txt",
            ]
        )
        result = partition_snapshot_by_storage_profile(
            manifest=manifest,
            output_paths=["C:/Users/artist/project/outputs"],
            referenced_paths=[],
        )

        assert len(result) == 1
        assert result[0].root_path == "C:/Users/artist/project"
        assert len(result[0].manifest.files) == 2
        assert result[0].outputs == ["outputs"]

    def test_multiple_drive_letters(self):
        """Files on multiple drive letters are partitioned by drive."""
        manifest = _make_abs_snapshot(
            [
                "C:/projects/scene/model.blend",
                "D:/assets/textures/wood.png",
                "D:/assets/textures/metal.png",
            ]
        )
        result = partition_snapshot_by_storage_profile(
            manifest=manifest,
            output_paths=["C:/projects/scene/outputs"],
            referenced_paths=[],
        )

        assert len(result) == 2
        c_group = next(g for g in result if g.root_path.startswith("C:"))
        d_group = next(g for g in result if g.root_path.startswith("D:"))

        assert len(c_group.manifest.files) == 1
        assert len(d_group.manifest.files) == 2

    def test_unc_path(self):
        """Files on UNC path are handled correctly."""
        manifest = _make_abs_snapshot(
            [
                "//server/share/project/file1.txt",
                "//server/share/project/file2.txt",
            ]
        )
        result = partition_snapshot_by_storage_profile(
            manifest=manifest,
            output_paths=["//server/share/project/outputs"],
            referenced_paths=[],
        )

        assert len(result) == 1
        assert result[0].root_path == "//server/share/project"
        assert len(result[0].manifest.files) == 2
        assert result[0].outputs == ["outputs"]

    def test_mixed_drive_and_unc(self):
        """Files on both drive letters and UNC paths."""
        manifest = _make_abs_snapshot(
            [
                "C:/projects/scene/model.blend",
                "//render-farm/assets/texture.png",
            ]
        )
        result = partition_snapshot_by_storage_profile(
            manifest=manifest,
            output_paths=["C:/projects/scene/outputs"],
            referenced_paths=[],
        )

        assert len(result) == 2
        c_group = next(g for g in result if g.root_path.startswith("C:"))
        unc_group = next(g for g in result if g.root_path.startswith("//"))

        assert len(c_group.manifest.files) == 1
        assert len(unc_group.manifest.files) == 1

    def test_local_storage_profile_with_drive_letter(self):
        """LOCAL storage profile with drive letter path."""
        manifest = _make_abs_snapshot(
            [
                "C:/Users/artist/movie1/scene.blend",
                "D:/other/file.txt",
            ]
        )
        storage_profile = StorageProfile(
            storageProfileId="sp-123",
            displayName="Test Profile",
            osFamily=StorageProfileOperatingSystemFamily.WINDOWS,
            fileSystemLocations=[
                FileSystemLocation(
                    name="Movie 1 - Local",
                    path="C:/Users/artist/movie1",
                    type=FileSystemLocationType.LOCAL,
                ),
            ],
        )

        result = partition_snapshot_by_storage_profile(
            manifest=manifest,
            output_paths=["C:/Users/artist/movie1/outputs"],
            referenced_paths=[],
            storage_profile=storage_profile,
        )

        assert len(result) == 2
        movie1_group = next(g for g in result if g.root_path == "C:/Users/artist/movie1")
        assert movie1_group.file_system_location_name == "Movie 1 - Local"
        assert len(movie1_group.manifest.files) == 1

    def test_shared_unc_path_filtered(self):
        """Files under SHARED UNC path are filtered out."""
        manifest = _make_abs_snapshot(
            [
                "C:/projects/scene/model.blend",
                "//shared-storage/assets/texture.png",
            ]
        )
        storage_profile = StorageProfile(
            storageProfileId="sp-123",
            displayName="Test Profile",
            osFamily=StorageProfileOperatingSystemFamily.WINDOWS,
            fileSystemLocations=[
                FileSystemLocation(
                    name="Shared Assets",
                    path="//shared-storage/assets",
                    type=FileSystemLocationType.SHARED,
                ),
            ],
        )

        result = partition_snapshot_by_storage_profile(
            manifest=manifest,
            output_paths=["C:/projects/scene/outputs"],
            referenced_paths=[],
            storage_profile=storage_profile,
        )

        assert len(result) == 1
        assert result[0].root_path == "C:/projects/scene"
        assert len(result[0].manifest.files) == 1

    def test_multiple_unc_shares_same_server(self):
        """Different UNC shares on the same server are partitioned separately."""
        manifest = _make_abs_snapshot(
            [
                "//server/share1/project/file1.txt",
                "//server/share1/project/file2.txt",
                "//server/share2/data/file3.txt",
                "//server/share2/data/file4.txt",
            ]
        )
        result = partition_snapshot_by_storage_profile(
            manifest=manifest,
            output_paths=[],
            referenced_paths=[],
        )

        assert len(result) == 2
        share1_group = next(g for g in result if "share1" in g.root_path)
        share2_group = next(g for g in result if "share2" in g.root_path)

        assert share1_group.root_path == "//server/share1/project"
        assert len(share1_group.manifest.files) == 2
        assert share2_group.root_path == "//server/share2/data"
        assert len(share2_group.manifest.files) == 2
