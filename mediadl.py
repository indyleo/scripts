#!/usr/bin/env python3
"""
mediadl.py - polished, pylint-friendly unified media downloader

Features:
- Subcommands: `spotify` and `yt` (yt-dlp)
- Dataclasses for configuration
- Logging instead of prints
- Dependency checks
- Clear error handling
- Automatic output folders for Spotify albums/playlists
- Default output dirs: Music for audio, Videos for video
- No redefinition of builtins; good type hints

Usage examples:
  mediadl spotify https://open.spotify.com/track/... --audio-format flac -o ~/Music
  mediadl yt https://youtube.com/watch?v=... --audio-only -q 720 -o ~/Videos

Note: this script shells out to `spotdl` and `yt-dlp`. Make sure they're installed.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

LOG_FORMAT = "%(asctime)s %(levelname)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("mediadl")

REQUIRED_TOOLS = {
    "spotify": "spotdl",
    "yt": "yt-dlp",
}


@dataclass
class SpotifyConfig:
    """Configuration for downloading Spotify tracks/playlists via spotdl."""

    urls: List[str]
    output_dir: Optional[Path] = None
    audio_format: str = "mp3"
    bitrate: str = "best"


@dataclass
class YTDLPConfig:
    """Configuration for downloading YouTube/other media via yt-dlp."""

    urls: List[str]
    output_dir: Optional[Path] = None
    yt_format: Optional[str] = None
    audio_only: bool = False
    video_quality: Optional[int] = None
    playlist: bool = False


class DependencyError(RuntimeError):
    """Raised when required external tools are missing."""


class MediaDownloader:
    """High-level media downloader using spotdl and yt-dlp."""

    def __init__(self) -> None:
        pass

    @staticmethod
    def _ensure_tool(name: str) -> None:
        """Ensure an external tool is available on PATH."""
        if not shutil.which(name):
            raise DependencyError(
                f"Required tool '{name}' is not installed or not on PATH"
            )

    @staticmethod
    def _prepare_output_dir(path: Optional[Path]) -> Optional[Path]:
        if path is None:
            return None
        path = path.expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def download_spotify(self, cfg: SpotifyConfig) -> int:
        """Download Spotify tracks/playlists using spotdl."""
        self._ensure_tool(REQUIRED_TOOLS["spotify"])
        output_path = self._prepare_output_dir(cfg.output_dir)

        cmd: List[str] = ["spotdl", "download"]

        if cfg.audio_format:
            cmd.extend(["--format", cfg.audio_format])
        if cfg.bitrate:
            cmd.extend(["--bitrate", cfg.bitrate])
        if output_path:
            # Organize tracks into album/playlist folders
            template = str(output_path / "%(album)s/%(title)s.%(ext)s")
            cmd.extend(["--output", template])

        cmd.extend(cfg.urls)

        logger.info("Starting Spotify download for %d URL(s)", len(cfg.urls))
        logger.debug("Running command: %s", " ".join(cmd))

        try:
            subprocess.run(cmd, check=True, text=True)
            logger.info("Spotify download completed successfully")
            return 0
        except subprocess.CalledProcessError as exc:
            logger.error("spotdl returned non-zero exit status: %s", exc.returncode)
            return exc.returncode
        except OSError as exc:
            logger.exception("Failed to execute spotdl: %s", exc)
            return 2

    def download_yt(self, cfg: YTDLPConfig) -> int:
        """Download media using yt-dlp according to configuration."""
        self._ensure_tool(REQUIRED_TOOLS["yt"])
        output_path = self._prepare_output_dir(cfg.output_dir)

        cmd: List[str] = ["yt-dlp"]

        if cfg.audio_only:
            cmd.extend(["-x", "--audio-format", "mp3", "--audio-quality", "0"])
        if cfg.yt_format:
            cmd.extend(["-f", cfg.yt_format])
        elif cfg.video_quality and not cfg.audio_only:
            selector = (
                f"bestvideo[height<={cfg.video_quality}]+bestaudio/"
                f"best[height<={cfg.video_quality}]"
            )
            cmd.extend(["-f", selector])
        if not cfg.playlist:
            cmd.append("--no-playlist")
        if output_path:
            template = str(output_path / "%(title)s.%(ext)s")
            cmd.extend(["-o", template])
        cmd.extend(cfg.urls)

        logger.info("Starting yt-dlp for %d URL(s)", len(cfg.urls))
        logger.debug("Running command: %s", " ".join(cmd))

        try:
            subprocess.run(cmd, check=True, text=True)
            logger.info("Media download completed successfully")
            return 0
        except subprocess.CalledProcessError as exc:
            logger.error("yt-dlp returned non-zero exit status: %s", exc.returncode)
            return exc.returncode
        except OSError as exc:
            logger.exception("Failed to execute yt-dlp: %s", exc)
            return 2


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="mediadl",
        description="Unified media download tool (spotdl + yt-dlp)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    sp = subparsers.add_parser(
        "spotify", help="Download tracks/playlists from Spotify using spotdl"
    )
    sp.add_argument("urls", nargs="+", help="Spotify track/playlist/album URLs")
    sp.add_argument(
        "-o", "--output", dest="output_dir", help="Output directory", metavar="DIR"
    )
    sp.add_argument(
        "--audio-format",
        dest="audio_format",
        default="mp3",
        help="Audio format (mp3, flac, ogg, m4a)",
    )
    sp.add_argument(
        "--bitrate",
        dest="bitrate",
        default="best",
        help="Audio bitrate or quality (best, 320k, 256k, etc.)",
    )

    yt = subparsers.add_parser("yt", help="Download media using yt-dlp")
    yt.add_argument("urls", nargs="+", help="Media URLs to download")
    yt.add_argument(
        "-o", "--output", dest="output_dir", help="Output directory", metavar="DIR"
    )
    yt.add_argument(
        "--yt-format",
        dest="yt_format",
        help="Custom yt-dlp format selector (overrides --video-quality)",
    )
    yt.add_argument(
        "-a",
        "--audio-only",
        action="store_true",
        dest="audio_only",
        help="Extract audio only",
    )
    yt.add_argument(
        "-q",
        "--video-quality",
        dest="video_quality",
        type=int,
        help="Maximum video height (e.g. 1080, 720, 480)",
    )
    yt.add_argument(
        "-p",
        "--playlist",
        action="store_true",
        dest="playlist",
        help="Download entire playlist",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint for mediadl."""
    parser = build_parser()
    args = parser.parse_args(argv)
    downloader = MediaDownloader()

    try:
        if args.command == "spotify":
            output_dir = (
                Path(args.output_dir) if args.output_dir else Path.home() / "Music"
            )
            cfg = SpotifyConfig(
                urls=args.urls,
                output_dir=output_dir,
                audio_format=args.audio_format,
                bitrate=args.bitrate,
            )
            return downloader.download_spotify(cfg)

        if args.command == "yt":
            output_dir = (
                Path(args.output_dir)
                if args.output_dir
                else Path.home() / ("Music" if args.audio_only else "Videos")
            )
            cfg = YTDLPConfig(
                urls=args.urls,
                output_dir=output_dir,
                yt_format=args.yt_format,
                audio_only=bool(args.audio_only),
                video_quality=args.video_quality,
                playlist=bool(args.playlist),
            )
            return downloader.download_yt(cfg)

        logger.error("Unknown command: %s", args.command)
        return 3

    except DependencyError as exc:
        logger.error("Dependency error: %s", exc)
        logger.info("Install the missing tool and ensure it is on your PATH")
        return 4
    except KeyboardInterrupt:
        logger.warning("Download cancelled by user")
        return 130
    except BaseException as exc:
        logger.exception("Unhandled error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
