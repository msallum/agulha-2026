# Agulha 2026

Live probabilistic "needle" for Brazil's 2026 presidential election, tracking Lula-lineage vs. Bolsonaro-lineage vote share against the 2018 and 2022 baselines as TSE results come in on election night.

Dashboards (GitHub Pages):
- Runoff, live: https://msallum.github.io/agulha-2026/2turno/ (updated automatically by the election-night workflow)
- First round, final (frozen): https://msallum.github.io/agulha-2026/

## What this is

A side project, separate from the author's FGV-EPGE thesis. Fetches TSE's live per-município results, projects the national outcome with a regionally-correlated hierarchical model (Monte Carlo posterior propagation over the between/within-região variance components, doubled-SE hedge), and publishes each update to a static dashboard on GitHub Pages.

## Files

- `section_model.py` — the section-level projection model (the headline number since the runoff). Each seção is compared with its own polling place in a base election rather than with its whole município; see the module docstring and `replay/README.md` for the method and its validation.
- `live_section_model.py` — runs the section model after each cycle. It works out which seções each município's counted votes cover from TSE's seção configuration (`arquivo-urna/.../config/{uf}/...-cs.json`), projects the national and per-UF result, and adds it to the summary under `section_model`.
- `build_section_base.py` — builds `section_base_2026.csv.gz`, matching every 2026 seção to its 2022 polling place (93% exact by electorate), with the 2022 first-round and runoff votes there and the seção's own 2026 first-round turnout. Also writes `pivot_transfer.json`, the first-round → runoff transfer function fitted on 2018 and 2022 seções. Rebuild once TSE publishes 2026 per-seção votes (bweb / votacao_secao) so the runoff can use the 2026 first round of each seção as its base.
- `replay/` — replays of past election nights in true arrival order, used to validate and calibrate both models.
- `poll_live_results.R` — one fetch+estimate cycle: pulls every município's live result from TSE, computes the swing vs. baseline, and writes `live_needle_summary_latest.json` (+ a history CSV, + a full snapshot CSV).
- `run_polling_schedule.R` — wraps the above in a tiered polling loop calibrated to how Brazilian election nights actually unfold (steep ramp in the first ~3h, long unpredictable tail after) — see the header comments in each file for the full rationale and tuning history.
- `site/index.html` — the first-round dashboard, frozen with its final data in `site/data/`.
- `site/2turno/index.html` — the runoff dashboard. Reads `data/needle.json` and `data/history.json` (from the `live-data` branch) and refreshes every 30s.
- `scripts/publish.sh`, `scripts/update_site_data.py` — after each cycle, run the section model, then write those two JSON files to the `live-data` branch and trigger a site redeploy.
- `.github/workflows/election-night.yml` — runs the polling loop on GitHub Actions (up to 6h per run), publishing after every cycle.
- `.github/workflows/pages.yml` — deploys `site/` plus the latest `live-data` JSON to GitHub Pages.
- `build_runoff_baseline.py` — builds the runoff baseline (`historical_baseline_municipio_2turno.csv`, 2018 and 2022 runoffs) and its regional prior (`regional_correlation_prior_2turno.json`) straight from TSE's public archives. Runnable from this repo (Python standard library only).
- `build_historical_baseline.R` — reference only, **not runnable from this repo**: needs the full raw multi-election TSE archive, which lives in the (separate, private) thesis project, not here. Produces the three data files below.
- `historical_baseline_municipio.csv`, `municipio_regional_clusters.csv`, `regional_correlation_prior.json` — small derived data files `poll_live_results.R` actually reads at runtime.
- `simulate_*.R` — synthetic test scenarios used to validate the model end-to-end before relying on it live.

## Running it

Requires R with `tidyverse`, `jsonlite`, `curl`, `readr`, `tibble` installed, and Python 3 with `numpy` and `pandas` for the section model. From this repo's root:

```
Rscript poll_live_results.R          # one cycle
Rscript run_polling_schedule.R       # tiered loop through election night
python3 live_section_model.py        # section model on the latest cycle (publish.sh runs it automatically)
```

Locally, neither script publishes anything unless `POST_CYCLE_CMD` is set.

`ROUND` (env var, default `1`) selects the round: `2` uses the runoff's election code and the runoff baseline and prior. `USE_SIMULADO` (env var, default `TRUE`) chooses TSE's public rehearsal environment; set `USE_SIMULADO=FALSE` for the real election.

## Election night on GitHub Actions

Run the **Election night** workflow (Actions tab → Election night → Run workflow, or `gh workflow run election-night.yml`). It installs R, runs `run_polling_schedule.R` against TSE, and after every cycle `scripts/publish.sh` commits the fresh JSON to the `live-data` branch and redeploys the site. Inputs: `round` (default `2`), `use_simulado`, `polls_close_time` (the cadence is timed from it) and `max_iterations` (for short test runs). A single run lasts at most 6 hours; start a second one if the count runs longer.
