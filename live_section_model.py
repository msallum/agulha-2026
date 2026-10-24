"""Live section-level projection, run after each poll_live_results.R cycle (scripts/publish.sh).

Reads the cycle's município results (live_snapshot_latest.csv) and TSE's per-UF seção configuration (arquivo-urna
.../config/{uf}/{uf}-p{pleito}-cs.json, whose per-seção time marks the seções already totalized), works out which
seções each município's counted votes cover, and projects the national and per-UF result with section_model.py.
Adds the result to live_needle_summary_latest.json under "section_model".

Base (section_base_2026.csv.gz, build_section_base.py): first round -- each seção's 2022 first-round polling place.
Runoff -- the seção's own 2026 first round through the historical first-round -> runoff transfer
(pivot_transfer.json), with the 2022 runoff polling place as an extra covariate; until TSE publishes per-seção 2026
first-round votes (r1_* columns), the 2022 runoff polling place alone. Expected valid votes in the runoff: the seção's
own 2026 first-round valid votes.

No projection is published until 2% of the electorate is in counted seções (status "preliminar").

Identification: a UF's cs.json is used only if its stamps look like totalization times (not bunched, not listing
more seções than TSE reports counted); otherwise, and for any remainder, a município's count is assumed to cover its
seções in their 4 Oct totalization order. The band widens with the share of counted votes whose coverage is guessed.
Each cycle appends diagnostics, and the old município model's numbers for private comparison, to
section_model_log.csv.

Env: ROUND (1/2), SECTION_BASE, PIVOT_FILE, SUMMARY, SNAPSHOT, N_BOOT, CS_DIR (read cs.json files from a directory
instead of TSE, for tests).
"""
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from math import erf, sqrt

import numpy as np
import pandas as pd

from section_model import DEMO_COLS, SectionModel, apply_pivot, demo_covariates

ROUND = int(os.environ.get("ROUND", "1"))
SECTION_BASE = os.environ.get("SECTION_BASE", "section_base_2026.csv.gz")
PIVOT_FILE = os.environ.get("PIVOT_FILE", "pivot_transfer.json")
SUMMARY = os.environ.get("SUMMARY", "live_needle_summary_latest.json")
SNAPSHOT = os.environ.get("SNAPSHOT", "live_snapshot_latest.csv")
N_BOOT = int(os.environ.get("N_BOOT", "100"))
KEY = ["uf", "cd_mun", "zona", "secao"]
MIN_IDENTIFIED = 0.8  # share of a município's counted seções cs.json must identify for it to inform the swing
MIN_FIT_VOTE_SHARE = 0.5  # the identified municípios must hold this share of counted votes to be the fit's only input
FALLBACK_PP = 1.5  # extra band (pp of margin x unidentified share x share uncounted): replays of the 1t-order fallback
LOG = os.environ.get("SECTION_LOG", "section_model_log.csv")
MIN_ELECTORATE_PCT = 2.0  # no projection until this share of the electorate is in counted seções (as projecao.2026)


def phi(z):
    return 0.5 * (1 + erf(z / sqrt(2)))


def get_json(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "agulha-2026"}), timeout=timeout) as r:
        return json.load(r)


def find_pleito(base_url, election_code):
    cfg = get_json(f"{base_url}/comum/config/ele-c.json")
    for p in cfg.get("pl", []):
        if any(str(e.get("cd")) == str(election_code) for e in p.get("e", [])):
            return str(p["cd"])
    return None


def counted_sections(base_url, cycle, pleito, ufs):
    """{(uf, cd_mun, zona, secao): totalization time} for the seções TSE lists as totalized, from cs.json."""
    def one(uf):
        name = f"{uf.lower()}-p{int(pleito):06d}-cs.json"
        try:
            if os.environ.get("CS_DIR"):
                return uf, json.load(open(os.path.join(os.environ["CS_DIR"], name)))
            return uf, get_json(f"{base_url}/{cycle}/arquivo-urna/{pleito}/config/{uf.lower()}/{name}")
        except Exception as e:  # a missing UF just falls back to proportional reconciliation
            print(f"cs.json {uf}: {e}", file=sys.stderr)
            return uf, None

    out = {}
    with ThreadPoolExecutor(8) as ex:
        for uf, cs in ex.map(one, ufs):
            if not cs:
                continue
            for ab in cs.get("abr", []):
                for mu in ab.get("mu", []):
                    for zo in mu.get("zon", []):
                        for se in zo.get("sec", []):
                            if "da" in se:  # "nsp" entries are seções aggregated into another, with no BU of their own
                                out[(uf.upper(), int(mu["cd"]), int(zo["cd"]), int(se["ns"]))] = f"{se['da']} {se['ha']}"
    return out


