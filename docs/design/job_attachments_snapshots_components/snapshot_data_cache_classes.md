# Content-Addressed Data Cache Classes

*← [Back to Job Attachments Snapshots](../job_attachments_snapshots.md)*

**Location:** `_content_addressed_data_cache.py`

This document describes the content-addressed data cache abstraction used by the HASH_UPLOAD and DOWNLOAD operations.

## Overview

A content-addressed data cache stores data using the content's hash as the key. This enables:
- **Deduplication**: Identical content is stored only once
- **Efficient retrieval**: Content can be fetched by its hash without knowing the original filename
- **Integrity verification**: The hash serves as both the key and a checksum

The library provides an abstract base class and two concrete implementations:

```
ContentAddressedDataCache (abstract)
├── S3DataCache         - Amazon S3 storage
└── FileSystemDataCache - Local or network filesystem
```

## Design Principles

### Content-Addressable Storage

Files are stored with keys derived from their content hash:
```
{prefix}/{hash}.{algorithm}
```

Examples:
- S3: `Data/a1b2c3d4e5f67890abcdef1234567890.xxh128`
- Filesystem: `/mnt/cache/a1b2c3d4e5f67890abcdef1234567890.xxh128`

This design means:
- The same content always maps to the same key (deterministic)
- Different content always maps to different keys (collision-resistant)
- No metadata about original filenames is stored in the cache

### Hash-Then-Upload Invariant

The HASH_UPLOAD operation always computes the hash while reading the data for upload. It never uploads based on a pre-computed hash alone. This guarantees the content-addressed storage invariant: data stored for a hash key always equals its hash.

If we did a two-pass approach (hash first, upload later), concurrent processes could modify files between passes, causing content mismatches. This is true even with file locking, since filesystems may not support locking or may have it disabled for performance.

**Large file exception:** When a file is too large to fit in memory (larger than `max_memory_bytes` with `WHOLE_FILE_CHUNK_SIZE` mode), we must use two passes: first to compute the hash, then to upload. In this case, we re-compute per-part hashes during the upload pass and verify they match the original. If any part's hash differs, the upload is aborted and a `ValueError` is raised, ensuring corrupted data is never stored.

### Existence Checking Strategy

Before uploading, the operation checks if content already exists in the cache. The checking strategy differs by cache type:

**S3DataCache:**
1. Check local `S3CheckCache` (SQLite database of known-existing hashes)
2. If miss, make `HeadObject` API call to S3
3. Cache positive results for future checks

**Stale cache detection:** The S3CheckCache can become stale if objects are deleted from S3 (e.g., lifecycle policies, manual deletion).
To detect this during upload without excessive performance impact:
- First 100 cache hits: Always verify with `HeadObject`
- Remaining cache hits: 1% random sampling with `HeadObject`

If any verification fails, the cache is invalidated, deleted, and all previously-skipped items are re-queued for upload.

**FileSystemDataCache:**
- Direct filesystem `exists()` check (no caching)

## Abstract Interface

```python
@dataclass
class ContentAddressedDataCache(ABC):
    @abstractmethod
    def get_object_key(self, hash_value: str, algorithm: str) -> str:
        """Returns the storage key/path for a given hash."""
        ...

    @abstractmethod
    def object_exists(self, hash_value: str, algorithm: str) -> bool:
        """Checks if an object with the given hash already exists."""
        ...
```

## S3DataCache

Content-addressed storage backed by Amazon S3.

### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `s3_bucket` | `str` | (required) | S3 bucket name |
| `s3_key_prefix` | `str` | (required) | Key prefix (e.g., `"Data"`) |
| `s3_client` | boto3 client | (required) | S3 client with GetObject, PutObject, HeadObject permissions |
| `s3_check_cache` | `Optional[S3CheckCache]` | `None` | Local cache to avoid redundant HeadObject calls |
| `multipart_part_size` | `int` | 32 MB | Part size for multipart uploads/downloads |
| `force_s3_check` | `bool` | `False` | Bypass s3_check_cache, always call HeadObject |
| `account_id` | `Any` | `None` | AWS account ID for ExpectedBucketOwner |

### Account ID and Security

The `account_id` field controls the `ExpectedBucketOwner` parameter on S3 API calls, which prevents confused deputy attacks where a malicious actor tricks your code into accessing a bucket they control.

| Value | Behavior |
|-------|----------|
| `None` (default) | Auto-detect from AWS credentials at construction |
| `NO_ACCOUNT_ID_CHECK` | Disable ExpectedBucketOwner checks |
| String | Use the provided account ID |

Auto-detection queries STS to get the account ID from the current credentials. If this fails (e.g., no credentials configured), construction raises `ValueError`.

### S3 Check Cache

The `s3_check_cache` is a local SQLite database that tracks which hashes are known to exist in S3. This avoids redundant `HeadObject` calls, which cost ~15ms each.

**Performance impact:**
- Without cache: 2000 files × 15ms = 30 seconds of HeadObject calls
- With cache: Near-instant for previously-seen hashes

