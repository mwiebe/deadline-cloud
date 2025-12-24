# Composable Manifest Operations

A manifest is a data structure that captures a directory tree snapshot—similar to a zip file's table of contents, but without the actual file content. It records metadata for each file (path, size, modification time, content hash) and, in newer formats, directories and symlinks. Manifests enable efficient change detection, incremental uploads, and content-addressable storage workflows.

This document describes the composable operations design for job attachment manifests in AWS Deadline Cloud. These operations provide a modular approach to creating, transforming, and comparing manifest objects.

## Overview

The manifest system uses four composable operations that can be combined to implement various workflows:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        COMPOSABLE OPERATIONS                            │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  1. COLLECT: Directory → Manifest (with hash="" for files)              │
│     _collect_manifest_structure(root, version) → BaseAssetManifest      │
│                                                                         │
│  2. HASH: Manifest → Manifest (fills in hashes)                         │
│     _hash_manifest(manifest, root, hash_cache, force_rehash)            │
│                                                                         │
│  3. FILTER: Manifest → Manifest (keeps matching entries)                │
│     _filter_manifest(manifest, entry_filter) → BaseAssetManifest        │
│                                                                         │
│  4. DIFF: (Snapshot, Snapshot) → Diff Manifest                          │
│     _compute_diff_manifest(parent, current, parent_hash, ignore_hashes) │
│                                                                         │
│  5. COMPOSE: (Manifest, Manifest, ...) → Manifest                       │
│     _compose_manifests(manifests) → BaseAssetManifest                   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Why Separate COLLECT and HASH?

Separating structure collection from hashing enables:

- **Fast diff comparison:** Compare manifests by mtime/size without hashing unchanged files
- **Hash cache integration:** Only hash files with cache misses
- **Deferred hashing:** Collect structure first, hash only what's needed
- **Reduced redundant reads:** Can read file to memory, then hash + upload instead of separate reads

## Module Organization

The composable operations are implemented in separate modules under `src/deadline/job_attachments/asset_manifests/`:

| Module | Operation | Description |
|--------|-----------|-------------|
| `_collect_manifest.py` | COLLECT | Scans directory, creates manifest with `hash=""` |
| `_hash_manifest.py` | HASH | Fills in hashes for collected manifest |
| `_filter_manifest.py` | FILTER | Filters manifest entries using callable filter |
| `_diff_manifest.py` | DIFF | Computes difference between two manifests |
| `_compose_manifest.py` | COMPOSE | Layers manifests together into one |

## Operation Details

### 1. COLLECT: `_collect_manifest_structure()`

**Location:** `_collect_manifest.py`

Scans a directory and creates a manifest structure WITHOUT computing hashes:

```python
def _collect_manifest_structure(
    root: Path | str,
    version: ManifestVersion,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> BaseAssetManifest:
```

**Behavior by version:**

| Feature | v2023-03-03 | v2025-12-04-beta |
|---------|-------------|------------------|
| Regular files | ✓ (hash="") | ✓ (hash="") |
| Symlinks | Skipped | ✓ (with validated target) |
| Directories | Not included | ✓ (including empty) |
| Execute bit | Not captured | ✓ (runnable field) |

**Key implementation details:**

- Files have `hash=""` (empty string) to indicate hashing is needed
- Symlinks have `symlink_target` set (no hash needed)
- Symlink targets are validated to be relative and within the manifest root
- Uses `os.walk()` with `followlinks=False` to avoid following symlinks

**Helper functions:**

- `_create_unhashed_file_entry()` - Creates file entry with `hash=""` and metadata
- `_create_symlink_entry()` - Creates symlink entry with validated target

**Example:**

```python
from deadline.job_attachments.asset_manifests._collect_manifest import _collect_manifest_structure
from deadline.job_attachments.asset_manifests.versions import ManifestVersion

# Collect a v2025 manifest from a project directory
manifest = _collect_manifest_structure(
    root="/projects/my_scene",
    version=ManifestVersion.v2025_12_04_beta,
)

# Inspect the collected structure
print(f"Found {len(manifest.paths)} files/symlinks")
print(f"Found {len(manifest.dirs)} directories")

for entry in manifest.paths[:3]:
    if entry.symlink_target:
        print(f"  symlink: {entry.path} -> {entry.symlink_target}")
    else:
        print(f"  file: {entry.path} (size={entry.size}, hash='{entry.hash}')")
        # Note: hash is "" until _hash_manifest() is called
```

