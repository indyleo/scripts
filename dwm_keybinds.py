#!/usr/bin/env python3
import os
import re
import subprocess
import tempfile

# Path to your DWM config
CONFIG_PATH = os.path.expanduser("~/Github/suckless/dwm/config.def.h")

# Regular expressions
key_array_re = re.compile(r"^static\s+const\s+Key\s+keys\[\]\s*=\s*{")
keybinding_re = re.compile(
    r"\{\s*([^,]+)\s*,\s*([A-Za-z0-9_]+)\s*,\s*([a-zA-Z0-9_]+)\s*,\s*(.*)\},?"
)
comment_re = re.compile(r"/\*\s*(.*?)\s*\*/")
define_re = re.compile(r"#define\s+([A-Z0-9_]+)\s+([A-Za-z0-9_|]+)")
tagkeys_re = re.compile(r"TAGKEYS\s*\(\s*([A-Za-z0-9_]+)\s*,\s*([0-9]+)\s*\)")


# Functions
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


def parse_config(path):
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


def generate_markdown(groups):
    md = ["# DWM Keybindings Cheat Sheet\n"]
    for group, bindings in groups.items():
        if not bindings:
            continue
        md.append(f"## {group}\n")
        md.append("| Key Combination | Function | Argument |")
        md.append("|-----------------|-----------|-----------|")
        for mods, key, func, arg in bindings:
            combo = f"{mods}+{prettify_key(key)}" if mods else prettify_key(key)
            md.append(f"| `{combo}` | `{func}` | `{arg}` |")
        md.append("")  # blank line
    return "\n".join(md)


def view_markdown(md_content):
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md") as tmp:
        tmp.write(md_content)
        tmp_path = tmp.name

    viewer = None
    for cmd in ["nvim", "glow -p", "moar", "less"]:
        if (
            subprocess.call(
                ["which", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            == 0
        ):
            viewer = cmd
            break

    if viewer:
        subprocess.call([viewer, tmp_path])
    else:
        print(md_content)


if __name__ == "__main__":
    if not os.path.exists(CONFIG_PATH):
        print(f"Error: {CONFIG_PATH} not found.")
        exit(1)

    groups = parse_config(CONFIG_PATH)
    md = generate_markdown(groups)
    view_markdown(md)
