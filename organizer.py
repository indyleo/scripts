#!/usr/bin/env python3
"""
Download Organizer: Auto-organize downloads by file type.
"""
import argparse
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Set

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

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
        "Pictures": ["jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "ico", "tiff"],
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
        ],
        "Archives": ["zip", "tar", "gz", "7z", "rar", "xz", "bz2", "tgz"],
        "Music": ["mp3", "wav", "flac", "ogg", "m4a", "aac", "wma", "opus"],
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
        ],
        "Diffs": ["diff", "patch"],
        "Bin": ["bin", "o", "obj", "so", "a", "dylib"],
        "Applications": [
            "deb",
            "rpm",
            "exe",
            "msi",
            "pkg",
            "AppImage",
            "flatpak",
            "snap",
        ],
        "Img": ["iso", "img", "vdi", "vmdk", "qcow2", "vhd"],
    },
    "temp_extensions": [
        ".part",
        ".crdownload",
        ".tmp",
        ".download",
        ".!qB",
        ".aria2",
        ".qldKpe",
    ],
    "log_file": str(
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "download_organizer.log"
    ),
    "cleanup_empty_dirs": True,
    "config_reload_interval": 30,  # seconds
}

CONFIG_PATH = Path.home() / ".download_organizer_config.json"

# === Globals ===
DOWNLOADS = Path(DEFAULT_CONFIG["downloads_dir"])
DIRS = {k: Path(v) for k, v in DEFAULT_CONFIG["dirs"].items()}
EXTENSIONS = {k: set(v) for k, v in DEFAULT_CONFIG["extensions"].items()}
TEMP_EXTENSIONS: Set[str] = set(DEFAULT_CONFIG["temp_extensions"])
CLEANUP_EMPTY_DIRS = DEFAULT_CONFIG["cleanup_empty_dirs"]
CONFIG_RELOAD_INTERVAL = DEFAULT_CONFIG["config_reload_interval"]

# === Logger setup ===
logger = logging.getLogger()
logger.setLevel(logging.INFO)

fh = logging.FileHandler(DEFAULT_CONFIG["log_file"])
fh.setLevel(logging.INFO)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(fh)

sh = logging.StreamHandler()
sh.setLevel(logging.INFO)
sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(sh)


# === Config reload ===
def reload_config() -> None:
    """Reload configuration from JSON file if it exists."""
    global DIRS, EXTENSIONS, TEMP_EXTENSIONS, CLEANUP_EMPTY_DIRS, DOWNLOADS, CONFIG_RELOAD_INTERVAL
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            DOWNLOADS = Path(cfg.get("downloads_dir", str(DOWNLOADS)))
            DIRS = {
                k: Path(v)
                for k, v in cfg.get(
                    "dirs", {k: str(v) for k, v in DIRS.items()}
                ).items()
            }
            EXTENSIONS = {
                k: set(v)
                for k, v in cfg.get(
                    "extensions", {k: list(v) for k, v in EXTENSIONS.items()}
                ).items()
            }
            TEMP_EXTENSIONS.update(cfg.get("temp_extensions", []))
            CLEANUP_EMPTY_DIRS = cfg.get("cleanup_empty_dirs", CLEANUP_EMPTY_DIRS)
            CONFIG_RELOAD_INTERVAL = cfg.get(
                "config_reload_interval", CONFIG_RELOAD_INTERVAL
            )
            logger.info("Configuration reloaded")
        except Exception as e:
            logger.error("Failed to reload config: %s", e)


# === Setup directories ===
def setup_dirs() -> None:
    """Ensure all target directories exist."""
    for d in DIRS.values():
        d.mkdir(parents=True, exist_ok=True)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)


# === File category & unique path ===
def get_category(file: Path) -> str:
    """Return category based on file extension."""
    ext = file.suffix.lower().lstrip(".")
    for category, exts in EXTENSIONS.items():
        if ext in exts:
            return category
    return "Other"


def get_unique_path(target: Path) -> Path:
    """Return a unique path to avoid overwriting."""
    counter = 1
    while target.exists():
        target = target.with_name(f"{target.stem}_{counter}{target.suffix}")
        counter += 1
    return target


# === File readiness ===
def is_file_ready(file: Path, retries: int = 10, delay: float = 1.0) -> bool:
    """Check if file is fully downloaded and ready to move."""
    if file.suffix.lower() in TEMP_EXTENSIONS or file.name.startswith("."):
        return False
    prev_size = -1
    for _ in range(retries):
        try:
            size = file.stat().st_size
            if size > 0 and size == prev_size:
                return True
            prev_size = size
        except FileNotFoundError:
            return False
        time.sleep(delay)
    return False


# === Move file safely ===
def move_file(file: Path) -> None:
    """Move a file to its categorized folder, skip temporary files with info log."""
    if not file.is_file():
        return

    if file.suffix.lower() in TEMP_EXTENSIONS or file.name.startswith("."):
        logger.info("Skipping in-progress or temporary file: %s", file.name)
        return

    if not is_file_ready(file):
        logger.info("File not ready yet: %s", file.name)
        return

    category = get_category(file)
    target_dir = DIRS.get(category, DIRS["Other"])
    target = get_unique_path(target_dir / file.name)

    attempts = 3
    for _ in range(attempts):
        try:
            shutil.move(str(file), str(target))
            logger.info("✓ Moved %s → %s", file.name, category)
            break
        except (OSError, shutil.Error) as e:
            logger.error("Failed to move %s: %s", file.name, e)
            time.sleep(0.5)

    if CLEANUP_EMPTY_DIRS:
        cleanup_empty_dirs(file.parent)


# === Organize existing files ===
def organize_existing() -> None:
    """Organize all existing files in downloads directory."""
    logger.info("Organizing existing files...")
    reload_config()
    for file in DOWNLOADS.iterdir():
        move_file(file)


# === Cleanup empty dirs ===
def cleanup_empty_dirs(directory: Path) -> None:
    """Recursively remove empty directories."""
    try:
        if (
            directory != DOWNLOADS
            and directory.exists()
            and directory.is_dir()
            and not any(directory.iterdir())
        ):
            directory.rmdir()
            logger.info("Removed empty directory: %s", directory)
            cleanup_empty_dirs(directory.parent)
    except OSError as e:
        logger.error("Failed to remove directory %s: %s", directory, e)


# === Watchdog handler ===
class DownloadHandler(FileSystemEventHandler):
    """Watchdog event handler to move new files in Downloads directory."""

    def on_created(self, event):
        """Called when a new file is created."""
        if not event.is_directory:
            time.sleep(0.2)
            move_file(Path(str(event.src_path)))


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

    if not args.organize and not args.daemon:
        parser.print_help()
        return

    setup_dirs()
    reload_config()

    if args.organize:
        organize_existing()

    if args.daemon:
        observer = Observer()
        observer.schedule(DownloadHandler(), str(DOWNLOADS), recursive=False)
        observer.start()
        logger.info("Watching %s", DOWNLOADS)
        last_reload = time.time()
        try:
            while True:
                now = time.time()
                if now - last_reload >= CONFIG_RELOAD_INTERVAL:
                    reload_config()
                    last_reload = now
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
