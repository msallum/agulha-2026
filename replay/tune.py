"""Score section-model configurations on every replayable election night at once.

Usage: python3 replay/tune.py <data_dir> <night> [<night> ...]   (writes <data_dir>/tune_<night>.csv)
Nights: e.g. 2022-1, 2022-2p (runoff, pivot base), 2022-2 (runoff, previous-runoff base), 2018-1, 2018-2p, 2018-2,
2014-*, 2010-*, 2006-* (proxy arrival order, see replay.load), 2026-1 (our live snapshots of 4 Oct; needs
section_base_2026.csv.gz, the 2026 detalhe parquet and the snapshots in the repo root).
Each configuration is projected at every checkpoint in the município-unit (live) mode, point estimate only.
"""
import glob
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import replay as R  # noqa: E402
from section_model import SectionModel  # noqa: E402

CONFIGS = {"national": None, "eb": "eb", "uf_3e5": 3e5, "uf_1e5": 1e5, "uf_3e4": 3e4, "uf_1e4": 1e4}
if os.environ.get("TUNE_CONFIGS"):
    CONFIGS = {k: v for k, v in CONFIGS.items() if k in os.environ["TUNE_CONFIGS"].split(",")}
PCTS = [1, 2, 3, 5, 7.5, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90]


def night_inputs(data_dir, night):
    """(model frame, extra covariates, turno, list of (checkpoint label, unit_of, votes), truth margin)."""
    year, kind = night.split("-")
    year, turno, pivot = int(year), int(kind[0]), kind.endswith("p")
    if year == 2026:
        return night_2026(data_dir)
    base = {2022: 2018, 2018: 2014, 2014: 2010, 2010: 2006, 2006: 2002}[year]
    s, _ = R.load(data_dir, year, base, turno)
    s["exp_valid"] = s.aptos * s.b_valid / s.b_aptos
    s["mun_code"] = pd.factorize(s.cd_mun)[0]
    extra = None
    if pivot:
        s, extra = R.pivot_base(s, data_dir, year, [base])
    truth = (s.pt.sum() - s.pl.sum()) / s.valid.sum()
    cps = []
    for p in PCTS:
        n = int(round(len(s) * p / 100))
        u, v, _ = R.units(s, n, "agg")
        cps.append((p, u, v))
    return s, extra, turno, cps, truth


def night_2026(data_dir):
    k = ["uf", "cd_mun", "zona", "secao"]
    b = pd.read_csv("section_base_2026.csv.gz")
    det = pd.read_parquet(os.path.join(data_dir, "2026", "detalhe_2026_1t.parquet"))[k + ["totalizado"]]
    s = b.merge(det, on=k, how="left")
    s["totalizado"] = s.totalizado.fillna(pd.Timestamp.max)
    s = s.rename(columns={"b1_pt": "b_pt", "b1_pl": "b_pl", "b1_outros": "b_outros"})
    s["exp_valid"] = (s.aptos * s.b1_valid / s.b1_aptos.where(s.b1_aptos > 0)).fillna(s.aptos * 0.75)
    s["log_size"] = np.log(s.groupby("cd_mun").aptos.transform("sum"))
    s = s.sort_values(["cd_mun", "totalizado"]).reset_index(drop=True)
    rk = s.groupby("cd_mun").cumcount().to_numpy()
    n_in = s.groupby("cd_mun").secao.transform("size").to_numpy()
    mc, mu = pd.factorize(s.cd_mun)
    cps, done = [], set()
    for f in sorted(glob.glob("live_snapshot_2026100[45]_*.csv")):
        stamp = datetime.strptime(os.path.basename(f)[14:29], "%Y%m%d_%H%M%S") - timedelta(hours=3)
        if stamp < datetime(2026, 10, 4, 17, 0):
            continue
        sn = pd.read_csv(f, dtype={"codigo_tse": str})
        sn = sn[sn.pct_secoes_apuradas > 0]
        sn["cd_mun"] = sn.codigo_tse.astype(int)
        pct = s.cd_mun.map(sn.set_index("cd_mun").pct_secoes_apuradas).fillna(0).to_numpy()
        cnt = rk < np.rint(pct / 100 * n_in)
        share = 100 * cnt.mean()
        cp = next((p for p in PCTS if abs(share - p) <= max(0.5, p * 0.1) and p not in done), None)
        if cp is None:
            continue
        done.add(cp)
        sv = sn.set_index("cd_mun").reindex(mu)
        v = np.column_stack([sv.votos_lula_2026.fillna(0), sv.votos_bolsonaro_2026.fillna(0),
                             (sv.votos_validos_total_2026 - sv.votos_lula_2026 - sv.votos_bolsonaro_2026).fillna(0)])
        v = np.where(np.bincount(mc, cnt, len(mu))[:, None] > 0, v, 0)
        cps.append((cp, np.where(cnt, mc, -1), v))
    return s, None, 1, cps, (45.16 - 47.03) / 100


def main():
    data_dir = sys.argv[1]
    for night in sys.argv[2:]:
        s, extra, turno, cps, truth = night_inputs(data_dir, night)
        rows = []
        for name, kappa in CONFIGS.items():
            m = SectionModel(s, turno, extra_X=extra)
            m.slope_prior_votes = kappa
            n_boot = int(os.environ.get("TUNE_BOOT", "0"))
            for p, u, v in cps:
                if n_boot:
                    r = m.project_with_band(u, v, unit_mun=np.arange(len(v)), n_boot=n_boot, seed=int(p * 10))
                    extra_cols = dict(boot_sd=100 * r["margin_boot_sd"], frac_counted=r["frac_counted"])
                else:
                    r, extra_cols = m.project(u, v), {}
                rows.append(dict(night=night, config=name, pct=p, err=100 * (r["margin"] - truth), **extra_cols))
            e = [x["err"] for x in rows if x["config"] == name and 2 <= x["pct"] <= 90]
            print(f"{night} {name:9s} mean|err| 2-90% {np.nanmean(np.abs(e)):.3f}  max {np.nanmax(np.abs(e)):.2f}", flush=True)
        tag = os.environ.get("TUNE_TAG", "")
        pd.DataFrame(rows).to_csv(os.path.join(data_dir, f"tune_{night}{tag}.csv"), index=False)


if __name__ == "__main__":
    main()
