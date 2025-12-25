#!/usr/bin/env python3
"""Wayland Clipboard manager with grouped menus, text/image support, and persistence."""

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

# ------------------- Configurable limits -------------------
MAX_TEXT_HISTORY = 150
MAX_IMAGE_HISTORY = 10
MAX_PINNED_HISTORY = 50

POLL_INTERVAL = 0.5
PREVIEW_MAX = 60
ROFI_LINES = 20

HOME = Path.home()
CLIP_DIR = HOME.joinpath(".cache", "clipboard_wayland")
HISTORY_PATH = CLIP_DIR.joinpath("history.json")
PIN_PATH = CLIP_DIR.joinpath("pinned.json")
IMAGE_DIR = CLIP_DIR.joinpath("images")

CLIP_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


@dataclass
class Clip:
    type: str
    content: Optional[str] = None
    path: Optional[str] = None
    fmt: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Clip:
        return cls(
            type=data.get("type", "text"),
            content=data.get("content"),
            path=data.get("path"),
            fmt=data.get("fmt"),
            timestamp=data.get("timestamp") or datetime.now().isoformat(),
        )


# --- Helper Functions (Atomic Write, Load/Save remain similar) ---


def atomic_write_text(path: Path, data: str) -> None:
    temp = None
    try:
        dirpath = path.parent
        fd, temp = tempfile.mkstemp(dir=str(dirpath))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(temp, path)
    except OSError:
        pass
    finally:
        if temp and os.path.exists(temp):
            os.remove(temp)


def load_clips_from_file(path: Path) -> List[Clip]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [Clip.from_dict(item) for item in data]
    except:
        return []


def save_clips_to_file(path: Path, clips: List[Clip]) -> None:
    text = json.dumps([c.to_dict() for c in clips], ensure_ascii=False, indent=2)
    atomic_write_text(path, text)


def save_image_bytes(image_data: bytes) -> Optional[Path]:
    if not HAS_PIL:
        return None
    try:
        h = hashlib.sha256(image_data).hexdigest()[:16]
        filepath = IMAGE_DIR.joinpath(f"{h}.png")
        if filepath.exists():
            return filepath

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(image_data)
            tmp_path = tmp.name
        try:
            with Image.open(tmp_path) as img:
                img = (
                    img.convert("RGBA")
                    if "transparency" in img.info
                    else img.convert("RGB")
                )
                img.save(str(filepath), "PNG")
            return filepath
        finally:
            os.remove(tmp_path)
    except:
        return None


# -------------------- WAYLAND CLIPBOARD OPERATIONS --------------------


def copy_text_to_clipboard(text: str) -> None:
    """Wayland: Copy text using wl-copy."""
    try:
        subprocess.run(["wl-copy", text], check=True)
    except FileNotFoundError:
        print("Error: 'wl-clipboard' not found. Please install it.")


def copy_image_to_clipboard(path: str) -> None:
    """Wayland: Copy image using wl-copy."""
    try:
        subprocess.run(["wl-copy", "--type", "image/png", "--file", path], check=True)
    except Exception as e:
        print(f"Error copying image: {e}")


def get_clipboard_content() -> Optional[Clip]:
    """Wayland: Retrieve content using wl-paste."""
    try:
        # Check available types
        types_proc = subprocess.run(
            ["wl-paste", "--list-types"], capture_output=True, text=True, timeout=1
        )
        available = types_proc.stdout.splitlines()

        # Try images first
        image_types = ["image/png", "image/jpeg", "image/jpg"]
        for t in image_types:
            if t in available:
                img_data = subprocess.run(
                    ["wl-paste", "--type", t], capture_output=True, timeout=2
                ).stdout
                if img_data:
                    path = save_image_bytes(img_data)
                    if path:
                        return Clip(type="image", path=str(path), fmt=t)

        # Fallback to text
        text_proc = subprocess.run(
            ["wl-paste", "--type", "text/plain", "--no-newline"],
            capture_output=True,
            text=True,
            timeout=1,
        )
        text = text_proc.stdout.strip()
        if text:
            return Clip(type="text", content=text)

    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ):
        pass
    return None


