#!/usr/bin/env python3
"""
Download Organizer: Auto-organize downloads by file type.
"""

import argparse
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, Set

# Third-party dependency
try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    print(
        "Error: 'watchdog' library not found. Please install it using: pip install watchdog"
    )
    sys.exit(1)

# === Default config ===
DEFAULT_CONFIG = {
    "downloads_dir": str(Path.home() / "Downloads"),
    "dirs": {
        "Pictures": str(Path.home() / "Pictures"),
        "Videos": str(Path.home() / "Videos"),
        "Documents": str(Path.home() / "Documents"),
        "Archives": str(Path.home() / "Archives"),
        "Music": str(Path.home() / "Music"),
        "Code": str(Path.home() / "Code"),
        "Diffs": str(Path.home() / "Diffs"),
        "Bin": str(Path.home() / "Bin"),
        "Applications": str(Path.home() / "Applications"),
        "Img": str(Path.home() / "Img"),
        "Other": str(Path.home() / "Other"),
    },
    "extensions": {
        "Pictures": [
            "jpg",
            "jpeg",
            "png",
            "gif",
            "bmp",
            "webp",
            "svg",
            "ico",
            "tiff",
            "heic",
            "heif",
            "raw",
            "cr2",
            "nef",
            "orf",
            "arw",
            "dng",
        ],
        "Videos": [
            "mp4",
            "mkv",
            "mov",
            "avi",
            "webm",
            "flv",
            "m4v",
            "mpg",
            "mpeg",
            "wmv",
            "mts",
            "m2ts",
            "3gp",
            "3g2",
            "ogv",
            "dv",
            "ts",
        ],
        "Documents": [
            "pdf",
            "docx",
            "doc",
            "txt",
            "md",
            "odt",
            "rtf",
            "epub",
            "mobi",
            "tex",
            "xlsx",
            "xls",
            "csv",
            "pptx",
            "ppt",
            "ods",
            "odp",
            "json",
            "yaml",
            "yml",
        ],
        "Archives": [
            "zip",
            "tar",
            "gz",
            "7z",
            "rar",
            "xz",
            "bz2",
            "tgz",
            "tar.gz",
            "tar.xz",
            "tar.bz2",
            "cbz",
            "cbr",
            "lz",
            "lz4",
            "zst",
        ],
        "Music": [
            "mp3",
            "wav",
            "flac",
            "ogg",
            "m4a",
            "aac",
            "wma",
            "opus",
            "aiff",
            "mid",
            "midi",
            "alac",
        ],
        "Code": [
            "py",
            "cpp",
            "c",
            "js",
            "html",
            "css",
            "sh",
            "rb",
            "go",
            "rs",
            "java",
            "php",
            "ts",
            "tsx",
            "jsx",
            "toml",
            "ini",
            "lua",
            "kt",
            "dart",
            "scala",
        ],
        "Diffs": ["diff", "patch"],
        "Bin": ["bin", "o", "obj", "so", "a", "dylib", "dll", "pyd", "wasm"],
        "Applications": [
            "deb",
            "rpm",
            "exe",
            "msi",
            "pkg",
            "AppImage",
            "flatpak",
            "snap",
            "apk",
            "jar",
            "xpi",
            "whl",
            "nupkg",
            "rpmnew",
            "dmg",
        ],
        "Img": ["iso", "img", "vdi", "vmdk", "qcow2", "vhd", "qcow", "vhdx"],
    },
    "temp_extensions": [
        ".part",
        ".crdownload",
        ".tmp",
        ".download",
        ".!qB",
        ".aria2",
        ".qldKpe",
        ".opdownload",
    ],
    "log_file": str(
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "download_organizer.log"
    ),
    "cleanup_empty_dirs": True,
    "config_reload_interval": 30,  # seconds
}

CONFIG_PATH = Path.home() / ".download_organizer_config.json"

# === Logger setup ===
logger = logging.getLogger("DownloadOrganizer")
logger.setLevel(logging.INFO)

# Ensure log directory exists
Path(DEFAULT_CONFIG["log_file"]).parent.mkdir(parents=True, exist_ok=True)

fh = logging.FileHandler(DEFAULT_CONFIG["log_file"])
fh.setLevel(logging.INFO)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(fh)

sh = logging.StreamHandler()
sh.setLevel(logging.INFO)
sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(sh)


# === Configuration Class ===
class Settings:
    """Holds the runtime configuration to avoid global variable warnings."""

    def __init__(self):
        self.downloads: Path = Path(DEFAULT_CONFIG["downloads_dir"])
        self.dirs: Dict[str, Path] = {
            k: Path(v) for k, v in DEFAULT_CONFIG["dirs"].items()
        }
        self.extensions: Dict[str, Set[str]] = {
            k: set(v) for k, v in DEFAULT_CONFIG["extensions"].items()
        }
        self.temp_extensions: Set[str] = set(DEFAULT_CONFIG["temp_extensions"])
        self.cleanup: bool = DEFAULT_CONFIG["cleanup_empty_dirs"]
        self.reload_interval: int = DEFAULT_CONFIG["config_reload_interval"]

    def load_from_file(self) -> None:
        """Reload configuration from JSON file if it exists."""
        if not CONFIG_PATH.exists():
            return

        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)

            if "downloads_dir" in cfg:
                self.downloads = Path(cfg["downloads_dir"])
            if "dirs" in cfg:
                self.dirs = {k: Path(v) for k, v in cfg["dirs"].items()}
            if "extensions" in cfg:
                self.extensions = {k: set(v) for k, v in cfg["extensions"].items()}
            if "temp_extensions" in cfg:
                self.temp_extensions = set(cfg["temp_extensions"])

            self.cleanup = cfg.get("cleanup_empty_dirs", self.cleanup)
            self.reload_interval = cfg.get(
                "config_reload_interval", self.reload_interval
            )

            logger.debug("Configuration reloaded")
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to reload config: %s", e)


