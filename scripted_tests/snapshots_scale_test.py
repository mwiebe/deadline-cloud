# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

#! /usr/bin/env python3
"""
Stress test for the snapshots library with correctness verification.

This script tests the new composable snapshot operations at scale:
1. COLLECT - Directory scanning performance
2. HASH - Hashing performance with/without cache
3. HASH_UPLOAD - Pipeline performance to S3 or filesystem
4. DOWNLOAD - Download performance from S3 or filesystem
5. DIFF - Diff computation between large manifests

It also verifies correctness under high concurrency by checking that
downloaded files exactly match the original source files.

Usage:
  # With S3 (using default AWS credentials)
  python snapshots_scale_test.py --s3-uri s3://amzn-s3-demo-bucket/my-prefix

  # With local filesystem cache only (no AWS required)
  python snapshots_scale_test.py --local-only

  # High concurrency stress test (e.g., 50 workers on 8-core machine)
  python snapshots_scale_test.py --local-only --max-workers 50

  # Custom file counts
  python snapshots_scale_test.py --local-only --small-files 5000 --medium-files 100

  # Profile specific operation with cProfile (saves to <operation>.prof)
  python snapshots_scale_test.py --local-only --cprofile download
  python snapshots_scale_test.py --local-only --cprofile upload

  # Visualize profile with snakeviz
  snakeviz download.prof

Example with all options:
  python snapshots_scale_test.py \\
      --local-only \\
      --small-files 2000 \\
      --medium-files 500 \\
      --large-files 5 \\
      --max-workers 50 \\
      --max-memory 512 \\
      --verify-correctness
"""

from __future__ import annotations

import argparse
import cProfile
import os
import pstats
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

# Import snapshots library
from deadline.job_attachments._snapshots import (
    collect_manifest,
    hash_manifest,
    hash_upload_manifest,
    download_manifest,
    compute_diff_manifest,
    subtree_manifest,
    join_manifest,
    S3DataCache,
    FileSystemDataCache,
    SymlinkPolicy,
)
from deadline.job_attachments._snapshots._manifest import (
    AbsSnapshot,
    DEFAULT_FILE_CHUNK_SIZE,
    WHOLE_FILE_CHUNK_SIZE,
)
from deadline.job_attachments.caches.hash_cache import HashCache
from deadline.job_attachments.caches.s3_check_cache import S3CheckCache
from deadline.job_attachments.progress_tracker import ProgressTracker, ProgressStatus


# =============================================================================
# Configuration
# =============================================================================

# Default file counts
DEFAULT_SMALL_FILES = 400
DEFAULT_MEDIUM_FILES = 40
DEFAULT_LARGE_FILES = 2

# File sizes
SMALL_FILE_SIZE = 1024  # 1 KB
MEDIUM_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
LARGE_FILE_SIZE = 1 * 1024 * 1024 * 1024  # 1 GB

# Directory structure
DEFAULT_SUBDIRECTORIES = 1000
DEFAULT_MAX_NESTING_DEPTH = 10

# Default concurrency
DEFAULT_MAX_WORKERS = 10


def _get_default_max_memory_mb() -> int:
    """Calculate default memory limit matching the internal implementation."""
    import psutil

    mem = psutil.virtual_memory()
    min_memory = 256 * 1024 * 1024  # 256MB
    quarter_of_total = mem.total // 4
    available_minus_1gb = mem.available - (1024 * 1024 * 1024)
    return max(min_memory, quarter_of_total, available_minus_1gb) // (1024 * 1024)


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class TestConfig:
    """Configuration for the stress test."""

    small_files: int
    medium_files: int
    large_files: int
    large_file_size: int  # Size of large files in bytes
    subdirectories: int
    max_nesting_depth: int
    max_workers: int
    max_memory_mb: int
    verify_correctness: bool
    local_only: bool
    s3_bucket: Optional[str]
    s3_prefix: Optional[str]
    skip_download: bool
    setup_only: bool
    keep_files: bool
    chunk_size_bytes: int
    use_hash_cache: bool
    cprofile_operation: Optional[str] = None  # Operation to profile: "upload", "download", etc.


# =============================================================================
# Profiling Support
# =============================================================================


@contextmanager
def profile_operation(
    operation: str,
    enabled_operation: Optional[str],
    print_fn=print,
) -> Iterator[None]:
    """
    Context manager for profiling a specific operation.

    Args:
        operation: Name of the current operation (e.g., "download", "upload")
        enabled_operation: Which operation to profile (from --cprofile arg), or None
        print_fn: Function for printing status messages

    Usage:
        with profile_operation("download", config.cprofile_operation):
            # code to profile
    """
    if enabled_operation is None or operation != enabled_operation:
        yield
        return

    profiler = cProfile.Profile()
    output_file = f"{operation}.prof"
    print_fn(f"  [cProfile] Profiling '{operation}' operation...")

    profiler.enable()
    try:
        yield
    finally:
        profiler.disable()
        profiler.dump_stats(output_file)
        print_fn(f"  [cProfile] Profile saved to: {output_file}")

        # Print top 20 functions by cumulative time
        print_fn("  [cProfile] Top 20 functions by cumulative time:")
        stats = pstats.Stats(profiler)
        stats.sort_stats("cumulative")
        stats.print_stats(20)


@dataclass
class TimingResult:
    """Timing result for an operation."""

    operation: str
    duration_seconds: float
    files_processed: int
    bytes_processed: int
    throughput_mb_s: float
    throughput_paths_s: float = 0.0  # For COLLECT operation (paths/second)


@dataclass
class CorrectnessResult:
    """Result of correctness verification."""

    total_files: int
    verified_files: int
    failed_files: int
    failures: List[str]


# =============================================================================
# Test Data Generation
# =============================================================================


def generate_deterministic_content(seed: int, size: int) -> bytes:
    """
    Generate deterministic content based on seed and size.

    Uses a simple PRNG seeded with the given value to generate
    reproducible content for verification.
    """
    import random

    rng = random.Random(seed)
    return rng.randbytes(size)