# -------------------- REMAINING MANAGER LOGIC --------------------


class ClipboardManager:
    def __init__(self) -> None:
        self.history = load_clips_from_file(HISTORY_PATH)
        self.pinned = load_clips_from_file(PIN_PATH)

    def save(self):
        self.history = self._trim(self.history)
        self.pinned = self.pinned[:MAX_PINNED_HISTORY]
        save_clips_to_file(HISTORY_PATH, self.history)
        save_clips_to_file(PIN_PATH, self.pinned)

    def _trim(self, clips: List[Clip]) -> List[Clip]:
        t_count, i_count = 0, 0
        trimmed = []
        for c in clips:
            if c.type == "text" and t_count < MAX_TEXT_HISTORY:
                trimmed.append(c)
                t_count += 1
            elif c.type == "image" and i_count < MAX_IMAGE_HISTORY:
                trimmed.append(c)
                i_count += 1
        return trimmed

    def add_clip(self, clip: Clip):
        clip.timestamp = datetime.now().isoformat()
        if any(self._match(clip, p) for p in self.pinned):
            return
        self.history = [h for h in self.history if not self._match(clip, h)]
        self.history.insert(0, clip)

    def _match(self, a, b):
        if a.type != b.type:
            return False
        return a.content == b.content if a.type == "text" else a.path == b.path

    def select_clip(self):
        # UI Logic remains the same (Uses Rofi)
        self.history = load_clips_from_file(HISTORY_PATH)
        self.pinned = load_clips_from_file(PIN_PATH)

        lines, entries = [], []
        # (Shortened logic for brevity, same as original Rofi builder)
        for i, p in enumerate(self.pinned, 1):
            lbl = (
                f"[P{i}] {p.content[:30]}"
                if p.type == "text"
                else f"[P{i}] IMAGE: {p.path}"
            )
            lines.append(lbl)
            entries.append((lbl, p))
        lines.append("--- History ---")
        for i, h in enumerate(self.history, 1):
            lbl = f"[{h.type[0].upper()}{i}] {h.content[:30] if h.type == 'text' else h.path}"
            lines.append(lbl)
            entries.append((lbl, h))

        sel = run_rofi("\n".join(lines), "Wayland Clipboard:")
        for lbl, c in entries:
            if lbl == sel:
                if c.type == "text":
                    copy_text_to_clipboard(c.content)
                else:
                    copy_image_to_clipboard(c.path)


def run_rofi(menu_input: str, prompt: str = "") -> str:
    # Note: Rofi works on Wayland via 'rofi-wayland' or XWayland
    cmd = ["rofi", "-dmenu", "-i", "-p", prompt]
    try:
        proc = subprocess.run(cmd, input=menu_input.encode(), stdout=subprocess.PIPE)
        return proc.stdout.decode().strip()
    except:
        return ""


def daemon_loop(manager: ClipboardManager):
    last_hash = None
    while True:
        curr = get_clipboard_content()
        if curr:
            h = hashlib.sha256(
                curr.content.encode() if curr.type == "text" else curr.path.encode()
            ).hexdigest()
            if h != last_hash:
                with _lock:
                    manager.add_clip(curr)
                    save_clips_to_file(HISTORY_PATH, manager.history)
                last_hash = h
        time.sleep(POLL_INTERVAL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["daemon", "select", "clear"])
    args = parser.parse_args()
    mgr = ClipboardManager()
    if args.command == "daemon":
        daemon_loop(mgr)
    elif args.command == "select":
        mgr.select_clip()
    elif args.command == "clear":
        save_clips_to_file(HISTORY_PATH, [])
        print("Cleared.")


if __name__ == "__main__":
    main()
