# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for ManifestFilePath validation in v2025-12-04 format."""

import math

import pytest

from deadline.job_attachments.asset_manifests import FILE_CHUNK_SIZE_BYTES
from deadline.job_attachments.asset_manifests.v2025_12_04 import ManifestFilePath
from deadline.job_attachments.exceptions import ManifestDecodeValidationError


class TestManifestFilePathValidation:
    """Tests for ManifestFilePath validation rules."""

    # ==================== Valid cases ====================

    def test_valid_file_with_hash(self):
        """A regular file with hash, size, and mtime is valid."""
        path = ManifestFilePath(
            path="test/file.txt",
            hash="abc123",
            size=1024,
            mtime=1234567890,
        )
        assert path.path == "test/file.txt"
        assert path.hash == "abc123"
        assert path.size == 1024
        assert path.mtime == 1234567890
        assert path.deleted is False

    def test_valid_file_with_hash_and_runnable(self):
        """A file with hash and runnable=True is valid."""
        path = ManifestFilePath(
            path="script.sh",
            hash="abc123",
            size=512,
            mtime=1234567890,
            runnable=True,
        )
        assert path.runnable is True

    def test_valid_symlink(self):
        """A symlink with only symlink_target is valid (no size/mtime required)."""
        path = ManifestFilePath(
            path="link.txt",
            symlink_target="target.txt",
        )
        assert path.symlink_target == "target.txt"
        assert path.hash is None
        assert path.size is None
        assert path.mtime is None

    def test_valid_deleted_file(self):
        """A deleted file with only path is valid."""
        path = ManifestFilePath(
            path="deleted_file.txt",
            deleted=True,
        )
        assert path.deleted is True
        assert path.hash is None
        assert path.size is None
        assert path.mtime is None


    def test_valid_chunked_file(self):
        """A large file with chunkhashes is valid when chunk count matches size."""
        # 300MB file = 2 chunks (256MB + 44MB)
        size = 300 * 1024 * 1024
        expected_chunks = math.ceil(size / FILE_CHUNK_SIZE_BYTES)
        chunkhashes = ["hash1", "hash2"]
        assert len(chunkhashes) == expected_chunks

        path = ManifestFilePath(
            path="large_file.bin",
            chunkhashes=chunkhashes,
            size=size,
            mtime=1234567890,
        )
        assert path.chunkhashes == chunkhashes
        assert path.hash is None

    def test_valid_chunked_file_exact_boundary(self):
        """A file exactly at chunk boundary + 1 byte needs 2 chunks."""
        size = FILE_CHUNK_SIZE_BYTES + 1
        expected_chunks = 2
        chunkhashes = ["hash1", "hash2"]

        path = ManifestFilePath(
            path="boundary_file.bin",
            chunkhashes=chunkhashes,
            size=size,
            mtime=1234567890,
        )
        assert len(path.chunkhashes) == expected_chunks

    # ==================== Deleted file validation errors ====================

    def test_deleted_file_cannot_have_hash(self):
        """Deleted file cannot have hash field."""
        with pytest.raises(ManifestDecodeValidationError, match="cannot have 'hash' field"):
            ManifestFilePath(
                path="file.txt",
                deleted=True,
                hash="abc123",
            )

    def test_deleted_file_cannot_have_chunkhashes(self):
        """Deleted file cannot have chunkhashes field."""
        with pytest.raises(ManifestDecodeValidationError, match="cannot have 'chunkhashes' field"):
            ManifestFilePath(
                path="file.txt",
                deleted=True,
                chunkhashes=["hash1"],
            )

    def test_deleted_file_cannot_have_symlink_target(self):
        """Deleted file cannot have symlink_target field."""
        with pytest.raises(
            ManifestDecodeValidationError, match="cannot have 'symlink_target' field"
        ):
            ManifestFilePath(
                path="file.txt",
                deleted=True,
                symlink_target="target.txt",
            )

    def test_deleted_file_cannot_have_runnable(self):
        """Deleted file cannot have runnable=True."""
        with pytest.raises(ManifestDecodeValidationError, match="cannot have 'runnable' set"):
            ManifestFilePath(
                path="file.txt",
                deleted=True,
                runnable=True,
            )

    def test_deleted_file_cannot_have_size(self):
        """Deleted file cannot have size field."""
        with pytest.raises(ManifestDecodeValidationError, match="cannot have 'size' field"):
            ManifestFilePath(
                path="file.txt",
                deleted=True,
                size=1024,
            )

    def test_deleted_file_cannot_have_mtime(self):
        """Deleted file cannot have mtime field."""
        with pytest.raises(ManifestDecodeValidationError, match="cannot have 'mtime' field"):
            ManifestFilePath(
                path="file.txt",
                deleted=True,
                mtime=1234567890,
            )

    # ==================== Non-deleted file validation errors ====================

    def test_file_must_have_content_field(self):
        """Non-deleted file must have exactly one of hash, chunkhashes, or symlink_target."""
        with pytest.raises(
            ManifestDecodeValidationError,
            match="must have exactly one of 'hash', 'chunkhashes', or 'symlink_target'",
        ):
            ManifestFilePath(
                path="file.txt",
                size=1024,
                mtime=1234567890,
            )

    def test_file_cannot_have_multiple_content_fields(self):
        """Non-deleted file cannot have both hash and chunkhashes."""
        with pytest.raises(
            ManifestDecodeValidationError,
            match="must have exactly one of 'hash', 'chunkhashes', or 'symlink_target'",
        ):
            ManifestFilePath(
                path="file.txt",
                hash="abc123",
                chunkhashes=["hash1", "hash2"],
                size=300 * 1024 * 1024,
                mtime=1234567890,
            )

    def test_file_cannot_have_hash_and_symlink(self):
        """Non-deleted file cannot have both hash and symlink_target."""
        with pytest.raises(
            ManifestDecodeValidationError,
            match="must have exactly one of 'hash', 'chunkhashes', or 'symlink_target'",
        ):
            ManifestFilePath(
                path="file.txt",
                hash="abc123",
                symlink_target="target.txt",
                size=1024,
                mtime=1234567890,
            )

    def test_regular_file_must_have_size(self):
        """Regular file (with hash) must have size field."""
        with pytest.raises(ManifestDecodeValidationError, match="must have 'size' field"):
            ManifestFilePath(
                path="file.txt",
                hash="abc123",
                mtime=1234567890,
            )

    def test_regular_file_must_have_mtime(self):
        """Regular file (with hash) must have mtime field."""
        with pytest.raises(ManifestDecodeValidationError, match="must have 'mtime' field"):
            ManifestFilePath(
                path="file.txt",
                hash="abc123",
                size=1024,
            )

    # ==================== Chunkhashes validation errors ====================

    def test_chunked_file_must_have_size(self):
        """File with chunkhashes must have size field."""
        with pytest.raises(ManifestDecodeValidationError, match="must have 'size' field"):
            ManifestFilePath(
                path="file.txt",
                chunkhashes=["hash1", "hash2"],
                mtime=1234567890,
            )

    def test_chunked_file_size_must_exceed_chunk_size(self):
        """File with chunkhashes must have size > 256MB."""
        with pytest.raises(
            ManifestDecodeValidationError, match=r"must have size > \d+ \(256MB\)"
        ):
            ManifestFilePath(
                path="file.txt",
                chunkhashes=["hash1"],
                size=1024,  # Too small
                mtime=1234567890,
            )

    def test_chunked_file_chunk_count_must_match_size(self):
        """Number of chunkhashes must match ceil(size / CHUNK_SIZE)."""
        # 300MB = 2 chunks, but we provide 3
        size = 300 * 1024 * 1024
        with pytest.raises(
            ManifestDecodeValidationError, match=r"should have \d+ chunks, got 3"
        ):
            ManifestFilePath(
                path="file.txt",
                chunkhashes=["hash1", "hash2", "hash3"],  # Wrong count
                size=size,
                mtime=1234567890,
            )

    def test_chunked_file_too_few_chunks(self):
        """Too few chunkhashes for the file size."""
        # 600MB = 3 chunks, but we provide 2
        size = 600 * 1024 * 1024
        with pytest.raises(
            ManifestDecodeValidationError, match=r"should have \d+ chunks, got 2"
        ):
            ManifestFilePath(
                path="file.txt",
                chunkhashes=["hash1", "hash2"],  # Too few
                size=size,
                mtime=1234567890,
            )


    # ==================== Symlink validation ====================

    def test_symlink_target_normalized_from_windows_path(self):
        """Symlink target with Windows backslashes is normalized to POSIX."""
        path = ManifestFilePath(
            path="link.txt",
            symlink_target="subdir\\target.txt",
        )
        assert path.symlink_target == "subdir/target.txt"

    def test_symlink_target_normalized_collapses_dot_dot(self):
        """Symlink target with '..' components is normalized."""
        path = ManifestFilePath(
            path="subdir/link.txt",
            symlink_target="subdir/../other/target.txt",
        )
        assert path.symlink_target == "other/target.txt"

    def test_symlink_target_normalized_collapses_dot(self):
        """Symlink target with '.' components is normalized."""
        path = ManifestFilePath(
            path="link.txt",
            symlink_target="./subdir/./target.txt",
        )
        assert path.symlink_target == "subdir/target.txt"

    def test_symlink_target_rejects_absolute_path(self):
        """Symlink target cannot be an absolute path."""
        with pytest.raises(
            ManifestDecodeValidationError, match="must be a relative path"
        ):
            ManifestFilePath(
                path="link.txt",
                symlink_target="/absolute/path/target.txt",
            )

    def test_symlink_target_rejects_escape_with_leading_dotdot(self):
        """Symlink target cannot start with '..' that escapes manifest root."""
        with pytest.raises(
            ManifestDecodeValidationError, match="escapes manifest root"
        ):
            ManifestFilePath(
                path="link.txt",
                symlink_target="../outside/target.txt",
            )

    def test_symlink_target_rejects_escape_with_multiple_dotdot(self):
        """Symlink target cannot have '..' that escapes manifest root."""
        with pytest.raises(
            ManifestDecodeValidationError, match="escapes manifest root"
        ):
            ManifestFilePath(
                path="link.txt",
                symlink_target="subdir/../../outside/target.txt",
            )

    def test_symlink_target_allows_dotdot_within_manifest(self):
        """Symlink target can use '..' as long as it stays within manifest."""
        path = ManifestFilePath(
            path="subdir/link.txt",
            symlink_target="deep/nested/../sibling/target.txt",
        )
        # After normalization: deep/sibling/target.txt
        assert path.symlink_target == "deep/sibling/target.txt"

    def test_symlink_target_valid_relative_path(self):
        """Valid relative symlink target is accepted."""
        path = ManifestFilePath(
            path="link.txt",
            symlink_target="subdir/target.txt",
        )
        assert path.symlink_target == "subdir/target.txt"

    def test_symlink_target_valid_same_directory(self):
        """Symlink to file in same directory is valid."""
        path = ManifestFilePath(
            path="link.txt",
            symlink_target="target.txt",
        )
        assert path.symlink_target == "target.txt"
