# Composable Data Snapshot Operations

A manifest is a data structure that captures a directory tree snapshot—similar to a zip file's table of contents, but without the actual file content. It records metadata for each file (path, size, modification time, content hash) and, in newer formats, directories and symlinks. Manifests enable efficient change detection, incremental uploads, and content-addressable storage workflows.

This document describes the composable operations design for job attachment manifests in AWS Deadline Cloud. These operations provide a modular approach to creating, transforming, and comparing manifest objects.

## Overview

The manifest system uses composable operations that can be combined to implement various workflows.

Here are the manifest types used by these operations:

```
Manifest
├── Snapshot
│   ├── AbsSnapshot    (absolute paths)
│   └── RelSnapshot    (relative paths)
└── Diff
    ├── AbsDiff        (absolute paths)
    └── RelDiff        (relative paths)
```

Here are the data cache types used by these operations:

```
DataCache
├── S3DataCache         (data on S3)
└── FileSystemDataCache (data on a file system)
```

Here are the operations for working with data snapshots:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                 FILE SYSTEM AND DATA CACHE OPERATIONS                   │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  1. COLLECT: Paths → AbsSnapshot                                        │
│         Collects from lists of directories and filenames into a         │
│         snapshot with absolute paths. No hashing.                       │
│                                                                         │
│  2. HASH: AbsManifest → AbsManifest                                     │
│         Computes hashes for all files in the manifest.                  │
│         Works with both snapshots and diffs.                            │
│         Requires absolute paths; raises error for relative paths.       │
│                                                                         │
│  3. HASH_UPLOAD: (AbsManifest, DataCache) → AbsManifest                 │
│         Pipelines read/hash/upload for all files, returning a           │
│         manifest with hashes populated.                                 │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       MANIFEST TRANSFORMATIONS                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  4. FILTER: Manifest → Manifest                                         │
│         Applies a filter to entries, returning only accepted ones.      │
│                                                                         │
│  5. DIFF: (Snapshot, Snapshot) → Diff                                   │
│         Compares two snapshots, returning a diff of the changes.        │
│                                                                         │
│  6. COMPOSE: (Snapshot, Diff, ...) → Snapshot                           │
│     COMPOSE: (Diff, Diff, ...) → Diff                                   │
│         Layers diffs together, optionally onto a snapshot base,         │
│         as if applied sequentially to a filesystem.                     │
│                                                                         │
│  7. SUBTREE: (Snapshot, subtree_path) → RelSnapshot                     │
│         Extracts a subtree, returning a snapshot with relative paths.   │
│                                                                         │
│  8. PARTITION: (Snapshot, roots?) → List[(root, RelSnapshot)]           │
│         Partitions a manifest into multiple (root, RelSnapshot) pairs.  │
│                                                                         │
│  9. JOIN: (RelSnapshot, abs_prefix) → AbsSnapshot                       │
│     JOIN: (RelSnapshot, rel_prefix) → RelSnapshot                       │
│         Prepends a prefix to all paths.                                 │
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
       but when using HASH_UPLOAD provide a DataCache that goes to your local file system
       to place in a zip file instead of uploading to the cloud.
4. To collect a single directory tree into a manifest with relative paths:
    1. Use COLLECT with a single directory to collect, with PRESERVE as
       the symlink_policy
    2. Use SUBTREE to extract the directory as a relative-path manifest,
       with COLLAPSE_ESCAPING as the symlink_policy.
    3.

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
- **Reduced redundant reads:** The HASH_UPLOAD operation reads chunks of files to memory, then performs a hash + upload instead of one read for hash and a second read for upload.

## In-progress Refactor

This section tracks progress on unifying the in-memory manifest representation.

### Goal

**Before:** There are two memory manifest formats (v2023 and v2025), and there are two on-disk manifest formats (v2023 and v2025).

**After:** There is one memory manifest format (unversioned, matches the current v2025 memory format), and there are two on-disk manifest formats.

### Constraint

We must maintain backwards compatibility on all `Base*` classes and the v2023 interface.

### Design

In `job_attachments/asset_manifests/manifest.py`, we define the unified manifest classes:

```
Manifest Classes (new unified in-memory representation)
├── Manifest                    # Base class, calls validations from mixins
├── AbsManifestMixin            # Validates absolute paths
├── RelManifestMixin            # Validates relative paths
├── SnapshotManifestMixin       # Validates snapshot constraints
├── DiffManifestMixin           # Validates diff constraints
│
├── AbsSnapshotManifest(Manifest, AbsManifestMixin, SnapshotManifestMixin)
├── AbsDiffManifest(Manifest, AbsManifestMixin, DiffManifestMixin)
├── RelSnapshotManifest(Manifest, RelManifestMixin, SnapshotManifestMixin)
└── RelDiffManifest(Manifest, RelManifestMixin, DiffManifestMixin)

Path/Directory Classes (also in manifest.py)
├── ManifestFilePath            # File entry (unified, matches v2025 capabilities)
└── ManifestDirectoryPath       # Directory entry
```

For backwards compatibility in `base_manifest.py`:

