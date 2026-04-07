# Changelog

All notable changes to the Digital Signage Player will be documented in this file.

## [1.0.0] - 2024

### Added

#### Core Features
- Media player engine using mpv with support for videos and images
- Playlist execution engine with sequential playback
- Seamless transitions with frame-holding capability
- Support for looping playlists

#### API Integration
- HTTP client for CMS API communication
- Heartbeat system with status reporting (every 15 seconds)
- Get assigned playlist from API
- Command handling system for remote control:
  - Play/pause/stop commands
  - Next/previous navigation
  - Playlist loading

#### Caching System
- Local cache manager with SQLite database
- LRU (Least Recently Used) eviction policy
- Configurable cache size limits
- Content integrity verification with checksums
- Prefetch capability for upcoming playlist items
- Offline playback support

#### Network Features
- Network share monitoring using watchdog
- Support for local paths, SMB, and NFS shares
- Auto-discovery of media files
- File change detection (add/modify/delete)

#### Configuration
- YAML-based configuration system
- Pydantic settings validation
- Environment variable support
- Multiple config file search paths

#### System Integration
- Systemd service configuration
- Logging to console and file
- Signal handling for graceful shutdown
- Auto-start on boot capability

#### Development Tools
- Comprehensive test suite with pytest
- Development utilities script (dev_tools.py)
- Configuration generation and validation tools
- Cache management utilities
- Test playlist generator

#### Documentation
- Complete README with installation instructions
- Quick start guide
- Systemd service setup guide
- Troubleshooting section
- API integration documentation

### Technical Details

#### Architecture
- Asynchronous design using asyncio
- Modular component structure
- Type hints throughout codebase
- Event-driven player callbacks

#### Dependencies
- python-mpv for media playback
- httpx for async HTTP client
- watchdog for file system monitoring
- pydantic for configuration validation
- PyYAML for config file parsing

#### Testing
- Unit tests for playlist management
- Cache manager tests
- Configuration loading tests
- Test coverage reporting

### Known Limitations

- SMB/NFS shares must be pre-mounted at system level
- No built-in authentication for API (relies on network security)
- Frame-perfect transitions depend on mpv capabilities
- Requires X11 display server (no Wayland support yet)

### Future Enhancements

Potential features for future releases:
- WebSocket support for real-time commands
- Advanced transition effects (fade, slide, etc.)
- Audio-only content support
- Multi-zone playback
- Performance metrics reporting
- Auto-update capability
- Web-based configuration interface
- Support for streaming content (RTSP, HTTP)

## Version History

- **1.0.0** - Initial release with full feature set