# Instantiate global settings
config = Settings()


# === Setup directories ===
def setup_dirs() -> None:
    """Ensure all target directories exist."""
    for d in config.dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    config.downloads.mkdir(parents=True, exist_ok=True)


# === File category & unique path ===
def get_category(file: Path) -> str:
    """Return category based on file extension."""
    ext = file.suffix.lower().lstrip(".")
    for category, exts in config.extensions.items():
        if ext in exts:
            return category
    return "Other"


def get_unique_path(target: Path) -> Path:
    """Return a unique path to avoid overwriting."""
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    counter = 1

    while True:
        new_name = f"{stem}_{counter}{suffix}"
        new_target = parent / new_name
        if not new_target.exists():
            return new_target
        counter += 1


# === File readiness ===
def is_file_ready(file: Path, retries: int = 5, delay: float = 1.0) -> bool:
    """
    Check if file is fully downloaded and ready to move.
    Checks for: Temp extensions, stable size, and file locking.
    """
    if not file.exists():
        return False

    if file.suffix.lower() in config.temp_extensions or file.name.startswith("."):
        return False

    prev_size = -1
    for _ in range(retries):
        try:
            # 1. Check size
            size = file.stat().st_size

            # 2. Check lock (Exclusive access check)
            # Using 'with' to ensure the file handle is closed immediately (Pylint R1732)
            with open(file, "ab"):
                pass

            if size > 0 and size == prev_size:
                return True

            prev_size = size
        except (OSError, PermissionError, FileNotFoundError):
            # File is locked, being written to, or vanished
            pass

        time.sleep(delay)

    return False


# === Move file safely ===
def move_file(file: Path) -> None:
    """Move a file to its categorized folder, skip temporary files with info log."""
    if not file.is_file():
        return

    # Quick check before expensive lock checks
    if file.suffix.lower() in config.temp_extensions or file.name.startswith("."):
        return

    if not is_file_ready(file):
        return

    category = get_category(file)
    target_dir = config.dirs.get(category, config.dirs["Other"])

    # Ensure target dir exists
    target_dir.mkdir(parents=True, exist_ok=True)

    target = get_unique_path(target_dir / file.name)

    attempts = 3
    for _ in range(attempts):
        try:
            shutil.move(str(file), str(target))
            logger.info("✓ Moved %s -> %s/%s", file.name, category, target.name)
            break
        except (OSError, shutil.Error) as e:
            logger.error("Failed to move %s: %s", file.name, e)
            time.sleep(1)

    if config.cleanup:
        cleanup_empty_dirs(file.parent)


# === Organize existing files ===
def organize_existing() -> None:
    """Organize all existing files in downloads directory."""
    logger.info("Organizing existing files in %s...", config.downloads)
    config.load_from_file()

    if not config.downloads.exists():
        logger.error("Downloads directory does not exist: %s", config.downloads)
        return

    for file in config.downloads.iterdir():
        move_file(file)
    logger.info("Organization of existing files complete.")


# === Cleanup empty dirs ===
def cleanup_empty_dirs(directory: Path) -> None:
    """Recursively remove empty directories."""
    try:
        # Don't delete the main downloads folder or dirs outside it
        if directory == config.downloads or not directory.exists():
            return

        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
            logger.info("Removed empty directory: %s", directory)
            cleanup_empty_dirs(directory.parent)
    except OSError:
        pass


# === Watchdog handler ===
class DownloadHandler(FileSystemEventHandler):
    """Watchdog event handler to move new files in Downloads directory."""

    def on_created(self, event):
        """Called when a new file is created."""
        if not event.is_directory:
            # Short sleep to let the OS register the file handle
            time.sleep(0.5)
            # os.fsdecode ensures we pass a string, satisfying Pyright (bytes | str issue)
            move_file(Path(os.fsdecode(event.src_path)))

    def on_modified(self, event):
        """Called when a file is modified."""
        if not event.is_directory:
            move_file(Path(os.fsdecode(event.src_path)))


# === Main ===
def main() -> None:
    """Entry point for the download organizer."""
    parser = argparse.ArgumentParser(
        description="Download Organizer: Auto-organize downloads by file type."
    )
    parser.add_argument(
        "-o", "--organize", action="store_true", help="Organize existing files and exit"
    )
    parser.add_argument(
        "-d",
        "--daemon",
        action="store_true",
        help="Run as daemon (watch for new files)",
    )
    args = parser.parse_args()

    setup_dirs()
    config.load_from_file()

    if args.organize:
        organize_existing()

    if args.daemon:
        logger.info("Starting daemon, watching: %s", config.downloads)
        observer = Observer()
        handler = DownloadHandler()
        observer.schedule(handler, str(config.downloads), recursive=False)
        observer.start()

        last_reload = time.time()
        try:
            while True:
                time.sleep(1)
                now = time.time()
                # Reload config periodically
                if now - last_reload >= config.reload_interval:
                    config.load_from_file()
                    last_reload = now
        except KeyboardInterrupt:
            logger.info("Stopping daemon...")
            observer.stop()
        observer.join()

    if not args.organize and not args.daemon:
        parser.print_help()


if __name__ == "__main__":
    main()