```python
# Aliases for backwards compatibility
from .manifest import Manifest as BaseAssetManifest
from .manifest import ManifestFilePath as BaseManifestPath
from .manifest import ManifestDirectoryPath as BaseManifestDirectoryPath
```

The v2023 and v2025 modules continue to provide:
- `encode()` - Serialize to on-disk format
- `decode()` - Deserialize from on-disk format
- Version-specific validation during encode (e.g., v2023 rejects symlinks)

### Progress

| Task | Status | Notes |
|------|--------|-------|
| Create `manifest.py` with unified classes | ☐ Not started | |
| Create mixin classes for validation | ☐ Not started | |
| Create concrete manifest classes | ☐ Not started | |
| Update `base_manifest.py` to use aliases | ☐ Not started | |
| Update v2023 module for compatibility | ☐ Not started | |
| Update v2025 module for compatibility | ☐ Not started | |
| Update operations to use new classes | ☐ Not started | |
| Update tests | ☐ Not started | |
| Verify backwards compatibility | ☐ Not started | |

## Path Separator Convention

**All paths in manifests use forward slashes (`/`) as the directory separator, regardless of the host operating system.**

This convention ensures manifests are portable across platforms:

| Platform | Filesystem Separator | Manifest Separator |
|----------|---------------------|-------------------|
| Windows | `\` (backslash) | `/` (forward slash) |
| POSIX (Linux, macOS) | `/` (forward slash) | `/` (forward slash) |

**Important platform differences:**

- **On Windows:** The backslash (`\`) is a directory separator. When collecting paths from the filesystem, backslashes are converted to forward slashes for storage in the manifest.
- **On POSIX:** The backslash (`\`) is a valid character in file and directory names (though rarely used). It is NOT treated as a directory separator. A file named `foo\bar.txt` on POSIX is a single filename containing a backslash, not a file `bar.txt` in directory `foo`.

**Implementation requirements:**

1. **COLLECT operation:** When scanning the filesystem on Windows, use `Path.as_posix()` to convert paths to forward slashes. On POSIX, paths already use forward slashes.
2. **SUBTREE operation:** The `_normalize_subtree_path()` function converts backslashes to forward slashes in the subtree parameter only when running on Windows.
3. **JOIN operation:** The `_normalize_prefix()` function converts backslashes to forward slashes in the prefix parameter only when running on Windows.
4. **All operations:** Path comparisons and manipulations use forward slashes consistently.

**Note:** Operations that accept path parameters (SUBTREE, JOIN) normalize backslashes to forward slashes only when running on Windows. On POSIX systems, backslashes are preserved as valid filename characters.

## Symlink Target Path Consistency

**Symlink targets must use the same path style (absolute or relative) as the manifest's entry paths.**

| Manifest Type | Entry Paths | Symlink Targets |
|---------------|-------------|-----------------|
| AbsSnapshot / AbsDiff | Absolute (e.g., `/projects/scene/file.txt`) | Absolute (e.g., `/projects/scene/target.txt`) |
| RelSnapshot / RelDiff | Relative (e.g., `scene/file.txt`) | Relative (e.g., `scene/target.txt`) |

**Important:** In relative-path manifests, symlink targets are relative to the manifest root, NOT relative to the symlink's location. This differs from on-filesystem symlink representations where targets are typically relative to the symlink's parent directory.

| Context | Symlink Target Interpretation |
|---------|------------------------------|
| Filesystem | Relative to symlink's parent directory |
| Manifest (relative) | Relative to manifest root |

This consistency requirement ensures:

1. **Portability:** Relative manifests can be relocated without breaking symlink references
2. **Correctness:** Operations like SUBTREE and JOIN can transform symlink targets consistently with entry paths
3. **Validation:** Tools can verify manifest integrity by checking path style consistency

**Example - Absolute manifest with symlinks:**

```python
# In an absolute-path manifest, symlink targets are also absolute
ManifestFilePath(
    path="/projects/my_scene/link.txt",
    symlink_target="/projects/my_scene/target.txt",  # Absolute target
)
```

**Example - Relative manifest with symlinks:**

```python
# In a relative-path manifest, symlink targets are relative to manifest root
# NOT relative to the symlink's parent directory
ManifestFilePath(
    path="my_scene/link.txt",
    symlink_target="my_scene/target.txt",  # Relative to manifest root, not to my_scene/
)
```

## Module Organization

The composable operations are implemented in separate modules under `src/deadline/job_attachments/asset_manifests/`:

| Module | Operation | Description |
|--------|-----------|-------------|
| `_collect_manifest.py` | COLLECT | Scans directories/files, creates manifest with `hash=""` |
| `_hash_manifest.py` | HASH | Fills in hashes for collected manifest |
| `_hash_upload_manifest.py` | HASH_UPLOAD | Fills in hashes AND uploads to a data cache in a pipelined manner |
| `_filter_manifest.py` | FILTER | Filters manifest entries using callable filter |
| `_diff_manifest.py` | DIFF | Computes difference between two manifests |
| `_compose_manifest.py` | COMPOSE | Layers manifests together into one |
| `_subtree_manifest.py` | SUBTREE | Extracts a subtree as a new manifest |
| `_partition_manifest.py` | PARTITION | Partitions manifest into (root, RelSnapshot) pairs |
| `_join_manifest.py` | JOIN | Joins a prefix to all paths in a manifest |

## Operation Details

### 1. COLLECT: `collect_manifest()`

**Location:** `_collect_manifest.py`

Collects provided lists of paths into a manifest with absolute paths, WITHOUT computing hashes:

```python
def collect_manifest(
    directories: List[Path | str],
    filenames: List[Path | str],
    *,
    optional_filenames: Optional[List[Path | str]] = None,
    version: ManifestVersion,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.PRESERVE,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> BaseAssetManifest:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `directories` | (positional) List of directory paths whose full contents are collected. All paths must exist and be directories. Empty directories are included in the manifest (v2025 only). |
| `filenames` | (positional) List of file/symlink paths that must exist. Raises `FileNotFoundError` if any file does not exist. |
| `optional_filenames` | List of file/symlink paths to include if they exist. Missing files are silently ignored. |
| `version` | Manifest version to create (determines features) |
| `symlink_policy` | How to handle symlinks during collection (see below). Default `PRESERVE`. |
| `print_function_callback` | Progress callback for status messages |

**Symlink Policy Options (for `collect_manifest`):**

| Policy | Description | v2023 Support |
|--------|-------------|---------------|
| `PRESERVE` | Keep all symlinks as symlink entries with absolute targets. (default) | ✗ (v2025 only) |
| `COLLAPSE` | Follow all symlinks, treating them as files/directories. | ✓ |
| `TRANSITIVE_INCLUDE_TARGETS` | Keep all symlinks and add their targets to the manifest. | ✗ (v2025 only) |
| `EXCLUDE` | Skip all symlinks entirely. | ✓ |

**Note:** `COLLAPSE_ESCAPING` is not supported by `collect_manifest` because there is no root path to determine what "escaping" means.

**Validation Rules:**

| Condition | Behavior |
|-----------|----------|
| `symlink_policy=COLLAPSE_ESCAPING` | Raises `ValueError` |
| File in `filenames` does not exist | Raises `FileNotFoundError` |
| File in `optional_filenames` does not exist | Silently ignored |
| Directory in `directories` does not exist | Raises `FileNotFoundError` |
| Path in `directories` is not a directory | Raises `ValueError` |
| Path in `filenames` is not a file or symlink | Raises `ValueError` |

**Key implementation details:**

- Files have `hash=""` (empty string) to indicate hashing is needed
- Symlinks have `symlink_target` set as absolute paths (no hash needed)
- All paths in the manifest are absolute
- Useful for intermediate in-memory processing or collecting from multiple locations

**Example - Collecting from multiple directories:**

```python
from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest
)
from deadline.job_attachments.asset_manifests.versions import ManifestVersion, SymlinkPolicy

# Collect files from different locations using absolute paths (default: PRESERVE symlinks)
manifest = collect_manifest(
    ["/data/shared/models", "/data/shared/textures"],  # directories (positional)
    ["/home/user/project/scene.blend"],                 # filenames (positional)
    optional_filenames=["/home/user/project/cache.bin"],  # Included if exists
    version=ManifestVersion.v2025_12_04_beta,
)

# Paths in manifest are absolute
for entry in manifest.paths[:2]:
    print(f"  {entry.path}")  # e.g., "/data/shared/models/car.obj"
```

**Example - Symlinks are preserved by default:**

```python
from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest
)
from deadline.job_attachments.asset_manifests.versions import ManifestVersion

# Collect with symlinks preserved (default behavior)
manifest = collect_manifest(
    ["/projects/my_scene"],  # directories
    [],                       # filenames (empty list)
    version=ManifestVersion.v2025_12_04_beta,
)

# Symlinks have absolute targets
for entry in manifest.paths:
    if entry.symlink_target:
        print(f"  symlink: {entry.path} -> {entry.symlink_target}")
        # e.g., symlink: /projects/my_scene/link.txt -> /projects/my_scene/target.txt
```

**Helper functions:**

- `_create_unhashed_file_entry()` - Creates file entry with `hash=""` and metadata
- `_create_symlink_entry()` - Creates symlink entry with validated target

### 2. HASH: `hash_manifest()`

**Location:** `_hash_manifest.py`

Fills in hashes for a manifest that was created by `collect_manifest()` or `compute_diff_manifest()`. The input manifest must have absolute paths.

```python
def hash_manifest(
    manifest: BaseAssetManifest,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> BaseAssetManifest:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | Manifest with absolute paths and empty hashes. Can be either a snapshot (from `collect_manifest`) or a diff (from `compute_diff_manifest` with `ignore_hashes=True`) |
| `hash_cache` | Optional hash cache for efficiency |
| `force_rehash` | If `True`, ignore cache and recalculate all hashes |
| `print_function_callback` | Progress callback for status messages |

**Returns:** A NEW manifest with all hashes filled in. The manifest type (snapshot/diff) and `parentManifestHash` are preserved from the input.

**Raises:** `ValueError` if the manifest contains relative paths

**Hash cache behavior:**

| Condition | Behavior |
|-----------|----------|
| `hash_cache` provided, `force_rehash=False` | Check cache by (path, mtime); use cached hash on hit |
| `hash_cache` provided, `force_rehash=True` | Always compute hash, update cache |
| `hash_cache` is None | Always compute hash |

**Large file handling (v2025-12-04-beta):**

Files larger than 256MB (`FILE_CHUNK_SIZE_BYTES`) use chunked hashing:

- `hash` field is `None`
- `chunkhashes` contains list of hashes, one per 256MB chunk
- Chunk count must equal `ceil(size / 256MB)`

**Entry type handling:**

| Entry Type | Action |
|------------|--------|
| Regular file (≤256MB) | Compute single hash |
| Large file (>256MB) | Compute chunkhashes |
| Symlink | Pass through unchanged |
| Deleted marker | Pass through unchanged (diff manifests only) |
| Directory | Pass through unchanged |

**Manifest type handling:**

| Manifest Type | Behavior |
|---------------|----------|
| Snapshot | All file entries are hashed |
| Diff | Only new/modified file entries are hashed; deleted entries pass through unchanged |

The `manifestType` and `parentManifestHash` fields are preserved from the input manifest.

**Helper functions:**

- `_get_or_compute_hash()` - Gets hash from cache or computes it (supports byte ranges)
- `_hash_file_chunked()` - Hashes large file in chunks with cache support

**Example - Hashing a snapshot:**

```python
from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
    hash_manifest,
)
from deadline.job_attachments.asset_manifests.versions import ManifestVersion
from deadline.job_attachments.caches.hash_cache import HashCache

