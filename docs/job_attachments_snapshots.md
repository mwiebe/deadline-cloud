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

### Refactoring the Existing Job Attachments Implementation

#### Conversions for the BaseAssetManifest class - DONE

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

#### Progress tracking

The current job attachments code uses different progress tracking interfaces than `hash_upload_abs_manifest()`
and `download_abs_manifest()`. We can write adaptor classes to facilitate refactoring, and
plan to later switch the interfaces by releasing a breaking change.

#### upload.py S3AssetManager.prepare_paths_for_upload() - DONE

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

#### upload.py S3AssetUploader.upload_input_files()

We can replace this with `hash_upload_abs_manifest()` using an `S3DataCache`. If we refactor this
early, we can use `join_manifest()` and `manifest.clear_hashes()` to adapt the relative paths and
remove the hashes. Ideally we refactor this later, so that we can call `hash_upload_abs_manifest()`
once on the entire dataset that we're uploading all at once.

#### upload.py S3AssetManager._create_manifest_file()

This function collects and hashes files for a single root of a `deadline bundle submit` operation,
into a relative-path manifest. While this could be achieved with `collect_abs_snapshot()`
followed by `hash_abs_manifest()`, we don't want to refactor it this way. We want our `collect_abs_snapshot()`
to happen earlier in the job submission flow, and use `partition_manifest()` to determine
the groupings for each root. Therefore refactoring this function comes later.

#### upload.py S3AssetUploader._snapshot_input_files()

This is for the `deadline bundle submit --debug-snapshot` command. It can be replaced with
a `hash_upload_abs_manifest()` call using the FileSystemDataCache. Before this will work,
other refactoring needs to happen to structure the manifests that are provided as input here.

#### download.py download_file()

We won't need this anymore, downloading an individual file is handled within `download_abs_manifest()`
and we can refactor at a higher level.

#### download.py _download_files_parallel()

The implementation of `download_abs_manifest()` has a full multi-threaded download that we can
use with `S3DataCache`. We likely want to refactor at a higher manifest level instead of at this
function.

#### download.py download_files_from_manifests()

This can be replaced with `download_abs_manifest()` after suitable adaptation of the inputs.
First we would convert each manifest to use absolute paths with `join_manifest()` by joining
it with its root absolute path, and then we would use `compose_manifest()` to layer them all
into a single absolute manifest. That is then something we can provide to `download_abs_manfiest()`.

#### download.py merge_asset_manifests()

This can be replaced with `compose_manifests()`.

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

### Benefits of Composable Design

1. **Testability:** Each operation can be unit tested independently
2. **Reusability:** Operations can be composed in different ways for different workflows
3. **Performance:** Deferred hashing allows skipping unchanged files
4. **Flexibility:** Custom filters enable advanced filtering beyond glob patterns
5. **Consistency:** Same filter applied to both sides ensures correct diff computation

### Why Separate COLLECT, HASH, and HASH_UPLOAD?

Separating structure collection, hashing, and hashing+uploading enables:

- **Fast diff comparison:** Compare manifests by mtime/size without hashing unchanged files
- **Hash cache integration:** Only hash files with cache misses
- **Deferred hashing:** Collect structure first, hash only what's needed
- **Reduced redundant reads:** The HASH_UPLOAD operation combines read+hash into a single stage, computing the hash while bytes stream into the memory buffer, then uploads. This avoids reading files twice (once for hash, once for upload).

## Design Choices

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

## Operation Details

### 1. COLLECT: `collect_abs_snapshot()`

**Location:** `_collect_abs_snapshot.py`

Collects provided lists of paths into a manifest with absolute paths, WITHOUT computing hashes:

```python
def collect_abs_snapshot(
    directories: List[Path | str],
    filenames: List[Path | str],
    *,
    optional_filenames: Optional[List[Path | str]] = None,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
    file_chunk_size_bytes: Optional[int] = None,
) -> AbsSnapshot:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `directories` | (positional) List of directory paths whose full contents are collected. All paths must exist and be directories. Empty directories are included in the manifest. |
| `filenames` | (positional) List of file/symlink paths that must exist. Raises `FileNotFoundError` if any file does not exist. |
| `optional_filenames` | List of file/symlink paths to include if they exist. Missing files are silently ignored. |
| `symlink_policy` | How to handle symlinks during collection (see below). Default `COLLAPSE_ESCAPING`. |
| `file_chunk_size_bytes` | Chunk size for large file hashing. `None` = use `DEFAULT_FILE_CHUNK_SIZE` (256MB). `WHOLE_FILE_CHUNK_SIZE` (-1) = no chunking. Positive int = chunk size in bytes. |

**Symlink Policy Options (for `collect_abs_snapshot`):**

| Policy | Description |
|--------|-------------|
| `COLLAPSE_ESCAPING` | Preserve symlinks whose targets are within the collected paths; collapse symlinks whose targets are outside (escaping symlinks) to files/directories. (default) |
| `COLLAPSE_ALL` | Follow all symlinks, treating them as files/directories. |
| `PRESERVE` | Keep all symlinks as symlink entries with absolute targets. |
| `TRANSITIVE_INCLUDE_TARGETS` | Keep all symlinks and add their targets to the manifest. |
| `EXCLUDE_ALL` | Skip all symlinks entirely. |
| `EXCLUDE_ESCAPING` | Preserve symlinks whose targets are within the collected paths; exclude symlinks whose targets are outside (escaping symlinks). |

**Symlink Collapsing Behavior:**

When a symlink is collapsed (via `COLLAPSE_ALL` or `COLLAPSE_ESCAPING`), it is replaced with the actual content at its target location. The behavior depends on whether the target is a file or directory:

*File symlink collapsing:*
- The symlink entry is replaced with a regular file entry
- The file's metadata (size, mtime, runnable) comes from the target file
- The entry's path remains the symlink's path (not the target's path)

*Directory symlink collapsing:*
- The symlink is replaced with the entire directory tree at the target location
- All entries appear under the symlink's path, not the target's path (path translation)
- Nested symlinks within the collapsed directory are handled recursively:
  - If a nested symlink points within the same collapsed directory → preserve it with translated target
  - If a nested symlink points outside the collapsed directory → collapse it recursively

*Example - Directory symlink collapsing with nested symlinks:*

```
/project/                        # Being collected
└── assets -> /external/v2       # Directory symlink to collapse

/external/v2/
├── model.obj
├── current -> textures/wood.png # Points within collapsed dir
└── textures/
    ├── wood.png
    └── shared -> /library/tex   # Points outside collapsed dir

/library/tex/
└── metal.png
```

After collapsing `/project/assets`:
- `/project/assets/model.obj` (file)
- `/project/assets/current` → `/project/assets/textures/wood.png` (symlink, target translated)
- `/project/assets/textures/wood.png` (file)
- `/project/assets/textures/shared/metal.png` (file, recursively collapsed)

The nested symlink `current` is preserved because its target is within the collapsed directory (translated from `/external/v2/textures/wood.png` to `/project/assets/textures/wood.png`). The nested symlink `shared` is recursively collapsed because its target escapes the collapsed directory.

**Escaping Symlink Detection Algorithm:**

For `COLLAPSE_ESCAPING` and `EXCLUDE_ESCAPING` policies, the COLLECT operation uses a two-pass algorithm to determine which symlinks are "escaping" (pointing outside the collected paths):

*Pass 1 - Build the collected set:*
1. Walk all directories and collect all non-symlink files and directories
2. Collect all non-symlink files from `filenames` and `optional_filenames`
3. Defer all symlinks encountered for later processing
4. The result is a set of all collected paths (the "collected set")

*Pass 2 - Process deferred symlinks:*
For each deferred symlink, resolve its target (without following symlink chains) and check if the target is within the collected set:

```python
def is_escaping(symlink_target: Path, collected_set: Set[str]) -> bool:
    target_posix = symlink_target.as_posix()
    # Direct match - target path itself was collected
    if target_posix in collected_set:
        return False
    # Prefix match - target is under a collected directory
    for collected_path in collected_set:
        if target_posix.startswith(collected_path + "/"):
            return False
    return True
```

- If the target is in the collected set → preserve the symlink
- If the target escapes → collapse (inline the target's contents) or exclude, based on policy

*Why two passes?*

A single-pass approach cannot correctly identify escaping symlinks because the full collected set isn't known until all paths are visited. Consider:

```
/project/
├── data/
│   └── file.txt
└── link -> data/file.txt    # Is this escaping?
```

If we process `link` before `data/file.txt`, we don't yet know that `data/file.txt` will be collected. The two-pass approach ensures we have the complete picture before making escaping decisions.

**Transitive Include Targets Algorithm:**

The `TRANSITIVE_INCLUDE_TARGETS` policy preserves all symlinks and recursively collects their targets into the manifest. This ensures the manifest contains all data reachable through symlinks, regardless of where those targets are located on the filesystem.

*Purpose and caller responsibility:*

This policy collects all transitively reachable paths without restriction. The resulting manifest may contain paths from anywhere on the filesystem (e.g., `/usr/lib`, `/home/other_user`, etc.). It is the caller's responsibility to validate that the resulting paths are within acceptable boundaries before using the manifest.

*Algorithm:*

1. During the main collection pass, when a symlink is encountered:
   - Preserve the symlink entry with its absolute target
   - Queue the target path for transitive collection

2. After the main pass, process queued transitive targets:
   - If target is a file: add it to the manifest
   - If target is a symlink: preserve it and queue its target (recursive)
   - If target is a directory: walk it, preserving any nested symlinks and queuing their targets

3. Continue until no new targets are queued (fixed-point)

*Example:*

```
/project/                    # Being collected
└── link1 -> /external/data

/external/
└── data/
    ├── file.txt
    └── link2 -> /other/resource

/other/
└── resource.bin
```

Result with `TRANSITIVE_INCLUDE_TARGETS`:
- `/project/link1` (symlink → `/external/data`)
- `/external/data/file.txt` (file)
- `/external/data/link2` (symlink → `/other/resource`)
- `/other/resource.bin` (file)

All paths reachable through symlinks are included, preserving the symlink structure.

**Symlink Cycle Handling:**

Symlink cycles occur when following symlinks leads back to a previously visited path. Examples:
- Self-referential: `A -> A` (length 1)
- Direct cycle: `A -> B -> A` (length 2)
- Longer cycles: `A -> B -> C -> A` (length 3+)

Cycles can also be revealed during collapsing when symlinks point to intermediate directories:
```
/root/                          # Being collected
├── link_to_external -> /ext    # Escaping symlink, will be collapsed
/ext/
└── link_back -> /root          # Points back to collected root - cycle!
```
In this case, `/root` is being collected, `link_to_external` escapes so it's collapsed (contents
inlined), and when processing `link_back` inside `/ext`, the cycle is detected because `/root`
is already being visited.

The COLLECT operation detects symlink cycles and handles them gracefully:

| Policy | Cycle Behavior |
|--------|----------------|
| `PRESERVE` | No recursion needed; symlinks are recorded as-is with their targets |
| `EXCLUDE_ALL` | No recursion needed; all symlinks are skipped |
| `EXCLUDE_ESCAPING` | Cycles in escaping symlinks are skipped (non-escaping preserved) |
| `COLLAPSE_ALL` | Cycles detected during traversal; cyclic symlink skipped with warning |
| `COLLAPSE_ESCAPING` | Cycles detected when collapsing escaping symlinks; cyclic symlink skipped with warning |
| `TRANSITIVE_INCLUDE_TARGETS` | Cycles detected during transitive collection; cyclic target skipped with warning |

When a cycle is detected:
1. A warning is logged identifying the cyclic symlink
2. The cyclic symlink is skipped to prevent infinite recursion
3. Collection continues with non-cyclic parts of the directory tree
4. Files and directories already collected before the cycle are preserved

**Validation Rules:**

| Condition | Behavior |
|-----------|----------|
| File in `filenames` does not exist | Raises `FileNotFoundError` |
| File in `optional_filenames` does not exist | Silently ignored |
| Directory in `directories` does not exist | Raises `FileNotFoundError` |
| Path in `directories` is not a directory | Raises `ValueError` |
| Path in `filenames` is not a file or symlink | Raises `ValueError` |

**Key implementation details:**

- Files have `hash=None` to indicate hashing is needed
- Symlinks have `symlink_target` set as absolute paths (no hash needed)
- All paths in the manifest are absolute
- Directories are included in the manifest
- Useful for intermediate in-memory processing or collecting from multiple locations

**Example - Collecting from multiple directories:**

```python
from deadline.job_attachments._snapshots import (
    collect_abs_snapshot,
    SymlinkPolicy,
)

# Collect files from different locations using absolute paths (default: COLLAPSE_ESCAPING)
manifest = collect_abs_snapshot(
    ["/data/shared/models", "/data/shared/textures"],  # directories (positional)
    ["/home/user/project/scene.blend"],                 # filenames (positional)
    optional_filenames=["/home/user/project/cache.bin"],  # Included if exists
)

# Paths in manifest are absolute
for entry in manifest.files[:2]:
    print(f"  {entry.path}")  # e.g., "/data/shared/models/car.obj"
```

**Example - Escaping symlinks are collapsed by default:**

```python
from deadline.job_attachments._snapshots import (
    collect_abs_snapshot
)

# Collect with COLLAPSE_ESCAPING (default behavior)
# Symlinks pointing within the collected paths are preserved
# Symlinks pointing outside are collapsed to files/directories
manifest = collect_abs_snapshot(
    ["/projects/my_scene"],  # directories
    [],                       # filenames (empty list)
)

