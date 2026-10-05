"""Section-level projection model, shared by the live pipeline (live_section_model.py) and the replays (replay/).

Every seção of the target election carries a BASE: the votes its polling place cast in a reference election
(PT, PL = the Bolsonaro lineage, outros), its expected valid votes, and its group codes (região > UF >
município). The live count arrives in UNITS of counted seções: single seções in a replay with per-seção data, or,
live, the counted part of each município (TSE publishes município totals plus which seções are totalized).
For each unit the swing is the change in log-ratios (PT/outros and PL/outros in a first round, PT/PL in a runoff)
between its counted votes and the base of exactly the seções it covers. A weighted regression relates the swing
to the base vote profile and município size, and empirical-Bayes residual effects by região > UF > município
absorb what the regression misses. Every pending seção is projected as base + predicted swing, with its valid votes
= expected valid votes x a UF turnout adjustment, and added to what is already counted. A Poisson bootstrap over
municípios gives the spread, widened by the band calibration from the 2022 replays (replay/README.md).
"""
import numpy as np
import pandas as pd

REGIAO = {**dict.fromkeys(["AC", "AM", "AP", "PA", "RO", "RR", "TO"], "N"),
          **dict.fromkeys(["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"], "NE"),
          **dict.fromkeys(["DF", "GO", "MS", "MT"], "CO"),
          **dict.fromkeys(["ES", "MG", "RJ", "SP"], "SE"),
          **dict.fromkeys(["PR", "RS", "SC"], "S"), "ZZ": "ZZ"}

# 90% band half-width = sqrt((Z90 * BOOT_SCALE * bootstrap_sd)^2 + (FLOOR_PP * share uncounted)^2), calibrated on
# the 2018-2026 replays (7 nights, replay/tune.py, replay/README.md).
Z90 = 1.645
BOOT_SCALE = 1.45
FLOOR_PP = 0.5
TURNOUT_PSEUDO_VOTES = 2000.0


def eb_effects(r, w, g, n_groups):
    """Empirical-Bayes shrunk group effects of residual r (weights w = valid votes, integer groups g).

    Each group's weighted mean m_g is shrunk by tau2 / (tau2 + noise_g). Groups holding several units (regiões, UFs,
    or municípios when seções are units): noise_g = within-group scatter / the group's effective number of units, and
    tau2 by moments. Groups of one unit each (municípios, when only município totals are known) have no within-group
    scatter, so noise_g = s2 / W_g (W_g = its counted votes), with tau2 and s2 from a least-squares fit of m_g^2 on
    [1, 1 / W_g]: a município with a handful of counted votes is shrunk toward zero instead of being trusted fully.
    """
    W = np.bincount(g, w, n_groups)
    has = W > 0
    G = int(has.sum())
    if G < 2:
        return np.zeros(n_groups)
    m = np.zeros(n_groups)
    m[has] = np.bincount(g, w * r, n_groups)[has] / W[has]
    units = np.bincount(g, None, n_groups)[has]
    if np.median(units) >= 2:
        # Within-group scatter per unit, divided by each group's effective (Kish) number of units.
        within = (w * (r - m[g]) ** 2).sum() / w.sum()
        neff = W[has] ** 2 / np.bincount(g, w * w, n_groups)[has]
        tau2 = (W[has] * m[has] ** 2).sum() / W[has].sum() - within * (W[has] / neff).sum() / W[has].sum()
        noise = within / neff
    else:
        A = np.column_stack([np.ones(G), 1 / W[has]])
        sw = np.sqrt(W[has])
        tau2, s2 = np.linalg.lstsq(A * sw[:, None], m[has] ** 2 * sw, rcond=None)[0]
        noise = max(s2, 0.0) / W[has]
    eff = np.zeros(n_groups)
    if tau2 > 0:
        eff[has] = m[has] * tau2 / (tau2 + noise)
    return eff


