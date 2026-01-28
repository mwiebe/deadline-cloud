# PARTITION Operation: `partition_manifest()`

*← [Back to Job Attachments Snapshots](../job_attachments_snapshots.md)*


**Location:** `_partition_manifest.py`

Partitions a manifest into multiple (root, RelSnapshot) pairs, dividing entries by their root paths. Each RelSnapshot is an extracted subtree per the SUBTREE operation, with paths relative to its root:

```python
def partition_manifest(
    manifest: Manifest,
    roots: Optional[List[str]] = None,
    *,
    referenced_paths: Optional[List[str]] = None,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
) -> List[Tuple[str, RelManifest]]:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | Source manifest (absolute or relative paths) |
| `roots` | Optional list of root paths to partition by. No root may be a subpath of another. |
| `referenced_paths` | Optional list of paths referenced by the workload. These paths must be within one of the resulting roots, affecting auto-root determination even if no files exist under them. |
| `symlink_policy` | How to handle symlinks that escape their partition root. Default `COLLAPSE_ESCAPING`. Only `COLLAPSE_ALL`, `COLLAPSE_ESCAPING`, and `EXCLUDE_ALL` are supported. |

**Returns:** A list of `(root, RelManifest)` tuples where:
- Each `root` is an absolute or relative path string
- Each `RelManifest` is a manifest with paths relative to that root

**Validation Rules:**

| Condition | Behavior |
|-----------|----------|
| A root is a subpath of another root | Raises `ValueError` |
| Root path style doesn't match manifest path style | Raises `ValueError` |
| `symlink_policy=PRESERVE` | Raises `ValueError` |
| `symlink_policy=TRANSITIVE_INCLUDE_TARGETS` | Raises `ValueError` |

**Path Separator Behavior:**

Paths within manifests use POSIX forward slashes (see [Path Normalization](snapshot_manifest_classes.md#path-normalization)). However, PARTITION returns root paths using the platform's native separator for filesystem API compatibility:

| Platform | Output (`root` in returned tuples) |
|----------|-----------------------------------|
| Windows | Uses native `\` separators |
| POSIX | Uses `/` separators |

**Output Ordering:**

1. First: Entries for each explicitly provided root (in the same order as `roots` parameter)
2. Then: Auto-determined roots for remaining entries (sorted alphabetically)

If no entries exist under an explicitly provided root, its RelSnapshot is empty (but still included in output).

**Auto-Root Determination:**

| Scenario | Platform | Behavior |
|----------|----------|----------|
| `roots` is None or empty | POSIX | Single root: longest common path prefix of all entries and referenced_paths |
| `roots` is None or empty | Windows | One root per drive letter or UNC root path that contains entries or referenced_paths |
| `roots` provided | Any | Provided roots first, then smallest set of additional roots to cover remaining entries and referenced_paths |

When `roots` is provided, remaining entries (not under any provided root) are grouped into additional auto-determined roots. These additional roots form the smallest set that:
- Covers all remaining entries
- Covers all referenced_paths not under a provided root
- Does not include any provided root as a subpath

The `referenced_paths` parameter influences root determination by treating each referenced path as if it were an entry in the manifest for the purpose of computing roots. This ensures workload-referenced directories are accessible under one of the resulting roots, even if no files currently exist there.

This typically results in more roots than the empty-roots case, since the provided roots may not align with the natural grouping of entries.

**Empty Directory Handling:**

Empty directories (entries in `manifest.dirs`) are included in root determination alongside file parent directories. This ensures that manifests containing empty directories are partitioned correctly:

```
# Files only under /a/b, but empty dir at /a/c
# Auto-root will be /a (not /a/b) to include both
/a/b/file.txt
/a/c/           (empty directory)
```

Without this, a manifest with files under `/a/b` and an empty directory at `/a/c` would incorrectly compute `/a/b` as the root, excluding the empty directory from the partition.

**Symlink Handling:**

Symlinks are handled per-partition using the same logic as SUBTREE. See [snapshot_symlink_handling.md](snapshot_symlink_handling.md) for details.

**Example - Auto-partition on POSIX (no roots provided):**

```python
from deadline.job_attachments._snapshots import partition_manifest

# Manifest with absolute paths under a common root
# /projects/scene/assets/model.blend
# /projects/scene/assets/texture.png
# /projects/scene/render/output.exr

partitions = partition_manifest(manifest)
# Result: [("/projects/scene", RelSnapshot)]
# RelSnapshot contains:
#   assets/model.blend
#   assets/texture.png
#   render/output.exr
```

**Example - Auto-partition on Windows (no roots provided):**

```python
# Manifest with paths on multiple drives
# C:/projects/scene/model.blend
# C:/projects/scene/texture.png
# D:/shared/library/material.mtl

partitions = partition_manifest(manifest)
# Result: [
#   ("C:/projects/scene", RelSnapshot with model.blend, texture.png),
#   ("D:/shared/library", RelSnapshot with material.mtl),
# ]
```

**Example - Explicit roots with remainder:**

```python
# Manifest entries:
# /projects/scene/model.blend
# /projects/scene/texture.png
# /data/shared/library/material.mtl
# /home/user/cache/temp.bin

partitions = partition_manifest(
    manifest,
    roots=["/projects/scene"],
)
# Result: [
#   ("/projects/scene", RelSnapshot),      # explicit root
#   ("/data/shared/library", RelSnapshot), # auto-determined for remaining
#   ("/home/user/cache", RelSnapshot),     # auto-determined for remaining
# ]

# Compare to no explicit roots on POSIX:
partitions = partition_manifest(manifest)
# Result: [("/", RelSnapshot)]  # single root covering everything
```

**Example - Empty partition for explicit root:**

```python
# Request a root that has no entries
partitions = partition_manifest(
    manifest,
    roots=["/projects/scene", "/empty/path"],
)
# Result: [
#   ("/projects/scene", RelSnapshot with entries),
#   ("/empty/path", empty RelSnapshot),  # Still included, but empty
# ]
```

**Relationship to SUBTREE and JOIN:**

PARTITION is conceptually the inverse of multiple JOIN operations followed by COMPOSE:

```python
# PARTITION splits:
[(root1, rel1), (root2, rel2)] = partition_manifest(abs_manifest)

# JOIN combines (inverse):
abs1 = join_manifest(rel1, root1)
abs2 = join_manifest(rel2, root2)
composed = compose_manifests([abs1, abs2])
# composed ≈ abs_manifest
```