# Non-escaping symlinks have absolute targets
for entry in manifest.files:
    if entry.symlink_target:
        print(f"  symlink: {entry.path} -> {entry.symlink_target}")
        # e.g., symlink: /projects/my_scene/link.txt -> /projects/my_scene/target.txt
```

**Helper functions:**

- `_create_unhashed_file_entry()` - Creates file entry with `hash=None` and metadata
- `_handle_symlink()` - Handles symlink according to policy

### 2. HASH: `hash_abs_manifest()`

**Location:** `_hash_abs_manifest.py`

Fills in hashes for a manifest that was created by `collect_abs_snapshot()` or `diff_snapshots()`. The input manifest must have absolute paths.

```python
def hash_abs_manifest(
    manifest: AbsManifest,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    file_chunk_size_bytes: Optional[int] = None,
) -> AbsManifest:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | Manifest with absolute paths and `hash=None` for unhashed files. Can be either a snapshot (from `collect_abs_snapshot`) or a diff (from `diff_snapshots` with `ignore_hashes=True`) |
| `hash_cache` | Optional hash cache for efficiency |
| `force_rehash` | If `True`, ignore cache and recalculate all hashes |
| `file_chunk_size_bytes` | Chunk size for output manifest. `None` = preserve from input manifest. `WHOLE_FILE_CHUNK_SIZE` (-1) = no chunking. Positive int = chunk size in bytes. |

**Returns:** A NEW `AbsManifest` (either `AbsSnapshot` or `AbsSnapshotDiff`) with all hashes filled in. The manifest type (snapshot/diff) and `parentManifestHash` are preserved from the input.

**Raises:**
- `ValueError` if the manifest contains relative paths
- `ValueError` if any regular file entry already has a hash (is not unhashed)

**Input Validation:**

The HASH operation validates that all regular file entries are unhashed (`hash=None` and `chunkhashes=None`). This validation:
- Prevents accidental re-hashing of already-hashed manifests
- Ensures the operation is safe to call only once per manifest
- Catches programming errors where a hashed manifest is passed incorrectly

To re-hash a manifest (e.g., after file modifications), call `clear_hashes()` on the manifest first to reset it to an unhashed state.

**Hash cache behavior:**

| Condition | Behavior |
|-----------|----------|
| `hash_cache` provided, `force_rehash=False` | Check cache by (path, mtime); use cached hash on hit |
| `hash_cache` provided, `force_rehash=True` | Always compute hash, update cache |
| `hash_cache` is None | Always compute hash |

**Hash cache key resolution:**

Cache keys are generated by resolving the file path using `Path.resolve()`, which:
- Resolves any symlinks in the path components
- Normalizes `.` and `..` components
- Returns an absolute path

This ensures that different paths referring to the same physical file (e.g., via symlinks or relative references) share the same cache entry.

**Hash cache byte-range support:**

The hash cache supports caching hashes for both whole files and arbitrary byte ranges, enabling efficient caching for any chunking scheme:

| Entry Type | `range_start` | `range_end` | Description |
|------------|---------------|-------------|-------------|
| Whole-file | 0 | `WHOLE_FILE_RANGE_END` (-1) | Hash of entire file |
| Byte-range | ≥ 0 | > 0 | Hash of bytes in range [start, end) |

Cache entries are keyed by `(file_path, hash_algorithm, range_start, range_end)`. This allows:
- Caching whole-file hashes alongside chunk hashes for the same file
- Supporting different chunk sizes without cache invalidation
- Reusing cached chunk hashes when chunk boundaries align

When looking up or storing a hash, the `range_start` and `range_end` parameters specify which portion of the file the hash covers. The `WHOLE_FILE_RANGE_END` constant (-1) is a sentinel value indicating a whole-file hash.

**Chunking behavior (controlled by `manifest.fileChunkSizeBytes`):**

| `fileChunkSizeBytes` | File Size | Behavior |
|---------------------|-----------|----------|
| `DEFAULT_FILE_CHUNK_SIZE` (256MB) | ≤ chunk size | Compute single `hash` (default) |
| `DEFAULT_FILE_CHUNK_SIZE` (256MB) | > chunk size | Compute `chunkhashes` (one per chunk) |
| `WHOLE_FILE_CHUNK_SIZE` (-1) | Any | Hash entire file as a whole (no chunking) |
| Positive int (e.g., 64MB) | ≤ chunk size | Compute single `hash` |
| Positive int (e.g., 64MB) | > chunk size | Compute `chunkhashes` (one per chunk) |

When chunking is enabled and a file is larger than the chunk size:
- `hash` field is `None`
- `chunkhashes` contains list of hashes, one per chunk
- Chunk count equals `ceil(size / fileChunkSizeBytes)`

**Entry type handling:**

| Entry Type | Action |
|------------|--------|
| Regular file (no chunking or ≤ chunk size) | Compute single hash |
| Large file (> chunk size, when chunking enabled) | Compute chunkhashes |
| Symlink | Pass through unchanged |
| Deleted marker | Pass through unchanged (diff manifests only) |
| Directory | Pass through unchanged |

**Manifest type handling:**

| Manifest Type | Behavior |
|---------------|----------|
| Snapshot | All file entries are hashed |
| Diff | Only new/modified file entries are hashed; deleted entries pass through unchanged |

The `parentManifestHash` field is preserved from the input manifest. The manifest type is determined by the class (e.g., `AbsSnapshotDiff`, `Snapshot`).

**Helper functions:**

- `_get_or_compute_hash()` - Gets hash from cache or computes it (supports byte ranges)
- `_hash_file_chunked()` - Hashes large file in chunks with cache support

**Example - Hashing a snapshot:**

```python
from deadline.job_attachments._snapshots import (
    collect_abs_snapshot,
    hash_abs_manifest,
)
from deadline.job_attachments.caches.hash_cache import HashCache

# Collect the directory tree with absolute paths
abs_manifest = collect_abs_snapshot(
    ["/projects/my_scene"],  # directories
    [],                       # filenames
)

# Hash with a cache for efficiency
with HashCache("/tmp/hash_cache") as cache:
    hashed = hash_abs_manifest(
        manifest=abs_manifest,
        hash_cache=cache,
        force_rehash=False,  # Use cached hashes when available
    )

# Now entries have their hashes filled in (paths are still absolute)
for entry in hashed.files[:2]:
    if entry.symlink_target:
        print(f"  symlink: {entry.path} -> {entry.symlink_target}")
    elif entry.chunkhashes:
        print(f"  large file: {entry.path} ({len(entry.chunkhashes)} chunks)")
    else:
        print(f"  file: {entry.path} hash={entry.hash[:16]}...")
```

Output:
```
  file: /projects/my_scene/assets/model.blend hash=a1b2c3d4e5f67890...
  large file: /projects/my_scene/renders/output.exr (3 chunks)
```

**Example - Hashing a diff manifest:**

```python
from deadline.job_attachments._snapshots import (
    diff_snapshots,
    hash_abs_manifest,
)

# Compute a diff between two snapshots (with ignore_hashes=True for fast comparison)
diff = diff_snapshots(
    parent=parent_snapshot,
    current=current_snapshot,
    parent_manifest_hash="abc123...",
    ignore_hashes=True,  # Compare by mtime/size only
)

# Now hash the diff to fill in hashes for new/modified files
hashed_diff = hash_abs_manifest(diff)

# Deleted entries are preserved unchanged
for entry in hashed_diff.files:
    if entry.deleted:
        print(f"  deleted: {entry.path}")
    else:
        print(f"  new/modified: {entry.path} hash={entry.hash[:16]}...")
```

### 3. HASH_UPLOAD: `hash_upload_abs_manifest()`

**Location:** `_hash_upload_abs_manifest.py` (main entry point), with pipeline implementation split across:
- `_hash_upload_abs_manifest_pipeline.py` - Base pipeline class, progress state, work items, memory pool
- `_hash_upload_abs_manifest_s3_pipeline.py` - S3-specific upload logic (multipart, streaming)
- `_hash_upload_abs_manifest_file_system_pipeline.py` - FileSystem-specific upload logic

Fills in hashes for a manifest AND uploads file content to a data cache in a pipelined manner. This operation combines hashing and uploading into a single pass over the data, avoiding the need to read files twice (once for hashing, once for uploading).

```python
def hash_upload_abs_manifest(
    manifest: AbsManifest,
    data_cache: ContentAddressedDataCache,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    max_memory_bytes: Optional[int] = None,
    max_workers: Optional[int] = None,
    file_chunk_size_bytes: Optional[int] = None,
    on_progress: Optional[HashUploadProgressCallback] = None,
) -> UploadResult:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | Manifest with absolute paths and `hash=None` for unhashed files. Can be either a snapshot (from `collect_abs_snapshot`) or a diff (from `diff_snapshots` with `ignore_hashes=True`) |
| `data_cache` | Content-addressable data cache destination. Either `S3DataCache` for cloud storage or `FileSystemDataCache` for local/network storage. |
| `hash_cache` | Optional hash cache for efficiency |
| `force_rehash` | If `True`, ignore cache and recalculate all hashes |
| `max_memory_bytes` | Maximum memory to use for buffering (default: auto-detect) |
| `max_workers` | Maximum number of parallel workers (default: 10) |
| `file_chunk_size_bytes` | Chunk size for output manifest. `None` = preserve from input manifest. `WHOLE_FILE_CHUNK_SIZE` (-1) = no chunking. Positive int = chunk size in bytes. |
| `on_progress` | Optional callback for progress reporting. Called periodically with `HashUploadProgressMetadata`. Return `True` to continue, `False` to cancel. |

**Progress Reporting:**

The `on_progress` callback receives `HashUploadProgressMetadata` with separate tracking for hashing and uploading phases:

```python
@dataclass
class HashUploadProgressMetadata:
    # Totals
    total_file_chunks: int  # Total files + chunks to process
    total_bytes: int

    # Hashing phase progress
    hashed_file_chunks: int
    hashed_bytes: int
    hash_skipped_file_chunks: int  # Skipped due to hash cache hit
    hash_skipped_bytes: int

    # Upload phase progress
    uploaded_file_chunks: int
    uploaded_bytes: int
    upload_skipped_file_chunks: int  # Skipped because already in data cache
    upload_skipped_bytes: int

    # Overall progress (based on upload completion, which is the final stage)
    progress: float  # 0-100
    progressMessage: str
```

The callback type is:
```python
HashUploadProgressCallback = Callable[[HashUploadProgressMetadata], bool]
```

**Progress Callback Behavior:**

| Behavior | Description |
|----------|-------------|
| Invocation interval | Called at most every 0.2 seconds (5 times per second) |
| Final callback | Always called at operation completion via `force_callback()` |
| Cancellation | Return `False` from callback to cancel the operation |
| Thread safety | Callback is invoked from worker threads; metadata is built under lock |

**Progress Field Semantics:**

For chunked files, each chunk is counted separately in `total_file_chunks`, `hashed_file_chunks`, etc.

| Field | When Incremented |
|-------|------------------|
| `hashed_bytes` / `hashed_file_chunks` | After hash computation completes for a file or chunk |
| `hash_skipped_bytes` / `hash_skipped_file_chunks` | When hash cache hit allows skipping hash computation |
| `uploaded_bytes` / `uploaded_file_chunks` | After upload completes for a file or chunk |
| `upload_skipped_bytes` / `upload_skipped_file_chunks` | When data cache already contains the content |

**Example - Progress callback:**

```python
from deadline.job_attachments._snapshots import (
    hash_upload_abs_manifest,
    HashUploadProgressMetadata,
)

def on_progress(metadata: HashUploadProgressMetadata) -> bool:
    print(f"Progress: {metadata.progress:.1f}% - {metadata.progressMessage}")
    # Return False to cancel, True to continue
    return True

