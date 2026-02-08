#!/usr/bin/env python3
"""
Neovim Version Manager (2025)
A Python-based manager for installing and switching Neovim versions
"""

import json
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict
from typing import List as ListType
from typing import Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# === CONFIG ===
REPO = "neovim/neovim"
INSTALL_DIR = Path.home() / ".local" / "nvim"
BIN_DIR = Path.home() / ".local" / "bin"
LINK_PATH = BIN_DIR / "nvim"
KEEP_VERSIONS = 2
DRY_RUN = False


# === COLORS ===
class Colors:  # pylint: disable=too-few-public-methods
    """ANSI color codes for terminal output"""

    GREEN = "\033[32m" if sys.stdout.isatty() else ""
    YELLOW = "\033[33m" if sys.stdout.isatty() else ""
    RED = "\033[31m" if sys.stdout.isatty() else ""
    BOLD = "\033[1m" if sys.stdout.isatty() else ""
    RESET = "\033[0m" if sys.stdout.isatty() else ""


def msg(color: str, text: str) -> None:
    """Print colored message"""
    print(f"{color}{text}{Colors.RESET}")


def die(text: str) -> None:
    """Print error and exit"""
    msg(Colors.RED, f"❌ {text}")
    sys.exit(1)


# === NETWORK UTILITIES ===
def fetch_json(url: str, timeout: int = 10) -> Dict[str, Any]:
    """Fetch and parse JSON from URL with error handling"""
    req = Request(url, headers={"User-Agent": "bob.py/1.0"})
    try:
        with urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
            if isinstance(data, list):
                return {"releases": data}
            if isinstance(data, dict):
                return data
            die("Invalid JSON response")
            return {}  # Unreachable but satisfies type checker
    except HTTPError as e:
        die(f"HTTP error {e.code}: {e.reason}")
        return {}  # Unreachable but satisfies type checker
    except URLError as e:
        die(f"Network error: {e.reason}")
        return {}  # Unreachable but satisfies type checker
    except json.JSONDecodeError:
        die("Invalid JSON response")
        return {}  # Unreachable but satisfies type checker


def download_file(url: str, dest: Path, progress: bool = True) -> None:
    """Download file with progress indication"""
    try:
        req = Request(url, headers={"User-Agent": "bob.py/1.0"})
        with urlopen(req, timeout=30) as response:
            total_size = int(response.headers.get("content-length", 0))
            block_size = 8192
            downloaded = 0

            with open(dest, "wb") as f:
                while True:
                    chunk = response.read(block_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

                    if progress and total_size > 0:
                        percent = (downloaded / total_size) * 100
                        print(f"\r⬇️  Downloading: {percent:.1f}%", end="", flush=True)

            if progress:
                print()  # New line after progress

    except (HTTPError, URLError) as e:
        die(f"Download failed: {e}")


# === OS + ARCH DETECTION ===
def get_os_arch() -> Tuple[str, str]:
    """Detect OS and architecture"""
    os_name = platform.system().lower()
    machine = platform.machine().lower()

    arch_map: Dict[str, str] = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "armv7l": "armv7",
    }

    arch = arch_map.get(machine)
    if not arch:
        die(f"Unsupported architecture: {machine}")
        # Add unreachable return to satisfy type checker
        return ("", "")  # Unreachable

    return os_name, arch


# === FETCH RELEASE TAG ===
def fetch_tag(channel: str = "stable") -> str:
    """Fetch latest release tag for given channel"""
    api_url = f"https://api.github.com/repos/{REPO}/releases"
    response = fetch_json(api_url)

    # Handle the response properly - it should be a list or dict with "releases" key
    if isinstance(response, list):
        releases: ListType[Dict[str, Any]] = response
    else:
        releases = response.get("releases", [])

    if channel == "nightly":
        for release in releases:
            if release.get("prerelease", False):
                tag = release.get("tag_name")
                if tag:
                    return str(tag)
    else:
        for release in releases:
            if not release.get("prerelease", False):
                tag = release.get("tag_name")
                if tag:
                    return str(tag)

    die(f"Could not find {channel} release")
    return ""  # Unreachable but satisfies type checker


