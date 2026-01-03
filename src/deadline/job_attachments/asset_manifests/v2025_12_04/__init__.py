# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
EXPERIMENTAL: Asset manifest format v2025-12.

This module provides encode/decode functions for the v2025-12 manifest format,
which uses a specificationVersion field to identify the manifest type.

This format is under development and subject to change. Do not use in production.
"""

from .decode import decode_v2025
from .encode import encode_v2025

__all__ = [
    "encode_v2025",
    "decode_v2025",
]
