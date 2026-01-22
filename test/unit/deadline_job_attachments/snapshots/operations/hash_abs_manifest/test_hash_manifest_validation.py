# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for hash_abs_manifest input validation and error cases.

These tests cover:
- Relative path rejection
- Pre-hashed file rejection
- Invalid chunkhashes rejection
- Large file validation
- Small file validation
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from deadline.job_attachments._snapshots import (
    hash_abs_manifest,
    collect_abs_snapshot,
    SymlinkPolicy,
    DEFAULT_FILE_CHUNK_SIZE,
    AbsSnapshot,
    ManifestDirectoryPath,
    ManifestFilePath,
)
from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm


class TestInputValidation:
    """Tests for input validation in hash_abs_manifest."""

    def test_rejects_relative_paths(self, tmp_path: Path) -> None:
        """Manifest with relative paths raises ValueError."""
        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path="/absolute/path/file.txt",
                    hash=None,
                    size=100,
                    mtime=12345,
                )
            ],
            total_size=100,
        )
        # Manually override path to relative (bypassing validation)
        manifest.files[0].path = "relative/path/file.txt"

        with pytest.raises(ValueError, match="requires absolute paths"):
            hash_abs_manifest(manifest)

    def test_accepts_absolute_paths(self, tmp_path: Path) -> None:
        """Manifest with absolute paths is accepted."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_abs_snapshot(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.COLLAPSE_ALL,
        )

        result = hash_abs_manifest(collected)
        assert result.manifest.files[0].hash is not None
        assert result.manifest.files[0].hash != ""

    def test_large_file_rejects_non_none_hash(self, tmp_path: Path) -> None:
        """Large file with hash set (not None) raises ValueError."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)
        abs_path = str(test_file).replace("\\", "/")
        stat_info = test_file.stat()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    chunkhashes=["a", "b"],
                    size=DEFAULT_FILE_CHUNK_SIZE + 1000,
                    mtime=int(stat_info.st_mtime_ns // 1000),
                )
            ],
            total_size=DEFAULT_FILE_CHUNK_SIZE + 1000,
        )
        # Manually set hash (invalid for large file)
        manifest.files[0].hash = "somehash"

        with pytest.raises(ValueError, match="should have hash=None"):
            hash_abs_manifest(manifest)

    def test_large_file_valid_input_with_none_chunkhashes(self, tmp_path: Path) -> None:
        """Large file with chunkhashes=None (unhashed) is valid and gets hashed."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)
        abs_path = str(test_file).replace("\\", "/")
        stat_info = test_file.stat()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=DEFAULT_FILE_CHUNK_SIZE + 1000,
                    mtime=int(stat_info.st_mtime_ns // 1000),
                )
            ],
            total_size=DEFAULT_FILE_CHUNK_SIZE + 1000,
        )

        with patch(
            "deadline.job_attachments._snapshots._operations._hash_abs_manifest._hash_file_chunked"
        ) as mock_chunk:
            mock_chunk.return_value = ["hash1", "hash2"]

            result = hash_abs_manifest(manifest)

            assert result.manifest.files[0].chunkhashes is not None
            assert len(result.manifest.files[0].chunkhashes) == 2

    def test_large_file_valid_input_passes(self, tmp_path: Path) -> None:
        """Large file with valid input (hash=None, chunkhashes=None) passes."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 1000)
        abs_path = str(test_file).replace("\\", "/")
        stat_info = test_file.stat()

        manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            dirs=[],
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    size=DEFAULT_FILE_CHUNK_SIZE * 2 + 1000,
                    mtime=int(stat_info.st_mtime_ns // 1000),
                )
            ],
            total_size=DEFAULT_FILE_CHUNK_SIZE * 2 + 1000,
        )

        with patch(
            "deadline.job_attachments._snapshots._operations._hash_abs_manifest._hash_file_chunked"
        ) as mock_chunk:
            mock_chunk.return_value = ["hash1", "hash2", "hash3"]

            result = hash_abs_manifest(manifest)

            assert result.manifest.files[0].chunkhashes is not None
            assert len(result.manifest.files[0].chunkhashes) == 3

    def test_small_file_rejects_non_none_chunkhashes(self, tmp_path: Path) -> None:
        """Small file with chunkhashes set raises ValueError."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_abs_snapshot(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        # Manually set chunkhashes (invalid for small file)
        collected.files[0].chunkhashes = ["a", "b"]

        with pytest.raises(ValueError, match="should have chunkhashes=None"):
            hash_abs_manifest(collected)

    def test_small_file_valid_input_passes(self, tmp_path: Path) -> None:
        """Small file with valid input (hash=None, chunkhashes=None) passes."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        collected = collect_abs_snapshot(
            [tmp_path],
            [],
            symlink_policy=SymlinkPolicy.PRESERVE,
        )
        assert collected.files[0].hash is None
        assert collected.files[0].chunkhashes is None

        result = hash_abs_manifest(collected)

        assert isinstance(result.manifest.files[0].hash, str)
        assert len(result.manifest.files[0].hash) > 0
        assert result.manifest.files[0].chunkhashes is None