result = hash_upload_abs_manifest(
    manifest=abs_manifest,
    data_cache=s3_cache,
    on_progress=on_progress,
)
```

**Returns:** `UploadResult` containing:
- `statistics`: `HashUploadProgressMetadata` with detailed hash/upload metrics (see below)
- `manifest`: A NEW `AbsManifest` (either `AbsSnapshot` or `AbsSnapshotDiff`) with all hashes filled in

**UploadResult Statistics:**

The `statistics` field contains a `HashUploadProgressMetadata` object with separate tracking for hashing and uploading phases:

| Field | Description |
|-------|-------------|
| `total_file_chunks` | Total files + chunks to process |
| `total_bytes` | Total size of all files |
| `hashed_file_chunks` | Number of files/chunks that were hashed |
| `hashed_bytes` | Bytes that were hashed |
| `hash_skipped_file_chunks` | Files/chunks skipped due to hash cache hit |
| `hash_skipped_bytes` | Bytes skipped due to hash cache hit |
| `uploaded_file_chunks` | Number of files/chunks that were uploaded |
| `uploaded_bytes` | Bytes that were uploaded |
| `upload_skipped_file_chunks` | Files/chunks skipped (already in data cache) |
| `upload_skipped_bytes` | Bytes skipped (already in data cache) |
| `progress` | Overall progress percentage (0-100) |
| `progressMessage` | Human-readable summary message |

Files/chunks are skipped when:
1. The hash cache has the file's hash AND the data cache already contains that hash
2. The HeadObject check finds the object already exists in S3

**Raises:**
- `ValueError` if the manifest contains relative paths
- `ValueError` if any regular file entry already has a hash (is not unhashed)

**Input Validation:**

Like the HASH operation, HASH_UPLOAD validates that all regular file entries are unhashed (`hash=None` and `chunkhashes=None`). This prevents accidental re-processing of already-hashed manifests and catches programming errors early.

**Pipelined Architecture:**

The operation uses two thread pools connected by a bounded memory pool:

```
┌─────────────────────────────────────────────────────────────────┐
│                    READ + HASH POOL                             │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐                           │
│  │ Worker  │ │ Worker  │ │ Worker  │  (max_workers threads)    │
│  │  1      │ │  2      │ │  N      │                           │
│  └────┬────┘ └────┬────┘ └────┬────┘                           │
└───────┼──────────┼──────────┼──────────────────────────────────┘
        │          │          │
        │    allocate(size)   │   ← blocks when pool is full
        └──────────┼──────────┘
                   ▼
    ┌─────────────────────────────────┐
    │  Memory Pool                    │
    │  (bounded by max_memory_bytes)  │
    │                                 │
    │  READ+HASH fills ──► UPLOAD drains
    └─────────────────────────────────┘
                   │
        release(size)   ← frees space for more reads
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────┐
│                      UPLOAD POOL                                │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐                           │
│  │ Worker  │ │ Worker  │ │ Worker  │  (max_workers threads)    │
│  │  1      │ │  2      │ │  N      │                           │
│  └─────────┘ └─────────┘ └─────────┘                           │
│                                                                 │
│  For multipart uploads, each part is a separate upload task    │
└─────────────────────────────────────────────────────────────────┘
```

**Key Design Points:**

1. **Two thread pools:** READ+HASH pool reads files and computes hashes; UPLOAD pool handles uploads
2. **Memory pool as bounded buffer:** READ+HASH allocates memory before reading, blocks when full; UPLOAD releases memory after completing, unblocking readers
3. **Parallel multipart:** For S3, large files use multipart upload with parts uploaded in parallel
4. **Cache-specific pipelines:** `S3HashUploadPipeline` and `FileSystemHashUploadPipeline` implement cache-specific upload logic

**Multipart Upload Conditions (S3 only):**

Multipart upload is used when:
- Uploading to `S3DataCache` (not `FileSystemDataCache`)
- Chunk size >= `2 * multipart_part_size` (default threshold: 64MB with 32MB parts)
- For streaming files (> `max_memory_bytes`), file size > multipart threshold

**Part Scheduling (S3 only):**

- **Chunked files >= multipart threshold:** Read entire chunk into memory, hash incrementally, then submit all parts for parallel upload
- **Streaming files (> max_memory_bytes):** Two-pass approach - first pass computes hash (discards data), second pass reads and submits parts one at a time with memory throttling

**Chunking and Multipart Upload Behavior (S3 only):**

The multipart threshold is `2 * S3DataCache.multipart_part_size` (default: 64MB with 32MB parts).

| `fileChunkSizeBytes` | File Size | vs Multipart Threshold | Processing |
|---------------------|-----------|------------------------|------------|
| `DEFAULT_FILE_CHUNK_SIZE` (256MB) | ≤ chunk size | < threshold | Single chunk: read+hash → single PUT upload |
| `DEFAULT_FILE_CHUNK_SIZE` (256MB) | ≤ chunk size | >= threshold | Single chunk: read+hash → multipart upload |
| `DEFAULT_FILE_CHUNK_SIZE` (256MB) | > chunk size | Any | Multiple chunks, each >= threshold uses multipart |
| `WHOLE_FILE_CHUNK_SIZE` (-1) | ≤ `max_memory_bytes` | < threshold | Single pass: read+hash → single PUT upload |
| `WHOLE_FILE_CHUNK_SIZE` (-1) | ≤ `max_memory_bytes` | >= threshold | Single pass: read+hash → parallel multipart upload |
| `WHOLE_FILE_CHUNK_SIZE` (-1) | > `max_memory_bytes` | > threshold | Two-pass: hash first (discard data), then parallel multipart with memory throttling |
| Positive int (e.g., 64MB) | ≤ chunk size | < threshold | Single chunk: read+hash → single PUT upload |
| Positive int (e.g., 64MB) | ≤ chunk size | >= threshold | Single chunk: read+hash → multipart upload |
| Positive int (e.g., 64MB) | > chunk size | Any | Multiple chunks, each >= threshold uses multipart |

**Multipart Upload Coordination (S3 only):**

For files using multipart upload, the system uses two coordination structures:

*`_MultipartUploadState` - Tracks the overall multipart upload:*

| Field | Description |
|-------|-------------|
| `file_hash` | Final hash of the complete file |
| `s3_key` | S3 object key for the upload |
| `upload_id` | S3 multipart upload ID from `CreateMultipartUpload` |
| `parts_remaining` | Atomic counter of parts still being uploaded |
| `completed_parts` | List of `{"PartNumber": int, "ETag": str}` for `CompleteMultipartUpload` |
| `part_hashes` | Per-part hashes for verification (streaming files only) |
| `part_errors` | Errors from failed part uploads |
| `lock` | Thread lock for safe concurrent updates |

*`_MultipartPartWorkItem` - Represents a single part to upload:*

| Field | Description |
|-------|-------------|
| `multipart_state` | Reference to the parent `_MultipartUploadState` |
| `part_number` | 1-based part number for S3 |
| `data` | Part content bytes to upload |
| `expected_hash` | Hash for verification (streaming files only) |

*Coordination flow:*

1. **Initiate:** Create `_MultipartUploadState` with `CreateMultipartUpload`, set `parts_remaining` to total part count
2. **Submit parts:** Create `_MultipartPartWorkItem` for each part, submit to upload thread pool
3. **Part completion:** Each part uploads independently; on success, atomically:
   - Append `{"PartNumber", "ETag"}` to `completed_parts`
   - Decrement `parts_remaining`
4. **Finalize:** The thread that decrements `parts_remaining` to 0 calls `CompleteMultipartUpload`
5. **Error handling:** On any part failure, record error in `part_errors` and call `AbortMultipartUpload`

**Streaming File Handling:**

When chunking is disabled (`WHOLE_FILE_CHUNK_SIZE`) and a file is larger than `max_memory_bytes`:

For S3:
- **Pass 1:** Stream through file computing full file hash AND per-part hashes (discard data to avoid OOM)
- **Pass 2:** Stream through file again, submitting parts one at a time with memory throttling; each part verifies its hash before upload
- If any part's hash doesn't match, the multipart upload is aborted and a `ValueError` is raised

For FileSystem:
- **Pass 1:** Stream through file computing hash (discard data)
- **Pass 2:** Stream copy to destination while re-verifying full file hash

This verification ensures that if a file is modified between passes, the error is detected and the upload fails cleanly rather than uploading corrupted data.

When chunking is enabled (positive `fileChunkSizeBytes`):
- `max_memory_bytes` must be >= `fileChunkSizeBytes` (raises `ValueError` otherwise)
- Large files are processed chunk by chunk through the pipeline

**Memory Management:**

The pipeline constrains total memory usage across both stages:

- When `max_memory_bytes` is reached, the READ+HASH stage blocks until UPLOAD completes and frees memory
- Each chunk occupies memory from READ+HASH through UPLOAD completion

**Concurrent Upload Deduplication:**

When multiple files or chunks have identical content (same hash), the pipeline prevents redundant
concurrent uploads. This is particularly valuable for:
- Files with repeated chunks (e.g., sparse files, files with repeated patterns)
- Multiple files with identical content in the same upload batch
- Large datasets with many duplicate files

The deduplication mechanism uses a hash-to-event map to coordinate concurrent uploads:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    UPLOAD DEDUPLICATION                                 │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Thread A (hash=abc123):              Thread B (hash=abc123):           │
│  ┌─────────────────────┐              ┌─────────────────────┐           │
│  │ 1. Check map        │              │ 1. Check map        │           │
│  │    hash not found   │              │    hash found!      │           │
│  │ 2. Register hash    │              │ 2. Get event        │           │
│  │    with new Event   │              │ 3. Wait on event    │           │
│  │ 3. Upload data      │              │    (blocks)         │           │
│  │ 4. Signal event     │──────────────│ 4. Event signaled   │           │
│  │ 5. Remove from map  │              │ 5. Skip upload      │           │
│  └─────────────────────┘              │ 6. Release memory   │           │
│                                       └─────────────────────┘           │
│                                                                         │
│  Result: Only ONE upload to data cache, both threads complete           │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

*Implementation details:*

| Field | Description |
|-------|-------------|
| `_uploading_hashes` | `Dict[str, threading.Event]` mapping hash → completion event |
| `_uploading_hashes_lock` | Lock protecting the map for thread-safe access |

*Coordination flow:*

1. **First thread with hash:** Registers hash in map with a new `Event`, proceeds to upload
2. **Subsequent threads with same hash:** Find existing entry, wait on the `Event`
3. **Upload completion:** First thread signals `Event` and removes hash from map
4. **Waiting threads:** Wake up, mark item as skipped, release memory, record result

*Benefits:*

- Eliminates redundant network I/O for duplicate content
- Reduces data cache write operations
- Memory is released promptly for skipped uploads
- Progress tracking correctly counts skipped uploads

This deduplication is separate from the S3 check cache (which prevents re-uploading content that
already exists in the data cache from previous operations). The concurrent deduplication handles
duplicates within a single HASH_UPLOAD operation.

**Default Memory Limit Calculation:**

When `max_memory_bytes` is not specified, the default is calculated as:

| Option | Value | Rationale |
|--------|-------|-----------|
| Minimum | 256MB | One chunk must fit for default 256MB chunk size; worst case processes one chunk at a time |
| Maximum | 16GB | Cap to avoid excessive memory usage on high-memory systems |
| Quarter of total | `total_memory / 4` | Use a reasonable portion of system resources |
| Available minus 1GB | `available_memory - 1GB` | When lots of free memory exists (e.g., 60GB), use most of it |

```python
default_limit = min(16GB, max(256MB, total_memory // 4, available_memory - 1GB))
```

**Example calculations:**

| System | Total | Available | Quarter | Avail-1GB | Result |
|--------|-------|-----------|---------|-----------|--------|
| Low memory | 4GB | 2GB | 1GB | 1GB | 1GB |
| Typical workstation | 32GB | 20GB | 8GB | 19GB | 16GB |
| High memory server | 128GB | 100GB | 32GB | 99GB | 16GB |
| Constrained (busy) | 32GB | 1.5GB | 8GB | 0.5GB | 8GB |

This ensures the pipeline uses as much memory as safely available while maintaining a reasonable lower bound and capping at 16GB to avoid excessive memory usage.

**Storage Key Format:**

Files are stored in content-addressable format with keys/paths based on the data cache type:

| Data Cache Type | Key Format | Example |
|-----------------|------------|---------|
| `S3DataCache` | `{s3_key_prefix}/{hash}.{algorithm}` | `Data/a1b2c3d4e5f67890abcdef1234567890.xxh128` |
| `FileSystemDataCache` | `{root_path}/{hash}.{algorithm}` | `/mnt/cache/a1b2c3d4e5f67890abcdef1234567890.xxh128` |

For chunked files, each chunk is stored separately:
```
{prefix}/{chunk0_hash}.xxh128
{prefix}/{chunk1_hash}.xxh128
...
```

**Cache Integration:**

| Cache | Purpose | Location |
|-------|---------|----------|
| `hash_cache` | Skip hashing for files with unchanged mtime | `hash_upload_abs_manifest()` parameter |
| `s3_check_cache` | Skip upload for files already in S3 | `S3DataCache` member |

When both caches hit (for `S3DataCache`), the file is completely skipped (no read, no hash, no upload).

For `FileSystemDataCache`, the `object_exists()` method checks the local filesystem directly, so no separate check cache is needed.

**Cache Check Architecture:**

All cache checks (hash cache, S3 check cache, and HeadObject fallback) are performed **inside the
worker thread pool**, not on the main thread. This design choice provides significant performance
benefits:

```
┌─────────────────────────────────────────────────────────────────┐
│                     Worker Thread (per item)                     │
├─────────────────────────────────────────────────────────────────┤
│  1. Check hash cache (thread-local SQLite connection)           │
│     └─► If hit + mtime match, get cached_hash                   │
│                                                                  │
│  2. Check if object exists in data cache:                       │
│     a. S3 check cache lookup (thread-local SQLite)              │
│     b. If miss, HeadObject call to S3                           │
│     └─► If exists, mark as skipped (no memory allocation)       │
│                                                                  │
│  3. If object doesn't exist (need to upload):                   │
│     a. Allocate memory from pool                                │
│     b. Read file chunk from disk                                │
│     c. Compute actual hash                                      │
│     d. If actual hash != cached_hash:                           │
│        - Re-check HeadObject with actual hash                   │
│        - If exists, skip upload (release memory)                │
│     e. Submit to upload stage                                   │
└─────────────────────────────────────────────────────────────────┘
```

**Why we re-read and re-hash when S3 misses (even with hash cache hit):**

When the hash cache hits but the object doesn't exist in S3, we must re-read the file and
compute the hash ourselves before uploading. We cannot trust the hash cache alone for uploads
because:

1. The hash cache could be stale (file changed but mtime check passed due to clock skew)
2. The hash cache could be corrupted
3. We need the actual file data to upload anyway

The hash cache is only trusted for **skipping** when the data already exists in S3 (verified
by HeadObject). For uploads, we always verify by reading and hashing the actual file content.

**Why we re-check HeadObject when hash changes:**

If the computed hash differs from the cached hash, the file content has changed. The new hash
might already exist in S3 from a previous upload of identical content (content-addressable
storage). Rather than uploading redundantly, we do one more HeadObject check with the new hash.

This handles the scenario:
1. Hash cache has stale `hash_A` (file was modified)
2. HeadObject(`hash_A`) returns 404 (original content deleted or never uploaded)
3. We read file, compute `hash_B` (current content)
4. HeadObject(`hash_B`) returns 200 (content exists from another upload)
5. Skip upload - no redundant transfer

**Why cache checks are in worker threads (not main thread):**

1. **Parallelizes HeadObject calls:** When the S3 check cache misses, HeadObject calls to S3
   take ~15ms each. With 2000 chunks, serial execution takes ~30 seconds. With 8 workers
   in parallel, this drops to ~4 seconds.

2. **Memory efficiency:** By checking caches before allocating memory, items that will be
   skipped never consume memory pool resources.

3. **Thread-safe caches:** Both `HashCache` and `S3CheckCache` use thread-local SQLite
   connections (`get_local_connection()`), making concurrent access safe and efficient.

4. **Simpler code flow:** Each worker handles its item end-to-end, from cache check through
   upload, rather than splitting logic between main thread and workers.

**Performance comparison (25GB, 1920 chunks, warm hash cache, cold S3 check cache):**

| Architecture | HeadObject Time | Reason |
|--------------|-----------------|--------|
| Main thread (serial) | ~30 seconds | 1920 × 15ms = 28.8s |
| Worker threads (8 workers) | ~4 seconds | Parallelized across workers |

**Probabilistic S3 Cache Validation:**

The S3 check cache can become stale if objects are deleted from S3 (e.g., by lifecycle policies,
manual deletion, or bucket recreation). To detect this without sacrificing performance, the
HASH_UPLOAD operation performs probabilistic validation during upload.

Note: The legacy `upload.py` implementation performs a pre-upload check of 30 random cached
entries before starting uploads. The new snapshots-based approach instead performs inline
validation during the upload itself, which provides better coverage and automatic recovery.

```
┌─────────────────────────────────────────────────────────────────┐
│              Probabilistic S3 Cache Validation                   │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  For each item where S3 check cache says "exists":               │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  Item 1-100:     Always verify with HeadObject          │    │
│  │  Item 101+:      1% random sampling (HeadObject)        │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  If ANY verification fails (object missing from S3):            │
│                                                                  │
│  1. Mark cache as invalid (set invalidation flag)               │
│  2. Close and delete the S3 check cache database                │
│  3. Re-queue all previously skipped items for upload            │
│  4. Continue processing remaining items without cache           │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**Sampling Strategy:**

| Item Number | Verification | Rationale |
|-------------|--------------|-----------|
| 1-100 | Always HeadObject | Catch stale cache early with high confidence |
| 101+ | 1% random sample | Balance validation coverage vs. performance |

The first 100 items provide early detection—if the cache is completely stale (e.g., bucket
was recreated), we'll detect it within the first 100 items with near certainty. The 1%
sampling for remaining items catches partial staleness (e.g., some objects deleted by
lifecycle policy) while keeping HeadObject overhead minimal.

**Expected HeadObject overhead for warm cache scenarios:**

| Total Items | Verified Items | HeadObject Time (8 workers) |
|-------------|----------------|----------------------------|
| 100 | 100 | ~0.2 seconds |
| 1,000 | 109 | ~0.2 seconds |
| 10,000 | 199 | ~0.4 seconds |
| 100,000 | 1,099 | ~2.1 seconds |

**Recovery Flow:**

When a cache miss is detected during validation:

```
┌─────────────────────────────────────────────────────────────────┐
│                     Cache Invalidation Recovery                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. DETECT: HeadObject returns 404 for cached item              │
│     └─► Set cache_invalidated flag (atomic)                     │
│                                                                  │
│  2. INVALIDATE: Close and delete S3 check cache database        │
│     └─► Prevents further cache hits                             │
│                                                                  │
│  3. RE-QUEUE: Collect all items that were skipped due to cache  │
│     └─► These items trusted the now-invalid cache               │
│                                                                  │
│  4. RETRY: Re-submit skipped items to the pipeline              │
│     └─► Items now go through HeadObject (no cache)              │
│     └─► Upload if object truly missing, skip if exists          │
│                                                                  │
│  5. CONTINUE: Process remaining items without cache             │
│     └─► All subsequent items use HeadObject directly            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**Implementation Details:**

The `_TaskBasedPipeline` class tracks validation state:

```python
@dataclass
class _S3CacheValidationState:
    """Tracks probabilistic S3 cache validation during upload."""

    # Counters for sampling decision
    cache_hit_count: int = 0  # Total items where cache said "exists"

    # Validation results
    cache_invalidated: bool = False  # Set True on first validation failure

    # Items to retry if cache is invalidated
    skipped_items: List[PipelineWorkItem] = field(default_factory=list)

    # Lock for thread-safe updates
    lock: threading.Lock = field(default_factory=threading.Lock)

    def should_verify(self) -> bool:
        """Determine if this cache hit should be verified with HeadObject."""
        with self.lock:
            self.cache_hit_count += 1
            if self.cache_hit_count <= 100:
                return True  # Always verify first 100
            # 1% random sampling for items 101+
            return random.random() < 0.01
```

**Why This Approach:**

1. **Early detection:** First 100 checks catch completely stale caches quickly
2. **Minimal overhead:** 1% sampling adds negligible latency for large uploads
3. **Self-healing:** Automatic recovery without user intervention
4. **No data loss:** Re-queued items are uploaded if truly missing
5. **Graceful degradation:** After invalidation, falls back to HeadObject for all items

**Entry Type Handling:**

| Entry Type | Action |
|------------|--------|
| Regular file (fits in memory or no chunking) | Read+Hash → Upload (single pass) |
| Large file (> memory, no chunking, S3) | Two-pass streaming with parallel multipart (see below) |
| Large file (> memory, no chunking, filesystem) | Stream read+hash → Stream upload (two-pass) |
| Large file (chunking enabled) | Read+Hash → Upload (per chunk) |
| Symlink | Pass through unchanged (no upload) |
| Deleted marker | Pass through unchanged (no upload) |
| Directory | Pass through unchanged (no upload) |

**Streaming Files Larger Than Memory Buffer (S3):**

When uploading to S3 with `WHOLE_FILE_CHUNK_SIZE` (-1) and a file exceeds `max_memory_bytes`, the system uses a two-pass approach with parallel multipart upload:

```
┌─────────────────────────────────────────────────────────────────┐
│                    READ+HASH Thread Pool                         │
├─────────────────────────────────────────────────────────────────┤
│  Pass 1: Compute Hash (discard data)                            │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  Read part 1 → hash → discard                           │    │
│  │  Read part 2 → hash → discard                           │    │
│  │  ...                                                     │    │
│  │  Read part N → hash → discard → final_hash              │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  Check if object exists in S3 (HeadObject with final_hash)      │
│  └─► If exists, skip upload (done)                              │
│                                                                  │
│  Pass 2: Read and Submit Parts (with memory throttling)         │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  Create S3 multipart upload session                      │    │
│  │  For each part:                                          │    │
│  │    1. Allocate memory (blocks if pool exhausted)         │    │
│  │    2. Read part data into buffer                         │    │
│  │    3. Submit part to UPLOAD pool                         │    │
│  │    4. Continue to next part (don't wait for upload)      │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      UPLOAD Thread Pool                          │
├─────────────────────────────────────────────────────────────────┤
│  Parts upload in parallel:                                       │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐                            │
│  │ Part 1  │ │ Part 2  │ │ Part 3  │  ...                       │
│  │ Upload  │ │ Upload  │ │ Upload  │                            │
│  │ (free   │ │ (free   │ │ (free   │                            │
│  │ memory) │ │ memory) │ │ memory) │                            │
│  └─────────┘ └─────────┘ └─────────┘                            │
│                                                                  │
│  Last part to complete → CompleteMultipartUpload                │
│  On any error → AbortMultipartUpload                            │
└─────────────────────────────────────────────────────────────────┘
```

**Why Two Passes?**

1. **Hash-before-upload:** S3 object keys are based on content hash, so we must know the hash before creating the multipart upload session
2. **Memory efficiency:** By discarding data in pass 1, we avoid buffering the entire file
3. **Skip optimization:** If the object already exists (HeadObject hit), we skip pass 2 entirely

**Memory Throttling in Pass 2:**

- Each part allocates from the memory pool before reading
- If the pool is exhausted, the READ+HASH thread blocks until UPLOAD threads free memory
- This creates backpressure: parts are submitted at the rate they can be uploaded
- Maximum memory usage is bounded by `max_memory_bytes`

**Example: 1GB file with 128MB max_memory and 32MB parts (default):**

1. Pass 1: Stream through 1GB computing hash (no memory allocation)
2. HeadObject check with computed hash
3. Pass 2: Submit 32 parts (1GB / 32MB)
   - At any time, at most 4 parts buffered (128MB / 32MB)
   - As uploads complete, memory is freed for new parts

The part size is determined by `S3DataCache.multipart_part_size` (default: 32MB).

**Manifest type handling:**

| Manifest Type | Behavior |
|---------------|----------|
| Snapshot | All file entries are hashed and uploaded |
| Diff | Only new/modified file entries are hashed and uploaded; deleted entries pass through unchanged |

The `parentManifestHash` and `fileChunkSizeBytes` fields are preserved from the input manifest. The manifest type is determined by the class (e.g., `AbsSnapshotDiff`, `Snapshot`).

**Error Handling:**

- If upload fails, the operation raises an exception with details
- Partial uploads are not cleaned up (S3 content-addressable storage is idempotent)
- The hash cache is updated even if upload fails (hash is still valid)

**Example - Uploading to S3:**

```python
import boto3
from deadline.job_attachments._snapshots import (
    collect_abs_snapshot,
    hash_upload_abs_manifest,
    S3DataCache,
)
from deadline.job_attachments.caches.hash_cache import HashCache
from deadline.job_attachments.caches.s3_check_cache import S3CheckCache

# Collect the directory tree with absolute paths
abs_manifest = collect_abs_snapshot(
    ["/projects/my_scene"],  # directories
    [],                       # filenames
)

# Create S3 data cache with client and optional check cache
with HashCache("/tmp/hash_cache") as hash_cache:
    with S3CheckCache("/tmp/s3_cache") as s3_cache:
        data_cache = S3DataCache(
            s3_bucket="my-job-attachments-bucket",
            s3_key_prefix="Data",
            s3_client=boto3.client("s3"),
            s3_check_cache=s3_cache,
        )

        # Hash and upload in a single pipelined pass
        result = hash_upload_abs_manifest(
            manifest=abs_manifest,
            data_cache=data_cache,
            hash_cache=hash_cache,
            max_memory_bytes=1024 * 1024 * 1024,  # 1GB memory limit
        )

# Print upload statistics
stats = result.statistics
print(f"Hashed {stats.hashed_bytes} bytes, skipped {stats.hash_skipped_bytes} bytes (cache hit)")
print(f"Uploaded {stats.uploaded_bytes} bytes, skipped {stats.upload_skipped_bytes} bytes (already in cache)")
print(f"Progress: {stats.progressMessage}")

# Now entries have their hashes filled in AND files are uploaded (paths are still absolute)
for entry in result.manifest.files[:2]:
    if entry.symlink_target:
        print(f"  symlink: {entry.path} -> {entry.symlink_target}")
    elif entry.chunkhashes:
        print(f"  large file: {entry.path} ({len(entry.chunkhashes)} chunks) - uploaded")
    else:
        print(f"  file: {entry.path} hash={entry.hash[:16]}... - uploaded")
```

Output:
```
  file: /projects/my_scene/assets/model.blend hash=a1b2c3d4e5f67890... - uploaded
  large file: /projects/my_scene/renders/output.exr (3 chunks) - uploaded
```

**Example - Writing to local filesystem (debug snapshot):**

```python
from pathlib import Path
from deadline.job_attachments._snapshots import (
    collect_abs_snapshot,
    hash_upload_abs_manifest,
    FileSystemDataCache,
)
from deadline.job_attachments.caches.hash_cache import HashCache

# Collect the directory tree with absolute paths
abs_manifest = collect_abs_snapshot(
    ["/projects/my_scene"],  # directories
    [],                       # filenames
)

# Create filesystem data cache for debug snapshot
data_cache = FileSystemDataCache(
    root_path=Path("/tmp/debug_snapshot/data"),
)

# Hash and write to local filesystem
with HashCache("/tmp/hash_cache") as hash_cache:
    result = hash_upload_abs_manifest(
        manifest=abs_manifest,
        data_cache=data_cache,
        hash_cache=hash_cache,
    )

# Files are now stored in /tmp/debug_snapshot/data/{hash}.xxh128
print(f"Debug snapshot created with {len(result.manifest.files)} entries")
print(f"Uploaded: {result.statistics.uploaded_bytes} bytes, Skipped: {result.statistics.upload_skipped_bytes} bytes")
```

**Performance Comparison:**

| Approach | Disk Reads | Network Uploads | Memory Peak | S3 Upload Parallelism |
|----------|------------|-----------------|-------------|---------------------|
| HASH then upload | 2× (hash + upload) | 1× | Low | Serial multipart |
| HASH_UPLOAD (new) | 1× | 1× | Bounded by `max_memory_bytes` | Parallel multipart |

For large datasets, HASH_UPLOAD provides:
- Up to 2× faster I/O due to single-pass disk reads
- Significantly faster S3 uploads due to parallel multipart upload of large files
- Better network utilization through concurrent part uploads

**When to Use HASH vs HASH_UPLOAD:**

| Use Case | Recommended Operation |
|----------|----------------------|
| Local manifest creation (no upload) | HASH |
| Diff computation only | HASH |
| Job submission with upload | HASH_UPLOAD |
| Output sync from worker | HASH_UPLOAD |
| Testing/debugging | HASH (simpler) |

### 4. DOWNLOAD: `download_abs_manifest()`

**Location:** `_download_abs_manifest.py`

Downloads files from a data cache (S3 or filesystem) to the local filesystem. For snapshot manifests (`AbsSnapshot`), recreates the directory structure specified in the manifest. For diff manifests (`AbsSnapshotDiff`), applies changes by downloading new/modified files and deleting removed files. The manifest must have absolute paths.

```python
def download_abs_manifest(
    manifest: AbsManifest,
    data_cache: ContentAddressedDataCache,
    *,
    hash_cache: Optional[HashCache] = None,
    file_conflict_resolution: FileConflictResolution = FileConflictResolution.OVERWRITE,
    apply_deletes: bool = True,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.PRESERVE,
    max_workers: Optional[int] = None,
    on_progress: Optional[DownloadProgressCallback] = None,
) -> DownloadResult:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | Manifest with absolute paths and hashes. Can be `AbsSnapshot` or `AbsSnapshotDiff`. |
| `data_cache` | Data cache to download from (`S3DataCache` or `FileSystemDataCache`) |
| `hash_cache` | Optional hash cache to skip downloads for files that already have the correct content (see below). |
| `file_conflict_resolution` | How to handle existing files (see below). Default `OVERWRITE`. Note: When `hash_cache` is provided, files with matching hashes are skipped regardless of this setting. |
| `apply_deletes` | If `True` (default), apply deletions from diff manifests. If `False`, skip deletions and only download new/modified files. |
| `symlink_policy` | How to handle symlinks. Default `PRESERVE`. Only `PRESERVE` and `EXCLUDE_ALL` are supported. |
| `max_workers` | Maximum parallel download workers. Default: 10. |
| `on_progress` | Optional callback for progress reporting. Called periodically with `DownloadProgressMetadata`. Return `True` to continue, `False` to cancel. |

**Progress Reporting:**

The `on_progress` callback receives `DownloadProgressMetadata` with download progress tracking:

```python
@dataclass
class DownloadProgressMetadata:
    # Totals
    total_file_chunks: int  # Total files + chunks to process
    total_bytes: int

    # Download progress
    downloaded_file_chunks: int
    downloaded_bytes: int
    skipped_file_chunks: int  # Skipped due to hash cache hit or conflict resolution
    skipped_bytes: int

    # Overall progress
    progress: float  # 0-100
    progressMessage: str
```

The callback type is:
```python
DownloadProgressCallback = Callable[[DownloadProgressMetadata], bool]
```

**Progress Callback Behavior:**

| Behavior | Description |
|----------|-------------|
| Invocation interval | Called at most every 0.2 seconds (5 times per second) |
| Final callback | Always called at operation completion via `force_callback()` |
| Cancellation | Return `False` from callback to cancel the operation |
| Thread safety | Callback is invoked from worker threads; metadata is built under lock |

**Progress Field Semantics:**

For chunked files, each chunk is counted separately in `total_file_chunks`, `downloaded_file_chunks`, etc.

| Field | When Incremented |
|-------|------------------|
| `downloaded_bytes` / `downloaded_file_chunks` | After download completes for a file or chunk |
| `skipped_bytes` / `skipped_file_chunks` | When hash cache hit or conflict resolution skips the download |

**Example - Progress callback:**

```python
from deadline.job_attachments._snapshots import (
    download_abs_manifest,
    DownloadProgressMetadata,
)

def on_progress(metadata: DownloadProgressMetadata) -> bool:
    print(f"Progress: {metadata.progress:.1f}% - {metadata.progressMessage}")
    # Return False to cancel, True to continue
    return True

result = download_abs_manifest(
    manifest=abs_manifest,
    data_cache=s3_cache,
    on_progress=on_progress,
)
```

**Hash Cache Skip Optimization:**

When a `hash_cache` is provided, the DOWNLOAD operation checks each file before downloading:

1. If the local file exists and its path+mtime is in the hash cache
2. And the cached hash matches the expected hash from the manifest
3. Then the download is skipped (file already has correct content)

This optimization is particularly useful for:
- **Repeated downloads:** Downloading the same manifest twice skips all files the second time
- **Incremental updates:** When downloading a new manifest version, only changed files are downloaded
- **Resume after interruption:** Files successfully downloaded before interruption are skipped

The hash cache is the same cache used by HASH and HASH_UPLOAD operations, so files that were previously hashed or uploaded will have their hashes available for skip detection.

**Returns:** `DownloadResult` dataclass containing:

| Field | Type | Description |
|-------|------|-------------|
| `statistics` | `DownloadSummaryStatistics` | Summary statistics about the download operation |
| `manifest` | `AbsManifest` | A copy of the input manifest with mtime values updated to match the local filesystem |

The `statistics` field contains:
- `total_files`: Number of files in manifest
- `total_bytes`: Total bytes to download
- `processed_files`: Number of files successfully downloaded
- `processed_bytes`: Bytes successfully downloaded
- `skipped_files`: Number of files skipped (hash cache match or SKIP resolution)
- `skipped_bytes`: Bytes skipped
- `total_time`: Total operation time in seconds
- `transfer_rate`: Download throughput in bytes/second

**Why Return an Updated Manifest?**

The `manifest` field in the return value contains a copy of the input manifest with `mtime` values updated to match the actual local filesystem timestamps after download. This is essential for reliable cross-platform workflows:

1. **File system mtime precision varies by OS:**
   - Linux (ext4): nanosecond precision
   - Windows (NTFS): 100-nanosecond precision
   - macOS (APFS): nanosecond precision, but HFS+ has 1-second precision

2. **Problem scenario:** A manifest created on Linux captures an mtime like `1704067200123456` (microseconds). When downloaded to Windows, the file system may store it as `1704067200123400` due to precision differences. A subsequent DIFF operation comparing the original manifest against a COLLECT of the downloaded files would incorrectly detect the file as "modified" due to the mtime mismatch.

3. **Solution:** By returning a manifest with the actual filesystem mtimes, callers can use this updated manifest as the baseline for subsequent DIFF operations, ensuring reliable change detection regardless of the OS where the snapshot was created vs. where it was downloaded.

**Example - Using the updated manifest for reliable diffs:**

```python
# Download files from a manifest created on a different OS
result = download_abs_manifest(manifest=cloud_manifest, data_cache=s3_cache)

# Use the updated manifest (with local filesystem mtimes) as the baseline
local_baseline = result.manifest

# Later, detect actual user changes reliably
current_state = collect_abs_snapshot([download_dir], [])
current_hashed = hash_abs_manifest(current_state)
changes = diff_snapshots(parent=local_baseline, current=current_hashed)
# 'changes' now correctly reflects only real user modifications,
# not false positives from mtime precision differences
```

**Example - Using hash cache to skip unchanged files:**

```python
from deadline.job_attachments.caches.hash_cache import HashCache

# Use a hash cache to skip files that already have correct content
with HashCache() as hash_cache:
    # First download - all files are downloaded
    result1 = download_abs_manifest(
        manifest=manifest,
        data_cache=s3_cache,
        hash_cache=hash_cache,
    )
    print(f"Downloaded {result1.statistics.processed_files} files")

    # Second download of same manifest - all files skipped
    result2 = download_abs_manifest(
        manifest=manifest,
        data_cache=s3_cache,
        hash_cache=hash_cache,
    )
    print(f"Skipped {result2.statistics.skipped_files} files (already up to date)")
```

**Raises:**
- `ValueError` if the manifest contains relative paths
- `AssetSyncCancelledError` if cancelled via progress tracker

**File Conflict Resolution:**

| Resolution | Behavior |
|------------|----------|
| `SKIP` | Skip download if file already exists at target path |
| `OVERWRITE` | Overwrite existing file with downloaded content |
| `CREATE_COPY` | Create a new file with suffix (e.g., `file (1).ext`) if file exists |

**Entry Type Handling:**

| Entry Type | Action |
|------------|--------|
| Regular file | Download from data cache using hash as key |
| Large file (chunkhashes) | Download each chunk, concatenate to target file |
| Symlink | Create symlink pointing to `symlink_target` (see ordering below) |
| Deleted file marker (diff) | Delete the file at the path if it exists |
| Deleted directory marker (diff) | Delete the directory only if empty (see below) |
| Directory | Create directory (with parents) if it doesn't exist |

**Symlink Ordering:**

For chained symlinks (e.g., `A -> B -> C` where A points to B and B points to C), the operation ensures targets are created before the symlinks that point to them. This is achieved through topological sorting of the symlink dependency graph:

1. Build a dependency graph where symlink A depends on symlink B if A's target is B's path
2. Perform topological sort to determine creation order
3. Create symlinks in sorted order (targets first, then dependents)

This ensures that when symlink A is created, its target B already exists (if B is also a symlink in the manifest).

**Diff Manifest Behavior:**

When downloading a diff manifest (`AbsSnapshotDiff`):
- Entries with `deleted=True` cause the target file/directory to be removed
- Non-deleted entries are downloaded normally
- This allows applying incremental updates to a local directory

**Deletion Ordering and Non-Empty Directories:**

Deletions are processed in order of path length (longest paths first), ensuring that child files and subdirectories are deleted before their parent directories. This is important because:

1. **Directories are only deleted if empty.** The DOWNLOAD operation uses `rmdir()` (not recursive delete) for directories. If a directory still contains files, it is silently left in place.

2. **Diff manifests must explicitly list all deletions.** To delete a directory and its contents, the diff manifest must include deletion markers for every file and subdirectory within it, followed by the directory itself. The DIFF operation produces manifests that satisfy this requirement.

3. **Untracked files are preserved.** If files exist in a directory that were not part of the original manifest (e.g., user-created files, logs, caches), deleting the parent directory marker will leave the directory intact because it's not empty. This prevents accidental data loss.

Example: To delete directory `/project/old_assets/` containing `model.obj` and `texture.png`:
```
# Diff manifest must include (in any order, sorted by DOWNLOAD):
/project/old_assets/model.obj    (deleted=True)
/project/old_assets/texture.png  (deleted=True)
/project/old_assets/             (deleted=True, directory)
```

**Interleaved Directory Creation:**

The DOWNLOAD operation interleaves directory creation with file download submission for optimal performance:

```python
# Directories sorted by path (parents before children)
for dir_path in sorted_dirs:
    create_directory(dir_path)        # Create this directory
    for entry in files_in_dir:        # Submit files in this directory
        pipeline.submit_file(entry)   # Downloads start immediately
```

This approach provides two performance benefits:

1. **Avoids redundant directory creation:** By sorting directories and creating them in order (parents before children), each directory is created exactly once. Without this, creating nested paths like `/a/b/c/file.txt` would redundantly create `/a`, `/a/b`, and `/a/b/c` for every file.

2. **Maximizes parallelism:** Files are submitted for download as soon as their parent directory exists, rather than waiting for all directories to be created first. This allows downloads to proceed in parallel with directory creation for deeper paths.

**Parallel Download Architecture:**

The DOWNLOAD operation uses a `ThreadPoolExecutor` with callbacks to coordinate parallel downloads of both regular files and chunked files, with S3 multi-part parallel downloads for large files:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    DOWNLOAD PIPELINE COORDINATOR                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Regular Files (with multi-part for large files ≥64MB):                 │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  1. executor.submit() → pre-allocate temp file + handle conflicts│   │
│  │  2. For large files: parallel byte-range downloads               │   │
│  │     For small files: single get_object request                   │   │
│  │  3. Atomic replace + mtime update                                │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  Chunked Files (fan-out/fan-in via atomic counter):                     │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  1. executor.submit() → pre-allocate temp file + handle conflicts│   │
│  │     └─► Fan-out: submit all chunk downloads to executor         │   │
│  │  2. Each chunk downloads in parallel                             │   │
│  │     - Large chunks (≥64MB): parallel byte-range parts            │   │
│  │     - Small chunks: single get_object request                    │   │
│  │     └─► Each chunk decrements atomic counter on completion       │   │
│  │  3. Last chunk (counter reaches 0) → atomic replace + mtime      │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  All tasks share a single ThreadPoolExecutor(max_workers=N)            │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

Both regular files and chunked files download concurrently. For S3 downloads of files or chunks
larger than `2 * multipart_part_size` (default 64MB), parallel byte-range requests are used for
improved throughput. For chunked files, all chunks download in parallel, with each chunk
written directly to its correct byte offset in a pre-allocated temporary file. Completion is
tracked via an atomic counter—when the last chunk completes (counter reaches 0), that thread
performs the atomic file replacement.

This architecture maximizes throughput by:
- Downloading multiple files simultaneously (regular and chunked)
- Downloading all chunks of chunked files in parallel
- Using parallel byte-range requests for large files/chunks (S3 multi-part download)
- Using callbacks instead of async/await for lower overhead

**S3 Multi-Part Download:**

For S3 downloads, files and chunks larger than `2 * multipart_part_size` are downloaded using
parallel byte-range requests. With the default 32MB part size, this means files ≥64MB use
multi-part download.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    S3 MULTI-PART DOWNLOAD                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  For a 100MB file with 32MB parts:                                      │
│                                                                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐                   │
│  │ Part 0   │ │ Part 1   │ │ Part 2   │ │ Part 3   │                   │
│  │ 0-32MB   │ │ 32-64MB  │ │ 64-96MB  │ │ 96-100MB │                   │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘                   │
│       │            │            │            │                          │
│       ▼            ▼            ▼            ▼                          │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │              Pre-allocated Temp File (100MB)                    │   │
│  │  [part 0 region][part 1 region][part 2 region][part 3]          │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  Each part uses S3 GetObject with Range header:                         │
│    Range: bytes={start}-{end}  (inclusive on both ends)                 │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Storage Key Format:**

Files are retrieved from content-addressable storage using keys based on the data cache type:

| Data Cache Type | Key Format | Example |
|-----------------|------------|---------|
| `S3DataCache` | `{s3_key_prefix}/{hash}.{algorithm}` | `Data/a1b2c3d4e5f67890abcdef1234567890.xxh128` |
| `FileSystemDataCache` | `{root_path}/{hash}.{algorithm}` | `/mnt/cache/a1b2c3d4e5f67890abcdef1234567890.xxh128` |

**Atomic File Downloads:**

All file downloads are atomic to ensure target files are never in a partial or corrupt state:

1. **Temporary file creation:** Files are downloaded to a temporary file beside the target (e.g., `myfile.dat.tmp048df` where `048df` is a random hex suffix)
2. **Atomic move:** After the download completes successfully, `os.replace()` atomically moves the temp file to the final location
3. **Error cleanup:** If any error occurs during download, the temporary file is deleted

This is particularly important for chunked files (>256MB), where multiple chunks are downloaded in parallel. The target file only appears once all chunks have been successfully downloaded and the last completing thread performs the atomic move.

| File Type | Behavior |
|-----------|----------|
| Regular file | Pre-allocate temp file, download (multi-part for large), atomic move |
| Chunked file | Pre-allocate temp file, download all chunks in parallel to their byte offsets, then atomic move after all chunks complete |

**Parallel Chunked File Downloads:**

Large files that exceed the chunk size (default 256MB) are stored as multiple chunks in the data cache.
When downloading these files, all chunks are downloaded in parallel for maximum throughput:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    CHUNKED FILE DOWNLOAD                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  1. Pre-allocate temp file to exact size (using truncate)               │
│                                                                         │
│  2. Submit all chunk downloads to shared thread pool:                   │
│                                                                         │
│     ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│     │ Chunk 0  │  │ Chunk 1  │  │ Chunk 2  │  │ Chunk N  │             │
│     │ offset=0 │  │ offset=  │  │ offset=  │  │ offset=  │             │
│     │          │  │ 256MB    │  │ 512MB    │  │ N*256MB  │             │
│     └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘             │
│          │             │             │             │                    │
│          ▼             ▼             ▼             ▼                    │
│     ┌─────────────────────────────────────────────────────┐            │
│     │              Temp File (pre-allocated)              │            │
│     │  [chunk 0 region][chunk 1 region][...][chunk N]     │            │
│     └─────────────────────────────────────────────────────┘            │
│                                                                         │
│  3. Wait for all chunks to complete                                     │
│                                                                         │
│  4. Atomic move: os.replace(temp_file, target_file)                     │
│                                                                         │
│  5. Restore mtime from manifest                                         │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

Key implementation details:

- **Pre-allocation:** The temp file is created with `truncate(size)` to establish the exact file size before any writes. This creates a sparse file on filesystems that support it.
- **Parallel writes:** Each chunk download writes to a non-overlapping byte range, so no locking is required. Each thread opens its own file handle and seeks to the correct offset.
- **Offset calculation:** `offset = chunk_index * chunk_size_bytes`. The last chunk may be smaller than `chunk_size_bytes`.
- **Shared thread pool:** Chunk downloads share the same `ThreadPoolExecutor` used for regular file downloads, controlled by `max_workers`.
- **Error handling:** If any chunk fails, all pending chunk downloads for that file are allowed to complete or fail, the temp file is deleted, and the error is propagated.
- **Atomicity:** The target file only appears after ALL chunks have been successfully written and `os.replace()` completes.

**Modification Time Restoration:**

Downloaded files have their modification time (`mtime`) set to the value stored in the manifest, preserving the original file timestamps.

**Example - Downloading from S3:**

```python
import boto3
from deadline.job_attachments._snapshots import (
    download_abs_manifest,
    join_manifest,
    S3DataCache,
)
from deadline.job_attachments.asset_manifests.decode import decode_manifest
from deadline.job_attachments.models import FileConflictResolution

# Load a manifest with relative paths
with open("scene.manifest") as f:
    rel_manifest = decode_manifest(f.read())

# Join with absolute path to create AbsSnapshot
abs_manifest = join_manifest(rel_manifest, "/home/user/projects/scene")

# Create S3 data cache
data_cache = S3DataCache(
    s3_bucket="my-job-attachments-bucket",
    s3_key_prefix="Data",
    s3_client=boto3.client("s3"),
)

# Download all files to local filesystem
stats = download_abs_manifest(
    manifest=abs_manifest,
    data_cache=data_cache,
    file_conflict_resolution=FileConflictResolution.OVERWRITE,
)

print(f"Downloaded {stats.processed_files} files ({stats.processed_bytes} bytes)")
print(f"Skipped {stats.skipped_files} files")
print(f"Total time: {stats.total_time:.2f}s")
```

Output:
```
Downloaded 42 files (1234567890 bytes)
Skipped 0 files
Total time: 12.34s
```

**Example - Downloading from local cache (debug snapshot):**

```python
from pathlib import Path
from deadline.job_attachments._snapshots import (
    download_abs_manifest,
    join_manifest,
    FileSystemDataCache,
)
from deadline.job_attachments.asset_manifests.decode import decode_manifest

# Load manifest from debug snapshot
with open("/tmp/debug_snapshot/manifest.json") as f:
    rel_manifest = decode_manifest(f.read())

# Join with target directory
abs_manifest = join_manifest(rel_manifest, "/home/user/restored_scene")

# Create filesystem data cache pointing to debug snapshot data
data_cache = FileSystemDataCache(
    root_path=Path("/tmp/debug_snapshot/data"),
)

# Download (copy) files from cache to target directory
stats = download_abs_manifest(
    manifest=abs_manifest,
    data_cache=data_cache,
)

print(f"Restored {stats.processed_files} files to /home/user/restored_scene")
```

**Example - Applying a diff manifest:**

```python
from deadline.job_attachments._snapshots import (
    download_abs_manifest,
    join_manifest,
    S3DataCache,
)
from deadline.job_attachments.asset_manifests.decode import decode_manifest

# Load a diff manifest
with open("changes.diff.manifest") as f:
    rel_diff = decode_manifest(f.read())

# Join with target directory
abs_diff = join_manifest(rel_diff, "/home/user/projects/scene")

# Apply the diff - downloads new/modified files, deletes removed files
data_cache = S3DataCache(
    s3_bucket="my-job-attachments-bucket",
    s3_key_prefix="Data",
    s3_client=boto3.client("s3"),
)

stats = download_abs_manifest(
    manifest=abs_diff,
    data_cache=data_cache,
)

print(f"Applied diff: {stats.processed_files} files updated")
```

**When to Use DOWNLOAD:**

| Use Case | Recommended Approach |
|----------|---------------------|
| Worker input sync | DOWNLOAD with S3DataCache |
| Job output download | DOWNLOAD with S3DataCache |
| Restore from debug snapshot | DOWNLOAD with FileSystemDataCache |
| Apply incremental update | DOWNLOAD with AbsSnapshotDiff |
| Step-step dependency sync | DOWNLOAD with composed output manifests |

**Relationship to HASH_UPLOAD:**

DOWNLOAD is the inverse of HASH_UPLOAD:

| Operation | Direction | Input | Output |
|-----------|-----------|-------|--------|
| HASH_UPLOAD | Local → Cache | AbsManifest (no hashes) | UploadResult (manifest with hashes + statistics) |
| DOWNLOAD | Cache → Local | AbsManifest (with hashes) | DownloadResult (files on filesystem + statistics) |

```python
# Round-trip example:
# 1. Collect and upload
abs_manifest = collect_abs_snapshot(["/projects/scene"], [])
upload_result = hash_upload_abs_manifest(abs_manifest, s3_cache)

# 2. Save manifest
with open("scene.manifest", "w") as f:
    f.write(encode_manifest(upload_result.manifest))

# 3. Later, download to different location
loaded = decode_manifest(open("scene.manifest").read())
abs_for_download = join_manifest(
    subtree_manifest(loaded, "/projects/scene"),
    "/home/other_user/scene"
)
download_abs_manifest(abs_for_download, s3_cache)
```

### 5. FILTER: `filter_manifest()`

**Location:** `_filter_manifest.py`

Applies a filter to manifest entries, returning a new manifest with only matching entries:

```python
def filter_manifest(
    manifest: Manifest,
    entry_filter: Callable[[Union[ManifestFilePath, ManifestDirectoryPath]], bool],
) -> Manifest:
```

**Filter interface:**

The filter is a callable that takes a manifest entry and returns `True` to keep it:

```python
# Example: Custom filter for large files only
def large_files_only(entry):
    if isinstance(entry, ManifestFilePath) and entry.size is not None:
        return entry.size > 1_000_000  # > 1MB
    return False

filtered = filter_manifest(manifest, large_files_only)
```

**Built-in filter: `IncludeExcludePathsFilter`**

Implements classic include/exclude glob pattern matching:

```python
filter = IncludeExcludePathsFilter(
    include=["*.blend", "textures/*"],
    exclude=["backup/*", "*.tmp"]
)
filtered = filter_manifest(manifest, filter)
```

Pattern matching rules:
- If include patterns specified, path must match at least one
- Path must not match any exclude pattern
- Uses `fnmatch` for glob-style matching

**Critical for diff computation:**

When computing a diff of filtered manifests, BOTH parent and current manifests must be filtered with the SAME filter before comparison.
This ensures deletions are computed correctly within the filtered view.

**Example:**

```python
from deadline.job_attachments._snapshots import (
    filter_manifest,
    IncludeExcludePathsFilter,
)

# Filter to only include Blender files and textures, excluding backups
filter = IncludeExcludePathsFilter(
    include=["*.blend", "textures/**/*"],
    exclude=["backup/*", "*_old.*"],
)

filtered = filter_manifest(manifest, filter)
print(f"Filtered from {len(manifest.files)} to {len(filtered.files)} entries")

# Or use a custom filter function
def python_files_only(entry):
    return entry.path.endswith(".py")

py_manifest = filter_manifest(manifest, python_files_only)
```

### 6. DIFF: `diff_snapshots()`

**Location:** `_diff_snapshots.py`

Computes the difference between two snapshot manifests:

```python
def diff_snapshots(
    parent: SnapshotManifest,
    current: SnapshotManifest,
    parent_manifest_hash: Optional[str] = None,
    ignore_hashes: bool = False,
    *,
    preserve_runnable: bool = False,
) -> DiffManifest:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `parent` | The parent snapshot manifest (filtered, with hashes) |
| `current` | The current snapshot manifest (filtered, with hashes) |
| `parent_manifest_hash` | Optional hash of the parent manifest (for v2025 diff manifests) |
| `ignore_hashes` | If `True`, compare by metadata only (size, mtime, runnable) without hashes |
| `preserve_runnable` | If `True`, copy `runnable` from parent for modified files (see below) |

**Preconditions:**

1. Both manifests must be the same version
2. Both manifests should be filtered with the same patterns
3. Both manifests should have hashes computed (unless `ignore_hashes=True`)

**Hash State Validation:**

When `ignore_hashes=False`, the function validates that both manifests have compatible hash states:

| Parent State | Current State | Result |
|--------------|---------------|--------|
| Hashed | Hashed | ✓ Comparison proceeds |
| Unhashed | Unhashed | ✓ Comparison proceeds |
| Empty/symlinks-only | Any | ✓ Comparison proceeds |
| Any | Empty/symlinks-only | ✓ Comparison proceeds |
| Hashed | Unhashed | ✗ Raises `ManifestHashMismatchError` |
| Unhashed | Hashed | ✗ Raises `ManifestHashMismatchError` |

Empty manifests and manifests containing only symlinks are considered compatible with either hashed or unhashed manifests, since they have no regular files whose hash state could conflict.

**Comparison modes:**

| Mode | `ignore_hashes` | Comparison Fields |
|------|-----------------|-------------------|
| Full | `False` | hash, chunkhashes, size, mtime, runnable |
| Fast | `True` | size, mtime, runnable only |

**Output by version:**

| Feature | v2023-03-03 | v2025-12-04-beta |
|---------|-------------|------------------|
| New entries | ✓ | ✓ |
| Modified entries | ✓ | ✓ |
| Deleted entries | Not tracked | ✓ (deleted=True markers) |
| Manifest class | AbsSnapshot/Snapshot | AbsSnapshotDiff/SnapshotDiff |
| parentManifestHash | N/A | ✓ (if provided) |

**Returns:** A `DiffManifest` (either `AbsSnapshotDiff` or `SnapshotDiff` depending on input path style) with:
- `parentManifestHash` if provided
- New/modified entries with full content
- Deleted entries with `deleted=True` markers

**Entry comparison logic (`_entries_differ()`):**

- Type transitions (file ↔ symlink) are always different
- Symlinks compared by `symlink_target` only
- Regular files compared by hash/chunkhashes (unless `ignore_hashes`), size, mtime, runnable

**The `preserve_runnable` Parameter:**

The `runnable` field captures the POSIX execute bit (`chmod +x`). On Windows, this filesystem concept doesn't exist—all files report `runnable=False` when collected. This creates a problem for cross-platform workflows:

1. A manifest is created on POSIX with `script.sh` having `runnable=True`
2. The manifest is used on Windows, files are extracted
3. User modifies `script.sh` content on Windows
4. A new manifest is collected on Windows—`script.sh` now has `runnable=False`
5. The diff shows `script.sh` as modified, but with `runnable=False`
6. When applied back to POSIX, the execute bit is incorrectly removed

Setting `preserve_runnable=True` solves this by:
- Ignoring `runnable` differences when determining if entries differ (so a file that only changed `runnable` won't appear in the diff)
- Copying the `runnable` value from the parent manifest for modified files that have other changes

New files always use the current manifest's `runnable` value (which will be `False` on Windows, but that's correct for newly created files).

**When to use `preserve_runnable=True`:**
- On Windows when computing diffs against a parent manifest that may have come from POSIX
- In any cross-platform workflow where you want to preserve execute bits through modifications

**Example:**

```python
from deadline.job_attachments._snapshots import diff_snapshots
from deadline.job_attachments.asset_manifests.decode import decode_manifest
from deadline.job_attachments.asset_manifests.hash_algorithms import hash_data, HashAlgorithm

# Load the parent manifest
with open("previous.manifest") as f:
    parent_str = f.read()
    parent = decode_manifest(parent_str)
    parent_hash = hash_data(parent_str.encode("utf-8"), HashAlgorithm.XXH128)

# Assume current_hashed is a collected and hashed manifest of the current directory
diff = diff_snapshots(
    parent=parent,
    current=current_hashed,
    parent_manifest_hash=parent_hash,
    ignore_hashes=False,  # Compare by hash for accuracy
    preserve_runnable=True,  # Preserve execute bits from parent for modified files
)

# Inspect the diff
new_files = [p for p in diff.files if p.path not in {e.path for e in parent.files}]
deleted = [p for p in diff.files if p.deleted]
print(f"New: {len(new_files)}, Deleted: {len(deleted)}")
print(f"Diff manifest type: {type(diff).__name__}")  # AbsSnapshotDiff or SnapshotDiff
print(f"Parent hash: {diff.parentManifestHash[:16]}...")
```

Output:
```
New: 3, Deleted: 1
Diff manifest type: SnapshotDiff
Parent hash: f8e9d0c1b2a34567...
```

### 7. COMPOSE: `compose_manifests()`

**Location:** `_compose_manifest.py`

Layers multiple manifests together into a single manifest, as if applying each manifest as a set of changes in order:

```python
def compose_manifests(
    manifests: List[Manifest],
) -> Manifest:
```

**Behavior by version:**

| Version | Input | Output | Description |
|---------|-------|--------|-------------|
| v2023-03-03 | (snapshot, snapshot, ...) | snapshot | Layer snapshots; later entries override earlier |
| v2025-12-04-beta | (snapshot, diff, diff, ...) | snapshot | Apply diffs to base snapshot |
| v2025-12-04-beta | (diff, diff, ...) | diff | Combine diffs into single equivalent diff |

**Composition semantics:**

The result represents the directory tree you would get by:
1. Starting with the first manifest's directory tree
2. Applying each subsequent manifest as a "patch"—adding new entries, updating modified entries, and removing deleted entries

For v2023-03-03 (no deletion markers):
- Later manifests override earlier ones for the same path
- Files only in earlier manifests are preserved
- No way to express deletions

For v2025-12-04-beta (with deletion markers):
- First manifest determines the composition type:
  - If snapshot: subsequent must be diffs, result is a snapshot
  - If diff: all must be diffs, result is a combined diff
- `deleted=True` markers remove entries from the result
- For diff composition, `parentManifestHash` comes from the first diff

**Key implementation details:**

- All manifests must be the same version
- All manifests must have the same `fileChunkSizeBytes` value
- Entries are keyed by path; later entries replace earlier ones
- Deleted markers remove the entry entirely from the result (v2025)
- Total size is recomputed from the final entry set
- For v2025 snapshot+diffs: first must be snapshot, rest must be diffs
- For v2025 diff composition: all must be diffs, `parentManifestHash` from first diff

**Trie-Based Implementation:**

The COMPOSE operation uses a trie (prefix tree) structure where each node represents a path component. This provides efficient handling of directory operations and cascading effects.

*Trie node structure:*

| Field | Description |
|-------|-------------|
| `children` | Child nodes keyed by path component |
| `file_entry` | File/symlink entry at this node (None for directories) |
| `deleted` | Deletion marker flag (for diff composition only) |

*Why a trie?*

1. **Efficient path operations:** Insert, lookup, and delete are O(path depth) rather than O(n) for flat lists
2. **Natural directory structure:** The trie mirrors the filesystem hierarchy, making directory operations intuitive
3. **Cascading deletions:** When a directory is deleted, its subtree can be efficiently removed or marked

*Snapshot + Diffs composition:*

When composing a snapshot with diffs, the trie accumulates the final state:
- Base snapshot entries are inserted into the trie
- For each diff: deletions remove nodes from the trie, additions/modifications insert or update nodes
- The final trie contains only the entries that exist after all diffs are applied
- Deletion markers are NOT preserved in the output (it's a snapshot, not a diff)

*Diff + Diffs composition:*

When composing multiple diffs (without a base snapshot), the trie tracks cumulative changes:
- Each node has a `deleted` flag to track deletion markers
- Deletions set `deleted=True`; additions clear it and set `file_entry`
- After all diffs are applied, `reconcile_deleted_flags()` handles the case where a deleted directory has non-deleted children (the directory must exist for its children)
- The output includes both current entries AND deletion markers

*The reconciliation step:*

The `reconcile_deleted_flags()` method handles this scenario:
1. diff1 deletes `/dir/` and all its contents
2. diff2 adds `/dir/newfile.txt`

After diff2, `/dir/` must NOT be marked as deleted because it has a non-deleted child. The reconciliation traverses the trie depth-first and clears the `deleted` flag on any node that has non-deleted descendants.

**Validation Rules:**

| Condition | Behavior |
|-----------|----------|
| Empty manifest list | Raises `ValueError` |
| Manifests have different `fileChunkSizeBytes` | Raises `ValueError` |
| Snapshot+diffs: non-snapshot first | Raises `ValueError` |
| Snapshot+diffs: non-diff after first | Raises `ValueError` |
| Diff composition: non-diff in list | Raises `ValueError` |

**Example:**

```python
from deadline.job_attachments._snapshots import compose_manifests
from deadline.job_attachments.asset_manifests.decode import decode_manifest

# Load a base snapshot and incremental diffs
with open("base.manifest") as f:
    base = decode_manifest(f.read())
with open("day1.manifest") as f:
    diff1 = decode_manifest(f.read())
with open("day2.manifest") as f:
    diff2 = decode_manifest(f.read())

# Compose into a single snapshot representing the final state
final = compose_manifests([base, diff1, diff2])

print(f"Final manifest has {len(final.files)} entries")
print(f"Manifest type: {type(final).__name__}")  # AbsSnapshot or Snapshot
```

For v2023 format (layering snapshots):

```python
# Multiple output manifests from different render tasks
task1_output = decode_manifest(read_file("task1_output.manifest"))
task2_output = decode_manifest(read_file("task2_output.manifest"))
task3_output = decode_manifest(read_file("task3_output.manifest"))

# Merge into single manifest (later tasks override earlier for same paths)
merged = compose_manifests([task1_output, task2_output, task3_output])
```

### 8. SUBTREE: `subtree_manifest()`

**Location:** `_subtree_manifest.py`

Extracts a subtree from a manifest, producing a new manifest rooted at the specified subdirectory:

```python
def subtree_manifest(
    manifest: Manifest,
    subtree: str,
    *,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
) -> RelManifest:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | The source manifest to extract from |
| `subtree` | Path to the subtree root, or `"."` or `""` for identity transformation (see below) |
| `symlink_policy` | How to handle symlinks that escape the new subtree root (see below) |

**Identity Subtree (`subtree="."` or `subtree=""`):**

When `subtree="."` or `subtree=""`, the operation acts as an identity transformation that applies the `symlink_policy` without rebasing paths. This is useful for:
- Collapsing all symlinks in a manifest before serialization to v2023 format
- Excluding all symlinks from a manifest
- Processing symlinks without changing the directory structure

The identity subtree requires a manifest with relative paths (since the output is always `RelManifest`).

**Conceptual Model:**

SUBTREE is a virtual re-rooting operation. Given a manifest rooted at `/projects/scene` and a subtree path of `assets/textures`, the result is a new manifest that represents only the `assets/textures` directory as if it were the root:

```
Original manifest root: /projects/scene
├── assets/
│   ├── textures/
│   │   ├── wood.png
│   │   └── metal.png
│   └── models/
│       └── chair.blend
└── scripts/
    └── render.py

SUBTREE(manifest, "assets/textures") produces:

New manifest root: /projects/scene/assets/textures
├── wood.png
└── metal.png
```

The operation:
1. Filters to entries within the subtree
2. Rebases paths relative to the new root (strips the subtree prefix)
3. Handles symlinks according to `symlink_policy`

**Path Style Requirements:**

The `subtree` path must match the path style used in the manifest:

| Manifest Paths | Subtree Path | Valid |
|----------------|--------------|-------|
| Relative (`assets/file.txt`) | Relative (`assets`) | ✓ |
| Absolute (`/projects/scene/assets/file.txt`) | Absolute (`/projects/scene/assets`) | ✓ |
| Relative | Absolute | ✗ Error |
| Absolute | Relative | ✗ Error |

**Output:**

The output manifest always uses relative paths, regardless of whether the input used absolute paths. This makes the result suitable for storage or transport.

The `fileChunkSizeBytes` field IS preserved in the output manifest, ensuring chunk size settings are maintained through subtree operations.

**Note:** The `parentManifestHash` field is NOT preserved in the output manifest. Since the subtree operation changes the root path, the original parent manifest hash would be invalid for the new subtree manifest.

**Symlink Handling:**

When re-rooting a manifest, symlinks that were previously "within root" may now "escape" the new subtree root. The `symlink_policy` parameter controls how these are handled:

| Policy | Behavior for Escaping Symlinks |
|--------|-------------------------------|
| `COLLAPSE_ALL` | Collapse every symlink in the result (regardless of whether it escapes). |
| `COLLAPSE_ESCAPING` | Collapse only symlinks escaping the new subtree; preserve symlinks within subtree |
| `EXCLUDE_ALL` | Exclude every symlink from the result (regardless of whether it escapes). |
| `EXCLUDE_ESCAPING` | Exclude only symlinks escaping the new subtree; preserve symlinks within subtree |

**Note:** `PRESERVE` and `TRANSITIVE_INCLUDE_TARGETS` are not supported for SUBTREE. Since the output always uses relative paths, escaping symlinks cannot be represented—a relative symlink target like `../outside/file.txt` would point outside the manifest root, which is invalid. Therefore, escaping symlinks must either be collapsed or excluded.

**Symlink Policy Behavior in SUBTREE:**

| Policy | Symlinks within subtree | Symlinks escaping subtree |
|--------|------------------------|---------------------------|
| `COLLAPSE_ALL` | Collapsed to file/directory | Collapsed to file/directory |
| `COLLAPSE_ESCAPING` | Preserved (target rebased) | Collapsed to file/directory |
| `EXCLUDE_ALL` | Excluded | Excluded |
| `EXCLUDE_ESCAPING` | Preserved (target rebased) | Excluded |

**Note:** Unlike COLLECT, SUBTREE operates purely on manifest data—it never accesses the filesystem. When a symlink is "collapsed," the operation looks up the target path in the original manifest and copies that entry's data (hash, size, mtime, etc.) to replace the symlink entry.

**Important: Symlink Target Storage Format:**

In our manifest format, symlink targets are stored **relative to the manifest root**, not relative to the symlink location (unlike POSIX filesystem symlinks). For example, a symlink at `assets/textures/current` pointing to `assets/shared/latest.png` stores the target as `assets/shared/latest.png`, not as `../shared/latest.png`.

This design choice simplifies manifest operations since targets can be looked up directly in the manifest's path index without needing to resolve relative paths from the symlink's location.

**Symlink Collapse Behavior:**

When collapsing a symlink, the operation looks up the target path in the original manifest:

- **File target:** The symlink entry is replaced with a copy of the target file entry (using the symlink's path, but the target's hash, size, mtime, runnable)
- **Directory target:** The symlink entry is replaced with all entries under that directory in the original manifest, recursively. Paths are rebased so the symlink path becomes the new prefix (e.g., symlink `current` with target `assets/shared/v2` containing `assets/shared/v2/a.txt` and `assets/shared/v2/sub/b.txt` produces `current/a.txt` and `current/sub/b.txt`)
- **Missing target:** If the target doesn't exist in the manifest (e.g., it was an escaping symlink that was already collapsed during COLLECT), the symlink is excluded with a warning

**Symlink Cycle Handling:**

Symlink cycles occur when following symlinks leads back to a previously visited target. Examples:
- Self-referential: `A -> A` (length 1)
- Direct cycle: `A -> B -> A` (length 2)
- Longer cycles: `A -> B -> C -> A` (length 3+)

When collapsing symlinks, SUBTREE detects cycles and handles them gracefully:

| Policy | Cycle Behavior |
|--------|----------------|
| `COLLAPSE_ALL` | Cycles detected during collapse; cyclic symlink skipped with warning |
| `COLLAPSE_ESCAPING` | Cycles detected when collapsing escaping symlinks; cyclic symlink skipped with warning |
| `EXCLUDE_ALL` | No collapse needed; all symlinks excluded |
| `EXCLUDE_ESCAPING` | Cycles detected when collapsing escaping symlinks; cyclic symlink skipped with warning |

When a cycle is detected:
1. A warning is logged identifying the cyclic symlink
2. The cyclic symlink is skipped (produces no output entries)
3. Processing continues with remaining entries

**Preserved Symlink Target Rebasing:**

Symlinks that are preserved (not collapsed) must have their `symlink_target` rebased relative to the new subtree root. Since targets are stored relative to the manifest root, rebasing simply strips the subtree prefix from the target path. For example:

```
Original manifest (rooted at /projects/scene):
  assets/textures/wood.png
  assets/textures/current -> assets/shared/v2/latest.png  (target outside subtree - escapes)
  assets/textures/alt -> assets/textures/variants/dark.png  (target within subtree)
  assets/shared/v2/latest.png

SUBTREE(manifest, "assets/textures") with COLLAPSE_ESCAPING:

Result (rooted at /projects/scene/assets/textures):
  wood.png
  current                    (collapsed: now a file with latest.png's content)
  alt -> variants/dark.png   (preserved: target rebased from assets/textures/variants/dark.png)
```

The preserved symlink `alt` originally had target `assets/textures/variants/dark.png`. After rebasing (stripping the `assets/textures/` prefix), it becomes `variants/dark.png`.

**Example - Basic Subtree Extraction:**

```python
from deadline.job_attachments._snapshots import subtree_manifest
from deadline.job_attachments.asset_manifests.decode import decode_manifest

# Load a manifest rooted at /projects/scene
with open("scene.manifest") as f:
    full_manifest = decode_manifest(f.read())

# Extract just the textures directory
textures = subtree_manifest(
    manifest=full_manifest,
    subtree="assets/textures",
)

# Result is a manifest with paths relative to assets/textures/
for entry in textures.files:
    print(entry.path)  # "wood.png", "metal.png", etc.
```

**Example - Handling Symlinks:**

```python
# Original manifest structure (symlink targets are relative to manifest root):
# /projects/scene/
# ├── assets/
# │   ├── textures/
# │   │   ├── wood.png
# │   │   └── current -> assets/shared/latest.png  (target outside subtree - escapes!)
# │   └── shared/
# │       └── latest.png
# └── ...

# With COLLAPSE_ESCAPING (default): symlink is replaced with file content
textures = subtree_manifest(
    manifest=full_manifest,
    subtree="assets/textures",
    symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
)
# Result: "current" becomes a regular file with latest.png's hash/size/mtime

# With EXCLUDE_ALL: symlink is removed
textures = subtree_manifest(
    manifest=full_manifest,
    subtree="assets/textures",
    symlink_policy=SymlinkPolicy.EXCLUDE_ALL,
)
# Result: only "wood.png" is included
```

**Use Cases:**

1. **Absolute to relative conversion:** Collect manifest directory trees with `absolute_paths=True` for intermediate processing, then use SUBTREE to convert to relative paths for saving as manifest files
2. **Partial deployment:** Extract only the assets needed for a specific render task
3. **Manifest splitting:** Break a large manifest into smaller, focused manifests
4. **Re-rooting for transport:** Create a manifest for a subdirectory to upload independently
5. **Testing:** Extract a subset of a manifest for focused testing

**Relationship to FILTER:**

SUBTREE and FILTER are complementary but distinct:

| Operation | Purpose | Path Transformation |
|-----------|---------|---------------------|
| FILTER | Keep entries matching a predicate | Paths unchanged |
| SUBTREE | Extract entries under a path prefix | Paths rebased to new root |

You might use both together:

```python
# Extract textures subtree, then filter to only PNG files
textures = subtree_manifest(full_manifest, "assets/textures")
png_only = filter_manifest(textures, lambda e: e.path.endswith(".png"))
```

### 9. PARTITION: `partition_manifest()`

**Location:** `_partition_manifest.py`

Partitions a manifest into multiple (root, RelSnapshot) pairs, dividing entries by their root paths. Each RelSnapshot is an extracted subtree per the SUBTREE operation, with paths relative to its root:

```python
def partition_manifest(
    manifest: Manifest,
    roots: Optional[List[str]] = None,
    *,
    referenced_paths: Optional[List[str]] = None,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
) -> List[Tuple[str, RelManifest]]:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | Source manifest (absolute or relative paths) |
| `roots` | Optional list of root paths to partition by. No root may be a subpath of another. |
| `referenced_paths` | Optional list of paths referenced by the workload. These paths must be within one of the resulting roots, affecting auto-root determination even if no files exist under them. |
| `symlink_policy` | How to handle symlinks that escape their partition root. Only COLLAPSE_ALL, COLLAPSE_ESCAPING, and EXCLUDE_ALL are supported. |

**Returns:** A list of `(root, RelManifest)` tuples where:
- Each `root` is an absolute or relative path string
- Each `RelManifest` is a manifest with paths relative to that root

**Validation Rules:**

| Condition | Behavior |
|-----------|----------|
| A root is a subpath of another root | Raises `ValueError` |
| Root path style doesn't match manifest path style | Raises `ValueError` |
| `symlink_policy=PRESERVE` | Raises `ValueError` |
| `symlink_policy=TRANSITIVE_INCLUDE_TARGETS` | Raises `ValueError` |

**Path Separator Behavior:**

| Platform | Input (`roots`, `referenced_paths`) | Output (`root` in returned tuples) |
|----------|-------------------------------------|-----------------------------------|
| Windows | Either `\` or `/` separators accepted | Always uses Windows `\` separators |
| POSIX | `/` separators | `/` separators |

On Windows, the function normalizes returned root paths to use native backslash separators, regardless of whether the input used forward or backslashes. This ensures consistency when the returned roots are used with Windows filesystem APIs.

**Output Ordering:**

1. First: Entries for each explicitly provided root (in the same order as `roots` parameter)
2. Then: Auto-determined roots for remaining entries (sorted alphabetically)

If no entries exist under an explicitly provided root, its RelSnapshot is empty (but still included in output).

**Auto-Root Determination:**

| Scenario | Platform | Behavior |
|----------|----------|----------|
| `roots` is None or empty | POSIX | Single root: longest common path prefix of all entries and referenced_paths |
| `roots` is None or empty | Windows | One root per drive letter or UNC root path that contains entries or referenced_paths |
| `roots` provided | Any | Provided roots first, then smallest set of additional roots to cover remaining entries and referenced_paths |

When `roots` is provided, remaining entries (not under any provided root) are grouped into additional auto-determined roots. These additional roots form the smallest set that:
- Covers all remaining entries
- Covers all referenced_paths not under a provided root
- Does not include any provided root as a subpath

The `referenced_paths` parameter influences root determination by treating each referenced path as if it were an entry in the manifest for the purpose of computing roots. This ensures workload-referenced directories are accessible under one of the resulting roots, even if no files currently exist there.

This typically results in more roots than the empty-roots case, since the provided roots may not align with the natural grouping of entries.

**Empty Directory Handling:**

Empty directories (entries in `manifest.dirs`) are included in root determination alongside file parent directories. This ensures that manifests containing empty directories are partitioned correctly:

```
# Files only under /a/b, but empty dir at /a/c
# Auto-root will be /a (not /a/b) to include both
/a/b/file.txt
/a/c/           (empty directory)
```

Without this, a manifest with files under `/a/b` and an empty directory at `/a/c` would incorrectly compute `/a/b` as the root, excluding the empty directory from the partition.

**Symlink Handling:**

Symlinks are handled per-partition using the same logic as SUBTREE:
- Symlinks pointing within their partition root are preserved (rebased)
- Symlinks escaping their partition root are handled per `symlink_policy`

**Example - Auto-partition on POSIX (no roots provided):**

```python
from deadline.job_attachments._snapshots import partition_manifest

# Manifest with absolute paths under a common root
# /projects/scene/assets/model.blend
# /projects/scene/assets/texture.png
# /projects/scene/render/output.exr

partitions = partition_manifest(manifest)
# Result: [("/projects/scene", RelSnapshot)]
# RelSnapshot contains:
#   assets/model.blend
#   assets/texture.png
#   render/output.exr
```

**Example - Auto-partition on Windows (no roots provided):**

```python
# Manifest with paths on multiple drives
# C:/projects/scene/model.blend
# C:/projects/scene/texture.png
# D:/shared/library/material.mtl

partitions = partition_manifest(manifest)
# Result: [
#   ("C:/projects/scene", RelSnapshot with model.blend, texture.png),
#   ("D:/shared/library", RelSnapshot with material.mtl),
# ]
```

**Example - Explicit roots with remainder:**

```python
# Manifest entries:
# /projects/scene/model.blend
# /projects/scene/texture.png
# /data/shared/library/material.mtl
# /home/user/cache/temp.bin

partitions = partition_manifest(
    manifest,
    roots=["/projects/scene"],
)
# Result: [
#   ("/projects/scene", RelSnapshot),      # explicit root
#   ("/data/shared/library", RelSnapshot), # auto-determined for remaining
#   ("/home/user/cache", RelSnapshot),     # auto-determined for remaining
# ]

# Compare to no explicit roots on POSIX:
partitions = partition_manifest(manifest)
# Result: [("/", RelSnapshot)]  # single root covering everything
```

**Example - Empty partition for explicit root:**

```python
# Request a root that has no entries
partitions = partition_manifest(
    manifest,
    roots=["/projects/scene", "/empty/path"],
)
# Result: [
#   ("/projects/scene", RelSnapshot with entries),
#   ("/empty/path", empty RelSnapshot),  # Still included, but empty
# ]
```

**Relationship to SUBTREE and JOIN:**

PARTITION is conceptually the inverse of multiple JOIN operations followed by COMPOSE:

```python
# PARTITION splits:
[(root1, rel1), (root2, rel2)] = partition_manifest(abs_manifest)

# JOIN combines (inverse):
abs1 = join_manifest(rel1, root1)
abs2 = join_manifest(rel2, root2)
composed = compose_manifests([abs1, abs2])
# composed ≈ abs_manifest
```

### 10. JOIN: `join_manifest()`

**Location:** `_join_manifest.py`

Joins a prefix to all paths in a manifest, producing a new manifest with prefixed paths:

```python
def join_manifest(
    manifest: RelManifest,
    prefix: str,
) -> AnyManifest:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | The source manifest with relative paths (`Snapshot` or `SnapshotDiff`) |
| `prefix` | Path prefix to join to all paths (relative or absolute) |

**Conceptual Model:**

JOIN is the inverse of SUBTREE. While SUBTREE strips a prefix from paths (re-rooting to a subdirectory), JOIN adds a prefix to paths (re-rooting to a parent directory).

```
Original manifest (relative paths):
  wood.png
  metal.png
  current -> wood.png

JOIN(manifest, "/projects/scene/assets/textures") produces:

New manifest (absolute paths):
  /projects/scene/assets/textures/wood.png
  /projects/scene/assets/textures/metal.png
  /projects/scene/assets/textures/current -> /projects/scene/assets/textures/wood.png
```

**Path Style Behavior:**

| Prefix Type | Input Paths | Output Paths |
|-------------|-------------|--------------|
| Relative (`assets/textures`) | Relative | Relative (prefixed) |
| Absolute (`/projects/scene`) | Relative | Absolute |

**What Gets Prefixed:**

- File paths (`entry.path`)
- Directory paths (`dir.path`)
- Symlink targets (`entry.symlink_target`)

**What Gets Preserved:**

- `fileChunkSizeBytes` - Chunk size settings are preserved
- `totalSize` - Total size is preserved
- All file metadata (hash, size, mtime, runnable, chunkhashes)

**What Does NOT Get Preserved:**

- `parentManifestHash` - Since joining a prefix changes the root path structure, the original parent manifest hash is no longer valid for the new paths. The output manifest has `parentManifestHash=None`.

**Example - Converting Relative to Absolute:**

```python
from deadline.job_attachments._snapshots import join_manifest
from deadline.job_attachments.asset_manifests.decode import decode_manifest

# Load a manifest with relative paths
with open("textures.manifest") as f:
    manifest = decode_manifest(f.read())

# Join with absolute prefix to get absolute paths
absolute_manifest = join_manifest(manifest, "/projects/scene/assets/textures")

for entry in absolute_manifest.files:
    print(entry.path)  # "/projects/scene/assets/textures/wood.png", etc.
```

**Example - Combining Multiple Manifests:**

```python
# Load manifests from different roots
textures = decode_manifest(read_file("textures.manifest"))
models = decode_manifest(read_file("models.manifest"))
scripts = decode_manifest(read_file("scripts.manifest"))

# Join each to its absolute root
textures_abs = join_manifest(textures, "/projects/scene/assets/textures")
models_abs = join_manifest(models, "/projects/scene/assets/models")
scripts_abs = join_manifest(scripts, "/projects/scene/scripts")

# Compose into a single manifest representing all data
combined = compose_manifests([textures_abs, models_abs, scripts_abs])

# Now 'combined' has all files with absolute paths for unified processing
```

**Use Cases:**

1. **Unified download:** Join manifests to absolute paths, compose them, then download all files from S3 in one operation
2. **Path normalization:** Convert relative manifests to absolute for consistent processing
3. **Manifest merging:** Prepare manifests from different roots for composition
4. **Inverse of SUBTREE:** Restore original paths after subtree extraction

**Relationship to SUBTREE:**

JOIN and SUBTREE are inverse operations:

| Operation | Input | Output | Path Transformation |
|-----------|-------|--------|---------------------|
| SUBTREE | Manifest + subtree path | Manifest | Strips prefix from paths |
| JOIN | Manifest + prefix | Manifest | Adds prefix to paths |

```python
# These operations are inverses (for paths within the subtree)
original = join_manifest(subtree_manifest, "assets/textures")
back_to_subtree = subtree_manifest(original, "assets/textures")
# back_to_subtree has the same paths as subtree_manifest
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
