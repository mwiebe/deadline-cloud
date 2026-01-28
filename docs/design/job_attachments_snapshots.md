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

## Quick Start

The most common workflow is collecting files, hashing, and uploading to S3:

```python
import boto3
from deadline.job_attachments._snapshots import (
    collect_abs_snapshot,
    hash_upload_abs_manifest,
    subtree_manifest,
    S3DataCache,
)

# 1. Collect directory tree into a manifest (no hashing yet)
manifest = collect_abs_snapshot(
    directories=["/projects/my_scene"],
    filenames=[],
)

# 2. Hash and upload to S3 in a single pipelined pass
data_cache = S3DataCache(
    s3_bucket="my-bucket",
    s3_key_prefix="Data",
    s3_client=boto3.client("s3"),
)
result = hash_upload_abs_manifest(manifest, data_cache)

# 3. Extract as relative-path manifest for storage/transport
relative_manifest = subtree_manifest(result.manifest, "/projects/my_scene")

print(f"Uploaded {result.statistics.uploaded_bytes} bytes")
```

To download files from a manifest:

```python
from deadline.job_attachments._snapshots import download_abs_manifest, join_manifest

# Convert relative-path manifest to absolute paths, then download
abs_manifest = join_manifest(relative_manifest, "/local/destination")
download_abs_manifest(abs_manifest, data_cache)
```

## Use Cases

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
2. (`deadline bundle submit --save-debug-snapshot`) When debugging a job, create
   a portable debug snapshot of all the input asset files. With a debug snapshot
   in hand, you can provide a reproducible artifact to a render TD or to
   vendor support personnel.
    1. Same as for submitting a job to a cloud render farm with COLLECT/HASH_UPLOAD/PARTITION,
       but when using HASH_UPLOAD provide a `FileSystemDataCache` that writes to your local file system
       to place in a zip file instead of uploading to the cloud.
3. (`deadline job download-output`) To download the output of a single Deadline Cloud job,
   take all the output manifests, join them to have absolute paths, compose them into
   a single manifest, and then download.
   1. Use JOIN make each task output manifest have absolute paths.
   2. Order the manifests by their S3 last-modified timestamp, then COMPOSE them into a single manifest.
   3. Use DOWNLOAD to apply the changes locally.
4. (`deadline queue upload` - implementation TBD) Before submitting a job to your queue,
   you have much of the asset data ready and would like to pre-populate your render farm
   data cache in the cloud. This case doesn't need the manifest, just the data uploads.
    1. Use COLLECT, providing the directories and individual filenames of the
       asset data. Use COLLAPSE_ESCAPING for the symlink_policy.
    2. Use HASH_UPLOAD to hash and upload the files that aren't already in the data
       cache. There's no need to convert the manifest to relative paths, as what's
       important for this use case is populating the local hash cache and the
       cloud data cache.
5. (`deadline manifest snapshot`) To collect a single directory tree into a manifest with relative paths:
    1. Use COLLECT with a single directory to collect, with COLLAPSE_ESCAPING as
       the symlink_policy
    2. (Optional) Use HASH to populate the hash values in the manifest. Run this
       while the manifest has absolute paths.
    3. Use SUBTREE to extract the directory as a relative-path manifest.
6. (`deadline attachment upload`) To hash and upload data for a manifest.
    1. Use JOIN to prepend the absolute root path to the provided manifest so it has absolute paths.
    2. Call clear_hashes() on the manifest to remove any pre-existing hashes.
    3. Use HASH_UPLOAD to hash and upload all the files to the data cache.
    4. Use SUBTREE to extract the directory as a relative-path manifest with hashes included.
7. (`deadline manifest diff`) To compute the changes that occurred in a directory since a snapshot was collected.
    1. Use COLLECT to collect an absolute manifest of the directory.
    2. If the original manifest was filtered, use FILTER to apply the exact same filter to the newly collected manifest.
    3. Use DIFF to take the difference between the two manifests.
    4. Use SUBTREE to extract the directory as a relative-path diff of that directory.

## Overview

The library provides four concrete manifest classes organized by two dimensions:

**Path Style:**
- **Relative paths** (`Snapshot`, `SnapshotDiff`) - Paths relative to an unspecified root, portable across systems
- **Absolute paths** (`AbsSnapshot`, `AbsSnapshotDiff`) - Full filesystem paths, required for file system operations

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