# Collect the directory tree with absolute paths
abs_manifest = collect_manifest(
    ["/projects/my_scene"],  # directories
    [],                       # filenames
    version=ManifestVersion.v2025_12_04_beta,
)

# Hash with a cache for efficiency
with HashCache("/tmp/hash_cache") as cache:
    hashed = hash_manifest(
        manifest=abs_manifest,
        hash_cache=cache,
        force_rehash=False,  # Use cached hashes when available
    )

# Now entries have their hashes filled in (paths are still absolute)
for entry in hashed.paths[:2]:
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
from deadline.job_attachments.asset_manifests._operations import (
    compute_diff_manifest,
    hash_manifest,
)

# Compute a diff between two snapshots (with ignore_hashes=True for fast comparison)
diff = compute_diff_manifest(
    parent=parent_snapshot,
    current=current_snapshot,
    parent_manifest_hash="abc123...",
    ignore_hashes=True,  # Compare by mtime/size only
)

# Now hash the diff to fill in hashes for new/modified files
hashed_diff = hash_manifest(diff)

# Deleted entries are preserved unchanged
for entry in hashed_diff.paths:
    if entry.deleted:
        print(f"  deleted: {entry.path}")
    else:
        print(f"  new/modified: {entry.path} hash={entry.hash[:16]}...")
