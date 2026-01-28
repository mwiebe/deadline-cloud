# FILTER Operation: `filter_manifest()`

[Job Attachments Snapshots](../job_attachments_snapshots.md) · FILTER Operation

**Location:** `_filter_manifest.py`

Applies a filter to manifest entries, returning a new manifest with only matching entries:

```python
def filter_manifest(
    manifest: M,
    entry_filter: Callable[[Union[ManifestFilePath, ManifestDirectoryPath]], bool],
) -> M:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `manifest` | The manifest to filter (any manifest type: `AbsSnapshot`, `Snapshot`, `AbsSnapshotDiff`, `SnapshotDiff`) |
| `entry_filter` | A callable that takes a manifest entry (`ManifestFilePath` or `ManifestDirectoryPath`) and returns `True` to keep it |

## Returns

A new manifest of the same type as the input, containing only entries that pass the filter. The returned manifest has:
- `totalSize` recomputed from the filtered entries
- `parentManifestHash` preserved from the input
- `fileChunkSizeBytes` preserved from the input

## Entry Type Handling

| Entry Type | Behavior |
|------------|----------|
| Regular file | Passed to filter; included if filter returns `True` |
| Symlink | Passed to filter; included if filter returns `True` |
| Deleted marker | Passed to filter; included if filter returns `True` |
| Directory | Passed to filter; included if filter returns `True` |

## Filter Interface

The filter is a callable that receives a manifest entry and returns a boolean:

```python
Callable[[Union[ManifestFilePath, ManifestDirectoryPath]], bool]
```

The filter has access to all entry fields:
- `path` - The entry's path (always available)
- `size`, `mtime`, `hash`, `chunkhashes` - For regular files
- `symlink_target` - For symlinks
- `deleted` - For deletion markers
- `runnable` - Execute bit (POSIX only)

## Built-in Filter: `IncludeExcludePathsFilter`

A ready-to-use filter implementing classic include/exclude glob pattern matching:

```python
class IncludeExcludePathsFilter:
    def __init__(
        self,
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
    ) -> None:
```

**Parameters:**

| Parameter | Description |
|-----------|-------------|
| `include` | Glob patterns for paths to include. Empty or `None` means include all paths. |
| `exclude` | Glob patterns for paths to exclude. Empty or `None` means exclude nothing. |

**Pattern Matching Rules:**

| Rule | Description |
|------|-------------|
| Include empty | All paths are candidates for inclusion |
| Include specified | Path must match at least one include pattern |
| Exclude | Path must not match any exclude pattern |
| Evaluation order | Include is checked first, then exclude |

**Pattern Syntax:**

Uses Python's `fnmatch` for glob-style matching:

| Pattern | Matches |
|---------|---------|
| `*` | Any characters except path separator |
| `?` | Any single character |
| `[seq]` | Any character in seq |
| `[!seq]` | Any character not in seq |
| `**` | Any characters including path separator (recursive) |

**Examples:**

| Pattern | Matches | Does Not Match |
|---------|---------|----------------|
| `*.blend` | `scene.blend` | `assets/scene.blend` |
| `assets/*` | `assets/model.obj` | `assets/textures/wood.png` |
| `assets/**/*` | `assets/textures/wood.png` | `scene.blend` |
| `*.tmp` | `cache.tmp`, `data.tmp` | `tmp/file.txt` |
| `backup/*` | `backup/old.blend` | `my_backup/old.blend` |

## Using FILTER with DIFF

When using FILTER in combination with DIFF, apply filters consistently to both manifests:

| Scenario | Result |
|----------|--------|
| No filters on either manifest | ✓ Correct diff |
| Same filter on both manifests | ✓ Correct diff (within filtered view) |
| Filter on only one manifest | ✗ Incorrect diff |
| Different filters on each manifest | ✗ Incorrect diff |

Inconsistent filtering produces unsound results. For example, filtering only the current manifest would cause all entries in the unfiltered parent (that don't match the filter) to appear as deletions.

```python
# CORRECT: Same filter applied to both
filter = IncludeExcludePathsFilter(include=["*.blend"], exclude=["backup/*"])
filtered_parent = filter_manifest(parent, filter)
filtered_current = filter_manifest(current, filter)
diff = diff_snapshots(filtered_parent, filtered_current)

# CORRECT: No filters at all
diff = diff_snapshots(parent, current)

# INCORRECT: Filter applied inconsistently
filtered_current = filter_manifest(current, filter)
diff = diff_snapshots(parent, filtered_current)  # Wrong!
```

## Key Implementation Details

- Returns a NEW manifest; the original is not modified
- Manifest type is preserved (`AbsSnapshot` → `AbsSnapshot`, etc.)
- `totalSize` is recomputed by summing sizes of non-deleted, non-symlink file entries
- Empty result is valid (manifest with no entries)
- Filter is called once per entry (files and directories)

## Examples

**Using IncludeExcludePathsFilter:**

```python
from deadline.job_attachments._snapshots import (
    filter_manifest,
    IncludeExcludePathsFilter,
)

# Filter to only include Blender files and textures, excluding backups
filter = IncludeExcludePathsFilter(
    include=["*.blend", "textures/**/*"],
    exclude=["backup/*", "*_old.*"],
)

