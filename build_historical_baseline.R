# build_historical_baseline.R
#
# Part of the "election needle" project: builds the municipality-level
# HISTORICAL baseline (2018 and 2022 first-round vote shares for Lula and
# Bolsonaro) that live 2026 results will be compared against on election
# night. This is the "2022 result, at the same geographic unit" side of
# the comparison -- the live-polling script (poll_live_results.R) builds
# the "2026 result, as it comes in" side, using the SAME município keys
# (TSE 5-digit codigo_tse) so the two join directly with no extra
# crosswalk work.
#
# WHY MUNICÍPIO LEVEL, NOT MUNICÍPIO x ZONA: TSE's live results system
# (confirmed 2026-09-24 by driving the actual resultados.tse.jus.br app
# and inspecting its network requests) serves one JSON file per município
# that is ALREADY aggregated across that município's zones -- there is no
# live per-zona file. So the historical side is aggregated to município
# level here too, for a clean, direct join (this is a coarser unit than
# the municipio x zona panel used elsewhere in this project, e.g.
# exploratory_regressions_demografia_voto.R, but it is the finest unit
# the live feed actually offers).
#
# CANDIDATE MAPPING (confirmed with Miguel, 2026-09-24, extended
# 2026-09-25): compare whichever person was each PARTY's presidential
# candidate in each cycle, not just a fixed name -- Jair Bolsonaro in
# 2018 and 2022, Flávio Bolsonaro in 2026, on one side; on the PT side,
# Fernando Haddad in 2018 (Lula himself was barred from running that
# year) and Lula in 2022 and again in 2026. This script builds the
# generic "candidate vote share by município" extractor so all of these
# comparisons (Lula/Haddad "PT lineage" 2026 vs 2022 vs 2018; Bolsonaro
# "family lineage" 2026 vs 2022 vs 2018) can be computed from the same
# output table -- each row is tagged with BOTH the actual person
# (`candidato`) and the lineage it belongs to (`linhagem`), so the
# live-polling script can join 2026's PT and Bolsonaro-family results
# against either baseline year for that lineage.

library(tidyverse)
library(jsonlite)

RAW <- "C:/Users/WIN10/Documents/tese/dados/raw"
OUT_DIR <- "C:/Users/WIN10/Documents/tese/dados/scripts/needle"

# TSE's own CD_MUNICIPIO export format is inconsistent across years
# (leading zeros sometimes stripped) -- same fix already established in
# exploratory_regressions_demografia_voto.R: normalize via as.integer()
# then re-pad to 5 digits, on both the vote data and the crosswalk.
crosswalk <- read_csv(file.path(RAW, "municipios_tse_ibge.csv"), col_types = cols(.default = "c")) %>%
  transmute(codigo_tse = sprintf("%05d", as.integer(codigo_tse)), uf, nome_municipio, codigo_ibge)

# Reads one year's round-1 presidential file and returns, per município,
# TOTAL valid votes and the named candidate's votes/share. `candidate_
# urna_name` must match NM_URNA_CANDIDATO exactly (the ballot name TSE
# actually publishes, e.g. "JAIR BOLSONARO", "LULA", "FERNANDO HADDAD")
# -- verified against the raw files before use (see header note /
# session log), not assumed. `linhagem` tags which "lineage" (PT vs.
# Bolsonaro family) this person's result belongs to, so the live script
# can join 2026's PT and Bolsonaro-family candidates against whichever
# year's PERSON actually ran for that side, while still grouping by
# lineage for the swing calculation.
read_candidate_share <- function(year, candidate_urna_name, candidate_label, linhagem) {
  path <- file.path(RAW, sprintf("tse_%d_votacao_candidato_munzona", year),
                     sprintf("votacao_candidato_munzona_%d_BR_presidente_utf8.csv", year))
  df <- read_delim(path, delim = ";", locale = locale(encoding = "utf-8"),
                    col_types = cols(.default = "c")) %>%
    mutate(CD_MUNICIPIO = sprintf("%05d", as.integer(CD_MUNICIPIO)))
  votecol <- if ("QT_VOTOS_NOMINAIS_VALIDOS" %in% names(df)) "QT_VOTOS_NOMINAIS_VALIDOS" else "QT_VOTOS_NOMINAIS"
  df <- df %>%
    filter(NR_TURNO == "1") %>%
    mutate(votos = as.numeric(.data[[votecol]]), votos = ifelse(is.na(votos), 0, votos))

  tot <- df %>% group_by(CD_MUNICIPIO) %>% summarise(votos_validos_total = sum(votos), .groups = "drop")
  cand <- df %>% filter(NM_URNA_CANDIDATO == candidate_urna_name) %>%
    group_by(CD_MUNICIPIO) %>% summarise(votos_candidato = sum(votos), .groups = "drop")

  tot %>% left_join(cand, by = "CD_MUNICIPIO") %>%
    mutate(votos_candidato = ifelse(is.na(votos_candidato), 0, votos_candidato),
           share = votos_candidato / votos_validos_total,
           ano = year, candidato = candidate_label, linhagem = linhagem,
           codigo_tse = CD_MUNICIPIO) %>%
    select(codigo_tse, ano, linhagem, candidato, votos_validos_total, votos_candidato, share)
}

