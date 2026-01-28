# DIFF Operation: `diff_snapshots()`

[Job Attachments Snapshots](../job_attachments_snapshots.md) · DIFF Operation


**Location:** `_diff_snapshots.py`

Computes the difference between two snapshot manifests:

```python
def diff_snapshots(
    parent: SnapshotManifest,
    current: SnapshotManifest,
    parent_manifest_hash: Optional[str] = None,
    ignore_hashes: bool = False,
    *,
    preserve_runnable: bool = False,
) -> DiffManifest:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `parent` | The parent snapshot manifest (filtered, with hashes) |
| `current` | The current snapshot manifest (filtered, with hashes) |
| `parent_manifest_hash` | Optional hash of the parent manifest (for v2025 diff manifests) |
| `ignore_hashes` | If `True`, compare by metadata only (size, mtime, runnable) without hashes |
| `preserve_runnable` | If `True`, copy `runnable` from parent for modified files (see below) |

**Preconditions:**

1. Both manifests must be the same version
2. Both manifests should be filtered with the same patterns
3. Both manifests should have hashes computed (unless `ignore_hashes=True`)

**Hash State Validation:**

When `ignore_hashes=False`, the function validates that both manifests have compatible hash states:

| Parent State | Current State | Result |
|--------------|---------------|--------|
| Hashed | Hashed | ✓ Comparison proceeds |
| Unhashed | Unhashed | ✓ Comparison proceeds |
| Empty/symlinks-only | Any | ✓ Comparison proceeds |
| Any | Empty/symlinks-only | ✓ Comparison proceeds |
| Hashed | Unhashed | ✗ Raises `ManifestHashMismatchError` |
| Unhashed | Hashed | ✗ Raises `ManifestHashMismatchError` |

Empty manifests and manifests containing only symlinks are considered compatible with either hashed or unhashed manifests, since they have no regular files whose hash state could conflict.

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
| Manifest class | AbsSnapshot/Snapshot | AbsSnapshotDiff/SnapshotDiff |
| parentManifestHash | N/A | ✓ (if provided) |

**Returns:** A `DiffManifest` (either `AbsSnapshotDiff` or `SnapshotDiff` depending on input path style) with:
- `parentManifestHash` if provided
- New/modified entries with full content
- Deleted entries with `deleted=True` markers

**Entry comparison logic (`_entries_differ()`):**

- Type transitions (file ↔ symlink) are always different
- Symlinks compared by `symlink_target` only
- Regular files compared by hash/chunkhashes (unless `ignore_hashes`), size, mtime, runnable

**The `preserve_runnable` Parameter:**

The `runnable` field captures the POSIX execute bit (`chmod +x`). On Windows, this filesystem concept doesn't exist—all files report `runnable=False` when collected. This creates a problem for cross-platform workflows:

1. A manifest is created on POSIX with `script.sh` having `runnable=True`
2. The manifest is used on Windows, files are extracted
3. User modifies `script.sh` content on Windows
4. A new manifest is collected on Windows—`script.sh` now has `runnable=False`
5. The diff shows `script.sh` as modified, but with `runnable=False`
6. When applied back to POSIX, the execute bit is incorrectly removed

Setting `preserve_runnable=True` solves this by:
- Ignoring `runnable` differences when determining if entries differ (so a file that only changed `runnable` won't appear in the diff)
- Copying the `runnable` value from the parent manifest for modified files that have other changes

New files always use the current manifest's `runnable` value (which will be `False` on Windows, but that's correct for newly created files).

**When to use `preserve_runnable=True`:**
- On Windows when computing diffs against a parent manifest that may have come from POSIX
- In any cross-platform workflow where you want to preserve execute bits through modifications

**Examples:**

```python
from deadline.job_attachments._snapshots import diff_snapshots
from deadline.job_attachments.asset_manifests.decode import decode_manifest
from deadline.job_attachments.asset_manifests.hash_algorithms import hash_data, HashAlgorithm

# Load the parent manifest
with open("previous.manifest") as f:
    parent_str = f.read()
    parent = decode_manifest(parent_str)
    parent_hash = hash_data(parent_str.encode("utf-8"), HashAlgorithm.XXH128)

# Assume current_hashed is a collected and hashed manifest of the current directory
diff = diff_snapshots(
    parent=parent,
    current=current_hashed,
    parent_manifest_hash=parent_hash,
    ignore_hashes=False,  # Compare by hash for accuracy
    preserve_runnable=True,  # Preserve execute bits from parent for modified files
)

# Inspect the diff
new_files = [p for p in diff.files if p.path not in {e.path for e in parent.files}]
deleted = [p for p in diff.files if p.deleted]
print(f"New: {len(new_files)}, Deleted: {len(deleted)}")
print(f"Diff manifest type: {type(diff).__name__}")  # AbsSnapshotDiff or SnapshotDiff
print(f"Parent hash: {diff.parentManifestHash[:16]}...")
```

Output:
```
New: 3, Deleted: 1
Diff manifest type: SnapshotDiff
Parent hash: f8e9d0c1b2a34567...
```

