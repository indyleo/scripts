#!/usr/bin/env python3
"""
Clipboard Manager for dwm — Python with Image Support

Features:
- Keeps history in memory for fast operations with text and images
- Persists history and pinned items to JSON files (atomic writes)
- Saves images as PNG files and displays previews in dmenu (requires patched dmenu)
- Integrates with xclip and dmenu
- Commands: daemon | select | pin | clear

Dependencies: python3 (3.8+), xclip, dmenu (with image preview patch optionally), Pillow (optional)
Usage examples:
  python3 clipmgr.py daemon
  python3 clipmgr.py select
  python3 clipmgr.py pin
  python3 clipmgr.py clear
"""
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
from typing import Dict, List, Optional

# Attempt to import Pillow for image handling
try:
    from PIL import Image  # type: ignore

    HAS_PIL = True
except ImportError:
    Image = None  # type: ignore
    HAS_PIL = False

# Configuration
HOME = Path.home()
CLIP_DIR = HOME.joinpath(".cache", "clipboard")
HISTORY_PATH = CLIP_DIR.joinpath("history.json")
PIN_PATH = CLIP_DIR.joinpath("pinned.json")
IMAGE_DIR = CLIP_DIR.joinpath("images")
MAX_CLIPS = 50
MAX_IMAGE_CLIPS = 20
POLL_INTERVAL = 0.5
PREVIEW_MAX = 60
DMENU_LINES = 20

# Ensure directories exist
CLIP_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


