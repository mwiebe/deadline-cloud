# Manifest Classes

*← [Back to Job Attachments Snapshots](../job_attachments_snapshots.md)*

**Location:** `_manifest.py`

This document describes the manifest classes used to represent directory tree snapshots and diffs in memory.

## Overview

The library provides four concrete manifest classes organized by two dimensions:

| | Snapshot (full capture) | Diff (changes only) |
|---|---|---|
| **Relative paths** | `Snapshot` | `SnapshotDiff` |
| **Absolute paths** | `AbsSnapshot` | `AbsSnapshotDiff` |

- **Relative-path manifests** (`Snapshot`, `SnapshotDiff`) are portable across systems
- **Absolute-path manifests** (`AbsSnapshot`, `AbsSnapshotDiff`) are required for filesystem operations

## Class Hierarchy

```
_BaseManifest (common fields and methods)
├── AbsSnapshot     (_AbsManifestMixin, _SnapshotManifestMixin)
├── AbsSnapshotDiff (_AbsManifestMixin, _DiffManifestMixin)
├── Snapshot        (_RelManifestMixin, _SnapshotManifestMixin)
└── SnapshotDiff    (_RelManifestMixin, _DiffManifestMixin)
```

Validation mixins:
- `_AbsManifestMixin` - validates all paths are absolute
- `_RelManifestMixin` - validates all paths are relative
- `_SnapshotManifestMixin` - validates no deleted entries exist
- `_DiffManifestMixin` - validates diff-specific constraints

## Type Aliases

```python
# By path style
RelManifest = Union[Snapshot, SnapshotDiff]       # Any relative-path manifest
AbsManifest = Union[AbsSnapshot, AbsSnapshotDiff] # Any absolute-path manifest

# By manifest type
AnySnapshot = Union[AbsSnapshot, Snapshot]        # Any snapshot (full capture)
AnyDiff = Union[AbsSnapshotDiff, SnapshotDiff]    # Any diff (changes only)

# All manifest types
AnyManifest = Union[AbsSnapshot, AbsSnapshotDiff, Snapshot, SnapshotDiff]
```

## Manifest Fields

All four manifest classes share these fields from `_BaseManifest`:

| Field | Type | Description |
|-------|------|-------------|
| `hashAlg` | `HashAlgorithm` | Hashing algorithm for file content (default: `XXH128`) |
| `files` | `List[ManifestFilePath]` | List of file/symlink entries |
| `dirs` | `List[ManifestDirectoryPath]` | List of directory entries (for empty dirs) |
| `totalSize` | `int` | Total size of all files in bytes |
| `parentManifestHash` | `Optional[str]` | Hash of parent manifest (for diffs) |
| `fileChunkSizeBytes` | `int` | Chunk size for large file hashing |

### Constructor

```python
def __init__(
    self,
    *,
    hash_alg: HashAlgorithm,
    files: List[ManifestFilePath],
    total_size: int = 0,
    dirs: Optional[List[ManifestDirectoryPath]] = None,
    parent_manifest_hash: Optional[str] = None,
    file_chunk_size_bytes: int = DEFAULT_FILE_CHUNK_SIZE,
) -> None
```

### Methods

| Method | Description |
|--------|-------------|
| `validate()` | Run all validation checks; raises `ManifestDecodeValidationError` on failure |
| `clear_hashes()` | Set `hash` and `chunkhashes` to None for all regular files |
| `get_default_hash_alg()` | Class method returning `HashAlgorithm.XXH128` |

## Entry Classes

### ManifestFilePath

Represents a file, symlink, or deletion marker:

| Field | Type | Description |
|-------|------|-------------|
| `path` | `str` | File path (relative or absolute depending on manifest type) |
| `hash` | `Optional[str]` | Content hash (None if unhashed, chunked, symlink, or deleted) |
| `size` | `Optional[int]` | File size in bytes (None for symlinks and deleted entries) |
| `mtime` | `Optional[int]` | Modification time in microseconds since epoch (None for symlinks and deleted) |
| `chunkhashes` | `Optional[List[str]]` | Per-chunk hashes for large files |
| `symlink_target` | `Optional[str]` | Symlink target path (None for regular files) |
| `runnable` | `bool` | POSIX execute bit (always False on Windows) |
| `deleted` | `bool` | Deletion marker for diff manifests |

#### Entry States

A `ManifestFilePath` can be in one of these states:

| State | `deleted` | `symlink_target` | `hash` | `chunkhashes` | `size`/`mtime` |
|-------|-----------|------------------|--------|---------------|----------------|
| Deleted | True | None | None | None | None |
| Symlink | False | Set | None | None | None |
| Unhashed file | False | None | None | None | Required |
| Hashed (single) | False | None | Set | None | Required |
| Hashed (chunked) | False | None | None | Set | Required |

#### Validation Rules

- **Deleted entries**: Only `path` and `deleted=True` may be set; all other fields must be None/False
- **Symlinks**: `symlink_target` is set; `hash` and `chunkhashes` must be None
- **Regular files**: At most one of `hash`, `chunkhashes`, or `symlink_target` may be set
- **Non-symlink files**: Must have `size` and `mtime` fields

