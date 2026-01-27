# HASH Operation: `hash_abs_manifest()`

*← [Back to Job Attachments Snapshots](../job_attachments_snapshots.md)*


**Location:** `_hash_abs_manifest.py`

Fills in hashes for a manifest that was created by `collect_abs_snapshot()` or `diff_snapshots()`. The input manifest must have absolute paths.

```python
def hash_abs_manifest(
    manifest: AbsManifest,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    file_chunk_size_bytes: Optional[int] = None,
    on_progress: Optional[HashProgressCallback] = None,
) -> HashResult:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | Manifest with absolute paths and `hash=None` for unhashed files. Can be either a snapshot (from `collect_abs_snapshot`) or a diff (from `diff_snapshots` with `ignore_hashes=True`) |
| `hash_cache` | Optional hash cache for efficiency |
| `force_rehash` | If `True`, ignore cache and recalculate all hashes |
| `file_chunk_size_bytes` | Chunk size for output manifest. `None` = preserve from input manifest. `WHOLE_FILE_CHUNK_SIZE` (-1) = no chunking. Positive int = chunk size in bytes. |
| `on_progress` | Optional callback for progress reporting. Called periodically with `HashProgressMetadata`. Return `True` to continue, `False` to cancel. |

## Progress Reporting

The `on_progress` callback receives `HashProgressMetadata` with tracking for the hashing phase:

```python
@dataclass
class HashProgressMetadata:
    # Totals
    total_file_chunks: int  # Total files + chunks to process
    total_bytes: int

    # Hashing phase progress
    hashed_file_chunks: int
    hashed_bytes: int
    skipped_file_chunks: int  # Skipped due to hash cache hit
    skipped_bytes: int

    # Overall progress
    progress: float  # 0-100
    progressMessage: str

    # Timing information
    total_time: float  # Elapsed time since operation start (seconds)
    rate: float  # Current hashing rate (bytes/second)
```

### Rate Calculation

The `rate` field uses a sliding window algorithm for smooth, responsive rate estimation:

- Maintains a deque of `(timestamp, hashed_bytes)` snapshots
- Window size: 12 seconds (`RATE_WINDOW_SECONDS`)
- Rate = `(current_bytes - oldest_bytes) / (current_time - oldest_time)`
- At operation start (< 12s elapsed), uses all available history from start to current time

This approach provides:
- Smooth rate display that doesn't jump erratically
- Quick response to throughput changes (12s window)
- Accurate rates even during bursty I/O

The callback type is:
```python
HashProgressCallback = Callable[[HashProgressMetadata], bool]
```

### Progress Callback Behavior

| Behavior | Description |
|----------|-------------|
| Invocation interval | Called at most every 0.2 seconds (5 times per second) |
| Final callback | Always called at operation completion via `force_callback()` |
| Cancellation | Return `False` from callback to cancel the operation |
| Thread safety | Callback is invoked from the main thread (hashing is single-threaded) |

### Progress Field Semantics

For chunked files, each chunk is counted separately in `total_file_chunks`, `hashed_file_chunks`, etc.

| Field | When Incremented |
|-------|------------------|
| `hashed_bytes` / `hashed_file_chunks` | After hash computation completes for a file or chunk |
| `skipped_bytes` / `skipped_file_chunks` | When hash cache hit allows skipping hash computation |

### Example - Progress callback

```python
from deadline.job_attachments._snapshots import (
    hash_abs_manifest,
    HashProgressMetadata,
)

def on_progress(metadata: HashProgressMetadata) -> bool:
    # Access timing information
    rate_mb_s = metadata.rate / (1024 * 1024)
    print(f"Progress: {metadata.progress:.1f}% - {rate_mb_s:.1f} MB/s - {metadata.progressMessage}")
    # Return False to cancel, True to continue
    return True

result = hash_abs_manifest(
    manifest=abs_manifest,
    on_progress=on_progress,
)

