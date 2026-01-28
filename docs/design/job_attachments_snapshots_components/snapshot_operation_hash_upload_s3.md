# HASH_UPLOAD S3-Specific Behavior

*← [Back to HASH_UPLOAD Operation](snapshot_operation_hash_upload.md)*

This document describes S3-specific behavior of the HASH_UPLOAD operation, including multipart uploads, cache validation, and streaming file handling.

## Multipart Upload

Multipart upload is used when uploading to `S3DataCache` and the file/chunk size exceeds `2 × multipart_part_size` (default threshold: 64MB with 32MB parts).

### File Chunking and Multipart Behavior

| `fileChunkSizeBytes` | File Size | vs Multipart Threshold | Processing |
|---------------------|-----------|------------------------|------------|
| `DEFAULT_FILE_CHUNK_SIZE` (256MB) | ≤ file chunk size | < 2 × part size | Single PUT upload |
| `DEFAULT_FILE_CHUNK_SIZE` (256MB) | ≤ file chunk size | ≥ 2 × part size | Multipart upload |
| `DEFAULT_FILE_CHUNK_SIZE` (256MB) | > file chunk size | Any | Per file chunk, each ≥ 2 × part size uses multipart |
| `WHOLE_FILE_CHUNK_SIZE` (-1) | ≤ `max_memory_bytes` | < 2 × part size | Single PUT upload |
| `WHOLE_FILE_CHUNK_SIZE` (-1) | ≤ `max_memory_bytes` | ≥ 2 × part size | Parallel multipart upload |
| `WHOLE_FILE_CHUNK_SIZE` (-1) | > `max_memory_bytes` | Any | Two-pass streaming (see below) |

### Multipart Coordination

For files using multipart upload, the system uses two coordination structures:

**`_MultipartUploadState`** - Tracks the overall upload:

| Field | Description |
|-------|-------------|
| `file_hash` | Final hash of the complete file |
| `s3_key` | S3 object key for the upload |
| `upload_id` | S3 multipart upload ID |
| `parts_remaining` | Atomic counter of parts still uploading |
| `completed_parts` | List of `{"PartNumber", "ETag"}` for completion |
| `part_hashes` | Per-part hashes for verification (streaming only) |
| `part_errors` | Errors from failed part uploads |
| `lock` | Thread lock for safe concurrent updates |

**`_MultipartPartWorkItem`** - Represents a single part:

| Field | Description |
|-------|-------------|
| `multipart_state` | Reference to parent state |
| `part_number` | 1-based part number |
| `data` | Part content bytes |
| `expected_hash` | Hash for verification (streaming only) |

**Coordination flow:**

1. Create `_MultipartUploadState` with `CreateMultipartUpload`
2. Submit `_MultipartPartWorkItem` for each part to upload pool
3. Each part uploads independently; on success, append to `completed_parts` and decrement `parts_remaining`
4. Thread that decrements to 0 calls `CompleteMultipartUpload`
5. On any failure, record error and call `AbortMultipartUpload`

## Streaming Files (Larger Than Memory)

When file chunking is disabled (`WHOLE_FILE_CHUNK_SIZE`) and a file exceeds `max_memory_bytes`, a two-pass approach is used:

```
┌─────────────────────────────────────────────────────────────────┐
│  Pass 1: Compute Hash (discard data)                            │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  Read part 1 → hash (full + part) → discard             │    │
│  │  Read part 2 → hash (full + part) → discard             │    │
│  │  ...                                                    │    │
│  │  Read part N → hash (full + part) → discard             │    │
│  │  Result: final_hash + per-part hashes                   │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                 │
│  HeadObject check with final_hash                               │
│  └─► If exists, skip upload (done)                              │
│                                                                 │
│  Pass 2: Read, Verify, and Upload Parts (memory throttled)      │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  Create S3 multipart upload session                     │    │
│  │  For each part:                                         │    │
│  │    1. Allocate memory (blocks if pool exhausted)        │    │
│  │    2. Read part data into buffer                        │    │
│  │    3. Compute part hash, verify against pass 1          │    │
│  │       └─► If mismatch: AbortMultipartUpload, raise error│    │
│  │    4. Submit part to UPLOAD pool                        │    │
│  │    5. Continue to next part (don't wait)                │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

**Why two passes?**

1. S3 object keys are based on content hash—must know hash before creating multipart session
2. Discarding data in pass 1 avoids buffering the entire file
3. If object already exists (HeadObject hit), skip pass 2 entirely

**Hash verification:** During pass 1, per-part hashes are computed alongside the full file hash. In pass 2, each part re-computes its hash and verifies it matches the pass 1 value before uploading. If any part's hash differs (file modified between passes), the multipart upload is aborted and a `ValueError` is raised, ensuring corrupted data is never stored.

**Memory throttling:** Each part allocates from the memory pool before reading. If exhausted, the READ+HASH thread blocks until UPLOAD threads free memory, creating backpressure.

**Example:** 1GB file with 128MB max_memory and 32MB parts:
1. Pass 1: Stream through 1GB computing full hash + 32 part hashes (no allocation)
2. HeadObject check
3. Pass 2: Submit 32 parts; each verifies its hash before upload; at most 4 buffered at once (128MB / 32MB)

## Probabilistic S3 Cache Validation

The S3 check cache can become stale if objects are deleted (lifecycle policies, manual deletion, bucket recreation). HASH_UPLOAD performs inline validation during upload:

```
┌─────────────────────────────────────────────────────────────────┐
│  For each item where S3 check cache says "exists":              │
│                                                                 │
│  Item 1-100:     Always verify with HeadObject                  │
│  Item 101+:      1% random sampling                             │
│                                                                 │
│  If ANY verification fails:                                     │
│  1. Mark cache as invalid                                       │
│  2. Delete S3 check cache database                              │
│  3. Re-queue all previously skipped items                       │
│  4. Continue without cache                                      │
└─────────────────────────────────────────────────────────────────┘
```

### Sampling Strategy

| Item Number | Verification | Rationale |
|-------------|--------------|-----------|
| 1-100 | Always HeadObject | Catch stale cache early |
| 101+ | 1% random sample | Balance coverage vs. performance |

### Expected HeadObject Overhead (Warm Cache)

| Total Items | Verified | Time (8 workers) |
|-------------|----------|------------------|
| 100 | 100 | ~0.2s |
| 1,000 | 109 | ~0.2s |
| 10,000 | 199 | ~0.4s |
| 100,000 | 1,099 | ~2.1s |

### Recovery Flow

When validation fails:

1. **Detect:** HeadObject returns 404 for cached item
2. **Invalidate:** Close and delete S3 check cache database
3. **Re-queue:** Collect all items skipped due to cache
4. **Retry:** Re-submit skipped items (now use HeadObject directly)
5. **Continue:** Process remaining items without cache

### Implementation

```python
@dataclass
class _S3CacheValidationState:
    cache_hit_count: int = 0
    cache_invalidated: bool = False
    skipped_items: List[PipelineWorkItem] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def should_verify(self) -> bool:
        with self.lock:
            self.cache_hit_count += 1
            if self.cache_hit_count <= 100:
                return True
            return random.random() < 0.01
```

**Benefits:**
- Early detection of completely stale caches
- Minimal overhead for large uploads
- Self-healing without user intervention
- No data loss (skipped items are re-uploaded if missing)

## FileSystem Data Cache

For `FileSystemDataCache`, behavior is simpler:
- No multipart uploads
- Existence check via filesystem `exists()`
- Streaming files use two-pass: hash first, then stream copy while re-verifying hash
