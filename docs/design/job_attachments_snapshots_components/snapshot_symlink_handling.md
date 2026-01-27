# Symlink Handling

*← [Back to Job Attachments Snapshots](../job_attachments_snapshots.md)*

This document describes how symlinks are represented in manifests and how the `SymlinkPolicy` enum controls symlink handling across operations.

## Symlink Representation in Manifests

Symlinks are stored as `ManifestFilePath` entries with the `symlink_target` field set:

| Field | Value |
|-------|-------|
| `path` | The symlink's path |
| `symlink_target` | The target path (see storage format below) |
| `hash` | Always `None` |
| `size` | Always `None` |
| `mtime` | Always `None` |

### Target Storage Format

Symlink targets are stored **relative to the manifest root**, not relative to the symlink location (unlike POSIX filesystem symlinks). For example, a symlink at `assets/textures/current` pointing to `assets/shared/latest.png` stores the target as `assets/shared/latest.png`, not as `../shared/latest.png`.

This design simplifies manifest operations since targets can be looked up directly in the manifest's path index without resolving relative paths from the symlink's location.

## SymlinkPolicy Enum

The `SymlinkPolicy` enum controls how symlinks are handled during operations:

| Policy | Description |
|--------|-------------|
| `COLLAPSE_ESCAPING` | Preserve symlinks whose targets are included; collapse symlinks whose targets escape. |
| `COLLAPSE_ALL` | Collapse all symlinks to files/directories. |
| `EXCLUDE_ESCAPING` | Preserve symlinks whose targets are included; exclude symlinks whose targets escape. |
| `EXCLUDE_ALL` | Exclude all symlinks from the result. |
| `PRESERVE` | Keep all symlinks as-is. Only valid for absolute-path manifests. |
| `TRANSITIVE_INCLUDE_TARGETS` | Keep symlinks and add their targets to the manifest. Only valid for COLLECT. |

### Policy Support by Operation

| Policy | COLLECT | SUBTREE | PARTITION | DOWNLOAD |
|--------|---------|---------|-----------|----------|
| `COLLAPSE_ESCAPING` | ✓ (default) | ✓ (default) | ✓ (default) | — |
| `COLLAPSE_ALL` | ✓ | ✓ | ✓ | — |
| `EXCLUDE_ESCAPING` | ✓ | ✓ | — | — |
| `EXCLUDE_ALL` | ✓ | ✓ | ✓ | ✓ |
| `PRESERVE` | ✓ | — | — | ✓ (default) |
| `TRANSITIVE_INCLUDE_TARGETS` | ✓ | — | — | — |

## Escaping vs Non-Escaping Symlinks

A symlink is "escaping" if its target is outside the relevant root path:

- **COLLECT:** Target is outside the collected directories/files
- **SUBTREE:** Target is outside the new subtree root
- **PARTITION:** Target is outside the partition's root

Non-escaping symlinks point to paths that are (or will be) part of the manifest.

## Escaping Symlink Detection (COLLECT)

For `COLLAPSE_ESCAPING` and `EXCLUDE_ESCAPING` policies, COLLECT uses a two-pass algorithm:

**Pass 1 - Build the collected set:**
1. Walk all directories and collect all non-symlink files and directories
2. Collect all non-symlink files from `filenames` and `optional_filenames`
3. Defer all symlinks for later processing
4. Result: a set of all collected paths

**Pass 2 - Process deferred symlinks:**

For each deferred symlink, check if its target is within the collected set:

```python
def is_escaping(symlink_target: Path, collected_set: Set[str]) -> bool:
    target_posix = symlink_target.as_posix()
    # Direct match
    if target_posix in collected_set:
        return False
    # Prefix match - target is under a collected directory
    for collected_path in collected_set:
        if target_posix.startswith(collected_path + "/"):
            return False
    return True
```

**Why two passes?**

A single-pass approach cannot correctly identify escaping symlinks because the full collected set isn't known until all paths are visited:

