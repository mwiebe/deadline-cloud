# FILTER Operation: `filter_manifest()`

[Job Attachments Snapshots](../job_attachments_snapshots.md) · FILTER Operation


**Location:** `_filter_manifest.py`

Applies a filter to manifest entries, returning a new manifest with only matching entries:

```python
def filter_manifest(
    manifest: Manifest,
    entry_filter: Callable[[Union[ManifestFilePath, ManifestDirectoryPath]], bool],
) -> Manifest:
```

**Filter interface:**

The filter is a callable that takes a manifest entry and returns `True` to keep it:

```python
# Example: Custom filter for large files only
def large_files_only(entry):
    if isinstance(entry, ManifestFilePath) and entry.size is not None:
        return entry.size > 1_000_000  # > 1MB
    return False

filtered = filter_manifest(manifest, large_files_only)
```

**Built-in filter: `IncludeExcludePathsFilter`**

Implements classic include/exclude glob pattern matching:

```python
filter = IncludeExcludePathsFilter(
    include=["*.blend", "textures/*"],
    exclude=["backup/*", "*.tmp"]
)
filtered = filter_manifest(manifest, filter)
```

Pattern matching rules:
- If include patterns specified, path must match at least one
- Path must not match any exclude pattern
- Uses `fnmatch` for glob-style matching

**Critical for diff computation:**

When computing a diff of filtered manifests, BOTH parent and current manifests must be filtered with the SAME filter before comparison.
This ensures deletions are computed correctly within the filtered view.

**Example:**

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

# Or use a custom filter function
def python_files_only(entry):
    return entry.path.endswith(".py")

py_manifest = filter_manifest(manifest, python_files_only)
```

