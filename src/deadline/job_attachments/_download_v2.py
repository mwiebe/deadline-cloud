# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Refactored download functions using the snapshots composable operations.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, cast

import boto3

from .asset_manifests.base_manifest import BaseAssetManifest
from .asset_manifests.v2023_03_03 import AssetManifest
from .models import FileConflictResolution
from .progress_tracker import (
    DownloadSummaryStatistics,
    ProgressReportMetadata,
    ProgressStatus,
)
from ._aws.aws_clients import get_s3_client
from ..client.config import config_file


def download_files_from_manifests_v2(
    s3_bucket: str,
    manifests_by_root: Dict[str, BaseAssetManifest],
    cas_prefix: Optional[str] = None,
    session: Optional[boto3.Session] = None,
    on_downloading_files: Optional[Callable[[ProgressReportMetadata], bool]] = None,
    conflict_resolution: FileConflictResolution = FileConflictResolution.CREATE_COPY,
) -> DownloadSummaryStatistics:
    """
    Download files from manifests using the snapshots library.

    Args:
        s3_bucket: The name of the S3 bucket.
        manifests_by_root: A map from each local root path to a corresponding manifest.
        cas_prefix: The CAS prefix of the files.
        session: The boto3 session to use.
        on_downloading_files: A callback to periodically report progress.
            Returns True to continue, False to cancel.
        conflict_resolution: How to handle file conflicts.

    Returns:
        Download summary statistics.
    """
    from ._snapshots import (
        AbsSnapshotDiff,
        download_abs_manifest,
        join_manifest,
        compose_manifests,
        v2023_manifest_to_snapshot_diff,
        S3DataCache,
        NO_ACCOUNT_ID_CHECK,
        DownloadProgressMetadata,
    )
    from .caches.hash_cache import HashCache

    if not manifests_by_root:
        return DownloadSummaryStatistics(
            total_files=0,
            total_bytes=0,
            processed_files=0,
            processed_bytes=0,
            skipped_files=0,
            skipped_bytes=0,
        )

    # Convert v2023 manifests to diffs and join with absolute roots
    abs_diffs = []
    for root_path, manifest in manifests_by_root.items():
        # v2023_manifest_to_snapshot_diff expects AssetManifest (v2023 format)
        snapshot_diff = v2023_manifest_to_snapshot_diff(
            cast(AssetManifest, manifest), parent_manifest_hash=None
        )
        abs_diff = join_manifest(snapshot_diff, root_path)
        abs_diffs.append(abs_diff)

    # Compose diffs into single diff - result is AbsSnapshotDiff since all inputs are diffs
    combined = compose_manifests(abs_diffs) if len(abs_diffs) > 1 else abs_diffs[0]

    # Create S3 data cache
    s3_client = get_s3_client(session=session)
    s3_data_cache = S3DataCache(
        s3_bucket=s3_bucket,
        s3_key_prefix=cas_prefix or "Data",
        s3_client=s3_client,
        account_id=NO_ACCOUNT_ID_CHECK,
    )

    # Adapt progress callback
    def on_progress(metadata: DownloadProgressMetadata) -> bool:
        if on_downloading_files:
            return on_downloading_files(
                ProgressReportMetadata(
                    status=ProgressStatus.DOWNLOAD_IN_PROGRESS,
                    progress=metadata.progress,
                    transferRate=0,
                    progressMessage=metadata.progressMessage,
                    processedFiles=metadata.downloaded_file_chunks + metadata.skipped_file_chunks,
                )
            )
        return True

    cache_dir = config_file.get_cache_directory()
    with HashCache(cache_dir) as hash_cache:
        result = download_abs_manifest(
            manifest=cast(AbsSnapshotDiff, combined),
            data_cache=s3_data_cache,
            hash_cache=hash_cache,
            file_conflict_resolution=conflict_resolution,
            on_progress=on_progress,
        )

    # Convert snapshots DownloadProgressMetadata to progress_tracker DownloadSummaryStatistics
    stats = result.statistics

    return DownloadSummaryStatistics(
        total_time=stats.total_time,
        total_files=stats.total_file_chunks,
        total_bytes=stats.total_bytes,
        processed_files=stats.downloaded_file_chunks,
        processed_bytes=stats.downloaded_bytes,
        skipped_files=stats.skipped_file_chunks,
        skipped_bytes=stats.skipped_bytes,
        transfer_rate=stats.transfer_rate,
    )
