"""Per-seção base for the 2026 section-level model: match every 2026 seção to its 2022 polling place.

Usage: python3 build_section_base.py <locais_dir> <data_dir> [out]
  <locais_dir>: eleitorado_local_votacao_2022.zip and eleitorado_local_votacao_2026.zip (cdn.tse.jus.br, odsele/
                eleitorado_locais_votacao/), and detalhe_votacao_secao_2026.zip (odsele/detalhe_votacao_secao/)
  <data_dir>:   secoes_{2018,2022}_{1,2}t.parquet (replay/prep_secao.py, replay/prep_bweb.py), and, once TSE publishes
                it, secoes_2026_1t.parquet (2026 first-round votes per seção, the runoff's pivot base)
  out:          default section_base_2026.csv.gz; also writes pivot_transfer.json next to it (the first-round ->
                runoff transfer function, section_model.fit_pivot, averaged over the 2018 and 2022 elections)

Match cascade, per 2026 polling place (município, zona, local number):
  exact -- the same key existed in 2022 and is the same place (same CEP, < 200 m apart, or same name)
  geo   -- otherwise the nearest 2022 place of the same município within 200 m
  key   -- otherwise the same key, unconfirmed
  zona  -- otherwise the whole 2022 zona;  mun -- otherwise the whole 2022 município;  uf -- otherwise the UF
           (new municípios, new cities abroad)
Output: one row per 2026 principal seção with its electorate (aggregated seções folded in), the match type, the 2022
1st-round (b1_*) and runoff (b2_*) votes of the matched place, the seção's 2026 1st-round turnout and valid votes
(n1_comparecimento, n1_valid, from the detalhe file), its electorate profile (section_model.DEMO_COLS, from
<data_dir>/perfil_2026.parquet, replay/prep_perfil.py) and, once published, its 2026 1st-round votes (r1_*).
"""
import csv
import io
import json
import os
import re
import sys
import unicodedata
import zipfile

import numpy as np
import pandas as pd

from section_model import DEMO_COLS, fit_pivot

KEY = ["uf", "cd_mun", "zona", "local"]
COLS = {"SG_UF": "uf", "CD_MUNICIPIO": "cd_mun", "NR_ZONA": "zona", "NR_SECAO": "secao", "NR_LOCAL_VOTACAO": "local",
        "NM_LOCAL_VOTACAO": "nome", "NR_CEP": "cep", "NR_LATITUDE": "lat", "NR_LONGITUDE": "lon",
        "QT_ELEITOR_SECAO": "aptos", "DS_TIPO_SECAO_AGREGADA": "tipo", "NR_SECAO_PRINCIPAL": "principal",
        "NR_TURNO": "turno"}


def read_locais(path):
    frames = []
    with zipfile.ZipFile(path) as z:
        for name in sorted(n for n in z.namelist() if n.endswith(".csv") and not n.endswith("_BRASIL.csv")):
            frames.append(pd.read_csv(z.open(name), sep=";", encoding="latin-1", usecols=list(COLS), dtype=str))
    d = pd.concat(frames).rename(columns=COLS)
    d = d[d.turno == "1"]
    for c in ("cd_mun", "zona", "secao", "local", "aptos", "principal"):
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(-1).astype(int)
    for c in ("lat", "lon"):
        d[c] = pd.to_numeric(d[c].str.replace(",", "."), errors="coerce")
    d.loc[(d.lat.abs() < 1e-6) | (d.lat == -1), ["lat", "lon"]] = np.nan
    d["nome"] = d.nome.fillna("").map(norm_name)
    return d


def norm_name(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]+", " ", s).strip()


def places(d):
    return d.groupby(KEY, as_index=False).agg(nome=("nome", "first"), cep=("cep", "first"), lat=("lat", "first"),
                                              lon=("lon", "first"))


