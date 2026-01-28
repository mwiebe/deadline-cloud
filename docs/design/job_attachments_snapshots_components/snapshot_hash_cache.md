# Hash Cache

[Job Attachments Snapshots](../job_attachments_snapshots.md) · Hash Cache

The hash cache is a local SQLite database that stores file hashes keyed by path, modification time, and byte range. It enables operations to skip re-hashing or re-downloading unchanged files, significantly improving performance for repeated operations on the same files.

## Overview

| Property | Value |
|----------|-------|
| Location | `~/.deadline/job_attachments/hash_cache.db` |
| Format | SQLite database with WAL journaling |
| Thread safety | Thread-safe via locking and thread-local connections |

The hash cache is shared across HASH, HASH_UPLOAD, and DOWNLOAD operations. Files hashed during upload are available for skip detection during download, and vice versa.

## Cache Entry Structure

Each entry in the hash cache contains:

| Field | Type | Description |
|-------|------|-------------|
| `file_path` | blob | Absolute file path (UTF-8 encoded) |
| `hash_algorithm` | text | Hash algorithm (e.g., `xxh128`) |
| `range_start` | integer | Start byte offset (0 for whole-file) |
| `range_end` | integer | End byte offset (-1 for whole-file) |
| `file_hash` | text | The computed hash value |
| `last_modified_time` | timestamp | File mtime when hash was computed |

The primary key is `(file_path, hash_algorithm, range_start, range_end)`.

## Whole-File vs Byte-Range Hashes

The cache supports two types of entries:

| Entry Type | `range_start` | `range_end` | Description |
|------------|---------------|-------------|-------------|
| Whole-file | 0 | -1 | Hash of entire file |
| Byte-range | ≥ 0 | > 0 | Hash of bytes in range [start, end) |

Byte-range support enables efficient caching for file chunked files. Different file chunk sizes can coexist in the cache—entries are keyed by their exact byte range.

## Cache Lookup Behavior

When looking up a hash:

1. Query by `(file_path, hash_algorithm, range_start, range_end)`
2. If entry exists, compare `last_modified_time` with current file mtime
3. If mtime matches, return cached hash (cache hit)
4. If mtime differs or entry missing, return None (cache miss)

**Path resolution:** Paths are resolved via `Path.resolve()` before lookup, which resolves symlinks and normalizes `.` and `..` components. This ensures different paths to the same physical file share cache entries.

## Usage by Operation

### HASH Operation

| Scenario | Behavior |
|----------|----------|
| `hash_cache` provided, `force_rehash=False` | Check cache; use cached hash on hit |
| `hash_cache` provided, `force_rehash=True` | Always compute hash, update cache |
| `hash_cache` is None | Always compute hash, no caching |

On cache miss, the computed hash is stored in the cache for future lookups.

### HASH_UPLOAD Operation

The hash cache works together with the S3 check cache:

1. **Hash cache hit + S3 check cache hit:** Skip entirely (no read, no hash, no upload)
2. **Hash cache hit + S3 miss:** Must re-read and re-hash the file (see below)
3. **Hash cache miss:** Read file, compute hash, upload if needed, update cache

**Why re-hash on S3 miss?**

When the hash cache hits but the object doesn't exist in S3, we cannot trust the cached hash for upload because:
- The hash cache could be stale (file changed but mtime check passed due to clock skew)
- The hash cache could be corrupted
- We need the actual file data to upload anyway

The hash cache is only trusted for **skipping** when the data already exists in S3 (verified by HeadObject). For uploads, we always verify by reading and hashing the actual file content.

### DOWNLOAD Operation

The hash cache enables skipping downloads for files that already have correct content:

1. Check if local file exists at target path
2. If exists, look up `(path, mtime)` in hash cache
3. If cached hash matches manifest hash, skip download

For file chunked files, all file chunk hashes must match:
1. For each file chunk, look up `(path, mtime, range_start, range_end)` in hash cache
2. If any file chunk's cached hash is missing or doesn't match, download the entire file
3. If all file chunks match, skip the download

After downloading a file chunked file, all file chunk hashes are stored in the cache for future skip detection.

This is useful for:
- **Repeated downloads:** Downloading the same manifest twice skips all files the second time
- **Incremental updates:** Only changed files are downloaded
- **Resume after interruption:** Successfully downloaded files are skipped

## Thread Safety

The hash cache uses multiple mechanisms for thread safety:

| Mechanism | Purpose |
|-----------|---------|
| `db_lock` | Protects writes to the database |
| Thread-local connections | Each thread gets its own SQLite connection |
| WAL journaling | Allows concurrent reads during writes |

Worker threads in HASH_UPLOAD use `get_local_connection()` to obtain thread-local connections, enabling parallel cache lookups without contention.

## Performance Impact

The hash cache provides significant performance benefits:

| Scenario | Without Cache | With Cache |
|----------|---------------|------------|
| Re-hash 10,000 files (100GB) | ~5 minutes | < 1 second |
| Upload unchanged files | Hash + HeadObject per file | Skip entirely |
| Download unchanged files | Download all | Skip matching |

The cache is especially valuable for:
- Iterative workflows where files change incrementally
- Large projects with many unchanged files between submissions
- Repeated job submissions with the same input files

## Custom Cache Location

By default, the cache is stored at `~/.deadline/job_attachments/hash_cache.db`. To use a custom location:

```python
from deadline.job_attachments.caches.hash_cache import HashCache

with HashCache(cache_dir="/custom/path") as cache:
    result = hash_abs_manifest(manifest, hash_cache=cache)
```

## Cache Maintenance

The hash cache grows over time as new files are hashed. There is no automatic cleanup—entries persist until manually removed.

To clear the cache:
```python
import os
os.remove(os.path.expanduser("~/.deadline/job_attachments/hash_cache.db"))
```

Or delete the file directly from the filesystem.

## Relationship to S3 Check Cache

The hash cache and S3 check cache serve different purposes:

| Cache | Purpose | Key |
|-------|---------|-----|
| Hash cache | Skip re-hashing unchanged local files | (path, mtime, byte range) |
| S3 check cache | Skip HeadObject calls for known-existing S3 objects | (bucket, key prefix, hash) |

Both caches work together in HASH_UPLOAD to minimize redundant work. See [snapshot_data_cache_classes.md](snapshot_data_cache_classes.md) for S3 check cache details.
