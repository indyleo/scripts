#!/usr/bin/env python3
"""
mediadl.py — Unified media downloader

Features restored:
- spotify + yt subcommands
- multiple URLs per invocation
- batch files
- dry-run
- archive + metadata history
- safe Ctrl+C
- per-playlist subfolders
- playlist index prefixes (01 - title)
- folder structure: Artist/Album or Channel/Date
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# ────────────────────────────── Constants ──────────────────────────────

PROG_NAME = "mediadl.py"
VERSION = "2.4.0"

XDG_CONFIG_HOME = Path.home() / ".config"
XDG_DATA_HOME = Path.home() / ".local" / "share"

CONFIG_DIR = XDG_CONFIG_HOME / PROG_NAME
DATA_DIR = XDG_DATA_HOME / PROG_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
ARCHIVE_FILE = DATA_DIR / "archive.txt"
METADATA_DB = DATA_DIR / "downloads.json"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

REQUIRED_TOOLS = {"spotify": "spotdl", "yt": "yt-dlp"}

# Prefer mp4/m4a first (fast merge, no re-encode, broad compatibility),
# fall back to whatever's best overall if mp4/m4a isn't available.
QUALITY_PRESETS = {
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo*+bestaudio/best",
    "4k": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/"
    "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
    "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
    "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/"
    "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "worst": "worstvideo*+worstaudio/worst",
}

logger = logging.getLogger(PROG_NAME)

# ────────────────────────────── Exceptions ──────────────────────────────


class DependencyError(RuntimeError):
    """Missing external dependency."""


class ConfigError(RuntimeError):
    """Invalid configuration."""


# ────────────────────────────── Data Models ──────────────────────────────


@dataclass
class DownloadMetadata:
    """Metadata describing a completed download."""

    url: str
    identifier: str
    task_type: str
    download_time: str
    output_dir: str
    files: List[str]


@dataclass
class Config:
    """Application configuration."""

    default_music_dir: Path = Path.home() / "Music"
    default_video_dir: Path = Path.home() / "Videos"
    default_workers: int = 4

    spotify_template: str = (
        "%(artist)s/%(album)s/%(track_number)02d - %(title)s.%(ext)s"
    )
    # Fixed template: use proper yt-dlp syntax
    yt_template: str = (
        "%(uploader,channel)s/%(upload_date>%Y-%m-%d)s/" "%(title)s [%(id)s].%(ext)s"
    )

    embed_thumbnails: bool = True
    use_archive: bool = True
    max_retries: int = 3
    retry_delay: int = 5

    @classmethod
    def load(cls) -> "Config":
        """Load configuration from disk."""
        if not CONFIG_FILE.exists():
            return cls()
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        cfg = cls()
        for k, v in raw.items():
            if not hasattr(cfg, k):
                continue
            current = getattr(cfg, k)
            try:
                if isinstance(current, Path):
                    val = Path(v)
                elif isinstance(current, bool):
                    val = bool(v)
                elif isinstance(current, int):
                    val = int(v)
                elif isinstance(current, str):
                    val = str(v)
                else:
                    val = v
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"Invalid value for '{k}' in {CONFIG_FILE}: {v!r}"
                ) from exc
            setattr(cfg, k, val)
        return cfg

    def save(self) -> None:
        """Save configuration to disk."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    k: str(v) if isinstance(v, Path) else v
                    for k, v in self.__dict__.items()
                },
                f,
                indent=2,
            )


@dataclass
class DownloadTask:
    """Single download task."""

    url: str
    task_type: str
    output_dir: Path
    options: Dict[str, Any] = field(default_factory=dict)


# ────────────────────────────── Downloader ──────────────────────────────


