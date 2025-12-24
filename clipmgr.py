#!/usr/bin/env python3
"""Clipboard manager with grouped menus, text and image support, and persistent history."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    Image = None
    HAS_PIL = False

# ------------------- Configurable history limits -------------------
MAX_TEXT_HISTORY = 150
MAX_IMAGE_HISTORY = 10
MAX_PINNED_HISTORY = 50

POLL_INTERVAL = 0.5
PREVIEW_MAX = 60
ROFI_LINES = 20

HOME = Path.home()
CLIP_DIR = HOME.joinpath(".cache", "clipboard")
HISTORY_PATH = CLIP_DIR.joinpath("history.json")
PIN_PATH = CLIP_DIR.joinpath("pinned.json")
IMAGE_DIR = CLIP_DIR.joinpath("images")

CLIP_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


@dataclass
class Clip:
    """Represents a single clipboard item, either text or image."""

    type: str
    content: Optional[str] = None
    path: Optional[str] = None
    fmt: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        """Return dictionary representation of the Clip."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Clip:
        """Construct a Clip from a dictionary."""
        if not isinstance(data, dict):
            return cls(type="text", content=str(data))
        return cls(
            type=data.get("type", "text"),
            content=data.get("content"),
            path=data.get("path"),
            fmt=data.get("format") or data.get("fmt"),
            timestamp=data.get("timestamp") or datetime.now().isoformat(),
        )


def atomic_write_text(path: Path, data: str) -> None:
    """Write data to file atomically using a temporary file."""
    temp = None
    try:
        dirpath = path.parent
        fd, temp = tempfile.mkstemp(dir=str(dirpath))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(temp, path)
    except OSError:
        try:
            with path.open("w", encoding="utf-8") as fh:
                fh.write(data)
        except OSError:
            pass
    finally:
        if temp and os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass


def load_clips_from_file(path: Path) -> List[Clip]:
    """Load clips from a JSON file."""
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [Clip.from_dict(item) for item in data if isinstance(item, dict)]


def save_clips_to_file(path: Path, clips: List[Clip]) -> None:
    """Save clips to a JSON file."""
    text = json.dumps([c.to_dict() for c in clips], ensure_ascii=False, indent=2)
    atomic_write_text(path, text)


def get_content_hash(content: bytes) -> str:
    """Return a 16-character hash of the content."""
    return hashlib.sha256(content).hexdigest()[:16]


def save_image_bytes(image_data: bytes) -> Optional[Path]:
    """Save image bytes to disk as PNG and return the path."""
    if not HAS_PIL or Image is None:
        return None
    try:
        h = get_content_hash(image_data)
        filepath = IMAGE_DIR.joinpath(f"{h}.png")
        if filepath.exists():
            return filepath

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(image_data)
            tmp_path = tmp.name

        try:
            with Image.open(tmp_path) as img:
                if img.mode not in ("RGB", "RGBA"):
                    if img.mode == "P" and "transparency" in img.info:
                        img = img.convert("RGBA")
                    else:
                        img = img.convert("RGB")
                img.save(str(filepath), "PNG")
            return filepath
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    except (OSError, IOError):
        return None


def cleanup_old_images(keep_paths: List[str]) -> None:
    """Remove images in IMAGE_DIR that are not in keep_paths."""
    keep = {Path(p).name for p in keep_paths if p}
    try:
        for f in IMAGE_DIR.iterdir():
            if f.suffix.lower() == ".png" and f.name not in keep:
                try:
                    f.unlink()
                except OSError:
                    pass
    except OSError:
        pass