def _print_progress_bar(current: int, total: int, prefix: str = "", width: int = 40) -> str:
    """Generate a progress bar string."""
    if total == 0:
        pct = 100.0
    else:
        pct = current / total * 100
    filled = int(width * current // total) if total > 0 else width
    bar = "█" * filled + "░" * (width - filled)
    return f"\r{prefix} [{bar}] {pct:5.1f}% ({current:,}/{total:,})"


def _compute_xxh128(data: bytes) -> str:
    """Compute XXH128 hash of data."""
    import xxhash

    return xxhash.xxh128(data).hexdigest()


def _create_small_file(args: Tuple[Path, int, int]) -> Tuple[str, str, int]:
    """Create a single small file. Returns (relative_path, hash, bytes_written)."""
    file_path, seed, size = args
    content = generate_deterministic_content(seed=seed, size=size)
    content_hash = _compute_xxh128(content)
    if not file_path.exists():
        file_path.write_bytes(content)
    return (file_path.name, content_hash, size)


def create_test_files(
    root_path: Path,
    config: TestConfig,
    print_fn=print,
) -> Tuple[int, int, Dict[str, str]]:
    """
    Create test files for the stress test.

    Subdirectories are created with random nesting depths between 1 and max_nesting_depth.
    Small files are distributed across subdirectories with a skewed distribution:
    - 70% in one "hot" directory
    - 10% in a second directory
    - 20% distributed across remaining directories

    Uses thread pool for parallel small file creation.

    Returns:
        Tuple of (total_files, total_bytes, checksums_dict)    Returns:
        Tuple of (total_files, total_bytes, checksums_dict)
    """
    import random
    import sys
    import xxhash
    from concurrent.futures import ThreadPoolExecutor, as_completed

    print_fn(f"Creating test files in {root_path}...")
    root_path.mkdir(parents=True, exist_ok=True)

    total_files = 0
    total_bytes = 0
    checksums: Dict[str, str] = {}  # rel_path -> xxh128 hash

    # Create subdirectory structure for small files with random nesting
    subdirs: List[Path] = []
    if config.subdirectories > 0:
        small_root = root_path / "small"
        small_root.mkdir(exist_ok=True)

        # Use deterministic RNG for reproducibility
        rng = random.Random(42)

        print_fn(
            f"  Creating {config.subdirectories} subdirectories with random nesting (1-{config.max_nesting_depth})..."
        )

        for i in range(config.subdirectories):
            # Random nesting depth between 1 and max_nesting_depth
            depth = rng.randint(1, max(1, config.max_nesting_depth))

            # Build nested path
            current = small_root
            for d in range(depth):
                current = current / f"d{i:04d}_l{d}"

            current.mkdir(parents=True, exist_ok=True)
            subdirs.append(current)

            # Progress for directory creation
            if (i + 1) % 100 == 0 or i == config.subdirectories - 1:
                sys.stdout.write(_print_progress_bar(i + 1, config.subdirectories, "    Dirs"))
                sys.stdout.flush()
        print_fn("")  # Newline after progress bar

    # Create small files with skewed distribution using thread pool
    if config.small_files > 0 and subdirs:
        print_fn(f"  Creating {config.small_files:,} small files ({SMALL_FILE_SIZE} bytes each)...")
        print_fn(f"    Distributing across {len(subdirs)} subdirectories (skewed: 70%/10%/20%)")

        # Calculate distribution
        hot_dir_count = int(config.small_files * 0.70)  # 70% in hot directory
        warm_dir_count = int(config.small_files * 0.10)  # 10% in warm directory
        remaining_count = config.small_files - hot_dir_count - warm_dir_count  # 20% distributed

        # Assign directories
        hot_dir = subdirs[0] if subdirs else root_path / "small"
        warm_dir = subdirs[1] if len(subdirs) > 1 else hot_dir
        other_dirs = subdirs[2:] if len(subdirs) > 2 else [hot_dir]

        # Build list of (file_path, seed, size) tuples for all small files
        file_tasks: List[Tuple[Path, int, int]] = []
        file_idx = 0

        # Hot directory (70%)
        for i in range(hot_dir_count):
            file_tasks.append((hot_dir / f"small_{file_idx:08d}.dat", file_idx, SMALL_FILE_SIZE))
            file_idx += 1

        # Warm directory (10%)
        for i in range(warm_dir_count):
            file_tasks.append((warm_dir / f"small_{file_idx:08d}.dat", file_idx, SMALL_FILE_SIZE))
            file_idx += 1

        # Distribute remaining files across other directories (20%)
        if other_dirs and remaining_count > 0:
            files_per_dir = remaining_count // len(other_dirs)
            extra_files = remaining_count % len(other_dirs)

            for dir_idx, subdir in enumerate(other_dirs):
                count = files_per_dir + (1 if dir_idx < extra_files else 0)
                for i in range(count):
                    file_tasks.append(
                        (subdir / f"small_{file_idx:08d}.dat", file_idx, SMALL_FILE_SIZE)
                    )
                    file_idx += 1

        # Create files in parallel using thread pool, collecting hashes
        completed = 0
        # Map task index to file path for checksum collection
        task_to_path = {i: task[0] for i, task in enumerate(file_tasks)}

        with ThreadPoolExecutor() as executor:
            future_to_idx = {
                executor.submit(_create_small_file, task): i for i, task in enumerate(file_tasks)
            }

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                file_path = task_to_path[idx]
                rel_path = file_path.relative_to(root_path).as_posix()
                _, content_hash, bytes_written = future.result()

                checksums[rel_path] = content_hash
                total_bytes += bytes_written
                total_files += 1
                completed += 1

                # Update progress bar
                if completed % 1000 == 0 or completed == len(file_tasks):
                    sys.stdout.write(_print_progress_bar(completed, len(file_tasks), "    Small"))
                    sys.stdout.flush()

        print_fn("")  # Newline after progress bar

    elif config.small_files > 0:
        # Fallback: no subdirectories configured, put all in one directory
        small_dir = root_path / "small"
        small_dir.mkdir(exist_ok=True)
        print_fn(f"  Creating {config.small_files:,} small files ({SMALL_FILE_SIZE} bytes each)...")

        file_tasks = [
            (small_dir / f"small_{i:08d}.dat", i, SMALL_FILE_SIZE)
            for i in range(config.small_files)
        ]
        completed = 0
        task_to_path = {i: task[0] for i, task in enumerate(file_tasks)}

        with ThreadPoolExecutor() as executor:
            future_to_idx = {
                executor.submit(_create_small_file, task): i for i, task in enumerate(file_tasks)
            }

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                file_path = task_to_path[idx]
                rel_path = file_path.relative_to(root_path).as_posix()
                _, content_hash, bytes_written = future.result()

                checksums[rel_path] = content_hash
                total_bytes += bytes_written
                total_files += 1
                completed += 1

                if completed % 1000 == 0 or completed == len(file_tasks):
                    sys.stdout.write(_print_progress_bar(completed, len(file_tasks), "    Small"))
                    sys.stdout.flush()

        print_fn("")

    # Create medium files with progress bar
    if config.medium_files > 0:
        medium_dir = root_path / "medium"
        medium_dir.mkdir(exist_ok=True)
        print_fn(
            f"  Creating {config.medium_files:,} medium files "
            f"({MEDIUM_FILE_SIZE // (1024 * 1024)} MB each)..."
        )
        for i in range(config.medium_files):
            file_path = medium_dir / f"medium_{i:04d}.dat"
            content = generate_deterministic_content(seed=10000000 + i, size=MEDIUM_FILE_SIZE)
            content_hash = _compute_xxh128(content)
            if not file_path.exists():
                file_path.write_bytes(content)

            rel_path = file_path.relative_to(root_path).as_posix()
            checksums[rel_path] = content_hash
            total_files += 1
            total_bytes += MEDIUM_FILE_SIZE

            # Update progress bar
            if (i + 1) % 10 == 0 or i == config.medium_files - 1:
                sys.stdout.write(_print_progress_bar(i + 1, config.medium_files, "    Medium"))
                sys.stdout.flush()

        print_fn("")

    # Create large files (chunked) with progress bar - compute hash while writing
    if config.large_files > 0:
        large_dir = root_path / "large"
        large_dir.mkdir(exist_ok=True)
        large_file_size = config.large_file_size
        print_fn(
            f"  Creating {config.large_files} large files "
            f"({large_file_size // (1024 * 1024 * 1024)} GB each)..."
        )
        for i in range(config.large_files):
            file_path = large_dir / f"large_{i:02d}.dat"
            hasher = xxhash.xxh128()

            if not file_path.exists():
                # Write in chunks to avoid memory issues, computing hash as we go
                with open(file_path, "wb") as f:
                    remaining = large_file_size
                    chunk_num = 0
                    total_chunks = (large_file_size + 64 * 1024 * 1024 - 1) // (64 * 1024 * 1024)
                    while remaining > 0:
                        chunk_size = min(64 * 1024 * 1024, remaining)  # 64MB chunks
                        content = generate_deterministic_content(
                            seed=20000000 + i * 1000 + chunk_num, size=chunk_size
                        )
                        hasher.update(content)
                        f.write(content)
                        remaining -= chunk_size
                        chunk_num += 1

                        # Progress for this large file
                        sys.stdout.write(
                            _print_progress_bar(
                                chunk_num, total_chunks, f"    Large {i + 1}/{config.large_files}"
                            )
                        )
                        sys.stdout.flush()
                print_fn("")
            else:
                # File exists, compute hash by reading it
                with open(file_path, "rb") as f:
                    while chunk := f.read(64 * 1024 * 1024):
                        hasher.update(chunk)

            rel_path = file_path.relative_to(root_path).as_posix()
            checksums[rel_path] = hasher.hexdigest()
            total_files += 1
            total_bytes += large_file_size

    print_fn(f"  Total: {total_files:,} files, {total_bytes / (1024 * 1024):,.2f} MB")
    return total_files, total_bytes, checksums


# =============================================================================
# Correctness Verification
# =============================================================================


def compute_file_checksum(file_path: Path) -> str:
    """Compute XXH128 checksum of a file for verification."""
    import xxhash

    hasher = xxhash.xxh128()
    with open(file_path, "rb") as f:
        while chunk := f.read(1024 * 1024):  # 1MB chunks
            hasher.update(chunk)
    return hasher.hexdigest()


def build_checksum_map(root_path: Path, print_fn=print) -> Dict[str, str]:
    """
    Build a map of relative paths to XXH128 checksums for all files.

    This is used to verify correctness after download.
    """
    import sys

    print_fn("Building checksum map for source files...")

    # First count total files for progress bar
    total_files = sum(len(files) for _, _, files in os.walk(root_path))
    print_fn(f"  Found {total_files:,} files to checksum...")

    checksums: Dict[str, str] = {}
    file_count = 0

    for dirpath, _, filenames in os.walk(root_path):
        for filename in filenames:
            file_path = Path(dirpath) / filename
            rel_path = file_path.relative_to(root_path).as_posix()
            checksums[rel_path] = compute_file_checksum(file_path)
            file_count += 1

            # Update progress bar
            if file_count % 1000 == 0 or file_count == total_files:
                sys.stdout.write(_print_progress_bar(file_count, total_files, "  Checksum"))
                sys.stdout.flush()

    print_fn("")  # Newline after progress bar
    print_fn(f"  Total: {len(checksums):,} files checksummed")
    return checksums


def verify_downloaded_files(
    download_root: Path,
    expected_checksums: Dict[str, str],
    print_fn=print,
) -> CorrectnessResult:
    """
    Verify that downloaded files match expected checksums.

    Returns a CorrectnessResult with verification statistics.
    """
    import sys

    print_fn("Verifying downloaded files...")
    verified = 0
    failed = 0
    failures: List[str] = []
    total_expected = len(expected_checksums)

    for rel_path, expected_checksum in expected_checksums.items():
        file_path = download_root / rel_path
        if not file_path.exists():
            failures.append(f"MISSING: {rel_path}")
            failed += 1
        else:
            actual_checksum = compute_file_checksum(file_path)
            if actual_checksum != expected_checksum:
                failures.append(
                    f"MISMATCH: {rel_path} "
                    f"(expected {expected_checksum[:16]}..., got {actual_checksum[:16]}...)"
                )
                failed += 1
            else:
                verified += 1

        # Update progress bar
        current = verified + failed
        if current % 500 == 0 or current == total_expected:
            sys.stdout.write(_print_progress_bar(current, total_expected, "  Verify"))
            sys.stdout.flush()

    print_fn("")  # Newline after progress bar

    # Check for unexpected files
    unexpected_count = 0
    for dirpath, _, filenames in os.walk(download_root):
        for filename in filenames:
            file_path = Path(dirpath) / filename
            rel_path = file_path.relative_to(download_root).as_posix()
            if rel_path not in expected_checksums:
                failures.append(f"UNEXPECTED: {rel_path}")
                failed += 1
                unexpected_count += 1

    if unexpected_count > 0:
        print_fn(f"  Found {unexpected_count} unexpected files")

    print_fn(f"  Verification complete: {verified} OK, {failed} FAILED")
    return CorrectnessResult(
        total_files=len(expected_checksums),
        verified_files=verified,
        failed_files=failed,
        failures=failures,
    )


# =============================================================================
# Test Operations
# =============================================================================


def test_collect(
    source_root: Path,
    config: TestConfig,
    print_fn=print,
) -> Tuple[AbsSnapshot, TimingResult]:
    """Test the COLLECT operation."""
    print_fn("\n" + "=" * 60)
    print_fn("TEST: COLLECT (directory scanning)")
    print_fn("=" * 60)

    start = time.perf_counter()
    with profile_operation("collect", config.cprofile_operation, print_fn):
        manifest = collect_manifest(
            directories=[source_root],
            filenames=[],
            symlink_policy=SymlinkPolicy.COLLAPSE_ESCAPING,
            file_chunk_size_bytes=config.chunk_size_bytes,
        )
    duration = time.perf_counter() - start

    file_count = len(manifest.files)
    dir_count = len(manifest.dirs)
    total_paths = file_count + dir_count
    total_bytes = manifest.totalSize
    paths_per_sec = total_paths / duration if duration > 0 else 0

    print_fn(f"  Files collected: {file_count}")
    print_fn(f"  Directories collected: {dir_count}")
    print_fn(f"  Total size: {total_bytes / (1024 * 1024):.2f} MB")
    print_fn(f"  Duration: {duration:.2f} seconds")
    print_fn(f"  Throughput: {paths_per_sec:.0f} paths/s")

    return manifest, TimingResult(
        operation="COLLECT",
        duration_seconds=duration,
        files_processed=total_paths,
        bytes_processed=total_bytes,
        throughput_mb_s=0,  # Not applicable for COLLECT
        throughput_paths_s=paths_per_sec,
    )


def test_hash(
    manifest: AbsSnapshot,
    hash_cache_dir: Path,
    config: TestConfig,
    print_fn=print,
) -> Tuple[AbsSnapshot, TimingResult]:
    """Test the HASH operation."""
    print_fn("\n" + "=" * 60)
    print_fn("TEST: HASH (file hashing)")
    print_fn("=" * 60)

    hash_cache_dir.mkdir(parents=True, exist_ok=True)

    with HashCache(str(hash_cache_dir)) as hash_cache:
        start = time.perf_counter()
        hashed_manifest = hash_manifest(
            manifest=manifest,
            hash_cache=hash_cache,
            force_rehash=True,  # Force rehash for accurate timing
        )
        duration = time.perf_counter() - start

    file_count = len(hashed_manifest.files)
    total_bytes = hashed_manifest.totalSize
    throughput = (total_bytes / (1024 * 1024)) / duration if duration > 0 else 0

    # Count chunked files
    chunked_files = sum(1 for f in hashed_manifest.files if f.chunkhashes)

    print_fn(f"  Files hashed: {file_count}")
    print_fn(f"  Chunked files: {chunked_files}")
    print_fn(f"  Total size: {total_bytes / (1024 * 1024):.2f} MB")
    print_fn(f"  Duration: {duration:.2f} seconds")
    print_fn(f"  Throughput: {throughput:.2f} MB/s")

    return hashed_manifest, TimingResult(
        operation="HASH",
        duration_seconds=duration,
        files_processed=file_count,
        bytes_processed=total_bytes,
        throughput_mb_s=throughput,
    )


def test_hash_upload_filesystem(
    manifest: AbsSnapshot,
    data_cache_root: Path,
    hash_cache_dir: Path,
    config: TestConfig,
    print_fn=print,
) -> Tuple[AbsSnapshot, TimingResult]:
    """Test the HASH_UPLOAD operation with FileSystemDataCache."""
    print_fn("\n" + "=" * 60)
    print_fn(f"TEST: HASH_UPLOAD (filesystem, max_workers={config.max_workers})")
    print_fn("=" * 60)

    data_cache_root.mkdir(parents=True, exist_ok=True)
    hash_cache_dir.mkdir(parents=True, exist_ok=True)

    data_cache = FileSystemDataCache(root_path=data_cache_root)

    # Progress tracking
    # ProgressReportMetadata has: progress (%), transferRate, progressMessage, processedFiles
    # We track percentage and estimate bytes from it
    total_bytes = manifest.totalSize
    total_files = len(manifest.files)
    progress_state = {"pct": 0.0, "last_print": time.perf_counter()}

    def on_progress(metadata) -> bool:
        progress_state["pct"] = metadata.progress
        now = time.perf_counter()
        # Print progress every 5 seconds
        if now - progress_state["last_print"] >= 5.0:
            processed_bytes = int(total_bytes * metadata.progress / 100) if total_bytes > 0 else 0
            print_fn(
                f"    Progress: {metadata.progress:.1f}% ({processed_bytes / (1024 * 1024):.1f} MB)"
            )
            progress_state["last_print"] = now
        return True

    progress_tracker = ProgressTracker(
        status=ProgressStatus.UPLOAD_IN_PROGRESS,
        total_files=total_files,
        total_bytes=total_bytes,
        on_progress_callback=on_progress,
    )

    print_fn(
        f"  Starting hash_upload_manifest with {total_files} files, {total_bytes / (1024 * 1024):.1f} MB..."
    )
    if not config.use_hash_cache:
        print_fn("  (force_rehash=True, so hash cache checking should be skipped)")

    with HashCache(str(hash_cache_dir)) as hash_cache:
        actual_hash_cache = hash_cache if config.use_hash_cache else None
        start = time.perf_counter()
        with profile_operation("upload", config.cprofile_operation, print_fn):
            upload_result = hash_upload_manifest(
                manifest=manifest,
                data_cache=data_cache,
                hash_cache=actual_hash_cache,
                force_rehash=not config.use_hash_cache,
                max_memory_bytes=config.max_memory_mb * 1024 * 1024,
                max_workers=config.max_workers,
                progress_tracker=progress_tracker,
            )
        duration = time.perf_counter() - start

    hashed_manifest = upload_result.manifest
    stats = upload_result.statistics
    file_count = len(hashed_manifest.files)
    total_bytes = hashed_manifest.totalSize
    throughput = (total_bytes / (1024 * 1024)) / duration if duration > 0 else 0

    # Count files in data cache
    cache_files = list(data_cache_root.glob("*"))
    print_fn(f"  Files processed: {stats.processed_files} ({stats.processed_bytes / (1024 * 1024):.2f} MB)")
    print_fn(f"  Files skipped (already in cache): {stats.skipped_files} ({stats.skipped_bytes / (1024 * 1024):.2f} MB)")
    print_fn(f"  Objects in data cache: {len(cache_files)}")
    print_fn(f"  Total size: {total_bytes / (1024 * 1024):.2f} MB")
    print_fn(f"  Duration: {duration:.2f} seconds")
    print_fn(f"  Throughput: {throughput:.2f} MB/s")

    return hashed_manifest, TimingResult(
        operation="HASH_UPLOAD (filesystem)",
        duration_seconds=duration,
        files_processed=file_count,
        bytes_processed=total_bytes,
        throughput_mb_s=throughput,
    )


def test_hash_upload_s3(
    manifest: AbsSnapshot,
    s3_bucket: str,
    s3_prefix: str,
    hash_cache_dir: Path,
    s3_check_cache_dir: Path,
    config: TestConfig,
    print_fn=print,
) -> Tuple[AbsSnapshot, TimingResult]:
    """Test the HASH_UPLOAD operation with S3DataCache."""
    import boto3

    print_fn("\n" + "=" * 60)
    print_fn(f"TEST: HASH_UPLOAD (S3, max_workers={config.max_workers})")
    print_fn("=" * 60)

    hash_cache_dir.mkdir(parents=True, exist_ok=True)
    s3_check_cache_dir.mkdir(parents=True, exist_ok=True)

    s3_client = boto3.client("s3")

    # Progress tracking
    # ProgressReportMetadata has: progress (%), transferRate, progressMessage, processedFiles
    # We track percentage and estimate bytes from it
    total_bytes = manifest.totalSize
    total_files = len(manifest.files)
    progress_state = {"pct": 0.0, "last_print": time.perf_counter()}

    def on_progress(metadata) -> bool:
        progress_state["pct"] = metadata.progress
        now = time.perf_counter()
        # Print progress every 5 seconds
        if now - progress_state["last_print"] >= 5.0:
            processed_bytes = int(total_bytes * metadata.progress / 100) if total_bytes > 0 else 0
            print_fn(
                f"    Progress: {metadata.progress:.1f}% ({processed_bytes / (1024 * 1024):.1f} MB)"
            )
            progress_state["last_print"] = now
        return True

    progress_tracker = ProgressTracker(
        status=ProgressStatus.UPLOAD_IN_PROGRESS,
        total_files=total_files,
        total_bytes=total_bytes,
        on_progress_callback=on_progress,
    )

    with S3CheckCache(str(s3_check_cache_dir)) as s3_check_cache:
        data_cache = S3DataCache(
            s3_bucket=s3_bucket,
            s3_key_prefix=s3_prefix,
            s3_client=s3_client,
            s3_check_cache=s3_check_cache,
        )

        with HashCache(str(hash_cache_dir)) as hash_cache:
            start = time.perf_counter()
            with profile_operation("upload", config.cprofile_operation, print_fn):
                upload_result = hash_upload_manifest(
                    manifest=manifest,
                    data_cache=data_cache,
                    hash_cache=hash_cache,
                    force_rehash=not config.use_hash_cache,
                    max_memory_bytes=config.max_memory_mb * 1024 * 1024,
                    max_workers=config.max_workers,
                    progress_tracker=progress_tracker,
                )
            duration = time.perf_counter() - start

    hashed_manifest = upload_result.manifest
    stats = upload_result.statistics
    file_count = len(hashed_manifest.files)
    total_bytes = hashed_manifest.totalSize
    throughput = (total_bytes / (1024 * 1024)) / duration if duration > 0 else 0

    print_fn(f"  Files processed: {stats.processed_files} ({stats.processed_bytes / (1024 * 1024):.2f} MB)")
    print_fn(f"  Files skipped (already in cache): {stats.skipped_files} ({stats.skipped_bytes / (1024 * 1024):.2f} MB)")
    print_fn(f"  Total size: {total_bytes / (1024 * 1024):.2f} MB")
    print_fn(f"  Duration: {duration:.2f} seconds")
    print_fn(f"  Throughput: {throughput:.2f} MB/s")

    return hashed_manifest, TimingResult(
        operation="HASH_UPLOAD (S3)",
        duration_seconds=duration,
        files_processed=file_count,
        bytes_processed=total_bytes,
        throughput_mb_s=throughput,
    )


def test_download_filesystem(
    manifest: AbsSnapshot,
    source_root: Path,
    download_root: Path,
    data_cache_root: Path,
    hash_cache_dir: Path,
    config: TestConfig,
    print_fn=print,
) -> TimingResult:
    """Test the DOWNLOAD operation with FileSystemDataCache."""
    print_fn("\n" + "=" * 60)
    print_fn(f"TEST: DOWNLOAD (filesystem, max_workers={config.max_workers})")
    print_fn("=" * 60)

    download_root.mkdir(parents=True, exist_ok=True)
    hash_cache_dir.mkdir(parents=True, exist_ok=True)

    data_cache = FileSystemDataCache(root_path=data_cache_root)

    # Convert manifest to relative paths, then join with download root
    rel_manifest = subtree_manifest(manifest, str(source_root))
    download_manifest_abs = join_manifest(rel_manifest, str(download_root))

    # Progress tracking
    total_bytes = download_manifest_abs.totalSize
    total_files = len(download_manifest_abs.files)
    progress_state = {"pct": 0.0, "last_print": time.perf_counter()}

    def on_progress(metadata) -> bool:
        progress_state["pct"] = metadata.progress
        now = time.perf_counter()
        if now - progress_state["last_print"] >= 5.0:
            processed_bytes = int(total_bytes * metadata.progress / 100) if total_bytes > 0 else 0
            print_fn(
                f"    Progress: {metadata.progress:.1f}% ({processed_bytes / (1024 * 1024):.1f} MB)"
            )
            progress_state["last_print"] = now
        return True

    progress_tracker = ProgressTracker(
        status=ProgressStatus.DOWNLOAD_IN_PROGRESS,
        total_files=total_files,
        total_bytes=total_bytes,
        on_progress_callback=on_progress,
    )

    print_fn(
        f"  Starting download_manifest with {total_files} files, {total_bytes / (1024 * 1024):.1f} MB..."
    )

    with HashCache(str(hash_cache_dir)) as hash_cache:
        start = time.perf_counter()
        with profile_operation("download", config.cprofile_operation, print_fn):
            result = download_manifest(
                manifest=download_manifest_abs,
                data_cache=data_cache,
                hash_cache=hash_cache,
                max_workers=config.max_workers,
                progress_tracker=progress_tracker,
            )
        duration = time.perf_counter() - start

    stats = result.statistics
    total_bytes = stats.processed_bytes
    throughput = (total_bytes / (1024 * 1024)) / duration if duration > 0 else 0

    print_fn(f"  Files downloaded: {stats.processed_files}")
    print_fn(f"  Files skipped: {stats.skipped_files}")
    print_fn(f"  Total bytes: {total_bytes / (1024 * 1024):.2f} MB")
    print_fn(f"  Duration: {duration:.2f} seconds")
    print_fn(f"  Throughput: {throughput:.2f} MB/s")

    return TimingResult(
        operation="DOWNLOAD (filesystem)",
        duration_seconds=duration,
        files_processed=stats.processed_files,
        bytes_processed=total_bytes,
        throughput_mb_s=throughput,
    )


def test_download_s3(
    manifest: AbsSnapshot,
    source_root: Path,
    download_root: Path,
    s3_bucket: str,
    s3_prefix: str,
    hash_cache_dir: Path,
    config: TestConfig,
    print_fn=print,
) -> TimingResult:
    """Test the DOWNLOAD operation with S3DataCache."""
    import boto3

    print_fn("\n" + "=" * 60)
    print_fn(f"TEST: DOWNLOAD (S3, max_workers={config.max_workers})")
    print_fn("=" * 60)

    download_root.mkdir(parents=True, exist_ok=True)
    hash_cache_dir.mkdir(parents=True, exist_ok=True)

    s3_client = boto3.client("s3")
    data_cache = S3DataCache(
        s3_bucket=s3_bucket,
        s3_key_prefix=s3_prefix,
        s3_client=s3_client,
    )

    # Convert manifest to relative paths, then join with download root
    rel_manifest = subtree_manifest(manifest, str(source_root))
    download_manifest_abs = join_manifest(rel_manifest, str(download_root))

    # Progress tracking
    total_bytes = download_manifest_abs.totalSize
    total_files = len(download_manifest_abs.files)
    progress_state = {"pct": 0.0, "last_print": time.perf_counter()}

    def on_progress(metadata) -> bool:
        progress_state["pct"] = metadata.progress
        now = time.perf_counter()
        if now - progress_state["last_print"] >= 5.0:
            processed_bytes = int(total_bytes * metadata.progress / 100) if total_bytes > 0 else 0
            print_fn(
                f"    Progress: {metadata.progress:.1f}% ({processed_bytes / (1024 * 1024):.1f} MB)"
            )
            progress_state["last_print"] = now
        return True

    progress_tracker = ProgressTracker(
        status=ProgressStatus.DOWNLOAD_IN_PROGRESS,
        total_files=total_files,
        total_bytes=total_bytes,
        on_progress_callback=on_progress,
    )

    print_fn(
        f"  Starting download_manifest with {total_files} files, {total_bytes / (1024 * 1024):.1f} MB..."
    )
    import threading
    import sys

    sys.stdout.flush()

    # Heartbeat thread
    stop_heartbeat = threading.Event()

    def heartbeat():
        count = 0
        while not stop_heartbeat.is_set():
            stop_heartbeat.wait(10.0)
            if not stop_heartbeat.is_set():
                count += 1
                processed_bytes = (
                    int(total_bytes * progress_state["pct"] / 100) if total_bytes > 0 else 0
                )
                print_fn(
                    f"    [heartbeat {count}] {progress_state['pct']:.1f}% - {processed_bytes / (1024 * 1024):.1f} MB processed"
                )
                sys.stdout.flush()

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()

    try:
        with HashCache(str(hash_cache_dir)) as hash_cache:
            start = time.perf_counter()
            result = download_manifest(
                manifest=download_manifest_abs,
                data_cache=data_cache,
                hash_cache=hash_cache,
                max_workers=config.max_workers,
                progress_tracker=progress_tracker,
            )
            duration = time.perf_counter() - start
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=1.0)

    stats = result.statistics
    total_bytes = stats.processed_bytes
    throughput = (total_bytes / (1024 * 1024)) / duration if duration > 0 else 0

    print_fn(f"  Files downloaded: {stats.processed_files}")
    print_fn(f"  Files skipped: {stats.skipped_files}")
    print_fn(f"  Total bytes: {total_bytes / (1024 * 1024):.2f} MB")
    print_fn(f"  Duration: {duration:.2f} seconds")
    print_fn(f"  Throughput: {throughput:.2f} MB/s")

    return TimingResult(
        operation="DOWNLOAD (S3)",
        duration_seconds=duration,
        files_processed=stats.processed_files,
        bytes_processed=total_bytes,
        throughput_mb_s=throughput,
    )