```

### 3. HASH_UPLOAD: `hash_upload_manifest()`

**Location:** `_hash_upload_manifest.py`

Fills in hashes for a manifest AND uploads file content to S3 in a pipelined manner. This operation combines hashing and uploading into a single pass over the data, avoiding the need to read files twice (once for hashing, once for uploading).

```python
def hash_upload_manifest(
    manifest: BaseAssetManifest,
    s3_bucket: str,
    s3_key_prefix: str,
    boto3_session: Optional[boto3.Session] = None,
    hash_cache: Optional[HashCache] = None,
    s3_check_cache: Optional[S3CheckCache] = None,
    force_rehash: bool = False,
    max_memory_bytes: Optional[int] = None,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
    progress_tracker: Optional[ProgressTracker] = None,
) -> BaseAssetManifest:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | Manifest with absolute paths and empty hashes. Can be either a snapshot (from `collect_manifest`) or a diff (from `compute_diff_manifest` with `ignore_hashes=True`) |
| `s3_bucket` | S3 bucket name for uploads |
| `s3_key_prefix` | S3 key prefix for content-addressable storage (e.g., `"Data"`) |
| `boto3_session` | Optional boto3 session for AWS credentials |
| `hash_cache` | Optional hash cache for efficiency |
| `s3_check_cache` | Optional S3 check cache to skip already-uploaded files |
| `force_rehash` | If `True`, ignore cache and recalculate all hashes |
| `max_memory_bytes` | Maximum memory to use for buffering (default: auto-detect from system) |
| `print_function_callback` | Progress callback for status messages |
| `progress_tracker` | Optional progress tracker for upload progress |

**Returns:** A NEW manifest with all hashes filled in. The manifest type (snapshot/diff) and `parentManifestHash` are preserved from the input.

**Raises:** `ValueError` if the manifest contains relative paths

**Pipelined Architecture:**

The operation uses a multi-threaded pipeline with three stages:

