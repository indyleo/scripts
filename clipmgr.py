#!/usr/bin/env python3
"""
Wayland Clipboard manager with grouped menus, text/image support, and persistence.
Handles history, pinning, and image caching via wl-clipboard and rofi.
Optimized for older versions of wl-clipboard using foreground backgrounding.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# --- Config ---
MAX_TEXT_HISTORY = 150
MAX_IMAGE_HISTORY = 10
MAX_PINNED_HISTORY = 50
POLL_INTERVAL = 0.5
PREVIEW_MAX = 80

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
    """Represents a single clipboard entry (text or image)."""

    type: str
    content: Optional[str] = None
    path: Optional[str] = None
    fmt: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        """Converts the Clip instance to a dictionary."""
        return asdict(self)


# --- Clipboard Core ---


def copy_to_clipboard(clip: Clip) -> None:
    """
    Uses wl-copy with --foreground.
    Kills old wl-copy processes first to prevent process accumulation.
    """
    env = os.environ.copy()

    try:
        subprocess.run(["pkill", "wl-copy"], check=False)
    except OSError:
        pass

    try:
        if clip.type == "text" and clip.content:
            subprocess.Popen(
                ["wl-copy", "--foreground", clip.content],
                env=env,
                start_new_session=True,
            )
        elif clip.type == "image" and clip.path:
            subprocess.Popen(
                ["wl-copy", "--foreground", "--type", "image/png", "--file", clip.path],
                env=env,
                start_new_session=True,
            )
    except OSError as e:
        print(f"Copy failed: {e}")


def get_clipboard_content() -> Optional[Clip]:
    """Retrieves current clipboard content, handling text and images."""
    try:
        t_proc = subprocess.run(
            ["wl-paste", "--list-types"],
            capture_output=True,
            text=True,
            timeout=1,
            check=True,
        )
        available = t_proc.stdout.splitlines()

        if "image/png" in available or "image/jpeg" in available:
            img_res = subprocess.run(
                ["wl-paste"], capture_output=True, timeout=2, check=True
            )
            img_data = img_res.stdout
            if img_data:
                h = hashlib.sha256(img_data).hexdigest()[:16]
                p = IMAGE_DIR.joinpath(f"{h}.png")
                if not p.exists() and HAS_PIL:
                    with open(p, "wb") as f:
                        f.write(img_data)
                return Clip(type="image", path=str(p))

        text_proc = subprocess.run(
            ["wl-paste", "-n"], capture_output=True, text=True, timeout=1, check=True
        )
        text = text_proc.stdout.strip()
        if text:
            return Clip(type="text", content=text)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        pass
    return None


# --- Manager Logic ---


class ClipboardManager:
    """Manages loading, saving, and selecting clipboard history items."""

    def __init__(self) -> None:
        self.history: list[Clip] = []
        self.pinned: list[Clip] = []
        self.load()

    def load(self) -> None:
        """Loads history and pinned items from JSON files."""

        def _load_file(path: Path) -> list[Clip]:
            if not path.exists():
                return []
            try:
                with path.open("r", encoding="utf-8") as f:
                    return [Clip(**c) for c in json.load(f)]
            except (json.JSONDecodeError, KeyError, TypeError, OSError):
                return []

        self.history = _load_file(HISTORY_PATH)
        self.pinned = _load_file(PIN_PATH)

    def save(self) -> None:
        """Trims and saves history and pinned items to disk."""
        with _lock:

            def _dedup(clips: list[Clip], limit: int) -> list[Clip]:
                seen = set()
                unique = []
                for c in clips:
                    val = c.content if c.type == "text" else c.path
                    if val and val not in seen:
                        unique.append(c)
                        seen.add(val)
                return unique[:limit]

            self.history = _dedup(self.history, MAX_TEXT_HISTORY)
            self.pinned = _dedup(self.pinned, MAX_PINNED_HISTORY)

            try:
                with HISTORY_PATH.open("w", encoding="utf-8") as f:
                    json.dump([asdict(c) for c in self.history], f, indent=2)
                with PIN_PATH.open("w", encoding="utf-8") as f:
                    json.dump([asdict(c) for c in self.pinned], f, indent=2)
            except OSError as e:
                print(f"Failed to save history: {e}")

    def add_clip(self, clip: Clip) -> None:
        """Adds a new clip to history, moving it to the top if it exists."""
        val = clip.content if clip.type == "text" else clip.path
        if not val:
            return

        if any((p.content if p.type == "text" else p.path) == val for p in self.pinned):
            return

        self.history = [
            h
            for h in self.history
            if (h.content if h.type == "text" else h.path) != val
        ]
        self.history.insert(0, clip)
        self.save()

    def toggle_pin(self, clip: Clip) -> None:
        """Adds to pinned if not there, otherwise removes it."""
        val = clip.content if clip.type == "text" else clip.path
        if not val:
            return

        target_idx = -1
        for i, p in enumerate(self.pinned):
            if (p.content if p.type == "text" else p.path) == val:
                target_idx = i
                break

        if target_idx != -1:
            removed = self.pinned.pop(target_idx)
            self.history.insert(0, removed)
        else:
            self.pinned.insert(0, clip)
            self.history = [
                h
                for h in self.history
                if (h.content if h.type == "text" else h.path) != val
            ]

        self.save()

    def select_clip(self) -> None:
        """Opens Rofi menu with [P#], [T#], and [I#] prefixes."""
        self.load()
        lines, mapping = [], []

        # Pinned entries
        for i, p in enumerate(self.pinned, 1):
            content = (p.content or "")[:PREVIEW_MAX].replace("\n", " ")
            lbl = (
                f"[P{i}] {content}"
                if p.type == "text"
                else f"[P{i}] IMAGE: {Path(p.path or '').name}"
            )
            lines.append(lbl)
            mapping.append(p)

        if self.history:
            lines.append("--- History ---")
            mapping.append(None)  # Placeholder for the separator

            t_idx, i_idx = 1, 1
            for h in self.history:
                content = (h.content or "")[:PREVIEW_MAX].replace("\n", " ")
                if h.type == "text":
                    lbl = f"[T{t_idx}] {content}"
                    t_idx += 1
                else:
                    lbl = f"[I{i_idx}] IMAGE: {Path(h.path or '').name}"
                    i_idx += 1
                lines.append(lbl)
                mapping.append(h)

        if not lines:
            return

        res = subprocess.run(
            ["rofi", "-dmenu", "-i", "-p", "Clipboard", "-format", "i"],
            input="\n".join(lines).encode(),
            capture_output=True,
            check=False,
        )

        try:
            idx_str = res.stdout.decode().strip()
            if not idx_str:
                return

            idx = int(idx_str)
            selected_clip = mapping[idx]
            if selected_clip is None:  # User clicked the separator
                return

            sel_val = (
                selected_clip.content
                if selected_clip.type == "text"
                else selected_clip.path
            )
            is_currently_pinned = any(
                (p.content if p.type == "text" else p.path) == sel_val
                for p in self.pinned
            )

            pin_label = "Unpin" if is_currently_pinned else "Pin"
            actions = ["Apply (Copy)", f"{pin_label} Item"]

            act_res = subprocess.run(
                ["rofi", "-dmenu", "-i", "-p", "Action"],
                input="\n".join(actions).encode(),
                capture_output=True,
                check=False,
            )

            choice = act_res.stdout.decode().strip()
            if "Apply" in choice:
                copy_to_clipboard(selected_clip)
            elif "Pin" in choice:
                self.toggle_pin(selected_clip)

        except (ValueError, IndexError):
            pass


# --- Main ---


def daemon_loop(mgr: ClipboardManager) -> None:
    """Continuously monitors the clipboard for changes."""
    last_val = None
    while True:
        curr = get_clipboard_content()
        if curr:
            curr_val = curr.content if curr.type == "text" else curr.path
            if curr_val != last_val:
                mgr.add_clip(curr)
                last_val = curr_val
        time.sleep(POLL_INTERVAL)


def main() -> None:
    """Entry point for the clipboard manager CLI."""
    parser = argparse.ArgumentParser(description="Wayland Clipboard Manager")
    parser.add_argument("command", choices=["daemon", "select", "clear"])
    args = parser.parse_args()
    mgr = ClipboardManager()

    if args.command == "daemon":
        daemon_loop(mgr)
    elif args.command == "select":
        mgr.select_clip()
    elif args.command == "clear":
        for p in [HISTORY_PATH, PIN_PATH, IMAGE_DIR]:
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                for f in p.iterdir():
                    f.unlink()
        print("Cleared.")


if __name__ == "__main__":
    main()
