# SUBTREE Operation: `subtree_manifest()`

*← [Back to Job Attachments Snapshots](../job_attachments_snapshots.md)*


**Location:** `_subtree_manifest.py`

Extracts a subtree from a manifest, producing a new manifest rooted at the specified subdirectory:

```python
def subtree_manifest(
    manifest: Manifest,
    subtree: str,
    *,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
) -> RelManifest:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | The source manifest to extract from |
| `subtree` | Path to the subtree root, or `"."` or `""` for identity transformation (see below) |
| `symlink_policy` | How to handle symlinks that escape the new subtree root (see below) |

**Identity Subtree (`subtree="."` or `subtree=""`):**

When `subtree="."` or `subtree=""`, the operation acts as an identity transformation that applies the `symlink_policy` without rebasing paths. This is useful for:
- Collapsing all symlinks in a manifest before serialization to v2023 format
- Excluding all symlinks from a manifest
- Processing symlinks without changing the directory structure

The identity subtree requires a manifest with relative paths (since the output is always `RelManifest`).

**Conceptual Model:**

SUBTREE is a virtual re-rooting operation. Given a manifest rooted at `/projects/scene` and a subtree path of `assets/textures`, the result is a new manifest that represents only the `assets/textures` directory as if it were the root:

```
Original manifest root: /projects/scene
├── assets/
│   ├── textures/
│   │   ├── wood.png
│   │   └── metal.png
│   └── models/
│       └── chair.blend
└── scripts/
    └── render.py

SUBTREE(manifest, "assets/textures") produces:

New manifest root: /projects/scene/assets/textures
├── wood.png
└── metal.png
```

The operation:
1. Filters to entries within the subtree
2. Rebases paths relative to the new root (strips the subtree prefix)
3. Handles symlinks according to `symlink_policy`

**Path Style Requirements:**

The `subtree` path must match the path style used in the manifest:

| Manifest Paths | Subtree Path | Valid |
|----------------|--------------|-------|
| Relative (`assets/file.txt`) | Relative (`assets`) | ✓ |
| Absolute (`/projects/scene/assets/file.txt`) | Absolute (`/projects/scene/assets`) | ✓ |
| Relative | Absolute | ✗ Error |
| Absolute | Relative | ✗ Error |

**Output:**

The output manifest always uses relative paths, regardless of whether the input used absolute paths. This makes the result suitable for storage or transport.

The `fileChunkSizeBytes` field IS preserved in the output manifest, ensuring chunk size settings are maintained through subtree operations.

**Note:** The `parentManifestHash` field is NOT preserved in the output manifest. Since the subtree operation changes the root path, the original parent manifest hash would be invalid for the new subtree manifest.

**Symlink Handling:**

When re-rooting a manifest, symlinks that were previously "within root" may now "escape" the new subtree root. The `symlink_policy` parameter controls how these are handled:

| Policy | Behavior for Escaping Symlinks |
|--------|-------------------------------|
| `COLLAPSE_ALL` | Collapse every symlink in the result (regardless of whether it escapes). |
| `COLLAPSE_ESCAPING` | Collapse only symlinks escaping the new subtree; preserve symlinks within subtree |
| `EXCLUDE_ALL` | Exclude every symlink from the result (regardless of whether it escapes). |
| `EXCLUDE_ESCAPING` | Exclude only symlinks escaping the new subtree; preserve symlinks within subtree |

**Note:** `PRESERVE` and `TRANSITIVE_INCLUDE_TARGETS` are not supported for SUBTREE. Since the output always uses relative paths, escaping symlinks cannot be represented—a relative symlink target like `../outside/file.txt` would point outside the manifest root, which is invalid. Therefore, escaping symlinks must either be collapsed or excluded.

**Symlink Policy Behavior in SUBTREE:**

| Policy | Symlinks within subtree | Symlinks escaping subtree |
|--------|------------------------|---------------------------|
| `COLLAPSE_ALL` | Collapsed to file/directory | Collapsed to file/directory |
| `COLLAPSE_ESCAPING` | Preserved (target rebased) | Collapsed to file/directory |
| `EXCLUDE_ALL` | Excluded | Excluded |
| `EXCLUDE_ESCAPING` | Preserved (target rebased) | Excluded |

**Note:** Unlike COLLECT, SUBTREE operates purely on manifest data—it never accesses the filesystem. When a symlink is "collapsed," the operation looks up the target path in the original manifest and copies that entry's data (hash, size, mtime, etc.) to replace the symlink entry.

**Important: Symlink Target Storage Format:**

In our manifest format, symlink targets are stored **relative to the manifest root**, not relative to the symlink location (unlike POSIX filesystem symlinks). For example, a symlink at `assets/textures/current` pointing to `assets/shared/latest.png` stores the target as `assets/shared/latest.png`, not as `../shared/latest.png`.

This design choice simplifies manifest operations since targets can be looked up directly in the manifest's path index without needing to resolve relative paths from the symlink's location.

**Symlink Collapse Behavior:**

When collapsing a symlink, the operation looks up the target path in the original manifest:

- **File target:** The symlink entry is replaced with a copy of the target file entry (using the symlink's path, but the target's hash, size, mtime, runnable)
- **Directory target:** The symlink entry is replaced with all entries under that directory in the original manifest, recursively. Paths are rebased so the symlink path becomes the new prefix (e.g., symlink `current` with target `assets/shared/v2` containing `assets/shared/v2/a.txt` and `assets/shared/v2/sub/b.txt` produces `current/a.txt` and `current/sub/b.txt`)
- **Missing target:** If the target doesn't exist in the manifest (e.g., it was an escaping symlink that was already collapsed during COLLECT), the symlink is excluded with a warning

**Symlink Cycle Handling:**

Symlink cycles occur when following symlinks leads back to a previously visited target. Examples:
- Self-referential: `A -> A` (length 1)
- Direct cycle: `A -> B -> A` (length 2)
- Longer cycles: `A -> B -> C -> A` (length 3+)

When collapsing symlinks, SUBTREE detects cycles and handles them gracefully:

| Policy | Cycle Behavior |
|--------|----------------|
| `COLLAPSE_ALL` | Cycles detected during collapse; cyclic symlink skipped with warning |
| `COLLAPSE_ESCAPING` | Cycles detected when collapsing escaping symlinks; cyclic symlink skipped with warning |
| `EXCLUDE_ALL` | No collapse needed; all symlinks excluded |
| `EXCLUDE_ESCAPING` | Cycles detected when collapsing escaping symlinks; cyclic symlink skipped with warning |

When a cycle is detected:
1. A warning is logged identifying the cyclic symlink
2. The cyclic symlink is skipped (produces no output entries)
3. Processing continues with remaining entries

**Preserved Symlink Target Rebasing:**

Symlinks that are preserved (not collapsed) must have their `symlink_target` rebased relative to the new subtree root. Since targets are stored relative to the manifest root, rebasing simply strips the subtree prefix from the target path. For example:

```
Original manifest (rooted at /projects/scene):
  assets/textures/wood.png
  assets/textures/current -> assets/shared/v2/latest.png  (target outside subtree - escapes)
  assets/textures/alt -> assets/textures/variants/dark.png  (target within subtree)
  assets/shared/v2/latest.png