```
┌─────────┐     ┌─────────┐     ┌─────────┐
│  READ   │────►│  HASH   │────►│ UPLOAD  │
│ Thread  │     │ Thread  │     │ Thread  │
└─────────┘     └─────────┘     └─────────┘
     │               │               │
     └───────────────┴───────────────┘
              Memory Pool
         (bounded by max_memory_bytes)
```

1. **READ stage:** Reads file chunks (256MB for large files, whole file for small files) from disk into memory buffers
2. **HASH stage:** Computes XXH128 hash of each chunk in memory
3. **UPLOAD stage:** Uploads the chunk to S3 using the hash as the object key

**Memory Management:**

The pipeline constrains total memory usage across all stages:

- When `max_memory_bytes` is reached, the READ stage blocks until UPLOAD completes and frees memory
- Each chunk occupies memory from READ through UPLOAD completion

**Default Memory Limit Calculation:**

When `max_memory_bytes` is not specified, the default is calculated as the maximum of:

| Option | Value | Rationale |
|--------|-------|-----------|
| Minimum | 256MB | One chunk must fit; worst case processes one chunk at a time |
| Quarter of total | `total_memory / 4` | Use a reasonable portion of system resources |
| Available minus 1GB | `available_memory - 1GB` | When lots of free memory exists (e.g., 60GB), use most of it |

```python
default_limit = max(256MB, total_memory // 4, available_memory - 1GB)
```

**Example calculations:**

| System | Total | Available | Quarter | Avail-1GB | Result |
|--------|-------|-----------|---------|-----------|--------|
| Low memory | 4GB | 2GB | 1GB | 1GB | 1GB |
| Typical workstation | 32GB | 20GB | 8GB | 19GB | 19GB |
| High memory server | 128GB | 100GB | 32GB | 99GB | 99GB |
| Constrained (busy) | 32GB | 1.5GB | 8GB | 0.5GB | 8GB |

This ensures the pipeline uses as much memory as safely available while maintaining a reasonable lower bound

**Chunk Processing:**

| File Size | Chunk Size | Processing |
|-----------|------------|------------|
| ≤256MB | Whole file | Single chunk: read → hash → upload |
| >256MB | 256MB | Multiple chunks processed sequentially per file |

For large files (>256MB), chunks are processed in order:
1. Read chunk 0 → Hash chunk 0 → Upload chunk 0
2. Read chunk 1 → Hash chunk 1 → Upload chunk 1
3. ... and so on

This ensures that for any single large file, memory usage is bounded to ~256MB per file in the pipeline.

**S3 Key Format:**

Files are uploaded to content-addressable storage with keys:
```
{s3_key_prefix}/{hash}.{algorithm}
```

Example: `Data/a1b2c3d4e5f67890abcdef1234567890.xxh128`

For chunked files, each chunk is uploaded separately:
```
Data/{chunk0_hash}.xxh128
Data/{chunk1_hash}.xxh128
...
```

**Cache Integration:**

| Cache | Purpose |
|-------|---------|
| `hash_cache` | Skip hashing for files with unchanged mtime |
| `s3_check_cache` | Skip upload for files already in S3 |

When both caches hit, the file is completely skipped (no read, no hash, no upload).

**Entry Type Handling:**

| Entry Type | Action |
|------------|--------|
| Regular file (≤256MB) | Read → Hash → Upload (single chunk) |
| Large file (>256MB) | Read → Hash → Upload (per 256MB chunk) |
| Symlink | Pass through unchanged (no upload) |
| Deleted marker | Pass through unchanged (no upload) |
| Directory | Pass through unchanged (no upload) |

**Manifest type handling:**

| Manifest Type | Behavior |
|---------------|----------|
| Snapshot | All file entries are hashed and uploaded |
| Diff | Only new/modified file entries are hashed and uploaded; deleted entries pass through unchanged |

The `manifestType` and `parentManifestHash` fields are preserved from the input manifest.

**Error Handling:**

- If upload fails, the operation raises an exception with details
- Partial uploads are not cleaned up (S3 content-addressable storage is idempotent)
- The hash cache is updated even if upload fails (hash is still valid)

**Example:**