def test_diff(
    manifest1: AbsSnapshot,
    manifest2: AbsSnapshot,
    print_fn=print,
    config: Optional[TestConfig] = None,
) -> TimingResult:
    """Test the DIFF operation between two manifests."""
    print_fn("\n" + "=" * 60)
    print_fn("TEST: DIFF (manifest comparison)")
    print_fn("=" * 60)

    cprofile_op = config.cprofile_operation if config else None
    start = time.perf_counter()
    with profile_operation("diff", cprofile_op, print_fn):
        diff = compute_diff_manifest(
            parent=manifest1,
            current=manifest2,
            parent_manifest_hash="test_parent_hash",
        )
    duration = time.perf_counter() - start

    # Count changes
    added = sum(1 for f in diff.files if not f.deleted)
    deleted = sum(1 for f in diff.files if f.deleted)
    dir_added = sum(1 for d in diff.dirs if not d.deleted)
    dir_deleted = sum(1 for d in diff.dirs if d.deleted)

    print_fn(f"  Files added/modified: {added}")
    print_fn(f"  Files deleted: {deleted}")
    print_fn(f"  Directories added: {dir_added}")
    print_fn(f"  Directories deleted: {dir_deleted}")
    print_fn(f"  Duration: {duration:.4f} seconds")

    return TimingResult(
        operation="DIFF",
        duration_seconds=duration,
        files_processed=len(manifest1.files) + len(manifest2.files),
        bytes_processed=0,
        throughput_mb_s=0,
    )


