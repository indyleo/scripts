#!/bin/env bash
set -euo pipefail

# If no args, print usage
if [[ $# -eq 0 ]]; then
    echo "Usage: $0 gruvbox|nord"
    exit 1
fi

# Args
THEME=$1
THEME_FILE="${XDG_CACHE_HOME:-$HOME/.cache}/theme"

# Check if theme is valid
if [[ "$THEME" != "gruvbox" && "$THEME" != "nord" ]]; then
    echo "Usage: $0 gruvbox|nord"
    exit 1
fi

# Check if that theme is already set
if [[ -f "$THEME_FILE" && "$(cat "$THEME_FILE")" == "$THEME" ]]; then
    echo "Theme '$THEME' is already active."
    exit 0
fi

# Vars
HOME_CONFIG="${XDG_CONFIG_HOME:-"$HOME/.config"}"
WALLPAPERS_DIR="$HOME/Pictures/Wallpapers"
SUCKLESS_DIR="$HOME/Github/suckless"
NEOVIM_PLUGIN="$HOME_CONFIG/nvim/lua/plugins"
ALACRITTY_CONFIG="$HOME_CONFIG/alacritty"
DUNST_CONFIG="$HOME_CONFIG/dunst"
QUTEBROWSER_CONFIG="$HOME_CONFIG/qutebrowser"
TMUX_CONFIG="$HOME_CONFIG/tmux"
XRESOURCES="$HOME"
STARTPAGE="$HOME/Github/portfilio/startpage/"

# Kill any existing xwall process
pkill -f 'xwall timexwalr' || true

# Start new xwall process with selected theme, disown it so it runs independently
xwall timexwalr "${WALLPAPERS_DIR}/${THEME}" 900 & disown

# Dunst: symlink the correct config
ln -sf "${DUNST_CONFIG}/dunstrc_${THEME}" "${DUNST_CONFIG}/dunstrc"

# Kill any existing dunst process
pkill -x dunst || true

# Start new dunst process with selected theme, disown it so it runs independently
dunst & disown

# Alacritty: symlink the correct config
ln -sf "${ALACRITTY_CONFIG}/alacritty_${THEME}.toml" "${ALACRITTY_CONFIG}/alacritty.toml"

# Xresources: symlink and reload
ln -sf "${XRESOURCES}/.Xresources_${THEME}" "${XRESOURCES}/.Xresources"
xrdb "${XRESOURCES}/.Xresources"

# Suckless apps to theme
APPS=(dmenu dwm slock st tabbed)
for APP in "${APPS[@]}"; do
    APP_DIR="${SUCKLESS_DIR}/${APP}"
    if [[ -d "$APP_DIR" ]]; then
        echo "Theming $APP..."
        cd "$APP_DIR"
        ln -sf "config_${THEME}.def.h" "config.def.h"
        sudo cp config.def.h config.h
        sudo make clean install
    else
        echo "Warning: $APP directory not found at $APP_DIR"
    fi
done

# Neovim: symlink colourscheme.lua, lualine.lua, and indent-blankline.lua
ln -sf "${NEOVIM_PLUGIN}/colourscheme.lua_${THEME}" "${NEOVIM_PLUGIN}/colourscheme.lua"
ln -sf "${NEOVIM_PLUGIN}/lualine.lua_${THEME}" "${NEOVIM_PLUGIN}/lualine.lua"
ln -sf "${NEOVIM_PLUGIN}/indent-blankline.lua_${THEME}" "${NEOVIM_PLUGIN}/indent-blankline.lua"

# Tmux: symlink the correct config
ln -sf "${TMUX_CONFIG}/tmux_${THEME}.conf" "${TMUX_CONFIG}/tmux.conf"

# Startpage: symlink the correct config
if [[ -d "$STARTPAGE" ]]; then
    echo "Found startpage, Theming startpage..."
    ln -sf "${STARTPAGE}/index_${THEME}.html" "${STARTPAGE}/index.html"
fi

# Qutebrowser: symlink the correct config
ln -sf "${QUTEBROWSER_CONFIG}/config_${THEME}.py" "${QUTEBROWSER_CONFIG}/config.py"

# Reload dwm
pgrep -x dwm && pkill dwm

# Update theme file
echo "$THEME" > "$THEME_FILE"
echo "Theme switched to $THEME!"
