"""Copy the latest pipeline summary into the site's data dir and append a history point."""
import json
import os
import sys

summary_path, data_dir = sys.argv[1], sys.argv[2]
summary = json.load(open(summary_path))
json.dump(summary, open(os.path.join(data_dir, "needle.json"), "w"), ensure_ascii=False)

history_path = os.path.join(data_dir, "history.json")
history = json.load(open(history_path)) if os.path.exists(history_path) else {"points": []}
points = history.setdefault("points", [])
if summary.get("n_municipios") and (not points or points[-1]["t"] != summary["updated_at"]):
    points.append({
        "t": summary["updated_at"],
        "margin_2022": summary["margin_projected_2022_pp"],
        "margin_2018": summary["margin_projected_2018_pp"],
        "se_2022": summary["margin_se_2022_pp"],
        "se_2018": summary["margin_se_2018_pp"],
    })
    sm = summary.get("section_model") or {}
    if sm.get("status") == "ok":
        points[-1].update(margin_sec=sm["margin_pp"], hw90_sec=sm["margin_hw90_pp"], pt_sec=sm["pt_pct"],
                          pl_sec=sm["pl_pct"], counted=100 * sm["frac_votes_counted"])
json.dump(history, open(history_path, "w"), ensure_ascii=False)
print(f"site data updated: {len(points)} history points")