# =============================================================================
# High Concurrency Stress Test
# =============================================================================


def run_high_concurrency_test(
    source_root: Path,
    config: TestConfig,
    expected_checksums: Optional[Dict[str, str]] = None,
    print_fn=print,
) -> Tuple[List[TimingResult], Optional[CorrectnessResult]]:
    """
    Run a high-concurrency stress test with correctness verification.

    This test:
    1. Uses pre-computed checksums (or builds them if not provided)
    2. Runs COLLECT -> HASH_UPLOAD -> DOWNLOAD with high concurrency
    3. Verifies downloaded files match source checksums exactly
    """
    print_fn("\n" + "=" * 60)
    print_fn("HIGH CONCURRENCY STRESS TEST")
    print_fn(f"  max_workers: {config.max_workers}")
    print_fn(f"  max_memory: {config.max_memory_mb} MB")
    print_fn(f"  verify_correctness: {config.verify_correctness}")
    print_fn("=" * 60)

    results: List[TimingResult] = []
    correctness_result: Optional[CorrectnessResult] = None

    # Create temporary directories
    with tempfile.TemporaryDirectory(prefix="snapshots_stress_") as temp_dir:
        temp_path = Path(temp_dir)
        data_cache_root = temp_path / "data_cache"
        hash_cache_dir = temp_path / "hash_cache"
        download_root = temp_path / "download"

        # Use provided checksums or build them
        if config.verify_correctness:
            if expected_checksums is not None:
                print_fn(f"  Using {len(expected_checksums):,} pre-computed checksums")
            else:
                expected_checksums = build_checksum_map(source_root, print_fn)
        else:
            expected_checksums = {}

        # Test COLLECT
        manifest, collect_result = test_collect(source_root, config, print_fn)
        results.append(collect_result)

        # Test HASH_UPLOAD with filesystem cache
        hashed_manifest, hash_upload_result = test_hash_upload_filesystem(
            manifest=manifest,
            data_cache_root=data_cache_root,
            hash_cache_dir=hash_cache_dir,
            config=config,
            print_fn=print_fn,
        )
        results.append(hash_upload_result)

        if not config.skip_download:
            # Test DOWNLOAD
            download_result = test_download_filesystem(
                manifest=hashed_manifest,
                source_root=source_root,
                download_root=download_root,
                data_cache_root=data_cache_root,
                hash_cache_dir=hash_cache_dir,
                config=config,
                print_fn=print_fn,
            )
            results.append(download_result)

            # Verify correctness
            if config.verify_correctness:
                correctness_result = verify_downloaded_files(
                    download_root=download_root,
                    expected_checksums=expected_checksums,
                    print_fn=print_fn,
                )

        # Test DIFF (compare manifest with itself - should be empty)
        diff_result = test_diff(hashed_manifest, hashed_manifest, print_fn, config)
        results.append(diff_result)

    return results, correctness_result


