"""
Media file utilities.
"""

import hashlib
import mimetypes
from pathlib import Path
from typing import Optional, Tuple

import magic


def get_mime_type(file_path: Path) -> str:
    """
    Get the MIME type of a file.

    Args:
        file_path: Path to the file

    Returns:
        MIME type string
    """
    # Try python-magic first (more accurate)
    try:
        mime = magic.Magic(mime=True)
        return mime.from_file(str(file_path))
    except Exception:
        # Fallback to mimetypes
        mime_type, _ = mimetypes.guess_type(str(file_path))
        return mime_type or "application/octet-stream"


def get_media_type(file_path: Path) -> str:
    """
    Get the media type (video, image, audio) from a file.

    Args:
        file_path: Path to the file

    Returns:
        Media type: "video", "image", "audio", or "unknown"
    """
    mime_type = get_mime_type(file_path)

    if mime_type.startswith("video/"):
        return "video"
    elif mime_type.startswith("image/"):
        return "image"
    elif mime_type.startswith("audio/"):
        return "audio"
    else:
        return "unknown"


def calculate_checksum(file_path: Path, algorithm: str = "sha256") -> str:
    """
    Calculate the checksum of a file.

    Args:
        file_path: Path to the file
        algorithm: Hash algorithm (md5, sha1, sha256)

    Returns:
        Hexadecimal checksum string
    """
    hash_func = hashlib.new(algorithm)

    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hash_func.update(chunk)

    return hash_func.hexdigest()


def verify_checksum(file_path: Path, expected_checksum: str, algorithm: str = "sha256") -> bool:
    """
    Verify the checksum of a file.

    Args:
        file_path: Path to the file
        expected_checksum: Expected checksum value
        algorithm: Hash algorithm

    Returns:
        True if checksum matches, False otherwise
    """
    actual_checksum = calculate_checksum(file_path, algorithm)
    return actual_checksum == expected_checksum


def get_file_size(file_path: Path) -> int:
    """
    Get the size of a file in bytes.

    Args:
        file_path: Path to the file

    Returns:
        File size in bytes
    """
    return file_path.stat().st_size


def format_file_size(size_bytes: int) -> str:
    """
    Format file size in human-readable format.

    Args:
        size_bytes: Size in bytes

    Returns:
        Formatted size string (e.g., "1.5 GB")
    """
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


def is_supported_media(file_path: Path) -> bool:
    """
    Check if a file is a supported media type.

    Args:
        file_path: Path to the file

    Returns:
        True if supported, False otherwise
    """
    media_type = get_media_type(file_path)
    return media_type in ["video", "image", "audio"]


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename by removing/replacing invalid characters.

    Args:
        filename: Original filename

    Returns:
        Sanitized filename
    """
    # Replace invalid characters with underscores
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, "_")

    # Remove leading/trailing spaces and dots
    filename = filename.strip(". ")

    return filename