def uf_usable(uf, listed, t, k):
    """Whether a UF's cs.json time stamps plausibly mark which seções are counted. TSE's final first-round files carry
    stamps bunched within ~20 s after the whole state finished (a bulk rewrite, not totalization times), and a file
    listing more seções than TSE reports counted cannot be marking the counted ones."""
    out = {}
    df = pd.DataFrame({"uf": uf, "listed": listed, "t": t, "k": k})
    for u, g in df.groupby("uf"):
        c = int(g.listed.sum())
        k_uf = g.k.sum()  # k is given once per município, 0 on its other seções
        tt = g.t[g.listed]
        span = (tt.max() - tt.min()).total_seconds() if c else 0.0
        reason = None
        if c == 0:
            reason = "sem seções listadas"
        elif c >= 100 and span < 120:
            reason = f"horários agrupados ({span:.0f}s para {c} seções)"
        elif c > 1.5 * k_uf + 50:
            reason = f"lista {c} seções, TSE conta {k_uf:.0f}"
        out[u] = reason
    return out


def reconcile(s, pct_by_mun, cs_counted):
    """Share of each seção counted, how well that is identified, and which UFs' seção lists were usable.

    Per município, k = TSE's share of seções counted x its seções. Seções are ordered and the first floor(k) count
    fully, the next partly. In a UF whose cs.json is usable, the listed seções come first (by stamp, ties broken by
    4 Oct order); otherwise -- and for the unlisted remainder -- the order is the seção's totalization order within
    its município on 4 Oct (t1_rank), the best available guess of which seções a count covers. identified = share of
    the município's k counted seções the list pins down: all listed ones when it lists no more than k, else those
    strictly before the k-th stamp (a tie at the cut-off is ambiguous); 0 in a UF whose list is unusable."""
    pct = s.cd_mun.map(pct_by_mun).fillna(0).to_numpy() / 100
    t = pd.to_datetime(pd.Series([cs_counted.get(key) for key in zip(s.uf, s.cd_mun, s.zona, s.secao)]),
                       format="%d/%m/%Y %H:%M:%S", errors="coerce")
    listed_raw = t.notna().to_numpy()
    mun = s.cd_mun.to_numpy()
    n = pd.Series(1, index=mun).groupby(level=0).transform("size").to_numpy()
    k = pct * n
    k_mun_first = np.where(pd.Series(mun).duplicated().to_numpy(), 0.0, k)  # k once per município
    reasons = uf_usable(s.uf.to_numpy(), listed_raw, t, k_mun_first)
    ok_uf = s.uf.map(lambda u: reasons.get(u) is None).to_numpy()
    listed = listed_raw & ok_uf
    t1 = s.t1_rank.to_numpy() if "t1_rank" in s else np.zeros(len(s))
    big = pd.Timestamp.max
    d = pd.DataFrame({"mun": mun, "not_listed": ~listed, "t": t.where(listed, big), "t1": t1})
    order = d.sort_values(["mun", "not_listed", "t", "t1"], kind="stable").index
    rank = np.empty(len(s), int)
    rank[order] = d.loc[order].groupby("mun").cumcount().to_numpy()
    frac = np.clip(k - rank, 0, 1)

    # Identification per município.
    c = pd.Series(listed.astype(float)).groupby(mun).transform("sum").to_numpy()
    ident = np.where((k > 0) & (c <= k), c / np.maximum(k, 1e-9), 0.0)
    over = np.unique(mun[(c > k) & (k > 0) & listed])
    if len(over):
        dl = d[listed & np.isin(mun, over)].assign(k=k[listed & np.isin(mun, over)])
        certain = {}
        for m, g in dl.groupby("mun"):
            kk = g.k.iloc[0]
            ts = g.t.sort_values().to_numpy()
            cut = ts[max(int(np.ceil(kk)) - 1, 0)]
            before, at = (ts < cut).sum(), (ts == cut).sum()
            certain[m] = (before + at if before + at <= kk + 1e-9 else before) / kk
        ident = np.where(np.isin(mun, over), pd.Series(mun).map(certain).fillna(0).to_numpy(), ident)
    return frac, listed, np.clip(ident, 0, 1), reasons


