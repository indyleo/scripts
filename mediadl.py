#!/usr/bin/env python3
"""
mediadl.py - Unified media downloader with batch processing

Features:
- Spotify (spotdl) and YouTube (yt-dlp) download support
- Batch processing from files or URLs
- Parallel downloads with configurable workers
- Archive tracking to avoid re-downloads
- Config file support with XDG compliance
- Dry-run mode for testing
- Smart folder organization
- Progress tracking and detailed logging

Usage:
  mediadl spotify URL [URL...] [-o DIR] [--format flac]
  mediadl spotify --batch channels.txt [-j 4]
  mediadl yt URL [URL...] [-a] [-q 1080]
  mediadl yt --batch videos.txt [--archive archive.txt]
  mediadl config --set default_music_dir ~/Music

Requirements: spotdl, yt-dlp
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

# Constants
PROG_NAME = "mediadl"
VERSION = "2.0.0"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# XDG paths
XDG_CONFIG_HOME = Path.home() / ".config"
XDG_DATA_HOME = Path.home() / ".local" / "share"
CONFIG_DIR = XDG_CONFIG_HOME / PROG_NAME
DATA_DIR = XDG_DATA_HOME / PROG_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_ARCHIVE = DATA_DIR / "archive.txt"

REQUIRED_TOOLS = {"spotify": "spotdl", "yt": "yt-dlp"}

# Setup logging
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=DATE_FORMAT)
logger = logging.getLogger(PROG_NAME)


class DependencyError(RuntimeError):
    """Raised when required external tools are missing."""


class ConfigError(RuntimeError):
    """Raised when configuration is invalid."""


@dataclass
class Config:
    """Application configuration."""

    default_music_dir: Path = field(default_factory=lambda: Path.home() / "Music")
    default_video_dir: Path = field(default_factory=lambda: Path.home() / "Videos")
    default_workers: int = 4
    spotify_format: str = "mp3"
    spotify_bitrate: str = "320k"
    yt_default_quality: int = 1080
    use_archive: bool = True
    archive_file: Path = field(default_factory=lambda: DEFAULT_ARCHIVE)

    @classmethod
    def load(cls) -> Config:
        """Load configuration from file or return defaults."""
        if not CONFIG_FILE.exists():
            return cls()
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            config = cls()
            for key, value in data.items():
                if hasattr(config, key):
                    if key.endswith("_dir") or key == "archive_file":
                        setattr(config, key, Path(value))
                    else:
                        setattr(config, key, value)
            return config
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load config: %s. Using defaults.", exc)
            return cls()

    def save(self) -> None:
        """Save configuration to file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        for key, value in self.__dict__.items():
            if isinstance(value, Path):
                data[key] = str(value)
            else:
                data[key] = value
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info("Configuration saved to %s", CONFIG_FILE)
        except OSError as exc:
            raise ConfigError(f"Failed to save config: {exc}") from exc


@dataclass
class DownloadTask:
    """Represents a single download task."""

    url: str
    output_dir: Path
    task_type: str  # 'spotify' or 'yt'
    options: Dict[str, Any] = field(default_factory=dict)


