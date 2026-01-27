# DOWNLOAD Operation: `download_abs_manifest()`

*← [Back to Job Attachments Snapshots](../job_attachments_snapshots.md)*


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

    # Timing information
    total_time: float  # Elapsed time since operation start (seconds)
    transfer_rate: float  # Current transfer rate (bytes/second)
```

**Transfer Rate Calculation:**

The `transfer_rate` field uses a sliding window algorithm for smooth, responsive rate estimation:

- Maintains a deque of `(timestamp, downloaded_bytes)` snapshots
- Window size: 12 seconds (`TRANSFER_RATE_WINDOW_SECONDS`)
- Rate = `(current_bytes - oldest_bytes) / (current_time - oldest_time)`
- At operation start (< 12s elapsed), uses all available history from start to current time
- Updated on every part download for granular progress (not just per-file/chunk completion)

*Window adjustment:* After each progress update, old entries are pruned from the front of the deque.
Entries are removed while there are at least 2 entries and the second entry is older than 12 seconds ago.
This keeps the oldest entry just outside the window boundary, ensuring the rate calculation always
spans approximately the full window duration.

This approach provides:
- Smooth rate display that doesn't jump erratically
- Quick response to throughput changes (12s window)
- Accurate rates even during bursty transfers

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
| `total_time` | Updated on every progress callback (elapsed time since start) |
| `transfer_rate` | Updated on every progress callback (sliding window calculation) |

**Example - Progress callback:**

```python
from deadline.job_attachments._snapshots import (
    download_abs_manifest,
    DownloadProgressMetadata,
)

def on_progress(metadata: DownloadProgressMetadata) -> bool:
    # Access timing information
    rate_mb_s = metadata.transfer_rate / (1024 * 1024)
    print(f"Progress: {metadata.progress:.1f}% - {rate_mb_s:.1f} MB/s - {metadata.progressMessage}")
    # Return False to cancel, True to continue
    return True

result = download_abs_manifest(
    manifest=abs_manifest,
    data_cache=s3_cache,
    on_progress=on_progress,
)

# Access final timing from statistics
print(f"Completed in {result.statistics.total_time:.2f}s")
print(f"Average rate: {result.statistics.transfer_rate / (1024 * 1024):.1f} MB/s")
```

**Hash Cache Skip Optimization:**

When a `hash_cache` is provided, the DOWNLOAD operation checks each file before downloading. If the local file exists and its cached hash matches the manifest hash, the download is skipped.

This is useful for repeated downloads, incremental updates, and resuming after interruption. The hash cache is shared with HASH and HASH_UPLOAD operations. See [snapshot_hash_cache.md](snapshot_hash_cache.md) for details.

**Returns:** `DownloadResult` dataclass containing:

| Field | Type | Description |
|-------|------|-------------|
| `statistics` | `DownloadProgressMetadata` | Final progress metadata with download statistics |
| `manifest` | `AbsManifest` | A copy of the input manifest with mtime values updated to match the local filesystem |

The `statistics` field contains the same fields as the progress callback metadata, with final values:
- `total_file_chunks`: Total files + chunks to process
- `total_bytes`: Total bytes to download
- `downloaded_file_chunks`: Number of files/chunks successfully downloaded
- `downloaded_bytes`: Bytes successfully downloaded
- `skipped_file_chunks`: Number of files/chunks skipped (hash cache match or SKIP resolution)
- `skipped_bytes`: Bytes skipped
- `progress`: Final progress percentage (100.0)
- `progressMessage`: Summary message with download rate
- `total_time`: Total operation time in seconds
- `transfer_rate`: Final transfer rate in bytes/second (total_bytes / total_time)

Note: In the final statistics, `transfer_rate` is calculated as `total_bytes / total_time` for accuracy,
which may differ slightly from the sliding window rate shown during progress callbacks.

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
| Symlink | Create symlink pointing to `symlink_target` (topologically sorted) |
| Deleted file marker (diff) | Delete the file at the path if it exists |
| Deleted directory marker (diff) | Delete the directory only if empty (see below) |
| Directory | Create directory (with parents) if it doesn't exist |

**Symlink Handling:**

Only `PRESERVE` (default) and `EXCLUDE_ALL` are supported. For chained symlinks, targets are created before symlinks that point to them via topological sorting. See [snapshot_symlink_handling.md](snapshot_symlink_handling.md) for details.

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

