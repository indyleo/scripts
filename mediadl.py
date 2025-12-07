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
- Real progress tracking and detailed logging
- Automatic retry with exponential backoff
- Download verification

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from random import uniform
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

# Constants
PROG_NAME = "mediadl"
VERSION = "2.1.0"
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
logger = logging.getLogger(PROG_NAME)


class DependencyError(RuntimeError):
    """Raised when required external tools are missing."""


class ConfigError(RuntimeError):
    """Raised when configuration is invalid."""


class DownloadError(RuntimeError):
    """Raised when a download fails."""


@dataclass
class Config:
    """Application configuration."""

    default_music_dir: Path = field(default_factory=lambda: Path.home() / "Music")
    default_video_dir: Path = field(default_factory=lambda: Path.home() / "Videos")
    default_workers: int = 4
    spotify_format: str = "mp3"
    spotify_bitrate: str = "320k"
    spotify_template: str = "%(album)s/%(title)s.%(ext)s"
    yt_default_quality: int = 1080
    yt_template: str = "%(upload_date)s - %(title)s [%(id)s].%(ext)s"
    use_archive: bool = True
    archive_file: Path = field(default_factory=lambda: DEFAULT_ARCHIVE)
    rate_limit_delay: float = 0.5
    max_retries: int = 3
    verify_downloads: bool = True
    # pylint: disable=too-many-instance-attributes

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


@dataclass
class DownloadResult:
    """Result of a download operation."""

    success: bool
    url: str
    task_type: str
    output_dir: Path
    skipped: bool = False
    error: Optional[str] = None
    files_created: int = 0
    retries: int = 0