# ---------------------------------------------------------------------
# Build the baseline: PT lineage (Haddad 2018, Lula 2022) and Bolsonaro
# lineage (Jair 2018, Jair 2022)
# ---------------------------------------------------------------------
baseline <- bind_rows(
  read_candidate_share(2018, "FERNANDO HADDAD", "Haddad", "PT"),
  read_candidate_share(2022, "LULA", "Lula", "PT"),
  read_candidate_share(2018, "JAIR BOLSONARO", "Bolsonaro", "Bolsonaro"),
  read_candidate_share(2022, "JAIR BOLSONARO", "Bolsonaro", "Bolsonaro")
)

cat("Baseline rows built:", nrow(baseline), "\n")
print(baseline %>% count(ano, linhagem, candidato))

baseline_wide <- baseline %>%
  left_join(crosswalk, by = "codigo_tse") %>%
  select(codigo_tse, uf, nome_municipio, codigo_ibge, ano, linhagem, candidato,
         votos_validos_total, votos_candidato, share)

match_rate <- baseline_wide %>% group_by(ano, linhagem, candidato) %>%
  summarise(n = n(), matched = sum(!is.na(codigo_ibge)), pct = round(100 * matched / n, 1), .groups = "drop")
cat("\nCrosswalk match rate:\n")
print(match_rate)

# Sanity check against known national results (unweighted-by-population
# municipality count, not the same as the real vote-weighted national
# share -- this is just a plausibility check, not meant to reproduce
# known real results exactly). Known figures for cross-check: Haddad
# 2018 29.28%, Lula 2022 48.43%, Jair Bolsonaro 2018 46.03%, Jair
# Bolsonaro 2022 43.20%.
cat("\nSanity check -- national vote-weighted share (should match known results closely):\n")
print(baseline_wide %>% group_by(ano, linhagem, candidato) %>%
        summarise(pct_nacional = round(100 * sum(votos_candidato) / sum(votos_validos_total), 2), .groups = "drop"))

write_csv(baseline_wide, file.path(OUT_DIR, "historical_baseline_municipio.csv"))
cat("\nSaved to", file.path(OUT_DIR, "historical_baseline_municipio.csv"), "\n")

# ---------------------------------------------------------------------
# Historical swing-volatility PRIOR (added 2026-09-25, for the live
# needle's probability/uncertainty model in poll_live_results.R).
#
# WHAT THIS IS: how much a município's PT-vs-Bolsonaro MARGIN (share_PT
# minus share_Bolsonaro) typically moved between two consecutive real
# elections (2018 -> 2022), vote-weighted. This is NOT a prediction of
# 2026 -- it's a baseline answer to "how noisy is município-level swing
# normally?", used as a Bayesian prior so the live needle's uncertainty
# estimate doesn't look artificially precise in the first minutes of
# election night, when only a handful of (non-representative) municípios
# have reported and an empirical variance computed from them alone would
# be unstable. As real 2026 results accumulate, the live model shrinks
# away from this prior toward the empirically observed 2026 dispersion
# (see PRIOR_STRENGTH_K0 in poll_live_results.R). It is also the
# reference point for a live "are municípios behaving unusually
# compared to history?" diagnostic: if 2026's own dispersion far
# exceeds this historical figure, that is reported directly, even when
# the vote-weighted average swing looks unremarkable.
#
# CAVEAT: 2018->2022 is a single historical interval, shaped by its own
# idiosyncratic context (pandemic, Bolsonaro's first term, etc.) -- it
# is a reasonable, defensible proxy for "typical" swing dispersion, not
# a guarantee that 2026 will behave similarly.
# ---------------------------------------------------------------------
wide <- baseline_wide %>%
  select(codigo_tse, ano, linhagem, share, votos_validos_total) %>%
  pivot_wider(id_cols = codigo_tse, names_from = c(linhagem, ano), values_from = c(share, votos_validos_total))