SUBTREE(manifest, "assets/textures") with COLLAPSE_ESCAPING:

Result (rooted at /projects/scene/assets/textures):
  wood.png
  current                    (collapsed: now a file with latest.png's content)
  alt -> variants/dark.png   (preserved: target rebased from assets/textures/variants/dark.png)
```

The preserved symlink `alt` originally had target `assets/textures/variants/dark.png`. After rebasing (stripping the `assets/textures/` prefix), it becomes `variants/dark.png`.

**Example - Basic Subtree Extraction:**

```python
from deadline.job_attachments._snapshots import subtree_manifest
from deadline.job_attachments.asset_manifests.decode import decode_manifest

# Load a manifest rooted at /projects/scene
with open("scene.manifest") as f:
    full_manifest = decode_manifest(f.read())

# Extract just the textures directory
textures = subtree_manifest(
    manifest=full_manifest,
    subtree="assets/textures",
)

# Result is a manifest with paths relative to assets/textures/
for entry in textures.files:
    print(entry.path)  # "wood.png", "metal.png", etc.
```

**Example - Handling Symlinks:**

```python
# Original manifest structure (symlink targets are relative to manifest root):
# /projects/scene/
# ├── assets/
# │   ├── textures/
# │   │   ├── wood.png
# │   │   └── current -> assets/shared/latest.png  (target outside subtree - escapes!)
# │   └── shared/
# │       └── latest.png
# └── ...

# With COLLAPSE_ESCAPING (default): symlink is replaced with file content
textures = subtree_manifest(
    manifest=full_manifest,
    subtree="assets/textures",
    symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
)
# Result: "current" becomes a regular file with latest.png's hash/size/mtime

# With EXCLUDE_ALL: symlink is removed
textures = subtree_manifest(
    manifest=full_manifest,
    subtree="assets/textures",
    symlink_policy=SymlinkPolicy.EXCLUDE_ALL,
)
# Result: only "wood.png" is included
```

**Use Cases:**

1. **Absolute to relative conversion:** Collect manifest directory trees with `absolute_paths=True` for intermediate processing, then use SUBTREE to convert to relative paths for saving as manifest files
2. **Partial deployment:** Extract only the assets needed for a specific render task
3. **Manifest splitting:** Break a large manifest into smaller, focused manifests
4. **Re-rooting for transport:** Create a manifest for a subdirectory to upload independently
5. **Testing:** Extract a subset of a manifest for focused testing

**Relationship to FILTER:**

SUBTREE and FILTER are complementary but distinct:

| Operation | Purpose | Path Transformation |
|-----------|---------|---------------------|
| FILTER | Keep entries matching a predicate | Paths unchanged |
| SUBTREE | Extract entries under a path prefix | Paths rebased to new root |

You might use both together:

```python
# Extract textures subtree, then filter to only PNG files
textures = subtree_manifest(full_manifest, "assets/textures")
png_only = filter_manifest(textures, lambda e: e.path.endswith(".png"))
```