```python
from deadline.job_attachments.asset_manifests._operations import (
    collect_manifest,
    hash_upload_manifest,
)
from deadline.job_attachments.asset_manifests.versions import ManifestVersion
from deadline.job_attachments.caches.hash_cache import HashCache
from deadline.job_attachments.caches.s3_check_cache import S3CheckCache

# Collect the directory tree with absolute paths
abs_manifest = collect_manifest(
    ["/projects/my_scene"],  # directories
    [],                       # filenames
    version=ManifestVersion.v2025_12_04_beta,
)

# Hash and upload in a single pipelined pass (manifest has absolute paths)
with HashCache("/tmp/hash_cache") as hash_cache:
    with S3CheckCache("/tmp/s3_cache") as s3_cache:
        hashed = hash_upload_manifest(
            manifest=abs_manifest,
            s3_bucket="my-job-attachments-bucket",
            s3_key_prefix="Data",
            hash_cache=hash_cache,
            s3_check_cache=s3_cache,
            max_memory_bytes=1024 * 1024 * 1024,  # 1GB memory limit
        )

# Now entries have their hashes filled in AND files are uploaded (paths are still absolute)
for entry in hashed.paths[:2]:
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

**Performance Comparison:**

| Approach | Disk Reads | Network Uploads | Memory Peak |
|----------|------------|-----------------|-------------|
| HASH then upload | 2× (hash + upload) | 1× | Low |
| HASH_UPLOAD | 1× | 1× | Bounded by `max_memory_bytes` |

For large datasets, HASH_UPLOAD can be up to 2× faster due to single-pass I/O.

**When to Use HASH vs HASH_UPLOAD:**

| Use Case | Recommended Operation |
|----------|----------------------|
| Local manifest creation (no upload) | HASH |
| Diff computation only | HASH |
| Job submission with upload | HASH_UPLOAD |
| Output sync from worker | HASH_UPLOAD |
| Testing/debugging | HASH (simpler) |

### 4. FILTER: `filter_manifest()`

**Location:** `_filter_manifest.py`

Applies a filter to manifest entries, returning a new manifest with only matching entries:

```python
def filter_manifest(
    manifest: BaseAssetManifest,
    entry_filter: Callable[[Union[BaseManifestPath, BaseManifestDirectoryPath]], bool],
) -> BaseAssetManifest:
```

**Filter interface:**

The filter is a callable that takes a manifest entry and returns `True` to keep it:

```python
# Example: Custom filter for large files only
def large_files_only(entry):
    if isinstance(entry, BaseManifestPath) and entry.size is not None:
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
from deadline.job_attachments.asset_manifests._operations import (
    filter_manifest,
)
from deadline.job_attachments.asset_manifests._operations._filter_manifest import (
    IncludeExcludePathsFilter,
)

# Filter to only include Blender files and textures, excluding backups
filter = IncludeExcludePathsFilter(
    include=["*.blend", "textures/**/*"],
    exclude=["backup/*", "*_old.*"],
)

filtered = filter_manifest(manifest, filter)
print(f"Filtered from {len(manifest.paths)} to {len(filtered.paths)} entries")

# Or use a custom filter function
def python_files_only(entry):
    return entry.path.endswith(".py")

py_manifest = filter_manifest(manifest, python_files_only)
```

### 5. DIFF: `compute_diff_manifest()`

**Location:** `_diff_manifest.py`

Computes the difference between two snapshot manifests:

```python
def compute_diff_manifest(
    parent: BaseAssetManifest,
    current: BaseAssetManifest,
    parent_manifest_hash: Optional[str] = None,
    ignore_hashes: bool = False,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
    *,
    preserve_runnable: bool = False,
) -> BaseAssetManifest:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `parent` | The parent snapshot manifest (filtered, with hashes) |
| `current` | The current snapshot manifest (filtered, with hashes) |
| `parent_manifest_hash` | Optional hash of the parent manifest (for v2025 diff manifests) |
| `ignore_hashes` | If `True`, compare by metadata only (size, mtime, runnable) without hashes |
| `print_function_callback` | Progress callback for status messages |
| `preserve_runnable` | If `True`, copy `runnable` from parent for modified files (see below) |

**Preconditions:**

1. Both manifests must be the same version
2. Both manifests should be filtered with the same patterns
3. Both manifests should have hashes computed (unless `ignore_hashes=True`)

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
| manifestType | SNAPSHOT | DIFF |
| parentManifestHash | N/A | ✓ (if provided) |

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
from deadline.job_attachments.asset_manifests._operations import compute_diff_manifest
from deadline.job_attachments.asset_manifests.decode import decode_manifest
from deadline.job_attachments.asset_manifests.hash_algorithms import hash_data, HashAlgorithm

# Load the parent manifest
with open("previous.manifest") as f:
    parent_str = f.read()
    parent = decode_manifest(parent_str)
    parent_hash = hash_data(parent_str.encode("utf-8"), HashAlgorithm.XXH128)

# Assume current_hashed is a collected and hashed manifest of the current directory
diff = compute_diff_manifest(
    parent=parent,
    current=current_hashed,
    parent_manifest_hash=parent_hash,
    ignore_hashes=False,  # Compare by hash for accuracy
    preserve_runnable=True,  # Preserve execute bits from parent for modified files
)

