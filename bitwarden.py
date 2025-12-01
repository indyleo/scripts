#!/usr/bin/env python3
"""
Optimized dmenu Bitwarden password manager with caching
"""
import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from time import sleep, time
from typing import List, Optional


@dataclass
class BitwardenItem:
    """Represents a Bitwarden vault item"""
    id: str
    name: str
    username: str

    def display(self) -> str:
        """Return display format for dmenu"""
        return f"{self.name} [{self.username}]"


class BitwardenManager:
    """Manages Bitwarden vault interactions via dmenu"""
    CACHE_TIME = 86400  # 24 hours

    def __init__(self):
        self.dmenu_args = ['-l', '15', '-p', 'Bitwarden:']

        # Setup cache directory
        cache_home = os.getenv('XDG_CACHE_HOME', str(Path.home() / '.cache'))
        self.cache_dir = Path(cache_home) / 'dmenu_pass'
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.items_cache = self.cache_dir / 'items.json'
        self.session_file = self.cache_dir / 'session.token'

        # Detect clipboard tool
        self.copy_cmd = self._detect_clipboard()

        # Verify dependencies
        for cmd in ['bw', 'jq', 'dmenu', 'xdotool']:
            if not self._command_exists(cmd):
                print(f"Missing dependency: {cmd}", file=sys.stderr)
                sys.exit(1)

        self.session: Optional[str] = None
        self._items_cache: Optional[List[BitwardenItem]] = None

    @staticmethod
    def _command_exists(cmd: str) -> bool:
        """Check if a command exists in PATH"""
        return subprocess.run(
            ['which', cmd],
            capture_output=True,
            check=False
        ).returncode == 0

    def _detect_clipboard(self) -> List[str]:
        """Detect available clipboard tool"""
        if subprocess.run(['which', 'xclip'], capture_output=True, check=False).returncode == 0:
            return ['xclip', '-selection', 'clipboard']
        if subprocess.run(['which', 'wl-copy'], capture_output=True, check=False).returncode == 0:
            return ['wl-copy']
        return ['cat']

    def _dmenu(self, items: List[str], prompt: Optional[str] = None) -> Optional[str]:
        """Run dmenu with given items"""
        args = ['dmenu'] + self.dmenu_args
        if prompt:
            args[-1] = f"Bitwarden: {prompt}"

        try:
            result = subprocess.run(
                args,
                input='\n'.join(items),
                capture_output=True,
                text=True,
                check=False
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except FileNotFoundError:
            print("Error: dmenu not found", file=sys.stderr)
            sys.exit(1)

    def _notify(self, message: str) -> None:
        """Send desktop notification"""
        subprocess.run(['notify-send', 'Bitwarden', message], check=False)

    def _run_bw(self, args: List[str]) -> str:
        """Run bw command with session"""
        if self.session is None:
            raise RuntimeError("No active session")

        cmd = ['bw'] + args + ['--session', self.session]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"bw command failed: {result.stderr}")
        return result.stdout

    def _cache_expired(self, filepath: Path) -> bool:
        """Check if cache file is expired"""
        if not filepath.exists():
            return True
        mtime = filepath.stat().st_mtime
        return (time() - mtime) > self.CACHE_TIME

    def _ensure_login(self) -> None:
        """Ensure user is logged in to Bitwarden"""
        check = subprocess.run(
            ['bw', 'login', '--check'],
            capture_output=True,
            check=False
        )
        if check.returncode != 0:
            login = subprocess.run(['bw', 'login', '--apikey'], check=False)
            if login.returncode != 0:
                print("Bitwarden login failed", file=sys.stderr)
                sys.exit(1)

    def _unlock_session(self) -> None:
        """Unlock vault and get session token"""
        # Try loading existing session
        if self.session_file.exists():
            self.session = self.session_file.read_text(encoding='utf-8').strip()
            check = subprocess.run(
                ['bw', 'unlock', '--check', '--session', self.session],
                capture_output=True,
                check=False
            )
            if check.returncode == 0:
                return

        # Need to unlock
        if 'BW_PASSWORD' in os.environ:
            result = subprocess.run(
                ['bw', 'unlock', '--raw', '--passwordenv', 'BW_PASSWORD'],
                capture_output=True,
                text=True,
                check=False
            )
        else:
            result = subprocess.run(
                ['bw', 'unlock', '--raw'],
                capture_output=True,
                text=True,
                check=False
            )

        if result.returncode != 0 or not result.stdout.strip():
            print("Vault unlock failed", file=sys.stderr)
            sys.exit(1)

        self.session = result.stdout.strip()
        self.session_file.write_text(self.session, encoding='utf-8')

    def _load_items(self, force_reload: bool = False) -> List[BitwardenItem]:
        """Load Bitwarden items with caching"""
        if self._items_cache is not None and not force_reload:
            return self._items_cache

        if not force_reload and not self._cache_expired(self.items_cache):
            try:
                with open(self.items_cache, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._items_cache = [
                        BitwardenItem(
                            id=item['id'],
                            name=item['name'],
                            username=item.get('login', {}).get('username', 'no-username')
                        )
                        for item in data
                    ]
                    return self._items_cache
            except (json.JSONDecodeError, KeyError):
                pass

        # Refresh cache
        items_json = self._run_bw(['list', 'items'])
        self.items_cache.write_text(items_json, encoding='utf-8')

        data = json.loads(items_json)
        self._items_cache = [
            BitwardenItem(
                id=item['id'],
                name=item['name'],
                username=item.get('login', {}).get('username', 'no-username')
            )
            for item in data
        ]
        return self._items_cache

    def _refresh_cache_async(self) -> None:
        """Refresh cache in background if needed"""
        if self._cache_expired(self.items_cache):
            thread = threading.Thread(target=self._load_items, args=(True,), daemon=True)
            thread.start()

    def _get_password(self, item_id: str) -> str:
        """Get password for item"""
        return self._run_bw(['get', 'password', item_id]).strip()

    def _copy_to_clipboard(self, text: str, clear_after: int = 30) -> None:
        """Copy text to clipboard and optionally clear after delay"""
        subprocess.run(self.copy_cmd, input=text, text=True, check=False)

        if clear_after > 0:
            def clear_clipboard() -> None:
                """Clear clipboard after delay"""
                sleep(clear_after)
                subprocess.run(self.copy_cmd, input='', text=True, check=False)

            thread = threading.Thread(target=clear_clipboard, daemon=True)
            thread.start()

    def _auto_type(self, text: str) -> None:
        """Auto-type text using xdotool"""
        subprocess.run(
            ['xdotool', 'type', '--delay', '50', '--clearmodifiers', text],
            check=False
        )

    def add_login(self) -> None:
        """Add new login item"""
        name = self._dmenu([], "New Entry Name:")
        if not name:
            return

        username = self._dmenu([], "Username:")
        password = self._dmenu([], "Password:")

        item = {
            'type': 'login',
            'name': name,
            'login': {
                'username': username,
                'password': password
            }
        }

        subprocess.run(
            ['bw', 'create', 'item', '--session', self.session],
            input=json.dumps(item),
            text=True,
            capture_output=True,
            check=False
        )

        self._notify(f"Item '{name}' added")
        self.items_cache.unlink(missing_ok=True)
        self._items_cache = None

    def delete_login(self) -> None:
        """Delete a login item"""
        items = self._load_items()
        displays = [item.display() for item in items]

        selection = self._dmenu(displays)
        if not selection:
            return

        item = next((i for i in items if i.display() == selection), None)
        if not item:
            return

        self._run_bw(['delete', 'item', item.id, '--quiet'])
        self._notify(f"Deleted '{item.name}'")

        self.items_cache.unlink(missing_ok=True)
        self._items_cache = None

    def view_details(self) -> None:
        """View item details"""
        items = self._load_items()
        displays = [item.display() for item in items]

        selection = self._dmenu(displays)
        if not selection:
            return

        item = next((i for i in items if i.display() == selection), None)
        if not item:
            return

        details = self._run_bw(['get', 'item', item.id])
        formatted = subprocess.run(
            ['jq', '.'],
            input=details,
            text=True,
            capture_output=True,
            check=False
        ).stdout

        subprocess.run(['less'], input=formatted, text=True, check=False)

    def handle_action(self) -> None:
        """Handle main password action"""
        items = self._load_items()
        displays = [item.display() for item in items]

        selection = self._dmenu(displays)
        if not selection:
            return

        item = next((i for i in items if i.display() == selection), None)
        if not item:
            return

        password = self._get_password(item.id)

        actions = [
            "Auto-Type Username",
            "Auto-Type Password",
            "Copy Username",
            "Copy Password"
        ]

        action = self._dmenu(actions, f"{item.name} [{item.username}]")
        if not action:
            return

        if action == "Auto-Type Username":
            self._notify(f"Typing username for '{item.name}'...")
            self._auto_type(item.username)
        elif action == "Auto-Type Password":
            self._notify(f"Typing password for '{item.name}'...")
            self._auto_type(password)
        elif action == "Copy Username":
            self._copy_to_clipboard(item.username)
            self._notify("Username copied to clipboard")
        elif action == "Copy Password":
            self._copy_to_clipboard(password)
            self._notify("Password copied to clipboard")

    def run(self) -> None:
        """Main entry point"""
        self._ensure_login()
        self._unlock_session()

        # Async cache refresh
        self._refresh_cache_async()

        # Ensure cache exists for menu
        self._load_items()

        # Main menu
        items = self._load_items()
        menu_items = [item.display() for item in items]
        menu_items.extend(["", "[+] Add New Login", "[-] Delete Login", "[🔍] View Details"])

        selection = self._dmenu(menu_items)
        if not selection:
            return

        if selection == "[+] Add New Login":
            self.add_login()
        elif selection == "[-] Delete Login":
            self.delete_login()
        elif selection == "[🔍] View Details":
            self.view_details()
        else:
            # Restore selection for action handling
            self.handle_action()


if __name__ == "__main__":
    manager = BitwardenManager()
    manager.run()
