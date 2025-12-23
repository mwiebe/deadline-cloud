# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Module that defines the hashing algorithms supported by this library."""

import io

from enum import Enum

from ..exceptions import UnsupportedHashingAlgorithmError


class HashAlgorithm(str, Enum):
    """
    Enumerant of all hashing algorithms supported by this library.

    Algorithms:
      XXH128 - The xxhash 128-bit hashing algorithm.

    """

    XXH128 = "xxh128"


def hash_file(
    file_path: str,
    hash_alg: HashAlgorithm,
    range_start: int = 0,
    range_end: int = -1,
) -> str:
    """
    Hashes the given file (or byte range) using the given hashing algorithm.

    Args:
        file_path: Path to the file to hash
        hash_alg: Hash algorithm to use
        range_start: Start byte offset (0 for beginning of file)
        range_end: End byte offset exclusive (-1 for end of file)

    Returns:
        Hex digest of the hash
    """
    if hash_alg == HashAlgorithm.XXH128:
        from xxhash import xxh3_128

        hasher = xxh3_128()
    else:
        raise UnsupportedHashingAlgorithmError(
            f"Unsupported hashing algorithm provided: {hash_alg}"
        )

    with open(file_path, "rb") as file:
        if range_start > 0:
            file.seek(range_start)

        # Calculate bytes to read
        if range_end == -1:
            # Read to end of file
            bytes_remaining = -1
        else:
            bytes_remaining = range_end - range_start

        while True:
            # Determine chunk size
            if bytes_remaining == -1:
                chunk_size = io.DEFAULT_BUFFER_SIZE
            else:
                chunk_size = min(io.DEFAULT_BUFFER_SIZE, bytes_remaining)
                if chunk_size <= 0:
                    break

            chunk = file.read(chunk_size)
            if not chunk:
                break

            hasher.update(chunk)

            if bytes_remaining != -1:
                bytes_remaining -= len(chunk)

        return hasher.hexdigest()


def hash_data(data: bytes, hash_alg: HashAlgorithm) -> str:
    """Hashes the given data bytes using the given hashing algorithm."""
    if hash_alg == HashAlgorithm.XXH128:
        from xxhash import xxh3_128

        hasher = xxh3_128()
    else:
        raise UnsupportedHashingAlgorithmError(
            f"Unsupported hashing algorithm provided: {hash_alg}"
        )

    hasher.update(data)
    return hasher.hexdigest()
