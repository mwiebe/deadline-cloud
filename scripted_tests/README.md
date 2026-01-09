# Scripted Tests
This directory contains Python scripts designed to test functionalities of Job Attachment module that are hard to cover with unit tests.

## How to Use
Each script contains its own execution instructions at the top of the file. Please follow those instructions to run the individual tests.

## Available Scripts

### upload_scale_test.py
Job attachments scale test for measuring upload and hashing speed using the `S3AssetManager` API.

### snapshots_scale_test.py
Stress test for the new snapshots library with correctness verification. Tests the composable manifest operations (COLLECT, HASH, HASH_UPLOAD, DOWNLOAD, DIFF) at scale.

**Features:**
- Tests with local filesystem cache or S3
- High-concurrency stress testing (configurable max_workers)
- Correctness verification via SHA-256 checksums
- Configurable file counts and sizes
- Support for chunked large files (>256MB)

**Quick Start:**
```bash
# Local filesystem test (no AWS required)
hatch run python scripted_tests/snapshots_scale_test.py --local-only

# High concurrency stress test with correctness verification
hatch run python scripted_tests/snapshots_scale_test.py --local-only \
    --small-files 2000 --medium-files 100 --large-files 5 \
    --max-workers 50 --verify-correctness

# S3 test (requires Deadline Cloud farm/queue)
hatch run python scripted_tests/snapshots_scale_test.py \
    -f $FARM_ID -q $QUEUE_ID

# Profile with cProfile
python -m cProfile -o profile.prof scripted_tests/snapshots_scale_test.py --local-only
snakeviz profile.prof
```

**Options:**
| Option | Default | Description |
|--------|---------|-------------|
| `--local-only` | - | Use local filesystem cache (no AWS) |
| `--small-files` | 1000 | Number of 1KB files |
| `--medium-files` | 100 | Number of 5MB files |
| `--large-files` | 2 | Number of 300MB files (chunked) |
| `--max-workers` | 10 | Parallel worker threads |
| `--max-memory` | 512 | Max memory (MB) for pipeline |
| `--verify-correctness` | True | Verify downloaded files match source |
| `--skip-download` | - | Skip download test |
| `--keep-files` | - | Keep test files after completion |
