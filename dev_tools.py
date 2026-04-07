#!/usr/bin/env python3
"""
Development and testing utilities for the player daemon.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from config.settings import generate_default_config, load_settings
from cache.manager import CacheManager
from core.playlist import Playlist, PlaylistItem


def generate_config(output_path: str) -> None:
    """
    Generate a default configuration file.

    Args:
        output_path: Path to write configuration file
    """
    config_yaml = generate_default_config()

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with open(output, 'w') as f:
        f.write(config_yaml)

    print(f"Configuration file created: {output_path}")
    print("\nEdit this file with your specific settings before running the player.")


def validate_config(config_path: str) -> None:
    """
    Validate a configuration file.

    Args:
        config_path: Path to configuration file
    """
    try:
        settings = load_settings(Path(config_path))
        print(f"Configuration file is valid: {config_path}")
        print("\nSettings:")
        print(f"  Player ID: {settings.player.id}")
        print(f"  Player Name: {settings.player.name}")
        print(f"  API URL: {settings.api.base_url}")
        print(f"  Cache Directory: {settings.cache.directory}")
        print(f"  Max Cache Size: {settings.cache.max_size_gb} GB")

    except Exception as e:
        print(f"ERROR: Invalid configuration file: {e}")
        sys.exit(1)


def cache_stats(config_path: str) -> None:
    """
    Display cache statistics.

    Args:
        config_path: Path to configuration file
    """
    settings = load_settings(Path(config_path) if config_path else None)

    cache_size_bytes = settings.cache.max_size_gb * 1024 * 1024 * 1024
    cache_manager = CacheManager(
        cache_dir=settings.cache.directory,
        max_size_bytes=cache_size_bytes
    )

    stats = cache_manager.get_stats()

    print("Cache Statistics:")
    print(f"  Directory: {settings.cache.directory}")
    print(f"  Total Items: {stats['total_items']}")
    print(f"  Total Size: {stats['total_size_bytes'] / (1024*1024):.2f} MB")
    print(f"  Max Size: {stats['max_size_bytes'] / (1024*1024*1024):.2f} GB")
    print(f"  Used: {stats['used_percent']:.1f}%")


def clear_cache(config_path: str) -> None:
    """
    Clear the cache.

    Args:
        config_path: Path to configuration file
    """
    settings = load_settings(Path(config_path) if config_path else None)

    cache_size_bytes = settings.cache.max_size_gb * 1024 * 1024 * 1024
    cache_manager = CacheManager(
        cache_dir=settings.cache.directory,
        max_size_bytes=cache_size_bytes
    )

    response = input(f"Clear cache at {settings.cache.directory}? (yes/no): ")
    if response.lower() in ['yes', 'y']:
        cache_manager.clear()
        print("Cache cleared successfully")
    else:
        print("Operation cancelled")


def create_test_playlist(output_path: str) -> None:
    """
    Create a test playlist JSON file.

    Args:
        output_path: Path to write playlist file
    """
    playlist_data = {
        "id": "test-playlist",
        "name": "Test Playlist",
        "description": "A test playlist for development",
        "loop": True,
        "items": [
            {
                "id": "item-1",
                "content_id": "content-1",
                "duration": 0,
                "transition": "cut",
                "order": 0,
                "content": {
                    "id": "content-1",
                    "filename": "video1.mp4",
                    "path": "/path/to/video1.mp4",
                    "type": "video",
                    "mime_type": "video/mp4"
                }
            },
            {
                "id": "item-2",
                "content_id": "content-2",
                "duration": 10,
                "transition": "cut",
                "order": 1,
                "content": {
                    "id": "content-2",
                    "filename": "image1.jpg",
                    "path": "/path/to/image1.jpg",
                    "type": "image",
                    "mime_type": "image/jpeg"
                }
            }
        ]
    }

    output = Path(output_path)
    with open(output, 'w') as f:
        json.dump(playlist_data, f, indent=2)

    print(f"Test playlist created: {output_path}")
    print("\nEdit the file paths to point to actual media files on your system.")


def main():
    """Main entry point for dev tools."""
    parser = argparse.ArgumentParser(description="Player daemon development tools")
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')

    # Generate config command
    gen_config = subparsers.add_parser('generate-config', help='Generate default configuration file')
    gen_config.add_argument('output', help='Output file path')

    # Validate config command
    val_config = subparsers.add_parser('validate-config', help='Validate configuration file')
    val_config.add_argument('config', help='Configuration file path')

    # Cache stats command
    cache_stats_cmd = subparsers.add_parser('cache-stats', help='Display cache statistics')
    cache_stats_cmd.add_argument('--config', help='Configuration file path', default=None)

    # Clear cache command
    clear_cache_cmd = subparsers.add_parser('clear-cache', help='Clear the cache')
    clear_cache_cmd.add_argument('--config', help='Configuration file path', default=None)

    # Create test playlist command
    test_playlist = subparsers.add_parser('create-test-playlist', help='Create a test playlist file')
    test_playlist.add_argument('output', help='Output file path')

    args = parser.parse_args()

    if args.command == 'generate-config':
        generate_config(args.output)
    elif args.command == 'validate-config':
        validate_config(args.config)
    elif args.command == 'cache-stats':
        cache_stats(args.config)
    elif args.command == 'clear-cache':
        clear_cache(args.config)
    elif args.command == 'create-test-playlist':
        create_test_playlist(args.output)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
