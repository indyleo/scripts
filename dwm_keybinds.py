#!/usr/bin/env python3
import os
import re
import subprocess
import tempfile

# Paths
DWM_CONFIG_PATH = os.path.expanduser("~/Github/suckless/dwm/config.def.h")
SXHKD_CONFIG_PATH = os.path.expanduser("~/.config/sxhkd/sxhkdrc")

# DWM Regular expressions
key_array_re = re.compile(r"^static\s+const\s+Key\s+keys\[\]\s*=\s*{")
keybinding_re = re.compile(
    r"\{\s*([^,]+)\s*,\s*([A-Za-z0-9_]+)\s*,\s*([a-zA-Z0-9_]+)\s*,\s*(.*)\},?"
)
comment_re = re.compile(r"/\*\s*(.*?)\s*\*/")
define_re = re.compile(r"#define\s+([A-Z0-9_]+)\s+([A-Za-z0-9_|]+)")
tagkeys_re = re.compile(r"TAGKEYS\s*\(\s*([A-Za-z0-9_]+)\s*,\s*([0-9]+)\s*\)")


# DWM Functions
def parse_defines(lines):
    """Extract #define mappings to resolve modifier key names."""
    defines = {}
    for line in lines:
        m = define_re.match(line.strip())
        if m:
            key, val = m.groups()
            defines[key] = val
    return defines


def humanize_modifier(val):
    mapping = {
        "Mod1Mask": "Alt",
        "Mod4Mask": "Super",
        "ControlMask": "Ctrl",
        "ShiftMask": "Shift",
    }
    for k, v in mapping.items():
        if k in val:
            return v
    return val


def resolve_modifiers(raw_mods, defines):
    """Resolve macros like MODKEY | SHIFTKEY → Super+Shift"""
    mods = raw_mods
    for name, val in defines.items():
        if name.endswith("KEY") and name in mods:
            mods = mods.replace(name, humanize_modifier(val))
    mods = mods.replace("|", "+").replace(" ", "")
    return mods.strip("+")


def prettify_key(key):
    mapping = {
        "Return": "↵",
        "Tab": "⇥",
        "space": "Space",
        "Escape": "Esc",
        "Left": "←",
        "Right": "→",
        "Up": "↑",
        "Down": "↓",
        "Pause": "⏸",
        "Print": "PrtSc",
    }
    key = key.replace("XK_", "")
    return mapping.get(key, key)


def expand_tagkeys(match, defines):
    """Expand TAGKEYS macros into explicit bindings."""
    key = match.group(1).replace("XK_", "")
    tag = int(match.group(2))
    mod = humanize_modifier(defines.get("MODKEY", "Mod"))
    shift = humanize_modifier(defines.get("SHIFTKEY", "Shift"))
    ctrl = humanize_modifier(defines.get("CTRLKEY", "Ctrl"))

    return [
        (f"{mod}", key, "view", f"{{.ui = 1 << {tag}}}"),
        (f"{mod}+{ctrl}", key, "toggleview", f"{{.ui = 1 << {tag}}}"),
        (f"{mod}+{shift}", key, "tag", f"{{.ui = 1 << {tag}}}"),
        (f"{mod}+{ctrl}+{shift}", key, "toggletag", f"{{.ui = 1 << {tag}}}"),
    ]


