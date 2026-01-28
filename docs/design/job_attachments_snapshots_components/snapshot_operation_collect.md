# COLLECT Operation: `collect_abs_snapshot()`

[Job Attachments Snapshots](../job_attachments_snapshots.md) · COLLECT Operation


**Location:** `_collect_abs_snapshot.py`

Collects provided lists of paths into a manifest with absolute paths, WITHOUT computing hashes:

```python
def collect_abs_snapshot(
    directories: List[Path | str],
    filenames: List[Path | str],
    *,
    optional_filenames: Optional[List[Path | str]] = None,
    symlink_policy: SymlinkPolicy = SymlinkPolicy.COLLAPSE_ESCAPING,
    file_chunk_size_bytes: Optional[int] = None,
) -> AbsSnapshot:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `directories` | (positional) List of directory paths whose full contents are collected. All paths must exist and be directories. Empty directories are included in the manifest. |
| `filenames` | (positional) List of file/symlink paths that must exist. Raises `FileNotFoundError` if any file does not exist. |
| `optional_filenames` | List of file/symlink paths to include if they exist. Missing files are silently ignored. |
| `symlink_policy` | How to handle symlinks during collection (see below). Default `COLLAPSE_ESCAPING`. |
| `file_chunk_size_bytes` | Chunk size for large file hashing. `None` = use `DEFAULT_FILE_CHUNK_SIZE` (256MB). `WHOLE_FILE_CHUNK_SIZE` (-1) = no chunking. Positive int = chunk size in bytes. |

## Symlink Policy Options

| Policy | Description |
|--------|-------------|
| `COLLAPSE_ESCAPING` | Preserve symlinks whose targets are included; collapse escaping symlinks. |
| `COLLAPSE_ALL` | Collapse all symlinks to files/directories. |
| `PRESERVE` | Keep all symlinks as-is with absolute targets. |
| `TRANSITIVE_INCLUDE_TARGETS` | Keep symlinks and add their targets to the manifest. |
| `EXCLUDE_ALL` | Skip all symlinks. |
| `EXCLUDE_ESCAPING` | Preserve symlinks whose targets are included; exclude escaping symlinks. |

See [snapshot_symlink_handling.md](snapshot_symlink_handling.md) for detailed documentation on symlink policies, the escaping detection algorithm, collapsing behavior, and cycle handling.

## Validation Rules

| Condition | Behavior |
|-----------|----------|
| File in `filenames` does not exist | Raises `FileNotFoundError` |
| File in `optional_filenames` does not exist | Silently ignored |
| Directory in `directories` does not exist | Raises `FileNotFoundError` |
| Path in `directories` is not a directory | Raises `ValueError` |
| Path in `filenames` is not a file or symlink | Raises `ValueError` |

## Key Implementation Details

- Files have `hash=None` to indicate hashing is needed
- Symlinks have `symlink_target` set as absolute paths (no hash needed)
- All paths in the manifest are absolute
- Directories are included in the manifest
- Useful for intermediate in-memory processing or collecting from multiple locations

## Examples

**Collecting from multiple directories:**

```python
from deadline.job_attachments._snapshots import (
    collect_abs_snapshot,
    SymlinkPolicy,
)

# Collect files from different locations using absolute paths (default: COLLAPSE_ESCAPING)
manifest = collect_abs_snapshot(
    ["/data/shared/models", "/data/shared/textures"],  # directories (positional)
    ["/home/user/project/scene.blend"],                 # filenames (positional)
    optional_filenames=["/home/user/project/cache.bin"],  # Included if exists
)

# Paths in manifest are absolute
for entry in manifest.files[:2]:
    print(f"  {entry.path}")  # e.g., "/data/shared/models/car.obj"
```

**Escaping symlinks are collapsed by default:**

```python
from deadline.job_attachments._snapshots import (
    collect_abs_snapshot
)

# Collect with COLLAPSE_ESCAPING (default behavior)
# Symlinks pointing within the collected paths are preserved
# Symlinks pointing outside are collapsed to files/directories
manifest = collect_abs_snapshot(
    ["/projects/my_scene"],  # directories
    [],                       # filenames (empty list)
)

# Non-escaping symlinks have absolute targets
for entry in manifest.files:
    if entry.symlink_target:
        print(f"  symlink: {entry.path} -> {entry.symlink_target}")
        # e.g., symlink: /projects/my_scene/link.txt -> /projects/my_scene/target.txt
```

## Helper Functions

- `_create_unhashed_file_entry()` - Creates file entry with `hash=None` and metadata
- `_handle_symlink()` - Handles symlink according to policy
