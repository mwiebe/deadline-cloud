# Job Attachments Snapshots

Snapshots are a feature of Deadline Cloud job attachments for capturing directory tree snapshots, taking diffs between them,
and performing various transformations on them. Deadline Cloud uses a specific key structure on S3 where a Data/ prefix
holds a content addressed data cache and a Manifest/ prefix holds snapshot and diff manifests representing inputs and
outputs of jobs. This document describes the design of these snapshots and operations you can perform on them independently
of Deadline Cloud itself, providing a useful tool for working with directory trees.

A snapshot manifest is a data structure that captures a directory tree snapshot—similar to a zip file's table of contents,
but without the actual file content. It records metadata for each file (path, size, modification time, content hash) and,
in newer formats, directories and symlinks. Operations on these manifests provide efficient change detection, uploads,
downloads, and content-addressable storage workflows.

## Overview

The library provides four concrete manifest classes organized by two dimensions:

**Path Style:**
- **Relative paths** (`Snapshot`, `SnapshotDiff`) - Paths relative to an unspecified root, portable across systems
- **Absolute paths** (`AbsSnapshot`, `AbsSnapshotDiff`) - Full filesystem paths, required for file system operations

**Path Normalization:**

Within a single manifest, all paths (file paths, directory paths, and symlink targets) share these properties:
- All paths use the same style—either all absolute, or all relative to the same root
- Paths are normalized and cannot contain `.` or `..` components
- Path separators are always forward slash `/` (even on Windows, e.g., `C:/path/to/file.txt`)
- Windows long-path prefix (`//?/`) is stripped during normalization

**Windows Absolute Paths:**

Windows absolute paths in manifests can take two forms:
- Drive letter paths: `C:`, `C:/`, `X:/path/to/file.txt`
- UNC paths: `//server/share/path/to/file.txt`

Note that UNC paths use forward slashes like all other manifest paths (not the native `\\server\share` format).

**Manifest Type:**
- **Snapshot** - Complete point-in-time capture of a directory tree
- **Diff** - Changes between two snapshots (additions, modifications, deletions)

### Manifest Classes

```
Snapshot        - Relative-path snapshot (the primary portable format)
SnapshotDiff    - Relative-path diff between snapshots
AbsSnapshot     - Absolute-path snapshot (for filesystem operations)
AbsSnapshotDiff - Absolute-path diff (for applying changes to filesystem)
```

### Type Aliases

Type aliases group manifest classes for use in function signatures:

```python
# By path style
RelManifest = Union[Snapshot, SnapshotDiff]      # Any relative-path manifest
AbsManifest = Union[AbsSnapshot, AbsSnapshotDiff] # Any absolute-path manifest

# By manifest type
AnySnapshot = Union[AbsSnapshot, Snapshot]        # Any snapshot (full capture)
AnyDiff = Union[AbsSnapshotDiff, SnapshotDiff]    # Any diff (changes only)

# All manifest types
AnyManifest = Union[AbsSnapshot, AbsSnapshotDiff, Snapshot, SnapshotDiff]
```

### Data Cache Classes

```
ContentAddressedDataCache
├── S3DataCache         (data on S3)
└── FileSystemDataCache (data on a file system)
```

### Operations

