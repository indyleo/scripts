#!/usr/bin/env python3
"""
fzf_git.py

Fully-featured CLI git helper using fzf with live previews.
Features:
- Commit files with interactive staged/unstaged diff toggle
- Multi-file diff preview
- View commits with full diffs (including deleted files)
- Switch branches with preview of last commit
- Blame files (modern Git compatible)
- Reflog checkout
- Safe handling of spaces, status codes, and special Git revisions
"""

import os
import subprocess
import sys
from typing import List

COMMIT_TYPES = ["feat", "fix", "docs", "style", "refactor", "perf", "test", "chore"]


def run_fzf(
    options: List[str],
    prompt: str = "> ",
    multi: bool = False,
    ansi: bool = False,
    preview_cmd: str = "",
    bind: str = "",
) -> List[str]:
    """Run fzf and return selected items as a list."""
    cmd = ["fzf", "--prompt", prompt]
    if multi:
        cmd.append("--multi")
    if ansi:
        cmd.append("--ansi")
    if preview_cmd:
        cmd += ["--preview", preview_cmd, "--preview-window", "up:30%:wrap"]
    if bind:
        cmd += ["--bind", bind]

    with subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    ) as fzf:
        stdout, _ = fzf.communicate("\n".join(options))

    if not stdout.strip():
        return []
    return stdout.strip().splitlines() if multi else [stdout.strip()]


def clean_file_path(f: str) -> str:
    """Remove leading status codes from git status output."""
    parts = f.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else parts[0]


def git_status_files(include_unstaged: bool = True) -> List[str]:
    """Return a list of staged and optionally unstaged git files."""
    result = subprocess.run(
        ["git", "status", "--short"], capture_output=True, text=True, check=True
    )
    files = []
    for line in result.stdout.splitlines():
        status, file = line[:2].strip(), line[3:]
        if include_unstaged or status != "":
            files.append(f"{status} {file}")
    return files


def git_recent_commits(limit: int = 50) -> List[str]:
    """Return the last N git commits."""
    result = subprocess.run(
        ["git", "log", f"--pretty=format:%h %s (%an, %ar)", f"-{limit}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def git_all_branches() -> List[str]:
    """Return a sorted list of all local and remote branches."""
    result = subprocess.run(
        ["git", "branch", "--all"], capture_output=True, text=True, check=True
    )
    branches = [
        b.strip().replace("remotes/", "")
        for b in result.stdout.splitlines()
        if "HEAD" not in b
    ]
    return sorted(set(branches))


def commit_files() -> None:
    """Commit selected files with interactive staged/unstaged diff toggle and multi-file preview."""
    files = git_status_files()
    if not files:
        print("No files to commit.")
        return

    preview_staged = "git diff --staged --color=always -- {1}"
    preview_unstaged = "git diff --color=always -- {1}"
    bind = f"ctrl-t:reload({preview_unstaged})"

    # Multi-file diff preview
    selected = run_fzf(
        files,
        prompt="Select files> ",
        multi=True,
        ansi=True,
        preview_cmd=preview_staged,
        bind=bind,
    )
    if not selected:
        print("No files selected.")
        return

    selected_files = [clean_file_path(f) for f in selected]

    commit_type_list = run_fzf(COMMIT_TYPES, prompt="Commit type> ")
    if not commit_type_list:
        print("No commit type selected.")
        return
    commit_type = commit_type_list[0]

    commit_msg = input("Enter commit message: ").strip()
    if not commit_msg:
        print("Empty commit message. Abort.")
        return

    subprocess.run(["git", "add", "--"] + selected_files, check=True)
    full_commit = f"{commit_type}: {commit_msg}"
    subprocess.run(["git", "commit", "-m", full_commit], check=True)
    print(f"Committed: {full_commit}")

    if input("Push? (y/N): ").lower() == "y":
        subprocess.run(["git", "push"], check=True)
        print("Pushed.")


def view_commits() -> None:
    """View recent commits with full diffs including deleted files."""
    commits = git_recent_commits()
    preview_cmd = "git show --color=always {1}"  # full commit diff
    selected_list = run_fzf(
        commits, prompt="Select commit> ", ansi=True, preview_cmd=preview_cmd
    )
    if not selected_list:
        return

    selected = selected_list[0]
    commit_hash = selected.split()[0]

    action_list = run_fzf(
        ["Checkout commit", "Reset hard", "Show diff", "Show stats"], prompt="Action> "
    )
    if not action_list:
        return
    action = action_list[0]

    if action == "Checkout commit":
        subprocess.run(["git", "checkout", commit_hash], check=True)
    elif action == "Reset hard":
        subprocess.run(["git", "reset", "--hard", commit_hash], check=True)
    elif action == "Show diff":
        subprocess.run(["git", "show", "--color=always", commit_hash], check=True)
    elif action == "Show stats":
        subprocess.run(
            ["git", "show", "--stat", "--color=always", commit_hash], check=True
        )


def switch_branch() -> None:
    """Switch branch with preview of latest commit."""
    branches = git_all_branches()
    preview_cmd = "git log -1 --oneline --color=always {}"
    selected_list = run_fzf(
        branches, prompt="Branch> ", ansi=True, preview_cmd=preview_cmd
    )
    if not selected_list:
        return
    selected = selected_list[0]
    subprocess.run(["git", "checkout", selected], check=True)
    print(f"Switched to branch {selected}")


def blame_file() -> None:
    """Select a file and show git blame safely (modern Git)."""
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    )
    files = result.stdout.splitlines()
    preview_cmd = "git blame --color-lines -- {1}"
    selected_list = run_fzf(files, prompt="File> ", ansi=True, preview_cmd=preview_cmd)
    if not selected_list:
        return
    selected = clean_file_path(selected_list[0])
    subprocess.run(["git", "blame", "--", selected], check=True)


def reflog_checkout() -> None:
    """Checkout a commit from reflog safely."""
    result = subprocess.run(
        ["git", "reflog"], capture_output=True, text=True, check=True
    )
    entries = result.stdout.splitlines()
    preview_cmd = "git show --stat --color=always {1}"
    selected_list = run_fzf(
        entries, prompt="Reflog> ", ansi=True, preview_cmd=preview_cmd
    )
    if not selected_list:
        return
    selected = selected_list[0]
    commit_hash = selected.split()[0]
    subprocess.run(["git", "checkout", commit_hash], check=True)


def main() -> None:
    """Main CLI loop."""
    if not os.path.isdir(".git"):
        print("Not a git repository.")
        sys.exit(1)

    actions = [
        "Commit files",
        "View recent commits",
        "Switch branch",
        "Blame file",
        "Reflog checkout",
        "Exit",
    ]

    while True:
        action_list = run_fzf(actions, prompt="Action> ")
        if not action_list:
            break
        action = action_list[0]

        if action == "Commit files":
            commit_files()
        elif action == "View recent commits":
            view_commits()
        elif action == "Switch branch":
            switch_branch()
        elif action == "Blame file":
            blame_file()
        elif action == "Reflog checkout":
            reflog_checkout()
        elif action == "Exit":
            break


if __name__ == "__main__":
    main()