# =============================================================================
# S3 Test Runner
# =============================================================================


@dataclass
class S3TestPassResult:
    """Result from a single upload or download pass."""
    hashed_manifest: AbsSnapshot
    timing: TimingResult


def run_s3_upload_passes(
    manifest: AbsSnapshot,
    s3_bucket: str,
    s3_prefix: str,
    temp_path: Path,
    config: TestConfig,
    print_fn=print,
    label_suffix: str = "",
) -> Tuple[AbsSnapshot, List[TimingResult], str]:
    """
    Run upload passes (cold, warm-head, warm-all) and return results.
    
    Args:
        label_suffix: Optional suffix for operation labels (e.g., " (w=8)")
    
    Returns (hashed_manifest, list of TimingResults, s3_prefix_used).
    """
    results: List[TimingResult] = []
    
    hash_cache_dir = temp_path / "hash_cache"
    s3_check_cache_dir = temp_path / "s3_check_cache"
    
    # Use a unique prefix for cold upload
    cold_s3_prefix = f"{s3_prefix}-{int(time.time())}"
    
    # UPLOAD PASS 1: Cold
    print_fn(f"\n  [Upload Pass 1: COLD - fresh S3 prefix: {cold_s3_prefix}]")
    hashed_manifest, result = test_hash_upload_s3(
        manifest=manifest,
        s3_bucket=s3_bucket,
        s3_prefix=cold_s3_prefix,
        hash_cache_dir=hash_cache_dir,
        s3_check_cache_dir=s3_check_cache_dir,
        config=config,
        print_fn=print_fn,
    )
    results.append(TimingResult(
        operation=f"UPLOAD cold{label_suffix}",
        duration_seconds=result.duration_seconds,
        files_processed=result.files_processed,
        bytes_processed=result.bytes_processed,
        throughput_mb_s=result.throughput_mb_s,
    ))
    
    # UPLOAD PASS 2: Warm (no s3 check cache - HeadObject path)
    print_fn(f"\n  [Upload Pass 2: WARM - HeadObject path]")
    s3_check_cache_dir_fresh = temp_path / "s3_check_cache_fresh"
    _, result = test_hash_upload_s3(
        manifest=manifest,
        s3_bucket=s3_bucket,
        s3_prefix=cold_s3_prefix,
        hash_cache_dir=hash_cache_dir,
        s3_check_cache_dir=s3_check_cache_dir_fresh,
        config=config,
        print_fn=print_fn,
    )
    results.append(TimingResult(
        operation=f"UPLOAD warm-head{label_suffix}",
        duration_seconds=result.duration_seconds,
        files_processed=result.files_processed,
        bytes_processed=result.bytes_processed,
        throughput_mb_s=result.throughput_mb_s,
    ))
    
    # UPLOAD PASS 3: Warm (all caches - hash cache + s3 check cache from pass 1)
    print_fn(f"\n  [Upload Pass 3: WARM - all caches]")
    _, result = test_hash_upload_s3(
        manifest=manifest,
        s3_bucket=s3_bucket,
        s3_prefix=cold_s3_prefix,
        hash_cache_dir=hash_cache_dir,
        s3_check_cache_dir=s3_check_cache_dir,  # Original cache from pass 1
        config=config,
        print_fn=print_fn,
    )
    results.append(TimingResult(
        operation=f"UPLOAD warm-all{label_suffix}",
        duration_seconds=result.duration_seconds,
        files_processed=result.files_processed,
        bytes_processed=result.bytes_processed,
        throughput_mb_s=result.throughput_mb_s,
    ))
    
    return hashed_manifest, results, cold_s3_prefix


