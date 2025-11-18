#!/usr/bin/env python3
"""
Clipboard Manager for dwm — Python with Image Support
Features:
- Keeps history in memory for fast operations with text and images
- Persists history and pinned items to JSON files (atomic writes)
- Saves images as PNG files and displays previews in dmenu (requires patched dmenu)
- Integrates with xclip and dmenu
- Commands: daemon | select | pin | clear
Dependencies: python3 (3.8+), xclip, dmenu (with image preview patch), Pillow (PIL)
Usage examples:
  python3 clipmgr.py daemon
  python3 clipmgr.py select
  python3 clipmgr.py pin
  python3 clipmgr.py clear
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

# Check for PIL/Pillow
try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    Image = None  # type: ignore
    HAS_PIL = False
    print("Warning: Pillow not installed. Image support disabled.", file=sys.stderr)
    print("Install with: pip install Pillow", file=sys.stderr)

HOME = os.path.expanduser("~")
CLIP_DIR = os.path.join(HOME, ".cache", "clipboard")
HISTORY_PATH = os.path.join(CLIP_DIR, "history.json")
PIN_PATH = os.path.join(CLIP_DIR, "pinned.json")
IMAGE_DIR = os.path.join(CLIP_DIR, "images")
MAX_CLIPS = 50
MAX_IMAGE_CLIPS = 20  # Lower limit for images due to storage
POLL_INTERVAL = 0.5
PREVIEW_MAX = 60
DMENU_LINES = 20
# No image preview prefix needed - just show format and label

os.makedirs(CLIP_DIR, exist_ok=True)
os.makedirs(IMAGE_DIR, exist_ok=True)
_lock = threading.Lock()


def atomic_write(path: str, data: str) -> None:
    """Atomically write a string to a file."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def load_json_list(path: str) -> List[Dict]:
    """Load clipboard entries (supports both old text-only and new dict format)"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                # Convert old format (list of strings) to new format (list of dicts)
                result = []
                for item in data:
                    if isinstance(item, dict):
                        result.append(item)
                    else:
                        # Old text-only format
                        result.append(
                            {"type": "text", "content": str(item), "timestamp": None}
                        )
                return result
    except Exception:
        return []
    return []


def save_json_list(path: str, items: List[Dict]) -> None:
    """Save clipboard entries to JSON file"""
    try:
        atomic_write(path, json.dumps(items, ensure_ascii=False, indent=2))
    except Exception:
        with open(path, "w") as f:
            json.dump(items, f, ensure_ascii=False)


def get_content_hash(content: bytes) -> str:
    """Generate hash for content (used for image filenames)"""
    return hashlib.sha256(content).hexdigest()[:16]


def save_image_to_file(
    image_data: bytes, img_format: str = "png"
) -> Optional[str]:  # pylint: disable=unused-argument
    """Save image data to file and return path"""
    if not HAS_PIL or Image is None:
        return None

    try:
        # Generate filename based on content hash
        content_hash = get_content_hash(image_data)
        filename = f"{content_hash}.png"
        filepath = os.path.join(IMAGE_DIR, filename)

        # Don't save if already exists
        if os.path.exists(filepath):
            return filepath

        # Save as PNG
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmp:
            tmp.write(image_data)
            tmp_path = tmp.name

        # Convert to PNG with proper format for X11
        img = Image.open(tmp_path)

        # Convert to RGB or RGBA (remove palette mode, etc.)
        if img.mode not in ("RGB", "RGBA"):
            if img.mode == "P" and "transparency" in img.info:
                img = img.convert("RGBA")
            else:
                img = img.convert("RGB")

        # Save as PNG with standard settings
        img.save(filepath, "PNG", optimize=False)
        os.remove(tmp_path)

        return filepath
    except Exception as e:
        print(f"Error saving image: {e}", file=sys.stderr)
        return None


def cleanup_old_images(keep_paths: List[str]) -> None:
    """Remove image files that are no longer referenced"""
    try:
        keep_files = {os.path.basename(p) for p in keep_paths if p}
        for filename in os.listdir(IMAGE_DIR):
            if filename.endswith(".png") and filename not in keep_files:
                try:
                    os.remove(os.path.join(IMAGE_DIR, filename))
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
    except Exception:  # pylint: disable=broad-exception-caught
        pass


class ClipboardManager:
    """Manages clipboard history and pinned items"""

    def __init__(self):
        self.history: List[Dict] = load_json_list(HISTORY_PATH)
        self.pinned: List[Dict] = load_json_list(PIN_PATH)

    def save(self):
        """Save history and pinned items to disk"""
        # Trim history
        trimmed_history = []
        text_kept = 0
        image_kept = 0
        for item in self.history:
            if item.get("type") == "text" and text_kept < MAX_CLIPS:
                trimmed_history.append(item)
                text_kept += 1
            elif item.get("type") == "image" and image_kept < MAX_IMAGE_CLIPS:
                trimmed_history.append(item)
                image_kept += 1

        self.history = trimmed_history

        # Persist both lists
        save_json_list(HISTORY_PATH, self.history)
        save_json_list(PIN_PATH, self.pinned)

        # Cleanup unreferenced images
        all_image_paths = []
        for item in self.history + self.pinned:
            if item.get("type") == "image" and item.get("path"):
                all_image_paths.append(item["path"])
        cleanup_old_images(all_image_paths)

    def save_history_only(self):
        """Save only history (used by daemon to avoid overwriting pinned changes)"""
        # Trim and save only history
        text_count = 0
        image_count = 0
        trimmed_history = []
        for item in self.history:
            if item.get("type") == "text" and text_count < MAX_CLIPS:
                trimmed_history.append(item)
                text_count += 1
            elif item.get("type") == "image" and image_count < MAX_IMAGE_CLIPS:
                trimmed_history.append(item)
                image_count += 1

        self.history = trimmed_history
        save_json_list(HISTORY_PATH, self.history)

    def reload(self):
        """Reload from disk to get latest state"""
        self.history = load_json_list(HISTORY_PATH)
        self.pinned = load_json_list(PIN_PATH)

    def add_clip(self, clip_data: Dict) -> None:
        """Add a clip entry (text or image)"""
        if not clip_data or not clip_data.get("type"):
            return

        # Add timestamp
        clip_data["timestamp"] = datetime.now().isoformat()

        # Check if already pinned
        if self._is_duplicate(clip_data, self.pinned):
            return

        # Remove duplicates from history
        self.history = [
            c for c in self.history if not self._is_duplicate(clip_data, [c])
        ]

        # Prepend
        self.history.insert(0, clip_data)

    def _is_duplicate(self, clip: Dict, clip_list: List[Dict]) -> bool:
        """Check if clip is duplicate in list"""
        for item in clip_list:
            if clip.get("type") != item.get("type"):
                continue
            if clip.get("type") == "text":
                if clip.get("content") == item.get("content"):
                    return True
            elif clip.get("type") == "image":
                if clip.get("path") == item.get("path"):
                    return True
        return False

    def get_preview(self, item: Dict, max_len: int = PREVIEW_MAX) -> str:
        """Get preview string for an item"""
        if item.get("type") == "text":
            text = item.get("content", "")
            single = text.replace("\n", " ")
            if len(single) > max_len:
                return single[:max_len] + "…"
            return single
        elif item.get("type") == "image":
            path = item.get("path", "")
            filename = os.path.basename(path) if path else "image"
            timestamp = item.get("timestamp", "")
            if timestamp:
                try:
                    dt = datetime.fromisoformat(timestamp)
                    time_str = dt.strftime("%m/%d %H:%M")
                    return f"{filename} ({time_str})"
                except ValueError:
                    pass
            return filename
        return ""

    def get_image_type(self, path: str) -> str:
        """Get image type from file extension"""
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        return ext.upper() if ext else "IMAGE"

    def build_menu_entries(self):
        """Build menu entries for dmenu display"""
        entries = []  # tuples (label, item_dict)
        # pinned first
        for i, p in enumerate(self.pinned, start=1):
            if p.get("type") == "image" and p.get("path"):
                img_type = self.get_image_type(p["path"])
                label = f"{img_type}:{p['path']} [P{i}]"
            else:
                label = f"[P{i}] {self.get_preview(p)}"
            entries.append((label, p))

        # then history
        for i, h in enumerate(self.history, start=1):
            if h.get("type") == "image" and h.get("path"):
                img_type = self.get_image_type(h["path"])
                label = f"{img_type}:{h['path']} [H{i}]"
            else:
                label = f"[H{i}] {self.get_preview(h)}"
            entries.append((label, h))

        return entries

    def select_clip(self):
        """Show dmenu to select and restore a clip"""
        self.reload()
        entries = self.build_menu_entries()
        if not entries:
            return

        menu_text = "\n".join(label for label, _ in entries)
        try:
            sel = run_dmenu(menu_text, prompt="Clipboard History:")
        except subprocess.CalledProcessError:
            return

        if not sel:
            return

        # Find selected entry by matching the label
        for label, item in entries:

            if clean_label == sel:
                self._restore_clipboard(item)
                return

    def pin_menu(self):  # pylint: disable=too-many-branches
        """Show dmenu to pin/unpin items"""
        self.reload()

        lines = []
        entries = []

        if self.pinned:
            lines.append("=== PINNED (select to unpin) ===")
            for i, p in enumerate(self.pinned, start=1):
                if p.get("type") == "image" and p.get("path"):
                    img_type = self.get_image_type(p["path"])
                    label = f"{img_type}:{p['path']} [P{i}]"
                else:
                    label = f"[P{i}] {self.get_preview(p)}"
                lines.append(label)
                entries.append((label, p))
            lines.append("")

        if self.history:
            lines.append("=== HISTORY (select to pin) ===")
            for i, h in enumerate(self.history, start=1):
                if h.get("type") == "image" and h.get("path"):
                    img_type = self.get_image_type(h["path"])
                    label = f"{img_type}:{h['path']} [H{i}]"
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

        # Find entry by matching label
        for label, item in entries:
            if label == sel:
                if "[P" in label:
                    # Unpin
                    self.pinned = [
                        p for p in self.pinned if not self._is_duplicate(item, [p])
                    ]
                    # Add back to history
                    if not self._is_duplicate(item, self.history):
                        self.history.insert(0, item)
                else:
                    # Pin
                    self.history = [
                        h for h in self.history if not self._is_duplicate(item, [h])
                    ]
                    if not self._is_duplicate(item, self.pinned):
                        self.pinned.append(item)
                self.save()
                break

    def clear_history(self):
        """Clear clipboard history (keep pinned items)"""
        self.reload()
        self.history = []
        self.save()

    def _restore_clipboard(self, item: Dict):
        """Restore item to clipboard"""
        item_type = item.get("type")
        if item_type == "text":
            copy_to_clipboard(item.get("content", ""))
        if item_type == "image":
            copy_image_to_clipboard(item.get("path", ""))


# Helper utilities
def run_dmenu(menu_input: str, prompt: str = "") -> str:
    """Run dmenu and return selected item"""
    cmd = ["dmenu", "-l", str(DMENU_LINES), "-p", prompt]
    proc = subprocess.run(
        cmd, input=menu_input.encode("utf-8"), stdout=subprocess.PIPE, check=False
    )
    return proc.stdout.decode("utf-8").rstrip("\n")


def copy_to_clipboard(text: str) -> None:
    """Copy text to clipboard using xclip"""
    try:
        with subprocess.Popen(
            ["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE
        ) as proc:
            proc.communicate(text.encode("utf-8"))
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"Error copying to clipboard: {e}", file=sys.stderr)


def copy_image_to_clipboard(image_path: str) -> None:
    """Copy image file to clipboard"""
    if not os.path.exists(image_path):
        return
    try:
        with open(image_path, "rb") as f:
            with subprocess.Popen(
                ["xclip", "-selection", "clipboard", "-t", "image/png"],
                stdin=subprocess.PIPE,
            ) as proc:
                proc.communicate(f.read())
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"Error copying image to clipboard: {e}", file=sys.stderr)


def get_clipboard_content() -> Optional[Dict]:  # pylint: disable=too-many-nested-blocks
    """Get clipboard content (text or image)"""
    # Try to get image first (if PIL available)
    if HAS_PIL:
        try:
            # Check available targets
            targets = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=1,
                check=False,
            )
            available_targets = targets.stdout.decode("utf-8").strip().split("\n")

            # Check for image formats
            image_targets = ["image/png", "image/jpeg", "image/jpg", "image/gif"]
            for target in image_targets:
                if target in available_targets:
                    # Get image data
                    result = subprocess.run(
                        ["xclip", "-selection", "clipboard", "-t", target, "-o"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        timeout=2,
                        check=False,
                    )
                    if result.returncode == 0 and result.stdout:
                        image_path = save_image_to_file(
                            result.stdout, target.split("/")[1]
                        )
                        if image_path:
                            return {
                                "type": "image",
                                "path": image_path,
                                "format": target,
                            }
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    # Try to get text
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
            return {"type": "text", "content": text}
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    return None


def daemon_loop(manager: ClipboardManager, interval: float = POLL_INTERVAL):
    last_hash = None
    dirty = False

    try:
        while True:
            current = get_clipboard_content()
            if current:
                # Create hash to detect changes
                if current.get("type") == "text":
                    current_hash = hashlib.sha256(
                        current.get("content", "").encode()
                    ).hexdigest()
                else:
                    current_hash = current.get("path", "")

                if current_hash != last_hash:
                    with _lock:
                        manager.pinned = load_json_list(PIN_PATH)
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


def main():
    """Main entry point"""
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

    if not HAS_PIL and args.command == "daemon":
        print("Warning: Running without Pillow - image support disabled")

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
    """Entry point when run as script"""
    main()
