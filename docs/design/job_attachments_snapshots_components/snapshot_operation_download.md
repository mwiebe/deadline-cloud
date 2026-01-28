# DOWNLOAD Operation: `download_abs_manifest()`

*← [Back to Job Attachments Snapshots](../job_attachments_snapshots.md)*

**Location:** `_download_abs_manifest.py`

Downloads files from a data cache (S3 or filesystem) to the local filesystem. For snapshot manifests (`AbsSnapshot`), recreates the directory structure. For diff manifests (`AbsSnapshotDiff`), applies changes by downloading new/modified files and deleting removed files.

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

## Parameters

| Parameter | Description |
|-----------|-------------|
| `manifest` | Manifest with absolute paths and hashes. Can be `AbsSnapshot` or `AbsSnapshotDiff`. |
| `data_cache` | Data cache to download from (`S3DataCache` or `FileSystemDataCache`) |
| `hash_cache` | Optional hash cache to skip downloads for unchanged files. See [snapshot_hash_cache.md](snapshot_hash_cache.md). |
| `file_conflict_resolution` | How to handle existing files (see below). Default `OVERWRITE`. |
| `apply_deletes` | If `True` (default), apply deletions from diff manifests. |
| `symlink_policy` | How to handle symlinks. Only `PRESERVE` (default) and `EXCLUDE_ALL` supported. |
| `max_workers` | Maximum parallel download workers. Default: 10. |
| `on_progress` | Optional callback for progress reporting (see below). |

## Returns

`DownloadResult` containing:
- `statistics`: `DownloadProgressMetadata` with download metrics
- `manifest`: A copy of the input manifest with `mtime` values updated to match local filesystem

## Raises

- `ValueError` if the manifest contains relative paths
- `AssetSyncCancelledError` if cancelled via progress callback

## Progress Reporting

The `on_progress` callback receives `DownloadProgressMetadata`:

```python
@dataclass
class DownloadProgressMetadata:
    # Totals
    total_file_chunks: int
    total_bytes: int

    # Download progress
    downloaded_file_chunks: int
    downloaded_bytes: int
    skipped_file_chunks: int  # Hash cache hit or conflict resolution
    skipped_bytes: int

    # Overall
    progress: float  # 0-100
    progressMessage: str
    total_time: float  # seconds
    transfer_rate: float  # bytes/second
```

| Behavior | Description |
|----------|-------------|
| Invocation interval | At most every 0.2 seconds (5 times per second) |
| Final callback | Always called at operation completion |
| Cancellation | Return `False` from callback to cancel |
| Thread safety | Callback invoked from worker threads; metadata built under lock |

The `transfer_rate` uses a 12-second sliding window for smooth estimation.

## File Conflict Resolution

| Resolution | Behavior |
|------------|----------|
| `SKIP` | Skip download if file already exists |
| `OVERWRITE` | Overwrite existing file (default) |
| `CREATE_COPY` | Create new file with suffix (e.g., `file (1).ext`) |

Note: When `hash_cache` is provided, files with matching hashes are skipped regardless of this setting.

## Entry Type Handling

| Entry Type | Action |
|------------|--------|
| Regular file | Download using hash as key |
| Large file (chunkhashes) | Download each chunk, concatenate |
| Symlink | Create symlink (topologically sorted) |
| Deleted file marker (diff) | Delete file if exists |
| Deleted directory marker (diff) | Delete directory if empty |
| Directory | Create directory with parents |

## Why Return an Updated Manifest?

The returned manifest has `mtime` values updated to match actual filesystem timestamps. This is essential for cross-platform workflows:

- File system mtime precision varies (Linux: nanosecond, Windows: 100ns, macOS HFS+: 1 second)
- Without this, subsequent DIFF operations would incorrectly detect files as "modified"

```python
# Download and use updated manifest as baseline for future diffs
result = download_abs_manifest(manifest=cloud_manifest, data_cache=s3_cache)
local_baseline = result.manifest  # Use this for subsequent DIFF operations
```

## Basic Example

```python
import boto3
from deadline.job_attachments._snapshots import (
    download_abs_manifest,
    join_manifest,
    S3DataCache,
)
from deadline.job_attachments.asset_manifests.decode import decode_manifest

# Load manifest and convert to absolute paths
with open("scene.manifest") as f:
    rel_manifest = decode_manifest(f.read())
abs_manifest = join_manifest(rel_manifest, "/home/user/projects/scene")

# Download
data_cache = S3DataCache(
    s3_bucket="my-bucket",
    s3_key_prefix="Data",
    s3_client=boto3.client("s3"),
)
result = download_abs_manifest(manifest=abs_manifest, data_cache=data_cache)

print(f"Downloaded {result.statistics.downloaded_bytes} bytes")
```

## Downloading from Local Cache

For debug snapshots, use `FileSystemDataCache`:

```python
from pathlib import Path
from deadline.job_attachments._snapshots import FileSystemDataCache

data_cache = FileSystemDataCache(root_path=Path("/tmp/debug_snapshot/data"))
result = download_abs_manifest(manifest=abs_manifest, data_cache=data_cache)
```

## Applying a Diff Manifest

```python
# Load diff manifest and apply changes
with open("changes.diff.manifest") as f:
    rel_diff = decode_manifest(f.read())
abs_diff = join_manifest(rel_diff, "/home/user/projects/scene")

# Downloads new/modified files, deletes removed files
result = download_abs_manifest(manifest=abs_diff, data_cache=data_cache)
```

## When to Use DOWNLOAD

| Use Case | Recommended Approach |
|----------|---------------------|
| Worker input sync | DOWNLOAD with S3DataCache |
| Job output download | DOWNLOAD with S3DataCache |
| Restore from debug snapshot | DOWNLOAD with FileSystemDataCache |
| Apply incremental update | DOWNLOAD with AbsSnapshotDiff |

## Relationship to HASH_UPLOAD

DOWNLOAD is the inverse of HASH_UPLOAD:

| Operation | Direction | Input | Output |
|-----------|-----------|-------|--------|
| HASH_UPLOAD | Local → Cache | AbsManifest (no hashes) | UploadResult |
| DOWNLOAD | Cache → Local | AbsManifest (with hashes) | DownloadResult |

## Related Documentation

- [Pipeline Architecture](snapshot_operation_download_pipeline.md) - Threading, atomicity, chunked files, S3 multi-part
- [Hash Cache](snapshot_hash_cache.md) - Skip optimization for unchanged files
- [Data Cache Classes](snapshot_data_cache_classes.md) - S3DataCache and FileSystemDataCache
- [Symlink Handling](snapshot_symlink_handling.md) - Symlink policies
