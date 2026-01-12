# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for download_manifest atomic file write behavior.

These tests cover:
- No temporary files left after successful download
- Atomic file creation using temp files and os.replace()
- Temp file cleanup on error
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from deadline.job_attachments._snapshots import (
    collect_abs_snapshot,
    hash_upload_manifest,
    download_manifest,
    join_manifest,
    subtree_manifest,
    FileSystemDataCache,
)
from deadline.job_attachments._snapshots import AbsSnapshot


def _to_abs_snapshot(manifest: object) -> AbsSnapshot:
    """Cast a manifest to AbsSnapshot for type checking."""
    return cast(AbsSnapshot, manifest)


class TestDownloadManifestAtomicWrites:
    """Tests for atomic file write behavior."""

    def _create_filesystem_data_cache(self, cache_root: Path) -> FileSystemDataCache:
        cache_root.mkdir(parents=True, exist_ok=True)
        return FileSystemDataCache(root_path=cache_root)

    def test_no_temp_files_left_on_success(self, tmp_path: Path) -> None:
        """Test that no temporary files are left after successful download."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        (source_dir / "file1.txt").write_text("Content 1")
        (source_dir / "file2.txt").write_text("Content 2")

        collected = collect_abs_snapshot([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        upload_result = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(upload_result.manifest, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        download_manifest(manifest=download_manifest_obj, data_cache=data_cache)

        temp_files = list(download_dir.rglob("*.tmp*"))
        assert temp_files == [], f"Temp files left behind: {temp_files}"

    def test_uses_os_replace_for_atomic_write(self, tmp_path: Path) -> None:
        """Test that os.replace is called to atomically move temp file to target."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        (source_dir / "test.txt").write_text("Test content")

        collected = collect_abs_snapshot([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        upload_result = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(upload_result.manifest, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        expected_final_path = download_dir / "test.txt"

        import os

        original_replace = os.replace

        with patch(
            "deadline.job_attachments._snapshots._operations._download_manifest.os.replace"
        ) as mock_replace:
            mock_replace.side_effect = original_replace

            download_manifest(manifest=download_manifest_obj, data_cache=data_cache)

            # Verify os.replace was called exactly once for our single file
            assert mock_replace.call_count == 1
            temp_path, final_path = mock_replace.call_args[0]
            # Temp path should be the final path with .tmp suffix
            assert str(temp_path).startswith(str(expected_final_path))
            assert ".tmp" in str(temp_path)
            # Final path should be exactly the expected file path
            assert Path(final_path) == expected_final_path

    def test_temp_file_cleaned_up_on_error(self, tmp_path: Path) -> None:
        """Test that temp files are cleaned up when an error occurs during download."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        cache_root = tmp_path / "cache"
        download_dir = tmp_path / "download"
        download_dir.mkdir()

        (source_dir / "test.txt").write_text("Test content")

        collected = collect_abs_snapshot([source_dir], [])
        data_cache = self._create_filesystem_data_cache(cache_root)
        upload_result = hash_upload_manifest(collected, data_cache)

        rel_manifest = subtree_manifest(upload_result.manifest, str(source_dir))
        download_manifest_obj = _to_abs_snapshot(join_manifest(rel_manifest, str(download_dir)))

        with patch(
            "deadline.job_attachments._snapshots._operations._download_manifest.os.replace"
        ) as mock_replace:
            mock_replace.side_effect = OSError("Simulated error")

            with pytest.raises(OSError, match="Simulated error"):
                download_manifest(manifest=download_manifest_obj, data_cache=data_cache)

        # Verify no temp files are left behind after error
        temp_files = list(download_dir.rglob("*.tmp*"))
        assert temp_files == [], f"Temp files left behind after error: {temp_files}"
