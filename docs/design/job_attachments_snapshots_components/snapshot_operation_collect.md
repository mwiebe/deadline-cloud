# COLLECT Operation: `collect_abs_snapshot()`

*← [Back to Job Attachments Snapshots](../job_attachments_snapshots.md)*


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
| `COLLAPSE_ESCAPING` | Preserve symlinks whose targets are within the collected paths; collapse symlinks whose targets are outside (escaping symlinks) to files/directories. (default) |
| `COLLAPSE_ALL` | Follow all symlinks, treating them as files/directories. |
| `PRESERVE` | Keep all symlinks as symlink entries with absolute targets. |
| `TRANSITIVE_INCLUDE_TARGETS` | Keep all symlinks and add their targets to the manifest. |
| `EXCLUDE_ALL` | Skip all symlinks entirely. |
| `EXCLUDE_ESCAPING` | Preserve symlinks whose targets are within the collected paths; exclude symlinks whose targets are outside (escaping symlinks). |

## Symlink Collapsing Behavior

When a symlink is collapsed (via `COLLAPSE_ALL` or `COLLAPSE_ESCAPING`), it is replaced with the actual content at its target location. The behavior depends on whether the target is a file or directory:

*File symlink collapsing:*
- The symlink entry is replaced with a regular file entry
- The file's metadata (size, mtime, runnable) comes from the target file
- The entry's path remains the symlink's path (not the target's path)

*Directory symlink collapsing:*
- The symlink is replaced with the entire directory tree at the target location
- All entries appear under the symlink's path, not the target's path (path translation)
- Nested symlinks within the collapsed directory are handled recursively:
  - If a nested symlink points within the same collapsed directory → preserve it with translated target
  - If a nested symlink points outside the collapsed directory → collapse it recursively

*Example - Directory symlink collapsing with nested symlinks:*

```
/project/                        # Being collected
└── assets -> /external/v2       # Directory symlink to collapse

/external/v2/
├── model.obj
├── current -> textures/wood.png # Points within collapsed dir
└── textures/
    ├── wood.png
    └── shared -> /library/tex   # Points outside collapsed dir

/library/tex/
└── metal.png
```

After collapsing `/project/assets`:
- `/project/assets/model.obj` (file)
- `/project/assets/current` → `/project/assets/textures/wood.png` (symlink, target translated)
- `/project/assets/textures/wood.png` (file)
- `/project/assets/textures/shared/metal.png` (file, recursively collapsed)

The nested symlink `current` is preserved because its target is within the collapsed directory (translated from `/external/v2/textures/wood.png` to `/project/assets/textures/wood.png`). The nested symlink `shared` is recursively collapsed because its target escapes the collapsed directory.

## Escaping Symlink Detection Algorithm

For `COLLAPSE_ESCAPING` and `EXCLUDE_ESCAPING` policies, the COLLECT operation uses a two-pass algorithm to determine which symlinks are "escaping" (pointing outside the collected paths):

*Pass 1 - Build the collected set:*
1. Walk all directories and collect all non-symlink files and directories
2. Collect all non-symlink files from `filenames` and `optional_filenames`
3. Defer all symlinks encountered for later processing
4. The result is a set of all collected paths (the "collected set")

*Pass 2 - Process deferred symlinks:*
For each deferred symlink, resolve its target (without following symlink chains) and check if the target is within the collected set:

```python
def is_escaping(symlink_target: Path, collected_set: Set[str]) -> bool:
    target_posix = symlink_target.as_posix()
    # Direct match - target path itself was collected
    if target_posix in collected_set:
        return False
    # Prefix match - target is under a collected directory
    for collected_path in collected_set:
        if target_posix.startswith(collected_path + "/"):
            return False
    return True
```

- If the target is in the collected set → preserve the symlink
- If the target escapes → collapse (inline the target's contents) or exclude, based on policy

*Why two passes?*

A single-pass approach cannot correctly identify escaping symlinks because the full collected set isn't known until all paths are visited. Consider:

```
/project/
├── data/
│   └── file.txt
└── link -> data/file.txt    # Is this escaping?
```

If we process `link` before `data/file.txt`, we don't yet know that `data/file.txt` will be collected. The two-pass approach ensures we have the complete picture before making escaping decisions.

## Transitive Include Targets Algorithm

The `TRANSITIVE_INCLUDE_TARGETS` policy preserves all symlinks and recursively collects their targets into the manifest. This ensures the manifest contains all data reachable through symlinks, regardless of where those targets are located on the filesystem.

*Purpose and caller responsibility:*

This policy collects all transitively reachable paths without restriction. The resulting manifest may contain paths from anywhere on the filesystem (e.g., `/usr/lib`, `/home/other_user`, etc.). It is the caller's responsibility to validate that the resulting paths are within acceptable boundaries before using the manifest.

*Algorithm:*

1. During the main collection pass, when a symlink is encountered:
   - Preserve the symlink entry with its absolute target
   - Queue the target path for transitive collection

2. After the main pass, process queued transitive targets:
   - If target is a file: add it to the manifest
   - If target is a symlink: preserve it and queue its target (recursive)
   - If target is a directory: walk it, preserving any nested symlinks and queuing their targets

3. Continue until no new targets are queued (fixed-point)

*Example:*

```
/project/                    # Being collected
└── link1 -> /external/data

/external/
└── data/
    ├── file.txt
    └── link2 -> /other/resource

/other/
└── resource.bin
```

Result with `TRANSITIVE_INCLUDE_TARGETS`:
- `/project/link1` (symlink → `/external/data`)
- `/external/data/file.txt` (file)
- `/external/data/link2` (symlink → `/other/resource`)
- `/other/resource.bin` (file)

All paths reachable through symlinks are included, preserving the symlink structure.

## Symlink Cycle Handling

Symlink cycles occur when following symlinks leads back to a previously visited path. Examples:
- Self-referential: `A -> A` (length 1)
- Direct cycle: `A -> B -> A` (length 2)
- Longer cycles: `A -> B -> C -> A` (length 3+)

Cycles can also be revealed during collapsing when symlinks point to intermediate directories:
```
/root/                          # Being collected
├── link_to_external -> /ext    # Escaping symlink, will be collapsed
/ext/
└── link_back -> /root          # Points back to collected root - cycle!
```
In this case, `/root` is being collected, `link_to_external` escapes so it's collapsed (contents
inlined), and when processing `link_back` inside `/ext`, the cycle is detected because `/root`
is already being visited.

The COLLECT operation detects symlink cycles and handles them gracefully:

| Policy | Cycle Behavior |
|--------|----------------|
| `PRESERVE` | No recursion needed; symlinks are recorded as-is with their targets |
| `EXCLUDE_ALL` | No recursion needed; all symlinks are skipped |
| `EXCLUDE_ESCAPING` | Cycles in escaping symlinks are skipped (non-escaping preserved) |
| `COLLAPSE_ALL` | Cycles detected during traversal; cyclic symlink skipped with warning |
| `COLLAPSE_ESCAPING` | Cycles detected when collapsing escaping symlinks; cyclic symlink skipped with warning |
| `TRANSITIVE_INCLUDE_TARGETS` | Cycles detected during transitive collection; cyclic target skipped with warning |

When a cycle is detected:
1. A warning is logged identifying the cyclic symlink
2. The cyclic symlink is skipped to prevent infinite recursion
3. Collection continues with non-cyclic parts of the directory tree
4. Files and directories already collected before the cycle are preserved

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