See [snapshot_manifest_classes.md](job_attachments_snapshots_components/snapshot_manifest_classes.md) for detailed documentation.

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

See [snapshot_data_cache_classes.md](job_attachments_snapshots_components/snapshot_data_cache_classes.md) for detailed documentation.

### Symlink Handling

Symlinks are handled via the `SymlinkPolicy` enum, which controls whether symlinks are preserved, collapsed to files/directories, or excluded. The default policy `COLLAPSE_ESCAPING` preserves symlinks whose targets are included and collapses those whose targets escape.

See [snapshot_symlink_handling.md](job_attachments_snapshots_components/snapshot_symlink_handling.md) for detailed documentation.

### Hash Cache

The hash cache is a local SQLite database that stores file hashes keyed by path, modification time, and byte range. It enables HASH, HASH_UPLOAD, and DOWNLOAD operations to skip re-hashing or re-downloading unchanged files.

See [snapshot_hash_cache.md](job_attachments_snapshots_components/snapshot_hash_cache.md) for detailed documentation.

### Design Choices

1. Simple and flexible in-memory snapshots and diffs shared by composable operations. Code can modify values
   in ways that doesn't strictly follow the on-disk manifest storage, but the operations and I/O accept
   and use the data where it makes sense.
2. File system operations only work with absolute path manifests. This simplifies the definition and implementation
   of these operations. Conversion to/from relative path manifests is via the SUBTREE and JOIN operations.