def write_log(summary, out, identified_share, mode, uf_reasons):
    """One row per cycle in section_model_log.csv (published to the data branch), to watch identification live."""
    row = {"updated_at": summary.get("updated_at"), "status": out.get("status"),
           "pct_eleitorado": round(out.get("pct_eleitorado", float("nan")), 3),
           "votos_identificados_pct": round(100 * identified_share, 1), "modo": mode,
           "ufs_lista_ok": sum(r is None for r in uf_reasons.values()),
           "ufs_lista_inutilizavel": "; ".join(f"{u}: {r}" for u, r in sorted(uf_reasons.items()) if r),
           "pt_pct": out.get("pt_pct"), "pl_pct": out.get("pl_pct"), "margin_pp": out.get("margin_pp"),
           "margin_hw90_pp": out.get("margin_hw90_pp"), "prob_lula": out.get("prob_lula"),
           "municipios_no_ajuste": out.get("municipios_no_ajuste"),
           "old_model_margin_2022_pp": summary.get("margin_projected_2022_pp"),
           "old_model_se_2022_pp": summary.get("margin_se_2022_pp"),
           "old_model_prob_lula_2022": summary.get("prob_lula_2022")}
    pd.DataFrame([row]).to_csv(LOG, mode="a", header=not os.path.exists(LOG), index=False)


def build_frame():
    b = pd.read_csv(SECTION_BASE)
    b["log_size"] = np.log(b.groupby("cd_mun").aptos.transform("sum").clip(lower=1))
    rate1 = b.b1_valid / b.b1_aptos.where(b.b1_aptos > 0)
    exp_from_rate = (b.aptos * rate1).fillna(b.aptos * 0.75)
    extra, base_kind = None, "1º turno de 2022 no local de votação"
    if ROUND == 1:
        b["b_pt"], b["b_pl"], b["b_outros"] = b.b1_pt, b.b1_pl, b.b1_outros
        b["exp_valid"] = exp_from_rate
    else:
        b["exp_valid"] = b.n1_valid.where(b.n1_valid > 0) if "n1_valid" in b else np.nan
        b["exp_valid"] = b.exp_valid.fillna(exp_from_rate)
        prev = b[["b2_pt", "b2_pl"]].to_numpy(float)
        if {"r1_pt", "r1_pl", "r1_outros"} <= set(b.columns) and b.r1_pt.notna().mean() > 0.9:
            beta = np.array(json.load(open(PIVOT_FILE))["beta"])
            r1 = b[["r1_pt", "r1_pl", "r1_outros"]].fillna(0).to_numpy(float)
            v1 = r1.sum(axis=1)
            has = v1 > 0
            p = apply_pivot(beta, r1[:, 0], r1[:, 1], r1[:, 2], b.uf)
            b["b_pt"] = np.where(has, p * v1, prev[:, 0])
            b["b_pl"] = np.where(has, (1 - p) * v1, prev[:, 1])
            f1 = np.where(has, np.log((r1[:, 0] + 0.5) / (r1[:, 1] + 0.5)), 0)
            so = np.where(has, r1[:, 2] / np.maximum(v1, 1), 0)
            extra = np.column_stack([np.log((prev[:, 0] + 0.5) / (prev[:, 1] + 0.5)), f1, so])
            base_kind = "1º turno de 2026 na seção, via transferência histórica 1º→2º turno"
        else:
            b["b_pt"], b["b_pl"] = prev[:, 0], prev[:, 1]
            base_kind = "2º turno de 2022 no local de votação"
        b["b_outros"] = 0.0
    return b, extra, base_kind