def run_s3_download_passes(
    hashed_manifest: AbsSnapshot,
    source_root: Path,
    s3_bucket: str,
    s3_prefix: str,
    temp_path: Path,
    config: TestConfig,
    print_fn=print,
    label_suffix: str = "",
) -> Tuple[List[TimingResult], Path]:
    """
    Run download passes (cold, warm) and return results.
    
    Args:
        label_suffix: Optional suffix for operation labels (e.g., " (w=8)")
    
    Returns (list of TimingResults, download_root path).
    """
    results: List[TimingResult] = []
    
    download_root = temp_path / "download"
    download_hash_cache_dir = temp_path / "download_hash_cache"
    
    # DOWNLOAD PASS 1: Cold
    print_fn(f"\n  [Download Pass 1: COLD - fresh download dir]")
    result = test_download_s3(
        manifest=hashed_manifest,
        source_root=source_root,
        download_root=download_root,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        hash_cache_dir=download_hash_cache_dir,
        config=config,
        print_fn=print_fn,
    )
    results.append(TimingResult(
        operation=f"DOWNLOAD cold{label_suffix}",
        duration_seconds=result.duration_seconds,
        files_processed=result.files_processed,
        bytes_processed=result.bytes_processed,
        throughput_mb_s=result.throughput_mb_s,
    ))
    
    # DOWNLOAD PASS 2: Warm
    print_fn(f"\n  [Download Pass 2: WARM - files exist]")
    result = test_download_s3(
        manifest=hashed_manifest,
        source_root=source_root,
        download_root=download_root,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        hash_cache_dir=download_hash_cache_dir,
        config=config,
        print_fn=print_fn,
    )
    results.append(TimingResult(
        operation=f"DOWNLOAD warm{label_suffix}",
        duration_seconds=result.duration_seconds,
        files_processed=result.files_processed,
        bytes_processed=result.bytes_processed,
        throughput_mb_s=result.throughput_mb_s,
    ))
    
    return results, download_root


def run_s3_test(
    source_root: Path,
    s3_bucket: str,
    s3_prefix: str,
    config: TestConfig,
    expected_checksums: Optional[Dict[str, str]] = None,
    print_fn=print,
) -> Tuple[List[TimingResult], Optional[CorrectnessResult]]:
    """
    Run stress test with S3 backend.

    Uses default AWS credentials.
    """
    print_fn("\n" + "=" * 60)
    print_fn("S3 STRESS TEST")
    print_fn(f"  s3_bucket: {s3_bucket}")
    print_fn(f"  s3_prefix: {s3_prefix}")
    print_fn(f"  max_workers: {config.max_workers}")
    print_fn("=" * 60)

    results: List[TimingResult] = []
    correctness_result: Optional[CorrectnessResult] = None

    with tempfile.TemporaryDirectory(prefix="snapshots_s3_stress_") as temp_dir:
        temp_path = Path(temp_dir)

        # Use provided checksums or build them
        if config.verify_correctness:
            if expected_checksums is not None:
                print_fn(f"  Using {len(expected_checksums):,} pre-computed checksums")
            else:
                expected_checksums = build_checksum_map(source_root, print_fn)
        else:
            expected_checksums = {}

        # Test COLLECT
        manifest, collect_result = test_collect(source_root, config, print_fn)
        results.append(collect_result)

        # Run upload passes (cold, warm-all, warm-head)
        hashed_manifest, upload_results, cold_s3_prefix = run_s3_upload_passes(
            manifest=manifest,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            temp_path=temp_path,
            config=config,
            print_fn=print_fn,
        )
        results.extend(upload_results)

        if not config.skip_download:
            # Run download passes (cold, warm)
            download_results, download_root = run_s3_download_passes(
                hashed_manifest=hashed_manifest,
                source_root=source_root,
                s3_bucket=s3_bucket,
                s3_prefix=cold_s3_prefix,
                temp_path=temp_path,
                config=config,
                print_fn=print_fn,
            )
            results.extend(download_results)

            # Verify correctness after download
            if config.verify_correctness:
                correctness_result = verify_downloaded_files(
                    download_root=download_root,
                    expected_checksums=expected_checksums,
                    print_fn=print_fn,
                )

    return results, correctness_result


# =============================================================================
# Scaling Test Runner
# =============================================================================


