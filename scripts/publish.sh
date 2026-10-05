#!/bin/bash
# Publish the latest cycle to the website: update needle.json/history.json on
# the live-data branch (checked out at $DATA_DIR) and redeploy GitHub Pages.
set -euo pipefail
DATA_DIR="${DATA_DIR:-live-data}"
python3 scripts/update_site_data.py live_needle_summary_latest.json "$DATA_DIR"
cd "$DATA_DIR"
git add needle.json history.json
if git diff --cached --quiet; then echo "publish: no change"; exit 0; fi
git -c user.name="agulha-bot" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  commit -q -m "data $(date -u +%Y-%m-%dT%H:%M:%SZ)"
for delay in 2 4 8 16; do
  git push -q origin HEAD:live-data && break
  echo "publish: push failed, retrying in ${delay}s"; sleep "$delay"
done
gh workflow run pages.yml --ref main || echo "publish: could not trigger the Pages deploy"
