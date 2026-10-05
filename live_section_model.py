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

from section_model import SectionModel, apply_pivot

ROUND = int(os.environ.get("ROUND", "1"))
SECTION_BASE = os.environ.get("SECTION_BASE", "section_base_2026.csv.gz")
PIVOT_FILE = os.environ.get("PIVOT_FILE", "pivot_transfer.json")
SUMMARY = os.environ.get("SUMMARY", "live_needle_summary_latest.json")
SNAPSHOT = os.environ.get("SNAPSHOT", "live_snapshot_latest.csv")
N_BOOT = int(os.environ.get("N_BOOT", "100"))
KEY = ["uf", "cd_mun", "zona", "secao"]
MIN_IDENTIFIED = 0.8  # share of a município's counted seções cs.json must identify for it to inform the swing


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


def reconcile(s, pct_by_mun, cs_counted):
    """Share of each seção counted. Per município: k = share of seções counted (TSE) x its seções. The seções cs.json
    lists as totalized count first, earliest listed first; if it lists more than k, only the earliest k count; if
    fewer, the remainder is spread proportionally over the unlisted seções. With no cs.json this is k/n everywhere."""
    pct = s.cd_mun.map(pct_by_mun).fillna(0).to_numpy() / 100
    t = pd.to_datetime(pd.Series([cs_counted.get(k) for k in zip(s.uf, s.cd_mun, s.zona, s.secao)]),
                       format="%d/%m/%Y %H:%M:%S", errors="coerce")
    listed = t.notna().to_numpy()
    d = pd.DataFrame({"mun": s.cd_mun.to_numpy(), "t": t, "listed": listed})
    n = d.groupby("mun").mun.transform("size").to_numpy()
    c = d.groupby("mun").listed.transform("sum").to_numpy().astype(float)
    k = pct * n
    rank = d.sort_values("t", kind="stable").groupby("mun").cumcount().reindex(d.index).to_numpy()  # 0 = earliest
    frac_listed = np.clip(k - rank, 0, 1)  # the first floor(k) listed seções count fully, the next one partly
    frac = np.where(listed, np.where(c >= k, frac_listed, 1.0),
                    np.where(c >= k, 0.0, (k - c) / np.maximum(n - c, 1e-9)))
    identified = np.where(k > 0, np.minimum(c, k) / np.maximum(k, 1e-9), 0)
    return np.clip(frac, 0, 1), listed, identified


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
            print(f"section config unavailable ({e}); reconciling proportionally", file=sys.stderr)

    frac, listed, identified = reconcile(s, snap.set_index("cd_mun").pct_secoes_apuradas, cs_counted)
    mun_codes, mun_uniq = pd.factorize(s.cd_mun)
    unit_of = np.where(frac > 0, mun_codes, -1)
    sv = snap.set_index("cd_mun")
    # Units: municípios of the base, then any reporting município the base lacks (votes still count).
    others = [m for m in sv.index if m not in set(mun_uniq)]
    order = list(mun_uniq) + others
    votes = sv.reindex(order)[["votos_lula_2026", "votos_bolsonaro_2026", "outros"]].fillna(0).to_numpy(float)
    has_sections = np.bincount(mun_codes, frac > 0, len(mun_uniq)) > 0
    votes[: len(mun_uniq)][~has_sections] = 0  # reported share rounds to no seção: wait for the next cycle

    # Only municípios whose counted seções are (mostly) identified inform the swing: for the rest, the base of the
    # counted part is a proportional guess, which brings back the whole-município comparison the old model suffered
    # from. If the seção configuration is missing altogether, fall back to using everyone.
    ident_mun = np.bincount(mun_codes, identified, len(mun_uniq)) / np.bincount(mun_codes, None, len(mun_uniq))
    unit_fit = np.r_[ident_mun >= MIN_IDENTIFIED, np.zeros(len(others), bool)]
    if (unit_fit & (votes.sum(axis=1) > 0)).sum() < 50:
        unit_fit = None
    model = SectionModel(s, ROUND, extra_X=extra)
    p = model.project_with_band(unit_of, votes, unit_frac=frac, unit_mun=np.arange(len(votes)), n_boot=N_BOOT,
                                unit_fit=unit_fit)
    detail = model.project(unit_of, votes, unit_frac=frac, detail=True, unit_fit=unit_fit)

    out = {"base": base_kind, "updated_at": summary.get("updated_at"),
           "secoes_identificadas_pct": 100 * listed.mean(), "pleito": pleito,
           "municipios_no_ajuste": int(unit_fit.sum()) if unit_fit is not None else None}
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
        uf = uf_pend.add(uf_snap.reindex(uf_pend.index).fillna(0), fill_value=0)
        out["by_uf"] = {u: {"pt_pct": 100 * r.pt / r.valid, "pl_pct": 100 * r.pl / r.valid,
                            "counted_pct": 100 * uf_snap.valid.get(u, 0) / r.valid}
                        for u, r in uf.iterrows() if r.valid > 0}
    summary["section_model"] = out
    json.dump(summary, open(SUMMARY, "w"), ensure_ascii=False, indent=1, allow_nan=False, default=float)
    msg = (f"section model: {out['status']}" if out["status"] != "ok" else
           f"section model: Lula {out['pt_pct']:.2f} ±{out['pt_hw90_pp']:.2f}, Bolsonaro {out['pl_pct']:.2f}, "
           f"margin {out['margin_pp']:+.2f} ±{out['margin_hw90_pp']:.2f}, P(Lula) {out['prob_lula']:.3f}, "
           f"{100 * out['frac_votes_counted']:.1f}% of expected votes counted")
    print(msg + f"; cs.json identified {out['secoes_identificadas_pct']:.1f}% of seções as totalized")


if __name__ == "__main__":
    main()