class ClipboardManager:
    """Manages clipboard history and pinned items."""

    def __init__(self) -> None:
        self.history: List[Clip] = load_clips_from_file(HISTORY_PATH)
        self.pinned: List[Clip] = load_clips_from_file(PIN_PATH)

    def save(self) -> None:
        """Save history and pinned items, trimming excess clips and cleaning images."""
        text_count = 0
        image_count = 0
        trimmed = []
        for c in self.history:
            if c.type == "text" and text_count < MAX_TEXT_HISTORY:
                trimmed.append(c)
                text_count += 1
            elif c.type == "image" and image_count < MAX_IMAGE_HISTORY:
                trimmed.append(c)
                image_count += 1
        self.history = trimmed

        # Trim pinned if needed
        self.pinned = self.pinned[:MAX_PINNED_HISTORY]

        save_clips_to_file(HISTORY_PATH, self.history)
        save_clips_to_file(PIN_PATH, self.pinned)

        keep = [
            c.path for c in self.history + self.pinned if c.type == "image" and c.path
        ]
        cleanup_old_images(keep)

    def save_history_only(self) -> None:
        """Save only the history without touching pinned clips."""
        text_count = 0
        image_count = 0
        trimmed = []
        for c in self.history:
            if c.type == "text" and text_count < MAX_TEXT_HISTORY:
                trimmed.append(c)
                text_count += 1
            elif c.type == "image" and image_count < MAX_IMAGE_HISTORY:
                trimmed.append(c)
                image_count += 1
        self.history = trimmed
        save_clips_to_file(HISTORY_PATH, self.history)

    def reload(self) -> None:
        """Reload history and pinned clips from disk."""
        self.history = load_clips_from_file(HISTORY_PATH)
        self.pinned = load_clips_from_file(PIN_PATH)

    def _is_duplicate_in_list(self, clip: Clip, clip_list: List[Clip]) -> bool:
        """Check if a clip already exists in the given list."""
        for other in clip_list:
            if clip.type != other.type:
                continue
            if clip.type == "text" and clip.content == other.content:
                return True
            if (
                clip.type == "image"
                and clip.path
                and other.path
                and clip.path == other.path
            ):
                return True
        return False

    def add_clip(self, clip: Clip) -> None:
        """Add a new clip to history, avoiding duplicates with pinned clips."""
        if not clip:
            return
        clip.timestamp = datetime.now().isoformat()
        if self._is_duplicate_in_list(clip, self.pinned):
            return
        self.history = [
            c for c in self.history if not self._is_duplicate_in_list(clip, [c])
        ]
        self.history.insert(0, clip)

    def get_preview(self, clip: Clip, max_len: int = PREVIEW_MAX) -> str:
        """Return a short string preview of the clip."""
        if clip.type == "text":
            t = (clip.content or "").replace("\n", " ")
            return t[:max_len] + ("…" if len(t) > max_len else "")
        if clip.type == "image":
            filename = Path(clip.path or "").name
            try:
                dt = datetime.fromisoformat(clip.timestamp)
                return f"{filename} ({dt.strftime('%m/%d %H:%M')})"
            except Exception:
                return filename
        return ""

    def get_image_type_str(self, path: Optional[str]) -> str:
        """Return the image type string for display."""
        if not path:
            return "IMAGE"
        ext = Path(path).suffix.lower().lstrip(".")
        return ext.upper() if ext else "IMAGE"

    # -------------------- GROUPED MENU --------------------

    def build_grouped_menu_entries(self):
        """Return grouped lists of pinned, image, and text clips for rofi with separate numbering."""
        pinned = []
        images = []
        texts = []

        # Pinned items
        for i, p in enumerate(self.pinned, 1):
            label = (
                f"[P{i}] {self.get_image_type_str(p.path)}: {p.path}"
                if p.type == "image"
                else f"[P{i}] {self.get_preview(p)}"
            )
            pinned.append((label, p))

        # Image history with separate numbering
        for i, h in enumerate([c for c in self.history if c.type == "image"], 1):
            label = f"[I{i}] {self.get_image_type_str(h.path)}: {h.path}"
            images.append((label, h))

        # Text history with separate numbering
        for i, h in enumerate([c for c in self.history if c.type == "text"], 1):
            label = f"[T{i}] {self.get_preview(h)}"
            texts.append((label, h))

        return pinned, images, texts

    # -------------------- MENU SELECTION --------------------
    def select_clip(self) -> None:
        """Display a menu to select a clip to restore to clipboard."""
        self.reload()
        pinned, images, texts = self.build_grouped_menu_entries()

        lines = []
        entries = []

        if pinned:
            lines.append("=== PINNED ===")
            for label, c in pinned:
                lines.append(label)
                entries.append((label, c))
            lines.append("")

        if images:
            lines.append("=== IMAGES ===")
            for label, c in images:
                lines.append(label)
                entries.append((label, c))
            lines.append("")

        if texts:
            lines.append("=== TEXT ===")
            for label, c in texts:
                lines.append(label)
                entries.append((label, c))

        if not entries:
            return

        menu = "\n".join(lines)
        sel = run_rofi(menu, prompt="Clipboard:")
        if not sel or sel.startswith("==="):
            return

        for label, clip in entries:
            if label == sel:
                self._restore_clipboard(clip)
                return

    # -------------------- PIN MENU --------------------
    def pin_menu(self) -> None:
        """Display menu to pin/unpin clips."""
        self.reload()
        pinned, images, texts = self.build_grouped_menu_entries()

        lines = []
        entries = []

        if pinned:
            lines.append("=== PINNED (select to unpin) ===")
            for label, c in pinned:
                lines.append(label)
                entries.append((label, c, "pinned"))
            lines.append("")

        if images or texts:
            lines.append("=== HISTORY (select to pin) ===")
            for label, c in images + texts:
                lines.append(label)
                entries.append((label, c, "history"))

        if not entries:
            return

        menu = "\n".join(lines)
        sel = run_rofi(menu, prompt="Pin:")
        if not sel or sel.startswith("==="):
            return

        for label, clip, group in entries:
            if label == sel:
                if group == "pinned":
                    self.pinned = [
                        p
                        for p in self.pinned
                        if not self._is_duplicate_in_list(clip, [p])
                    ]
                    if not self._is_duplicate_in_list(clip, self.history):
                        self.history.insert(0, clip)
                else:
                    self.history = [
                        h
                        for h in self.history
                        if not self._is_duplicate_in_list(clip, [h])
                    ]
                    if not self._is_duplicate_in_list(clip, self.pinned):
                        self.pinned.append(clip)
                self.save()
                return

    def clear_history(self) -> None:
        """Clear clipboard history but keep pinned clips."""
        self.reload()
        self.history = []
        self.save()

    def _restore_clipboard(self, clip: Clip) -> None:
        """Restore clip content to system clipboard."""
        if clip.type == "text" and clip.content:
            copy_text_to_clipboard(clip.content)
        elif clip.type == "image" and clip.path:
            copy_image_to_clipboard(clip.path)