### ManifestDirectoryPath

Represents a directory entry:

| Field | Type | Description |
|-------|------|-------------|
| `path` | `str` | Directory path |
| `deleted` | `bool` | Deletion marker for diff manifests |

#### Implicit vs Explicit Directories

- Parent directories of files/symlinks are implicit—they don't need to be in `dirs`. This makes in-memory
  creation and editing of manifest objects easier. The on-disk file format is more strict, and operations
  that save a manifest file fill in implicit directories as needed to conform.
- Empty directories must be explicitly listed in `dirs`. The v2023 file format does not support empty directories,
  preserving them requires the v2025+ file format.

## Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `DEFAULT_FILE_CHUNK_SIZE` | 256 MB | Default file chunk size for chunked hashing |
| `WHOLE_FILE_CHUNK_SIZE` | -1 | Sentinel: Means to always hash whole file, no file chunks |

## Chunked File Hashing

The `fileChunkSizeBytes` field controls how large files are hashed:

| `fileChunkSizeBytes` | File Chunk Size | Hash Field |
|---------------------|-----------|------------|
| `WHOLE_FILE_CHUNK_SIZE` (-1) | Any | `hash` (whole file) |
| Positive value | ≤ chunk size | `hash` |
| Positive value | > chunk size | `chunkhashes` |

When `chunkhashes` is used:
- Chunk count = `ceil(size / fileChunkSizeBytes)`
- Each chunk hash represents exactly `fileChunkSizeBytes` bytes except the last chunk that may be smaller.

## Path Normalization

All paths are normalized on construction:
- POSIX forward slash `/` is the path separator in all manifest paths
- On Windows, backslashes are converted to forward slashes
- On POSIX, backslashes are preserved, they can be used in both directory names and filenames.
- `.` and `..` components are collapsed via `posixpath.normpath`
- Windows long-path prefix (`//?/`) is stripped

User code can modify an in-memory manifest object, so all the operations that accept
manifests must be robust and perform this normalization as well.

### Absolute Path Examples

```
/home/user/project/file.txt     # POSIX
C:/Users/name/project/file.txt  # Windows drive
//server/share/path/file.txt    # Windows UNC
```

### Relative Path Examples

```
project/assets/texture.png
scene/models/character.fbx
```

## SymlinkPolicy Enum

Controls symlink handling during COLLECT:

| Policy | Description |
|--------|-------------|
| `COLLAPSE_ESCAPING` | Follow symlinks pointing outside root; preserve internal symlinks |
| `COLLAPSE_ALL` | Follow all symlinks, turning them into files/directories |
| `EXCLUDE_ESCAPING` | Exclude symlinks pointing outside root; preserve internal symlinks |
| `EXCLUDE_ALL` | Exclude all symlinks from the manifest |
| `PRESERVE` | Keep all symlinks as-is (absolute paths only) |
| `TRANSITIVE_INCLUDE_TARGETS` | Keep symlinks and add their targets (absolute paths only) |

## Usage Examples

### Creating a Snapshot

```python
from deadline.job_attachments._snapshots import (
    AbsSnapshot,
    ManifestFilePath,
    ManifestDirectoryPath,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm

# Create an absolute-path snapshot
snapshot = AbsSnapshot(
    hash_alg=HashAlgorithm.XXH128,
    files=[
        ManifestFilePath(
            path="/home/user/project/scene.blend",
            size=1024000,
            mtime=1706000000000000,  # microseconds
        ),
    ],
    dirs=[
        ManifestDirectoryPath(path="/home/user/project/empty_dir"),
    ],
    total_size=1024000,
)

# Validate the manifest
snapshot.validate()
```

### Creating a Diff

```python
from deadline.job_attachments._snapshots import (
    SnapshotDiff,
    ManifestFilePath,
)

# Create a relative-path diff
diff = SnapshotDiff(
    hash_alg=HashAlgorithm.XXH128,
    files=[
        # Modified file
        ManifestFilePath(
            path="assets/texture.png",
            hash="abc123...",
            size=2048,
            mtime=1706100000000000,
        ),
        # Deleted file
        ManifestFilePath(
            path="old/unused.txt",
            deleted=True,
        ),
    ],
    parent_manifest_hash="parent_hash_value",
)
```

### Clearing Hashes for Re-upload

```python
# After files have been modified, clear hashes and re-hash
manifest.clear_hashes()
hashed = hash_abs_manifest(manifest, hash_cache)
```

## Validation

Manifests validate constraints on construction. After modifying fields directly, call `validate()` to check constraints:

```python
# Modify manifest
manifest.files.append(new_entry)

# Re-validate
manifest.validate()  # Raises ManifestDecodeValidationError if invalid
```

### Validation Checks by Manifest Type

| Manifest Type | Path Validation | Entry Validation |
|---------------|-----------------|------------------|
| `AbsSnapshot` | All paths absolute | No deleted entries |
| `AbsSnapshotDiff` | All paths absolute | Deleted entries allowed |
| `Snapshot` | All paths relative | No deleted entries |
| `SnapshotDiff` | All paths relative | Deleted entries allowed |
