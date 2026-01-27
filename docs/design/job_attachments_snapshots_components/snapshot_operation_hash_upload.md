# HASH_UPLOAD Operation: `hash_upload_abs_manifest()`

*← [Back to Job Attachments Snapshots](../job_attachments_snapshots.md)*


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

    # Timing information
    total_time: float  # Elapsed time since operation start (seconds)
    transfer_rate: float  # Current transfer rate (bytes/second)
```

**Transfer Rate Calculation:**

The `transfer_rate` field uses a sliding window algorithm for smooth, responsive rate estimation:

- Maintains a deque of `(timestamp, uploaded_bytes)` snapshots
- Window size: 12 seconds (`TRANSFER_RATE_WINDOW_SECONDS`)
- Rate = `(current_bytes - oldest_bytes) / (current_time - oldest_time)`
- At operation start (< 12s elapsed), uses all available history from start to current time
- Updated on every part upload for granular progress (not just per-file/chunk completion)

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
    # Access timing information
    rate_mb_s = metadata.transfer_rate / (1024 * 1024)
    print(f"Progress: {metadata.progress:.1f}% - {rate_mb_s:.1f} MB/s - {metadata.progressMessage}")
    # Return False to cancel, True to continue
    return True

result = hash_upload_abs_manifest(
    manifest=abs_manifest,
    data_cache=s3_cache,
    on_progress=on_progress,
)

# Access final timing from statistics
print(f"Completed in {result.statistics.total_time:.2f}s")
print(f"Average rate: {result.statistics.transfer_rate / (1024 * 1024):.1f} MB/s")
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
| `total_time` | Total operation time in seconds |
| `transfer_rate` | Final transfer rate in bytes/second (total_bytes / total_time) |

Note: In the final statistics, `transfer_rate` is calculated as `total_bytes / total_time` for accuracy,
which may differ slightly from the sliding window rate shown during progress callbacks.

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

