#!/usr/bin/env python3
"""
mediadl.py

Unified media downloader using yt-dlp and spotdl with
archive tracking, retries, and metadata storage.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

PROG_NAME = "mediadl"
VERSION = "2.2.2"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

XDG_CONFIG_HOME = Path.home() / ".config"
XDG_DATA_HOME = Path.home() / ".local" / "share"

CONFIG_DIR = XDG_CONFIG_HOME / PROG_NAME
DATA_DIR = XDG_DATA_HOME / PROG_NAME

CONFIG_FILE = CONFIG_DIR / "config.json"
ARCHIVE_FILE = DATA_DIR / "archive.txt"
METADATA_FILE = DATA_DIR / "downloads.json"

logger = logging.getLogger(PROG_NAME)


class DependencyError(RuntimeError):
    """Raised when a required external tool is missing."""


class ConfigError(RuntimeError):
    """Raised when configuration loading or saving fails."""


@dataclass
class DownloadMetadata:  # pylint: disable=too-many-instance-attributes
    """Represents stored metadata for a completed download."""

    url: str
    identifier: str
    task_type: str
    download_time: str
    output_dir: str
    files: List[str]
    audio_format: Optional[str] = None
    video_quality: Optional[str] = None
    thumbnail_embedded: bool = False


@dataclass
class Config:  # pylint: disable=too-many-instance-attributes
    """Application configuration loaded from disk."""

    default_music_dir: Path = field(default_factory=lambda: Path.home() / "Music")
    default_video_dir: Path = field(default_factory=lambda: Path.home() / "Videos")
    yt_default_quality: int = 1080
    yt_template: str = "%(upload_date)s - %(title)s [%(id)s].%(ext)s"
    max_retries: int = 3
    embed_thumbnails: bool = True
    save_metadata_json: bool = True
    archive_file: Path = ARCHIVE_FILE
    metadata_file: Path = METADATA_FILE

    @classmethod
    def load(cls) -> "Config":
        """Load configuration from disk or return defaults."""
        if not CONFIG_FILE.exists():
            return cls()

        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
                raw = json.load(handle)

            cfg = cls()
            for key, value in raw.items():
                if hasattr(cfg, key):
                    setattr(
                        cfg,
                        key,
                        Path(value) if isinstance(getattr(cfg, key), Path) else value,
                    )
            return cfg
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(str(exc)) from exc


@dataclass
class DownloadTask:
    """Represents a single download request."""

    url: str
    output_dir: Path
    audio_only: bool = False


@dataclass
class DownloadResult:
    """Represents the result of a download attempt."""

    success: bool
    error: Optional[str] = None
    files_created: int = 0
    retries: int = 0


class MediaDownloader:  # pylint: disable=too-many-instance-attributes
    """Core download engine for yt-dlp operations."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.archive: Set[str] = set()
        self._archive_lock = threading.Lock()
        self._load_archive()

    def _load_archive(self) -> None:
        """Load archive identifiers from disk."""
        if not self.config.archive_file.exists():
            return

        with open(self.config.archive_file, "r", encoding="utf-8") as handle:
            self.archive = {line.strip() for line in handle if line.strip()}

    def _save_archive(self, identifier: str) -> None:
        """Persist a new identifier to the archive."""
        with self._archive_lock:
            self.archive.add(identifier)
            self.config.archive_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config.archive_file, "a", encoding="utf-8") as handle:
                handle.write(f"{identifier}\n")

    @staticmethod
    def _ensure_tool(name: str) -> None:
        """Ensure an external dependency exists."""
        if not shutil.which(name):
            raise DependencyError(f"Missing required tool: {name}")

    @staticmethod
    def _extract_identifier(url: str, audio_only: bool) -> Optional[str]:
        """Extract a stable archive identifier from a YouTube URL."""
        parsed = urlparse(url)

        if "youtube.com" in parsed.netloc:
            match = re.search(r"[?&]v=([^&]+)", url)
            if match:
                return f"yt:{match.group(1)}:{'audio' if audio_only else 'video'}"

        if "youtu.be" in parsed.netloc:
            vid = parsed.path.strip("/")
            return f"yt:{vid}:{'audio' if audio_only else 'video'}"

        return None

    @staticmethod
    def _snapshot_files(path: Path) -> Set[Path]:
        """Return a snapshot of files under a directory."""
        return {p for p in path.rglob("*") if p.is_file()}

    def download_yt(self, task: DownloadTask) -> DownloadResult:
        """Download a single YouTube URL using yt-dlp."""
        self._ensure_tool("yt-dlp")

        identifier = self._extract_identifier(task.url, task.audio_only)
        if identifier and identifier in self.archive:
            return DownloadResult(success=True)

        task.output_dir.mkdir(parents=True, exist_ok=True)
        before = self._snapshot_files(task.output_dir)

        cmd = [
            "yt-dlp",
            "--no-overwrites",
            "--continue",
            "--progress",
        ]

        if self.config.embed_thumbnails:
            cmd.append("--embed-thumbnail")

        if self.config.save_metadata_json:
            cmd.append("--write-info-json")

        if task.audio_only:
            cmd.extend(["-x", "--audio-format", "mp3", "--audio-quality", "0"])
        else:
            q = self.config.yt_default_quality
            cmd.extend(["-f", f"bestvideo[height<={q}]+bestaudio/best"])

        cmd.extend(["-o", str(task.output_dir / self.config.yt_template), task.url])

        retries = 0
        for attempt in range(self.config.max_retries):
            try:
                subprocess.run(cmd, check=True)
                retries = attempt
                break
            except subprocess.CalledProcessError:
                retries = attempt + 1
                if attempt == self.config.max_retries - 1:
                    return DownloadResult(False, "yt-dlp failed", retries=retries)
                time.sleep(2**attempt)

        after = self._snapshot_files(task.output_dir)
        new_files = after - before

        if not new_files:
            return DownloadResult(False, "No files created", retries=retries)

        if identifier:
            self._save_archive(identifier)

        return DownloadResult(True, files_created=len(new_files), retries=retries)


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(prog=PROG_NAME)
    subparsers = parser.add_subparsers(dest="command", required=True)

    yt_parser = subparsers.add_parser("yt", help="Download from YouTube")
    yt_parser.add_argument("-a", "--audio-only", action="store_true")
    yt_parser.add_argument("url", help="YouTube URL")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    config = Config.load()
    downloader = MediaDownloader(config)

    if args.command == "yt":
        output = (
            config.default_music_dir if args.audio_only else config.default_video_dir
        )
        task = DownloadTask(args.url, output, args.audio_only)
        result = downloader.download_yt(task)

        if not result.success:
            logger.error("Download failed: %s", result.error)
            return 1

        logger.info(
            "Download complete (%d files, %d retries)",
            result.files_created,
            result.retries,
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