```
┌─────────────────────────────────────────────────────────────────────────┐
│                 FILE SYSTEM AND DATA CACHE OPERATIONS                   │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  1. COLLECT: Paths → AbsSnapshot                                        │
│         Scans directories and files into a snapshot (no hashing).       │
│                                                                         │
│  2. HASH: AbsManifest → AbsManifest                                     │
│         Computes hashes for all files in the manifest.                  │
│                                                                         │
│  3. HASH_UPLOAD: (AbsManifest, ContentAddressedDataCache) → UploadResult│
│         Hashes files and uploads them to a data cache.                  │
│                                                                         │
│  4. DOWNLOAD: (AbsManifest, ContentAddressedDataCache) → DownloadResult │
│         Downloads files from a data cache to local filesystem.          │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       MANIFEST TRANSFORMATIONS                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  5. FILTER: AnyManifest → AnyManifest                                   │
│         Filters entries, returning only those that match.               │
│                                                                         │
│  6. DIFF: (AnySnapshot, AnySnapshot) → AnyDiff                          │
│         Computes the difference between two snapshots.                  │
│                                                                         │
│  7. COMPOSE: (Snapshot, SnapshotDiff, ...) → Snapshot                   │
│     COMPOSE: (SnapshotDiff, ...) → SnapshotDiff                         │
│         Combines manifests sequentially (diffs onto snapshot or diffs). │
│                                                                         │
│  8. SUBTREE: (AnyManifest, path) → RelManifest                          │
│         Extracts a subtree as a relative-path manifest.                 │
│                                                                         │
│  9. PARTITION: (AnySnapshot, roots?) → List[(root, Snapshot)]           │
│         Splits a manifest into multiple (root, manifest) pairs.         │
│                                                                         │
│ 10. JOIN: (RelManifest, prefix) → AnyManifest                           │
│         Prepends a prefix to all paths (abs prefix → abs result).       │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Use Cases

1. (`deadline bundle submit`) When submitting a job to a cloud render farm,
   collect all the input asset files the job depends on into one or more manifests
   attached to the job.
    1. Use COLLECT, providing directories and individual filenames to collect.
       Set the symlink_policy to COLLAPSE_ESCAPING to ensure all
       symlink targets that are not part of the dataset become files instead of
       staying as symlinks.
    2. Use HASH_UPLOAD to hash and upload the files that aren't already in the data
       cache. This populates all the hash values in the manifest.
    3. Use PARTITION to divide up the absolute_manifest into a collection of
       (root_path, relative_manifest) pairs. This is the input format needed by
       the Deadline Cloud CreateJob API.
2. (TBD - `deadline queue upload-cache-inputs`) Before submitting a job, you have much of
   the asset data ready and would like to pre-populate your render farm data cache
   in the cloud.
    1. Use COLLECT, providing the directories and individual filenames of the
       asset data. Use COLLAPSE_ESCAPING for the symlink_policy.
    2. Use HASH_UPLOAD to hash and upload the files that aren't already in the data
       cache. There's no need to convert the manifest to relative paths, as what's
       important for this use case is populating the local hash cache and the
       cloud data cache.
3. (`deadline bundle submit --save-debug-snapshot`) When debugging a job, create
   a portable debug snapshot of all the input asset files. With a debug snapshot
   in hand, you can provide a reproducible artifact to a render TD or to
   vendor support personnel.
    1. Same as for submitting a job to a cloud render farm with COLLECT/HASH_UPLOAD/PARTITION,
       but when using HASH_UPLOAD provide a `FileSystemDataCache` that writes to your local file system
       to place in a zip file instead of uploading to the cloud.
5. (`deadline job download-output`) To download the output of a single Deadline Cloud job,
   take all the output manifests, join them to have absolute paths, compose them into
   a single manifest, and then download.
   1. Use JOIN make each task output manifest have absolute paths.
   2. Order the manifests by their S3 last-modified timestamp, then COMPOSE them into a single manifest.
   3. Use DOWNLOAD to apply the changes locally.
4. To collect a single directory tree into a manifest with relative paths:
    1. Use COLLECT with a single directory to collect, with COLLAPSE_ESCAPING as
       the symlink_policy
    2. (Optional) Use HASH to populate the hash values in the manifest. Run this
       while the manifest has absolute paths.
    3. Use SUBTREE to extract the directory as a relative-path manifest.

### Design Choices

1. Simple and flexible in-memory snapshots and diffs shared by composable operations. Code can modify values
   in ways that doesn't strictly follow the on-disk manifest storage, but the operations and I/O accept
   and use the data where it makes sense.
2. File system operations only work with absolute path manifests. This simplifies the definition and implementation
   of these operations. Conversion to/from relative path manifests is via the SUBTREE and JOIN operations.
3. Path separators are always POSIX forward slash '/' in manifest path strings. E.g. on Windows,
   an absolute path can look like "C:/path/to/file.txt". On Windows, operations should convert '\\'
   path separators to '/', while on POSIX operations should preserve '\\' within file and directory names.
4. Symlink targets are always absolute paths, or always relative to the same root that file and directory
   paths are relative to. This is different than symlink representations on file systems, where they are
   relative to the symlink's parent directory.
5. In diffs, directory deletions must be accompanied by deletion of all the contents of the directory.
   This is necessary for the COMPOSE operation to correctly compose multiple diffs. When applying a diff,
   a directory deletion means to delete the directory if it is empty, not to recursively delete its contents.
6. There is no operation that uploads a hashed manifest. When we perform an upload, we always hash the data
   on its way into the content-addressed data cache in order to guarantee that it always satisfies that hashing
   the data stored for a hash key always equals that hash. Currently hash_upload requires that the provided manifest
   has no hashes, but we could add a mode to it that validates existing hashes and fails if the content differs.
7. **Support v2023 on-disk format via lossy conversion.** When serializing to v2023 format, the following occurs:
   - Symlinks are collapsed to files/directories or excluded (symlink_policy decides)
   - Empty directories are not preserved
   - Deletions are not preserved
   - Chunk size must be set to WHOLE_FILE_CHUNK_SIZE.
   - Runnable flags are not preserved

### Benefits of Composable Design

1. **Testability:** Each operation can be unit tested independently
2. **Reusability:** Operations can be composed in different ways for different workflows
3. **Performance:** Deferred hashing allows skipping unchanged files
4. **Flexibility:** Custom filters enable advanced filtering beyond glob patterns

### Example Performance Measurement

Here's an example performance measurement from an EC2 instance against S3 in the same region.
The script scripted_tests/snapshots_scale_test.py was used to generate the data.

One thing we can see is that the caches are providing large benefit. In the cases where the caches
can fully eliminate S3 access, all the timings are sub-second.

#### S3 Transfer time for 25 GB, 1905 files

SCALING TEST SUMMARY (Duration as M:SS)
|  Workers |      UPLOAD cold | UPLOAD warm-head |  UPLOAD warm-all |    DOWNLOAD cold |    DOWNLOAD warm |
|---------:|-----------------:|-----------------:|-----------------:|-----------------:|-----------------:|
|        1 |             5:44 |             0:25 |           0:00.2 |            11:08 |           0:00.3 |
|        2 |             2:49 |             0:12 |           0:00.3 |             5:02 |           0:00.4 |
|        4 |             2:05 |             0:06 |           0:00.3 |             2:25 |           0:00.4 |
|        8 |             1:43 |             0:04 |           0:00.3 |             1:51 |           0:00.4 |
|       16 |             1:43 |             0:04 |           0:00.3 |             0:47 |           0:00.4 |
|       32 |             1:56 |             0:04 |           0:00.3 |             0:45 |           0:00.4 |

### Why Separate COLLECT, HASH, and HASH_UPLOAD?

Separating structure collection, hashing, and hashing+uploading enables:

- **Fast diff comparison:** Compare manifests by mtime/size without hashing unchanged files
- **Hash cache integration:** Only hash files with cache misses
- **Deferred hashing:** Collect structure first, hash only what's needed
- **Reduced redundant reads:** The HASH_UPLOAD operation combines read+hash into a single stage, computing the hash while bytes stream into the memory buffer, then uploads. This avoids reading files twice (once for hash, once for upload).

## Operation Details

The detailed documentation for each operation is in these sub-component documents:

| Operation | Description | Documentation |
|-----------|-------------|---------------|
| 1. COLLECT | Scans directories/files into a snapshot (no hashing) | [snapshot_operation_collect.md](job_attachments_snapshots_components/snapshot_operation_collect.md) |
| 2. HASH | Computes hashes for all files in a manifest | [snapshot_operation_hash.md](job_attachments_snapshots_components/snapshot_operation_hash.md) |
| 3. HASH_UPLOAD | Hashes files and uploads them to a data cache | [snapshot_operation_hash_upload.md](job_attachments_snapshots_components/snapshot_operation_hash_upload.md) |
| 4. DOWNLOAD | Downloads files from a data cache to local filesystem | [snapshot_operation_download.md](job_attachments_snapshots_components/snapshot_operation_download.md) |
| 5. FILTER | Filters entries, returning only those that match | [snapshot_operation_filter.md](job_attachments_snapshots_components/snapshot_operation_filter.md) |
| 6. DIFF | Computes the difference between two snapshots | [snapshot_operation_diff.md](job_attachments_snapshots_components/snapshot_operation_diff.md) |
| 7. COMPOSE | Combines manifests sequentially | [snapshot_operation_compose.md](job_attachments_snapshots_components/snapshot_operation_compose.md) |
| 8. SUBTREE | Extracts a subtree as a relative-path manifest | [snapshot_operation_subtree.md](job_attachments_snapshots_components/snapshot_operation_subtree.md) |
| 9. PARTITION | Splits a manifest into multiple (root, manifest) pairs | [snapshot_operation_partition.md](job_attachments_snapshots_components/snapshot_operation_partition.md) |
| 10. JOIN | Prepends a prefix to all paths | [snapshot_operation_join.md](job_attachments_snapshots_components/snapshot_operation_join.md) |

## Module Organization

The composable operations are implemented in separate modules under `src/deadline/job_attachments/_snapshots/_operations/`:

| Module | Operation | Description |
|--------|-----------|-------------|
| `_collect_abs_snapshot.py` | COLLECT | Scans directories/files, creates manifest with `hash=None` |
| `_hash_abs_manifest.py` | HASH | Fills in hashes for collected manifest |
| `_hash_upload_abs_manifest.py` | HASH_UPLOAD | Main entry point for hash+upload pipeline |
| `_hash_upload_abs_manifest_pipeline.py` | HASH_UPLOAD | Base pipeline class, progress state, work items, memory pool |
| `_hash_upload_abs_manifest_s3_pipeline.py` | HASH_UPLOAD | S3-specific upload logic (multipart, streaming) |
| `_hash_upload_abs_manifest_file_system_pipeline.py` | HASH_UPLOAD | FileSystem-specific upload logic |
| `_download_abs_manifest.py` | DOWNLOAD | Downloads files from a data cache to local filesystem |
| `_filter_manifest.py` | FILTER | Filters manifest entries using callable filter |
| `_diff_snapshots.py` | DIFF | Computes difference between two manifests |
| `_compose_manifest.py` | COMPOSE | Layers manifests together into one |
| `_subtree_manifest.py` | SUBTREE | Extracts a subtree as a new manifest |
| `_partition_manifest.py` | PARTITION | Partitions manifest into (root, RelSnapshot) pairs |
| `_join_manifest.py` | JOIN | Joins a prefix to all paths in a manifest |

## Refactoring the Existing Job Attachments Implementation

### Conversions for the BaseAssetManifest class - DONE

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

### Progress tracking

The current job attachments code uses different progress tracking interfaces than `hash_upload_abs_manifest()`
and `download_abs_manifest()`. We can write adaptor classes to facilitate refactoring, and
plan to later switch the interfaces by releasing a breaking change.

### upload.py S3AssetManager.prepare_paths_for_upload() - DONE

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

### upload.py S3AssetUploader.upload_input_files()

We can replace this with `hash_upload_abs_manifest()` using an `S3DataCache`. If we refactor this
early, we can use `join_manifest()` and `manifest.clear_hashes()` to adapt the relative paths and
remove the hashes. Ideally we refactor this later, so that we can call `hash_upload_abs_manifest()`
once on the entire dataset that we're uploading all at once.

### upload.py S3AssetManager._create_manifest_file()

This function collects and hashes files for a single root of a `deadline bundle submit` operation,
into a relative-path manifest. While this could be achieved with `collect_abs_snapshot()`
followed by `hash_abs_manifest()`, we don't want to refactor it this way. We want our `collect_abs_snapshot()`
to happen earlier in the job submission flow, and use `partition_manifest()` to determine
the groupings for each root. Therefore refactoring this function comes later.

### upload.py S3AssetUploader._snapshot_input_files()

This is for the `deadline bundle submit --debug-snapshot` command. It can be replaced with
a `hash_upload_abs_manifest()` call using the FileSystemDataCache. Before this will work,
other refactoring needs to happen to structure the manifests that are provided as input here.

### download.py download_file()

We won't need this anymore, downloading an individual file is handled within `download_abs_manifest()`
and we can refactor at a higher level.

### download.py _download_files_parallel()

The implementation of `download_abs_manifest()` has a full multi-threaded download that we can
use with `S3DataCache`. We likely want to refactor at a higher manifest level instead of at this
function.

### download.py download_files_from_manifests()

This can be replaced with `download_abs_manifest()` after suitable adaptation of the inputs.
First we would convert each manifest to use absolute paths with `join_manifest()` by joining
it with its root absolute path, and then we would use `compose_manifest()` to layer them all
into a single absolute manifest. That is then something we can provide to `download_abs_manfiest()`.

### download.py merge_asset_manifests()

This can be replaced with `compose_manifests()`.

## ContentAddressedDataCache Classes

**Location:** `_content_addressed_data_cache.py`

The `ContentAddressedDataCache` is an abstract base class that defines the interface for content-addressable storage backends. It encapsulates the destination-specific parameters needed by the HASH_UPLOAD operation.

```python
# In _content_addressed_data_cache.py

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ...caches.s3_check_cache import S3CheckCache