```
/project/
├── data/
│   └── file.txt
└── link -> data/file.txt    # Is this escaping?
```

If `link` is processed before `data/file.txt`, we don't yet know that `data/file.txt` will be collected.

## Collapsing Behavior

When a symlink is collapsed, it is replaced with the actual content at its target:

**File target:**
- The symlink entry becomes a regular file entry
- Uses the symlink's path but the target's metadata (hash, size, mtime, runnable)

**Directory target:**
- The symlink is replaced with the entire directory tree at the target
- All entries appear under the symlink's path (path translation)
- Nested symlinks are handled recursively

**Example - Directory symlink collapsing:**

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

## Transitive Include Targets (COLLECT only)

The `TRANSITIVE_INCLUDE_TARGETS` policy preserves all symlinks and recursively collects their targets:

1. During collection, when a symlink is encountered:
   - Preserve the symlink entry with its absolute target
   - Queue the target path for transitive collection

2. Process queued targets:
   - File target: add to manifest
   - Symlink target: preserve and queue its target
   - Directory target: walk it, preserving nested symlinks and queuing their targets

3. Continue until no new targets are queued (fixed-point)

**Caller responsibility:** The resulting manifest may contain paths from anywhere on the filesystem. Validate that paths are within acceptable boundaries before use.

## Cycle Detection

Symlink cycles occur when following symlinks leads back to a previously visited path:
- Self-referential: `A -> A`
- Direct cycle: `A -> B -> A`
- Longer cycles: `A -> B -> C -> A`

Cycles can also be revealed during collapsing:
```
/root/                          # Being collected
├── link_to_external -> /ext    # Escaping, will be collapsed
/ext/
└── link_back -> /root          # Points back - cycle!
```

**Cycle handling by policy:**

| Policy | Behavior |
|--------|----------|
| `PRESERVE` | No recursion; symlinks recorded as-is |
| `EXCLUDE_ALL` | No recursion; all symlinks skipped |
| `EXCLUDE_ESCAPING` | Cycles in escaping symlinks skipped |
| `COLLAPSE_ALL` | Cycles detected; cyclic symlink skipped with warning |
| `COLLAPSE_ESCAPING` | Cycles detected when collapsing escaping symlinks |
| `TRANSITIVE_INCLUDE_TARGETS` | Cycles detected; cyclic target skipped with warning |

When a cycle is detected:
1. A warning is logged identifying the cyclic symlink
2. The cyclic symlink is skipped
3. Collection continues with non-cyclic entries

## SUBTREE Symlink Handling

SUBTREE operates on manifest data without filesystem access. When re-rooting a manifest, symlinks that were "within root" may now "escape" the new subtree root.

**Key differences from COLLECT:**
- Collapsing looks up targets in the manifest, not the filesystem
- Missing targets (e.g., already collapsed during COLLECT) are excluded with a warning
- `PRESERVE` and `TRANSITIVE_INCLUDE_TARGETS` are not supported (escaping symlinks cannot be represented in relative-path output)

**Target rebasing:** Preserved symlinks have their targets rebased by stripping the subtree prefix:

```
Original: assets/textures/alt -> assets/textures/variants/dark.png
SUBTREE("assets/textures")
Result:   alt -> variants/dark.png
```

## DOWNLOAD Symlink Handling

DOWNLOAD creates symlinks on the filesystem. Only `PRESERVE` (default) and `EXCLUDE_ALL` are supported.

**Symlink ordering:** For chained symlinks (`A -> B -> C`), targets are created before symlinks that point to them via topological sorting of the dependency graph.

## Choosing a Policy

| Use Case | Recommended Policy |
|----------|-------------------|
| Job submission (portable manifest) | `COLLAPSE_ESCAPING` (default) |
| Debug snapshot (complete capture) | `COLLAPSE_ALL` or `TRANSITIVE_INCLUDE_TARGETS` |
| Exclude all symlinks | `EXCLUDE_ALL` |
| Preserve symlink structure (absolute paths only) | `PRESERVE` |
