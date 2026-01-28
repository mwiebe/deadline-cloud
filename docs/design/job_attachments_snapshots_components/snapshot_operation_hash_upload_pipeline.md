# HASH_UPLOAD Pipeline Architecture

[Job Attachments Snapshots](../job_attachments_snapshots.md) · [HASH_UPLOAD Operation](snapshot_operation_hash_upload.md) · Pipeline Architecture

This document describes the internal pipeline architecture, threading model, and memory management of the HASH_UPLOAD operation.

## Pipelined Architecture

The operation uses two thread pools connected by a bounded memory pool:

```
┌────────────────────────────────────────────────────────────────┐
│                    READ + HASH POOL                            │
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
┌────────────────────────────────────────────────────────────────┐
│                      UPLOAD POOL                               │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐                           │
│  │ Worker  │ │ Worker  │ │ Worker  │  (max_workers threads)    │
│  │  1      │ │  2      │ │  N      │                           │
│  └─────────┘ └─────────┘ └─────────┘                           │
└────────────────────────────────────────────────────────────────┘
```

**Key Design Points:**

1. **Two thread pools:** READ+HASH pool reads files and computes hashes; UPLOAD pool handles uploads
2. **Memory pool as bounded buffer:** READ+HASH allocates memory before reading, blocks when full; UPLOAD releases memory after completing
3. **Cache-specific pipelines:** `S3HashUploadPipeline` and `FileSystemHashUploadPipeline` implement cache-specific upload logic

## Memory Management

The pipeline constrains total memory usage across both stages:

- When `max_memory_bytes` is reached, the READ+HASH stage blocks until UPLOAD completes and frees memory
- Each chunk occupies memory from READ+HASH through UPLOAD completion

When chunking is enabled (positive `fileChunkSizeBytes`):
- `max_memory_bytes` must be >= `fileChunkSizeBytes` (raises `ValueError` otherwise)
- Large files are processed chunk by chunk through the pipeline

## Cache Check Architecture

All cache checks (hash cache, S3 check cache, and HeadObject fallback) are performed **inside the worker thread pool**, not on the main thread. This parallelizes HeadObject calls (~15ms each) and ensures items that will be skipped never consume memory pool resources.

```
┌─────────────────────────────────────────────────────────────────┐
│                     Worker Thread (per item)                    │
├─────────────────────────────────────────────────────────────────┤
│  1. Check hash cache (thread-local SQLite connection)           │
│     └─► If hit + mtime match, get cached_hash                   │
│                                                                 │
│  2. Check if object exists in data cache:                       │
│     a. S3 check cache lookup (thread-local SQLite)              │
│     b. If miss, HeadObject call to S3                           │
│     └─► If exists, mark as skipped (no memory allocation)       │
│                                                                 │
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

## Concurrent Upload Deduplication

When multiple files or chunks have identical content (same hash), the pipeline prevents redundant concurrent uploads. This is valuable for:
- Files with repeated chunks (e.g., sparse files)
- Multiple files with identical content in the same batch
- Large datasets with many duplicate files

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
└─────────────────────────────────────────────────────────────────────────┘
```

**Implementation:**

| Field | Description |
|-------|-------------|
| `_uploading_hashes` | `Dict[str, threading.Event]` mapping hash → completion event |
| `_uploading_hashes_lock` | Lock protecting the map for thread-safe access |

**Coordination flow:**

1. **First thread with hash:** Registers hash in map with a new `Event`, proceeds to upload
2. **Subsequent threads with same hash:** Find existing entry, wait on the `Event`
3. **Upload completion:** First thread signals `Event` and removes hash from map
4. **Waiting threads:** Wake up, mark item as skipped, release memory

This deduplication is separate from the S3 check cache (which prevents re-uploading content from previous operations). Concurrent deduplication handles duplicates within a single HASH_UPLOAD operation.

## Storage Key Format

Files are stored in content-addressable format:

| Data Cache Type | Key Format | Example |
|-----------------|------------|---------|
| `S3DataCache` | `{s3_key_prefix}/{hash}.{algorithm}` | `Data/a1b2c3d4...xxh128` |
| `FileSystemDataCache` | `{root_path}/{hash}.{algorithm}` | `/mnt/cache/a1b2c3d4...xxh128` |

For chunked files, each chunk is stored separately.

## Error Handling

- If upload fails, the operation raises an exception with details
- Partial uploads are not cleaned up (content-addressable storage is idempotent)
- The hash cache is updated even if upload fails (hash is still valid)

## Performance Comparison

| Approach | Disk Reads | Network Uploads | Memory Peak | S3 Parallelism |
|----------|------------|-----------------|-------------|----------------|
| HASH then upload | 2× | 1× | Low | Serial multipart |
| HASH_UPLOAD | 1× | 1× | Bounded | Parallel multipart |

For large datasets, HASH_UPLOAD provides:
- Up to 2× faster I/O due to single-pass disk reads
- Faster S3 uploads due to parallel multipart
- Better network utilization through concurrent part uploads

## Module Organization

| Module | Description |
|--------|-------------|
| `_hash_upload_abs_manifest.py` | Main entry point |
| `_hash_upload_abs_manifest_pipeline.py` | Base pipeline class, progress state, work items, memory pool |
| `_hash_upload_abs_manifest_s3_pipeline.py` | S3-specific upload logic |
| `_hash_upload_abs_manifest_file_system_pipeline.py` | FileSystem-specific upload logic |