@dataclass
class ContentAddressedDataCache(ABC):
    """Abstract base class for content-addressable data caches."""

    @abstractmethod
    def get_object_key(self, hash_value: str, algorithm: str) -> str:
        """Returns the storage key/path for a given hash."""
        ...

    @abstractmethod
    def object_exists(self, hash_value: str, algorithm: str) -> bool:
        """Checks if an object with the given hash already exists."""
        ...


@dataclass
class S3DataCache(ContentAddressedDataCache):
    """
    Content-addressable data cache backed by Amazon S3.

    Files are stored with keys in the format:
        {s3_key_prefix}/{hash}.{algorithm}

    Example: Data/a1b2c3d4e5f67890abcdef1234567890.xxh128
    """
    s3_bucket: str
    s3_key_prefix: str
    s3_client: Any  # boto3 S3 client with permissions for GetObject, PutObject, HeadObject
    s3_check_cache: Optional[S3CheckCache] = field(default=None)
    multipart_part_size: int = field(default=32 * 1024 * 1024)  # 32MB default
    force_s3_check: bool = field(default=False)
    account_id: Any = field(default=None)  # None = auto-detect, NO_ACCOUNT_ID_CHECK = disable

    @property
    def expected_bucket_owner(self) -> Optional[str]:
        """Returns the account ID to use for ExpectedBucketOwner, or None if disabled."""
        ...

    def get_object_key(self, hash_value: str, algorithm: str) -> str:
        """Returns the S3 key for a given hash."""
        return f"{self.s3_key_prefix}/{hash_value}.{algorithm}"

    def get_cache_key(self, hash_value: str, algorithm: str) -> str:
        """Returns the cache key for a given hash (bucket/key format)."""
        return f"{self.s3_bucket}/{self.get_object_key(hash_value, algorithm)}"

    def get_check_cache_entry(self, hash_value: str, algorithm: str) -> Optional[S3CheckCacheEntry]:
        """Check if hash exists in the S3 check cache (without HeadObject)."""
        ...

    def head_object_exists(self, hash_value: str, algorithm: str) -> bool:
        """Check if object exists in S3 using HeadObject (bypassing cache)."""
        ...

    def object_exists(self, hash_value: str, algorithm: str) -> bool:
        """Check local cache first, then fall back to HeadObject."""
        ...

**Fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `s3_bucket` | `str` | (required) | The S3 bucket name |
| `s3_key_prefix` | `str` | (required) | Key prefix for content-addressable storage (e.g., `"Data"`) |
| `s3_client` | boto3 S3 client | (required) | Client with GetObject, PutObject, HeadObject permissions |
| `s3_check_cache` | `Optional[S3CheckCache]` | `None` | Cache to avoid redundant S3 existence checks |
| `multipart_part_size` | `int` | 32MB | Part size for multipart uploads/downloads |
| `force_s3_check` | `bool` | `False` | If True, skip s3_check_cache and always make HeadObject calls |
| `account_id` | `Any` | `None` | AWS account ID for ExpectedBucketOwner. `None` = auto-detect from credentials. `NO_ACCOUNT_ID_CHECK` = disable the check. |

**Account ID Handling:**

The `account_id` field controls the `ExpectedBucketOwner` parameter on S3 API calls, which prevents confused deputy attacks:
- `None` (default): Auto-detect from credentials at construction time
- `NO_ACCOUNT_ID_CHECK`: Disable ExpectedBucketOwner checks entirely
- String value: Use the provided account ID


@dataclass
class FileSystemDataCache(ContentAddressedDataCache):
    """
    Content-addressable data cache backed by a local or network file system.

    Files are stored with paths in the format:
        {root_path}/{hash}.{algorithm}

    Example: /mnt/cache/a1b2c3d4e5f67890abcdef1234567890.xxh128

    This is useful for:
    - Creating portable debug snapshots (zip files)
    - Local testing without S3
    - Network-attached storage caches
    """
    root_path: Path

    def __post_init__(self) -> None:
        # Ensure root_path is absolute
        if not self.root_path.is_absolute():
            raise ValueError(f"root_path must be absolute, got: {self.root_path}")

    def get_object_key(self, hash_value: str, algorithm: str) -> str:
        return str(self.root_path / f"{hash_value}.{algorithm}")

    def object_exists(self, hash_value: str, algorithm: str) -> bool:
        return (self.root_path / f"{hash_value}.{algorithm}").exists()
