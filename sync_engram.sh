#!/usr/bin/env bash
set -euo pipefail

SERVER="ubuntu@42.193.231.57"
DEST="/opt/1panel/apps/astrbot/astrbot/data/plugins/astrbot_plugin_engram/"

echo "== Syncing Engram plugin to server =="

rsync -av --delete \
  -e "ssh -t" \
  --rsync-path="sudo rsync" \
  --exclude ".git" \
  --exclude "__pycache__" \
  --exclude "*.pyc" \
  --exclude ".DS_Store" \
  --exclude ".tests" \
  ./ "$SERVER:$DEST"

echo ""
echo "✅ Sync complete!"
echo "➡️ 现在打开 Astro Bot Web UI，然后点击：插件 -> 重新加载(engram)"