# === CONSTRUCT DOWNLOAD URL ===
def construct_download_url(tag: str, os_name: str, arch: str) -> str:
    """Build download URL for given version and platform"""
    if os_name == "linux":
        return f"https://github.com/{REPO}/releases/download/{tag}/nvim-linux-{arch}.AppImage"
    if os_name == "darwin":
        return f"https://github.com/{REPO}/releases/download/{tag}/nvim-macos.tar.gz"
    die(f"Unsupported OS: {os_name}")
    return ""  # Unreachable but satisfies type checker


# === INSTALL FUNCTION ===
def install_nvim(channel: str = "stable", specific: str = "") -> None:
    """Install Neovim version"""
    # If no arguments, show list and return
    if not channel or channel == "install":
        list_versions()
        return

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)

    os_name, arch = get_os_arch()

    # Determine tag
    if specific:
        tag = specific
    elif channel == "nightly":
        tag = fetch_tag("nightly")
    else:
        tag = fetch_tag("stable")

    url = construct_download_url(tag, os_name, arch)
    msg(Colors.YELLOW, f"⬇️  Downloading Neovim {tag} ...")

    if DRY_RUN:
        msg(Colors.YELLOW, f"[DRY RUN] Would download: {url}")
        return

    # Download to temp file
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        download_file(url, tmp_path)

        # Install based on OS
        if os_name == "darwin":
            install_path = INSTALL_DIR / tag
            install_path.mkdir(parents=True, exist_ok=True)

            with tarfile.open(tmp_path, "r:gz") as tar:
                tar.extractall(install_path)

            nvim_path = install_path / "bin" / "nvim"
        else:
            nvim_path = INSTALL_DIR / f"{tag}.AppImage"
            shutil.move(str(tmp_path), str(nvim_path))
            nvim_path.chmod(0o755)

        msg(Colors.GREEN, f"✅ Installed {tag} successfully.")

        # Create symlink
        if LINK_PATH.exists() or LINK_PATH.is_symlink():
            LINK_PATH.unlink()
        LINK_PATH.symlink_to(nvim_path)
        msg(Colors.GREEN, f"🔗 Linked {tag} → {LINK_PATH}")

        autoclean()

    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# === USE FUNCTION ===