Output:
```
Found 42 files/symlinks
Found 8 directories
  file: assets/model.blend (size=15234567, hash='')
  file: assets/texture.png (size=2048576, hash='')
  symlink: assets/current -> v2/model.blend
```

### 2. HASH: `_hash_manifest()`

**Location:** `_hash_manifest.py`

Fills in hashes for a manifest that was created by `_collect_manifest_structure()`:

```python
def _hash_manifest(
    manifest: BaseAssetManifest,
    root: Path | str,
    hash_cache: Optional[HashCache] = None,
    force_rehash: bool = False,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> BaseAssetManifest:
```

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
| Deleted marker | Pass through unchanged |
| Directory | Pass through unchanged |

**Helper functions:**

- `_get_or_compute_hash()` - Gets hash from cache or computes it (supports byte ranges)
- `_hash_file_chunked()` - Hashes large file in chunks with cache support

**Example:**

```python
from deadline.job_attachments.asset_manifests._collect_manifest import _collect_manifest_structure
from deadline.job_attachments.asset_manifests._hash_manifest import _hash_manifest
from deadline.job_attachments.asset_manifests.versions import ManifestVersion
from deadline.job_attachments.caches.hash_cache import HashCache

# First collect the structure
unhashed = _collect_manifest_structure(
    root="/projects/my_scene",
    version=ManifestVersion.v2025_12_04_beta,
)

# Then hash with a cache for efficiency
with HashCache("/tmp/hash_cache") as cache:
    hashed = _hash_manifest(
        manifest=unhashed,
        root="/projects/my_scene",
        hash_cache=cache,
        force_rehash=False,  # Use cached hashes when available
    )

# Now entries have their hashes filled in
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
  file: assets/model.blend hash=a1b2c3d4e5f67890...
  large file: renders/output.exr (3 chunks)
```

### 3. FILTER: `_filter_manifest()`

**Location:** `_filter_manifest.py`

Applies a filter to manifest entries, returning a new manifest with only matching entries:

```python
def _filter_manifest(
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

filtered = _filter_manifest(manifest, large_files_only)
```

**Built-in filter: `IncludeExcludePathsFilter`**

Implements classic include/exclude glob pattern matching:

```python
filter = IncludeExcludePathsFilter(
    include=["*.blend", "textures/*"],
    exclude=["backup/*", "*.tmp"]
)
filtered = _filter_manifest(manifest, filter)
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
from deadline.job_attachments.asset_manifests._filter_manifest import (
    _filter_manifest,
    IncludeExcludePathsFilter,
)

# Filter to only include Blender files and textures, excluding backups
filter = IncludeExcludePathsFilter(
    include=["*.blend", "textures/**/*"],
    exclude=["backup/*", "*_old.*"],
)

filtered = _filter_manifest(manifest, filter)
print(f"Filtered from {len(manifest.paths)} to {len(filtered.paths)} entries")

# Or use a custom filter function
def python_files_only(entry):
    return entry.path.endswith(".py")

py_manifest = _filter_manifest(manifest, python_files_only)
```

### 4. DIFF: `_compute_diff_manifest()`

**Location:** `_diff_manifest.py`

Computes the difference between two snapshot manifests:

```python
def _compute_diff_manifest(
    parent: BaseAssetManifest,
    current: BaseAssetManifest,
    parent_manifest_hash: Optional[str] = None,
    ignore_hashes: bool = False,
    print_function_callback: Callable[[Any], None] = lambda msg: None,
) -> BaseAssetManifest:
```

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

**Example:**