# Access final timing from statistics
print(f"Completed in {result.statistics.total_time:.2f}s")
print(f"Average rate: {result.statistics.rate / (1024 * 1024):.1f} MB/s")
```

## Returns

`HashResult` containing:
- `statistics`: `HashProgressMetadata` with detailed hash metrics (see below)
- `manifest`: A NEW `AbsManifest` (either `AbsSnapshot` or `AbsSnapshotDiff`) with all hashes filled in

### HashResult Statistics

The `statistics` field contains a `HashProgressMetadata` object with tracking for the hashing phase:

| Field | Description |
|-------|-------------|
| `total_file_chunks` | Total files + chunks to process |
| `total_bytes` | Total size of all files |
| `hashed_file_chunks` | Number of files/chunks that were hashed |
| `hashed_bytes` | Bytes that were hashed |
| `skipped_file_chunks` | Files/chunks skipped due to hash cache hit |
| `skipped_bytes` | Bytes skipped due to hash cache hit |
| `progress` | Overall progress percentage (0-100) |
| `progressMessage` | Human-readable summary message |
| `total_time` | Total operation time in seconds |
| `rate` | Final hashing rate in bytes/second (total_bytes / total_time) |

Note: In the final statistics, `rate` is calculated as `total_bytes / total_time` for accuracy,
which may differ slightly from the sliding window rate shown during progress callbacks.

Files/chunks are skipped when the hash cache has the file's hash and the mtime matches.

## Raises

- `ValueError` if the manifest contains relative paths
- `ValueError` if any regular file entry already has a hash (is not unhashed)

## Input Validation

The HASH operation validates that all regular file entries are unhashed (`hash=None` and `chunkhashes=None`). This validation:
- Prevents accidental re-hashing of already-hashed manifests
- Ensures the operation is safe to call only once per manifest
- Catches programming errors where a hashed manifest is passed incorrectly

To re-hash a manifest (e.g., after file modifications), call `clear_hashes()` on the manifest first to reset it to an unhashed state.

## Hash Cache Behavior

| Condition | Behavior |
|-----------|----------|
| `hash_cache` provided, `force_rehash=False` | Check cache by (path, mtime); use cached hash on hit |
| `hash_cache` provided, `force_rehash=True` | Always compute hash, update cache |
| `hash_cache` is None | Always compute hash |

### Hash Cache Key Resolution

Cache keys are generated by resolving the file path using `Path.resolve()`, which:
- Resolves any symlinks in the path components
- Normalizes `.` and `..` components
- Returns an absolute path

This ensures that different paths referring to the same physical file (e.g., via symlinks or relative references) share the same cache entry.

### Hash Cache Byte-Range Support

The hash cache supports caching hashes for both whole files and arbitrary byte ranges, enabling efficient caching for any chunking scheme:

| Entry Type | `range_start` | `range_end` | Description |
|------------|---------------|-------------|-------------|
| Whole-file | 0 | `WHOLE_FILE_RANGE_END` (-1) | Hash of entire file |
| Byte-range | ≥ 0 | > 0 | Hash of bytes in range [start, end) |

Cache entries are keyed by `(file_path, hash_algorithm, range_start, range_end)`. This allows:
- Caching whole-file hashes alongside chunk hashes for the same file
- Supporting different chunk sizes without cache invalidation
- Reusing cached chunk hashes when chunk boundaries align

When looking up or storing a hash, the `range_start` and `range_end` parameters specify which portion of the file the hash covers. The `WHOLE_FILE_RANGE_END` constant (-1) is a sentinel value indicating a whole-file hash.

## Chunking Behavior

Controlled by `manifest.fileChunkSizeBytes`:

| `fileChunkSizeBytes` | File Size | Behavior |
|---------------------|-----------|----------|
| `DEFAULT_FILE_CHUNK_SIZE` (256MB) | ≤ chunk size | Compute single `hash` (default) |
| `DEFAULT_FILE_CHUNK_SIZE` (256MB) | > chunk size | Compute `chunkhashes` (one per chunk) |
| `WHOLE_FILE_CHUNK_SIZE` (-1) | Any | Hash entire file as a whole (no chunking) |
| Positive int (e.g., 64MB) | ≤ chunk size | Compute single `hash` |
| Positive int (e.g., 64MB) | > chunk size | Compute `chunkhashes` (one per chunk) |

When chunking is enabled and a file is larger than the chunk size:
- `hash` field is `None`
- `chunkhashes` contains list of hashes, one per chunk
- Chunk count equals `ceil(size / fileChunkSizeBytes)`

## Entry Type Handling

| Entry Type | Action |
|------------|--------|
| Regular file (no chunking or ≤ chunk size) | Compute single hash |
| Large file (> chunk size, when chunking enabled) | Compute chunkhashes |
| Symlink | Pass through unchanged |
| Deleted marker | Pass through unchanged (diff manifests only) |
| Directory | Pass through unchanged |

## Manifest Type Handling

| Manifest Type | Behavior |
|---------------|----------|
| Snapshot | All file entries are hashed |
| Diff | Only new/modified file entries are hashed; deleted entries pass through unchanged |

The `parentManifestHash` field is preserved from the input manifest. The manifest type is determined by the class (e.g., `AbsSnapshotDiff`, `Snapshot`).

## Helper Functions

- `_get_or_compute_hash()` - Gets hash from cache or computes it (supports byte ranges)
- `_hash_file_chunked()` - Hashes large file in chunks with cache support

## Examples

### Hashing a snapshot

```python
from deadline.job_attachments._snapshots import (
    collect_abs_snapshot,
    hash_abs_manifest,
)
from deadline.job_attachments.caches.hash_cache import HashCache