def run_s3_scaling_test(
    source_root: Path,
    s3_bucket: str,
    s3_prefix: str,
    base_config: TestConfig,
    worker_counts: List[int],
    expected_checksums: Optional[Dict[str, str]] = None,
    print_fn=print,
) -> List[Tuple[int, List[TimingResult]]]:
    """
    Run scaling test with multiple worker counts.

    Creates test data once, then runs tests with each worker count using fresh caches.
    Returns list of (worker_count, results) tuples.
    """
    import boto3

    all_scaling_results: List[Tuple[int, List[TimingResult]]] = []

    # Use a unique S3 prefix for this scaling test
    scaling_s3_prefix = f"{s3_prefix}-scaling-{int(time.time())}"

    print_fn("\n" + "=" * 60)
    print_fn("S3 SCALING TEST")
    print_fn(f"  s3_bucket: {s3_bucket}")
    print_fn(f"  s3_prefix: {scaling_s3_prefix}")
    print_fn(f"  worker_counts: {worker_counts}")
    print_fn("=" * 60)

    # First, collect the manifest once (doesn't depend on worker count)
    manifest, _ = test_collect(source_root, base_config, print_fn)

    for worker_count in worker_counts:
        print_fn("\n" + "#" * 60)
        print_fn(f"# SCALING TEST: max_workers = {worker_count}")
        print_fn("#" * 60)

        # Create config with this worker count
        config = TestConfig(
            small_files=base_config.small_files,
            medium_files=base_config.medium_files,
            large_files=base_config.large_files,
            large_file_size=base_config.large_file_size,
            subdirectories=base_config.subdirectories,
            max_nesting_depth=base_config.max_nesting_depth,
            max_workers=worker_count,
            max_memory_mb=base_config.max_memory_mb,
            verify_correctness=False,  # Skip verification for scaling tests
            local_only=base_config.local_only,
            s3_bucket=base_config.s3_bucket,
            s3_prefix=base_config.s3_prefix,
            skip_download=base_config.skip_download,
            setup_only=base_config.setup_only,
            keep_files=base_config.keep_files,
            chunk_size_bytes=base_config.chunk_size_bytes,
            use_hash_cache=base_config.use_hash_cache,
            cprofile_operation=base_config.cprofile_operation,
        )

        label_suffix = f" (w={worker_count})"

        with tempfile.TemporaryDirectory(prefix=f"snapshots_scaling_{worker_count}_") as temp_dir:
            temp_path = Path(temp_dir)

            # Use unique S3 prefix per worker count
            worker_s3_prefix = f"{scaling_s3_prefix}-w{worker_count}"

            # Run upload passes
            hashed_manifest, upload_results, used_s3_prefix = run_s3_upload_passes(
                manifest=manifest,
                s3_bucket=s3_bucket,
                s3_prefix=worker_s3_prefix,
                temp_path=temp_path,
                config=config,
                print_fn=print_fn,
                label_suffix=label_suffix,
            )

            results = list(upload_results)

            if not config.skip_download:
                # Run download passes
                download_results, _ = run_s3_download_passes(
                    hashed_manifest=hashed_manifest,
                    source_root=source_root,
                    s3_bucket=s3_bucket,
                    s3_prefix=used_s3_prefix,
                    temp_path=temp_path,
                    config=config,
                    print_fn=print_fn,
                    label_suffix=label_suffix,
                )
                results.extend(download_results)

        all_scaling_results.append((worker_count, results))

    return all_scaling_results


def _format_duration(seconds: float) -> str:
    """Format duration as M:SS or 0:SS.s for sub-second values."""
    if seconds < 1:
        return f"0:{seconds:04.1f}"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}:{secs:02.0f}"


def print_scaling_summary(
    scaling_results: List[Tuple[int, List[TimingResult]]],
    total_bytes: int,
    print_fn=print,
) -> None:
    """Print a summary chart of scaling test results in design doc format."""
    if not scaling_results:
        return

    _, first_results = scaling_results[0]
    # Extract short operation names (remove " (w=N)" suffix)
    op_names = [r.operation.split(" (w=")[0] for r in first_results]

    # Map to design doc column names
    col_map = {
        "UPLOAD cold": "UPLOAD cold",
        "UPLOAD warm-head": "UPLOAD warm-head",
        "UPLOAD warm-all": "UPLOAD warm-all",
        "DOWNLOAD cold": "DOWNLOAD cold",
        "DOWNLOAD warm": "DOWNLOAD warm",
    }
    col_names = [col_map.get(op, op) for op in op_names]

    print_fn("\n" + "=" * 100)
    print_fn("SCALING TEST SUMMARY (Duration as M:SS)")
    print_fn("=" * 100)

    # Print header
    header = f"| {'Workers':>8} |"
    for col in col_names:
        header += f" {col:>16} |"
    print_fn(header)

    # Print separator
    sep = f"|{'-' * 10}:|"
    for _ in col_names:
        sep += f"{'-' * 17}:|"
    print_fn(sep)

    # Print data rows
    for worker_count, results in scaling_results:
        row = f"| {worker_count:>8} |"
        for r in results:
            duration_str = _format_duration(r.duration_seconds)
            row += f" {duration_str:>16} |"
        print_fn(row)


# =============================================================================
# Results Summary
# =============================================================================


def print_summary(
    results: List[TimingResult],
    correctness: Optional[CorrectnessResult],
    total_time: float,
    print_fn=print,
) -> bool:
    """
    Print a summary of all test results.

    Returns True if all tests passed, False otherwise.
    """
    print_fn("\n" + "=" * 60)
    print_fn("SUMMARY")
    print_fn("=" * 60)

    print_fn("\nTiming Results:")
    print_fn(f"  {'Operation':<30} {'Duration':>10} {'Files':>10} {'Throughput':>12}")
    print_fn(f"  {'-' * 30} {'-' * 10} {'-' * 10} {'-' * 12}")

    for r in results:
        duration_str = f"{r.duration_seconds:.2f}s"
        if r.throughput_paths_s > 0:
            throughput_str = f"{r.throughput_paths_s:.0f} paths/s"
        elif r.throughput_mb_s > 0:
            throughput_str = f"{r.throughput_mb_s:.1f} MB/s"
        else:
            throughput_str = "N/A"
        print_fn(
            f"  {r.operation:<30} {duration_str:>10} {r.files_processed:>10} {throughput_str:>12}"
        )

    print_fn(f"\n  Total test time: {total_time:.2f} seconds")

    all_passed = True

    if correctness is not None:
        print_fn("\nCorrectness Verification:")
        print_fn(f"  Total files: {correctness.total_files}")
        print_fn(f"  Verified OK: {correctness.verified_files}")
        print_fn(f"  Failed: {correctness.failed_files}")

        if correctness.failed_files > 0:
            all_passed = False
            print_fn("\n  FAILURES:")
            for failure in correctness.failures[:20]:  # Show first 20 failures
                print_fn(f"    - {failure}")
            if len(correctness.failures) > 20:
                print_fn(f"    ... and {len(correctness.failures) - 20} more")

    if all_passed:
        print_fn("\n✓ ALL TESTS PASSED")
    else:
        print_fn("\n✗ SOME TESTS FAILED")

    return all_passed


# =============================================================================
# Main Entry Point
# =============================================================================