def parse_dwm_config(path):
    """Parse DWM config and return grouped keybindings."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    defines = parse_defines(lines)
    in_keys = False
    current_group = "General"
    groups = {current_group: []}

    for line in lines:
        stripped = line.strip()

        # Section headers
        cmt = comment_re.match(stripped)
        if cmt:
            current_group = cmt.group(1).strip()
            if current_group not in groups:
                groups[current_group] = []
            continue

        # Start/end of key array
        if key_array_re.match(stripped):
            in_keys = True
            continue
        if in_keys and stripped.startswith("};"):
            break

        if not in_keys:
            continue

        # TAGKEYS macro expansion
        tmatch = tagkeys_re.search(stripped)
        if tmatch:
            for entry in expand_tagkeys(tmatch, defines):
                groups.setdefault("Tags", []).append(entry)
            continue

        # Normal keybindings
        m = keybinding_re.search(stripped)
        if m:
            mods, key, func, arg = m.groups()
            mods = mods.strip()
            key = key.strip()
            func = func.strip()
            arg = arg.strip()
            resolved_mods = resolve_modifiers(mods, defines) if mods != "0" else ""
            groups[current_group].append((resolved_mods, key, func, arg))

    return groups


# SXHKD Functions
def parse_sxhkd_config(path):
    """Parse SXHKD config and return grouped keybindings."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    groups = {}
    current_group = "General"
    i = 0

    while i < len(lines):
        line = lines[i].rstrip()

        # Skip empty lines
        if not line.strip():
            i += 1
            continue

        # Section headers (lines starting with #)
        if line.startswith("#"):
            # Check if it's a section divider
            if "=" in line or "-" in line:
                i += 1
                continue
            # Extract section name
            section = line.lstrip("#").strip()
            if section and not section.startswith("==="):
                # Skip very short comments that are likely inline
                if len(section) > 3:
                    current_group = section
                    if current_group not in groups:
                        groups[current_group] = []
            i += 1
            continue

        # Parse keybinding (non-comment, non-empty line followed by command)
        if not line.startswith("#") and line.strip():
            keybind = line.strip()
            command = ""

            # Look for the command on the next line(s)
            j = i + 1
            while j < len(lines):
                next_line = lines[j].rstrip()
                # Command lines typically start with whitespace or tab
                if next_line and (
                    next_line.startswith("\t") or next_line.startswith("    ")
                ):
                    command = next_line.strip()
                    i = j  # Move index forward
                    break
                elif not next_line.strip():
                    j += 1
                    continue
                else:
                    break

            if command:
                # Process keybind to normalize format
                keybind = normalize_sxhkd_keybind(keybind)
                groups.setdefault(current_group, []).append((keybind, command))

        i += 1

    return groups


def normalize_sxhkd_keybind(keybind):
    """Normalize SXHKD keybind format for better display."""
    # Handle brace expansions like {c,v} or {Up,Down,m}
    # For display purposes, we'll show them as-is since they represent multiple bindings

    # Capitalize modifier keys
    keybind = keybind.replace("super", "Super")
    keybind = keybind.replace("alt", "Alt")
    keybind = keybind.replace("ctrl", "Ctrl")
    keybind = keybind.replace("shift", "Shift")

    # Prettify special keys
    keybind = keybind.replace("Return", "↵")
    keybind = keybind.replace("Escape", "Esc")
    keybind = keybind.replace("Tab", "⇥")
    keybind = keybind.replace("Print", "PrtSc")

    return keybind


# Markdown Generation
def generate_markdown(dwm_groups, sxhkd_groups):
    md = ["# Keybindings Cheat Sheet\n"]

    # DWM Section
    if dwm_groups:
        md.append("# DWM Keybindings\n")
        for group, bindings in dwm_groups.items():
            if not bindings:
                continue
            md.append(f"## {group}\n")
            md.append("| Key Combination | Function | Argument |")
            md.append("|-----------------|----------|----------|")
            for mods, key, func, arg in bindings:
                combo = f"{mods}+{prettify_key(key)}" if mods else prettify_key(key)
                md.append(f"| `{combo}` | `{func}` | `{arg}` |")
            md.append("")

    # SXHKD Section
    if sxhkd_groups:
        md.append("# SXHKD Keybindings\n")
        for group, bindings in sxhkd_groups.items():
            if not bindings:
                continue
            md.append(f"## {group}\n")
            md.append("| Key Combination | Command |")
            md.append("|-----------------|---------|")
            for keybind, command in bindings:
                md.append(f"| `{keybind}` | `{command}` |")
            md.append("")

    return "\n".join(md)


def view_markdown(md_content):
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md") as tmp:
        tmp.write(md_content)
        tmp_path = tmp.name

    viewer = None
    for cmd in ["nvim", "glow", "moar", "less"]:
        if (
            subprocess.call(
                ["which", cmd.split()[0]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            == 0
        ):
            viewer = cmd
            break

    if viewer:
        if viewer == "glow":
            subprocess.call(["glow", "-p", tmp_path])
        else:
            subprocess.call([viewer, tmp_path])
    else:
        print(md_content)


if __name__ == "__main__":
    dwm_groups = {}
    sxhkd_groups = {}

    # Parse DWM config if it exists
    if os.path.exists(DWM_CONFIG_PATH):
        dwm_groups = parse_dwm_config(DWM_CONFIG_PATH)
    else:
        print(f"Warning: {DWM_CONFIG_PATH} not found, skipping DWM config.")

    # Parse SXHKD config if it exists
    if os.path.exists(SXHKD_CONFIG_PATH):
        sxhkd_groups = parse_sxhkd_config(SXHKD_CONFIG_PATH)
    else:
        print(f"Warning: {SXHKD_CONFIG_PATH} not found, skipping SXHKD config.")

    if not dwm_groups and not sxhkd_groups:
        print("Error: No config files found.")
        exit(1)

    md = generate_markdown(dwm_groups, sxhkd_groups)
    view_markdown(md)