**Cache invalidation:**
The HASH_UPLOAD operation performs probabilistic validation to detect stale caches (see [snapshot_operation_hash_upload.md](snapshot_operation_hash_upload.md) for details). If validation fails, the cache is deleted and rebuilt.

### Multipart Transfers

The `multipart_part_size` field controls the part size for both uploads and downloads:

**Uploads:** Files larger than `2 × multipart_part_size` (default 64MB) use S3 multipart upload with parallel part uploads.

**Downloads:** Files larger than `2 × multipart_part_size` use parallel byte-range requests for improved throughput.

### Methods

| Method | Description |
|--------|-------------|
| `get_object_key(hash, alg)` | Returns S3 key: `{prefix}/{hash}.{alg}` |
| `get_cache_key(hash, alg)` | Returns cache key: `{bucket}/{prefix}/{hash}.{alg}` |
| `get_check_cache_entry(hash, alg)` | Check S3CheckCache without HeadObject |
| `head_object_exists(hash, alg)` | Check S3 via HeadObject (bypasses cache) |
| `object_exists(hash, alg)` | Check cache first, then HeadObject |

## FileSystemDataCache

Content-addressed storage backed by a local or network filesystem.

### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `root_path` | `Path` | (required) | Absolute path to cache root directory |

### Validation

The `root_path` must be absolute. Relative paths raise `ValueError` at construction.

### Methods

| Method | Description |
|--------|-------------|
| `get_object_key(hash, alg)` | Returns file path: `{root}/{hash}.{alg}` |
| `object_exists(hash, alg)` | Checks if file exists on filesystem |

### Use Cases

- **Debug snapshots**: Create portable zip files with manifest + data for reproduction
- **Local testing**: Test upload/download logic without S3
- **Network storage**: Use NFS or SMB mounted paths as shared caches

## Usage Examples

### S3DataCache

```python
import boto3
from deadline.job_attachments._snapshots import (
    S3DataCache,
    hash_upload_abs_manifest,
)
from deadline.job_attachments.caches.s3_check_cache import S3CheckCache

# Create S3 data cache with check cache for efficiency
s3_client = boto3.client("s3")
s3_check_cache = S3CheckCache()  # Uses default location

data_cache = S3DataCache(
    s3_bucket="my-render-farm-bucket",
    s3_key_prefix="Data",
    s3_client=s3_client,
    s3_check_cache=s3_check_cache,
)

# Upload files
result = hash_upload_abs_manifest(
    manifest=collected_manifest,
    data_cache=data_cache,
)
```

### FileSystemDataCache

```python
from pathlib import Path
from deadline.job_attachments._snapshots import (
    FileSystemDataCache,
    hash_upload_abs_manifest,
)

# Create filesystem data cache for debug snapshot
data_cache = FileSystemDataCache(
    root_path=Path("/tmp/debug_snapshot/Data"),
)

# Upload files to local filesystem
result = hash_upload_abs_manifest(
    manifest=collected_manifest,
    data_cache=data_cache,
)

# Result: files stored as /tmp/debug_snapshot/Data/{hash}.xxh128
```

### Switching Between Caches

The same manifest can be uploaded to different cache types:

```python
# Upload to S3 for production
s3_result = hash_upload_abs_manifest(manifest, s3_cache)

# Upload to filesystem for debugging
fs_result = hash_upload_abs_manifest(manifest, fs_cache)

# Both produce identical manifests (same hashes)
assert s3_result.manifest.files == fs_result.manifest.files
```

## Integration with Operations

### HASH_UPLOAD

The data cache determines:
- Where content is stored
- How existence checks are performed
- Whether multipart upload is used (S3 only)
- Part size for large file transfers

See [snapshot_operation_hash_upload.md](snapshot_operation_hash_upload.md) for details.

### DOWNLOAD

The data cache determines:
- Where content is retrieved from
- Whether parallel byte-range downloads are used (S3 only)
- Part size for large file transfers

See [snapshot_operation_download.md](snapshot_operation_download.md) for details.

## Cache Hierarchy

The snapshots library uses multiple caches at different levels:

| Cache | Scope | Purpose |
|-------|-------|---------|
| `HashCache` | Local filesystem | Maps (path, mtime, byte range) → hash to skip re-hashing unchanged files or file chunks |
| `S3CheckCache` | Per S3 bucket | Tracks which hashes exist in S3 to skip HeadObject calls |
| `ContentAddressedDataCache` | Remote storage | The actual content storage (S3 or filesystem) |

**Upload flow with all caches:**
1. `HashCache` hit → skip reading file, use cached hash
2. `S3CheckCache` hit → skip HeadObject, assume exists
3. `HeadObject` confirms existence → skip upload
4. Otherwise → read, hash, upload

**Download flow with hash cache:**
1. Local file exists with matching mtime in `HashCache`
2. Cached hash matches manifest hash → skip download
3. Otherwise → download from data cache