```

## Workflow Examples

### Snapshot Flow

Create a complete snapshot manifest from a directory:

```
Directory ──[collect]──► AbsSnapshot ──[hash]──► Hashed ──[subtree]──► RelSnapshot ──[filter]──► Filtered ──[save]──► File
```

**Important:** The HASH operation must occur BEFORE SUBTREE because HASH requires absolute paths to read files from the filesystem. SUBTREE converts to relative paths, after which the manifest can no longer be hashed.

```python
# Step 1: Collect directory tree with absolute paths
abs_manifest = collect_abs_snapshot([root], [], version=version)

# Step 2: Hash all files (requires absolute paths)
hashed = hash_abs_manifest(abs_manifest, hash_cache)

# Step 3: Extract as relative paths
rel_manifest = subtree_manifest(hashed, root)

# Step 4: Filter the snapshot
filter = IncludeExcludePathsFilter(include=include, exclude=exclude)
filtered = filter_manifest(rel_manifest, filter)

# Step 5: Write to file
manifest_path = _write_manifest(root, filtered, destination, name)
```

### Diff Flow (Fast Mode)

Compare by mtime/size without hashing unchanged files:

```
Parent File ──[load]──► Parent ──[filter]──► Filtered Parent ──┐
                                                               ├──[diff]──► Diff Manifest
