"""Compact per-section presidential table from TSE's votacao_secao + detalhe_votacao_secao archives.

Usage: python3 replay/prep_secao.py <dir> <year> <pt_number> <right_number> <out_dir>
  <dir> holds votacao_secao_{year}_{UF}.zip (all UFs) and detalhe_votacao_secao_{year}.zip.
Writes <out_dir>/secoes_{year}_{turno}t.parquet with the same vote columns as prep_bweb.py (pt, pl = the
Bolsonaro-lineage candidate, outros, brancos, nulos) plus aptos, comparecimento, local name and address, and
the TSE receipt / first-totalization timestamps from the detalhe file (2018 on; earlier files have no timestamps,
names or addresses, left empty). Used for base elections and, from 2018, as a replay night.
"""
import csv
import glob
import io
import os
import sys
import zipfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

PT, PL = None, None


def parse_votes(path):
    votes = defaultdict(lambda: [0, 0, 0, 0, 0])  # pt, pl, outros, brancos, nulos
    with zipfile.ZipFile(path) as z:
        member = next(n for n in z.namelist() if n.endswith(".csv"))
        with z.open(member) as f:
            for r in csv.DictReader(io.TextIOWrapper(f, encoding="latin-1"), delimiter=";"):
                if r["CD_CARGO"] != "1":
                    continue
                key = (int(r["NR_TURNO"]), r["SG_UF"], int(r["CD_MUNICIPIO"]), int(r["NR_ZONA"]), int(r["NR_SECAO"]))
                n, v = r["NR_VOTAVEL"], int(r["QT_VOTOS"])
                idx = 0 if n == PT else 1 if n == PL else 3 if n == "95" else 4 if n == "96" else 2
                votes[key][idx] += v
    print(os.path.basename(path), len(votes), flush=True)
    return dict(votes)


def init(pt, pl):
    global PT, PL
    PT, PL = pt, pl


def main():
    src, year, pt, pl, out_dir = sys.argv[1:6]
    files = sorted(glob.glob(os.path.join(src, f"votacao_secao_{year}_*.zip")))
    votes = {}
    with ProcessPoolExecutor(initializer=init, initargs=(pt, pl)) as ex:
        for part in ex.map(parse_votes, files):
            votes.update(part)

    rows = []
    with zipfile.ZipFile(os.path.join(src, f"detalhe_votacao_secao_{year}.zip")) as z:
        member = next(n for n in z.namelist() if n.endswith("_BR.csv"))
        with z.open(member) as f:
            for r in csv.DictReader(io.TextIOWrapper(f, encoding="latin-1"), delimiter=";"):
                if r["CD_CARGO"] != "1":
                    continue
                key = (int(r["NR_TURNO"]), r["SG_UF"], int(r["CD_MUNICIPIO"]), int(r["NR_ZONA"]), int(r["NR_SECAO"]))
                v = votes.get(key)
                if v is None:
                    continue
                rows.append(dict(turno=key[0], uf=key[1], cd_mun=key[2], nm_mun=r["NM_MUNICIPIO"], zona=key[3],
                                 secao=key[4], local=int(r["NR_LOCAL_VOTACAO"]), nm_local=r.get("NM_LOCAL_VOTACAO"),
                                 endereco=r.get("DS_LOCAL_VOTACAO_ENDERECO"), aptos=int(r["QT_APTOS"]),
                                 comparecimento=int(r["QT_COMPARECIMENTO"]), recebido=r.get("DT_RECEBIMENTO_BU_HOR_TSE"),
                                 totalizado=r.get("DT_PRIM_TOT_PARCIAL_HOR_TSE"),
                                 pt=v[0], pl=v[1], outros=v[2], brancos=v[3], nulos=v[4]))
    df = pd.DataFrame(rows)
    for c in ("recebido", "totalizado"):
        df[c] = pd.to_datetime(df[c], format="%d/%m/%Y %H:%M:%S", errors="coerce")
    print(f"{len(votes)} seções with votes, {len(df)} matched to detalhe")
    for turno, d in df.groupby("turno"):
        d.to_parquet(os.path.join(out_dir, f"secoes_{year}_{turno}t.parquet"), index=False)
        val = (d.pt + d.pl + d.outros).sum()
        print(f"turno {turno}: {len(d)} seções, PT {100 * d.pt.sum() / val:.2f}%, right {100 * d.pl.sum() / val:.2f}%")


if __name__ == "__main__":
    main()