@dataclass
class Clip:
    """
    Represents a clipboard clip (text or image).
    Fields not set are saved as None in JSON.
    """

    type: str  # "text" or "image"
    content: Optional[str] = None  # used for text
    path: Optional[str] = None  # used for image file path
    fmt: Optional[str] = None  # MIME or extension-like string for images
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict:
        """Return JSON-serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "Clip":
        """Create Clip from dict, tolerant of older formats."""
        if not isinstance(data, dict):
            # Older format: plain string -> text clip
            return cls(type="text", content=str(data))
        return cls(
            type=data.get("type", "text"),
            content=data.get("content"),
            path=data.get("path"),
            fmt=data.get("format") or data.get("fmt"),
            timestamp=data.get("timestamp") or datetime.now().isoformat(),
        )


def atomic_write_text(path: Path, data: str) -> None:
    """
    Atomically write text to a file using UTF-8 encoding.
    Uses os.replace to guarantee atomic replace on same filesystem.
    """
    temp = None
    try:
        dirpath = path.parent
        fd, temp = tempfile.mkstemp(dir=str(dirpath))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(temp, path)
    except OSError:
        # If atomic replace fails, try best-effort fallback
        try:
            with path.open("w", encoding="utf-8") as fh:
                fh.write(data)
        except OSError:
            # Give up silently; caller may log if desired
            pass
    finally:
        if temp and os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass


def load_clips_from_file(path: Path) -> List[Clip]:
    """Load a list of Clip objects from JSON file. Returns empty list on error."""
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    clips: List[Clip] = []
    for item in data:
        try:
            clips.append(Clip.from_dict(item))
        except Exception:
            # Skip malformed entries
            continue
    return clips


def save_clips_to_file(path: Path, clips: List[Clip]) -> None:
    """Serialize clips to JSON file atomically."""
    data = [c.to_dict() for c in clips]
    text = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write_text(path, text)


def get_content_hash(content: bytes) -> str:
    """Return short SHA-256 hex digest for given content bytes."""
    h = hashlib.sha256(content).hexdigest()
    return h[:16]


def save_image_bytes(image_data: bytes) -> Optional[Path]:
    """
    Save image bytes to PNG file (converting via Pillow if available).
    Returns Path on success, None on failure or if Pillow is not available.
    """
    if not HAS_PIL or Image is None:
        return None

    try:
        content_hash = get_content_hash(image_data)
        filename = f"{content_hash}.png"
        filepath = IMAGE_DIR.joinpath(filename)
        if filepath.exists():
            return filepath

        # Write raw bytes to temp then open with Pillow to normalize and save PNG
        with tempfile.NamedTemporaryFile(delete=False, suffix=".img") as tmp:
            tmp.write(image_data)
            tmp_path = tmp.name
        try:
            with Image.open(tmp_path) as img:
                # Normalize mode
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
    except (OSError, ValueError):
        return None


def cleanup_old_images(keep_paths: List[str]) -> None:
    """
    Remove image files from IMAGE_DIR that are not referenced in keep_paths.
    keep_paths are full paths or filenames.
    """
    keep_filenames = {Path(p).name for p in keep_paths if p}
    try:
        for file in IMAGE_DIR.iterdir():
            if file.suffix.lower() == ".png" and file.name not in keep_filenames:
                try:
                    file.unlink()
                except OSError:
                    pass
    except OSError:
        pass


class ClipboardManager:
    """Manages history and pinned clips with persistence."""

    def __init__(self) -> None:
        self.history: List[Clip] = load_clips_from_file(HISTORY_PATH)
        self.pinned: List[Clip] = load_clips_from_file(PIN_PATH)

    def save(self) -> None:
        """Save trimmed history and pinned items, cleanup images."""
        # Trim history counts
        text_count = 0
        image_count = 0
        trimmed: List[Clip] = []
        for c in self.history:
            if c.type == "text" and text_count < MAX_CLIPS:
                trimmed.append(c)
                text_count += 1
            elif c.type == "image" and image_count < MAX_IMAGE_CLIPS:
                trimmed.append(c)
                image_count += 1
        self.history = trimmed

        save_clips_to_file(HISTORY_PATH, self.history)
        save_clips_to_file(PIN_PATH, self.pinned)

        # Cleanup images not referenced
        keep: List[str] = []
        for c in self.history + self.pinned:
            if c.type == "image" and c.path:
                keep.append(c.path)
        cleanup_old_images(keep)

    def save_history_only(self) -> None:
        """Save only the history list (daemon optimization)."""
        text_count = 0
        image_count = 0
        trimmed: List[Clip] = []
        for c in self.history:
            if c.type == "text" and text_count < MAX_CLIPS:
                trimmed.append(c)
                text_count += 1
            elif c.type == "image" and image_count < MAX_IMAGE_CLIPS:
                trimmed.append(c)
                image_count += 1
        self.history = trimmed
        save_clips_to_file(HISTORY_PATH, self.history)

    def reload(self) -> None:
        """Reload pinned/history from disk to pick up external changes."""
        self.history = load_clips_from_file(HISTORY_PATH)
        self.pinned = load_clips_from_file(PIN_PATH)

    def _is_duplicate_in_list(self, clip: Clip, clip_list: List[Clip]) -> bool:
        """Return True if clip is duplicate in clip_list."""
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
        """Add a clip to history (skip if duplicate of pinned or existing)."""
        if not clip or not clip.type:
            return
        # Update timestamp
        clip.timestamp = datetime.now().isoformat()

        if self._is_duplicate_in_list(clip, self.pinned):
            return

        # Remove duplicates from history
        self.history = [
            c for c in self.history if not self._is_duplicate_in_list(clip, [c])
        ]

        # Prepend
        self.history.insert(0, clip)

    def get_preview(self, clip: Clip, max_len: int = PREVIEW_MAX) -> str:
        """Return a one-line preview string for dmenu display."""
        if clip.type == "text":
            text = (clip.content or "").replace("\n", " ")
            if len(text) > max_len:
                return text[:max_len] + "…"
            return text
        if clip.type == "image":
            filename = Path(clip.path or "").name if clip.path else "image"
            try:
                dt = datetime.fromisoformat(clip.timestamp)
                timestr = dt.strftime("%m/%d %H:%M")
                return f"{filename} ({timestr})"
            except ValueError:
                return filename
        return ""

    def get_image_type_str(self, path: str) -> str:
        """Return image type string from extension (e.g., PNG, JPG)."""
        ext = Path(path).suffix.lower().lstrip(".")
        return ext.upper() if ext else "IMAGE"

    def build_menu_entries(self) -> List[tuple]:
        """
        Build list of (label, Clip) tuples for dmenu.
        Pinned entries come first, then history.
        Labels are like: "[P1] PNG:/path" or "[H3] Preview..."
        """
        entries: List[tuple] = []
        for i, p in enumerate(self.pinned, start=1):
            if p.type == "image" and p.path:
                label = f"[P{i}] {self.get_image_type_str(p.path)}:{p.path}"
            else:
                label = f"[P{i}] {self.get_preview(p)}"
            entries.append((label, p))
        for i, h in enumerate(self.history, start=1):
            if h.type == "image" and h.path:
                label = f"[H{i}] {self.get_image_type_str(h.path)}:{h.path}"
            else:
                label = f"[H{i}] {self.get_preview(h)}"
            entries.append((label, h))
        return entries

    def select_clip(self) -> None:
        """Show dmenu to select and restore a clip to clipboard."""
        self.reload()
        entries = self.build_menu_entries()
        if not entries:
            return
        menu_text = "\n".join(label for label, _ in entries)
        sel = run_dmenu(menu_text, prompt="Clipboard History:")
        if not sel:
            return
        for label, clip in entries:
            if label == sel:
                self._restore_clipboard(clip)
                return

    def pin_menu(self) -> None:
        """Show dmenu to pin/unpin items."""
        self.reload()
        lines: List[str] = []
        entries: List[tuple] = []

        if self.pinned:
            lines.append("=== PINNED (select to unpin) ===")
            for i, p in enumerate(self.pinned, start=1):
                if p.type == "image" and p.path:
                    label = f"[P{i}] {self.get_image_type_str(p.path)}:{p.path}"
                else:
                    label = f"[P{i}] {self.get_preview(p)}"
                lines.append(label)
                entries.append((label, p))
            lines.append("")

        if self.history:
            lines.append("=== HISTORY (select to pin) ===")
            for i, h in enumerate(self.history, start=1):
                if h.type == "image" and h.path:
                    label = f"[H{i}] {self.get_image_type_str(h.path)}:{h.path}"
                else:
                    label = f"[H{i}] {self.get_preview(h)}"
                lines.append(label)
                entries.append((label, h))

        if not lines:
            return

        menu_text = "\n".join(lines)
        sel = run_dmenu(menu_text, prompt="Pin/Unpin:")
        if not sel or sel.startswith("==="):
            return

        for label, clip in entries:
            if label == sel:
                if label.startswith("[P"):
                    # Unpin: remove from pinned and add back to front of history
                    self.pinned = [
                        p
                        for p in self.pinned
                        if not self._is_duplicate_in_list(clip, [p])
                    ]
                    if not self._is_duplicate_in_list(clip, self.history):
                        self.history.insert(0, clip)
                else:
                    # Pin: remove from history and append to pinned
                    self.history = [
                        h
                        for h in self.history
                        if not self._is_duplicate_in_list(clip, [h])
                    ]
                    if not self._is_duplicate_in_list(clip, self.pinned):
                        self.pinned.append(clip)
                self.save()
                break

    def clear_history(self) -> None:
        """Clear in-memory history (preserve pinned) and persist."""
        self.reload()
        self.history = []
        self.save()

    def _restore_clipboard(self, clip: Clip) -> None:
        """Restore clip to the system clipboard (text or image)."""
        if clip.type == "text" and clip.content:
            copy_text_to_clipboard(clip.content)
        elif clip.type == "image" and clip.path:
            copy_image_to_clipboard(clip.path)


def run_dmenu(menu_input: str, prompt: str = "") -> str:
    """
    Invoke dmenu with the provided input and return the selected line (no newline).
    Falls back to returning empty string on failure.
    """
    cmd = ["dmenu", "-l", str(DMENU_LINES), "-p", prompt]
    try:
        proc = subprocess.run(
            cmd,
            input=menu_input.encode("utf-8"),
            stdout=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        return proc.stdout.decode("utf-8").rstrip("\n")
    except (OSError, subprocess.SubprocessError):
        return ""


def copy_text_to_clipboard(text: str) -> None:
    """Copy a UTF-8 text string to the clipboard using xclip."""
    try:
        proc = subprocess.Popen(
            ["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE
        )
        proc.communicate(text.encode("utf-8"))
    except (OSError, subprocess.SubprocessError):
        pass


def copy_image_to_clipboard(image_path: str) -> None:
    """Copy local image file to the clipboard as image/png using xclip."""
    path = Path(image_path)
    if not path.exists():
        return
    try:
        with path.open("rb") as fh:
            data = fh.read()
        proc = subprocess.Popen(
            ["xclip", "-selection", "clipboard", "-t", "image/png"],
            stdin=subprocess.PIPE,
        )
        proc.communicate(data)
    except (OSError, subprocess.SubprocessError):
        pass


def get_clipboard_content() -> Optional[Clip]:
    """
    Read the current clipboard content and return a Clip if available.
    Tries image targets first (if Pillow is available), then text.
    """
    # Image targets
    if HAS_PIL:
        try:
            targets_proc = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=1,
                check=False,
            )
            available = targets_proc.stdout.decode("utf-8").splitlines()
            image_targets = ["image/png", "image/jpeg", "image/jpg", "image/gif"]
            for target in image_targets:
                if target not in available:
                    continue
                result = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-t", target, "-o"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    check=False,
                )
                if result.returncode == 0 and result.stdout:
                    saved = save_image_bytes(result.stdout)
                    if saved:
                        return Clip(type="image", path=str(saved), fmt=target)
        except (OSError, subprocess.SubprocessError):
            # If reading image targets fails, fall through to text
            pass

    # Text fallback
    try:
        out = subprocess.run(
            ["xclip", "-o", "-selection", "clipboard"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
        text = out.stdout.decode("utf-8").rstrip("\n")
        if text and text.strip():
            return Clip(type="text", content=text)
    except (OSError, subprocess.SubprocessError):
        pass

    return None


def daemon_loop(manager: ClipboardManager, interval: float = POLL_INTERVAL) -> None:
    """
    Loop to poll clipboard and persist new clips.
    Stops on KeyboardInterrupt (Ctrl-C).
    """
    last_hash: Optional[str] = None
    dirty = False
    try:
        while True:
            current = get_clipboard_content()
            if current:
                if current.type == "text":
                    current_hash = hashlib.sha256(
                        (current.content or "").encode("utf-8")
                    ).hexdigest()
                else:
                    # For images use path (content hash used as filename)
                    current_hash = current.path or ""
                if current_hash != last_hash:
                    with _lock:
                        # Reload pinned to avoid overwriting user changes
                        manager.pinned = load_clips_from_file(PIN_PATH)
                        manager.add_clip(current)
                        dirty = True
                    last_hash = current_hash
            if dirty:
                with _lock:
                    manager.save_history_only()
                    dirty = False
            time.sleep(interval)
    except KeyboardInterrupt:
        with _lock:
            manager.save_history_only()


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description="Clipboard manager with image support")
    parser.add_argument(
        "command",
        nargs="?",
        default=None,
        choices=["daemon", "select", "pin", "clear"],
        help="command to run",
    )
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    manager = ClipboardManager()

    if args.command == "daemon":
        print("Starting clipboard daemon (press Ctrl-C to stop)")
        if HAS_PIL:
            print("Image support: ENABLED")
        else:
            print("Image support: DISABLED (install Pillow to enable)")
        daemon_loop(manager)
    elif args.command == "select":
        manager.select_clip()
    elif args.command == "pin":
        manager.pin_menu()
    elif args.command == "clear":
        manager.clear_history()
        print("Clipboard history cleared (pinned items preserved)")


if __name__ == "__main__":
    main()