class MediaDownloader:
    """Unified media downloader with parallel processing."""
    # pylint: disable=too-many-instance-attributes

    def __init__(self, config: Config, dry_run: bool = False, verbose: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.verbose = verbose
        self.archive: Set[str] = set()
        self._load_archive()
        self._archive_lock = threading.Lock()
        self._print_lock = threading.Lock()

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
        """Save an identifier to the archive (only called on successful downloads)."""
        if not self.config.use_archive:
            return

        with self._archive_lock:
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
                match = re.search(r"/(track|album|playlist)/([a-zA-Z0-9]+)", url)
                if match:
                    return f"spotify:{match.group(2)}"
            if task_type == "yt":
                parsed = urlparse(url)
                if "youtube.com" in parsed.netloc:
                    match = re.search(r"[?&]v=([^&]+)", url)
                    if match:
                        return f"youtube:{match.group(1)}"
                if "youtu.be" in parsed.netloc:
                    video_id = parsed.path.strip("/").split("/")[0]
                    if video_id:
                        return f"youtube:{video_id}"
        except Exception:  # pylint: disable=broad-except
            logger.debug("Failed to extract identifier from %s", url)
        return None

    def _is_archived(self, url: str, task_type: str) -> bool:
        """Check if URL has already been downloaded."""
        if not self.config.use_archive:
            return False
        with self._archive_lock:
            identifier = self._extract_identifier(url, task_type)
            return identifier in self.archive if identifier else False

    def _prepare_output_dir(self, path: Path) -> Path:
        """Prepare output directory."""
        path = path.expanduser().resolve()
        if not self.dry_run:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _verify_download(self, output_dir: Path, min_files: int = 1) -> Tuple[bool, int]:
        """Verify that files were actually created."""
        if not self.config.verify_downloads:
            return True, 0

        try:
            files = [f for f in output_dir.rglob("*") if f.is_file()]
            file_count = len(files)
            return file_count >= min_files, file_count
        except OSError:
            return False, 0

    def _print_status(self, message: str) -> None:
        """Thread-safe status printing."""
        with self._print_lock:
            print(message)

    def _rate_limit(self) -> None:
        """Apply rate limiting between downloads."""
        if self.config.rate_limit_delay > 0:
            delay = uniform(
                self.config.rate_limit_delay,
                self.config.rate_limit_delay * 1.5
            )
            time.sleep(delay)

    def _run_with_retry(
        self,
        cmd: List[str],
        task_name: str,
        max_retries: Optional[int] = None
    ) -> Tuple[bool, Optional[str], int]:
        """Run a command with exponential backoff retry logic."""
        max_retries = max_retries or self.config.max_retries

        for attempt in range(max_retries):
            try:
                if self.verbose:
                    logger.debug("Running: %s", " ".join(cmd))

                subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=600  # 10 minute timeout
                )
                return True, None, attempt

            except subprocess.CalledProcessError as exc:
                error_msg = f"Exit code {exc.returncode}"
                if exc.stderr:
                    # Get last 200 chars of stderr for context
                    stderr_snippet = exc.stderr.strip()[-200:]
                    error_msg += f": {stderr_snippet}"

                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff
                    logger.warning(
                        "%s failed (attempt %d/%d), retrying in %ds: %s",
                        task_name, attempt + 1, max_retries, wait_time, error_msg
                    )
                    time.sleep(wait_time)
                else:
                    return False, error_msg, attempt + 1

            except subprocess.TimeoutExpired:
                error_msg = "Download timed out after 10 minutes"
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(
                        "%s timed out (attempt %d/%d), retrying in %ds",
                        task_name, attempt + 1, max_retries, wait_time
                    )
                    time.sleep(wait_time)
                else:
                    return False, error_msg, attempt + 1

            except OSError as exc:
                return False, str(exc), attempt + 1

        return False, "Max retries exceeded", max_retries

    def download_spotify(self, task: DownloadTask) -> DownloadResult:
        """Download from Spotify using spotdl."""
        # pylint: disable=too-many-locals
        self._ensure_tool(REQUIRED_TOOLS["spotify"])

        if self._is_archived(task.url, "spotify"):
            logger.info("⏭️  Skipping archived: %s", task.url)
            return DownloadResult(
                success=True,
                url=task.url,
                task_type="spotify",
                output_dir=task.output_dir,
                skipped=True
            )

        output_dir = self._prepare_output_dir(task.output_dir)
        audio_format = task.options.get("format", self.config.spotify_format)
        bitrate = task.options.get("bitrate", self.config.spotify_bitrate)

        # Use configurable template
        template = str(output_dir / self.config.spotify_template)

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

        self._print_status(f"⬇️  Downloading: {task.url}")

        if self.dry_run:
            logger.info("[DRY RUN] %s", " ".join(cmd))
            self._print_status(f"✅  [DRY RUN] Would download: {task.url}")
            return DownloadResult(
                success=True,
                url=task.url,
                task_type="spotify",
                output_dir=output_dir
            )

        # Apply rate limiting
        self._rate_limit()

        # Run with retry logic
        success, error, retries = self._run_with_retry(cmd, "Spotify download")

        if success:
            # Verify download
            verified, file_count = self._verify_download(output_dir)

            if not verified:
                error = "Download verification failed - no files created"
                success = False
            else:
                identifier = self._extract_identifier(task.url, "spotify")
                if identifier:
                    self._save_to_archive(identifier)

                self._print_status(f"✅  Downloaded: {task.url} ({file_count} files)")
                return DownloadResult(
                    success=True,
                    url=task.url,
                    task_type="spotify",
                    output_dir=output_dir,
                    files_created=file_count,
                    retries=retries
                )

        self._print_status(f"❌  Failed: {task.url} - {error}")
        return DownloadResult(
            success=False,
            url=task.url,
            task_type="spotify",
            output_dir=output_dir,
            error=error,
            retries=retries
        )

    def download_yt(self, task: DownloadTask) -> DownloadResult:
        """Download from YouTube/other sites using yt-dlp."""
        # pylint: disable=too-many-locals,too-many-branches
        self._ensure_tool(REQUIRED_TOOLS["yt"])

        if self._is_archived(task.url, "yt"):
            logger.info("⏭️  Skipping archived: %s", task.url)
            return DownloadResult(
                success=True,
                url=task.url,
                task_type="yt",
                output_dir=task.output_dir,
                skipped=True
            )

        output_dir = self._prepare_output_dir(task.output_dir)

        audio_only = task.options.get("audio_only", False)
        video_quality = task.options.get("video_quality", self.config.yt_default_quality)
        playlist = task.options.get("playlist", False)
        yt_format = task.options.get("format")
        organize = task.options.get("organize", True)

        # Organize by channel if enabled and not a playlist
        if organize and not playlist:
            # Let yt-dlp handle channel organization via template
            template = str(
                output_dir / "%(uploader)s" /
                datetime.now().strftime("%Y-%m-%d") /
                self.config.yt_template
            )
        else:
            template = str(output_dir / self.config.yt_template)

        cmd = [
            "yt-dlp",
            "--no-overwrites",
            "--continue",
            "--retries",
            "10",
            "--fragment-retries",
            "10",
        ]

        # Add verbosity control
        if not self.verbose:
            cmd.extend(["--no-warnings", "--quiet", "--progress"])

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

        self._print_status(f"⬇️  Downloading: {task.url}")

        if self.dry_run:
            logger.info("[DRY RUN] %s", " ".join(cmd))
            self._print_status(f"✅  [DRY RUN] Would download: {task.url}")
            return DownloadResult(
                success=True,
                url=task.url,
                task_type="yt",
                output_dir=output_dir
            )

        # Apply rate limiting
        self._rate_limit()

        # Run with retry logic
        success, error, retries = self._run_with_retry(cmd, "YouTube download")

        if success:
            # Verify download
            verified, file_count = self._verify_download(output_dir)

            if not verified:
                error = "Download verification failed - no files created"
                success = False
            else:
                identifier = self._extract_identifier(task.url, "yt")
                if identifier:
                    self._save_to_archive(identifier)

                self._print_status(f"✅  Downloaded: {task.url} ({file_count} files)")
                return DownloadResult(
                    success=True,
                    url=task.url,
                    task_type="yt",
                    output_dir=output_dir,
                    files_created=file_count,
                    retries=retries
                )

        self._print_status(f"❌  Failed: {task.url} - {error}")
        return DownloadResult(
            success=False,
            url=task.url,
            task_type="yt",
            output_dir=output_dir,
            error=error,
            retries=retries
        )

    def process_batch(
        self, tasks: List[DownloadTask], workers: int = 4
    ) -> Dict[str, Any]:
        """Process multiple download tasks in parallel."""
        results = {
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "total_files": 0,
            "total_retries": 0,
            "errors": []
        }

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}

            for task in tasks:
                if task.task_type == "spotify":
                    future = executor.submit(self.download_spotify, task)
                elif task.task_type == "yt":
                    future = executor.submit(self.download_yt, task)
                else:
                    logger.error("Unknown task type: %s", task.task_type)
                    results["failed"] += 1
                    continue
                futures[future] = task

            for future in as_completed(futures):
                try:
                    result = future.result()

                    if result.skipped:
                        results["skipped"] += 1
                    elif result.success:
                        results["success"] += 1
                        results["total_files"] += result.files_created
                        results["total_retries"] += result.retries
                    else:
                        results["failed"] += 1
                        if result.error:
                            results["errors"].append({
                                "url": result.url,
                                "error": result.error
                            })

                except Exception as exc:  # pylint: disable=broad-except
                    task = futures[future]
                    logger.error("Task failed with exception: %s", exc)
                    results["failed"] += 1
                    results["errors"].append({
                        "url": task.url,
                        "error": str(exc)
                    })

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

    logger.info("Loaded %d URLs from batch file", len(urls))
    return urls


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROG_NAME,
        description="Unified media downloader with batch processing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG_NAME} {VERSION}")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose output for debugging"
    )

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
            logger.info("Available keys: %s", ", ".join(config.__dict__.keys()))
            return 1

        # Type conversion
        current_value = getattr(config, key)
        try:
            if isinstance(current_value, Path):
                setattr(config, key, Path(value))
            elif isinstance(current_value, int):
                setattr(config, key, int(value))
            elif isinstance(current_value, float):
                setattr(config, key, float(value))
            elif isinstance(current_value, bool):
                setattr(config, key, value.lower() in ("true", "yes", "1"))
            else:
                setattr(config, key, value)
            config.save()
            print(f"✅  Set {key} = {value}")
            return 0
        except ValueError as exc:
            logger.error("Invalid value for %s: %s", key, exc)
            return 1

    if args.reset:
        CONFIG_FILE.unlink(missing_ok=True)
        print("✅  Configuration reset to defaults")
        return 0

    logger.error("No config action specified. Use --show, --set, or --reset")
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Configure logging based on verbosity
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format=LOG_FORMAT, datefmt=DATE_FORMAT)

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
        downloader = MediaDownloader(config, dry_run=dry_run, verbose=args.verbose)

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
        print(f"\n{'='*60}")
        print(f"Starting {len(tasks)} download(s) with {workers} worker(s)")
        print(f"Output: {output_dir}")
        print(f"{'='*60}\n")

        results = downloader.process_batch(tasks, workers=workers)

        # Summary
        print(f"\n{'='*60}")
        print("DOWNLOAD SUMMARY")
        print(f"{'='*60}")
        print(f"✅  Successful:  {results['success']}")
        print(f"❌  Failed:      {results['failed']}")
        print(f"⏭️  Skipped:     {results['skipped']}")
        print(f"📁  Files:       {results['total_files']}")
        print(f"🔄  Retries:     {results['total_retries']}")

        if results["errors"]:
            print(f"\n{'='*60}")
            print("ERRORS")
            print(f"{'='*60}")
            for err in results["errors"][:10]:  # Show first 10 errors
                print(f"❌  {err['url']}")
                print(f"    {err['error']}")
            if len(results["errors"]) > 10:
                print(f"\n... and {len(results['errors']) - 10} more errors")

        print(f"{'='*60}\n")

        return 0 if results["failed"] == 0 else 1

    except DependencyError as exc:
        logger.error("Dependency error: %s", exc)
        return 2
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 3
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Interrupted by user")
        return 130
    except Exception as exc:
        logger.exception("Unhandled error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
