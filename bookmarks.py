#!/usr/bin/env python3
"""
Optimized rofi bookmark manager with folder support
"""
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Bookmark:
    label: str
    url: str
    folder: Optional[str] = None

    def display(self) -> str:
        return f"{self.label} | {self.url}"

    def to_file_format(self) -> str:
        if self.folder:
            return f"{self.folder} | {self.label} | {self.url}"
        return f"{self.label} | {self.url}"


class BookmarkManager:
    def __init__(self, browser: Optional[str] = None):
        self.bookmark_file = (
            Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local/share"))
            / "bookmarks"
            / "bookmarks.txt"
        )
        self.rofi_args = ["-dmenu", "-i", "-l", "10"]
        self.browser = browser or os.getenv("BROWSER", "xdg-open")

        # Verify browser exists
        if not self._command_exists(self.browser):
            print(f"Error: {self.browser} not found", file=sys.stderr)
            sys.exit(1)

        # Ensure bookmark file exists
        self.bookmark_file.parent.mkdir(parents=True, exist_ok=True)
        self.bookmark_file.touch(exist_ok=True)

        # Cache bookmarks for performance
        self._bookmarks_cache: Optional[List[Bookmark]] = None

    @staticmethod
    def _command_exists(cmd: str) -> bool:
        return subprocess.run(["which", cmd], capture_output=True).returncode == 0

    def _rofi(self, prompt: str, items: List[str]) -> Optional[str]:
        """Run rofi with given items and return selection"""
        try:
            result = subprocess.run(
                ["rofi"] + self.rofi_args + ["-p", prompt],
                input="\n".join(items),
                capture_output=True,
                text=True,
                check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except FileNotFoundError:
            print("Error: rofi not found", file=sys.stderr)
            sys.exit(1)

    def _notify(self, title: str, message: str):
        """Send desktop notification"""
        subprocess.run(["notify-send", title, message], check=False)

    def _load_bookmarks(self, force_reload: bool = False) -> List[Bookmark]:
        """Load and parse bookmarks with caching"""
        if self._bookmarks_cache is not None and not force_reload:
            return self._bookmarks_cache

        bookmarks = []
        with open(self.bookmark_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                parts = [p.strip() for p in line.split("|")]
                if len(parts) == 2:
                    bookmarks.append(Bookmark(label=parts[0], url=parts[1]))
                elif len(parts) == 3:
                    bookmarks.append(
                        Bookmark(folder=parts[0], label=parts[1], url=parts[2])
                    )

        self._bookmarks_cache = bookmarks
        return bookmarks

    def _save_bookmarks(self, bookmarks: List[Bookmark]):
        """Save bookmarks to file"""
        with open(self.bookmark_file, "w", encoding="utf-8") as f:
            for bm in bookmarks:
                f.write(bm.to_file_format() + "\n")
        self._bookmarks_cache = None  # Invalidate cache

    def _get_folders(self) -> List[str]:
        """Get unique folder names"""
        folders = set()
        for bm in self._load_bookmarks():
            if bm.folder:
                folders.add(bm.folder)
        return sorted(folders)

    def _top_level_menu(self) -> Optional[str]:
        """Show folders and root bookmarks"""
        bookmarks = self._load_bookmarks()
        items = []

        # Add root bookmarks
        items.extend(bm.display() for bm in bookmarks if not bm.folder)

        # Add folders
        items.extend(self._get_folders())

        return self._rofi("Bookmarks", sorted(items))

    def _folder_menu(self, folder: str) -> Optional[str]:
        """Show bookmarks in a specific folder"""
        bookmarks = self._load_bookmarks()
        items = [bm.display() for bm in bookmarks if bm.folder == folder]
        return self._rofi(folder, sorted(items))

    def _open_url(self, entry: str):
        """Extract URL and open in browser"""
        if "|" in entry:
            url = entry.split("|", 1)[1].strip()
            subprocess.Popen(
                [self.browser, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def open_bookmark(self):
        """Navigate and open a bookmark"""
        choice = self._top_level_menu()
        if not choice:
            return

        if "|" in choice:
            self._open_url(choice)
        else:
            # It's a folder
            entry = self._folder_menu(choice)
            if entry:
                self._open_url(entry)

    def add_bookmark(self):
        """Add a new bookmark"""
        folders = self._get_folders()
        folder_choice = self._rofi("Folder (or none)", ["(none)"] + folders)
        if not folder_choice:
            return

        label = self._rofi("Label:", [])
        if not label:
            return

        url = self._rofi("URL:", [])
        if not url:
            return

        folder = None if folder_choice == "(none)" else folder_choice
        new_bookmark = Bookmark(label=label, url=url, folder=folder)

        bookmarks = self._load_bookmarks()
        bookmarks.append(new_bookmark)
        self._save_bookmarks(bookmarks)

        self._notify("Bookmark added", f"{label} → {url}")

    def delete_bookmark(self):
        """Navigate and delete a bookmark"""
        choice = self._top_level_menu()
        if not choice:
            return

        if "|" not in choice:
            # It's a folder
            choice = self._folder_menu(choice)
            if not choice:
                return

        bookmarks = self._load_bookmarks()
        # Find and remove matching bookmark
        original_len = len(bookmarks)
        bookmarks = [bm for bm in bookmarks if bm.display() != choice]

        if len(bookmarks) < original_len:
            self._save_bookmarks(bookmarks)
            self._notify("Bookmark deleted", choice)

    def main_menu(self):
        """Show main action menu"""
        action = self._rofi("Bookmark Manager", ["Open", "Add", "Delete", "Quit"])

        if action == "Open":
            self.open_bookmark()
        elif action == "Add":
            self.add_bookmark()
        elif action == "Delete":
            self.delete_bookmark()


if __name__ == "__main__":
    browser = sys.argv[1] if len(sys.argv) > 1 else None
    manager = BookmarkManager(browser)
    manager.main_menu()
