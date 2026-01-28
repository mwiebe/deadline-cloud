# HASH_UPLOAD Operation: `hash_upload_abs_manifest()`

*← [Back to Job Attachments Snapshots](../job_attachments_snapshots.md)*

**Location:** `_hash_upload_abs_manifest.py`

Fills in hashes for a manifest AND uploads file content to a data cache in a single pipelined pass. This avoids reading files twice (once for hashing, once for uploading).

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

## Parameters

| Parameter | Description |
|-----------|-------------|
| `manifest` | Manifest with absolute paths and `hash=None` for unhashed files. Can be either a snapshot (from `collect_abs_snapshot`) or a diff (from `diff_snapshots` with `ignore_hashes=True`) |
| `data_cache` | Content-addressable data cache destination. Either `S3DataCache` for cloud storage or `FileSystemDataCache` for local/network storage. |
| `hash_cache` | Optional hash cache for efficiency. See [snapshot_hash_cache.md](snapshot_hash_cache.md). |
| `force_rehash` | If `True`, ignore cache and recalculate all hashes |
| `max_memory_bytes` | Maximum memory to use for buffering (default: auto-detect, see below) |
| `max_workers` | Maximum number of parallel workers (default: 10) |
| `file_chunk_size_bytes` | File chunk size for output manifest. `None` = preserve from input. `WHOLE_FILE_CHUNK_SIZE` (-1) = no file chunking. |
| `on_progress` | Optional callback for progress reporting (see below) |

## Returns

`UploadResult` containing:
- `statistics`: `HashUploadProgressMetadata` with detailed hash/upload metrics
- `manifest`: A NEW `AbsManifest` with all hashes filled in

## Raises

- `ValueError` if the manifest contains relative paths
- `ValueError` if any regular file entry already has a hash (is not unhashed)

## Progress Reporting

The `on_progress` callback receives `HashUploadProgressMetadata`:

```python
@dataclass
class HashUploadProgressMetadata:
    # Totals
    total_file_chunks: int
    total_bytes: int

    # Hashing phase
    hashed_file_chunks: int
    hashed_bytes: int
    hash_skipped_file_chunks: int  # Skipped due to hash cache hit
    hash_skipped_bytes: int

    # Upload phase
    uploaded_file_chunks: int
    uploaded_bytes: int
    upload_skipped_file_chunks: int  # Already in data cache
    upload_skipped_bytes: int

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

The `transfer_rate` uses a 12-second sliding window for smooth, responsive estimation.

## Entry Type Handling

| Entry Type | Action |
|------------|--------|
| Regular file | Read+Hash → Upload (single pass) |
| Large file (chunking enabled) | Read+Hash → Upload (per chunk) |
| Large file (no chunking, > memory) | Two-pass: hash first, then verify hash+upload |
| Symlink | Pass through unchanged (no upload) |
| Deleted marker | Pass through unchanged (no upload) |
| Directory | Pass through unchanged (no upload) |

## Default Memory Limit

When `max_memory_bytes` is not specified:

```python
default_limit = min(16GB, max(256MB, total_memory // 4, available_memory - 1GB))
```

| System | Total | Available | Result |
|--------|-------|-----------|--------|
| Low memory | 4GB | 2GB | 1GB |
| Typical workstation | 32GB | 20GB | 16GB |
| High memory server | 128GB | 100GB | 16GB |

## Cache Integration

| Cache | Purpose |
|-------|---------|
| `hash_cache` | Skip hashing for files with unchanged mtime |
| `s3_check_cache` | Skip upload for files already in S3 (S3DataCache only) |

When both caches hit, the file is completely skipped (no read, no hash, no upload).

See [snapshot_hash_cache.md](snapshot_hash_cache.md) for hash cache details.

## Basic Example

```python
import boto3
from deadline.job_attachments._snapshots import (
    collect_abs_snapshot,
    hash_upload_abs_manifest,
    S3DataCache,
)
from deadline.job_attachments.caches.hash_cache import HashCache

# Collect directory tree
abs_manifest = collect_abs_snapshot(["/projects/my_scene"], [])

# Hash and upload
with HashCache() as hash_cache:
    data_cache = S3DataCache(
        s3_bucket="my-bucket",
        s3_key_prefix="Data",
        s3_client=boto3.client("s3"),
    )
    
    result = hash_upload_abs_manifest(
        manifest=abs_manifest,
        data_cache=data_cache,
        hash_cache=hash_cache,
    )

print(f"Uploaded {result.statistics.uploaded_bytes} bytes")
```

## Writing to Local Filesystem

For debug snapshots, use `FileSystemDataCache`:

```python
from pathlib import Path
from deadline.job_attachments._snapshots import FileSystemDataCache

data_cache = FileSystemDataCache(root_path=Path("/tmp/debug_snapshot/data"))
result = hash_upload_abs_manifest(manifest=abs_manifest, data_cache=data_cache)
# Files stored as /tmp/debug_snapshot/data/{hash}.xxh128
```

## When to Use HASH vs HASH_UPLOAD

| Use Case | Recommended |
|----------|-------------|
| Local manifest creation (no upload) | HASH |
| Diff computation only | HASH |
| Job submission with upload | HASH_UPLOAD |
| Output sync from worker | HASH_UPLOAD |

## Related Documentation

- [Pipeline Architecture](snapshot_operation_hash_upload_pipeline.md) - Threading model, memory management, deduplication
- [S3-Specific Behavior](snapshot_operation_hash_upload_s3.md) - Multipart uploads, cache validation, streaming files
- [Hash Cache](snapshot_hash_cache.md) - Local hash caching
- [Data Cache Classes](snapshot_data_cache_classes.md) - S3DataCache and FileSystemDataCache
