# SUBTREE Operation: `subtree_manifest()`

[Job Attachments Snapshots](../job_attachments_snapshots.md) · SUBTREE Operation


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
| `symlink_policy` | How to handle symlinks that escape the new subtree root. Default `COLLAPSE_ESCAPING`. |

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
2. Rebases paths relative to the new root (removes the subtree prefix)
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

The `fileChunkSizeBytes` field IS preserved in the output manifest, ensuring file chunk size settings are maintained through subtree operations.

**Note:** The `parentManifestHash` field is NOT preserved in the output manifest. Since the subtree operation changes the root path, the original parent manifest hash would be invalid for the new subtree manifest.

**Symlink Handling:**

When re-rooting a manifest, symlinks that were previously "within root" may now "escape" the new subtree root. The `symlink_policy` parameter controls how these are handled:

| Policy | Symlinks within subtree | Symlinks escaping subtree |
|--------|------------------------|---------------------------|
| `COLLAPSE_ALL` | Collapsed | Collapsed |
| `COLLAPSE_ESCAPING` | Preserved (target rebased) | Collapsed |
| `EXCLUDE_ALL` | Excluded | Excluded |
| `EXCLUDE_ESCAPING` | Preserved (target rebased) | Excluded |

`PRESERVE` and `TRANSITIVE_INCLUDE_TARGETS` are not supported—escaping symlinks cannot be represented in relative-path output.

Unlike COLLECT, SUBTREE operates purely on manifest data without filesystem access. When collapsing, it looks up targets in the original manifest.

See [snapshot_symlink_handling.md](snapshot_symlink_handling.md) for detailed documentation on collapsing behavior, target rebasing, and cycle handling.

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

