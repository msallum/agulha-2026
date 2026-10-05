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
`PIVOT_YEARS=2018` makes the runoff replay use the pivot base (below). `replay_2026.py` replays the 2026 first round from our own 4 Oct município snapshots (see its docstring).

## Models

- **mun**: port of the live model's point estimate and SE (`poll_live_results.R`). Each município's counted seções are compared with the whole município's 2018 margin, shrunk by região imediata, and the swing is applied to the uncounted base votes.
- **sec**: section-level model in the style of projecao.2026elections.
  - **Base:** each 2022 seção takes the 2018 result of the same polling place (município, zona, local number). This covers 99.1% of valid votes; the rest fall back to their município.
  - **Swing:** log-ratios, PT and PL vs. others in the first round, PT vs. PL in the runoff. It is measured between counted seções and their own base.
  - **Fit:** weighted regression on the base profile (shares and log-ratios) and município size, plus empirical-Bayes residual effects by região > UF > município.
  - **Pending seções:** base + predicted swing, with valid votes = electorate × base valid-vote rate × a UF turnout adjustment.
  - **Band:** 90% band from a Poisson município bootstrap, calibrated below.

## 2022 results, first pass (per-seção counts)

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

## Live version: município units (`section_model.py`)

Live, TSE publishes município totals, not per-seção votes, plus a per-UF seção configuration that marks which seções are totalized. So the live model's units are the counted part of each município, compared with the base of exactly those seções (`agg` in `replay.py`). Three fixes came out of replaying it:

1. **Per-seção base weights.** Each seção enters its unit's base with its own expected votes, not its polling place's raw base, which every seção of the place shares. The raw version over-weighted big polling places and left a +0.2pp bias in the late count.
2. **Exact per-seção shift.** A unit's swing is the log-ratio shift that, applied to each of its seções, reproduces the unit's counted shares, rather than the shift of the aggregate.
3. **Size-aware município shrinkage.** With one unit per município there is no within-município scatter, so the município effect is shrunk by `tau2 / (tau2 + s2 / votes)`. Before this, a município with one 42-vote seção counted (37 Lula, 4 Bolsonaro) moved the national 1st-round projection by +0.3pp.

**Runoff pivot.** Each seção's base can instead be its own same-year first round, put through the historical first-round → runoff transfer: a regression of the seção's runoff log(PT/PL) on its first-round log(PT/PL), the others' share, their interaction and square, and a region-specific transfer (`section_model.fit_pivot`).
- Fitted on 2018 and applied to 2022, its pre-count level is off by +3pp, because others' voters split differently each year; the count fixes that in minutes.
- Its seção-level shape is strong (R² 0.955).
- The previous runoff's polling-place result stays in as an extra covariate.
- A first round synthesized from zona totals does not work: errors reach +1.5pp, worse than the previous-runoff base. The pivot needs real per-seção first-round votes.

Error of the projected PT−PL margin (pp) and the calibrated 90% band, live version:

| seções counted | 2022 1t, old município model | 2022 1t, section | runoff, old município model | runoff, section, previous-runoff base | runoff, section, pivot base |
|---:|---:|---:|---:|---:|---:|
| 1% | +2.32 | −0.11 ±2.23 | +5.84 | +3.62 ±3.41 | +0.41 ±0.96 |
| 2% | +0.02 | −0.95 ±1.95 | +3.18 | +1.02 ±2.10 | +0.35 ±0.90 |
| 3% | −1.08 | −0.74 ±1.72 | +1.50 | +0.80 ±1.70 | +0.20 ±0.93 |
| 5% | −2.43 | −0.33 ±1.15 | −0.98 | +0.31 ±1.34 | +0.07 ±0.86 |
| 10% | −3.06 | +0.17 ±1.00 | −2.90 | +0.22 ±1.03 | +0.22 ±0.80 |
| 20% | −2.78 | +0.24 ±0.80 | −3.26 | +0.30 ±0.75 | +0.20 ±0.70 |
| 30% | −2.33 | +0.32 ±0.67 | −3.15 | +0.21 ±0.66 | +0.16 ±0.61 |
| 50% | −1.49 | +0.34 ±0.50 | −2.14 | +0.09 ±0.46 | +0.09 ±0.43 |
| 70% | −0.80 | +0.29 ±0.30 | −1.73 | +0.05 ±0.28 | +0.04 ±0.26 |
| 90% | −0.48 | +0.16 ±0.09 | −0.80 | +0.02 ±0.10 | +0.01 ±0.08 |

2026 first round, from our live snapshots of 4 Oct (2022 polling-place base, 93% exact matches). Errors are vs. the final 45.16 / 47.03; the competitor column is projecao.2026elections' own live projection:

| time | seções | Lula share error | margin error, 90% band | competitor, Lula share error |
|---|---:|---:|---:|---:|
| 17:35 | 3% | +0.73 | +1.10 ±1.15 | +0.57 |
| 17:47 | 6% | +0.57 | +0.87 ±1.07 | −0.07 |
| 18:00 | 11% | +0.41 | +0.55 ±1.07 | +0.13 |
| 18:19 | 20% | +0.33 | +0.42 ±0.96 | −0.03 |
| 18:51 | 50% | +0.19 | +0.29 ±0.52 | +0.02 |
| 19:14 | 69% | +0.13 | +0.21 ±0.31 | −0.04 |
| 20:06 | 85% | +0.04 | +0.07 ±0.15 | +0.00 |

That night the live município model was off by 1–3pp. The section model is a large improvement, but still trails the competitor by a few tenths mid-count. Both 1st-round nights show the same residual pro-Lula lean: late seções inside partly counted cities lean further right than their base and their city's early swing predict. It is present, at about half the size, even with per-seção data.

**Band.** Half-width = `sqrt((1.645 * bootstrap_sd)^2 + (0.85pp * share uncounted)^2)` (`BOOT_SCALE`, `FLOOR_PP`). It was fitted on the 2022 nights. Coverage of the final margin at checkpoints from 1% counted:
- 2026 1st round (not used in the fit): 97%.
- 2022 runoff, pivot base: 100%; previous-runoff base: 95%.
- 2022 1st round: 80%; the misses are at 80–98% counted, where the residual lean outlasts the band.

## Why the competitor is ahead on 2026 (data vs. model)

- **Data: not the cause.** Our counted Lula share matches projecao.2026elections' within ±0.05pp at every checkpoint, so the gap is the model.
- **Município effect: not the cause.** Projecting pending seções from their own profile only, as their method page describes, makes ours worse: +0.78 vs. +0.57 Lula-share error at 17:47.
- **State-varying coefficients: most of the 2026 gap.** Their regression coefficients vary Brazil → região → UF; ours are national. `SectionModel.slope_prior_votes` (off by default) fits each região's, then each UF's, coefficients as a ridge toward the level above.
  - **2026 1st round, at about 10,000 votes' worth of pull:** Lula-share error falls to +0.09 / +0.11 / +0.06 at 17:47 / 18:19 / 18:51, from +0.57 / +0.33 / +0.19.
  - **2022 1st round, same settings:** margin error goes from +0.32 to +0.43–0.49 at 30–50% counted.
  - **2022 runoff, fallback base:** stronger settings went down to −1.1pp at 2–5% counted (the two weakest crashed on this night, now fixed).
  - **2022 runoff, pivot base:** mixed — better at 1–3% counted, slightly worse from 5–10%.
- **Status:** not enabled. Choose the strength on all four nights jointly before using it.
