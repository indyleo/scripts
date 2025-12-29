#!/usr/bin/env python3
"""
Wayland Clipboard manager with grouped menus, text/image support, and persistence.
Handles history, pinning, and image caching via wl-clipboard and rofi.
Optimized for Wayland compositors (Hyprland, Sway, GNOME, etc).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

try:
    # We only check for the existence of PIL to enable image features.
    import PIL.Image  # pylint: disable=unused-import

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# --- Config ---
MAX_TEXT_HISTORY = 150
MAX_IMAGE_HISTORY = 10
MAX_PINNED_HISTORY = 50
POLL_INTERVAL = 1.0
PREVIEW_MAX = 80
# Added -no-custom to prevent typing new text, forces selection
ROFI_COMMAND = ["rofi", "-dmenu", "-i", "-p", "Clipboard", "-no-custom"]

HOME = Path.home()
CLIP_DIR = HOME.joinpath(".cache", "wayclip")
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
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        """Convert clip to dictionary for JSON serialization."""
        return asdict(self)


# --- Clipboard Core (wl-clipboard wrappers) ---


def run_command(
    cmd: List[str], input_data: Optional[bytes] = None, timeout: Optional[float] = None
) -> Optional[bytes]:
    """
    Helper to run subprocess commands safely.
    timeout=None allows infinite wait (required for Rofi).
    """
    try:
        proc = subprocess.run(
            cmd,
            input=input_data,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def copy_to_clipboard(clip: Clip) -> None:
    """Restores an item to the clipboard."""
    # Clean up previous background wl-copy instances
    subprocess.run(
        ["pkill", "-u", str(os.getuid()), "-x", "wl-copy"],
        stderr=subprocess.DEVNULL,
        check=False,
    )

    try:
        if clip.type == "text" and clip.content:
            # pylint: disable=consider-using-with
            subprocess.Popen(["wl-copy", clip.content])

        elif clip.type == "image" and clip.path:
            path = Path(clip.path)
            if path.exists():
                with open(path, "rb") as f:
                    img_data = f.read()

                # pylint: disable=consider-using-with
                proc = subprocess.Popen(
                    ["wl-copy", "--type", "image/png"], stdin=subprocess.PIPE
                )
                try:
                    proc.communicate(input=img_data, timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()

    except OSError as e:
        print(f"Error copying to clipboard: {e}", file=sys.stderr)


def get_clipboard_content() -> Optional[Clip]:
    """Retrieves current clipboard content using wl-paste."""

    # 1. Check available MIME types
    types_out = run_command(["wl-paste", "--list-types"], timeout=1.0)
    if not types_out:
        return None

    mime_types = types_out.decode("utf-8", errors="ignore").splitlines()

    # 2. Priority: Image
    if HAS_PIL and any(t in mime_types for t in ["image/png", "image/jpeg"]):
        img_data = run_command(["wl-paste", "--type", "image/png"], timeout=3.0)
        if img_data:
            h = hashlib.sha256(img_data).hexdigest()[:16]
            img_path = IMAGE_DIR.joinpath(f"{h}.png")

            if not img_path.exists():
                try:
                    with open(img_path, "wb") as f:
                        f.write(img_data)
                except OSError:
                    return None
            return Clip(type="image", path=str(img_path))

    # 3. Fallback: Text
    text_data = run_command(["wl-paste", "--no-newline"], timeout=1.0)
    if text_data:
        try:
            text_str = text_data.decode("utf-8").strip()
            if text_str:
                return Clip(type="text", content=text_str)
        except UnicodeDecodeError:
            pass

    return None


# --- Manager Logic ---


class ClipboardManager:
    """Manages history, pinning, and disk IO for clips."""

    def __init__(self) -> None:
        self.history: List[Clip] = []
        self.pinned: List[Clip] = []
        self.reload()

    def reload(self) -> None:
        """Reloads data from JSON files."""
        self.history = self._load_file(HISTORY_PATH)
        self.pinned = self._load_file(PIN_PATH)

    def _load_file(self, path: Path) -> List[Clip]:
        if not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                return [Clip(**item) for item in data]
        except (json.JSONDecodeError, OSError):
            return []

    def save(self) -> None:
        """Saves data to JSON files and cleans up old images."""
        with _lock:
            self.history = self._deduplicate(self.history)[:MAX_TEXT_HISTORY]
            self.pinned = self._deduplicate(self.pinned)[:MAX_PINNED_HISTORY]

            self._atomic_save(HISTORY_PATH, self.history)
            self._atomic_save(PIN_PATH, self.pinned)
            self._cleanup_images()

    def _atomic_save(self, path: Path, clips: List[Clip]) -> None:
        try:
            temp = path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as f:
                json.dump([c.to_dict() for c in clips], f, indent=2, ensure_ascii=False)
            temp.replace(path)
        except OSError as e:
            print(f"Save failed for {path}: {e}", file=sys.stderr)

    def _deduplicate(self, clips: List[Clip]) -> List[Clip]:
        seen = set()
        unique = []
        for c in clips:
            key = (c.type, c.content if c.type == "text" else c.path)
            if key not in seen and key[1]:
                unique.append(c)
                seen.add(key)
        return unique

    def _cleanup_images(self) -> None:
        valid_paths = {
            Path(c.path).name
            for c in (self.history + self.pinned)
            if c.type == "image" and c.path
        }
        for p in IMAGE_DIR.iterdir():
            if p.name not in valid_paths:
                try:
                    p.unlink()
                except OSError:
                    pass

    def add_clip(self, clip: Clip) -> None:
        """Adds a clip to history unless it is already pinned."""
        is_pinned = any(
            p.type == clip.type
            and (p.content == clip.content if p.type == "text" else p.path == clip.path)
            for p in self.pinned
        )

        if is_pinned:
            return

        self.history = [
            h
            for h in self.history
            if not (
                h.type == clip.type
                and (
                    h.content == clip.content
                    if h.type == "text"
                    else h.path == clip.path
                )
            )
        ]
        self.history.insert(0, clip)
        self.save()

    def toggle_pin(self, clip: Clip) -> None:
        """Moves a clip between history and pinned lists."""
        found_in_pinned = -1
        for i, p in enumerate(self.pinned):
            if p.type == clip.type and (
                p.content == clip.content if p.type == "text" else p.path == clip.path
            ):
                found_in_pinned = i
                break

        if found_in_pinned >= 0:
            removed = self.pinned.pop(found_in_pinned)
            self.history.insert(0, removed)
        else:
            self.history = [
                h
                for h in self.history
                if not (
                    h.type == clip.type
                    and (
                        h.content == clip.content
                        if h.type == "text"
                        else h.path == clip.path
                    )
                )
            ]
            self.pinned.insert(0, clip)

        self.save()

    # --- UI Logic ---

    def show_menu(self) -> None:
        """Shows the main selection menu with sorted sections."""
        self.reload()

        # 1. Filter lists
        pinned_clips = self.pinned
        image_clips = [c for c in self.history if c.type == "image"]
        text_clips = [c for c in self.history if c.type == "text"]

        # 2. Build menu entries list: Tuple(Label, Clip or None)
        # None indicates a header row
        menu_items: List[Tuple[str, Optional[Clip]]] = []

        def add_section(header_title: str, clips: List[Clip], type_char: str):
            if not clips:
                return
            # Add Header
            menu_items.append((f"# -- {header_title} -- #", None))
            # Add Items
            for i, c in enumerate(clips, 1):
                # Format prefix as [T#], [P#], [I#]
                prefix = f"[{type_char}{i}]"
                label = self._format_label(c, prefix)
                menu_items.append((label, c))

        # 3. Construct the list in desired order
        # "P" for Pinned, "I" for Images, "T" for Text
        add_section("Pinned", pinned_clips, "P")
        add_section("Images", image_clips, "I")
        add_section("Text", text_clips, "T")

        if not menu_items:
            return

        # 4. Run Rofi
        menu_str = "\n".join(m[0] for m in menu_items)
        selection_idx = self._run_rofi(menu_str, "-format", "i")

        if selection_idx is None or not selection_idx.isdigit():
            return

        try:
            idx = int(selection_idx)
            if 0 <= idx < len(menu_items):
                label, clip = menu_items[idx]
                if clip is not None:
                    self._handle_selection_action(clip)
        except (ValueError, IndexError):
            pass

    def _format_label(self, clip: Clip, prefix: str) -> str:
        """Formats the label for Rofi."""
        if clip.type == "image":
            filename = Path(clip.path).name if clip.path else "Unknown"
            return f"{prefix} Image: {filename}"

        txt = (clip.content or "").replace("\n", " ").strip()
        if len(txt) > PREVIEW_MAX:
            txt = txt[:PREVIEW_MAX] + "…"
        return f"{prefix} {txt}"

    def _handle_selection_action(self, clip: Clip) -> None:
        """Sub-menu to Copy or Pin."""
        is_pinned = any(
            p.type == clip.type
            and (p.content == clip.content if p.type == "text" else p.path == clip.path)
            for p in self.pinned
        )

        pin_label = "Unpin Item" if is_pinned else "Pin Item"
        options = [" Paste (Copy to Clipboard)", f" {pin_label}"]

        sel_str = run_command(
            ["rofi", "-dmenu", "-p", "Action", "-i"],
            input_data="\n".join(options).encode(),
            timeout=None,
        )

        if not sel_str:
            return

        choice = sel_str.decode().strip()

        if "Paste" in choice:
            copy_to_clipboard(clip)
        elif "Pin" in choice or "Unpin" in choice:
            self.toggle_pin(clip)
            self.show_menu()

    def _run_rofi(self, input_str: str, *args) -> Optional[str]:
        cmd = ROFI_COMMAND + list(args)
        # PASS timeout=None so it waits indefinitely for user selection
        out = run_command(cmd, input_data=input_str.encode(), timeout=None)
        return out.decode().strip() if out else None


# --- Main Execution ---


def daemon_loop(manager: ClipboardManager) -> None:
    """Run the main monitoring loop."""
    print("WayClip Daemon started.")
    print(f"History: {HISTORY_PATH}")
    print(f"Images: {'Enabled' if HAS_PIL else 'Disabled (install python-pillow)'}")

    last_hash = ""

    while True:
        clip = get_clipboard_content()
        current_hash = ""

        if clip:
            if clip.type == "text" and clip.content:
                current_hash = hashlib.md5(clip.content.encode()).hexdigest()
            elif clip.type == "image" and clip.path:
                current_hash = hashlib.md5(clip.path.encode()).hexdigest()

            if current_hash and current_hash != last_hash:
                manager.add_clip(clip)
                last_hash = current_hash

        time.sleep(POLL_INTERVAL)


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Wayland Clipboard Manager")
    parser.add_argument(
        "command", choices=["daemon", "select", "clear"], help="Command to run"
    )
    args = parser.parse_args()

    manager = ClipboardManager()

    if args.command == "daemon":
        try:
            daemon_loop(manager)
        except KeyboardInterrupt:
            print("\nStopping daemon.")
    elif args.command == "select":
        manager.show_menu()
    elif args.command == "clear":
        manager.history = []
        manager.save()
        print("History cleared (Pinned items preserved).")


if __name__ == "__main__":
    main()
