"""Per-seção electorate profile (sex, age, education) from TSE's perfil_eleitor_secao files.

Usage: python3 replay/prep_perfil.py <dir> <year> <out_dir>
  <dir> holds p_{UF}.zip = cdn.tse.jus.br/estatistica/sead/odsele/perfil_eleitor_secao/perfil_eleitor_secao_{year}_{UF}.zip
Writes <out_dir>/perfil_{year}.parquet: one row per (uf, cd_mun, zona, secao) with the electorate and the shares of
women, ages 16-24 / 25-39 / 60+, low education (illiterate to incomplete primary) and higher education (some college
or more). These are the demographic covariates of section_model.DEMO_COLS.
"""
import glob
import os
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

LOW_EDU = {"ANALFABETO", "LÊ E ESCREVE", "ENSINO FUNDAMENTAL INCOMPLETO"}
HIGH_EDU = {"SUPERIOR INCOMPLETO", "SUPERIOR COMPLETO"}


def age_group(label):
    s = label.strip()
    if s.startswith("Inválid") or not s[:2].isdigit():
        return "other"
    lo = int(s[:2])
    return "a16_24" if lo < 25 else "a25_39" if lo < 40 else "a60p" if lo >= 60 else "a40_59"


def one(path):
    cols = ["SG_UF", "CD_MUNICIPIO", "NR_ZONA", "NR_SECAO", "DS_GENERO", "DS_FAIXA_ETARIA", "DS_GRAU_ESCOLARIDADE"]
    out = []
    with zipfile.ZipFile(path) as z:
        member = next(n for n in z.namelist() if n.endswith(".csv"))
        header = z.open(member).readline().decode("latin-1")
        qt = "QT_ELEITORES_PERFIL" if "QT_ELEITORES_PERFIL" in header else "QT_ELEITORES"  # renamed in 2026
        for d in pd.read_csv(z.open(member), sep=";", encoding="latin-1", usecols=cols + [qt], dtype=str,
                             chunksize=2_000_000):
            n = pd.to_numeric(d[qt], errors="coerce").fillna(0)
            age = d.DS_FAIXA_ETARIA.map(age_group)
            f = pd.DataFrame({"uf": d.SG_UF, "cd_mun": d.CD_MUNICIPIO.astype(int), "zona": d.NR_ZONA.astype(int),
                              "secao": d.NR_SECAO.astype(int), "n": n,
                              "fem": n * (d.DS_GENERO == "FEMININO"),
                              "a16_24": n * (age == "a16_24"), "a25_39": n * (age == "a25_39"), "a60p": n * (age == "a60p"),
                              "edu_low": n * d.DS_GRAU_ESCOLARIDADE.isin(LOW_EDU),
                              "edu_high": n * d.DS_GRAU_ESCOLARIDADE.isin(HIGH_EDU)})
            out.append(f.groupby(["uf", "cd_mun", "zona", "secao"], as_index=False).sum())
    g = pd.concat(out).groupby(["uf", "cd_mun", "zona", "secao"], as_index=False).sum()
    print(os.path.basename(path), len(g), flush=True)
    return g


def main():
    src, year, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    with ProcessPoolExecutor(4) as ex:
        g = pd.concat(ex.map(one, sorted(glob.glob(os.path.join(src, "p_*.zip")))))
    for c in ("fem", "a16_24", "a25_39", "a60p", "edu_low", "edu_high"):
        g[c] = g[c] / g.n.where(g.n > 0)
    g = g.rename(columns={"n": "eleitores_perfil"})
    g.to_parquet(os.path.join(out_dir, f"perfil_{year}.parquet"), index=False)
    w = g.eleitores_perfil
    print(f"{year}: {len(g)} seções, {int(w.sum())} eleitores; national shares: "
          + ", ".join(f"{c} {(g[c] * w).sum() / w.sum():.3f}" for c in ("fem", "a16_24", "a25_39", "a60p", "edu_low", "edu_high")))


if __name__ == "__main__":
    main()
