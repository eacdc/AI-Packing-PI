#!/bin/bash
# Get the latest code from GitHub and restart the station.  Run on the Pi:  bash scripts/update.sh
# Never touches .env, config.local.yaml or data/ (photos, history, touchscreen settings).
set -e
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Files were edited on this Pi, so the update would overwrite them:"
  git status --short --untracked-files=no
  echo "Put Pi-only settings in config.local.yaml instead. To throw these edits away:  git checkout -- ."
  exit 1
fi

before=$(git rev-parse HEAD)
git pull --ff-only
after=$(git rev-parse HEAD)
if [ "$before" = "$after" ]; then
  echo "Already up to date."
  exit 0
fi

echo "== new changes"
git log --oneline "$before..$after"
changed=$(git diff --name-only "$before" "$after")

if echo "$changed" | grep -q "^scripts/install.sh"; then
  echo "== install.sh changed: run it again  (bash scripts/install.sh, add 'packing' on the packing Pi)"
fi

sudo systemctl restart book-inspector
echo "== station restarted"

if echo "$changed" | grep -q "^app/static/"; then
  echo "== the screen pages changed: reboot so the touchscreen loads them  (sudo reboot)"
fi
