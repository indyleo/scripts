#!/bin/sh
# lf-wrapper.sh - file / directory / save picker for GUI apps, backed by lf.
#
# Called by xdg-desktop-portal-termfilechooser as
#   lf-wrapper.sh <multiple> <directory> <save> <path> <out> [debug]
# and the chosen path(s) are read back from <out> (empty file = cancelled).
# Handy to try by hand:  lf-wrapper.sh 0 1 0 ~/Downloads /tmp/out; cat /tmp/out
#
#   open one file      multiple=0 directory=0 save=0
#   open many files    multiple=1 directory=0 save=0
#   choose a directory directory=1
#   save as            save=1   (path = suggested dir/name)
set -eu

multiple=${1:-0}
directory=${2:-0}
save=${3:-0}
path=${4:-}
out=${5:-}
[ -n "$out" ] || { echo "usage: ${0##*/} multiple directory save path out" >&2; exit 2; }

if   [ "$save" = 1 ];      then mode=save
elif [ "$directory" = 1 ]; then mode=dir
elif [ "$multiple" = 1 ];  then mode=files
else                            mode=file
fi

# Start directory, and for save the suggested file name
name=
if [ -d "$path" ]; then
    start=$path
elif [ -n "$path" ]; then
    start=$(dirname -- "$path")
    [ "$mode" != save ] || name=$(basename -- "$path")
else
    start=$HOME
fi
[ -d "$start" ] || start=$HOME

case $mode in
    file)  hint="Pick a file: enter chooses it" ;;
    files) hint="Pick files: space marks, enter or ctrl-s confirms" ;;
    dir)   hint="Pick a directory: ctrl-s chooses the current directory" ;;
    save)  hint="Save: ctrl-s names a file in this directory, enter on a file overwrites it" ;;
esac

: > "$out"
set -f
LF_PICKER_MODE=$mode LF_PICKER_OUT=$out LF_PICKER_NAME=$name \
    exec ${LF_PICKER_TERM:-st -c lf-picker -t lf-picker -e} lf -command "echo $hint" "$start"
