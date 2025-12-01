#!/usr/bin/env python3
"""Interactive Git TUI using fzf for browsing and managing git repositories."""
import re
import signal
import subprocess
import sys
from typing import List, Optional


# ------------------------------------------------------------
# Signal handling for clean exit
# ------------------------------------------------------------
def signal_handler(_sig, _frame):
    """Handle Ctrl+C gracefully."""
    print("\n\nExiting...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# ------------------------------------------------------------
# Utilities
# ------------------------------------------------------------
def run(cmd: List[str], check: bool = True) -> str:
    """Run a git command and return its output."""
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE).strip()
    except subprocess.CalledProcessError as e:
        if check:
            print(f"Error: {e.stderr}")
            sys.exit(1)
        return ""

def run_interactive(cmd: List[str]) -> int:
    """Run a command interactively and return exit code."""
    return subprocess.run(cmd, check=False).returncode

def fzf_with_preview(source: List[str], preview_cmd: str, multi: bool = False) -> Optional[str]:
    """Launch fzf with a preview command and return the selected line(s)."""
    try:
        args = ["fzf", "--ansi", "--preview", preview_cmd,
                "--preview-window=right:60%", "--bind=q:abort"]
        if multi:
            args.append("--multi")

        with subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as proc:
            out, _ = proc.communicate("\n".join(source))
            return out.strip() or None
    except FileNotFoundError:
        print("fzf not found")
        return None

def prompt_input(message: str) -> str:
    """Prompt user for input."""
    try:
        print(f"\n{message}")
        return input("> ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled")
        return ""

def confirm(message: str) -> bool:
    """Ask for y/n confirmation."""
    try:
        response = input(f"{message} (y/N): ").strip().lower()
        return response in ('y', 'yes')
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled")
        return False

# ------------------------------------------------------------
# Parsers
# ------------------------------------------------------------
HASH_RE = re.compile(r"^[0-9a-f]{7,40}")

def extract_hash(line: str) -> Optional[str]:
    """Extract commit hash from a git log --graph line"""
    # Remove ANSI color codes
    clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
    # Remove graph characters and whitespace
    clean = re.sub(r'^[\s|*/\\_.]+', '', clean)
    # Now try to match hash at the start
    m = HASH_RE.match(clean.strip())
    return m.group(0) if m else None

def extract_reflog_hash(line: str) -> Optional[str]:
    """Extract commit hash from reflog output."""
    parts = line.split()
    if parts and HASH_RE.fullmatch(parts[0]):
        return parts[0]
    return None

def extract_filepath(line: str) -> Optional[str]:
    """Extract filepath from git status --porcelain output."""
    line = line.rstrip()
    if len(line) < 4:
        return None
    status = line[:2]
    rest = line[3:].strip()
    if status.startswith("R"):  # rename entry
        parts = rest.split()
        return parts[-1] if parts else None
    return rest

def get_file_status(line: str) -> str:
    """Get the status code from porcelain output."""
    return line[:2] if len(line) >= 2 else ""

# ------------------------------------------------------------
# Read-only Views
# ------------------------------------------------------------
def view_commits() -> None:
    """Browse git commits with interactive preview."""
    raw = run(["git", "log", "--oneline", "--graph", "--color=always", "--all"])
    lines = raw.splitlines()

    preview = (
        "echo {} | "
        "sed 's/\\x1b\\[[0-9;]*m//g' | "
        "sed 's/^[[:space:]|*/\\\\_.]*//g' | "
        "awk '{print $1}' | "
        "xargs -r git show --color"
    )

    selection = fzf_with_preview(lines, preview)
    if not selection:
        return
    commit = extract_hash(selection)
    if commit:
        run_interactive(["git", "show", "--color", commit])

def view_reflog() -> None:
    """Browse git reflog with interactive preview."""
    lines = run(["git", "reflog"], check=False).splitlines()
    if not lines:
        print("Reflog is empty")
        return

    selection = fzf_with_preview(
        lines, "echo {} | awk '{print $1}' | xargs -r git show --color"
    )
    if not selection:
        return
    commit = extract_reflog_hash(selection)
    if commit:
        run_interactive(["git", "show", "--color", commit])

