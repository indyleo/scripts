#!/usr/bin/env python3
"""
Nerd Font Icon Picker (Wayland version)
Downloads Nerd Font icons and provides a fuzzy finder interface for selection
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import requests

# Configuration
CACHE_DIR = Path.home() / ".cache" / "nerdfont-picker"
NERDFONT_FILE = CACHE_DIR / "nerdfont.txt"
NERDFONT_URL = (
    "https://raw.githubusercontent.com/ryanoasis/nerd-fonts/"
    "master/css/nerd-fonts-generated.css"
)


def ensure_cache_dir():
    """Create cache directory if it doesn't exist"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def download_nerdfont_data():
    """Download and parse Nerd Font icons from GitHub CSS file"""
    print("Downloading Nerd Font data...", file=sys.stderr)

    try:
        response = requests.get(NERDFONT_URL, timeout=30)
        response.raise_for_status()

        icons = []
        # Parse CSS file for icon definitions
        # Format: .nf-dev-python:before { content: "\e73c"; }
        pattern = r'\.nf-([^:]+):before\s*\{\s*content:\s*"\\([0-9a-fA-F]+)"\s*;'

        for match in re.finditer(pattern, response.text):
            name = match.group(1)
            code = match.group(2)

            try:
                # Convert hex codepoint to Unicode character
                uni = chr(int(code, 16))
                # Format: icon name (with spaces instead of dashes)
                formatted_name = name.replace("-", " ")
                icons.append(f"{uni} {formatted_name}")
            except (ValueError, OverflowError):
                continue

        if icons:
            ensure_cache_dir()
            with open(NERDFONT_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(icons))
            print(f"Downloaded {len(icons)} icons to {NERDFONT_FILE}", file=sys.stderr)
            return True

        print("No icons found in CSS file", file=sys.stderr)
        return False

    except requests.RequestException as e:
        print(f"Error downloading: {e}", file=sys.stderr)
        return False
    except IOError as e:
        print(f"Error writing cache file: {e}", file=sys.stderr)
        return False


def check_dependencies():
    """Check if required tools are available"""
    # Check for fuzzy finder (try wofi, fuzzel, tofi, or rofi)
    finders = ["wofi", "fuzzel", "tofi", "rofi"]
    selected_finder = None

    for finder in finders:
        if (
            subprocess.run(
                ["which", finder], capture_output=True, check=False
            ).returncode
            == 0
        ):
            selected_finder = finder
            break

    if not selected_finder:
        print(
            "Error: No fuzzy finder found. Please install one of: wofi, fuzzel, tofi, or rofi.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Check for Wayland clipboard tool (wl-copy from wl-clipboard)
    has_wl_copy = (
        subprocess.run(
            ["which", "wl-copy"], capture_output=True, check=False
        ).returncode
        == 0
    )

    if not has_wl_copy:
        print("Error: wl-copy not found. Please install wl-clipboard.", file=sys.stderr)
        sys.exit(1)

    return selected_finder


def show_selection_menu(finder):
    """Display selection menu using available fuzzy finder"""
    if not NERDFONT_FILE.exists():
        print("Cache file not found. Downloading...", file=sys.stderr)
        if not download_nerdfont_data():
            sys.exit(1)

    with open(NERDFONT_FILE, "r", encoding="utf-8") as f:
        icons_data = f.read()

    # Configure command based on finder
    if finder == "wofi":
        cmd = ["wofi", "--dmenu", "--lines", "10", "--prompt", "Select Icon:"]
    elif finder == "fuzzel":
        cmd = ["fuzzel", "--dmenu", "--lines", "10", "--prompt", "Select Icon: "]
    elif finder == "tofi":
        cmd = ["tofi", "--prompt-text", "Select Icon: "]
    else:  # rofi (can work on Wayland)
        cmd = ["rofi", "-dmenu", "-l", "10", "-p", "Select Icon:"]

    try:
        result = subprocess.run(
            cmd,
            input=icons_data,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        if result.returncode == 0 and result.stdout.strip():
            # Extract the icon (first character)
            selected = result.stdout.strip()
            icon = selected.split()[0] if selected else ""
            return icon

    except (IOError, subprocess.SubprocessError) as e:
        print(f"Error showing menu: {e}", file=sys.stderr)

    return None


def copy_to_clipboard(text):
    """Copy text to clipboard using wl-copy"""
    try:
        subprocess.run(
            ["wl-copy"],
            input=text,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error copying to clipboard: {e}", file=sys.stderr)
        return False


def send_notification(icon):
    """Send desktop notification"""
    try:
        subprocess.run(
            ["notify-send", "Copied to clipboard!", f"Icon: {icon}"], check=False
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pass  # Notification is optional


def main():
    """Main function"""
    parser = argparse.ArgumentParser(
        description="Nerd Font Icon Picker (Wayland) - Download and select icons"
    )
    parser.add_argument(
        "--update", action="store_true", help="Force update the icon cache"
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Only download icons, do not show menu",
    )

    args = parser.parse_args()

    # Handle download/update mode
    if args.update or args.download_only:
        success = download_nerdfont_data()
        sys.exit(0 if success else 1)

    # Check dependencies
    finder = check_dependencies()

    # Show selection menu
    selected_icon = show_selection_menu(finder)

    if selected_icon:
        if copy_to_clipboard(selected_icon):
            send_notification(selected_icon)
            print(selected_icon)  # Also print to stdout
        else:
            sys.exit(1)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