filtered = filter_manifest(manifest, filter)
print(f"Filtered from {len(manifest.files)} to {len(filtered.files)} entries")
```

**Custom filter for large files:**

```python
from deadline.job_attachments._snapshots import filter_manifest
from deadline.job_attachments._snapshots._manifest import ManifestFilePath

def large_files_only(entry):
    """Keep only files larger than 1MB."""
    if isinstance(entry, ManifestFilePath) and entry.size is not None:
        return entry.size > 1_000_000  # > 1MB
    return False

filtered = filter_manifest(manifest, large_files_only)
```

**Custom filter for specific file types:**

```python
def python_files_only(entry):
    """Keep only Python source files."""
    return entry.path.endswith(".py")

py_manifest = filter_manifest(manifest, python_files_only)
```

**Custom filter excluding symlinks:**

```python
from deadline.job_attachments._snapshots._manifest import ManifestFilePath

def no_symlinks(entry):
    """Exclude all symlinks."""
    if isinstance(entry, ManifestFilePath):
        return entry.symlink_target is None
    return True  # Keep directories

filtered = filter_manifest(manifest, no_symlinks)
```

**Combining with diff workflow:**

```python
from deadline.job_attachments._snapshots import (
    filter_manifest,
    diff_snapshots,
    IncludeExcludePathsFilter,
)

# Load manifests
parent = load_manifest("previous.manifest")
current = collect_and_hash_current_directory()

# Apply same filter to both
filter = IncludeExcludePathsFilter(
    include=["src/**/*", "assets/**/*"],
    exclude=["*.pyc", "__pycache__/*", ".git/*"],
)
filtered_parent = filter_manifest(parent, filter)
filtered_current = filter_manifest(current, filter)

# Compute diff on filtered view
diff = diff_snapshots(
    parent=filtered_parent,
    current=filtered_current,
    parent_manifest_hash=parent_hash,
)
```

## Relationship to Other Operations

| Operation | Relationship |
|-----------|--------------|
| DIFF | Filter both manifests before diffing to get filtered change detection |
| SUBTREE | FILTER keeps/removes entries; SUBTREE extracts and rebases paths |
| COLLECT | FILTER operates on manifests; COLLECT creates manifests from filesystem |

## When to Use FILTER vs SUBTREE

| Use Case | Recommended |
|----------|-------------|
| Keep only certain file types | FILTER |
| Exclude backup directories | FILTER |
| Extract a subdirectory as new root | SUBTREE |
| Remove paths by pattern | FILTER |
| Convert absolute to relative paths | SUBTREE |