class MediaDownloader:
    """Unified media downloader with parallel processing."""

    def __init__(self, config: Config, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.archive: Set[str] = set()
        self._load_archive()
        self._progress_lock = threading.Lock()

    def _print_progress(self, message: str, end: str = "\n", update: bool = False):
        """Print progress message with timestamp and proper formatting."""
        timestamp = datetime.now().strftime(DATE_FORMAT)
        formatted = f"{timestamp} [INFO] {PROG_NAME}: {message}"

        with self._progress_lock:
            if update:
                # Move cursor to beginning of line and clear it
                sys.stdout.write("\r\033[K")
                sys.stdout.write(formatted)
                sys.stdout.flush()
            else:
                if end == "\n":
                    print(formatted)
                else:
                    sys.stdout.write(formatted)
                    sys.stdout.flush()

    def _load_archive(self) -> None:
        """Load download archive to track completed downloads."""
        if not self.config.use_archive:
            return
        archive_path = self.config.archive_file
        if archive_path.exists():
            try:
                with open(archive_path, "r", encoding="utf-8") as f:
                    self.archive = {line.strip() for line in f if line.strip()}
                logger.info("Loaded %d archived entries", len(self.archive))
            except OSError as exc:
                logger.warning("Failed to load archive: %s", exc)

    def _save_to_archive(self, identifier: str) -> None:
        """Save an identifier to the archive."""
        if not self.config.use_archive:
            return
        self.archive.add(identifier)
        archive_path = self.config.archive_file
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(archive_path, "a", encoding="utf-8") as f:
                f.write(f"{identifier}\n")
        except OSError as exc:
            logger.warning("Failed to save to archive: %s", exc)

    @staticmethod
    def _ensure_tool(name: str) -> None:
        """Ensure an external tool is available."""
        if not shutil.which(name):
            raise DependencyError(
                f"Required tool '{name}' not found. "
                f"Install it with: pip install {name}"
            )

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Sanitize a string for use as a filename."""
        name = re.sub(r'[<>:"/\\|?*]', "_", name)
        name = re.sub(r"_+", "_", name)
        return name.strip("_. ")

    def _extract_identifier(self, url: str, task_type: str) -> Optional[str]:
        """Extract a unique identifier from URL for archiving."""
        try:
            if task_type == "spotify":
                # Extract Spotify ID from URL
                match = re.search(r"/(track|album|playlist)/([a-zA-Z0-9]+)", url)
                if match:
                    return f"spotify:{match.group(2)}"
            if task_type == "yt":
                # Extract video ID
                parsed = urlparse(url)
                if "youtube.com" in parsed.netloc:
                    match = re.search(r"[?&]v=([^&]+)", url)
                    if match:
                        return f"youtube:{match.group(1)}"
                if "youtu.be" in parsed.netloc:
                    video_id = parsed.path.strip("/").split("/")[0]
                    if video_id:
                        return f"youtube:{video_id}"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("Failed to extract identifier from %s: %s", url, exc)
        return None

    def _is_archived(self, url: str, task_type: str) -> bool:
        """Check if URL has already been downloaded."""
        if not self.config.use_archive:
            return False
        identifier = self._extract_identifier(url, task_type)
        return identifier in self.archive if identifier else False

    def _prepare_output_dir(self, path: Path) -> Path:
        """Prepare output directory."""
        path = path.expanduser().resolve()
        if not self.dry_run:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _get_spotify_metadata(self, url: str) -> Dict[str, str]:
        """Extract metadata from Spotify URL."""
        try:
            cmd = [
                "spotdl",
                "meta",
                "--format",
                "json",
                url,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, check=False
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                # Handle both single track and playlist/album
                if isinstance(data, list) and data:
                    first = data[0]
                    return {
                        "name": first.get("name", "Unknown"),
                        "artist": first.get("artist", "Unknown Artist"),
                        "type": first.get("type", "track"),
                    }
                if isinstance(data, dict):
                    return {
                        "name": data.get("name", "Unknown"),
                        "artist": data.get("artist", "Unknown Artist"),
                        "type": data.get("type", "track"),
                    }
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
            logger.debug("Failed to get Spotify metadata: %s", exc)
        return {"name": "Unknown", "artist": "Unknown Artist", "type": "track"}

    def _get_yt_metadata(self, url: str) -> Dict[str, str]:
        """Extract metadata from YouTube URL."""
        try:
            cmd = [
                "yt-dlp",
                "--no-warnings",
                "--skip-download",
                "--print",
                "%(title)s|||%(channel)s|||%(uploader)s",
                url,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, check=False
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split("|||")
                title = parts[0] if parts else "Unknown"
                channel_name = (
                    parts[1]
                    if len(parts) > 1 and parts[1]
                    else (parts[2] if len(parts) > 2 else "Unknown Channel")
                )
                return {
                    "title": title,
                    "channel": self._sanitize_filename(channel_name),
                }
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("Failed to get YouTube metadata: %s", exc)

        # Fallback
        parsed = urlparse(url)
        return {
            "title": "Unknown",
            "channel": self._sanitize_filename(parsed.netloc or "unknown"),
        }

    def _get_channel_name(self, url: str) -> str:
        """Extract channel name from YouTube URL."""
        metadata = self._get_yt_metadata(url)
        return metadata["channel"]

    def _simulate_progress(self, name: str, duration: float = 2.0):
        """Simulate a progress bar for downloads."""
        steps = 20
        sleep_time = duration / steps

        for i in range(steps + 1):
            percent = int((i / steps) * 100)
            bar_filled = "█" * i
            bar_empty = "░" * (steps - i)
            progress_msg = f"  - [ ] {name} [{bar_filled}{bar_empty}] {percent}%"
            self._print_progress(progress_msg, update=True)
            if i < steps:
                time.sleep(sleep_time)

    def download_spotify(self, task: DownloadTask) -> bool:
        """Download from Spotify using spotdl."""
        self._ensure_tool(REQUIRED_TOOLS["spotify"])

        if self._is_archived(task.url, "spotify"):
            logger.info("Skipping archived Spotify URL: %s", task.url)
            return True

        # Get metadata for display
        metadata = self._get_spotify_metadata(task.url)
        name = metadata["name"]
        artist = metadata["artist"]

        output_dir = self._prepare_output_dir(task.output_dir)

        audio_format = task.options.get("format", self.config.spotify_format)
        bitrate = task.options.get("bitrate", self.config.spotify_bitrate)

        # Use album/playlist folder structure
        template = str(output_dir / "%(album)s" / "%(title)s.%(ext)s")

        cmd = [
            "spotdl",
            "download",
            "--format",
            audio_format,
            "--bitrate",
            bitrate,
            "--output",
            template,
            task.url,
        ]

        # Display progress indicator
        metadata_line = f"{artist}"
        initial_msg = f"  - [ ] {name}, {metadata_line}"
        self._print_progress(initial_msg)

        if self.dry_run:
            logger.info("[DRY RUN] %s", " ".join(cmd))
            final_msg = f"  - [X] {name}, {metadata_line} (dry run)"
            self._print_progress(final_msg, update=True)
            print()  # New line after update
            return True

        # Start progress bar in background
        progress_thread = threading.Thread(
            target=self._simulate_progress, args=(name,), daemon=True
        )
        progress_thread.start()

        try:
            # Suppress all output for clean logs
            subprocess.run(
                cmd,
                check=True,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Wait for progress to complete
            progress_thread.join()

            identifier = self._extract_identifier(task.url, "spotify")
            if identifier:
                self._save_to_archive(identifier)

            final_msg = f"  - [X] {name}, {metadata_line}"
            self._print_progress(final_msg, update=True)
            print()  # New line after completion
            return True
        except subprocess.CalledProcessError as exc:
            progress_thread.join()
            error_msg = (
                f"  - [✗] {name}, {metadata_line} (failed: exit code {exc.returncode})"
            )
            self._print_progress(error_msg, update=True)
            print()
            return False
        except OSError as exc:
            progress_thread.join()
            error_msg = f"  - [✗] {name}, {metadata_line} (failed: {exc})"
            self._print_progress(error_msg, update=True)
            print()
            return False

    def download_yt(self, task: DownloadTask) -> bool:
        """Download from YouTube/other sites using yt-dlp."""
        self._ensure_tool(REQUIRED_TOOLS["yt"])

        if self._is_archived(task.url, "yt"):
            logger.info("Skipping archived YouTube URL: %s", task.url)
            return True

        # Get metadata for display
        metadata = self._get_yt_metadata(task.url)
        title = metadata["title"]
        channel = metadata["channel"]

        output_dir = self._prepare_output_dir(task.output_dir)

        audio_only = task.options.get("audio_only", False)
        video_quality = task.options.get(
            "video_quality", self.config.yt_default_quality
        )
        playlist = task.options.get("playlist", False)
        yt_format = task.options.get("format")
        organize = task.options.get("organize", True)

        # Determine format type for display
        if audio_only:
            format_type = "audio"
        elif yt_format:
            format_type = "custom"
        else:
            format_type = f"video (≤{video_quality}p)"

        # Organize by channel/uploader if enabled
        if organize and not playlist:
            output_dir = output_dir / channel / datetime.now().strftime("%Y-%m-%d")
            output_dir = self._prepare_output_dir(output_dir)

        template = str(output_dir / "%(upload_date)s - %(title)s [%(id)s].%(ext)s")

        cmd = [
            "yt-dlp",
            "--no-overwrites",
            "--continue",
            "--retries",
            "10",
            "--fragment-retries",
            "10",
            "--no-warnings",
            "--quiet",
            "--progress",
        ]

        if audio_only:
            cmd.extend(["-x", "--audio-format", "mp3", "--audio-quality", "0"])
        elif yt_format:
            cmd.extend(["-f", yt_format])
        elif video_quality:
            selector = (
                f"bestvideo[height<={video_quality}]+bestaudio/"
                f"best[height<={video_quality}]"
            )
            cmd.extend(["-f", selector])

        if not playlist:
            cmd.append("--no-playlist")

        cmd.extend(["-o", template, task.url])

        # Display progress indicator
        metadata_line = f"{format_type}, {channel}"
        initial_msg = f"  - [ ] {title}, {metadata_line}"
        self._print_progress(initial_msg)

        if self.dry_run:
            logger.info("[DRY RUN] %s", " ".join(cmd))
            final_msg = f"  - [X] {title}, {metadata_line} (dry run)"
            self._print_progress(final_msg, update=True)
            print()  # New line after update
            return True

        # Start progress bar in background
        progress_thread = threading.Thread(
            target=self._simulate_progress,
            args=(title, 3.0),  # Slightly longer for videos
            daemon=True,
        )
        progress_thread.start()

        try:
            # Suppress all output including warnings
            subprocess.run(
                cmd,
                check=True,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Wait for progress to complete
            progress_thread.join()

            identifier = self._extract_identifier(task.url, "yt")
            if identifier:
                self._save_to_archive(identifier)

            final_msg = f"  - [X] {title}, {metadata_line}"
            self._print_progress(final_msg, update=True)
            print()  # New line after completion
            return True
        except subprocess.CalledProcessError as exc:
            progress_thread.join()
            error_msg = (
                f"  - [✗] {title}, {metadata_line} (failed: exit code {exc.returncode})"
            )
            self._print_progress(error_msg, update=True)
            print()
            return False
        except OSError as exc:
            progress_thread.join()
            error_msg = f"  - [✗] {title}, {metadata_line} (failed: {exc})"
            self._print_progress(error_msg, update=True)
            print()
            return False

    def process_batch(
        self, tasks: List[DownloadTask], workers: int = 4
    ) -> Dict[str, int]:
        """Process multiple download tasks in parallel."""
        results = {"success": 0, "failed": 0, "skipped": 0}

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = []
            for task in tasks:
                if task.task_type == "spotify":
                    future = executor.submit(self.download_spotify, task)
                elif task.task_type == "yt":
                    future = executor.submit(self.download_yt, task)
                else:
                    logger.error("Unknown task type: %s", task.task_type)
                    results["failed"] += 1
                    continue
                futures.append(future)

            for future in futures:
                try:
                    success = future.result()
                    if success:
                        results["success"] += 1
                    else:
                        results["failed"] += 1
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logger.error("Task failed with exception: %s", exc)
                    results["failed"] += 1

        return results


def parse_batch_file(filepath: Path) -> List[str]:
    """Parse a batch file containing URLs."""
    urls = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    except OSError as exc:
        raise ConfigError(f"Failed to read batch file {filepath}: {exc}") from exc
    return urls


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROG_NAME,
        description="Unified media downloader with batch processing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG_NAME} {VERSION}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Spotify subcommand
    spotify_parser = subparsers.add_parser("spotify", help="Download from Spotify")
    spotify_parser.add_argument("urls", nargs="*", help="Spotify URLs to download")
    spotify_parser.add_argument(
        "-b", "--batch", help="Batch file with URLs (one per line)"
    )
    spotify_parser.add_argument("-o", "--output", help="Output directory")
    spotify_parser.add_argument(
        "--format", choices=["mp3", "flac", "ogg", "m4a"], help="Audio format"
    )
    spotify_parser.add_argument("--bitrate", help="Audio bitrate (e.g., 320k, 256k)")
    spotify_parser.add_argument(
        "-j", "--jobs", type=int, help="Number of parallel downloads"
    )
    spotify_parser.add_argument(
        "-n", "--dry-run", action="store_true", help="Show what would be done"
    )

    # YouTube subcommand
    yt_parser = subparsers.add_parser(
        "yt", help="Download from YouTube and other sites"
    )
    yt_parser.add_argument("urls", nargs="*", help="URLs to download")
    yt_parser.add_argument("-b", "--batch", help="Batch file with URLs (one per line)")
    yt_parser.add_argument("-o", "--output", help="Output directory")
    yt_parser.add_argument(
        "-a", "--audio-only", action="store_true", help="Extract audio only"
    )
    yt_parser.add_argument(
        "-q", "--quality", type=int, help="Max video height (e.g., 1080, 720)"
    )
    yt_parser.add_argument("-f", "--format", help="Custom yt-dlp format selector")
    yt_parser.add_argument(
        "-p", "--playlist", action="store_true", help="Download entire playlist"
    )
    yt_parser.add_argument(
        "--no-organize", action="store_true", help="Don't organize by channel"
    )
    yt_parser.add_argument(
        "-j", "--jobs", type=int, help="Number of parallel downloads"
    )
    yt_parser.add_argument(
        "-n", "--dry-run", action="store_true", help="Show what would be done"
    )
    yt_parser.add_argument("--archive", help="Custom archive file path")

    # Config subcommand
    cfg_parser = subparsers.add_parser("config", help="Manage configuration")
    cfg_parser.add_argument(
        "--show", action="store_true", help="Show current configuration"
    )
    cfg_parser.add_argument(
        "--set", nargs=2, metavar=("KEY", "VALUE"), help="Set config value"
    )
    cfg_parser.add_argument("--reset", action="store_true", help="Reset to defaults")

    return parser


def handle_config_command(args: argparse.Namespace) -> int:
    """Handle config subcommand."""
    config = Config.load()

    if args.show:
        print(f"Configuration file: {CONFIG_FILE}")
        print(f"Archive file: {config.archive_file}")
        print("\nSettings:")
        for key, value in config.__dict__.items():
            print(f"  {key}: {value}")
        return 0

    if args.set:
        key, value = args.set
        if not hasattr(config, key):
            logger.error("Unknown config key: %s", key)
            return 1
        # Type conversion
        current_value = getattr(config, key)
        if isinstance(current_value, Path):
            setattr(config, key, Path(value))
        elif isinstance(current_value, int):
            setattr(config, key, int(value))
        elif isinstance(current_value, bool):
            setattr(config, key, value.lower() in ("true", "yes", "1"))
        else:
            setattr(config, key, value)
        config.save()
        print(f"Set {key} = {value}")
        return 0

    if args.reset:
        CONFIG_FILE.unlink(missing_ok=True)
        print("Configuration reset to defaults")
        return 0

    logger.error("No config action specified. Use --show, --set, or --reset")
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "config":
        return handle_config_command(args)

    config = Config.load()

    try:
        # Determine URLs
        urls = list(args.urls) if args.urls else []
        if hasattr(args, "batch") and args.batch:
            batch_urls = parse_batch_file(Path(args.batch))
            urls.extend(batch_urls)

        if not urls:
            logger.error("No URLs provided. Use positional arguments or --batch")
            return 1

        # Determine output directory
        if args.output:
            output_dir = Path(args.output)
        elif args.command == "spotify":
            output_dir = config.default_music_dir
        elif args.command == "yt":
            if args.audio_only:
                output_dir = config.default_music_dir
            else:
                output_dir = config.default_video_dir
        else:
            output_dir = Path.cwd()

        # Number of parallel jobs
        workers = getattr(args, "jobs", None) or config.default_workers

        # Handle custom archive for yt
        if args.command == "yt" and hasattr(args, "archive") and args.archive:
            config.archive_file = Path(args.archive)

        # Create downloader
        dry_run = getattr(args, "dry_run", False)
        downloader = MediaDownloader(config, dry_run=dry_run)

        # Build tasks
        tasks = []
        for url in urls:
            task_options: Dict[str, Any] = {}

            if args.command == "spotify":
                if hasattr(args, "format") and args.format:
                    task_options["format"] = args.format
                if hasattr(args, "bitrate") and args.bitrate:
                    task_options["bitrate"] = args.bitrate
            elif args.command == "yt":
                task_options["audio_only"] = getattr(args, "audio_only", False)
                task_options["playlist"] = getattr(args, "playlist", False)
                task_options["organize"] = not getattr(args, "no_organize", False)
                if hasattr(args, "quality") and args.quality:
                    task_options["video_quality"] = args.quality
                if hasattr(args, "format") and args.format:
                    task_options["format"] = args.format

            task = DownloadTask(
                url=url,
                output_dir=output_dir,
                task_type=args.command,
                options=task_options,
            )
            tasks.append(task)

        # Execute downloads
        logger.info("Starting %d download(s) with %d worker(s)", len(tasks), workers)
        results = downloader.process_batch(tasks, workers=workers)

        # Summary
        logger.info(
            "Complete: %d succeeded, %d failed, %d skipped",
            results["success"],
            results["failed"],
            results["skipped"],
        )

        return 0 if results["failed"] == 0 else 1

    except DependencyError as exc:
        logger.error("Dependency error: %s", exc)
        return 2
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 3
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("Unhandled error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
