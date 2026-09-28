#!/bin/bash
# Spotifyとの連携をやり直すためのファイルです。ダブルクリックで実行します。
cd "$(dirname "$0")"
python3 scripts/auth.py
echo ""
read -n 1 -s -r -p "（何かキーを押すとこの画面を閉じます）"
echo ""
osascript -e 'tell application "Terminal" to close front window' >/dev/null 2>&1 &