class SectionModel:
    """sections: one row per target seção with columns uf, cd_mun, b_pt, b_pl, b_outros (base votes), exp_valid
    (expected valid votes) and log_size (log of the município's electorate). turno: 1 or 2. extra_X: optional
    (n_sections x k) extra covariates, e.g. the previous runoff's log-ratio when the base is a first-round pivot."""

    def __init__(self, sections, turno, extra_X=None, demo_X=None, demo_prior_votes=None):
        s = sections.reset_index(drop=True)
        self.n = len(s)
        self.turno = turno
        ref = "b_outros" if turno == 1 else "b_pl"
        comps = ["b_pt", "b_pl"] if turno == 1 else ["b_pt"]
        self.bvotes = s[["b_pt", "b_pl", "b_outros"]].to_numpy(float)
        tot = self.bvotes.sum(axis=1)
        base_lr = np.column_stack([np.log((s[c] + 0.5) / (s[ref] + 0.5)) for c in comps])
        cols = [np.ones(self.n), s.b_pt / tot, s.b_pl / tot, s.log_size - s.log_size.mean(), base_lr]
        if extra_X is not None:
            cols.append(np.asarray(extra_X, float).reshape(self.n, -1))
        n_before = sum(1 if np.ndim(c) == 1 else c.shape[1] for c in cols)
        self.demo_idx = []
        if demo_X is not None:  # demographic covariates (demo_covariates), with a ridge prior toward no effect
            demo_X = np.asarray(demo_X, float).reshape(self.n, -1)
            cols.append(demo_X)
            self.demo_idx = list(range(n_before, n_before + demo_X.shape[1]))
        self.demo_prior_votes = demo_prior_votes
        self.X = np.column_stack(cols)
        self.levels = [pd.factorize(s.uf.map(REGIAO).fillna("ZZ"))[0], pd.factorize(s.uf)[0], pd.factorize(s.cd_mun)[0]]
        self.n_levels = [lv.max() + 1 for lv in self.levels]
        self.uf_codes, self.n_uf = self.levels[1], self.n_levels[1]
        self.mun_codes, self.n_mun = self.levels[2], self.n_levels[2]
        self.exp_valid = s.exp_valid.to_numpy(float)
        self.base_lr = base_lr
        self.exact_shift = True
        # How regression coefficients vary by região and UF. A number (default 1e4): each região's, then each UF's
        # coefficients are a ridge toward the level above worth that many votes (intercepts free). "eb": intercept and
        # base-profile slopes shrunk by empirical Bayes. None: national coefficients. Chosen on the 2018-2026 replays
        # together with the demographic covariates (replay/tune.py, replay/README.md).
        self.slope_prior_votes = 1e4
        self.mun_effects = True

    def _lr(self, v):
        """Log-ratios of an (n x 3) [pt, pl, outros] vote array."""
        v = v + 0.5
        if self.turno == 1:
            return np.column_stack([np.log(v[:, 0] / v[:, 2]), np.log(v[:, 1] / v[:, 2])])
        return np.log(v[:, 0] / v[:, 1])[:, None]

    def _shares(self, lr):
        e = np.exp(lr)
        return e / (1 + e.sum(axis=1))[:, None]

    def _unit_swing(self, uidx, wb, unit_votes, n_units, counted, n_iter=10):
        """Per-unit log-ratio shift that, applied to every seção of the unit, reproduces the unit's counted shares.
        (The log-ratio change of the unit's aggregate differs: a uniform shift moves lopsided seções less in share
        terms.)"""
        target = self._lr(unit_votes.astype(float))
        base = self.base_lr[counted]
        k = base.shape[1]
        tw = np.maximum(np.bincount(uidx, wb, n_units), 1e-12)

        def agg_lr(delta):
            sh = self._shares(base + delta[uidx])
            agg = np.column_stack([np.bincount(uidx, wb * sh[:, j], n_units) for j in range(k)]) / tw[:, None]
            rest = np.clip(1 - agg.sum(axis=1), 1e-9, None)
            return np.log(np.clip(agg, 1e-9, None) / rest[:, None])

        delta = target - agg_lr(np.zeros((n_units, k)))
        for _ in range(n_iter):
            delta = delta + (target - agg_lr(delta))
        return delta

    def _eb_slopes(self, Xk, wk, y, beta, ugroups, keep, pend, min_units=40):
        """Intercept and base-profile slopes (the log-ratio covariates) by região, then by UF, shrunk toward the parent
        level by empirical Bayes; the other coefficients stay national. Each group's deviation from its parent is a
        small WLS of the parent's residual on [1, base log-ratios], with sampling variance s2 * diag((X'WX)^-1); the
        between-group variance of each term, t_k, is the excess spread of those deviations over their sampling
        variance, and each deviation is kept in proportion t_k / (t_k + v_gk). So the data decide, each night, how much
        the swing pattern differs by place: a realignment pulls the groups apart, an ordinary night keeps them national.
        """
        cols = [0] + list(range(4, 4 + self.base_lr.shape[1]))
        b_unit = np.tile(beta, (len(wk), 1))
        b_pend = np.tile(beta, (pend.sum(), 1))
        for lv, ug in ((self.levels[0], ugroups[0]), (self.levels[1], ugroups[1])):
            gk, gp = ug[keep], lv[pend]
            res_parent = y - (Xk * b_unit).sum(axis=1)
            est, var, members = [], [], []
            for gcode in np.unique(gk):
                m_ = gk == gcode
                if m_.sum() < min_units:
                    continue
                Zg, wg, rg = Xk[m_][:, cols], wk[m_], res_parent[m_]
                A = (Zg * wg[:, None]).T @ Zg
                d = np.linalg.lstsq(A, (Zg * wg[:, None]).T @ rg, rcond=None)[0]
                s2 = (wg * (rg - Zg @ d) ** 2).sum() / max(1, m_.sum() - len(cols))
                est.append(d); var.append(s2 * np.diag(np.linalg.pinv(A))); members.append(gcode)
            if len(est) < 3:
                continue
            est, var = np.array(est), np.array(var)
            t = np.maximum(0.0, (est ** 2).mean(axis=0) - var.mean(axis=0))
            for gcode, d, v in zip(members, est, var):
                m_ = gk == gcode
                shrink = np.where(t > 0, t / (t + np.maximum(v, 1e-12)), 0.0)
                bg = b_unit[m_][0].copy()
                bg[cols] += shrink * d
                b_unit[m_] = bg
                b_pend[gp == gcode] = bg
        return b_unit, b_pend

    def project(self, unit_of, unit_votes, unit_mult=None, unit_frac=None, detail=False, beta_within=None,
                unit_fit=None):
        """Projection given the current count.

        unit_of:    (n_sections,) int, the counting unit each seção belongs to, -1 if not counted yet.
        unit_votes: (n_units x 3) counted [pt, pl, outros] votes per unit.
        unit_mult:  optional (n_units,) bootstrap weights.
        beta_within: optional (n_components x n_covariates) slopes of swing on the covariates WITHIN a município;
                    pending seções of a município with counted units then get the unit's fitted swing plus
                    beta_within x (their covariates - the unit's), instead of the between-município slope.
        unit_fit:   optional (n_units,) bool, units allowed to inform the swing (default all); the others' votes still
                    count, e.g. municípios whose counted seções are not yet identified (their base is only a guess).
        unit_frac:  optional (n_sections,) share of each seção counted, for proportional reconciliation (default:
                    1 for counted seções); a seção with 0 < frac < 1 contributes frac of its base to its unit and
                    1 - frac of it to the pending projection.
        Returns dict with pt, pl, margin (shares of valid votes) and frac_counted (share of projected valid votes);
        with detail=True also the pending seções' mask and projected PT / PL / valid votes ("pending", "pend_pt",
        "pend_pl", "pend_valid"), e.g. for per-UF projections.
        """
        counted = unit_of >= 0
        frac = np.where(counted, 1.0, 0.0) if unit_frac is None else np.where(counted, unit_frac, 0.0)
        n_units = len(unit_votes)
        uidx = unit_of[counted]
        fc = frac[counted]
        # Unit base votes, covariates and groups. Each seção enters with its base SHARES weighted by its own expected
        # valid votes (x share counted) -- not its polling place's raw base votes, which every seção of a place shares
        # and which would over-weight places with many seções.
        wb = self.exp_valid[counted] * fc
        bshare = self.bvotes[counted] / np.maximum(self.bvotes[counted].sum(axis=1), 1e-12)[:, None]
        ub = np.column_stack([np.bincount(uidx, bshare[:, k] * wb, n_units) for k in range(3)])
        uw = np.bincount(uidx, wb, n_units)
        uX = np.column_stack([np.bincount(uidx, self.X[counted, k] * wb, n_units) for k in range(self.X.shape[1])])
        uX = uX / np.maximum(uw, 1e-12)[:, None]
        ucount = np.maximum(np.bincount(uidx, None, n_units), 1)
        # A unit never spans groups, so the mean code over its seções is its code.
        ugroups = [np.rint(np.bincount(uidx, lv[counted], n_units) / ucount).astype(int) for lv in self.levels]
        uexp = np.bincount(uidx, self.exp_valid[counted] * fc, n_units)
        uvalid = unit_votes.sum(axis=1).astype(float)
        w = uvalid * (1.0 if unit_mult is None else unit_mult)
        keep = (w > 0) & (uw > 0)
        if unit_fit is not None:
            keep &= unit_fit
        if keep.sum() < self.X.shape[1] + 2:  # nothing (or too little) counted yet to fit anything
            nan = float("nan")
            return {"pt": nan, "pl": nan, "margin": nan, "frac_counted": 0.0}
        if self.exact_shift:
            y = self._unit_swing(uidx, wb, unit_votes, n_units, counted)[keep]
        else:
            y = self._lr(unit_votes[keep].astype(float)) - self._lr(ub[keep])
        Xk, wk = uX[keep], w[keep]

        pend = frac < 1
        Xp = self.X[pend]
        swing = np.zeros((pend.sum(), y.shape[1]))
        sw = np.sqrt(wk)
        ridge = None
        if self.demo_idx and self.demo_prior_votes:
            # Demographic slopes start at zero and are worth demo_prior_votes votes of evidence: unrepresentative
            # early municípios cannot swing them, while late in the count they are free (replay/README.md).
            XtWX = (Xk * wk[:, None]).T @ Xk
            ridge = np.zeros_like(XtWX)
            di = np.array(self.demo_idx)
            ridge[np.ix_(di, di)] = self.demo_prior_votes * XtWX[np.ix_(di, di)] / wk.sum()
        for j in range(y.shape[1]):
            if ridge is None:
                beta = np.linalg.lstsq(Xk * sw[:, None], y[:, j] * sw, rcond=None)[0]
            else:
                beta = np.linalg.lstsq(XtWX + ridge, (Xk * wk[:, None]).T @ y[:, j], rcond=None)[0]
            if self.slope_prior_votes:
                if self.slope_prior_votes == "eb":
                    b_unit, b_pend = self._eb_slopes(Xk, wk, y[:, j], beta, ugroups, keep, pend)
                else:
                    # Coefficients by região, then by UF, each a ridge toward its parent's: the parent counts as
                    # slope_prior_votes valid votes of the national design (X'WX scaled to that many votes).
                    XtWX = (Xk * wk[:, None]).T @ Xk
                    P = self.slope_prior_votes * XtWX / wk.sum()
                    P[0, :] = P[:, 0] = 0  # intercepts are free by group; only slopes are pulled toward the parent
                    b_unit = np.zeros((len(wk), len(beta)))
                    b_pend = np.zeros((pend.sum(), len(beta)))
                    b_unit[:], b_pend[:] = beta, beta
                    for lv, ug in ((self.levels[0], ugroups[0]), (self.levels[1], ugroups[1])):
                        gk, gp = ug[keep], lv[pend]
                        for gcode in np.unique(gk):
                            m_ = gk == gcode
                            parent = b_unit[m_][0]
                            A = (Xk[m_] * wk[m_, None]).T @ Xk[m_] + P
                            bg = np.linalg.lstsq(A, (Xk[m_] * wk[m_, None]).T @ y[m_, j] + P @ parent, rcond=None)[0]
                            b_unit[m_] = bg
                            b_pend[gp == gcode] = bg
                r = y[:, j] - (Xk * b_unit).sum(axis=1)
                pred = (Xp * b_pend).sum(axis=1)
                levels = list(zip(self.levels, ugroups, self.n_levels))
                if not self.mun_effects:
                    levels = levels[:2]
            else:
                r = y[:, j] - Xk @ beta
                pred = Xp @ beta
                levels = list(zip(self.levels, ugroups, self.n_levels))
            for lv, ug, ng in levels:
                eff = eb_effects(r, wk, ug[keep], ng)
                r = r - eff[ug[keep]]
                pred = pred + eff[lv[pend]]
            if beta_within is not None:
                mun_unit = np.full(self.n_mun, -1)
                mun_unit[ugroups[2][keep]] = np.flatnonzero(keep)
                pu = mun_unit[self.mun_codes[pend]]
                has = pu >= 0
                dX = Xp[has] - uX[pu[has]]
                pred[has] = pred[has] + dX @ (beta_within[j] - beta)
            swing[:, j] = pred
        e = np.exp(self.base_lr[pend] + swing)
        denom = 1 + e.sum(axis=1)
        p_pt = e[:, 0] / denom
        p_pl = e[:, 1] / denom if self.turno == 1 else 1 / denom

        # Turnout: counted valid votes vs expected, by UF, shrunk toward the national ratio.
        uuf = ugroups[1]
        V = np.bincount(uuf[keep], wk, self.n_uf)
        E = np.bincount(uuf[keep], uexp[keep] * (wk / uvalid[keep]), self.n_uf)
        adj_nat = V.sum() / E.sum() if E.sum() > 0 else 1.0
        adj = (V + TURNOUT_PSEUDO_VOTES * adj_nat) / (E + TURNOUT_PSEUDO_VOTES)
        vpend = self.exp_valid[pend] * (1 - frac[pend]) * adj[self.uf_codes[pend]]

        known = unit_votes.sum(axis=0).astype(float)
        pt = known[0] + (vpend * p_pt).sum()
        pl = known[1] + (vpend * p_pl).sum()
        tot = known.sum() + vpend.sum()
        out = {"pt": pt / tot, "pl": pl / tot, "margin": (pt - pl) / tot, "frac_counted": known.sum() / tot}
        if detail:
            out.update(pending=pend, pend_pt=vpend * p_pt, pend_pl=vpend * p_pl, pend_valid=vpend)
        return out

    def project_with_band(self, unit_of, unit_votes, unit_frac=None, unit_mun=None, n_boot=100, seed=0, unit_fit=None):
        """Point projection plus a calibrated 90% band (pp) for margin, PT share and PL share.
        unit_mun: (n_units,) município code of each unit, for the bootstrap (default: units are resampled)."""
        point = self.project(unit_of, unit_votes, unit_frac=unit_frac, unit_fit=unit_fit)
        if np.isnan(point["margin"]):
            return {**point, **{f"{n}_{k}": float("nan") for n in ("margin", "pt", "pl") for k in ("hw90", "boot_sd")}}
        rng = np.random.default_rng(seed)
        n_units = len(unit_votes)
        if unit_mun is None:
            unit_mun = np.arange(n_units)
        n_cl = unit_mun.max() + 1 if n_units else 0
        draws = []
        for _ in range(n_boot):
            mult = rng.poisson(1.0, n_cl).astype(float)[unit_mun]
            p = self.project(unit_of, unit_votes, unit_mult=mult, unit_frac=unit_frac, unit_fit=unit_fit)
            if np.isnan(p["margin"]):
                continue
            draws.append([p["margin"], p["pt"], p["pl"]])
        draws = np.array(draws)
        sd = draws.std(axis=0) if len(draws) > 1 else np.full(3, np.nan)
        uncounted = 1 - point["frac_counted"]
        # Share half-widths take half the margin floor (a margin error is ~2x a share error).
        floors = np.array([FLOOR_PP, FLOOR_PP / 2, FLOOR_PP / 2]) / 100 * uncounted
        hw = np.sqrt((Z90 * BOOT_SCALE * sd) ** 2 + floors ** 2)
        out = dict(point)
        for k, name in enumerate(["margin", "pt", "pl"]):
            out[name + "_hw90"] = hw[k]
            out[name + "_boot_sd"] = sd[k]
        return out