def main() -> int:
    """Main entry point for the stress test."""
    parser = argparse.ArgumentParser(
        description="Stress test for the snapshots library with correctness verification.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # AWS options
    parser.add_argument(
        "--s3-uri",
        type=str,
        help="S3 URI for data cache (e.g., s3://amzn-s3-demo-bucket/prefix). Uses default AWS credentials.",
    )

    # Local-only mode
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Run tests with local filesystem cache only (no AWS required)",
    )

    # Preset configurations
    parser.add_argument(
        "--preset",
        type=str,
        choices=["tiny", "small", "medium", "large"],
        default=None,
        help="Use a preset configuration (overrides file count options). "
        "tiny: 400 small, 40 medium, 2 large (1GB). "
        "small: 1500 small, 400 medium, 5 large (1GB). "
        "medium: 20000 small, 1000 medium, 10 large (1GB). "
        "large: 1000000 small, 10000 medium, 10 large (5GB).",
    )

    # File counts
    parser.add_argument(
        "--small-files",
        type=int,
        default=DEFAULT_SMALL_FILES,
        help=f"Number of small files to create (default: {DEFAULT_SMALL_FILES})",
    )
    parser.add_argument(
        "--medium-files",
        type=int,
        default=DEFAULT_MEDIUM_FILES,
        help=f"Number of medium files to create (default: {DEFAULT_MEDIUM_FILES})",
    )
    parser.add_argument(
        "--large-files",
        type=int,
        default=DEFAULT_LARGE_FILES,
        help=f"Number of large files to create (default: {DEFAULT_LARGE_FILES})",
    )

    # Directory structure
    parser.add_argument(
        "--subdirectories",
        type=int,
        default=DEFAULT_SUBDIRECTORIES,
        help=f"Number of subdirectories for small files (default: {DEFAULT_SUBDIRECTORIES})",
    )
    parser.add_argument(
        "--max-nesting-depth",
        type=int,
        default=DEFAULT_MAX_NESTING_DEPTH,
        help=f"Maximum nesting depth for directories (default: {DEFAULT_MAX_NESTING_DEPTH})",
    )

    # Concurrency options
    parser.add_argument(
        "--max-workers",
        type=str,
        default=str(DEFAULT_MAX_WORKERS),
        help=f"Maximum number of parallel workers. Can be comma-separated for scaling test, e.g. '1,2,4,8,16' (default: {DEFAULT_MAX_WORKERS})",
    )
    parser.add_argument(
        "--max-memory",
        type=int,
        default=None,
        help="Maximum memory in MB for pipeline (default: auto-detect)",
    )

    # Chunk size
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_FILE_CHUNK_SIZE // (1024 * 1024),
        help=f"Chunk size in MB for large files (default: {DEFAULT_FILE_CHUNK_SIZE // (1024 * 1024)})",
    )
    parser.add_argument(
        "--no-chunking",
        action="store_true",
        help="Disable file chunking (hash whole files)",
    )

    # Verification
    parser.add_argument(
        "--verify-correctness",
        action="store_true",
        default=True,
        help="Verify downloaded files match source (default: True)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip correctness verification",
    )

    # Other options
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip the download test",
    )
    parser.add_argument(
        "--setup-only",
        action="store_true",
        help="Only create test files, don't run tests",
    )
    parser.add_argument(
        "--keep-files",
        action="store_true",
        help="Keep test files after completion (default: delete)",
    )
    parser.add_argument(
        "--source-dir",
        type=str,
        help="Use existing directory as source instead of creating test files",
    )
    parser.add_argument(
        "--no-hash-cache",
        action="store_true",
        help="Disable hash cache (pass None to hash_upload_manifest)",
    )
    parser.add_argument(
        "--cprofile",
        type=str,
        choices=["collect", "upload", "download", "diff"],
        default=None,
        help="Profile a specific operation with cProfile. Saves to <operation>.prof",
    )

    args = parser.parse_args()

    # Validate arguments
    if not args.local_only and not args.s3_uri:
        parser.error("--s3-uri is required unless --local-only is specified")

    # Parse S3 URI
    s3_bucket = None
    s3_prefix = None
    if args.s3_uri:
        if not args.s3_uri.startswith("s3://"):
            parser.error("--s3-uri must start with s3://")
        uri_path = args.s3_uri[5:]  # Remove "s3://"
        if "/" in uri_path:
            s3_bucket, s3_prefix = uri_path.split("/", 1)
        else:
            s3_bucket = uri_path
            s3_prefix = ""

    # Apply preset configurations (override file counts and large file size)
    preset_large_file_size = LARGE_FILE_SIZE  # Default 1GB
    if args.preset == "tiny":
        # Use defaults: 400 small, 40 medium, 2 large (1GB)
        args.small_files = DEFAULT_SMALL_FILES
        args.medium_files = DEFAULT_MEDIUM_FILES
        args.large_files = DEFAULT_LARGE_FILES
        preset_large_file_size = 1 * 1024 * 1024 * 1024  # 1GB
    elif args.preset == "small":
        args.small_files = 1500
        args.medium_files = 400
        args.large_files = 5
        preset_large_file_size = 1 * 1024 * 1024 * 1024  # 1GB
    elif args.preset == "medium":
        args.small_files = 20000
        args.medium_files = 1000
        args.large_files = 10
        preset_large_file_size = 1 * 1024 * 1024 * 1024  # 1GB
    elif args.preset == "large":
        args.small_files = 1000000
        args.medium_files = 10000
        args.large_files = 10
        preset_large_file_size = 5 * 1024 * 1024 * 1024  # 5GB

    # Calculate default memory if not specified
    max_memory_mb = args.max_memory if args.max_memory is not None else _get_default_max_memory_mb()

    # Parse max_workers - can be single int or comma-separated list
    worker_counts = [int(w.strip()) for w in args.max_workers.split(",")]
    is_scaling_test = len(worker_counts) > 1

    # Build config (use first worker count for initial config)
    config = TestConfig(
        small_files=args.small_files,
        medium_files=args.medium_files,
        large_files=args.large_files,
        large_file_size=preset_large_file_size,
        subdirectories=args.subdirectories,
        max_nesting_depth=args.max_nesting_depth,
        max_workers=worker_counts[0],
        max_memory_mb=max_memory_mb,
        verify_correctness=not args.no_verify,
        local_only=args.local_only,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        skip_download=args.skip_download,
        setup_only=args.setup_only,
        keep_files=args.keep_files,
        chunk_size_bytes=WHOLE_FILE_CHUNK_SIZE
        if args.no_chunking
        else args.chunk_size * 1024 * 1024,
        use_hash_cache=not args.no_hash_cache,
        cprofile_operation=args.cprofile,
    )

    print("=" * 60)
    print("SNAPSHOTS LIBRARY STRESS TEST")
    print("=" * 60)
    preset_str = f" (preset: {args.preset})" if args.preset else ""
    print(f"Configuration:{preset_str}")
    print(f"  Small files: {config.small_files} x {SMALL_FILE_SIZE} bytes")
    print(f"  Medium files: {config.medium_files} x {MEDIUM_FILE_SIZE // (1024 * 1024)} MB")
    print(
        f"  Large files: {config.large_files} x {config.large_file_size // (1024 * 1024 * 1024)} GB"
    )
    print(f"  Subdirectories: {config.subdirectories}")
    print(f"  Max nesting depth: {config.max_nesting_depth}")
    if is_scaling_test:
        print(f"  Max workers: {worker_counts} (SCALING TEST)")
    else:
        print(f"  Max workers: {config.max_workers}")
    max_memory_str = f"{config.max_memory_mb / 1024:.1f} GB"
    if args.max_memory is None:
        max_memory_str += " (auto)"
    print(f"  Max memory: {max_memory_str}")
    print(
        f"  Chunk size: {'disabled' if config.chunk_size_bytes == WHOLE_FILE_CHUNK_SIZE else f'{config.chunk_size_bytes // (1024 * 1024)} MB'}"
    )
    print(f"  Verify correctness: {config.verify_correctness}")
    print(f"  Mode: {'local filesystem' if config.local_only else 'S3'}")

    start_time = time.perf_counter()

    # Determine source directory and checksums
    expected_checksums: Optional[Dict[str, str]] = None

    if args.source_dir:
        source_root = Path(args.source_dir)
        if not source_root.exists():
            print(f"ERROR: Source directory does not exist: {source_root}")
            return 1
        print(f"\nUsing existing source directory: {source_root}")
        # Will need to build checksums later if verification is enabled
    else:
        # Create test files in temp directory or fixed location
        if config.keep_files:
            source_root = Path(tempfile.gettempdir()) / "snapshots_stress_test_data"
        else:
            source_root = Path(tempfile.mkdtemp(prefix="snapshots_stress_src_"))

        _, total_bytes, expected_checksums = create_test_files(source_root, config)

        if config.setup_only:
            print(f"\nSetup complete. Test files are in: {source_root}")
            return 0

    try:
        # Run tests
        if is_scaling_test and not config.local_only:
            # Run scaling test with multiple worker counts
            scaling_results = run_s3_scaling_test(
                source_root=source_root,
                s3_bucket=config.s3_bucket,  # type: ignore
                s3_prefix=config.s3_prefix,  # type: ignore
                base_config=config,
                worker_counts=worker_counts,
                expected_checksums=expected_checksums,
            )

            total_time = time.perf_counter() - start_time

            # Print scaling summary
            print_scaling_summary(scaling_results, total_bytes)
            print(f"\n  Total test time: {total_time:.2f} seconds")
            print("\n✓ SCALING TEST COMPLETE")
            return 0

        elif config.local_only:
            results, correctness = run_high_concurrency_test(
                source_root, config, expected_checksums
            )
        else:
            results, correctness = run_s3_test(
                source_root=source_root,
                s3_bucket=config.s3_bucket,  # type: ignore
                s3_prefix=config.s3_prefix,  # type: ignore
                config=config,
                expected_checksums=expected_checksums,
            )

        total_time = time.perf_counter() - start_time

        # Print summary
        all_passed = print_summary(results, correctness, total_time)

        return 0 if all_passed else 1

    finally:
        # Cleanup
        if not config.keep_files and not args.source_dir:
            print(f"\nCleaning up test files in {source_root}...")
            shutil.rmtree(source_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