class MediaDownloader:
    """Unified Spotify / YouTube downloader."""

    def __init__(self, config: Config, dry_run: bool, verbose: bool):
        self.config = config
        self.dry_run = dry_run
        self.verbose = verbose
        self.stop_event = threading.Event()
        self.archive: Set[str] = set()
        self._load_archive()

    # ───── Utility ─────

    def _load_archive(self) -> None:
        """Load archive of previously downloaded items."""
        if ARCHIVE_FILE.exists():
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                self.archive = {line.strip() for line in f if line.strip()}

    def _save_archive(self, identifier: str) -> None:
        """Save identifier to archive."""
        if identifier in self.archive:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(ARCHIVE_FILE, "a", encoding="utf-8") as f:
            f.write(identifier + "\n")
        self.archive.add(identifier)

    def _record_metadata(self, task: "DownloadTask") -> None:
        """Append a completed-download record to the metadata DB."""
        record = DownloadMetadata(
            url=task.url,
            identifier=task.url,
            task_type=task.task_type,
            download_time=time.strftime(DATE_FORMAT),
            output_dir=str(task.output_dir),
            files=[],
        )
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        history: List[Dict[str, Any]] = []
        if METADATA_DB.exists():
            try:
                with open(METADATA_DB, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("Could not read %s, starting fresh", METADATA_DB)
        history.append(record.__dict__)
        with open(METADATA_DB, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

    @staticmethod
    def _ensure_tool(name: str) -> None:
        """Verify required tool is installed."""
        if not shutil.which(name):
            raise DependencyError(f"Required tool '{name}' not found")

    def _run_with_retry(self, cmd: List[str], max_retries: int = 0) -> None:
        """Run command with retry logic."""
        if max_retries == 0:
            max_retries = self.config.max_retries

        for attempt in range(max_retries):
            if self.stop_event.is_set():
                raise RuntimeError("Cancelled (stop requested)")

            try:
                if self.verbose:
                    logger.info("Running: %s", " ".join(cmd))

                subprocess.run(cmd, check=True)
                return
            except subprocess.CalledProcessError:
                if self.stop_event.is_set():
                    raise RuntimeError("Cancelled (stop requested)") from None
                if attempt < max_retries - 1:
                    logger.warning(
                        "Download failed (attempt %d/%d), retrying in %ds...",
                        attempt + 1,
                        max_retries,
                        self.config.retry_delay,
                    )
                    time.sleep(self.config.retry_delay)
                else:
                    logger.error("Download failed after %d attempts", max_retries)
                    raise

    # ───── Spotify ─────

    def download_spotify(self, task: DownloadTask) -> None:
        """Download from Spotify using spotdl."""
        self._ensure_tool(REQUIRED_TOOLS["spotify"])

        template = str(task.output_dir / self.config.spotify_template)

        cmd = [
            "spotdl",
            "download",
            "--output",
            template,
        ]

        if self.config.embed_thumbnails:
            cmd.append("--embed-metadata")

        if self.config.use_archive:
            cmd.extend(["--save-file", str(ARCHIVE_FILE)])

        cmd.append(task.url)

        if self.dry_run:
            print("[DRY-RUN]", " ".join(cmd))
            return

        self._run_with_retry(cmd)

    # ───── YouTube ─────

    def download_yt(self, task: DownloadTask) -> None:
        """Download from YouTube using yt-dlp."""
        self._ensure_tool(REQUIRED_TOOLS["yt"])

        audio_only = task.options.get("audio_only", False)
        playlist = task.options.get("playlist", False)

        # Ensure archive directory exists if using archive
        if self.config.use_archive:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            ARCHIVE_FILE.touch(exist_ok=True)

        template = str(task.output_dir / self.config.yt_template)

        cmd = [
            "yt-dlp",
            "--no-overwrites",
            "--continue",
            "--newline",
            "--no-warnings",
            "-o",
            template,
        ]

        # Better playlist handling
        if playlist:
            cmd.extend(
                [
                    "--yes-playlist",
                    "--playlist-start",
                    "1",
                ]
            )
        else:
            cmd.append("--no-playlist")

        if self.config.embed_thumbnails:
            cmd.extend(
                [
                    "--embed-thumbnail",
                    "--convert-thumbnails",
                    "jpg",
                ]
            )

        # Better metadata embedding
        cmd.extend(
            [
                "--embed-metadata",
                "--parse-metadata",
                "%(title)s:%(meta_title)s",
                "--parse-metadata",
                "%(uploader)s:%(meta_artist)s",
            ]
        )

        quality = task.options.get("quality", "best")

        if audio_only:
            if quality != "best":
                logger.warning(
                    "--quality %r has no effect with --audio-only; ignoring", quality
                )
            cmd.extend(
                [
                    "-x",
                    "--audio-format",
                    "mp3",
                    "--audio-quality",
                    "0",
                    "--embed-thumbnail",
                ]
            )
        else:
            # Resolve preset name to a yt-dlp format string, or pass through
            # a raw format string the user supplied directly.
            fmt = QUALITY_PRESETS.get(quality, quality)
            cmd.extend(
                [
                    "-f",
                    fmt,
                    "--merge-output-format",
                    "mp4",
                ]
            )

        if self.config.use_archive:
            cmd.extend(["--download-archive", str(ARCHIVE_FILE)])

        # Add cookies support for restricted content
        cookies_file = CONFIG_DIR / "cookies.txt"
        if cookies_file.exists():
            cmd.extend(["--cookies", str(cookies_file)])

        cmd.append(task.url)

        if self.dry_run:
            print("[DRY-RUN]", " ".join(cmd))
            return

        self._run_with_retry(cmd)

    # ───── Batch Processor ─────

    def process(self, tasks: List[DownloadTask], workers: int) -> None:
        """Process download tasks with thread pool."""
        if not tasks:
            logger.warning("No tasks to process")
            return

        logger.info("Processing %d task(s) with %d worker(s)", len(tasks), workers)

        if workers > 1 and any(t.task_type == "spotify" for t in tasks):
            logger.warning(
                "Running multiple spotdl processes in parallel (--workers %d) can "
                "trigger Spotify/YouTube rate limiting; consider -w 1 for spotify "
                "batches if you see failures.",
                workers,
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for task in tasks:
                if self.stop_event.is_set():
                    break
                fn = (
                    self.download_spotify
                    if task.task_type == "spotify"
                    else self.download_yt
                )
                future = pool.submit(fn, task)
                futures[future] = task

            completed = 0
            failed = 0
            for future in as_completed(futures):
                if self.stop_event.is_set():
                    logger.info("Stop requested, cancelling remaining tasks...")
                    break
                try:
                    future.result()
                    completed += 1
                    task = futures[future]
                    logger.info("Completed: %s", task.url)
                    if not self.dry_run:
                        self._record_metadata(task)
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    failed += 1
                    logger.error("Failed: %s - %s", futures[future].url, exc)

        logger.info("Done: %d succeeded, %d failed", completed, failed)


# ────────────────────────────── CLI ──────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROG_NAME, description="Unified media downloader"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--version", action="version", version=f"{PROG_NAME} {VERSION}")

    sub = parser.add_subparsers(dest="command", required=True)

    # Spotify subcommand
    spotify_parser = sub.add_parser("spotify", help="Download from Spotify")
    spotify_parser.add_argument("urls", nargs="*", help="Spotify URLs")
    spotify_parser.add_argument(
        "-b", "--batch", help="File containing URLs (one per line)"
    )
    spotify_parser.add_argument(
        "-n", "--dry-run", action="store_true", help="Show what would be done"
    )
    spotify_parser.add_argument(
        "-w", "--workers", type=int, help="Number of parallel downloads"
    )

    # YouTube subcommand
    yt_parser = sub.add_parser("yt", help="Download from YouTube")
    yt_parser.add_argument("urls", nargs="*", help="YouTube URLs")
    yt_parser.add_argument("-b", "--batch", help="File containing URLs (one per line)")
    yt_parser.add_argument(
        "-a", "--audio-only", action="store_true", help="Extract audio only"
    )
    yt_parser.add_argument(
        "-p",
        "--playlist",
        action="store_true",
        help="Download entire playlist",
    )
    yt_parser.add_argument(
        "-q",
        "--quality",
        default="best",
        help=(
            "Video quality: "
            + ", ".join(QUALITY_PRESETS)
            + ", or a raw yt-dlp format string (default: best)"
        ),
    )
    yt_parser.add_argument(
        "--list-formats",
        action="store_true",
        help="List available formats for each URL and exit (no download)",
    )
    yt_parser.add_argument(
        "-n", "--dry-run", action="store_true", help="Show what would be done"
    )
    yt_parser.add_argument(
        "-w", "--workers", type=int, help="Number of parallel downloads"
    )

    # Config subcommand
    cfg_parser = sub.add_parser("config", help="Manage configuration")
    cfg_parser.add_argument(
        "--show", action="store_true", help="Show current configuration"
    )
    cfg_parser.add_argument(
        "--init",
        action="store_true",
        help="Initialize default configuration",
    )

    return parser


# ────────────────────────────── Main ──────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point."""
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
    )

    config = Config.load()

    if args.command == "config":
        if args.show:
            print(json.dumps(config.__dict__, indent=2, default=str))
            return 0
        if args.init:
            config.save()
            logger.info("Configuration saved to %s", CONFIG_FILE)
            return 0
        return 0

    downloader = MediaDownloader(
        config, dry_run=getattr(args, "dry_run", False), verbose=args.verbose
    )

    def sigint_handler(_signum: int, _frame: Any) -> None:
        """Handle SIGINT (Ctrl+C)."""
        logger.warning("Ctrl+C received, stopping…")
        downloader.stop_event.set()

    signal.signal(signal.SIGINT, sigint_handler)

    # Collect URLs
    urls = list(args.urls or [])
    if hasattr(args, "batch") and args.batch:
        try:
            with open(args.batch, "r", encoding="utf-8") as f:
                batch_urls = [
                    line.strip()
                    for line in f
                    if line.strip() and not line.strip().startswith("#")
                ]
                urls.extend(batch_urls)
        except FileNotFoundError:
            logger.error("Batch file not found: %s", args.batch)
            return 1

    if not urls:
        logger.error("No URLs provided")
        return 1

    if args.command == "yt" and getattr(args, "list_formats", False):
        MediaDownloader._ensure_tool(REQUIRED_TOOLS["yt"])
        for url in urls:
            print(f"\n=== {url} ===")
            subprocess.run(["yt-dlp", "-F", url], check=False)
        return 0

    # Build tasks
    tasks: List[DownloadTask] = []
    for url in urls:
        opts: Dict[str, Any] = {}
        if args.command == "yt":
            opts["audio_only"] = args.audio_only
            opts["playlist"] = args.playlist
            opts["quality"] = args.quality

        is_music = args.command == "spotify" or (
            args.command == "yt" and args.audio_only
        )
        out = config.default_music_dir if is_music else config.default_video_dir
        tasks.append(
            DownloadTask(url=url, task_type=args.command, output_dir=out, options=opts)
        )

    # Determine worker count
    workers = getattr(args, "workers", None) or config.default_workers

    # Process
    try:
        downloader.process(tasks, workers)
        return 0
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 130
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("Fatal error: %s", exc)
        if args.verbose:
            import traceback  # pylint: disable=import-outside-toplevel

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