def run_rofi(menu_input: str, prompt: str = "") -> str:
    cmd = ["rofi", "-dmenu", "-i", "-l", str(ROFI_LINES), "-p", prompt]
    try:
        proc = subprocess.run(
            cmd,
            input=menu_input.encode("utf-8"),
            stdout=subprocess.PIPE,
            check=True,
        )
        return proc.stdout.decode("utf-8").rstrip("\n")
    except subprocess.CalledProcessError:
        return ""


def copy_text_to_clipboard(text: str) -> None:
    """Copy text to system clipboard using xclip."""
    try:
        with subprocess.Popen(
            ["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE
        ) as p:
            p.communicate(text.encode("utf-8"))
    except OSError:
        pass


def copy_image_to_clipboard(path: str) -> None:
    """Copy image file to system clipboard using xclip."""
    fp = Path(path)
    if not fp.exists():
        return
    try:
        with fp.open("rb") as f, subprocess.Popen(
            ["xclip", "-selection", "clipboard", "-t", "image/png"],
            stdin=subprocess.PIPE,
        ) as p:
            p.communicate(f.read())
    except OSError:
        pass


def get_clipboard_content() -> Optional[Clip]:
    """Retrieve current clipboard content as a Clip object."""
    if HAS_PIL:
        try:
            targets = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=1,
                check=True,
            )
            available = targets.stdout.decode("utf-8").splitlines()
            for t in ["image/png", "image/jpeg", "image/jpg", "image/gif"]:
                if t not in available:
                    continue
                r = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-t", t, "-o"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    check=True,
                )
                if r.stdout:
                    saved = save_image_bytes(r.stdout)
                    if saved:
                        return Clip(type="image", path=str(saved), fmt=t)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass
    try:
        out = subprocess.run(
            ["xclip", "-o", "-selection", "clipboard"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=True,
        )
        text = out.stdout.decode("utf-8").rstrip("\n")
        if text.strip():
            return Clip(type="text", content=text)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def daemon_loop(manager: ClipboardManager, interval: float = POLL_INTERVAL) -> None:
    """Run clipboard daemon loop, polling clipboard and saving new clips."""
    last_hash: Optional[str] = None
    dirty = False
    try:
        while True:
            current = get_clipboard_content()
            if current:
                h = (
                    hashlib.sha256((current.content or "").encode("utf-8")).hexdigest()
                    if current.type == "text"
                    else current.path or ""
                )
                if h != last_hash:
                    with _lock:
                        manager.pinned = load_clips_from_file(PIN_PATH)
                        manager.add_clip(current)
                        dirty = True
                    last_hash = h
            if dirty:
                with _lock:
                    manager.save_history_only()
                    dirty = False
            time.sleep(interval)
    except KeyboardInterrupt:
        with _lock:
            manager.save_history_only()


def main():
    """Main entry point for CLI interface."""
    parser = argparse.ArgumentParser(description="Clipboard manager with grouped menus")
    parser.add_argument(
        "command", nargs="?", choices=["daemon", "select", "pin", "clear"], default=None
    )
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    manager = ClipboardManager()

    if args.command == "daemon":
        print("Starting clipboard daemon…")
        print("Image support:", "ENABLED" if HAS_PIL else "DISABLED (install Pillow)")
        daemon_loop(manager)
    elif args.command == "select":
        manager.select_clip()
    elif args.command == "pin":
        manager.pin_menu()
    elif args.command == "clear":
        manager.clear_history()
        print("History cleared (pinned preserved)")


if __name__ == "__main__":
    main()