def main():
    summary = json.load(open(SUMMARY))
    s, extra, base_kind = build_frame()
    snap = pd.read_csv(SNAPSHOT, dtype={"codigo_tse": str})
    snap = snap[snap.pct_secoes_apuradas.fillna(0) > 0].copy()
    snap["cd_mun"] = snap.codigo_tse.astype(int)
    for c in ("votos_lula_2026", "votos_bolsonaro_2026", "votos_validos_total_2026"):
        snap[c] = snap[c].fillna(0)
    snap["outros"] = (snap.votos_validos_total_2026 - snap.votos_lula_2026 - snap.votos_bolsonaro_2026).clip(lower=0)

    cs_counted, pleito = {}, None
    base_url, cycle, code = summary.get("base_url"), summary.get("cycle", "ele2026"), summary.get("election_code")
    if (base_url and code) or os.environ.get("CS_DIR"):
        try:
            pleito = os.environ.get("PLEITO") or find_pleito(base_url, code)
            if pleito:
                cs_counted = counted_sections(base_url, cycle, pleito, sorted(s.uf.unique()))
        except Exception as e:
            print(f"section config unavailable ({e}); using 4 Oct counting order", file=sys.stderr)

    frac, listed, identified, uf_reasons = reconcile(s, snap.set_index("cd_mun").pct_secoes_apuradas, cs_counted)
    mun_codes, mun_uniq = pd.factorize(s.cd_mun)
    unit_of = np.where(frac > 0, mun_codes, -1)
    sv = snap.set_index("cd_mun")
    # Units: municípios of the base, then any reporting município the base lacks (votes still count).
    others = [m for m in sv.index if m not in set(mun_uniq)]
    order = list(mun_uniq) + others
    votes = sv.reindex(order)[["votos_lula_2026", "votos_bolsonaro_2026", "outros"]].fillna(0).to_numpy(float)
    has_sections = np.bincount(mun_codes, frac > 0, len(mun_uniq)) > 0
    votes[: len(mun_uniq)][~has_sections] = 0  # reported share rounds to no seção: wait for the next cycle

    # Only municípios whose counted seções are (mostly) identified inform the swing; for the rest, which seções their
    # count covers is a guess (4 Oct order). If the identified ones are too few -- under 50 municípios or under half the
    # counted votes -- everyone informs the swing, with the guessed coverage ("fallback" mode).
    ident_mun = np.bincount(mun_codes, identified, len(mun_uniq)) / np.bincount(mun_codes, None, len(mun_uniq))
    unit_fit = np.r_[ident_mun >= MIN_IDENTIFIED, np.zeros(len(others), bool)]
    vtot = votes.sum(axis=1)
    counted_votes = vtot.sum()
    identified_share = float((vtot * np.r_[ident_mun, np.zeros(len(others))]).sum() / counted_votes) if counted_votes else 0.0
    fit_share = float(vtot[unit_fit].sum() / counted_votes) if counted_votes else 0.0
    mode = "identificadas"
    if (unit_fit & (vtot > 0)).sum() < 50 or fit_share < MIN_FIT_VOTE_SHARE:
        unit_fit, mode = None, "ordem do 1º turno"
    unidentified = 1 - identified_share
    demo = None
    if set(DEMO_COLS) <= set(s.columns):  # electorate profile of each seção (sex, age, education)
        demo = demo_covariates(s, s[["uf", "cd_mun", "zona", "secao", "eleitores_perfil", *DEMO_COLS]].dropna())
    # Share of the electorate in counted seções (the competitor's axis). Below MIN_ELECTORATE_PCT the first few
    # municípios are too unrepresentative to publish a projection; the page says it is waiting instead.
    pct_eleitorado = float(100 * (s.aptos.to_numpy() * frac).sum() / s.aptos.sum())
    if pct_eleitorado < MIN_ELECTORATE_PCT:
        summary["section_model"] = {"base": base_kind, "updated_at": summary.get("updated_at"), "status": "preliminar",
                                    "pct_eleitorado": pct_eleitorado, "min_pct_eleitorado": MIN_ELECTORATE_PCT}
        json.dump(summary, open(SUMMARY, "w"), ensure_ascii=False, indent=1, allow_nan=False, default=float)
        print(f"section model: {pct_eleitorado:.2f}% of the electorate counted, below {MIN_ELECTORATE_PCT}%; "
              "no projection yet")
        write_log(summary, summary["section_model"], identified_share, mode, uf_reasons)
        return
    model = SectionModel(s, ROUND, extra_X=extra, demo_X=demo)
    p = model.project_with_band(unit_of, votes, unit_frac=frac, unit_mun=np.arange(len(votes)), n_boot=N_BOOT,
                                unit_fit=unit_fit)
    detail = model.project(unit_of, votes, unit_frac=frac, detail=True, unit_fit=unit_fit)

    # Widen the band by how much of the counted vote has guessed (not identified) coverage: replays of the 4 Oct-order
    # guess on the 2018 and 2022 runoffs needed FALLBACK_PP x share uncounted on top of the usual band.
    if not np.isnan(p["margin"]):
        add = FALLBACK_PP / 100 * unidentified * (1 - p["frac_counted"])
        for name, scale in (("margin", 1.0), ("pt", 0.5), ("pl", 0.5)):
            p[name + "_hw90"] = float(np.sqrt(p[name + "_hw90"] ** 2 + (scale * add) ** 2))

    out = {"base": base_kind, "updated_at": summary.get("updated_at"),
           "secoes_identificadas_pct": 100 * listed.mean(), "pleito": pleito, "pct_eleitorado": pct_eleitorado,
           "municipios_no_ajuste": int(unit_fit.sum()) if unit_fit is not None else None,
           "votos_identificados_pct": 100 * identified_share, "modo_identificacao": mode,
           "ufs_lista_inutilizavel": {u: r for u, r in uf_reasons.items() if r}}
    if np.isnan(p["margin"]):
        out["status"] = "aguardando"
    else:
        sd = {k: p[k + "_hw90"] / 1.645 for k in ("margin", "pt", "pl")}
        out.update(status="ok", frac_votes_counted=p["frac_counted"],
                   pt_pct=100 * p["pt"], pl_pct=100 * p["pl"], margin_pp=100 * p["margin"],
                   pt_hw90_pp=100 * p["pt_hw90"], pl_hw90_pp=100 * p["pl_hw90"], margin_hw90_pp=100 * p["margin_hw90"],
                   prob_lula=phi(p["margin"] / sd["margin"]) if sd["margin"] > 0 else float(p["margin"] > 0))
        if ROUND == 1:
            out["prob_lula_first_round"] = 1 - phi((0.5 - p["pt"]) / sd["pt"]) if sd["pt"] > 0 else float(p["pt"] > .5)
            out["prob_bolsonaro_first_round"] = 1 - phi((0.5 - p["pl"]) / sd["pl"]) if sd["pl"] > 0 else float(p["pl"] > .5)
        # Per UF: counted votes + projected pending seções.
        pend = detail["pending"]
        uf_pend = pd.DataFrame({"uf": s.uf.to_numpy()[pend], "pt": detail["pend_pt"], "pl": detail["pend_pl"],
                                "valid": detail["pend_valid"]}).groupby("uf").sum()
        uf_snap = snap.assign(uf=snap.uf.str.upper()).groupby("uf")[
            ["votos_lula_2026", "votos_bolsonaro_2026", "votos_validos_total_2026"]].sum()
        uf_snap.columns = ["pt", "pl", "valid"]
        # Union of both: a fully counted UF has no pending seções but must stay in the table.
        uf = uf_pend.add(uf_snap, fill_value=0)
        out["by_uf"] = {u: {"pt_pct": 100 * r.pt / r.valid, "pl_pct": 100 * r.pl / r.valid,
                            "counted_pct": 100 * uf_snap.valid.get(u, 0) / r.valid}
                        for u, r in uf.iterrows() if r.valid > 0}
    summary["section_model"] = out
    json.dump(summary, open(SUMMARY, "w"), ensure_ascii=False, indent=1, allow_nan=False, default=float)
    write_log(summary, out, identified_share, mode, uf_reasons)
    msg = (f"section model: {out['status']}" if out["status"] != "ok" else
           f"section model: Lula {out['pt_pct']:.2f} ±{out['pt_hw90_pp']:.2f}, Bolsonaro {out['pl_pct']:.2f}, "
           f"margin {out['margin_pp']:+.2f} ±{out['margin_hw90_pp']:.2f}, P(Lula) {out['prob_lula']:.3f}, "
           f"{100 * out['frac_votes_counted']:.1f}% of expected votes counted")
    print(msg + f"; {100 * identified_share:.0f}% of counted votes identified by cs.json (mode: {mode}); "
          f"usable lists in {sum(r is None for r in uf_reasons.values())} of {len(uf_reasons)} UFs")


if __name__ == "__main__":
    main()
