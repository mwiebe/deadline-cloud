# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Platform-specific sparse file allocation utilities.

This module provides efficient file pre-allocation that avoids the slow
truncate() behavior on Windows. On Windows, truncate() can take 150-260ms
per 50MB file because it may zero-fill the file contents.

Platform behavior:
- Windows: Uses SetFilePointerEx + SetEndOfFile via ctypes to create sparse files
- Linux: Uses os.posix_fallocate() for efficient sparse allocation
- macOS/Other: Falls back to simple file creation without pre-allocation
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import BinaryIO

# Windows constants for ctypes
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    # Windows API constants
    FILE_BEGIN = 0
    INVALID_SET_FILE_POINTER = 0xFFFFFFFF

    # Load kernel32
    _kernel32 = ctypes.windll.kernel32

    # SetFilePointerEx: BOOL SetFilePointerEx(HANDLE, LARGE_INTEGER, PLARGE_INTEGER, DWORD)
    _SetFilePointerEx = _kernel32.SetFilePointerEx
    _SetFilePointerEx.argtypes = [
        wintypes.HANDLE,
        wintypes.LARGE_INTEGER,
        ctypes.POINTER(wintypes.LARGE_INTEGER),
        wintypes.DWORD,
    ]
    _SetFilePointerEx.restype = wintypes.BOOL

    # SetEndOfFile: BOOL SetEndOfFile(HANDLE)
    _SetEndOfFile = _kernel32.SetEndOfFile
    _SetEndOfFile.argtypes = [wintypes.HANDLE]
    _SetEndOfFile.restype = wintypes.BOOL

    # GetLastError
    _GetLastError = _kernel32.GetLastError
    _GetLastError.argtypes = []
    _GetLastError.restype = wintypes.DWORD

    # msvcrt._get_osfhandle to get Windows HANDLE from file descriptor
    import msvcrt

    def _preallocate_file_windows(file_obj: BinaryIO, size: int) -> None:
        """
        Pre-allocate a file on Windows using SetFilePointerEx + SetEndOfFile.

        This creates a sparse file without zero-filling, which is much faster
        than using truncate() which may zero-fill the entire file.

        Args:
            file_obj: An open file object in binary write mode
            size: The desired file size in bytes
        """
        if size <= 0:
            return

        # Get the Windows HANDLE from the Python file object
        fd = file_obj.fileno()
        handle = msvcrt.get_osfhandle(fd)

        # Move file pointer to the desired size
        new_pos = wintypes.LARGE_INTEGER()
        result = _SetFilePointerEx(
            wintypes.HANDLE(handle),
            wintypes.LARGE_INTEGER(size),
            ctypes.byref(new_pos),
            FILE_BEGIN,
        )
        if not result:
            error_code = _GetLastError()
            raise OSError(f"SetFilePointerEx failed with error code {error_code}")

        # Set the end of file at the current position
        result = _SetEndOfFile(wintypes.HANDLE(handle))
        if not result:
            error_code = _GetLastError()
            raise OSError(f"SetEndOfFile failed with error code {error_code}")


def preallocate_file(file_obj: BinaryIO, size: int) -> None:
    """
    Pre-allocate a file to the specified size using platform-specific methods.

    This function efficiently allocates disk space for a file without the
    performance penalty of zero-filling that can occur with truncate() on
    some platforms (especially Windows).

    Args:
        file_obj: An open file object in binary write mode ('wb' or 'r+b')
        size: The desired file size in bytes

    Raises:
        OSError: If the pre-allocation fails

    Platform behavior:
        - Windows: Uses SetFilePointerEx + SetEndOfFile (fast, no zero-fill)
        - Linux: Uses posix_fallocate (fast, reserves disk space)
        - macOS/Other: Uses ftruncate (may be slower on some filesystems)
    """
    if size <= 0:
        return

    if sys.platform == "win32":
        _preallocate_file_windows(file_obj, size)
    elif hasattr(os, "posix_fallocate"):
        # Linux - use posix_fallocate for efficient sparse allocation
        try:
            os.posix_fallocate(file_obj.fileno(), 0, size)
        except OSError:
            # Fall back to ftruncate if posix_fallocate fails
            # (e.g., on filesystems that don't support it)
            os.ftruncate(file_obj.fileno(), size)
    else:
        # macOS and other platforms - use ftruncate
        # macOS doesn't have posix_fallocate, but ftruncate is reasonably fast
        os.ftruncate(file_obj.fileno(), size)


def create_preallocated_file(path: Path, size: int) -> None:
    """
    Create a new file pre-allocated to the specified size.

    This is a convenience function that creates a file and pre-allocates it
    in a single operation.

    Args:
        path: Path where the file should be created
        size: The desired file size in bytes

    Raises:
        OSError: If file creation or pre-allocation fails
    """
    with open(path, "wb") as f:
        preallocate_file(f, size)