prior_data <- wide %>%
  filter(!is.na(share_PT_2018), !is.na(share_PT_2022),
         !is.na(share_Bolsonaro_2018), !is.na(share_Bolsonaro_2022)) %>%
  transmute(
    codigo_tse,
    margin_2018 = share_PT_2018 - share_Bolsonaro_2018,
    margin_2022 = share_PT_2022 - share_Bolsonaro_2022,
    margin_swing_2022_vs_2018 = margin_2022 - margin_2018,
    weight = votos_validos_total_PT_2022
  )

margin_mean <- weighted.mean(prior_data$margin_swing_2022_vs_2018, prior_data$weight)
margin_var <- sum(prior_data$weight * (prior_data$margin_swing_2022_vs_2018 - margin_mean)^2) / sum(prior_data$weight)

cat("\nHistorical município-level margin-swing prior (2022 vs 2018, vote-weighted):\n")
cat("  mean swing:", round(margin_mean * 100, 2), "p.p. (sanity check only, not itself used downstream)\n")
cat("  sd:", round(sqrt(margin_var) * 100, 2), "p.p. -- this is what the live needle treats as the PRIOR typical dispersion\n")
cat("  built from", nrow(prior_data), "municípios\n")

prior <- list(
  prior_sigma2_margin = margin_var,
  prior_sd_margin_pp = sqrt(margin_var) * 100,
  prior_mean_margin_swing_pp = margin_mean * 100,
  n_municipios_used = nrow(prior_data),
  built = format(Sys.time(), "%Y-%m-%d %H:%M:%S"),
  note = "Vote-weighted variance of (share_PT - share_Bolsonaro) swing between municipio-level 2018 and 2022 first-round results. Used by poll_live_results.R as a Bayesian shrinkage prior for the live swing-dispersion estimate."
)
write_json(prior, file.path(OUT_DIR, "swing_volatility_prior.json"), auto_unbox = TRUE, pretty = TRUE)
cat("\nSaved prior to", file.path(OUT_DIR, "swing_volatility_prior.json"), "\n")

# ---------------------------------------------------------------------
# Regional correlation structure (added 2026-09-25, per Miguel's request
# that the live model "maintain correlation between voting regions"
# instead of treating every still-uncounted município as an independent
# draw around the national swing).
#
# CLUSTERING VARIABLE: IBGE's "região geográfica imediata" (CD_RGI) --
# an official census geography, NOT something we invented, designed
# specifically to group each município with the handful of NEIGHBORING
# municípios it shares a local economy/media market/commuting zone with
# (Brazil has ~510 of these nationally, ~11 municípios each on average).
# This is the "Census info" half of what Miguel asked for; the "results
# themselves" half is the historical swing decomposition below.
#
# WHAT WE COMPUTE HERE: using the SAME 2018->2022 município-level margin
# swings behind the dispersion prior above, decompose their total
# variance into a BETWEEN-região component (how much régiao-imediata
# AVERAGES differ from the national average) and a WITHIN-região
# component (how much municípios inside the same região differ from
# their own região's average) -- a standard weighted one-way ANOVA /
# variance-components decomposition. Their ratio is the historical
# intraclass correlation (rho): how strongly neighboring municípios'
# swings actually moved together last time, versus independently.
# poll_live_results.R uses this as a PRIOR for that same decomposition,
# estimated fresh from whichever municípios have reported so far, so
# that (a) uncertainty properly WIDENS when reporting municípios so far
# are concentrated in a few regions rather than spread out nationally,
# and (b) still-uncounted municípios in a región that IS reporting get
# projected using that región's own observed swing (shrunk toward the
# national trend by how much data that región actually has), not a
# flat national number that ignores what its neighbors just did.
# ---------------------------------------------------------------------
census_basico_zip <- file.path(RAW, "ibge_censo2022_municipio", "Agregados_por_municipios_basico_BR_20260520.zip")
regioes <- read_delim(unz(census_basico_zip, "Agregados_por_municipios_basico_BR.csv"),
                       delim = ";", locale = locale(encoding = "latin1"),
                       col_types = cols(.default = "c")) %>%
  transmute(codigo_ibge = CD_MUN, cd_rgi = CD_RGI, nm_rgi = NM_RGI,
            cd_rgint = CD_RGINT, nm_rgint = NM_RGINT,
            populacao_2022 = as.numeric(v0001)) %>%
  distinct(codigo_ibge, .keep_all = TRUE)

regional_clusters <- crosswalk %>%
  left_join(regioes, by = "codigo_ibge") %>%
  select(codigo_tse, uf, nome_municipio, codigo_ibge, cd_rgi, nm_rgi, cd_rgint, nm_rgint, populacao_2022)

match_rate_rgi <- round(100 * mean(!is.na(regional_clusters$cd_rgi)), 1)
cat("\nRegional cluster (região imediata) match rate:", match_rate_rgi, "% of municípios\n")