def dist_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def match_places(p26, p22):
    m = p26.merge(p22, on=KEY, how="left", suffixes=("", "_22"))
    d = dist_m(m.lat, m.lon, m.lat_22, m.lon_22)
    same = m.nome_22.notna() & ((m.cep == m.cep_22) | (d < 200) | (m.nome == m.nome_22))
    m["match"] = np.where(same, "exact", "")
    m["local_22"] = np.where(same, m.local, -1)
    # Nearest 2022 place of the same município within 200 m, for the rest.
    todo = m[(m.match == "") & m.lat.notna()]
    by_mun = {k: g for k, g in p22[p22.lat.notna()].groupby("cd_mun")}
    for idx, row in todo.iterrows():
        g = by_mun.get(row.cd_mun)
        if g is None:
            continue
        dd = dist_m(row.lat, row.lon, g.lat.to_numpy(), g.lon.to_numpy())
        j = int(np.argmin(dd))
        if dd[j] < 200:
            m.at[idx, "match"] = "geo"
            m.at[idx, "local_22"] = int(g.local.iloc[j])
            m.at[idx, "zona_22"] = int(g.zona.iloc[j])
    key_only = (m.match == "") & m.nome_22.notna()
    m.loc[key_only, "match"] = "key"
    m.loc[key_only, "local_22"] = m.loc[key_only, "local"]
    m["zona_22"] = m.get("zona_22", pd.Series(np.nan, index=m.index)).fillna(m.zona).astype(int)
    return m[KEY + ["match", "local_22", "zona_22"]]


def votes_by(df, keys, prefix):
    v = df.assign(valid=df.pt + df.pl + df.outros).groupby(keys, as_index=False)[
        ["pt", "pl", "outros", "valid", "aptos"]].sum()
    return v.rename(columns={c: prefix + c for c in ("pt", "pl", "outros", "valid", "aptos")})


