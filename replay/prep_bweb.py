"""Compact per-section presidential table from TSE's boletim-de-urna (bweb) zips.

Usage: python3 replay/prep_bweb.py <bweb_dir> <year> <out_dir>
Reads every bweb_{1t|2t}_{UF}_*.zip in <bweb_dir> and writes <out_dir>/secoes_{year}_{turno}t.parquet, one row
per seção: uf, cd_mun, nm_mun, zona, secao, local, recebido (BU receipt time), aptos, comparecimento,
pt, pl, outros (other nominal), brancos, nulos. PT/PL are identified by party number (13/22).
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


def parse_zip(path):
    rows = defaultdict(lambda: {"pt": 0, "pl": 0, "outros": 0, "brancos": 0, "nulos": 0})
    meta = {}
    with zipfile.ZipFile(path) as z:
        member = next(n for n in z.namelist() if n.endswith(".csv"))
        with z.open(member) as f:
            for r in csv.DictReader(io.TextIOWrapper(f, encoding="latin-1"), delimiter=";"):
                if r["DS_CARGO_PERGUNTA"] != "Presidente":
                    continue
                key = (r["SG_UF"], int(r["CD_MUNICIPIO"]), int(r["NR_ZONA"]), int(r["NR_SECAO"]))
                if key not in meta:
                    meta[key] = (r["NM_MUNICIPIO"], int(r["NR_LOCAL_VOTACAO"]), r["DT_BU_RECEBIDO"],
                                 int(r["QT_APTOS"]), int(r["QT_COMPARECIMENTO"]), int(r["NR_TURNO"]))
                v, n = int(r["QT_VOTOS"]), r["NR_VOTAVEL"]
                d = rows[key]
                if n == "13":
                    d["pt"] += v
                elif n == "22":
                    d["pl"] += v
                elif n == "95":
                    d["brancos"] += v
                elif n == "96":
                    d["nulos"] += v
                else:
                    d["outros"] += v
    out = []
    for key, d in rows.items():
        nm, local, rec, aptos, comp, turno = meta[key]
        out.append(dict(uf=key[0], cd_mun=key[1], nm_mun=nm, zona=key[2], secao=key[3], local=local,
                        recebido=rec, aptos=aptos, comparecimento=comp, turno=turno, **d))
    print(os.path.basename(path), len(out), flush=True)
    return out


def main():
    bweb_dir, year, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out_dir, exist_ok=True)
    for turno in (1, 2):
        files = sorted(glob.glob(os.path.join(bweb_dir, f"bweb_{turno}t_*.zip")))
        if not files:
            continue
        with ProcessPoolExecutor() as ex:
            df = pd.DataFrame([r for part in ex.map(parse_zip, files) for r in part])
        df["recebido"] = pd.to_datetime(df["recebido"], format="%d/%m/%Y %H:%M:%S")
        df.to_parquet(os.path.join(out_dir, f"secoes_{year}_{turno}t.parquet"), index=False)
        print(f"turno {turno}: {len(df)} seções, PT {df.pt.sum()}, PL {df.pl.sum()}, "
              f"válidos {(df.pt + df.pl + df.outros).sum()}")


if __name__ == "__main__":
    main()