class TestValidationErrorMessages:
    """Tests for validation error messages with specific file names."""

    def test_error_when_large_file_has_hash(self, tmp_path: Path) -> None:
        """Error raised when large file already has hash set (should be None)."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 100)
        abs_path = str(test_file).replace("\\", "/")

        input_manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="already_hashed",  # Should be None!
                    size=100,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=100,
            file_chunk_size_bytes=16,  # Small chunk size to trigger chunking
        )

        with pytest.raises(ValueError) as exc_info:
            hash_abs_manifest(manifest=input_manifest)

        assert "should have hash=None" in str(exc_info.value)
        assert "large.bin" in str(exc_info.value)

    def test_error_when_large_file_has_chunkhashes(self, tmp_path: Path) -> None:
        """Error raised when large file already has chunkhashes set (should be None)."""
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(b"x" * 100)
        abs_path = str(test_file).replace("\\", "/")

        input_manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    chunkhashes=["abc123"],  # Should be None!
                    size=100,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=100,
            file_chunk_size_bytes=16,
        )

        with pytest.raises(ValueError) as exc_info:
            hash_abs_manifest(manifest=input_manifest)

        assert "should have chunkhashes=None" in str(exc_info.value)
        assert "large.bin" in str(exc_info.value)

    def test_error_when_small_file_has_hash(self, tmp_path: Path) -> None:
        """Error raised when small file already has hash set (should be None)."""
        test_file = tmp_path / "small.txt"
        test_file.write_text("small")
        abs_path = str(test_file).replace("\\", "/")

        input_manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash="already_hashed",  # Should be None!
                    size=5,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=5,
            file_chunk_size_bytes=DEFAULT_FILE_CHUNK_SIZE,
        )

        with pytest.raises(ValueError) as exc_info:
            hash_abs_manifest(manifest=input_manifest)

        assert "should have hash=None" in str(exc_info.value)
        assert "small.txt" in str(exc_info.value)

    def test_error_when_small_file_has_chunkhashes(self, tmp_path: Path) -> None:
        """Error raised when small file has chunkhashes set (should be None)."""
        test_file = tmp_path / "small.txt"
        test_file.write_text("small")
        abs_path = str(test_file).replace("\\", "/")

        input_manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path=abs_path,
                    hash=None,
                    chunkhashes=["abc123"],  # Should be None!
                    size=5,
                    mtime=int(test_file.stat().st_mtime_ns // 1000),
                )
            ],
            total_size=5,
            file_chunk_size_bytes=DEFAULT_FILE_CHUNK_SIZE,
        )

        with pytest.raises(ValueError) as exc_info:
            hash_abs_manifest(manifest=input_manifest)

        assert "should have chunkhashes=None" in str(exc_info.value)
        assert "small.txt" in str(exc_info.value)

    def test_error_when_file_path_is_relative(self) -> None:
        """Error raised when file path is relative (HASH requires absolute paths)."""
        input_manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[
                ManifestFilePath(
                    path="relative/path/file.txt",  # Relative path!
                    hash=None,
                    size=10,
                    mtime=12345,
                )
            ],
            total_size=10,
        )

        with pytest.raises(ValueError) as exc_info:
            hash_abs_manifest(manifest=input_manifest)

        assert "requires absolute paths" in str(exc_info.value)
        assert "relative/path/file.txt" in str(exc_info.value)

    def test_error_when_directory_path_is_relative(self) -> None:
        """Error raised when directory path is relative."""
        input_manifest = AbsSnapshot(
            hash_alg=HashAlgorithm.XXH128,
            files=[],
            dirs=[
                ManifestDirectoryPath(
                    path="relative/dir",  # Relative path!
                )
            ],
            total_size=0,
        )

        with pytest.raises(ValueError) as exc_info:
            hash_abs_manifest(manifest=input_manifest)

        assert "requires absolute paths" in str(exc_info.value)
        assert "relative/dir" in str(exc_info.value)