def view_file_blame() -> None:
    """Browse files and show git blame."""
    files = run(["git", "ls-files"], check=False).splitlines()
    if not files:
        print("No tracked files")
        return
    selection = fzf_with_preview(files, "git blame --color-by-age -- {}")
    if selection:
        run_interactive(["git", "blame", "--color-by-age", "--", selection])

# ------------------------------------------------------------
# File Staging & Status
# ------------------------------------------------------------
def manage_status() -> None:
    """Interactive staging/unstaging of files."""
    while True:
        lines = run(["git", "status", "--porcelain"], check=False).splitlines()
        if not lines:
            print("Working tree clean")
            return

        preview = "echo {} | awk '{print substr($0, 4)}' | xargs -r git diff --color HEAD --"

        print("\nControls: s=stage, u=unstage, c=commit, q=quit")
        selection = fzf_with_preview(lines, preview, multi=True)

        if not selection:
            return

        selected_files = []
        for sel_line in selection.split('\n'):
            filepath = extract_filepath(sel_line)
            if filepath:
                selected_files.append((sel_line, filepath))

        if not selected_files:
            continue

        # Determine action based on file status
        action = prompt_input("Action: [s]tage, [u]nstage, [c]ommit, or [q]uit")

        if action == 'q':
            return
        if action == 'c':
            commit_changes()
            return
        if action == 's':
            for _, filepath in selected_files:
                run(["git", "add", filepath])
                print(f"Staged: {filepath}")
        if action == 'u':
            for _, filepath in selected_files:
                run(["git", "reset", "HEAD", filepath], check=False)
                print(f"Unstaged: {filepath}")

# ------------------------------------------------------------
# Commit Operations
# ------------------------------------------------------------
def commit_changes() -> None:
    """Create a commit with staged changes."""
    # Check if there are staged changes
    staged = run(["git", "diff", "--cached", "--name-only"], check=False)
    if not staged:
        print("No staged changes to commit")
        return

    print("\nStaged files:")
    print(staged)

    # Conventional commit types
    commit_types = [
        "feat: ✨ A new feature",
        "fix: 🐛 A bug fix",
        "docs: 📚 Documentation only changes",
        "style: 💎 Code style changes (formatting, etc)",
        "refactor: ♻️  Code refactoring",
        "perf: ⚡ Performance improvements",
        "test: 🧪 Adding or updating tests",
        "build: 🏗️  Build system or dependencies",
        "ci: 🤖 CI configuration changes",
        "chore: 🔧 Other changes (maintenance)",
        "revert: ⏮️  Revert a previous commit",
        "---",
        "custom: ✏️  Custom message (no prefix)",
    ]

    type_choice = fzf_with_preview(commit_types, "echo {}")
    if not type_choice:
        print("Commit cancelled")
        return

    if type_choice == "---":
        print("Commit cancelled")
        return

    # Extract the prefix
    prefix = ""
    if "custom:" not in type_choice:
        prefix = type_choice.split(":")[0] + ": "

    message = prompt_input(f"\nCommit message ({prefix}...):")
    if not message:
        print("Commit cancelled")
        return

    full_message = prefix + message

    result = run_interactive(["git", "commit", "-m", full_message])
    if result == 0:
        print(f"✓ Commit created: {full_message}")

        # Check if there's a remote configured
        remotes = run(["git", "remote"], check=False)
        if remotes:
            try:
                response = input("\nPush to remote? (Y/n): ").strip().lower()
                should_push = response in ('', 'y', 'yes')
            except (KeyboardInterrupt, EOFError):
                print("\nSkipping push")
                should_push = False

            if should_push:
                push_changes()

def amend_commit() -> None:
    """Amend the last commit."""
    if not confirm("Amend the last commit?"):
        return

    choice = prompt_input("Keep message? [y]es, [n]ew message, [e]dit")

    if choice == 'y':
        run_interactive(["git", "commit", "--amend", "--no-edit"])
    elif choice == 'e':
        run_interactive(["git", "commit", "--amend"])
    elif choice == 'n':
        message = prompt_input("New commit message:")
        if message:
            run_interactive(["git", "commit", "--amend", "-m", message])

    print("✓ Commit amended")