write_csv(regional_clusters, file.path(OUT_DIR, "municipio_regional_clusters.csv"))
cat("Saved to", file.path(OUT_DIR, "municipio_regional_clusters.csv"), "\n")

anova_data <- prior_data %>%
  left_join(regional_clusters %>% select(codigo_tse, cd_rgi), by = "codigo_tse") %>%
  filter(!is.na(cd_rgi))

grand_mean <- weighted.mean(anova_data$margin_swing_2022_vs_2018, anova_data$weight)

cluster_stats <- anova_data %>%
  group_by(cd_rgi) %>%
  summarise(
    cluster_mean = weighted.mean(margin_swing_2022_vs_2018, weight),
    cluster_weight = sum(weight),
    n_eff_c = (sum(weight))^2 / sum(weight^2),
    n_mun = n(),
    .groups = "drop"
  )

sigma2_between_rgi_raw <- sum(cluster_stats$cluster_weight * (cluster_stats$cluster_mean - grand_mean)^2) / sum(cluster_stats$cluster_weight)

within_data <- anova_data %>%
  left_join(cluster_stats %>% select(cd_rgi, cluster_mean), by = "cd_rgi")
sigma2_within_rgi <- sum(within_data$weight * (within_data$margin_swing_2022_vs_2018 - within_data$cluster_mean)^2) / sum(within_data$weight)

# BIAS CORRECTION (added 2026-09-25, per Miguel's request): the raw
# between-cluster estimate above is the weighted variance of CLUSTER
# MEANS around the grand mean -- but even under the null hypothesis of
# NO true regional effect, a cluster's mean has its own sampling
# variance (Var(cluster_mean) = sigma2_within / n_eff_c, Kish's
# effective cluster size), which inflates the raw estimate above its
# true value. This is the classic small-group bias in one-way ANOVA
# variance-components estimation (E[MS_between] = sigma2_between_true +
# sigma2_within * E[1/n_eff_c], not sigma2_between_true alone) --
# régiões imediatas average only ~11 municípios, so this bias is not
# negligible. Standard method-of-moments correction: subtract the
# expected sampling-noise contribution, weighted the same way the raw
# estimate was built, and floor at 0 (variance can't be negative).
bias_correction_factor <- sum(cluster_stats$cluster_weight / cluster_stats$n_eff_c) / sum(cluster_stats$cluster_weight)
sigma2_between_rgi <- max(0, sigma2_between_rgi_raw - sigma2_within_rgi * bias_correction_factor)

rho_rgi_raw <- sigma2_between_rgi_raw / (sigma2_between_rgi_raw + sigma2_within_rgi)
rho_rgi <- sigma2_between_rgi / (sigma2_between_rgi + sigma2_within_rgi)

cat("\nHistorical região-imediata variance decomposition (2022 vs 2018 margin swing):\n")
cat("  between-região variance component (raw):      ", signif(sigma2_between_rgi_raw, 4), "\n")
cat("  between-região variance component (corrected):", signif(sigma2_between_rgi, 4), "\n")
cat("  within-região variance component:              ", signif(sigma2_within_rgi, 4), "\n")
cat("  (check: corrected between+within =", signif(sigma2_between_rgi + sigma2_within_rgi, 4),
    "vs. total margin_var =", signif(margin_var, 4), "-- should be close, slightly under since bias correction removes real information too)\n")
cat("  intraclass correlation (rho), raw:      ", round(rho_rgi_raw, 3), "\n")
cat("  intraclass correlation (rho), corrected:", round(rho_rgi, 3),
    "-- fraction of município-level swing variance explained by which região it's in\n")
cat("  built from", nrow(cluster_stats), "regiões imediatas,", nrow(anova_data), "municípios\n")

prior_regional <- list(
  prior_sigma2_between_rgi = sigma2_between_rgi,
  prior_sigma2_within_rgi = sigma2_within_rgi,
  prior_rho_rgi = rho_rgi,
  n_clusters_used = nrow(cluster_stats),
  n_municipios_used = nrow(anova_data),
  built = format(Sys.time(), "%Y-%m-%d %H:%M:%S"),
  note = "Weighted one-way ANOVA decomposition of the 2018->2022 município-level margin swing, by IBGE regiao imediata (CD_RGI). Used by poll_live_results.R as a Bayesian shrinkage prior for the live between/within-regiao variance decomposition."
)
write_json(prior_regional, file.path(OUT_DIR, "regional_correlation_prior.json"), auto_unbox = TRUE, pretty = TRUE)
cat("\nSaved to", file.path(OUT_DIR, "regional_correlation_prior.json"), "\n")