```python
from deadline.job_attachments.asset_manifests._diff_manifest import _compute_diff_manifest
from deadline.job_attachments.asset_manifests.decode import decode_manifest
from deadline.job_attachments.asset_manifests.hash_algorithms import hash_data, HashAlgorithm

# Load the parent manifest
with open("previous.manifest") as f:
    parent_str = f.read()
    parent = decode_manifest(parent_str)
    parent_hash = hash_data(parent_str.encode("utf-8"), HashAlgorithm.XXH128)

# Assume current_hashed is a collected and hashed manifest of the current directory
diff = _compute_diff_manifest(
    parent=parent,
    current=current_hashed,
    parent_manifest_hash=parent_hash,
    ignore_hashes=False,  # Compare by hash for accuracy
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

### 5. COMPOSE: `_compose_manifests()`

**Location:** `_compose_manifest.py`

Layers multiple manifests together into a single manifest, as if applying each manifest as a set of changes in order:

```python
def _compose_manifests(
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
from deadline.job_attachments.asset_manifests._compose_manifest import _compose_manifests
from deadline.job_attachments.asset_manifests.decode import decode_manifest

# Load a base snapshot and incremental diffs
with open("base.manifest") as f:
    base = decode_manifest(f.read())
with open("day1.manifest") as f:
    diff1 = decode_manifest(f.read())
with open("day2.manifest") as f:
    diff2 = decode_manifest(f.read())

# Compose into a single snapshot representing the final state
final = _compose_manifests([base, diff1, diff2])

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
merged = _compose_manifests([task1_output, task2_output, task3_output])
```

## Workflow Examples

### Snapshot Flow

Create a complete snapshot manifest from a directory:

```
Directory ──[collect]──► Unhashed ──[hash]──► Hashed ──[filter]──► Filtered ──[save]──► File
```

```python
# Step 1: Collect directory structure (no hashes yet)
unhashed = _collect_manifest_structure(root, version)

# Step 2: Hash all files
hashed = _hash_manifest(unhashed, root, hash_cache)

# Step 3: Filter the snapshot
filter = IncludeExcludePathsFilter(include=include, exclude=exclude)
filtered = _filter_manifest(hashed, filter)

# Step 4: Write to file
manifest_path = _write_manifest(root, filtered, destination, name)
```

### Diff Flow (Fast Mode)

Compare by mtime/size without hashing unchanged files:

```
Parent File ──[load]──► Parent ──[filter]──► Filtered Parent ──┐
                                                               ├──[diff]──► Diff Manifest
Directory ──[collect]──► Unhashed ──[filter]──► Filtered Current ─┘
                         (no hashes)
```

```python
# Load parent manifest
with open(parent_path) as f:
    parent_str = f.read()
    parent = decode_manifest(parent_str)
    parent_hash = hash_data(parent_str.encode("utf-8"), HashAlgorithm.XXH128)

# Collect current directory (no hashes)
current_unhashed = _collect_manifest_structure(root, version)

# Filter BOTH with same patterns
filter = IncludeExcludePathsFilter(include=include, exclude=exclude)
filtered_parent = _filter_manifest(parent, filter)
filtered_current = _filter_manifest(current_unhashed, filter)

# Compute diff (fast mode - compare by mtime/size)
diff = _compute_diff_manifest(
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
Directory ──[collect]──► Unhashed ──[hash]──► Hashed ──[filter]──► Filtered Current ─┘
```

```python
# Load parent manifest
with open(parent_path) as f:
    parent_str = f.read()
    parent = decode_manifest(parent_str)
    parent_hash = hash_data(parent_str.encode("utf-8"), HashAlgorithm.XXH128)

# Collect and hash current directory
current_unhashed = _collect_manifest_structure(root, version)
current_hashed = _hash_manifest(current_unhashed, root, hash_cache, force_rehash=True)

# Filter BOTH with same patterns
filter = IncludeExcludePathsFilter(include=include, exclude=exclude)
filtered_parent = _filter_manifest(parent, filter)
filtered_current = _filter_manifest(current_hashed, filter)

# Compute diff (full mode - compare by hash)
diff = _compute_diff_manifest(
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

## Benefits of Composable Design

1. **Testability:** Each operation can be unit tested independently
2. **Reusability:** Operations can be composed in different ways for different workflows
3. **Performance:** Deferred hashing allows skipping unchanged files
4. **Flexibility:** Custom filters enable advanced filtering beyond glob patterns
5. **Consistency:** Same filter applied to both sides ensures correct diff computation
