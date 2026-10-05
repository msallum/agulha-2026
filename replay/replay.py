"""Replay an election night section by section, in true BU arrival order, and score two projection models.

Usage: python3 replay/replay.py <data_dir> <target_year> <base_year> <turno> [n_boot] [modes, default sec,agg]
Needs <data_dir>/secoes_{year}_{turno}t.parquet for both years (prep_bweb.py / prep_secao.py) and
municipio_regional_clusters.csv (repo root). Writes <data_dir>/replay_{target}_{turno}t.csv.

Models, evaluated at each checkpoint (share of seções counted):
  mun  -- port of the live model's point estimate (poll_live_results.R): per-município margin swing of the counted
          seções vs the WHOLE município's base margin, shrunk by IBGE região imediata toward the national mean, and
          applied to the município's uncounted base votes.
  sec  -- section-level model in the style of projecao.2026elections: each seção's base is its own polling place in
          the base election; swing in log-ratios of counted seções vs the base of exactly those places, fitted by
          weighted regression on the base vote profile (shares and log-ratios) and município size, plus hierarchical (região > UF >
          município) empirical-Bayes residual effects; pending seções get base + predicted swing, with valid votes
          from their electorate and the base valid-vote rate, adjusted by UF. 90% band from a município bootstrap.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from section_model import REGIAO, SectionModel, apply_pivot, fit_pivot  # noqa: E402

CHECKPOINTS = [0.5, 1, 2, 3, 4, 5, 7.5, 10, 12.5, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90, 95, 98]
VOTE = ["pt", "pl", "outros"]
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(data_dir, target, base, turno):
    s = pd.read_parquet(os.path.join(data_dir, f"secoes_{target}_{turno}t.parquet"))
    b = pd.read_parquet(os.path.join(data_dir, f"secoes_{base}_{turno}t.parquet"))
    for d in (s, b):
        d["valid"] = d.pt + d.pl + d.outros
    # Base at polling-place level, falling back to the whole município.
    kl = ["uf", "cd_mun", "zona", "local"]
    bl = b.groupby(kl, as_index=False)[VOTE + ["valid", "aptos"]].sum()
    bm = b.groupby("cd_mun", as_index=False)[VOTE + ["valid", "aptos"]].sum()
    s = s.merge(bl.rename(columns={c: "bl_" + c for c in VOTE + ["valid", "aptos"]}), on=kl, how="left")
    s = s.merge(bm.rename(columns={c: "bm_" + c for c in VOTE + ["valid", "aptos"]}), on="cd_mun", how="left")
    s["matched"] = s.bl_valid.notna() & (s.bl_valid > 0)
    for c in VOTE + ["valid", "aptos"]:
        s["b_" + c] = np.where(s.matched, s["bl_" + c], s["bm_" + c])
    # The handful of seções in places with no base at all get their UF's base.
    uf_base = b.groupby("uf")[VOTE + ["valid", "aptos"]].sum()
    miss = s.b_valid.isna() | (s.b_valid <= 0)
    for c in VOTE + ["valid", "aptos"]:
        s.loc[miss, "b_" + c] = s.loc[miss, "uf"].map(uf_base[c]).fillna(uf_base[c].sum())
    s["regiao"] = s.uf.map(REGIAO)
    clusters = pd.read_csv(os.path.join(REPO, "municipio_regional_clusters.csv"), dtype=str)
    s["cd_rgi"] = s.cd_mun.map(dict(zip(clusters.codigo_tse.astype(int), clusters.cd_rgi)))
    mun_aptos = s.groupby("cd_mun").aptos.transform("sum")
    s["log_size"] = np.log(mun_aptos)
    if s.recebido.isna().all():
        # No arrival times (TSE files before 2018): proxy order from a later night's arrival rank of the same seção,
        # else its município's median rank. Order is only partly stable between elections (2018 vs 2022 rank
        # correlation 0.49 by seção, 0.56 by município), so these are "proxy-order" nights.
        proxy = int(os.environ.get("PROXY_ORDER", "2018"))
        p = pd.read_parquet(os.path.join(data_dir, f"secoes_{proxy}_{turno}t.parquet"))
        p["rank"] = p.recebido.rank(pct=True)
        k = ["uf", "cd_mun", "zona", "secao"]
        s = s.merge(p[k + ["rank"]], on=k, how="left")
        s["rank"] = s["rank"].fillna(s.cd_mun.map(p.groupby("cd_mun")["rank"].median())).fillna(0.5)
        s["recebido"] = pd.Timestamp("2000-01-01 17:00") + pd.to_timedelta(s["rank"] * 4, unit="h")
        print(f"{target} {turno}t: proxy arrival order from {proxy}")
    # Arrival order. Results are released at 17:00 (Brasília); BUs received earlier (abroad) count from then.
    day = s.recebido.dt.normalize().mode()[0]
    s["t"] = s.recebido.clip(lower=day + pd.Timedelta(hours=17))
    s = s.sort_values(["t", "recebido"]).reset_index(drop=True)
    print(f"{target} {turno}t: {len(s)} seções, {100 * (s.valid * s.matched).sum() / s.valid.sum():.1f}% of valid "
          f"votes matched to a {base} polling place")
    return s, b


# ---------------------------------------------------------------- município model (port of the live model)
def pivot_base(s, data_dir, target, hist_years):
    """Runoff base from the SAME seção's first round: the 1st->2nd-round transfer function fitted on past elections'
    seções (hist_years), applied to the target's first-round votes. The previous-runoff polling-place base already on
    s (b_*) becomes extra covariates. Returns the modified frame and the extra covariate matrix."""
    k = ["uf", "cd_mun", "zona", "secao"]
    betas = []
    for y in hist_years:
        a = pd.read_parquet(os.path.join(data_dir, f"secoes_{y}_1t.parquet"))
        b = pd.read_parquet(os.path.join(data_dir, f"secoes_{y}_2t.parquet"))
        d = a[k + VOTE].merge(b[k + ["pt", "pl"]], on=k, suffixes=("1", "2"))
        d = d[(d.pt1 + d.pl1 + d.outros > 0) & (d.pt2 + d.pl2 > 0)]
        betas.append(fit_pivot(d.pt1, d.pl1, d.outros, d.uf, d.pt2, d.pl2))
    beta = np.mean(betas, axis=0)
    first = pd.read_parquet(os.path.join(data_dir, f"secoes_{target}_1t.parquet"))[k + VOTE]
    s = s.merge(first.rename(columns={c: c + "_1t" for c in VOTE}), on=k, how="left")
    has = s.pt_1t.notna() & ((s.pt_1t + s.pl_1t + s.outros_1t) > 0)
    prev_lr = np.log((s.b_pt + 0.5) / (s.b_pl + 0.5)).to_numpy()
    v1 = (s.pt_1t + s.pl_1t + s.outros_1t).fillna(0).to_numpy(float)
    p = apply_pivot(beta, s.pt_1t.fillna(0), s.pl_1t.fillna(0), s.outros_1t.fillna(0), s.uf)
    f1 = np.where(has, np.log((s.pt_1t.fillna(0) + 0.5) / (s.pl_1t.fillna(0) + 0.5)), 0)
    so = np.where(has, s.outros_1t.fillna(0) / np.maximum(v1, 1), 0)
    s["exp_valid"] = np.where(has, v1, s.aptos * s.b_valid / s.b_aptos)
    tot_prev = (s.b_pt + s.b_pl).to_numpy(float)
    s["b_pt"] = np.where(has, p * v1, s.b_pt)
    s["b_pl"] = np.where(has, (1 - p) * v1, s.b_pl)
    s["b_outros"] = 0.0
    print(f"pivot base from {hist_years} transfer: {100 * has.mean():.1f}% of seções have a first round; "
          f"pre-count PT share {100 * s.b_pt.sum() / (s.b_pt + s.b_pl).sum():.2f}")
    return s, np.column_stack([prev_lr, f1, so])


def mun_model(s, n, base_mun, prior):
    """Projected PT-PL margin (share of valid votes) and SE after the first n seções."""
    c = s.iloc[:n]
    g = c.groupby("cd_mun").agg(pt=("pt", "sum"), pl=("pl", "sum"), valid=("valid", "sum"), k=("secao", "size"))
    g = g.join(base_mun, how="left")
    g = g[g.valid > 0]
    g["swing"] = (g.pt - g.pl) / g.valid - g.b_margin
    w, x = g.valid.values, g.swing.values
    mu = (w * x).sum() / w.sum()
    n_eff = w.sum() ** 2 / (w ** 2).sum()
    cs = g.groupby("cd_rgi").apply(lambda d: pd.Series({
        "cmean": np.average(d.swing, weights=d.valid), "cw": d.valid.sum(),
        "neff": d.valid.sum() ** 2 / (d.valid ** 2).sum()}), include_groups=False)
    ncl = len(cs)
    if ncl >= 2:
        between_raw = (cs.cw * (cs.cmean - mu) ** 2).sum() / cs.cw.sum()
        within = (g.valid * (g.swing - g.cd_rgi.map(cs.cmean)) ** 2).sum() / g.valid.sum()
        between = max(0, between_raw - within * (cs.cw / cs.neff).sum() / cs.cw.sum())
    else:
        within, between = ((w * (x - mu) ** 2).sum() / w.sum() if len(g) > 1 else np.nan), np.nan
    sw = prior["w"] if np.isnan(within) else (n_eff * within + 30 * prior["w"]) / (n_eff + 30)
    sb = prior["b"] if np.isnan(between) else (ncl * between + 15 * prior["b"]) / (ncl + 15)
    sw, sb = 4 * sw, 4 * sb
    rho = sb / (sb + sw)
    deff = 1 + (len(g) / max(ncl, 1) - 1) * rho
    prec_g = (n_eff / deff) / sw
    mu_c = (cs.neff / sw * cs.cmean + prec_g * mu) / (cs.neff / sw + prec_g)
    var_c = 1 / (cs.neff / sw + prec_g)

    # Remaining votes: unreported municípios + uncounted base remainder of partly counted ones.
    rem = base_mun.copy()
    rem["counted"] = g.valid.reindex(rem.index).fillna(0)
    done = (g.k >= base_mun.n_secoes.reindex(g.index)).reindex(rem.index).fillna(False).astype(bool)
    rem["w"] = np.where(done, 0, np.maximum(0, rem.b_valid - rem.counted))
    rem = rem[rem.w > 0]
    swing = rem.cd_rgi.map(mu_c).fillna(mu).values
    total = g.valid.sum() + rem.w.sum()
    proj = ((g.pt - g.pl).sum() + (rem.w * (rem.b_margin + swing)).sum()) / total
    # Variance (plug-in version of the live MC): reporting-região groups + fallback groups + systematic term.
    rg = rem.assign(grp=rem.cd_rgi.fillna(pd.Series(rem.index.astype(str), index=rem.index)))
    grp = rg.groupby("grp").agg(wc=("w", "sum"), w2=("w", lambda v: (v ** 2).sum()), rgi=("cd_rgi", "first"))
    grp["neff"] = grp.wc ** 2 / grp.w2
    ins = grp.rgi.isin(cs.index)
    a = (grp.wc / total) ** 2
    var = (a[ins] * grp.rgi[ins].map(var_c)).sum() + sw * (a[ins] / grp.neff[ins]).sum()
    share_fb = grp.wc[~ins].sum() / total
    var += share_fb ** 2 / prec_g + sb * a[~ins].sum() + sw * (a[~ins] / grp.neff[~ins]).sum()
    var += (1.8 / 100) ** 2 * max(0, 1 - g.valid.sum() / total)
    return proj, np.sqrt(var)


# ---------------------------------------------------------------- section model (section_model.py)
def units(s, n, mode):
    """Counting units after the first n seções: one per seção ("sec"), or one per município ("agg", the live case:
    TSE publishes município totals plus which seções are totalized)."""
    unit_of = np.full(len(s), -1)
    if mode == "sec":
        unit_of[:n] = np.arange(n)
        votes = s[["pt", "pl", "outros"]].to_numpy()[:n]
        unit_mun = s.mun_code.to_numpy()[:n]
    else:
        codes, uniq = pd.factorize(s.mun_code.to_numpy()[:n])
        unit_of[:n] = codes
        votes = np.column_stack([np.bincount(codes, s[c].to_numpy()[:n], len(uniq)) for c in ("pt", "pl", "outros")])
        unit_mun = np.asarray(uniq)
    return unit_of, votes, unit_mun


def main():
    data_dir, target, base, turno = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    n_boot = int(sys.argv[5]) if len(sys.argv) > 5 else 100
    modes = sys.argv[6].split(",") if len(sys.argv) > 6 else ["sec", "agg"]
    s, b = load(data_dir, target, base, turno)
    truth_pt, truth_pl = s.pt.sum() / s.valid.sum(), s.pl.sum() / s.valid.sum()
    truth = truth_pt - truth_pl

    bm = b.groupby("cd_mun").agg(pt=("pt", "sum"), pl=("pl", "sum"), b_valid=("valid", "sum"))
    bm["b_margin"] = (bm.pt - bm.pl) / bm.b_valid
    base_mun = bm[["b_margin", "b_valid"]].copy()
    base_mun["cd_rgi"] = s.groupby("cd_mun").cd_rgi.first().reindex(base_mun.index)
    base_mun["n_secoes"] = s.groupby("cd_mun").size().reindex(base_mun.index).fillna(0)
    pj = pd.read_json(os.path.join(REPO, "regional_correlation_prior.json" if turno == 1 else "regional_correlation_prior_2turno.json"), typ="series")
    prior = {"w": pj.prior_sigma2_within_rgi, "b": pj.prior_sigma2_between_rgi}

    s["exp_valid"] = s.aptos * s.b_valid / s.b_aptos
    s["mun_code"] = pd.factorize(s.cd_mun)[0]
    extra = None
    pivot = os.environ.get("PIVOT_YEARS")  # e.g. "2018": runoff base from the same seção's first round
    if pivot and turno == 2:
        s, extra = pivot_base(s, data_dir, target, [int(y) for y in pivot.split(",")])
    model = SectionModel(s, turno, extra_X=extra)
    rows = []
    for pct in CHECKPOINTS:
        n = int(round(len(s) * pct / 100))
        mun_proj, mun_se = mun_model(s, n, base_mun, prior)
        c = s.iloc[:n]
        row = dict(pct_secoes=pct, hora=s.t.iloc[n - 1].strftime("%H:%M"),
                   pct_validos=100 * c.valid.sum() / s.valid.sum(),
                   apurado_margin=100 * (c.pt.sum() - c.pl.sum()) / c.valid.sum(),
                   mun_margin=100 * mun_proj, mun_se=100 * mun_se,
                   truth_margin=100 * truth, truth_pt=100 * truth_pt, truth_pl=100 * truth_pl)
        for mode in modes:
            unit_of, votes, unit_mun = units(s, n, mode)
            p = model.project_with_band(unit_of, votes, unit_mun=unit_mun, n_boot=n_boot, seed=int(pct * 10))
            row.update({f"{mode}_margin": 100 * p["margin"], f"{mode}_pt": 100 * p["pt"], f"{mode}_pl": 100 * p["pl"],
                        f"{mode}_boot_sd": 100 * p["margin_boot_sd"], f"{mode}_hw90": 100 * p["margin_hw90"],
                        "frac_counted": p["frac_counted"]})
        msg = "  ".join(f"{m} {row[m + '_margin'] - row['truth_margin']:+6.2f} ±{row[m + '_hw90']:.2f}" for m in modes)
        print(f"{pct:5.1f}% {row['hora']}  apurado {row['apurado_margin']:+6.2f}  "
              f"mun {row['mun_margin'] - row['truth_margin']:+6.2f}  {msg}", flush=True)
        rows.append(row)
    out = os.environ.get("REPLAY_OUT", os.path.join(data_dir, f"replay_{target}_{turno}t.csv"))
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"truth: PT {100 * truth_pt:.2f}  PL {100 * truth_pl:.2f}  margin {100 * truth:+.2f}")


if __name__ == "__main__":
    main()
