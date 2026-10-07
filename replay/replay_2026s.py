"""Replay the 2026 first-round night from TSE's per-seção results, minute by minute against projecao.2026elections.

Usage: python3 replay/replay_2026s.py <data_dir> <competitor.json> [n_boot]
  <data_dir>: secoes_2026_1t.parquet (prep_secao.py on votacao_secao_2026 + detalhe_votacao_secao_2026)
  section_base_2026.csv.gz (repo root): each seção's 2022 polling-place base and 2026 electorate profile
  <competitor.json>: projecao.2026elections.workers.dev/dados/apuracao.json

At each time the competitor published a projection, the counted seções are those TSE had totalized by then
(DT_PRIM_TOT_PARCIAL). Two modes: "agg" -- município totals plus which seções are counted, exactly what the live
pipeline sees; "sec" -- every counted seção's own votes, the upper bound if per-seção results were available live.
Writes <data_dir>/replay_2026s.csv.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import replay as R  # noqa: E402
from section_model import DEMO_COLS, SectionModel, demo_covariates  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    data_dir, comp_path = sys.argv[1], sys.argv[2]
    n_boot = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    k = ["uf", "cd_mun", "zona", "secao"]
    s = pd.read_parquet(os.path.join(data_dir, "secoes_2026_1t.parquet"))
    b = pd.read_csv(os.path.join(REPO, "section_base_2026.csv.gz"))
    s = s.merge(b[k + ["b1_pt", "b1_pl", "b1_outros", "b1_valid", "b1_aptos", "eleitores_perfil", *DEMO_COLS]],
                on=k, how="left")
    s = s.rename(columns={"b1_pt": "b_pt", "b1_pl": "b_pl", "b1_outros": "b_outros"})
    s["valid"] = s.pt + s.pl + s.outros
    s["exp_valid"] = (s.aptos * s.b1_valid / s.b1_aptos.where(s.b1_aptos > 0)).fillna(s.aptos * 0.75)
    s["log_size"] = np.log(s.groupby("cd_mun").aptos.transform("sum"))
    s["totalizado"] = s.totalizado.fillna(pd.Timestamp.max)
    s = s.sort_values("totalizado").reset_index(drop=True)
    s["mun_code"] = pd.factorize(s.cd_mun)[0]
    demo = demo_covariates(s, s[k + ["eleitores_perfil", *DEMO_COLS]].dropna())
    truth_pt, truth_pl = 100 * s.pt.sum() / s.valid.sum(), 100 * s.pl.sum() / s.valid.sum()
    model = SectionModel(s, 1, demo_X=demo)
    times = s.totalizado.to_numpy()

    comp = [p for p in json.load(open(comp_path))["pontos"] if p.get("projecao")]
    rows = []
    for p in comp[::3]:  # every third published point (~1-2 min apart) is plenty
        hh, mm = map(int, p["t"].split(":"))
        if hh < 12:
            continue  # after midnight: the count is long over
        t = np.datetime64(f"2026-10-04T{hh:02d}:{mm:02d}:59")
        n = int(np.searchsorted(times, t, side="right"))
        if n < 200:
            continue
        row = dict(hora=p["t"], pct_secoes=100 * n / len(s), comp_x=p["x"], comp_pt=p["projecao"]["PT"],
                   comp_pl=p["projecao"]["PL"], comp_lo=p["faixa"]["PT"][0] if p.get("faixa") else None,
                   comp_hi=p["faixa"]["PT"][1] if p.get("faixa") else None)
        for mode in ("agg", "sec"):
            u, v, um = R.units(s, n, mode)
            r = (model.project_with_band(u, v, unit_mun=um, n_boot=n_boot) if (n_boot and mode == "agg")
                 else model.project(u, v))
            row[f"{mode}_pt"], row[f"{mode}_pl"] = 100 * r["pt"], 100 * r["pl"]
            if "pt_hw90" in r:
                row["agg_pt_hw90"], row["agg_margin_hw90"] = 100 * r["pt_hw90"], 100 * r["margin_hw90"]
        rows.append(row)
        print(f"{p['t']}  {row['pct_secoes']:5.1f}%  Lula err: agg {row['agg_pt'] - truth_pt:+.2f}  "
              f"sec {row['sec_pt'] - truth_pt:+.2f}  competitor {p['projecao']['PT'] - truth_pt:+.2f}   "
              f"Flávio err: agg {row['agg_pl'] - truth_pl:+.2f}  sec {row['sec_pl'] - truth_pl:+.2f}  "
              f"competitor {p['projecao']['PL'] - truth_pl:+.2f}", flush=True)
    out = pd.DataFrame(rows).assign(truth_pt=truth_pt, truth_pl=truth_pl)
    out.to_csv(os.path.join(data_dir, "replay_2026s.csv"), index=False)


if __name__ == "__main__":
    main()