def main():
    locais_dir, data_dir = sys.argv[1], sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else "section_base_2026.csv.gz"
    l26 = read_locais(os.path.join(locais_dir, "eleitorado_local_votacao_2026.zip"))
    l22 = read_locais(os.path.join(locais_dir, "eleitorado_local_votacao_2022.zip"))

    # 2026 principal seções, with the electorate of seções aggregated into them folded in.
    agg = l26[l26.tipo != "Principal"]
    sec = l26[l26.tipo == "Principal"][["uf", "cd_mun", "zona", "secao", "local", "aptos"]].copy()
    extra = agg.groupby(["uf", "cd_mun", "zona", "principal"]).aptos.sum()
    sec["aptos"] += [extra.get(k, 0) for k in zip(sec.uf, sec.cd_mun, sec.zona, sec.secao)]

    pm = match_places(places(l26), places(l22))
    sec = sec.merge(pm, on=KEY, how="left")

    for turno, prefix in ((1, "b1_"), (2, "b2_")):
        s22 = pd.read_parquet(os.path.join(data_dir, f"secoes_2022_{turno}t.parquet"))
        loc = votes_by(s22, KEY, prefix).rename(columns={"zona": "zona_22", "local": "local_22"})
        zon = votes_by(s22, ["uf", "cd_mun", "zona"], prefix)
        mun = votes_by(s22, ["uf", "cd_mun"], prefix)
        ufb = votes_by(s22, ["uf"], prefix)
        vcols = [prefix + c for c in ("pt", "pl", "outros", "valid", "aptos")]
        a = sec.merge(loc, on=["uf", "cd_mun", "zona_22", "local_22"], how="left")[vcols]
        z = sec.merge(zon, on=["uf", "cd_mun", "zona"], how="left")[vcols]
        u = sec.merge(mun, on=["uf", "cd_mun"], how="left")[vcols]
        f = sec.merge(ufb, on=["uf"], how="left")[vcols]
        use_a = a[prefix + "valid"].fillna(0) > 0
        use_z = ~use_a & (z[prefix + "valid"].fillna(0) > 0)
        use_u = ~use_a & ~use_z & (u[prefix + "valid"].fillna(0) > 0)
        for c in vcols:
            sec[c] = np.where(use_a, a[c], np.where(use_z, z[c], np.where(use_u, u[c], f[c])))
        if turno == 1:
            sec["match"] = np.where(use_a, sec.match.fillna("exact"),
                                    np.where(use_z, "zona", np.where(use_u, "mun", "uf")))

    det = os.path.join(locais_dir, "detalhe_votacao_secao_2026.zip")
    if os.path.exists(det):
        rows = []
        with zipfile.ZipFile(det) as z:
            member = next(n for n in z.namelist() if n.endswith("_BR.csv"))
            for r in csv.DictReader(io.TextIOWrapper(z.open(member), encoding="latin-1"), delimiter=";"):
                if r["CD_CARGO"] == "1" and r["NR_TURNO"] == "1":
                    rows.append((r["SG_UF"], int(r["CD_MUNICIPIO"]), int(r["NR_ZONA"]), int(r["NR_SECAO"]),
                                 int(r["QT_COMPARECIMENTO"]), int(r["QT_VOTOS_NOMINAIS"])))
        n1 = pd.DataFrame(rows, columns=["uf", "cd_mun", "zona", "secao", "n1_comparecimento", "n1_valid"])
        sec = sec.merge(n1, on=["uf", "cd_mun", "zona", "secao"], how="left")

    betas = []
    for y in (2018, 2022):
        paths = [os.path.join(data_dir, f"secoes_{y}_{t}t.parquet") for t in (1, 2)]
        if all(os.path.exists(p) for p in paths):
            k = ["uf", "cd_mun", "zona", "secao"]
            a, b = (pd.read_parquet(p) for p in paths)
            d = a[k + ["pt", "pl", "outros"]].merge(b[k + ["pt", "pl"]], on=k, suffixes=("1", "2"))
            d = d[(d.pt1 + d.pl1 + d.outros > 0) & (d.pt2 + d.pl2 > 0)]
            betas.append(fit_pivot(d.pt1, d.pl1, d.outros, d.uf, d.pt2, d.pl2).tolist())
    if betas:
        pivot_path = os.path.join(os.path.dirname(os.path.abspath(out)), "pivot_transfer.json")
        json.dump({"beta": np.mean(betas, axis=0).tolist(), "by_year": dict(zip(["2018", "2022"], betas)),
                   "note": "section_model.pivot_features -> runoff log(PT/PL), fitted per election on seções"},
                  open(pivot_path, "w"), indent=1)
        print(f"wrote {pivot_path}")

    perfil = os.path.join(data_dir, "perfil_2026.parquet")  # replay/prep_perfil.py: electorate profile per seção
    if os.path.exists(perfil):
        p = pd.read_parquet(perfil)
        sec = sec.merge(p[["uf", "cd_mun", "zona", "secao", "eleitores_perfil", *DEMO_COLS]],
                        on=["uf", "cd_mun", "zona", "secao"], how="left")
        for c in DEMO_COLS:
            sec[c] = sec[c].round(4)

    first = os.path.join(data_dir, "secoes_2026_1t.parquet")
    if os.path.exists(first):
        r1 = pd.read_parquet(first)[["uf", "cd_mun", "zona", "secao", "pt", "pl", "outros"]]
        sec = sec.merge(r1.rename(columns={"pt": "r1_pt", "pl": "r1_pl", "outros": "r1_outros"}),
                        on=["uf", "cd_mun", "zona", "secao"], how="left")

    sec.to_csv(out, index=False)
    w = sec.aptos / sec.aptos.sum()
    print(f"{len(sec)} seções, {sec.aptos.sum()} eleitores; match by electorate: "
          + ", ".join(f"{k} {100 * w[sec.match == k].sum():.1f}%" for k in ("exact", "geo", "key", "zona", "mun", "uf")))


if __name__ == "__main__":
    main()
