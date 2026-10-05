"""Build the runoff (2º turno) baseline and regional prior from TSE's public archives.

Outputs, alongside the first-round files and in the same formats:
  historical_baseline_municipio_2turno.csv  per-município PT and Bolsonaro shares, 2018 and 2022 runoffs
  regional_correlation_prior_2turno.json    between/within-região variance of the 2018->2022 runoff margin swing

Reads only the presidential rows from cdn.tse.jus.br's votacao_candidato_munzona zips, fetching just the
needed zip members with HTTP range requests (the full archives are 0.4-0.6 GB each). Standard library only.
Run from the repo root: python3 build_runoff_baseline.py
"""
import csv
import io
import json
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime

URL = "https://cdn.tse.jus.br/estatistica/sead/odsele/votacao_candidato_munzona/votacao_candidato_munzona_{}.zip"
CANDIDATES = {  # year -> (PT candidate urna name, label), (Bolsonaro candidate urna name, label)
    2018: (("FERNANDO HADDAD", "Haddad"), ("JAIR BOLSONARO", "Bolsonaro")),
    2022: (("LULA", "Lula"), ("JAIR BOLSONARO", "Bolsonaro")),
}


class HttpRangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP range requests, so zipfile can read single members."""

    def __init__(self, url):
        self.url = url
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as r:
            self.size = int(r.headers["Content-Length"])
        self.pos = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
        return self.pos

    def readinto(self, b):
        if self.pos >= self.size or len(b) == 0:
            return 0
        end = min(self.pos + len(b), self.size) - 1
        req = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{end}"})
        with urllib.request.urlopen(req, timeout=300) as r:
            data = r.read()
        b[: len(data)] = data
        self.pos += len(data)
        return len(data)


def runoff_votes(year):
    """{codigo_tse: {urna_name: votes}} for the presidential runoff, Brazil only (abroad/ZZ excluded)."""
    z = zipfile.ZipFile(io.BufferedReader(HttpRangeFile(URL.format(year)), buffer_size=1 << 20))
    names = {i.filename for i in z.infolist()}
    for member in (f"votacao_candidato_munzona_{year}_BR.csv", f"votacao_candidato_munzona_{year}_BRASIL.csv"):
        if member not in names:
            continue
        votes = defaultdict(lambda: defaultdict(int))
        with z.open(member) as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="latin-1"), delimiter=";"):
                if row["DS_CARGO"].strip().upper() != "PRESIDENTE" or row["NR_TURNO"] != "2":
                    continue
                if row["SG_UF"].strip().upper() == "ZZ":
                    continue
                col = "QT_VOTOS_NOMINAIS_VALIDOS" if row.get("QT_VOTOS_NOMINAIS_VALIDOS") not in (None, "") else "QT_VOTOS_NOMINAIS"
                votes[row["CD_MUNICIPIO"].zfill(5)][row["NM_URNA_CANDIDATO"].strip()] += int(row[col] or 0)
        if votes:
            print(f"{year}: {len(votes)} municípios from {member}")
            return votes
    raise RuntimeError(f"{year}: no presidential runoff rows found")


def weighted_mean(xs, ws):
    return sum(x * w for x, w in zip(xs, ws)) / sum(ws)


def main():
    clusters = {r["codigo_tse"]: r for r in csv.DictReader(open("municipio_regional_clusters.csv", encoding="utf-8"))}

    rows = []
    margin = defaultdict(dict)  # codigo_tse -> {year: (margin, valid votes)}
    for year, ((pt_name, pt_label), (bo_name, bo_label)) in CANDIDATES.items():
        for code, by_cand in runoff_votes(year).items():
            total = sum(by_cand.values())
            if total == 0:
                continue
            info = clusters.get(code, {})
            for linhagem, name, label in (("PT", pt_name, pt_label), ("Bolsonaro", bo_name, bo_label)):
                v = by_cand.get(name, 0)
                rows.append({"codigo_tse": code, "uf": info.get("uf", ""), "nome_municipio": info.get("nome_municipio", ""),
                             "codigo_ibge": info.get("codigo_ibge", ""), "ano": year, "linhagem": linhagem,
                             "candidato": label, "votos_validos_total": total, "votos_candidato": v, "share": v / total})
            margin[code][year] = ((by_cand.get(pt_name, 0) - by_cand.get(bo_name, 0)) / total, total)

    with open("historical_baseline_municipio_2turno.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    for year in CANDIDATES:
        pt = sum(r["votos_candidato"] for r in rows if r["ano"] == year and r["linhagem"] == "PT")
        tot = sum(r["votos_validos_total"] for r in rows if r["ano"] == year and r["linhagem"] == "PT")
        print(f"{year} runoff, PT national share (sanity check): {100 * pt / tot:.2f}%")

    # Same weighted one-way ANOVA decomposition as build_historical_baseline.R, on runoff margin swings.
    data = [(c, m[2022][0] - m[2018][0], m[2022][1], clusters[c]["cd_rgi"])
            for c, m in margin.items() if 2018 in m and 2022 in m and clusters.get(c, {}).get("cd_rgi")]
    swings, weights = [d[1] for d in data], [d[2] for d in data]
    grand = weighted_mean(swings, weights)
    by_rgi = defaultdict(list)
    for _, s, wt, rgi in data:
        by_rgi[rgi].append((s, wt))
    stats = {}
    for rgi, members in by_rgi.items():
        wsum = sum(wt for _, wt in members)
        stats[rgi] = (weighted_mean([s for s, _ in members], [wt for _, wt in members]), wsum,
                      wsum ** 2 / sum(wt ** 2 for _, wt in members))
    total_w = sum(st[1] for st in stats.values())
    between_raw = sum(st[1] * (st[0] - grand) ** 2 for st in stats.values()) / total_w
    within = sum(wt * (s - stats[rgi][0]) ** 2 for _, s, wt, rgi in data) / sum(weights)
    bias = sum(st[1] / st[2] for st in stats.values()) / total_w
    between = max(0.0, between_raw - within * bias)
    prior = {
        "prior_sigma2_between_rgi": between,
        "prior_sigma2_within_rgi": within,
        "prior_rho_rgi": between / (between + within),
        "n_clusters_used": len(stats),
        "n_municipios_used": len(data),
        "built": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "Weighted one-way ANOVA decomposition of the 2018->2022 município-level RUNOFF margin swing, "
                "by IBGE regiao imediata (CD_RGI). Runoff counterpart of regional_correlation_prior.json.",
    }
    json.dump(prior, open("regional_correlation_prior_2turno.json", "w"), indent=2)
    print(f"runoff prior: sd within {100 * within ** 0.5:.2f}pp, sd between {100 * between ** 0.5:.2f}pp, "
          f"rho {prior['prior_rho_rgi']:.3f}, {len(stats)} regiões, {len(data)} municípios")


if __name__ == "__main__":
    main()