# ------------------------------------------------------------
# Remote Operations
# ------------------------------------------------------------
def push_changes() -> None:
    """Push commits to remote."""
    branch = run(["git", "branch", "--show-current"], check=False)
    if not branch:
        print("Not on a branch")
        return

    print(f"Current branch: {branch}")

    # Check if upstream is set
    upstream = run(["git", "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], check=False)

    if not upstream:
        if confirm(f"No upstream set. Push and set upstream for '{branch}'?"):
            run_interactive(["git", "push", "-u", "origin", branch])
    else:
        if confirm(f"Push to {upstream}?"):
            force = confirm("Force push? (WARNING: destructive)")
            if force:
                run_interactive(["git", "push", "--force-with-lease"])
            else:
                run_interactive(["git", "push"])

def pull_changes() -> None:
    """Pull changes from remote."""
    branch = run(["git", "branch", "--show-current"], check=False)
    if not branch:
        print("Not on a branch")
        return

    rebase = confirm("Use rebase instead of merge?")

    if rebase:
        run_interactive(["git", "pull", "--rebase"])
    else:
        run_interactive(["git", "pull"])

def fetch_changes() -> None:
    """Fetch from all remotes."""
    if confirm("Fetch from all remotes?"):
        run_interactive(["git", "fetch", "--all", "--prune"])
        print("✓ Fetch complete")

# ------------------------------------------------------------
# Stash Operations
# ------------------------------------------------------------
def manage_stashes() -> None:
    """Browse and manage stashes."""
    stashes = run(["git", "stash", "list"], check=False).splitlines()
    if not stashes:
        print("No stashes")
        return

    # Add action menu
    stashes_with_menu = stashes + ["", "[n] New stash"]

    preview = "echo {} | awk '{print $1}' | sed 's/:$//' | xargs -r git stash show -p --color"
    selection = fzf_with_preview(stashes_with_menu, preview)

    if not selection:
        return

    if selection.startswith("[n] New stash"):
        create_stash()
        return

    # Extract stash index
    match = re.match(r'stash@\{(\d+)\}', selection)
    if not match:
        return

    stash_ref = f"stash@{{{match.group(1)}}}"

    action = prompt_input(f"Action for {stash_ref}: [a]pply, [p]op, [d]rop, [s]how")

    if action == 'a':
        run_interactive(["git", "stash", "apply", stash_ref])
    elif action == 'p':
        run_interactive(["git", "stash", "pop", stash_ref])
    elif action == 'd':
        if confirm(f"Drop {stash_ref}?"):
            run(["git", "stash", "drop", stash_ref])
            print("✓ Stash dropped")
    elif action == 's':
        run_interactive(["git", "stash", "show", "-p", stash_ref])

def create_stash() -> None:
    """Create a new stash."""
    message = prompt_input("Stash message (optional):")

    include_untracked = confirm("Include untracked files?")

    cmd = ["git", "stash", "push"]
    if include_untracked:
        cmd.append("-u")
    if message:
        cmd.extend(["-m", message])

    result = run_interactive(cmd)
    if result == 0:
        print("✓ Stash created")

# ------------------------------------------------------------
# Branch Operations
# ------------------------------------------------------------
def manage_branches() -> None:
    """Browse and manage branches."""
    lines = run(["git", "branch", "--all", "--color=always"], check=False).splitlines()

    # Add action menu
    lines_with_menu = lines + ["", "[n] New branch", "[d] Delete branch"]

    preview = (
        "echo {} | "
        "sed 's/\\x1b\\[[0-9;]*m//g' | "
        "sed 's/^[* ]*//' | "
        "sed 's|^remotes/origin/||' | "
        "xargs -r git log --oneline --graph --color -20"
    )

    sel = fzf_with_preview(lines_with_menu, preview)
    if not sel:
        return

    if sel.startswith("[n] New branch"):
        create_branch()
        return

    if sel.startswith("[d] Delete branch"):
        delete_branch()
        return

    # Clean up branch name
    branch = re.sub(r'\x1b\[[0-9;]*m', '', sel).strip().lstrip("* ")
    if branch.startswith("remotes/origin/"):
        branch = branch.replace("remotes/origin/", "")

    if confirm(f"Checkout '{branch}'?"):
        run_interactive(["git", "checkout", branch])

def create_branch() -> None:
    """Create a new branch."""
    name = prompt_input("New branch name:")
    if not name:
        return

    checkout = confirm("Checkout after creation?")

    if checkout:
        run_interactive(["git", "checkout", "-b", name])
    else:
        run(["git", "branch", name])
        print(f"✓ Branch '{name}' created")

def delete_branch() -> None:
    """Delete a branch."""
    branches = run(["git", "branch", "--format=%(refname:short)"], check=False).splitlines()
    if not branches:
        print("No branches to delete")
        return

    selection = fzf_with_preview(branches, "git log --oneline --graph --color -20 {}")
    if not selection:
        return

    if not confirm(f"Delete branch '{selection}'?"):
        return

    force = confirm("Force delete? (for unmerged branches)")

    flag = "-D" if force else "-d"
    result = run_interactive(["git", "branch", flag, selection])
    if result == 0:
        print(f"✓ Branch '{selection}' deleted")

# ------------------------------------------------------------
# Reset & Revert Operations
# ------------------------------------------------------------
def reset_operations() -> None:
    """Perform reset operations."""
    lines = run(["git", "log", "--oneline", "--color=always", "-20"], check=False).splitlines()

    preview = "echo {} | awk '{print $1}' | xargs -r git show --color"
    selection = fzf_with_preview(lines, preview)

    if not selection:
        return

    commit = extract_hash(selection)
    if not commit:
        return

    print(f"\nReset to {commit}:")
    print("  [soft]  Keep changes staged")
    print("  [mixed] Keep changes unstaged (default)")
    print("  [hard]  DISCARD all changes (WARNING: destructive)")

    mode = prompt_input("Reset mode:")

    if mode not in ('soft', 'mixed', 'hard'):
        print("Invalid mode")
        return

    if mode == 'hard' and not confirm("⚠️  HARD RESET will destroy uncommitted work. Continue?"):
        return

    run_interactive(["git", "reset", f"--{mode}", commit])
    print(f"✓ Reset to {commit} ({mode})")

def revert_commit() -> None:
    """Revert a commit."""
    lines = run(["git", "log", "--oneline", "--color=always", "-20"], check=False).splitlines()

    preview = "echo {} | awk '{print $1}' | xargs -r git show --color"
    selection = fzf_with_preview(lines, preview)

    if not selection:
        return

    commit = extract_hash(selection)
    if not commit:
        return

    if confirm(f"Create revert commit for {commit}?"):
        run_interactive(["git", "revert", commit])

# ------------------------------------------------------------
# Dispatcher
# ------------------------------------------------------------
def dispatch_action(choice: str) -> None:
    """Dispatch menu choice to appropriate function."""
    if choice == "commit":
        commit_changes()
    elif choice == "amend commit":
        amend_commit()
    elif choice == "status  stage":
        manage_status()
    elif choice == "view commits":
        view_commits()
    elif choice == "reflog":
        view_reflog()
    elif choice == "blame":
        view_file_blame()
    elif choice == "manage branches":
        manage_branches()
    elif choice == "stash":
        manage_stashes()
    elif choice == "push":
        push_changes()
    elif choice == "pull":
        pull_changes()
    elif choice == "fetch":
        fetch_changes()
    elif choice == "reset":
        reset_operations()
    elif choice == "revert":
        revert_commit()

def main() -> None:
    """Main entry point - display menu and dispatch to selected view."""
    # Auto-stage all changes on launch
    try:
        run(["git", "add", "."], check=False)
    except subprocess.CalledProcessError:
        pass  # Ignore errors if not in a git repo

    menu = [
        "📝 commit",
        "📌 amend commit",
        "---",
        "📋 status & stage",
        "🔍 view commits",
        "🔄 reflog",
        "👤 blame",
        "---",
        "🌿 manage branches",
        "💾 stash",
        "---",
        "⬆️  push",
        "⬇️  pull",
        "🔄 fetch",
        "---",
        "↩️  reset",
        "⏪ revert",
    ]

    try:
        choice = fzf_with_preview(menu, "echo {}")

        if not choice or choice == "---":
            return

        # Strip emoji and whitespace
        choice = re.sub(r'[^\w\s]', '', choice).strip()

        dispatch_action(choice)
    except KeyboardInterrupt:
        print("\n\nExiting...")
        sys.exit(0)

if __name__ == "__main__":
    main()
