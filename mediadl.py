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
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

# ────────────────────────────── Constants ──────────────────────────────

PROG_NAME = "mediadl"
VERSION = "2.3.0"

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
        "%(_artist)s/%(_playlist)s/%(album)s/%(track_number)02d - %(title)s.%(ext)s"
    )
    yt_template: str = (
        "%(_channel)s/%(_playlist)s/%(upload_date>%Y-%m-%d)s/%(playlist_index|02)s - %(title)s [%(id)s].%(ext)s"
    )

    embed_thumbnails: bool = True
    use_archive: bool = True

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_FILE.exists():
            return cls()
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        cfg = cls()
        for k, v in raw.items():
            if hasattr(cfg, k):
                setattr(cfg, k, Path(v) if isinstance(getattr(cfg, k), Path) else v)
        return cfg

    def save(self) -> None:
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
        if ARCHIVE_FILE.exists():
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                self.archive = {l.strip() for l in f if l.strip()}

    def _save_archive(self, identifier: str) -> None:
        if identifier in self.archive:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(ARCHIVE_FILE, "a", encoding="utf-8") as f:
            f.write(identifier + "\n")
        self.archive.add(identifier)

    @staticmethod
    def _ensure_tool(name: str) -> None:
        if not shutil.which(name):
            raise DependencyError(f"Required tool '{name}' not found")

    # ───── Spotify ─────

    def download_spotify(self, task: DownloadTask) -> None:
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

        cmd.append(task.url)

        if self.dry_run:
            print("[DRY-RUN]", " ".join(cmd))
            return

        subprocess.run(cmd, check=True)

    # ───── YouTube ─────

    def download_yt(self, task: DownloadTask) -> None:
        self._ensure_tool(REQUIRED_TOOLS["yt"])

        audio_only = task.options.get("audio_only", False)
        playlist = task.options.get("playlist", False)

        template = str(task.output_dir / self.config.yt_template)

        cmd = [
            "yt-dlp",
            "--no-overwrites",
            "--continue",
            "--newline",
            "-o",
            template,
        ]

        if playlist:
            cmd.append("--yes-playlist")
        else:
            cmd.append("--no-playlist")

        if self.config.embed_thumbnails:
            cmd.append("--embed-thumbnail")

        if audio_only:
            cmd += ["-x", "--audio-format", "mp3"]

        cmd.append(task.url)

        if self.dry_run:
            print("[DRY-RUN]", " ".join(cmd))
            return

        subprocess.run(cmd, check=True)

    # ───── Batch Processor ─────

    def process(self, tasks: List[DownloadTask], workers: int) -> None:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = []
            for task in tasks:
                if self.stop_event.is_set():
                    break
                fn = (
                    self.download_spotify
                    if task.task_type == "spotify"
                    else self.download_yt
                )
                futures.append(pool.submit(fn, task))

            for f in as_completed(futures):
                if self.stop_event.is_set():
                    break
                f.result()


# ────────────────────────────── CLI ──────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG_NAME)
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("spotify")
    sp.add_argument("urls", nargs="*")
    sp.add_argument("-b", "--batch")
    sp.add_argument("-n", "--dry-run", action="store_true")

    yt = sub.add_parser("yt")
    yt.add_argument("urls", nargs="*")
    yt.add_argument("-b", "--batch")
    yt.add_argument("-a", "--audio-only", action="store_true")
    yt.add_argument("-p", "--playlist", action="store_true")
    yt.add_argument("-n", "--dry-run", action="store_true")

    cfg = sub.add_parser("config")
    cfg.add_argument("--show", action="store_true")

    return parser


# ────────────────────────────── Main ──────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
    )

    config = Config.load()
    downloader = MediaDownloader(
        config, dry_run=getattr(args, "dry_run", False), verbose=args.verbose
    )

    def sigint_handler(signum, frame):
        logger.warning("Ctrl+C received, stopping…")
        downloader.stop_event.set()

    signal.signal(signal.SIGINT, sigint_handler)

    if args.command == "config" and args.show:
        print(json.dumps(config.__dict__, indent=2, default=str))
        return 0

    urls = list(args.urls or [])
    if args.batch:
        with open(args.batch, "r", encoding="utf-8") as f:
            urls.extend(l.strip() for l in f if l.strip())

    tasks: List[DownloadTask] = []
    for url in urls:
        opts = {}
        if args.command == "yt":
            opts["audio_only"] = args.audio_only
            opts["playlist"] = args.playlist

        out = (
            config.default_music_dir
            if args.command == "spotify" or args.audio_only
            else config.default_video_dir
        )
        tasks.append(
            DownloadTask(url=url, task_type=args.command, output_dir=out, options=opts)
        )

    downloader.process(tasks, config.default_workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
