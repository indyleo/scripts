#!/usr/bin/env python3
"""
Clipboard Manager for dwm — Python (Option B)
Features:
- Keeps history in memory for fast operations with large multiline entries
- Persists history and pinned items to JSON files (atomic writes)
- Integrates with xclip and dmenu (same UX as your bash script)
- Commands: daemon | select | pin | clear
Dependencies: python3 (3.8+ recommended), xclip, dmenu
Usage examples:
  python3 clipmgr.py daemon
  python3 clipmgr.py select
  python3 clipmgr.py pin
  python3 clipmgr.py clear
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import List

HOME = os.path.expanduser("~")
CLIP_DIR = os.path.join(HOME, ".cache", "clipboard")
HISTORY_PATH = os.path.join(CLIP_DIR, "history.json")
PIN_PATH = os.path.join(CLIP_DIR, "pinned.json")
MAX_CLIPS = 50
POLL_INTERVAL = 0.5
PREVIEW_MAX = 60
DMENU_LINES = 20

os.makedirs(CLIP_DIR, exist_ok=True)
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

def load_json_list(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                # ensure all are strings
                return [str(x) for x in data]
    except Exception:
        # Backwards compatibility: maybe file is newline-escaped single-line entries
        try:
            with open(path, "r") as f:
                lines = [line.rstrip("\n") for line in f]
                if lines:
                    # try to detect escaped-newline format (\n inside lines)
                    # if we find literal "\\n" in any line, unescape it
                    if any("\\n" in line for line in lines):
                        return [line.replace("\\n", "\n") for line in lines]
                    return lines
        except Exception:
            return []
    return []

def save_json_list(path: str, items: List[str]) -> None:
    try:
        atomic_write(path, json.dumps(items, ensure_ascii=False, indent=2))
    except Exception:
        # best-effort fallback
        with open(path, "w") as f:
            json.dump(items, f, ensure_ascii=False)

class ClipboardManager:
    def __init__(self):
        self.history: List[str] = load_json_list(HISTORY_PATH)
        self.pinned: List[str] = load_json_list(PIN_PATH)

    def save(self):
        # Persist both lists
        save_json_list(HISTORY_PATH, self.history[:MAX_CLIPS])
        save_json_list(PIN_PATH, self.pinned)

    def save_history_only(self):
        # Save only history (used by daemon to avoid overwriting pinned changes)
        save_json_list(HISTORY_PATH, self.history[:MAX_CLIPS])

    def reload(self):
        """Reload from disk to get latest state (useful for non-daemon commands)"""
        self.history = load_json_list(HISTORY_PATH)
        self.pinned = load_json_list(PIN_PATH)

    def add_clip(self, clip: str) -> None:
        clip = clip if clip is not None else ""
        clip = clip.rstrip("\n")
        if not clip.strip():
            return
        # Skip if pinned
        if clip in self.pinned:
            return
        # Remove duplicates from history
        try:
            self.history = [c for c in self.history if c != clip]
        except Exception:
            pass
        # Prepend
        self.history.insert(0, clip)
        # Trim
        if len(self.history) > MAX_CLIPS:
            self.history = self.history[:MAX_CLIPS]

    def get_preview(self, text: str, max_len: int = PREVIEW_MAX) -> str:
        single = text.replace("\n", " ")
        if len(single) > max_len:
            return single[:max_len] + "…"
        return single

    def build_menu_entries(self):
        entries = []  # tuples (label, content)
        # pinned first
        for i, p in enumerate(self.pinned, start=1):
            label = f"[P{i}] {self.get_preview(p)}"
            entries.append((label, p))
        # then history
        for i, h in enumerate(self.history, start=1):
            label = f"[{i}] {self.get_preview(h)}"
            entries.append((label, h))
        return entries

    def select_clip(self):
        # Reload to get latest state
        self.reload()

        entries = self.build_menu_entries()
        if not entries:
            return
        menu_text = "\n".join(label for label, _ in entries)
        try:
            sel = run_dmenu(menu_text, prompt="Clipboard History:")
        except Exception:
            return
        if not sel:
            return
        # find selected label
        for label, content in entries:
            if label == sel:
                copy_to_clipboard(content)
                return
        # if dmenu returns a substring match, try to match by prefix
        for label, content in entries:
            if sel.startswith(label.split(" ", 1)[0]):
                copy_to_clipboard(content)
                return

    def pin_menu(self):
        # Reload to get latest state
        self.reload()

        # Build menu similar to bash: show pinned block then history block separated by headers
        lines = []
        entries = []
        if self.pinned:
            lines.append("=== PINNED (select to unpin) ===")
            for i, p in enumerate(self.pinned, start=1):
                label = f"[P{i}] {self.get_preview(p)}"
                lines.append(label)
                entries.append((label, p))
            lines.append("")
        if self.history:
            lines.append("=== HISTORY (select to pin) ===")
            for i, h in enumerate(self.history, start=1):
                label = f"[{i}] {self.get_preview(h)}"
                lines.append(label)
                entries.append((label, h))
        if not lines:
            return
        menu_text = "\n".join(lines)
        sel = run_dmenu(menu_text, prompt="Pin/Unpin:")
        if not sel or sel.startswith("==="):
            return
        # find entry
        for idx, (label, content) in enumerate(entries):
            if label == sel:
                if label.startswith("[P"):
                    # unpin
                    try:
                        self.pinned.remove(content)
                    except ValueError:
                        pass
                    # add back to history (as most recent)
                    self.add_clip(content)
                else:
                    # pin (remove from history and append to pinned)
                    try:
                        self.history.remove(content)
                    except ValueError:
                        pass
                    if content not in self.pinned:
                        self.pinned.append(content)
                self.save()
                return

    def clear_history(self):
        # Reload to get latest state
        self.reload()

        self.history = []
        self.save()

# Helper utilities
def run_dmenu(menu_input: str, prompt: str = "") -> str:
    # Use dmenu -l for a vertical list; we send the menu_input via stdin
    cmd = ["dmenu", "-l", str(DMENU_LINES), "-p", prompt]
    proc = subprocess.run(cmd, input=menu_input.encode("utf-8"), stdout=subprocess.PIPE)
    return proc.stdout.decode("utf-8").rstrip("\n")

def copy_to_clipboard(text: str) -> None:
    # Use xclip to set clipboard. Use -selection clipboard and pipe the bytes.
    try:
        p = subprocess.Popen(
            ["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE
        )
        p.communicate(text.encode("utf-8"))
    except Exception:
        pass

def get_clipboard_text() -> str:
    try:
        out = subprocess.run(
            ["xclip", "-o", "-selection", "clipboard"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1,
        )
        return out.stdout.decode("utf-8")
    except Exception:
        return ""

# Daemon that polls clipboard and keeps history in memory; periodically persists to disk
def daemon_loop(manager: ClipboardManager, interval: float = POLL_INTERVAL):
    last = None
    dirty = False
    try:
        while True:
            current = get_clipboard_text()
            if current and current != last:
                with _lock:
                    # Reload pinned list to avoid overwriting user changes
                    manager.pinned = load_json_list(PIN_PATH)
                    manager.add_clip(current)
                    dirty = True
                last = current
            # flush to disk if dirty (periodically)
            if dirty:
                with _lock:
                    # Only save history, not pinned (to avoid overwriting user pin changes)
                    manager.save_history_only()
                    dirty = False
            time.sleep(interval)
    except KeyboardInterrupt:
        # save on exit
        with _lock:
            manager.save_history_only()

def main():
    parser = argparse.ArgumentParser(description="Clipboard manager")
    parser.add_argument(
        "command",
        nargs="?",
        default=None,  # Changed from "select" to None
        choices=["daemon", "select", "pin", "clear"],
        help="command to run",
    )
    args = parser.parse_args()

    # Show help if no command provided
    if args.command is None:
        parser.print_help()
        sys.exit(0)

    manager = ClipboardManager()

    if args.command == "daemon":
        print("Starting clipboard daemon (press Ctrl-C to stop)")
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