Directory ──[collect]──► AbsSnapshot ──[subtree]──► RelSnapshot ──[filter]──► Filtered Current ─┘
                                                        (no hashes)
```

**Note:** In fast mode, we skip hashing entirely and compare by mtime/size. The SUBTREE operation can be applied to unhashed manifests since it only transforms paths, not file content. However, any changed files identified by the diff will still need hashing before upload.

```python
# Load parent manifest
with open(parent_path) as f:
    parent_str = f.read()
    parent = decode_manifest(parent_str)
    parent_hash = hash_data(parent_str.encode("utf-8"), HashAlgorithm.XXH128)

# Collect current directory with absolute paths, then extract as relative
abs_manifest = collect_abs_snapshot([root], [], version=version)
current_unhashed = subtree_manifest(abs_manifest, root)

# Filter BOTH with same patterns
filter = IncludeExcludePathsFilter(include=include, exclude=exclude)
filtered_parent = filter_manifest(parent, filter)
filtered_current = filter_manifest(current_unhashed, filter)

# Compute diff (fast mode - compare by mtime/size)
diff = diff_snapshots(
    parent=filtered_parent,
    current=filtered_current,
    parent_manifest_hash=parent_hash,
    ignore_hashes=True,  # Fast mode
)

# Note: Changed files in diff still need hashing before upload
```

### Diff Flow (Full Mode)

Hash everything for definitive comparison:

```
Parent File ──[load]──► Parent ──[filter]──► Filtered Parent ──┐
                                                               ├──[diff]──► Diff Manifest
