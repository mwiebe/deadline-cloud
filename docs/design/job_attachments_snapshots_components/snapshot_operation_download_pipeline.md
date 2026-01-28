# DOWNLOAD Pipeline Architecture

*← [Back to DOWNLOAD Operation](snapshot_operation_download.md)*

This document describes the internal pipeline architecture, threading model, atomicity guarantees, and S3-specific behavior of the DOWNLOAD operation.

## Parallel Download Architecture

The DOWNLOAD operation uses a `ThreadPoolExecutor` with callbacks to coordinate parallel downloads:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    DOWNLOAD PIPELINE COORDINATOR                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Regular Files:                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  1. executor.submit() → pre-allocate temp file + handle conflicts│   │
│  │  2. For large files (≥ 2 × part size): parallel part downloads   │   │
│  │     For small files: single get_object request                   │   │
│  │     └─► Each part decrements atomic counter on completion        │   │
│  │  3. Last part (counter reaches 0) → atomic replace + mtime       │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  Chunked Files (file chunks stored separately in data cache):           │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  1. executor.submit() → pre-allocate temp file + handle conflicts│   │
│  │     └─► Fan-out: submit all file chunk downloads to executor     │   │
│  │  2. Each file chunk downloads in parallel                        │   │
│  │     - Large file chunks: parallel part downloads                 │   │
│  │     - Small file chunks: single get_object request               │   │
│  │     └─► Each file chunk decrements atomic counter on completion  │   │
│  │  3. Last file chunk (counter reaches 0) → atomic replace + mtime │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  All tasks share a single ThreadPoolExecutor(max_workers=N)             │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

This architecture maximizes throughput by:
- Downloading multiple files simultaneously (regular and chunked)
- Downloading all file chunks of chunked files in parallel
- Using parallel part downloads for large files/file chunks (S3 byte-range requests)
- Using callbacks instead of async/await for lower overhead

## Interleaved Directory Creation

The operation interleaves directory creation with file download submission:

```python
# Directories sorted by path (parents before children)
for dir_path in sorted_dirs:
    create_directory(dir_path)        # Create this directory
    for entry in files_in_dir:        # Submit files in this directory
        pipeline.submit_file(entry)   # Downloads start immediately
```

Benefits:
1. **Avoids redundant directory creation:** Each directory created exactly once
2. **Maximizes parallelism:** Files submitted as soon as parent directory exists

## Atomic File Downloads

All downloads are atomic to ensure target files are never partial or corrupt:

1. **Temporary file creation:** Download to temp file beside target (e.g., `myfile.dat.tmp048df`)
2. **Atomic move:** `os.replace()` atomically moves temp to final location
3. **Error cleanup:** Temp file deleted on any error

| File Type | Behavior |
|-----------|----------|
| Regular file | Pre-allocate temp, download (multi-part for large), atomic move |
| Chunked file | Pre-allocate temp, download all file chunks in parallel, atomic move after all complete |

## S3 Multi-Part Download

For S3 downloads, files and file chunks larger than `2 × multipart_part_size` (default 64MB with 32MB parts) use parallel byte-range requests:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    S3 MULTI-PART DOWNLOAD                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  For a 100MB file with 32MB parts:                                      │
│                                                                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐                    │
│  │ Part 0   │ │ Part 1   │ │ Part 2   │ │ Part 3   │                    │
│  │ 0-32MB   │ │ 32-64MB  │ │ 64-96MB  │ │ 96-100MB │                    │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘                    │
│       │            │            │            │                          │
│       ▼            ▼            ▼            ▼                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │              Pre-allocated Temp File (100MB)                    │    │
│  │  [part 0 region][part 1 region][part 2 region][part 3]          │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                         │
│  Each part uses S3 GetObject with Range header:                         │
│    Range: bytes={start}-{end}  (inclusive on both ends)                 │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## Parallel Chunked File Downloads

Large files exceeding the file chunk size (default 256MB) are stored as multiple file chunks. All file chunks download in parallel:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    CHUNKED FILE DOWNLOAD                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  1. Pre-allocate temp file to exact size (using truncate)               │
│                                                                         │
│  2. Submit all file chunk downloads to shared thread pool:              │
│                                                                         │
│     ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│     │ Chunk 0  │  │ Chunk 1  │  │ Chunk 2  │  │ Chunk N  │              │
│     │ offset=0 │  │ offset=  │  │ offset=  │  │ offset=  │              │
│     │          │  │ 256MB    │  │ 512MB    │  │ N*256MB  │              │
│     └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘              │
│          │             │             │             │                    │
│          ▼             ▼             ▼             ▼                    │
│     ┌─────────────────────────────────────────────────────┐             │
│     │              Temp File (pre-allocated)              │             │
│     │  [chunk 0 region][chunk 1 region][...][chunk N]     │             │
│     └─────────────────────────────────────────────────────┘             │
│                                                                         │
│  3. Wait for all file chunks to complete                                │
│                                                                         │
│  4. Atomic move: os.replace(temp_file, target_file)                     │
│                                                                         │
│  5. Restore mtime from manifest                                         │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

Implementation details:

- **Pre-allocation:** Temp file created with `truncate(size)` (sparse file on supporting filesystems)
- **Parallel writes:** Each file chunk writes to non-overlapping byte range; no locking required
- **Offset calculation:** `offset = chunk_index × file_chunk_size_bytes`
- **Shared thread pool:** File chunk downloads share the same executor as regular files
- **Error handling:** On any file chunk failure, temp file deleted, error propagated
- **Atomicity:** Target file appears only after ALL file chunks complete

## Deletion Ordering

Deletions (from diff manifests) are processed longest-path-first:

1. **Directories only deleted if empty:** Uses `rmdir()`, not recursive delete
2. **Explicit deletion required:** Diff must list all files/subdirs before parent directory
3. **Untracked files preserved:** Non-manifest files prevent directory deletion

Example diff for deleting `/project/old_assets/`:
```
/project/old_assets/model.obj    (deleted=True)
/project/old_assets/texture.png  (deleted=True)
/project/old_assets/             (deleted=True, directory)
```

## Storage Key Format

Files retrieved using content-addressable keys:

| Data Cache Type | Key Format | Example |
|-----------------|------------|---------|
| `S3DataCache` | `{s3_key_prefix}/{hash}.{algorithm}` | `Data/a1b2c3d4...xxh128` |
| `FileSystemDataCache` | `{root_path}/{hash}.{algorithm}` | `/mnt/cache/a1b2c3d4...xxh128` |

## Modification Time Restoration

Downloaded files have `mtime` set to the manifest value, preserving original timestamps.

## Symlink Handling

For chained symlinks, targets are created before symlinks via topological sorting. See [snapshot_symlink_handling.md](snapshot_symlink_handling.md).

## Module Organization

| Module | Description |
|--------|-------------|
| `_download_abs_manifest.py` | Main entry point |
| `_download_abs_manifest_pipeline.py` | Base pipeline class for callback-based downloads |
| `_download_abs_manifest_s3_pipeline.py` | S3-specific download logic (parallel byte-range) |
| `_download_abs_manifest_file_system_pipeline.py` | FileSystem-specific download logic |
