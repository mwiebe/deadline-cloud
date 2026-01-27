# COMPOSE Operation: `compose_manifests()`

**Location:** `_compose_manifest.py`

Layers multiple manifests together into a single manifest, as if applying each manifest as a set of changes in order:

```python
def compose_manifests(
    manifests: List[Manifest],
) -> Manifest:
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
- All manifests must have the same `fileChunkSizeBytes` value
- Entries are keyed by path; later entries replace earlier ones
- Deleted markers remove the entry entirely from the result (v2025)
- Total size is recomputed from the final entry set
- For v2025 snapshot+diffs: first must be snapshot, rest must be diffs
- For v2025 diff composition: all must be diffs, `parentManifestHash` from first diff

**Trie-Based Implementation:**

The COMPOSE operation uses a trie (prefix tree) structure where each node represents a path component. This provides efficient handling of directory operations and cascading effects.

*Trie node structure:*

| Field | Description |
|-------|-------------|
| `children` | Child nodes keyed by path component |
| `file_entry` | File/symlink entry at this node (None for directories) |
| `deleted` | Deletion marker flag (for diff composition only) |

*Why a trie?*

1. **Efficient path operations:** Insert, lookup, and delete are O(path depth) rather than O(n) for flat lists
2. **Natural directory structure:** The trie mirrors the filesystem hierarchy, making directory operations intuitive
3. **Cascading deletions:** When a directory is deleted, its subtree can be efficiently removed or marked

*Snapshot + Diffs composition:*

When composing a snapshot with diffs, the trie accumulates the final state:
- Base snapshot entries are inserted into the trie
- For each diff: deletions remove nodes from the trie, additions/modifications insert or update nodes
- The final trie contains only the entries that exist after all diffs are applied
- Deletion markers are NOT preserved in the output (it's a snapshot, not a diff)

*Diff + Diffs composition:*

When composing multiple diffs (without a base snapshot), the trie tracks cumulative changes:
- Each node has a `deleted` flag to track deletion markers
- Deletions set `deleted=True`; additions clear it and set `file_entry`
- After all diffs are applied, `reconcile_deleted_flags()` handles the case where a deleted directory has non-deleted children (the directory must exist for its children)
- The output includes both current entries AND deletion markers

*The reconciliation step:*

The `reconcile_deleted_flags()` method handles this scenario:
1. diff1 deletes `/dir/` and all its contents
2. diff2 adds `/dir/newfile.txt`

After diff2, `/dir/` must NOT be marked as deleted because it has a non-deleted child. The reconciliation traverses the trie depth-first and clears the `deleted` flag on any node that has non-deleted descendants.

**Validation Rules:**

| Condition | Behavior |
|-----------|----------|
| Empty manifest list | Raises `ValueError` |
| Manifests have different `fileChunkSizeBytes` | Raises `ValueError` |
| Snapshot+diffs: non-snapshot first | Raises `ValueError` |
| Snapshot+diffs: non-diff after first | Raises `ValueError` |
| Diff composition: non-diff in list | Raises `ValueError` |

**Example:**

```python
from deadline.job_attachments._snapshots import compose_manifests
from deadline.job_attachments.asset_manifests.decode import decode_manifest

# Load a base snapshot and incremental diffs
with open("base.manifest") as f:
    base = decode_manifest(f.read())
with open("day1.manifest") as f:
    diff1 = decode_manifest(f.read())
with open("day2.manifest") as f:
    diff2 = decode_manifest(f.read())

# Compose into a single snapshot representing the final state
final = compose_manifests([base, diff1, diff2])

print(f"Final manifest has {len(final.files)} entries")
print(f"Manifest type: {type(final).__name__}")  # AbsSnapshot or Snapshot
```

For v2023 format (layering snapshots):

```python
# Multiple output manifests from different render tasks
task1_output = decode_manifest(read_file("task1_output.manifest"))
task2_output = decode_manifest(read_file("task2_output.manifest"))
task3_output = decode_manifest(read_file("task3_output.manifest"))

# Merge into single manifest (later tasks override earlier for same paths)
merged = compose_manifests([task1_output, task2_output, task3_output])
```

