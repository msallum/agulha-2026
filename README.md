# Agulha 2026

Live probabilistic "needle" for Brazil's 2026 presidential election, tracking Lula-lineage vs. Bolsonaro-lineage vote share against the 2018 and 2022 baselines as TSE results come in on election night.

Dashboard: https://claude.ai/artifact/QxxKjtNTfT6giou7ocVACC

## What this is

A side project, separate from the author's FGV-EPGE thesis. Fetches TSE's live per-município results, projects the national outcome with a regionally-correlated hierarchical model (Monte Carlo posterior propagation over the between/within-região variance components, doubled-SE hedge), and pushes updates to a live dashboard.

## Files

- `poll_live_results.R` — one fetch+estimate cycle: pulls every município's live result from TSE, computes the swing vs. baseline, and writes `live_needle_summary_latest.json` (+ a history CSV, + a full snapshot CSV).
- `run_polling_schedule.R` — wraps the above in a tiered polling loop calibrated to how Brazilian election nights actually unfold (steep ramp in the first ~3h, long unpredictable tail after) — see the header comments in each file for the full rationale and tuning history.
- `dashboard.html` — the published Artifact's source (kept here for reference; the live page is the Artifact itself, not this file).
- `build_historical_baseline.R` — reference only, **not runnable from this repo**: needs the full raw multi-election TSE archive, which lives in the (separate, private) thesis project, not here. Produces the three data files below.
- `historical_baseline_municipio.csv`, `municipio_regional_clusters.csv`, `regional_correlation_prior.json` — small derived data files `poll_live_results.R` actually reads at runtime.
- `simulate_*.R` — synthetic test scenarios used to validate the model end-to-end before relying on it live.

## Running it

Requires R with `tidyverse`, `jsonlite`, `curl`, `readr`, `tibble` installed. From this repo's root:

```
Rscript poll_live_results.R          # one cycle
Rscript run_polling_schedule.R       # tiered loop through election night
```

Neither script pushes to the dashboard on its own — that's a separate step (currently manual/Claude-mediated; see project history for the automation discussion).

`use_simulado` in `poll_live_results.R` defaults to `TRUE` (tests against TSE's public rehearsal environment) — flip to `FALSE` for the real election, and confirm `BOLSONARO_URNA_NAME` against the real ballot name first.
