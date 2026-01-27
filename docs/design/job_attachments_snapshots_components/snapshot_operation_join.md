# JOIN Operation: `join_manifest()`

**Location:** `_join_manifest.py`

Joins a prefix to all paths in a manifest, producing a new manifest with prefixed paths:

```python
def join_manifest(
    manifest: RelManifest,
    prefix: str,
) -> AnyManifest:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | The source manifest with relative paths (`Snapshot` or `SnapshotDiff`) |
| `prefix` | Path prefix to join to all paths (relative or absolute) |

**Conceptual Model:**

JOIN is the inverse of SUBTREE. While SUBTREE strips a prefix from paths (re-rooting to a subdirectory), JOIN adds a prefix to paths (re-rooting to a parent directory).

```
Original manifest (relative paths):
  wood.png
  metal.png
  current -> wood.png

JOIN(manifest, "/projects/scene/assets/textures") produces:

New manifest (absolute paths):
  /projects/scene/assets/textures/wood.png
  /projects/scene/assets/textures/metal.png
  /projects/scene/assets/textures/current -> /projects/scene/assets/textures/wood.png
```

**Path Style Behavior:**

| Prefix Type | Input Paths | Output Paths |
|-------------|-------------|--------------|
| Relative (`assets/textures`) | Relative | Relative (prefixed) |
| Absolute (`/projects/scene`) | Relative | Absolute |

**What Gets Prefixed:**

- File paths (`entry.path`)
- Directory paths (`dir.path`)
- Symlink targets (`entry.symlink_target`)

**What Gets Preserved:**

- `fileChunkSizeBytes` - Chunk size settings are preserved
- `totalSize` - Total size is preserved
- All file metadata (hash, size, mtime, runnable, chunkhashes)

**What Does NOT Get Preserved:**

- `parentManifestHash` - Since joining a prefix changes the root path structure, the original parent manifest hash is no longer valid for the new paths. The output manifest has `parentManifestHash=None`.

**Example - Converting Relative to Absolute:**

```python
from deadline.job_attachments._snapshots import join_manifest
from deadline.job_attachments.asset_manifests.decode import decode_manifest

# Load a manifest with relative paths
with open("textures.manifest") as f:
    manifest = decode_manifest(f.read())

# Join with absolute prefix to get absolute paths
absolute_manifest = join_manifest(manifest, "/projects/scene/assets/textures")

for entry in absolute_manifest.files:
    print(entry.path)  # "/projects/scene/assets/textures/wood.png", etc.
```

**Example - Combining Multiple Manifests:**

```python
# Load manifests from different roots
textures = decode_manifest(read_file("textures.manifest"))
models = decode_manifest(read_file("models.manifest"))
scripts = decode_manifest(read_file("scripts.manifest"))

# Join each to its absolute root
textures_abs = join_manifest(textures, "/projects/scene/assets/textures")
models_abs = join_manifest(models, "/projects/scene/assets/models")
scripts_abs = join_manifest(scripts, "/projects/scene/scripts")

# Compose into a single manifest representing all data
combined = compose_manifests([textures_abs, models_abs, scripts_abs])

# Now 'combined' has all files with absolute paths for unified processing
```

**Use Cases:**

1. **Unified download:** Join manifests to absolute paths, compose them, then download all files from S3 in one operation
2. **Path normalization:** Convert relative manifests to absolute for consistent processing
3. **Manifest merging:** Prepare manifests from different roots for composition
4. **Inverse of SUBTREE:** Restore original paths after subtree extraction

**Relationship to SUBTREE:**

JOIN and SUBTREE are inverse operations:

| Operation | Input | Output | Path Transformation |
|-----------|-------|--------|---------------------|
| SUBTREE | Manifest + subtree path | Manifest | Strips prefix from paths |
| JOIN | Manifest + prefix | Manifest | Adds prefix to paths |

```python
# These operations are inverses (for paths within the subtree)
original = join_manifest(subtree_manifest, "assets/textures")
back_to_subtree = subtree_manifest(original, "assets/textures")
# back_to_subtree has the same paths as subtree_manifest
```

