"""Replay an election night section by section, in true BU arrival order, and score two projection models.

Usage: python3 replay/replay.py <data_dir> <target_year> <base_year> <turno> [n_boot]
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

REGIAO = {**dict.fromkeys(["AC", "AM", "AP", "PA", "RO", "RR", "TO"], "N"),
          **dict.fromkeys(["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"], "NE"),
          **dict.fromkeys(["DF", "GO", "MS", "MT"], "CO"),
          **dict.fromkeys(["ES", "MG", "RJ", "SP"], "SE"),
          **dict.fromkeys(["PR", "RS", "SC"], "S"), "ZZ": "ZZ"}
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
    # Arrival order. Results are released at 17:00 (Brasília); BUs received earlier (abroad) count from then.
    day = s.recebido.dt.normalize().mode()[0]
    s["t"] = s.recebido.clip(lower=day + pd.Timedelta(hours=17))
    s = s.sort_values(["t", "recebido"]).reset_index(drop=True)
    print(f"{target} {turno}t: {len(s)} seções, {100 * (s.valid * s.matched).sum() / s.valid.sum():.1f}% of valid "
          f"votes matched to a {base} polling place")
    return s, b


# ---------------------------------------------------------------- município model (port of the live model)
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


# ---------------------------------------------------------------- section model
def eb_effects(r, w, g, n_groups):
    """Empirical-Bayes shrunk group means of residual r (weights w, integer groups g); returns per-group effects."""
    W = np.bincount(g, w, n_groups)
    W2 = np.bincount(g, w * w, n_groups)
    has = W > 0
    m = np.zeros(n_groups)
    m[has] = np.bincount(g, w * r, n_groups)[has] / W[has]
    within = (w * (r - m[g]) ** 2).sum() / w.sum()
    neff = np.zeros(n_groups)
    neff[has] = W[has] ** 2 / W2[has]
    between_raw = (W[has] * m[has] ** 2).sum() / W.sum()
    tau2 = max(0.0, between_raw - within * (W[has] / neff[has]).sum() / W.sum())
    eff = np.zeros(n_groups)
    eff[has] = m[has] * tau2 / (tau2 + within / neff[has])
    return eff


class SectionModel:
    def __init__(self, s, turno):
        self.turno = turno
        self.ref = "outros" if turno == 1 else "pl"
        self.comps = ["pt", "pl"] if turno == 1 else ["pt"]
        self.s = s
        base_tot = s[["b_pt", "b_pl", "b_outros"]].sum(axis=1)
        self.base_lr = np.column_stack([np.log((s["b_" + c] + 0.5) / (s["b_" + self.ref] + 0.5)) for c in self.comps])
        # Base vote profile as both shares and log-ratios: the log-ratios cut the 2022 first-round mid-count error
        # from ~0.9pp to ~0.3pp (replay/README.md).
        self.X = np.column_stack([np.ones(len(s)), s.b_pt / base_tot, s.b_pl / base_tot, s.log_size - s.log_size.mean(),
                                  self.base_lr])
        self.lr = np.column_stack([np.log((s[c] + 0.5) / (s[self.ref] + 0.5)) for c in self.comps])
        self.levels = []
        for col in ("regiao", "uf", "cd_mun"):
            codes, uniq = pd.factorize(s[col])
            self.levels.append((codes, len(uniq)))
        self.uf_codes, n_uf = pd.factorize(s.uf)
        self.n_uf = n_uf if isinstance(n_uf, int) else len(n_uf)
        self.mun_codes, self.n_mun = self.levels[2]
        self.rate = (s.b_valid / s.b_aptos).values  # base valid votes per eligible voter, at the polling place
        self.exp_valid = s.aptos.values * self.rate
        self.valid = s.valid.values.astype(float)

    def project(self, n, mult=None):
        """Projected (PT share, PL share, margin) after the first n seções; mult = bootstrap município weights."""
        s = self.s
        cnt = slice(0, n)
        w = self.valid[cnt].copy()
        if mult is not None:
            w = w * mult[self.mun_codes[cnt]]
        keep = w > 0
        X = self.X[cnt][keep]
        wk = w[keep]
        pend = slice(n, len(s))
        swing_pend = []
        for j in range(len(self.comps)):
            y = (self.lr[cnt, j] - self.base_lr[cnt, j])[keep]
            sw = np.sqrt(wk)
            beta = np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)[0]
            r = y - X @ beta
            pred = self.X[pend] @ beta
            for codes, ng in self.levels:
                eff = eb_effects(r, wk, codes[cnt][keep], ng)
                r = r - eff[codes[cnt][keep]]
                pred = pred + eff[codes[pend]]
            swing_pend.append(pred)
        lr = self.base_lr[pend] + np.column_stack(swing_pend)
        e = np.exp(lr)
        denom = 1 + e.sum(axis=1)
        shares = e / denom[:, None]
        # Turnout: observed valid votes vs expected among counted seções, by UF, shrunk toward national.
        ucodes = self.uf_codes[cnt][keep]
        V = np.bincount(ucodes, wk, self.n_uf)
        E = np.bincount(ucodes, (self.exp_valid[cnt][keep] * (w[keep] / self.valid[cnt][keep])), self.n_uf)
        adj_nat = V.sum() / E.sum()
        m = 2000.0
        adj = (V + m * adj_nat) / (E + m)
        vpend = self.exp_valid[pend] * adj[self.uf_codes[pend]]
        pt = s.pt.values[cnt].sum() + (vpend * shares[:, 0]).sum()
        pl_p = shares[:, 1] if self.turno == 1 else 1 / denom
        pl = s.pl.values[cnt].sum() + (vpend * pl_p).sum()
        tot = self.valid[cnt].sum() + vpend.sum()
        return pt / tot, pl / tot, (pt - pl) / tot


def main():
    data_dir, target, base, turno = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    n_boot = int(sys.argv[5]) if len(sys.argv) > 5 else 100
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

    model = SectionModel(s, turno)
    rng = np.random.default_rng(2022)
    rows = []
    for pct in CHECKPOINTS:
        n = int(round(len(s) * pct / 100))
        mun_proj, mun_se = mun_model(s, n, base_mun, prior)
        pt, pl, mg = model.project(n)
        boots = []
        for _ in range(n_boot):
            mult = rng.poisson(1.0, model.n_mun).astype(float)
            boots.append(model.project(n, mult))
        boots = np.array(boots)
        lo, hi = np.percentile(boots[:, 2], [5, 95])
        c = s.iloc[:n]
        row = dict(pct_secoes=pct, hora=s.t.iloc[n - 1].strftime("%H:%M"),
                   pct_validos=100 * c.valid.sum() / s.valid.sum(),
                   apurado_margin=100 * (c.pt.sum() - c.pl.sum()) / c.valid.sum(),
                   mun_margin=100 * mun_proj, mun_se=100 * mun_se,
                   sec_pt=100 * pt, sec_pl=100 * pl, sec_margin=100 * mg,
                   sec_lo=100 * lo, sec_hi=100 * hi, sec_boot_sd=100 * boots[:, 2].std(),
                   truth_margin=100 * truth, truth_pt=100 * truth_pt, truth_pl=100 * truth_pl)
        rows.append(row)
        print(f"{pct:5.1f}% {row['hora']}  apurado {row['apurado_margin']:+6.2f}  "
              f"mun {row['mun_margin'] - row['truth_margin']:+6.2f} (se {row['mun_se']:.2f})  "
              f"sec {row['sec_margin'] - row['truth_margin']:+6.2f} [{row['sec_lo'] - row['truth_margin']:+.2f}, "
              f"{row['sec_hi'] - row['truth_margin']:+.2f}]  pt {row['sec_pt'] - row['truth_pt']:+.2f}", flush=True)
    pd.DataFrame(rows).to_csv(os.path.join(data_dir, f"replay_{target}_{turno}t.csv"), index=False)
    print(f"truth: PT {100 * truth_pt:.2f}  PL {100 * truth_pl:.2f}  margin {100 * truth:+.2f}")


if __name__ == "__main__":
    main()