Directory ──[collect]──► AbsSnapshot ──[hash]──► Hashed ──[subtree]──► RelSnapshot ──[filter]──► Filtered Current ─┘
```

**Important:** The HASH operation must occur BEFORE SUBTREE because HASH requires absolute paths to read files from the filesystem.

```python
# Load parent manifest
with open(parent_path) as f:
    parent_str = f.read()
    parent = decode_manifest(parent_str)
    parent_hash = hash_data(parent_str.encode("utf-8"), HashAlgorithm.XXH128)

# Collect and hash current directory (hash before subtree!)
abs_manifest = collect_abs_snapshot([root], [], version=version)
hashed = hash_abs_manifest(abs_manifest, hash_cache, force_rehash=True)
current_rel = subtree_manifest(hashed, root)

# Filter BOTH with same patterns
filter = IncludeExcludePathsFilter(include=include, exclude=exclude)
filtered_parent = filter_manifest(parent, filter)
filtered_current = filter_manifest(current_rel, filter)

# Compute diff (full mode - compare by hash)
diff = diff_snapshots(
    parent=filtered_parent,
    current=filtered_current,
    parent_manifest_hash=parent_hash,
    ignore_hashes=False,  # Full mode
)
```

## Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `DEFAULT_FILE_CHUNK_SIZE` | 256 MB (256 × 1024 × 1024) | Default threshold for chunked hashing |
| `WHOLE_FILE_CHUNK_SIZE` | -1 | Sentinel value meaning "no chunking, hash whole file" |
| `DEFAULT_S3_MULTIPART_PART_SIZE` | 32 MB (32 × 1024 × 1024) | Default part size for S3 multipart uploads/downloads |
| `NO_ACCOUNT_ID_CHECK` | Sentinel object | Disables ExpectedBucketOwner checks when passed as `S3DataCache.account_id` |
| `WHOLE_FILE_RANGE_END` | -1 | Sentinel value for hash cache `range_end` indicating a whole-file hash |

## Validation Rules

Manifests validate their constraints on construction. However, manifest fields can be modified after construction, which may leave the manifest in an invalid state. Call `validate()` on a manifest to check that it still satisfies all constraints.

### Entry Classes

Manifests contain two types of entries: file entries and directory entries.

#### ManifestFilePath

Represents a file or symlink in the manifest:

| Field | Type | Description |
|-------|------|-------------|
| `path` | `str` | File path (relative or absolute depending on manifest type) |
| `hash` | `Optional[str]` | Content hash for small files (None if unhashed, chunked, or symlink) |
| `size` | `int` | File size in bytes |
| `mtime` | `int` | Modification time (microseconds since epoch) |
| `chunkhashes` | `Optional[List[str]]` | Per-chunk hashes for large files (None if unhashed, small, or symlink) |
| `symlink_target` | `Optional[str]` | Symlink target path (None for regular files) |
| `runnable` | `bool` | POSIX execute bit (always False on Windows) |
| `deleted` | `bool` | Deletion marker for diff manifests (v2025 only) |

**Symlinks vs Regular Files:**
- If `symlink_target` is set, the entry is a symlink and `hash`/`chunkhashes` must both be None
- If `symlink_target` is None, the entry is a regular file

**Hashed vs Unhashed Files:**
Regular files can be in one of three states:

| State | `hash` | `chunkhashes` | Description |
|-------|--------|---------------|-------------|
| Unhashed | None | None | Created by COLLECT; needs hashing before upload |
| Hashed (single) | Set | None | Small file or whole-file hashing mode |
| Hashed (chunked) | None | Set | Large file with per-chunk hashes |

The COLLECT operation produces unhashed manifests (both `hash` and `chunkhashes` are None for regular files). The HASH and HASH_UPLOAD operations populate the appropriate hash field(s) based on file size and chunk settings.

**Clearing Hashes:**

The `clear_hashes()` method on manifest classes sets `hash` and `chunkhashes` to None for all regular file entries, returning the manifest to an unhashed state. Symlinks and deleted entries are unchanged. This is useful when you need to re-hash a manifest after files have been modified.

```python
# Re-hash a manifest after files have changed
manifest.clear_hashes()
hashed = hash_abs_manifest(manifest, hash_cache)
```

#### ManifestDirectoryPath

Represents a directory in the manifest:

| Field | Type | Description |
|-------|------|-------------|
| `path` | `str` | Directory path (relative or absolute depending on manifest type) |
| `deleted` | `bool` | Deletion marker for diff manifests (v2025 only) |

**Implicit vs Explicit Directories:**

- Parent directories of files and symlinks are implicitly part of the manifest, even if not in the `dirs` list. Operations that need directory information expand to include all parent directories of files and symlinks.
- Empty directories must be explicitly listed in `dirs`, since no files or symlinks imply their existence.
- The v2023 format does not support empty directories.

**Directory Deletion Semantics:**

A directory marked as `deleted=True` is only considered deleted if no non-deleted file, symlink, or subdirectory exists under it. If any non-deleted entry exists as a subpath, the directory deletion is effectively ignored.

### Symlink Validation (v2025-12-04-beta)

- Target must be within the manifest root
- If target is absolute, it is converted to relative

### Chunked File Validation (v2025-12-04-beta)

For **hashed** regular files, chunking behavior is controlled by the manifest's `fileChunkSizeBytes` field:

| `fileChunkSizeBytes` | File Size | Required Field |
|---------------------|-----------|----------------|
| `DEFAULT_FILE_CHUNK_SIZE` (256MB) | ≤ chunk size | `hash` |
| `DEFAULT_FILE_CHUNK_SIZE` (256MB) | > chunk size | `chunkhashes` |
| `WHOLE_FILE_CHUNK_SIZE` (-1) | Any | `hash` (no chunking) |
| Positive int (chunk size) | ≤ chunk size | `hash` |
| Positive int (chunk size) | > chunk size | `chunkhashes` |

When `chunkhashes` is used:
- Chunk count must equal `ceil(size / fileChunkSizeBytes)`
- Each chunk hash corresponds to exactly `fileChunkSizeBytes` bytes (except possibly the last chunk)

### Deleted Entry Validation (v2025-12-04-beta)

Deleted entries can only have:
- `path` (required)
- `deleted=True` (required)

All other fields must be None/False.

### Directory Deletion Semantics (v2025-12-04-beta)

A directory deletion marker means "delete this empty directory". To delete a non-empty directory, you must explicitly delete all its contents first:
- All files and symlinks within the directory
- All subdirectories (recursively, following the same rule)
- Finally, the directory itself

This explicit deletion requirement ensures that diff manifests are fully composable—each deletion is self-contained and doesn't depend on knowing the parent snapshot's contents.
