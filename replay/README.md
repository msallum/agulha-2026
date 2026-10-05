# Election-night replays

Replays a past election night seção by seção, in the order the boletins de urna actually reached the TSE, and scores two projection models at each checkpoint against the final result.

## Data (TSE open data, cdn.tse.jus.br)

- Target night: `eleicoes/eleicoes2022/buweb/bweb_{1t|2t}_{UF}_{051020221321|311020221535}.zip` (56 files, 1.5 GB). Per-seção votes plus `DT_BU_RECEBIDO`, the BU receipt time (Brasília). `prep_bweb.py` turns them into `secoes_2022_{1,2}t.parquet` and reproduces the official totals exactly (1st round 48.43 / 43.20, runoff 50.90 / 49.10).
- Base election: `odsele/votacao_secao/votacao_secao_2018_BR.zip` (presidential votes per seção; the per-UF files have no presidential rows) and `odsele/detalhe_votacao_secao/detalhe_votacao_secao_2018.zip` (electorate, turnout, polling place). `prep_secao.py` builds `secoes_2018_{1,2}t.parquet` and also reproduces the official totals (29.28 / 46.03; 44.87 / 55.13).

```
python3 replay/prep_bweb.py  <bweb_dir> 2022 <data_dir>
python3 replay/prep_secao.py <secao_dir> 2018 13 17 <data_dir>
python3 replay/replay.py     <data_dir> 2022 2018 {1|2} [n_boot]
```

Results are released at 17:00, so BUs received earlier (abroad) enter at 17:00. Requires pandas, numpy and pyarrow.

## Models

- **mun**: port of the live model's point estimate and SE (`poll_live_results.R`). Each município's counted seções are compared with the whole município's 2018 margin, shrunk by região imediata, and the swing is applied to the uncounted base votes.
- **sec**: section-level model in the style of projecao.2026elections.
  - **Base:** each 2022 seção takes the 2018 result of the same polling place (município, zona, local number). This covers 99.1% of valid votes; the rest fall back to their município.
  - **Swing:** log-ratios, PT and PL vs. others in the first round, PT vs. PL in the runoff. It is measured between counted seções and their own base.
  - **Fit:** weighted regression on the base profile (shares and log-ratios) and município size, plus empirical-Bayes residual effects by região > UF > município.
  - **Pending seções:** base + predicted swing, with valid votes = electorate × base valid-vote rate × a UF turnout adjustment.
  - **Band:** 90% band from a Poisson município bootstrap, calibrated below.

## 2022 results

Error of the projected PT−PL margin vs. the final result, in pp (positive = too pro-Lula):

| seções counted | 1st round mun | 1st round sec | runoff mun | runoff sec |
|---:|---:|---:|---:|---:|
| 1% | +2.32 | −1.37 | +5.84 | +2.20 |
| 2% | +0.02 | −0.75 | +3.18 | +0.99 |
| 3% | −1.08 | −0.15 | +1.50 | +0.72 |
| 5% | −2.43 | +0.07 | −0.98 | +0.48 |
| 10% | −3.06 | +0.33 | −2.90 | +0.49 |
| 20% | −2.78 | +0.18 | −3.26 | +0.20 |
| 30% | −2.33 | +0.20 | −3.15 | +0.08 |
| 50% | −1.49 | +0.19 | −2.14 | +0.01 |
| 70% | −0.80 | +0.16 | −1.73 | +0.00 |
| 90% | −0.48 | +0.09 | −0.80 | +0.01 |

Lula's share error is about half the margin error: in the section model it stays within ±0.4pp from 3% counted in both rounds. The município model shows the same pro-Bolsonaro drift seen live on 4 Oct 2026, peaking around −3pp at 10–30% counted. The cause is comparing partly counted municípios with their whole-município base: the early seções inside each município are the more right-leaning ones.

Variants tried (`variants.py`-style sweep on both rounds):
- Polling-place or região-imediata effect levels: no gain.
- Region-specific slopes: worse in the 1st round.
- No covariates: +3–4pp bias.
- Log-ratio covariates: cut the 1st-round error at 5–30% from about 0.6–0.9pp to 0.1–0.3pp, at a small cost in the runoff.

**Band calibration.** The raw bootstrap 90% band covered the truth at only 79% of checkpoints. A half-width of

    sqrt((1.645 * 1.1 * bootstrap_sd)^2 + (0.35pp * share of votes uncounted)^2)

covers 38/42 checkpoints across both rounds. The misses are in the 1st round at 70–90% counted, where a +0.1–0.15pp residual bias outlasts the band. This is fitted on two nights with serially correlated checkpoints, so treat it as a first calibration.
