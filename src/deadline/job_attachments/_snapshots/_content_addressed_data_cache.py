# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Content-addressable data cache classes for the HASH_UPLOAD operation.

This module defines the abstract base class and concrete implementations for
content-addressable storage backends used by hash_upload_abs_manifest().

Classes:
    ContentAddressedDataCache: Abstract base class for data caches
    S3DataCache: Data cache backed by Amazon S3
    FileSystemDataCache: Data cache backed by a local or network file system
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..caches.s3_check_cache import S3CheckCache, S3CheckCacheEntry


# Default part size for S3 multipart uploads/downloads (32MB)
DEFAULT_S3_MULTIPART_PART_SIZE = 32 * 1024 * 1024  # 32MB


@dataclass
class ContentAddressedDataCache(ABC):
    """
    Abstract base class for content-addressable data caches.

    A content-addressable data cache stores data using the content's hash as the key.
    This enables deduplication and efficient retrieval of data by its content.

    Subclasses must implement:
        - get_object_key(): Returns the storage key/path for a given hash
        - object_exists(): Checks if an object with the given hash already exists
    """

    @abstractmethod
    def get_object_key(self, hash_value: str, algorithm: str) -> str:
        """
        Returns the storage key/path for a given hash.

        Args:
            hash_value: The hash of the content
            algorithm: The hash algorithm used (e.g., "xxh128")

        Returns:
            The storage key or path where the content should be stored
        """
        ...

    @abstractmethod
    def object_exists(self, hash_value: str, algorithm: str) -> bool:
        """
        Checks if an object with the given hash already exists.

        Args:
            hash_value: The hash of the content
            algorithm: The hash algorithm used (e.g., "xxh128")

        Returns:
            True if the object exists, False otherwise
        """
        ...


@dataclass
class S3DataCache(ContentAddressedDataCache):
    """
    Content-addressable data cache backed by Amazon S3.

    Files are stored with keys in the format:
        {s3_key_prefix}/{hash}.{algorithm}

    Example: Data/a1b2c3d4e5f67890abcdef1234567890.xxh128

    Attributes:
        s3_bucket: The S3 bucket name
        s3_key_prefix: The key prefix for content-addressable storage (e.g., "Data")
        s3_client: A boto3 S3 client with permissions for GetObject, PutObject, HeadObject
        s3_check_cache: Optional cache to avoid redundant S3 existence checks
        multipart_part_size: Part size for multipart uploads/downloads (default: 32MB)
        force_s3_check: If True, skip the s3_check_cache and always make HeadObject calls
    """

    s3_bucket: str
    s3_key_prefix: str
    s3_client: Any  # boto3 S3 client
    s3_check_cache: Optional[S3CheckCache] = field(default=None)
    multipart_part_size: int = field(default=DEFAULT_S3_MULTIPART_PART_SIZE)
    force_s3_check: bool = field(default=False)

    def get_object_key(self, hash_value: str, algorithm: str) -> str:
        """Returns the S3 key for a given hash."""
        return f"{self.s3_key_prefix}/{hash_value}.{algorithm}"

    def get_cache_key(self, hash_value: str, algorithm: str) -> str:
        """Returns the cache key for a given hash (bucket/key format)."""
        return f"{self.s3_bucket}/{self.get_object_key(hash_value, algorithm)}"

    def get_check_cache_entry(self, hash_value: str, algorithm: str) -> Optional[S3CheckCacheEntry]:
        """
        Check if hash exists in the S3 check cache (without HeadObject).

        Returns the cache entry if found, None otherwise.
        """
        if self.s3_check_cache is None or self.force_s3_check:
            return None
        return self.s3_check_cache.get_entry(self.get_cache_key(hash_value, algorithm))

    def head_object_exists(
        self, hash_value: str, algorithm: str, expected_bucket_owner: Optional[str] = None
    ) -> bool:
        """
        Check if object exists in S3 using HeadObject (bypassing cache).

        Args:
            hash_value: The hash of the content
            algorithm: The hash algorithm used (e.g., "xxh128")
            expected_bucket_owner: Optional AWS account ID for ExpectedBucketOwner

        Returns:
            True if the object exists, False if not found

        Raises:
            ClientError: For S3 errors other than 404
        """
        key = self.get_object_key(hash_value, algorithm)
        try:
            head_kwargs = {"Bucket": self.s3_bucket, "Key": key}
            if expected_bucket_owner is not None:
                head_kwargs["ExpectedBucketOwner"] = expected_bucket_owner
            self.s3_client.head_object(**head_kwargs)
            return True
        except self.s3_client.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise

    def object_exists(self, hash_value: str, algorithm: str) -> bool:
        """
        Checks if an object with the given hash exists in S3.

        First checks the local s3_check_cache if available (unless force_s3_check
        is True), then falls back to an S3 HeadObject call.

        Args:
            hash_value: The hash of the content
            algorithm: The hash algorithm used (e.g., "xxh128")

        Returns:
            True if the object exists, False otherwise
        """
        # Check local cache first (unless force_s3_check is True)
        if self.get_check_cache_entry(hash_value, algorithm) is not None:
            return True

        # Fall back to S3 HeadObject
        return self.head_object_exists(hash_value, algorithm)


@dataclass
class FileSystemDataCache(ContentAddressedDataCache):
    """
    Content-addressable data cache backed by a local or network file system.

    Files are stored with paths in the format:
        {root_path}/{hash}.{algorithm}

    Example: /mnt/cache/a1b2c3d4e5f67890abcdef1234567890.xxh128

    This is useful for:
        - Creating portable debug snapshots (zip files)
        - Local testing without S3
        - Network-attached storage caches

    Attributes:
        root_path: Absolute path to the root directory for the cache
    """

    root_path: Path

    def __post_init__(self) -> None:
        """Validate that root_path is absolute."""
        if not self.root_path.is_absolute():
            raise ValueError(f"root_path must be absolute, got: {self.root_path}")

    def get_object_key(self, hash_value: str, algorithm: str) -> str:
        """Returns the file path for a given hash."""
        return str(self.root_path / f"{hash_value}.{algorithm}")

    def object_exists(self, hash_value: str, algorithm: str) -> bool:
        """
        Checks if a file with the given hash exists on the filesystem.

        Args:
            hash_value: The hash of the content
            algorithm: The hash algorithm used (e.g., "xxh128")

        Returns:
            True if the file exists, False otherwise
        """
        return (self.root_path / f"{hash_value}.{algorithm}").exists()