# Collect the directory tree with absolute paths
abs_manifest = collect_abs_snapshot(
    ["/projects/my_scene"],  # directories
    [],                       # filenames
)

# Hash with a cache for efficiency
with HashCache("/tmp/hash_cache") as cache:
    result = hash_abs_manifest(
        manifest=abs_manifest,
        hash_cache=cache,
        force_rehash=False,  # Use cached hashes when available
    )

# Print hash statistics
stats = result.statistics
print(f"Hashed {stats.hashed_bytes} bytes, skipped {stats.skipped_bytes} bytes (cache hit)")
print(f"Completed in {stats.total_time:.2f}s at {stats.rate / (1024 * 1024):.1f} MB/s")

# Now entries have their hashes filled in (paths are still absolute)
for entry in result.manifest.files[:2]:
    if entry.symlink_target:
        print(f"  symlink: {entry.path} -> {entry.symlink_target}")
    elif entry.chunkhashes:
        print(f"  large file: {entry.path} ({len(entry.chunkhashes)} chunks)")
    else:
        print(f"  file: {entry.path} hash={entry.hash[:16]}...")
```

Output:
```
Hashed 1234567890 bytes, skipped 0 bytes (cache hit)
Completed in 5.23s at 236.1 MB/s
  file: /projects/my_scene/assets/model.blend hash=a1b2c3d4e5f67890...
  large file: /projects/my_scene/renders/output.exr (3 chunks)
```

### Hashing a diff manifest

```python
from deadline.job_attachments._snapshots import (
    diff_snapshots,
    hash_abs_manifest,
)

# Compute a diff between two snapshots (with ignore_hashes=True for fast comparison)
diff = diff_snapshots(
    parent=parent_snapshot,
    current=current_snapshot,
    parent_manifest_hash="abc123...",
    ignore_hashes=True,  # Compare by mtime/size only
)

# Now hash the diff to fill in hashes for new/modified files
result = hash_abs_manifest(diff)

# Print statistics
print(f"Hashed {result.statistics.hashed_file_chunks} files/chunks")

# Deleted entries are preserved unchanged
for entry in result.manifest.files:
    if entry.deleted:
        print(f"  deleted: {entry.path}")
    else:
        print(f"  new/modified: {entry.path} hash={entry.hash[:16]}...")
```
