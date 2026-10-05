"""Replay the 2026 first-round night through the section model, from our own live município snapshots.

Usage: python3 replay/replay_2026.py <section_base.csv.gz> <detalhe_2026_1t.parquet> <snapshot_dir> <competitor.json> [n_boot]
  section_base:  build_section_base.py output (each 2026 seção's 2022 polling-place base)
  detalhe:       2026 detalhe_votacao_secao presidential rows (seção keys, totalization time)
  snapshot_dir:  live_snapshot_2026100[45]_*.csv written by poll_live_results.R on 4 Oct (file times are UTC)
  competitor:    projecao.2026elections.workers.dev/dados/apuracao.json, for comparison

TSE's per-seção votes for 2026 are not published yet, so this is exactly the live situation: município totals from
each snapshot, and which seções they cover taken as each município's first k seções by totalization time, k from the
município's share of seções counted (proportional reconciliation, as live).
"""
import glob
import json
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from section_model import SectionModel  # noqa: E402

TRUTH = {"pt": 45.16, "pl": 47.03}  # TSE final, 2026 1st round (% of valid votes)


def main():
    base_path, det_path, snap_dir, comp_path = sys.argv[1:5]
    n_boot = int(sys.argv[5]) if len(sys.argv) > 5 else 100
    k = ["uf", "cd_mun", "zona", "secao"]
    b = pd.read_csv(base_path)
    det = pd.read_parquet(det_path)[k + ["totalizado"]]
    s = b.merge(det, on=k, how="left")
    s["totalizado"] = s.totalizado.fillna(pd.Timestamp.max)
    s = s.rename(columns={"b1_pt": "b_pt", "b1_pl": "b_pl", "b1_outros": "b_outros"})
    s["exp_valid"] = s.aptos * s.b1_valid / s.b1_aptos.where(s.b1_aptos > 0)
    s["exp_valid"] = s.exp_valid.fillna(s.aptos * 0.75)
    s["log_size"] = np.log(s.groupby("cd_mun").aptos.transform("sum"))
    s = s.sort_values(["cd_mun", "totalizado"]).reset_index(drop=True)
    s["rank_in_mun"] = s.groupby("cd_mun").cumcount()
    n_in_mun = s.groupby("cd_mun").secao.transform("size").to_numpy()
    mun_codes, mun_uniq = pd.factorize(s.cd_mun)
    model = SectionModel(s, turno=1)

    comp = json.load(open(comp_path))["pontos"]
    comp_t = [(p["t"], p) for p in comp if p.get("projecao")]

    rows = []
    for f in sorted(glob.glob(os.path.join(snap_dir, "live_snapshot_2026100[45]_*.csv"))):
        stamp = datetime.strptime(os.path.basename(f)[14:29], "%Y%m%d_%H%M%S") - timedelta(hours=3)
        if stamp < datetime(2026, 10, 4, 17, 0):
            continue
        snap = pd.read_csv(f, dtype={"codigo_tse": str})
        snap = snap[snap.pct_secoes_apuradas > 0]
        snap["cd_mun"] = snap.codigo_tse.astype(int)
        pct = s.cd_mun.map(snap.set_index("cd_mun").pct_secoes_apuradas).fillna(0).to_numpy()
        kk = np.rint(pct / 100 * n_in_mun)
        counted = s.rank_in_mun.to_numpy() < kk
        unit_of = np.where(counted, mun_codes, -1)
        sv = snap.set_index("cd_mun").reindex(mun_uniq)
        votes = np.column_stack([sv.votos_lula_2026.fillna(0), sv.votos_bolsonaro_2026.fillna(0),
                                 (sv.votos_validos_total_2026 - sv.votos_lula_2026 - sv.votos_bolsonaro_2026).fillna(0)])
        votes = np.where(np.bincount(mun_codes, counted, len(mun_uniq))[:, None] > 0, votes, 0)
        p = model.project_with_band(unit_of, votes, n_boot=n_boot)
        hhmm = stamp.strftime("%H:%M")
        c = min(comp_t, key=lambda tp: abs((datetime.strptime(tp[0], "%H:%M") - datetime.strptime(hhmm, "%H:%M"))
                                           .total_seconds() % 86400))[1] if comp_t else None
        row = dict(hora=hhmm, pct_secoes=100 * counted.mean(), pct_validos_proj=100 * p["frac_counted"],
                   pt=100 * p["pt"], pl=100 * p["pl"], margin=100 * p["margin"], margin_hw90=100 * p["margin_hw90"],
                   pt_hw90=100 * p["pt_hw90"], margin_boot_sd=100 * p["margin_boot_sd"],
                   comp_t=c["t"] if c else None, comp_x=c["x"] if c else None,
                   comp_pt=c["projecao"]["PT"] if c else None, comp_pl=c["projecao"]["PL"] if c else None)
        rows.append(row)
        print(f"{hhmm}  {row['pct_secoes']:5.1f}% seções  PT {row['pt']:.2f} ({row['pt'] - TRUTH['pt']:+.2f})  "
              f"PL {row['pl']:.2f} ({row['pl'] - TRUTH['pl']:+.2f})  margin err "
              f"{row['margin'] - (TRUTH['pt'] - TRUTH['pl']):+.2f} ±{row['margin_hw90']:.2f}   competitor {row['comp_t']} "
              f"x={row['comp_x']} PT {row['comp_pt'] - TRUTH['pt']:+.2f} PL {row['comp_pl'] - TRUTH['pl']:+.2f}", flush=True)
    out = os.environ.get("REPLAY_OUT", "replay_2026_1t.csv")
    pd.DataFrame(rows).to_csv(out, index=False)


if __name__ == "__main__":
    main()
