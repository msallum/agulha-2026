#!/bin/bash
# Publish the latest cycle to the website: update needle.json/history.json on
# the data branch (checked out at $DATA_DIR) and, for live-data, redeploy GitHub Pages.
# DATA_BRANCH=live-data-test (election-night.yml input data_branch) keeps test runs off the site.
set -euo pipefail
DATA_DIR="${DATA_DIR:-live-data}"
DATA_BRANCH="${DATA_BRANCH:-live-data}"
# Section-level projection (live_section_model.py): adds "section_model" to the summary. A failure here must not
# stop the município model's numbers from publishing.
python3 live_section_model.py || echo "publish: section model failed, publishing without it"
python3 scripts/update_site_data.py live_needle_summary_latest.json "$DATA_DIR"
cd "$DATA_DIR"
git add needle.json history.json
if git diff --cached --quiet; then echo "publish: no change"; exit 0; fi
git -c user.name="agulha-bot" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  commit -q -m "data $(date -u +%Y-%m-%dT%H:%M:%SZ)"
for delay in 2 4 8 16; do
  git push -q origin "HEAD:$DATA_BRANCH" && break
  echo "publish: push failed, retrying in ${delay}s"; sleep "$delay"
done
if [ "$DATA_BRANCH" = "live-data" ]; then
  gh workflow run pages.yml --ref main || echo "publish: could not trigger the Pages deploy"
else
  echo "publish: pushed to $DATA_BRANCH; site not redeployed"
fi