def use_nvim(req: str) -> None:
    """Switch to a different installed version"""
    if not req:
        msg(Colors.YELLOW, "Usage: bob.py use <version|stable|nightly>")
        return

    # Resolve tag
    if req in ["nightly", "stable"]:
        tag = fetch_tag(req)
    else:
        tag = req

    # Find installed version
    candidates = list(INSTALL_DIR.glob(f"{tag}*"))
    if not candidates:
        die(f"Version {tag} not installed. Run 'install {tag}' first.")

    target = candidates[0]
    nvim_bin = target / "bin" / "nvim" if target.is_dir() else target

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    if LINK_PATH.exists() or LINK_PATH.is_symlink():
        LINK_PATH.unlink()
    LINK_PATH.symlink_to(nvim_bin)

    msg(Colors.GREEN, f"✅ Using {tag}")

    # Show version
    try:
        result = subprocess.run(
            [str(LINK_PATH), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        print(result.stdout.split("\n")[0])
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


# === UNINSTALL FUNCTION ===
def uninstall_nvim(tag: str = "") -> None:
    """Remove installed version(s)"""
    if not tag:
        msg(Colors.YELLOW, "Installed versions:")
        if INSTALL_DIR.exists():
            for item in sorted(INSTALL_DIR.iterdir()):
                print(f"  {item.name}")
        else:
            msg(Colors.YELLOW, "None found.")
        msg(Colors.YELLOW, "\nUsage: bob.py uninstall <version|--all>")
        return

    if tag == "--all":
        confirm = input("⚠️  Remove ALL installed Neovim versions (y/N)? ")
        if confirm.lower() != "y":
            msg(Colors.YELLOW, "Aborted.")
            return

        if INSTALL_DIR.exists():
            shutil.rmtree(INSTALL_DIR)
        if LINK_PATH.exists() or LINK_PATH.is_symlink():
            LINK_PATH.unlink()
        msg(Colors.GREEN, "✅ All versions removed.")
        return

    # Find and remove specific version
    candidates = list(INSTALL_DIR.glob(f"{tag}*"))
    if not candidates:
        die(f"Version '{tag}' not found in {INSTALL_DIR}")

    target = candidates[0]
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

    # Remove symlink if it points to removed version
    if LINK_PATH.is_symlink():
        try:
            if LINK_PATH.resolve() == target.resolve():
                LINK_PATH.unlink()
        except (OSError, RuntimeError):
            pass

    msg(Colors.GREEN, f"✅ Uninstalled {tag}.")


# === AUTO-CLEAN FUNCTION ===
def autoclean() -> None:
    """Remove old versions, keeping only KEEP_VERSIONS most recent"""
    if not INSTALL_DIR.exists():
        return

    versions = sorted(
        INSTALL_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
    )

    if len(versions) <= KEEP_VERSIONS:
        return

    msg(Colors.YELLOW, f"🧹 Cleaning old versions (keeping {KEEP_VERSIONS})...")
    for old_version in versions[KEEP_VERSIONS:]:
        if old_version.is_dir():
            shutil.rmtree(old_version)
        else:
            old_version.unlink()
        msg(Colors.RED, f"🗑️  Removed old version: {old_version.name}")


# === CHECK CURRENT ===
def check_nvim() -> None:
    """Show currently active Neovim version"""
    if LINK_PATH.is_symlink():
        msg(Colors.GREEN, "🔍 Current Neovim:")
        print(f"  {LINK_PATH.resolve()}")
        try:
            result = subprocess.run(
                [str(LINK_PATH), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for line in result.stdout.split("\n")[:3]:
                if line.strip():
                    print(f"  {line}")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    else:
        msg(Colors.YELLOW, "⚠️  No active Neovim version.")


# === LIST VERSIONS ===
def list_versions() -> None:
    """List installed and available versions"""
    msg(Colors.BOLD, "Installed:")
    if INSTALL_DIR.exists():
        for item in sorted(INSTALL_DIR.iterdir()):
            print(f"  {item.name}")
    else:
        msg(Colors.YELLOW, "  None installed.")

    msg(Colors.BOLD, "\nAvailable (GitHub):")
    try:
        stable = fetch_tag("stable")
        nightly = fetch_tag("nightly")
        print(f"  Stable:  {stable}")
        print(f"  Nightly: {nightly}")
    except SystemExit:
        msg(Colors.RED, "  Could not fetch available versions")


# === UPDATE FUNCTION ===
def update_nvim(channel: str = "stable") -> None:
    """Update to the latest version of the specified channel"""
    msg(Colors.YELLOW, f"🔄 Checking for updates ({channel})...")

    # Fetch latest tag
    if channel == "nightly":
        latest_tag = fetch_tag("nightly")
    else:
        latest_tag = fetch_tag("stable")

    # Check if already installed
    if INSTALL_DIR.exists():
        installed = [item.name for item in INSTALL_DIR.iterdir()]
        # Check if this version already exists
        already_installed = any(latest_tag in name for name in installed)

        if already_installed:
            msg(Colors.GREEN, f"✅ Already on latest {channel} version: {latest_tag}")
            # Still switch to it in case we're on a different version
            use_nvim(latest_tag)
            return

    msg(Colors.YELLOW, f"📦 New version available: {latest_tag}")
    install_nvim(channel)


# === HELP MENU ===
def help_menu() -> None:
    """Display help information"""
    print("""Usage: bob.py [command] [args]

Commands:
  install [stable|nightly|version]  Install and verify a version (no args shows list)
  update [stable|nightly]           Update to latest version (default: stable)
  use <version|stable|nightly>      Activate an installed version
  uninstall <version|--all>         Remove one or all versions
  check                             Show the active Neovim version
  list                              List installed and available versions
  help                              Show this help message
""")


# === MAIN ===
def main() -> None:
    """Main entry point"""
    args = sys.argv[1:]

    if not args or args[0] == "help":
        help_menu()
        return

    cmd = args[0]

    if cmd == "install":
        channel = args[1] if len(args) > 1 else ""
        specific = args[2] if len(args) > 2 else ""
        install_nvim(channel, specific)
    elif cmd == "update":
        channel = args[1] if len(args) > 1 else "stable"
        update_nvim(channel)
    elif cmd == "use":
        req = args[1] if len(args) > 1 else ""
        use_nvim(req)
    elif cmd == "uninstall":
        tag = args[1] if len(args) > 1 else ""
        uninstall_nvim(tag)
    elif cmd == "check":
        check_nvim()
    elif cmd == "list":
        list_versions()
    else:
        die(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