# ---------------------------------------------------------------- runoff pivot (first round -> runoff transfer)
PIVOT_REGIOES = ["N", "NE", "CO", "S", "ZZ"]
DEMO_COLS = ["fem", "a16_24", "a25_39", "a60p", "edu_low", "edu_high"]


def demo_covariates(sections, perfil):
    """(n_sections x 6) electorate-profile shares (DEMO_COLS, replay/prep_perfil.py) for each seção, centered; a seção
    missing from the profile file takes its município's electorate-weighted mean, then the national one."""
    k = ["uf", "cd_mun", "zona", "secao"]
    d = sections[k].merge(perfil[k + DEMO_COLS + ["eleitores_perfil"]], on=k, how="left")
    w = perfil.eleitores_perfil
    for c in DEMO_COLS:
        mun = (perfil[c] * w).groupby(perfil.cd_mun).sum() / w.groupby(perfil.cd_mun).sum()
        nat = (perfil[c] * w).sum() / w.sum()
        d[c] = d[c].fillna(d.cd_mun.map(mun)).fillna(nat)
    X = d[DEMO_COLS].to_numpy(float)
    return X - X.mean(axis=0)


def pivot_features(pt1, pl1, o1, uf):
    """Features of a seção's first-round result for the runoff transfer function: log(PT/PL), the share of other
    candidates, their interaction and square, and region-specific transfer of the others' vote (SE = reference)."""
    pt1, pl1, o1 = (np.asarray(v, float) for v in (pt1, pl1, o1))
    v1 = np.maximum(pt1 + pl1 + o1, 1e-9)
    f1 = np.log((pt1 + 0.5) / (pl1 + 0.5))
    so = o1 / v1
    reg = pd.Series(np.asarray(uf)).map(REGIAO).fillna("ZZ").to_numpy()
    cols = [np.ones(len(v1)), f1, so, f1 * so, so ** 2] + [so * (reg == r) for r in PIVOT_REGIOES]
    return np.column_stack(cols)


def fit_pivot(pt1, pl1, o1, uf, pt2, pl2):
    """Weighted least squares of the runoff log(PT/PL) on pivot_features, over seções of a past election."""
    X = pivot_features(pt1, pl1, o1, uf)
    pt2, pl2 = np.asarray(pt2, float), np.asarray(pl2, float)
    y = np.log((pt2 + 0.5) / (pl2 + 0.5))
    sw = np.sqrt(pt2 + pl2)
    return np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)[0]


def apply_pivot(beta, pt1, pl1, o1, uf):
    """Predicted runoff PT share (of PT+PL) per seção from its first-round result."""
    return 1 / (1 + np.exp(-pivot_features(pt1, pl1, o1, uf) @ beta))

