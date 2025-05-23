#!/bin/env bash
set -euo pipefail

# If no args, print usage
if [[ $# -eq 0 ]]; then
    echo "Usage: $0 gruvbox|nord"
    exit 1
fi

# Args
THEME=$1
if [[ "$THEME" != "gruvbox" && "$THEME" != "nord" ]]; then
    echo "Usage: $0 gruvbox|nord"
    exit 1
fi

# Vars
HOME_CONFIG="${XDG_CONFIG_HOME:-"$HOME/.config"}"
WALLPAPERS_DIR="$HOME/Pictures/Wallpapers"
SUCKLESS_DIR="$HOME/Github/suckless"
NEOVIM_PLUGIN="$HOME_CONFIG/nvim/lua/plugins"
ALACRITTY_CONFIG="$HOME_CONFIG/alacritty"
DUNST_CONFIG="$HOME_CONFIG/dunst"
XRESOURCES="$HOME"

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
APPS=(dmenu dwm slock st)
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

# Neovim: symlink colourscheme.lua and lualine.lua
ln -sf "${NEOVIM_PLUGIN}/colourscheme.lua_${THEME}" "${NEOVIM_PLUGIN}/colourscheme.lua"
ln -sf "${NEOVIM_PLUGIN}/lualine.lua_${THEME}" "${NEOVIM_PLUGIN}/lualine.lua"

pgrep -x dwm && pkill dwm

echo "Theme switched to $THEME!"