# Inspect the diff
new_files = [p for p in diff.paths if p.path not in {e.path for e in parent.paths}]
deleted = [p for p in diff.paths if p.deleted]
print(f"New: {len(new_files)}, Deleted: {len(deleted)}")
print(f"Diff manifest type: {diff.manifestType}")  # DIFF for v2025
print(f"Parent hash: {diff.parentManifestHash[:16]}...")
```

Output:
```
New: 3, Deleted: 1
Diff manifest type: ManifestType.DIFF
Parent hash: f8e9d0c1b2a34567...
```

### 6. COMPOSE: `compose_manifests()`

**Location:** `_compose_manifest.py`

Layers multiple manifests together into a single manifest, as if applying each manifest as a set of changes in order:

```python
def compose_manifests(
    manifests: List[BaseAssetManifest],
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> BaseAssetManifest:
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
- Entries are keyed by path; later entries replace earlier ones
- Deleted markers remove the entry entirely from the result (v2025)
- Total size is recomputed from the final entry set
- For v2025 snapshot+diffs: first must be snapshot, rest must be diffs
- For v2025 diff composition: all must be diffs, `parentManifestHash` from first diff

**Example:**

```python
from deadline.job_attachments.asset_manifests._operations import compose_manifests
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

print(f"Final manifest has {len(final.paths)} entries")
print(f"Manifest type: {final.manifestType}")  # SNAPSHOT
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

### 7. SUBTREE: `subtree_manifest()`

**Location:** `_subtree_manifest.py`

Extracts a subtree from a manifest, producing a new manifest rooted at the specified subdirectory:

```python
def subtree_manifest(
    manifest: BaseAssetManifest,
    subtree: str,
    *,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> BaseAssetManifest:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | The source manifest to extract from |
| `subtree` | Path to the subtree root (relative or absolute, must match manifest path style) |
| `symlink_policy` | How to handle symlinks that escape the new subtree root (see below) |
| `print_function_callback` | Progress callback for status messages |

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

**Symlink Handling:**

When re-rooting a manifest, symlinks that were previously "within root" may now "escape" the new subtree root. The `symlink_policy` parameter controls how these are handled:

| Policy | Behavior for Escaping Symlinks |
|--------|-------------------------------|
| `COLLAPSE` | Replace all symlinks with their target's content (if target is in original manifest) |
| `COLLAPSE_ESCAPING` | Collapse only symlinks escaping the new subtree; preserve symlinks within subtree |
| `EXCLUDE` | Remove symlinks that escape the new subtree |

**Note:** `PRESERVE` and `TRANSITIVE_INCLUDE_TARGETS` are not supported for SUBTREE. Since the output always uses relative paths, escaping symlinks cannot be represented—a relative symlink target like `../outside/file.txt` would point outside the manifest root, which is invalid. Therefore, escaping symlinks must either be collapsed or excluded.

**Note:** Unlike COLLECT, SUBTREE operates purely on manifest data—it never accesses the filesystem. When a symlink is "collapsed," the operation looks up the target path in the original manifest and copies that entry's data (hash, size, mtime, etc.) to replace the symlink entry.

**Important: Symlink Target Storage Format:**

In our manifest format, symlink targets are stored **relative to the manifest root**, not relative to the symlink location (unlike POSIX filesystem symlinks). For example, a symlink at `assets/textures/current` pointing to `assets/shared/latest.png` stores the target as `assets/shared/latest.png`, not as `../shared/latest.png`.

This design choice simplifies manifest operations since targets can be looked up directly in the manifest's path index without needing to resolve relative paths from the symlink's location.

**Symlink Collapse Behavior:**

When collapsing a symlink, the operation looks up the target path in the original manifest:

- **File target:** The symlink entry is replaced with a copy of the target file entry (using the symlink's path, but the target's hash, size, mtime, runnable)
- **Directory target:** The symlink entry is replaced with all entries under that directory in the original manifest, recursively. Paths are rebased so the symlink path becomes the new prefix (e.g., symlink `current` with target `assets/shared/v2` containing `assets/shared/v2/a.txt` and `assets/shared/v2/sub/b.txt` produces `current/a.txt` and `current/sub/b.txt`)
- **Missing target:** If the target doesn't exist in the manifest (e.g., it was an escaping symlink that was already collapsed during COLLECT), the symlink is excluded with a warning

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
from deadline.job_attachments.asset_manifests._operations import subtree_manifest
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
for entry in textures.paths:
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

# With EXCLUDE: symlink is removed
textures = subtree_manifest(
    manifest=full_manifest,
    subtree="assets/textures",
    symlink_policy=SymlinkPolicy.EXCLUDE,
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

### 8. PARTITION: `partition_manifest()`

**Location:** `_partition_manifest.py`

Partitions a manifest into multiple (root, RelSnapshot) pairs, dividing entries by their root paths. Each RelSnapshot is an extracted subtree per the SUBTREE operation, with paths relative to its root:

```python
def partition_manifest(
    manifest: BaseAssetManifest,
    roots: Optional[List[str]] = None,
    *,
    referenced_paths: Optional[List[str]] = None,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> List[Tuple[str, BaseAssetManifest]]:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | Source manifest (absolute or relative paths) |
| `roots` | Optional list of root paths to partition by. No root may be a subpath of another. |
| `referenced_paths` | Optional list of paths referenced by the workload. These paths must be within one of the resulting roots, affecting auto-root determination even if no files exist under them. |
| `symlink_policy` | How to handle symlinks that escape their partition root. Only COLLAPSE, COLLAPSE_ESCAPING, and EXCLUDE are supported. |
| `print_function_callback` | Progress callback for status messages |

**Returns:** A list of `(root, RelSnapshot)` tuples where:
- Each `root` is an absolute or relative path string
- Each `RelSnapshot` is a manifest with paths relative to that root

**Validation Rules:**

| Condition | Behavior |
|-----------|----------|
| A root is a subpath of another root | Raises `ValueError` |
| Root path style doesn't match manifest path style | Raises `ValueError` |
| `symlink_policy=PRESERVE` | Raises `ValueError` |
| `symlink_policy=TRANSITIVE_INCLUDE_TARGETS` | Raises `ValueError` |

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

**Symlink Handling:**

Symlinks are handled per-partition using the same logic as SUBTREE:
- Symlinks pointing within their partition root are preserved (rebased)
- Symlinks escaping their partition root are handled per `symlink_policy`

**Example - Auto-partition on POSIX (no roots provided):**

```python
from deadline.job_attachments.asset_manifests._operations import partition_manifest

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

### 9. JOIN: `join_manifest()`

**Location:** `_join_manifest.py`

Joins a prefix to all paths in a manifest, producing a new manifest with prefixed paths:

```python
def join_manifest(
    manifest: BaseAssetManifest,
    prefix: str,
    *,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> BaseAssetManifest:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | The source manifest to transform |
| `prefix` | Path prefix to join to all paths (relative or absolute) |
| `print_function_callback` | Progress callback for status messages |

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

**Example - Converting Relative to Absolute:**

```python
from deadline.job_attachments.asset_manifests._operations import join_manifest
from deadline.job_attachments.asset_manifests.decode import decode_manifest

# Load a manifest with relative paths
with open("textures.manifest") as f:
    manifest = decode_manifest(f.read())

# Join with absolute prefix to get absolute paths
absolute_manifest = join_manifest(manifest, "/projects/scene/assets/textures")

for entry in absolute_manifest.paths:
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
Directory ──[collect]──► AbsSnapshot ──[subtree]──► Unhashed ──[hash]──► Hashed ──[filter]──► Filtered ──[save]──► File
```

```python
# Step 1: Collect directory tree with absolute paths
abs_manifest = collect_manifest([root], [], version=version)

# Step 2: Extract as relative paths
unhashed = subtree_manifest(abs_manifest, root)

# Step 3: Hash all files
hashed = hash_manifest(unhashed, root, hash_cache)

# Step 4: Filter the snapshot
filter = IncludeExcludePathsFilter(include=include, exclude=exclude)
filtered = filter_manifest(hashed, filter)

# Step 5: Write to file
manifest_path = _write_manifest(root, filtered, destination, name)
```

### Diff Flow (Fast Mode)

Compare by mtime/size without hashing unchanged files:

```
Parent File ──[load]──► Parent ──[filter]──► Filtered Parent ──┐
                                                               ├──[diff]──► Diff Manifest
Directory ──[collect]──► AbsSnapshot ──[subtree]──► Unhashed ──[filter]──► Filtered Current ─┘
                                                        (no hashes)
```

```python
# Load parent manifest
with open(parent_path) as f:
    parent_str = f.read()
    parent = decode_manifest(parent_str)
    parent_hash = hash_data(parent_str.encode("utf-8"), HashAlgorithm.XXH128)

# Collect current directory with absolute paths, then extract as relative
abs_manifest = collect_manifest([root], [], version=version)
current_unhashed = subtree_manifest(abs_manifest, root)

# Filter BOTH with same patterns
filter = IncludeExcludePathsFilter(include=include, exclude=exclude)
filtered_parent = filter_manifest(parent, filter)
filtered_current = filter_manifest(current_unhashed, filter)

# Compute diff (fast mode - compare by mtime/size)
diff = compute_diff_manifest(
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
Directory ──[collect]──► AbsSnapshot ──[subtree]──► Unhashed ──[hash]──► Hashed ──[filter]──► Filtered Current ─┘
```

```python
# Load parent manifest
with open(parent_path) as f:
    parent_str = f.read()
    parent = decode_manifest(parent_str)
    parent_hash = hash_data(parent_str.encode("utf-8"), HashAlgorithm.XXH128)

# Collect and hash current directory
abs_manifest = collect_manifest([root], [], version=version)
current_unhashed = subtree_manifest(abs_manifest, root)
current_hashed = hash_manifest(current_unhashed, root, hash_cache, force_rehash=True)

# Filter BOTH with same patterns
filter = IncludeExcludePathsFilter(include=include, exclude=exclude)
filtered_parent = filter_manifest(parent, filter)
filtered_current = filter_manifest(current_hashed, filter)

# Compute diff (full mode - compare by hash)
diff = compute_diff_manifest(
    parent=filtered_parent,
    current=filtered_current,
    parent_manifest_hash=parent_hash,
    ignore_hashes=False,  # Full mode
)
```

## Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `FILE_CHUNK_SIZE_BYTES` | 256 MB (256 × 1024 × 1024) | Threshold for chunked hashing |

## Validation Rules

### Symlink Validation (v2025-12-04-beta)

- Target must be within the manifest root
- If target is absolute, it is converted to relative

### Chunked File Validation (v2025-12-04-beta)

- Files >256MB must use `chunkhashes` (not `hash`)
- Files ≤256MB must use `hash` (not `chunkhashes`)
- Chunk count must equal `ceil(size / 256MB)`

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