3. Path separators are always POSIX forward slash '/' in manifest path strings. See
   [snapshot_manifest_classes.md](job_attachments_snapshots_components/snapshot_manifest_classes.md#path-normalization)
   for complete path normalization rules.
4. Within a single manifest, all paths (file paths, directory paths, and symlink targets) share the same style—either
   all absolute or all relative to the same root. Symlink targets are stored relative to the manifest root, not
   relative to the symlink location.
5. In snapshot diffs, directory deletions must be accompanied by deletion of all the contents of the directory.
    1. This is necessary for the COMPOSE operation to correctly compose multiple diffs without a snapshot present.
    2. When applying a diff, a directory deletion means to delete the directory if it is empty, not
       to recursively delete its contents. This means that applying a snapshot diff to a file system requires that
       the paths being deleted must be sorted so that deeper directories are always processed before their parents.
6. There is no operation that uploads a hashed manifest. When we perform an upload, we always hash the data on
   its way into the content-addressed data cache. The manifest classes include a clear_hashes() method to make
   re-uploading with potentially new data easy.
    1. This guarantees that the data cache maintains its content-addressed storage invariant that data stored for
       a hash key always equals its hash. If we do a two-pass hash and then upload, concurrent processes could write
       to files in between, causing a content mismatch. This is true even if we lock the files, because file systems
       may not support the locking, or have options to disable it for higher performance.
7. Support for the v2023 on-disk format is via lossy conversion. When serializing to v2023 format, the following occurs:
    1. Symlinks are collapsed to files/directories or excluded (symlink_policy decides)
    2. Empty directories are not preserved
    3. Deletions are not preserved
    4. File chunk size must be set to WHOLE_FILE_CHUNK_SIZE.
    5. Runnable flags are not preserved

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

### Implementation Details

For HASH_UPLOAD and DOWNLOAD, additional documents cover internal architecture:

| Document | Description |
|----------|-------------|
| [snapshot_operation_hash_upload_pipeline.md](job_attachments_snapshots_components/snapshot_operation_hash_upload_pipeline.md) | Threading model, memory management, deduplication |
| [snapshot_operation_hash_upload_s3.md](job_attachments_snapshots_components/snapshot_operation_hash_upload_s3.md) | S3 multipart uploads, cache validation, streaming files |
| [snapshot_operation_download_pipeline.md](job_attachments_snapshots_components/snapshot_operation_download_pipeline.md) | Threading, atomicity, file chunked files, S3 multi-part downloads |

## Module Organization

The composable operations are implemented in separate modules under `src/deadline/job_attachments/_snapshots/_operations/`:

| Module | Operation | Description |
|--------|-----------|-------------|
| `_collect_abs_snapshot.py` | COLLECT | Scans directories/files, creates manifest with `hash=None` |
| `_collect_abs_snapshot_symlinks.py` | COLLECT | Symlink target resolution and policy-based handling |
| `_hash_abs_manifest.py` | HASH | Fills in hashes for manifests with `hash=None` |
| `_hash_upload_abs_manifest.py` | HASH_UPLOAD | Main entry point for hash+upload pipeline |
| `_hash_upload_abs_manifest_pipeline.py` | HASH_UPLOAD | Base pipeline class, progress state, work items, memory pool |
| `_hash_upload_abs_manifest_s3_pipeline.py` | HASH_UPLOAD | S3-specific upload logic (multipart, streaming) |
| `_hash_upload_abs_manifest_file_system_pipeline.py` | HASH_UPLOAD | FileSystem-specific upload logic |
| `_download_abs_manifest.py` | DOWNLOAD | Main entry point for download pipeline |
| `_download_abs_manifest_pipeline.py` | DOWNLOAD | Base pipeline class for callback-based downloads |
| `_download_abs_manifest_s3_pipeline.py` | DOWNLOAD | S3-specific download logic (parallel byte-range requests) |
| `_download_abs_manifest_file_system_pipeline.py` | DOWNLOAD | FileSystem-specific download logic (copies from local cache) |
| `_filter_manifest.py` | FILTER | Filters manifest entries using callable filter |
| `_diff_snapshots.py` | DIFF | Computes difference between two manifests |
| `_compose_manifest.py` | COMPOSE | Layers manifests together, later entries override earlier |
| `_subtree_manifest.py` | SUBTREE | Extracts a subtree, rebasing paths relative to new root |
| `_partition_manifest.py` | PARTITION | Divides manifest into multiple (root, RelManifest) pairs |
| `_join_manifest.py` | JOIN | Adds a prefix to all paths (inverse of SUBTREE) |
| `_sparse_file.py` | (utility) | Cross-platform sparse file pre-allocation |

## Refactoring the Existing Job Attachments Implementation

The existing job attachments code in `upload.py` and `download.py` can be incrementally refactored to use the new snapshots library. Key refactoring targets include:

- **Manifest conversions**: Functions to convert between `BaseAssetManifest` and `Snapshot`/`SnapshotDiff` (done)
- **Upload path preparation**: Using `partition_snapshot_by_storage_profile()` (done)
- **Upload operations**: Replacing with `hash_upload_abs_manifest()` using `S3DataCache`
- **Download operations**: Replacing with `download_abs_manifest()` after manifest adaptation
- **Progress tracking**: Adaptor classes to bridge different progress interfaces

See [snapshot_job_attachments_refactor.md](job_attachments_snapshots_components/snapshot_job_attachments_refactor.md) for detailed refactoring guidance for each function.

## ContentAddressedDataCache Classes

**Location:** `_content_addressed_data_cache.py`

The `ContentAddressedDataCache` is an abstract base class for content-addressable storage backends. Content is stored using its hash as the key, enabling deduplication and efficient retrieval.

```
ContentAddressedDataCache (abstract)
├── S3DataCache         - Amazon S3 storage with multipart transfer support
└── FileSystemDataCache - Local or network filesystem storage
```

Key design principles:
- **Hash-then-upload invariant**: Content is always hashed while being read for upload, never uploaded based on a pre-computed hash alone. This guarantees data integrity.
- **Existence checking**: Before uploading, checks if content already exists (S3CheckCache + HeadObject for S3, filesystem exists() for local).
- **Multipart transfers**: S3DataCache uses parallel multipart uploads/downloads for files larger than 64MB (configurable).

See [snapshot_data_cache_classes.md](job_attachments_snapshots_components/snapshot_data_cache_classes.md) for detailed documentation including fields, methods, security considerations, and usage examples.

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
| `DEFAULT_FILE_CHUNK_SIZE` | 256 MB (256 × 1024 × 1024) | Default threshold for file chunked hashing |
| `WHOLE_FILE_CHUNK_SIZE` | -1 | Sentinel value meaning "no file chunking, hash whole file" |
| `DEFAULT_S3_MULTIPART_PART_SIZE` | 32 MB (32 × 1024 × 1024) | Default part size for S3 multipart uploads/downloads |
| `NO_ACCOUNT_ID_CHECK` | Sentinel object | Disables ExpectedBucketOwner checks when passed as `S3DataCache.account_id` |
| `WHOLE_FILE_RANGE_END` | -1 | Sentinel value for hash cache `range_end` indicating a whole-file hash |
