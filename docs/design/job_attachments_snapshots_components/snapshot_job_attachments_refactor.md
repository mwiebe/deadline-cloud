# Refactoring the Existing Job Attachments Implementation

[Job Attachments Snapshots](../job_attachments_snapshots.md) · Refactoring Guide

This document describes how to refactor the existing job attachments implementation to use the new snapshots library.

## Conversions for the BaseAssetManifest class - DONE

The snapshots design uses new, independent manifest classes. Conversion functions in
`_snapshots/_convert_v2023_manifest.py` convert between `BaseAssetManifest` and `Snapshot`/`SnapshotDiff`:

| Function | Description |
|----------|-------------|
| `snapshot_to_v2023_manifest(snapshot)` | Converts `Snapshot` → `AssetManifest`. Collapses symlinks via `COLLAPSE_ALL`, drops empty dirs with warning. |
| `snapshot_diff_to_v2023_manifest(snapshot_diff)` | Converts `SnapshotDiff` → `AssetManifest`. Collapses symlinks via `COLLAPSE_ALL`, drops deletions and empty dirs with warnings. |
| `v2023_manifest_to_snapshot(manifest)` | Converts `AssetManifest` → `Snapshot`. Sets `fileChunkSizeBytes=WHOLE_FILE_CHUNK_SIZE`. |
| `v2023_manifest_to_snapshot_diff(manifest, parent_hash)` | Converts `AssetManifest` → `SnapshotDiff`. |

```python
from deadline.job_attachments._snapshots import (
    snapshot_to_v2023_manifest,
    v2023_manifest_to_snapshot,
)

# Convert snapshot to v2023 for serialization
v2023_manifest = snapshot_to_v2023_manifest(hashed_snapshot)
manifest_json = v2023_manifest.encode()

# Convert v2023 back to snapshot for processing
snapshot = v2023_manifest_to_snapshot(v2023_manifest)
```

Note: The v2023 format is lossy—it cannot represent symlinks, empty directories, deletions, or chunked files.

## upload.py S3AssetManager.prepare_paths_for_upload() - DONE

The new `partition_snapshot_by_storage_profile()` function in `_upload_v2.py` takes an already-collected
`AbsSnapshot`, filters out SHARED storage profile locations, and partitions by LOCAL locations using
`filter_manifest()` and `partition_manifest()`.

```python
from deadline.job_attachments._upload_v2 import partition_snapshot_by_storage_profile
from deadline.job_attachments._snapshots import collect_abs_snapshot

# Collect inputs into an AbsSnapshot
abs_snapshot = collect_abs_snapshot(
    directories=["/home/user/movie1/assets"],
    filenames=["/home/user/movie1/scene.blend"],
)

# Partition by storage profile locations
groups = partition_snapshot_by_storage_profile(
    manifest=abs_snapshot,
    output_paths=["/home/user/movie1/outputs"],
    referenced_paths=[],
    storage_profile=storage_profile,  # From queue configuration
)

# Each group has: root_path, manifest (relative paths), outputs, file_system_location_name
for group in groups:
    print(f"Root: {group.root_path}, Files: {len(group.manifest.files)}")
```

## upload.py S3AssetUploader.upload_input_files()

We can replace this with `hash_upload_abs_manifest()` using an `S3DataCache`. If we refactor this
early, we can use `join_manifest()` and `manifest.clear_hashes()` to adapt the relative paths and
remove the hashes. Ideally we refactor this later, so that we can call `hash_upload_abs_manifest()`
once on the entire dataset that we're uploading all at once.

## upload.py S3AssetManager._create_manifest_file()

This function collects and hashes files for a single root of a `deadline bundle submit` operation,
into a relative-path manifest. While this could be achieved with `collect_abs_snapshot()`
followed by `hash_abs_manifest()`, we don't want to refactor it this way. We want our `collect_abs_snapshot()`
to happen earlier in the job submission flow, and use `partition_manifest()` to determine
the groupings for each root. Therefore refactoring this function comes later.

## upload.py S3AssetUploader._snapshot_input_files()

This is for the `deadline bundle submit --debug-snapshot` command. It can be replaced with
a `hash_upload_abs_manifest()` call using the FileSystemDataCache. Before this will work,
other refactoring needs to happen to structure the manifests that are provided as input here.

## download.py download_file()

We won't need this anymore, downloading an individual file is handled within `download_abs_manifest()`
and we can refactor at a higher level.

## download.py _download_files_parallel()

The implementation of `download_abs_manifest()` has a full multi-threaded download that we can
use with `S3DataCache`. We likely want to refactor at a higher manifest level instead of at this
function.

## download.py download_files_from_manifests()

This can be replaced with `download_abs_manifest()` after suitable adaptation of the inputs.
First we would convert each manifest to use absolute paths with `join_manifest()` by joining
it with its root absolute path, and then we would use `compose_manifest()` to layer them all
into a single absolute manifest. That is then something we can provide to `download_abs_manfiest()`.

## download.py merge_asset_manifests()

This can be replaced with `compose_manifests()`. When converting from the v2023 manifest class,
we would convert into snapshot diffs so that we can compose all the inputs together. We don't want
to add a mode to compose_manifests that composes snapshots, because that operation breaks the abstraction.
