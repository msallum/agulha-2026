# simulate_uniform_swing.R
#
# Part of the "election needle" project. NOT a live TSE fetch -- a
# synthetic test scenario for validating the needle/probability model
# end to end, requested by Miguel (2026-09-25): "Lula gains 1% more
# votes in every município compared to 2022" (interpreted as a uniform
# swing FROM Bolsonaro TO Lula: Lula's 2026 share = 2022 share + 1pp,
# Bolsonaro's 2026 share = 2022 share - 1pp, in every município,
# nothing else changes -- 2026 turnout assumed equal to 2022's). This
# is a clean, noiseless scenario: no município deviates from the exact
# national swing, so the empirical dispersion the model measures should
# stay low and the confidence interval should tighten steadily and
# smoothly as more of the country "reports" -- a good sanity check that
# the whole pipeline (extrapolation, regional shrinkage, SE, probability)
# behaves as expected before trusting it on real data.
#
# ROLLOUT: the "true" result is revealed in 5 chunks of ~20% of the
# national electorate each (by 2022 turnout, vote-weighted -- NOT 20%
# of municípios by count, since município size varies enormously),
# in a fixed random order (seeded, so it's reproducible) standing in
# for "the order results come in on election night" -- not meant to
# mimic any real reporting-order pattern, just a neutral test.
#
# OUTPUT: prints (and returns, if sourced) a list of 5 snapshots, one
# per update, each with the same fields poll_live_results.R's
# summary_out produces -- these get pushed to the live dashboard's DB
# one at a time, a minute apart, from the driving conversation (this
# script only computes; it does not push to the DB itself, since R has
# no credentials for that -- see session notes).

library(tidyverse)
library(jsonlite)

OUT_DIR <- "C:/Users/WIN10/Documents/tese/dados/scripts/needle"

baseline <- read_csv(file.path(OUT_DIR, "historical_baseline_municipio.csv"), col_types = cols(.default = "c")) %>%
  mutate(share = as.numeric(share), votos_validos_total = as.numeric(votos_validos_total))

pt_2018 <- baseline %>% filter(ano == 2018, linhagem == "PT") %>% select(codigo_tse, share_pt_2018 = share)
pt_2022 <- baseline %>% filter(ano == 2022, linhagem == "PT") %>%
  select(codigo_tse, share_pt_2022 = share, turnout_2022 = votos_validos_total)
bolso_2018 <- baseline %>% filter(ano == 2018, linhagem == "Bolsonaro") %>% select(codigo_tse, share_bolsonaro_2018 = share)
bolso_2022 <- baseline %>% filter(ano == 2022, linhagem == "Bolsonaro") %>% select(codigo_tse, share_bolsonaro_2022 = share)
regional_clusters <- read_csv(file.path(OUT_DIR, "municipio_regional_clusters.csv"), col_types = cols(.default = "c")) %>%
  select(codigo_tse, uf, cd_rgi)

universe <- pt_2022 %>%
  left_join(pt_2018, by = "codigo_tse") %>%
  left_join(bolso_2022, by = "codigo_tse") %>%
  left_join(bolso_2018, by = "codigo_tse") %>%
  left_join(regional_clusters, by = "codigo_tse") %>%
  filter(!is.na(share_pt_2022), !is.na(share_bolsonaro_2022), !is.na(turnout_2022), turnout_2022 > 0) %>%
  mutate(
    margin_baseline_2022 = share_pt_2022 - share_bolsonaro_2022,
    margin_baseline_2018 = share_pt_2018 - share_bolsonaro_2018,
    # THE SCENARIO: uniform +1pp to Lula, -1pp to Bolsonaro, vs 2022,
    # same turnout as 2022.
    share_lula_2026_true = share_pt_2022 + 0.01,
    share_bolsonaro_2026_true = share_bolsonaro_2022 - 0.01,
    votos_validos_total_2026_true = turnout_2022,
    votos_lula_2026_true = share_lula_2026_true * turnout_2022,
    votos_bolsonaro_2026_true = share_bolsonaro_2026_true * turnout_2022,
    swing_lula_vs_2022 = share_lula_2026_true - share_pt_2022,
    swing_bolsonaro_vs_2022 = share_bolsonaro_2026_true - share_bolsonaro_2022,
    margin_swing_vs_2022 = swing_lula_vs_2022 - swing_bolsonaro_vs_2022,
    swing_lula_vs_haddad_2018 = share_lula_2026_true - share_pt_2018,
    swing_bolsonaro_vs_2018 = share_bolsonaro_2026_true - share_bolsonaro_2018,
    margin_swing_vs_haddad_2018 = swing_lula_vs_haddad_2018 - swing_bolsonaro_vs_2018
  )
cat("Universe:", nrow(universe), "municípios with complete data.\n")

# --- reveal order: shuffle, then bucket by cumulative turnout share into 5 x ~20% chunks ---
set.seed(2026)
universe <- universe %>% slice_sample(prop = 1) %>%
  mutate(cum_turnout = cumsum(turnout_2022), cum_share = cum_turnout / sum(turnout_2022),
         update_bucket = pmin(5, ceiling(cum_share / 0.2)))
cat("Municípios per update bucket:\n"); print(table(universe$update_bucket))
cat("Population share per bucket (should be close to 20% each):\n")
print(universe %>% group_by(update_bucket) %>% summarise(pct_pop = round(100*sum(turnout_2022)/sum(universe$turnout_2022),1)))

# --- regional prior + compute_needle_probability() (verbatim from poll_live_results.R, 2026-09-25) ---
regional_prior <- fromJSON(file.path(OUT_DIR, "regional_correlation_prior.json"))
PRIOR_SIGMA2_WITHIN_RGI <- regional_prior$prior_sigma2_within_rgi
PRIOR_SIGMA2_BETWEEN_RGI <- regional_prior$prior_sigma2_between_rgi
PRIOR_STRENGTH_K0 <- 30
K0_BETWEEN <- 15

compute_needle_probability <- function(df_reporting, df_not_reporting, margin_swing_col, margin_baseline_col,
                                        votes_pt_col, votes_bolso_col) {
  rep_valid <- df_reporting %>%
    filter(!is.na(.data[[margin_swing_col]]), !is.na(.data[[votes_pt_col]]))
  w <- rep_valid$votos_validos_total_2026_true
  x <- rep_valid[[margin_swing_col]]
  n_mun_reporting <- nrow(rep_valid)
  votes_counted <- sum(w)
  n_clusters_reporting <- 0
  if (n_mun_reporting == 0) {
    mu_global <- NA_real_; n_eff_global <- 0; sigma2_within_emp <- NA_real_; sigma2_between_emp <- NA_real_
    cluster_stats <- tibble(cd_rgi = character(), cmean = numeric(), cweight = numeric(), n_eff_c = numeric(), cn = integer())
  } else {
    mu_global <- sum(w * x) / sum(w)
    n_eff_global <- (sum(w))^2 / sum(w^2)
    cluster_stats <- rep_valid %>% filter(!is.na(cd_rgi)) %>% group_by(cd_rgi) %>%
      summarise(cmean = weighted.mean(.data[[margin_swing_col]], votos_validos_total_2026_true),
                cweight = sum(votos_validos_total_2026_true),
                n_eff_c = (sum(votos_validos_total_2026_true))^2 / sum(votos_validos_total_2026_true^2),
                cn = n(), .groups = "drop")
    n_clusters_reporting <- nrow(cluster_stats)
    if (n_clusters_reporting >= 2) {
      sigma2_between_emp_raw <- sum(cluster_stats$cweight * (cluster_stats$cmean - mu_global)^2) / sum(cluster_stats$cweight)
      within_join <- rep_valid %>% filter(!is.na(cd_rgi)) %>% left_join(cluster_stats %>% select(cd_rgi, cmean), by = "cd_rgi")
      sigma2_within_emp <- sum(within_join$votos_validos_total_2026_true * (within_join[[margin_swing_col]] - within_join$cmean)^2) / sum(within_join$votos_validos_total_2026_true)
      bias_correction_factor <- sum(cluster_stats$cweight / cluster_stats$n_eff_c) / sum(cluster_stats$cweight)
      sigma2_between_emp <- max(0, sigma2_between_emp_raw - sigma2_within_emp * bias_correction_factor)
    } else {
      sigma2_between_emp <- NA_real_
      sigma2_within_emp <- if (n_mun_reporting > 1) sum(w * (x - mu_global)^2) / sum(w) else NA_real_
    }
  }
  sigma2_within_blended <- if (is.na(sigma2_within_emp)) PRIOR_SIGMA2_WITHIN_RGI else
    (n_eff_global * sigma2_within_emp + PRIOR_STRENGTH_K0 * PRIOR_SIGMA2_WITHIN_RGI) / (n_eff_global + PRIOR_STRENGTH_K0)
  sigma2_between_blended <- if (is.na(sigma2_between_emp)) PRIOR_SIGMA2_BETWEEN_RGI else
    (n_clusters_reporting * sigma2_between_emp + K0_BETWEEN * PRIOR_SIGMA2_BETWEEN_RGI) / (n_clusters_reporting + K0_BETWEEN)
  rho_blended <- sigma2_between_blended / (sigma2_between_blended + sigma2_within_blended)
  avg_cluster_size <- if (n_mun_reporting > 0 && n_clusters_reporting > 0) n_mun_reporting / n_clusters_reporting else 1
  design_effect <- 1 + (avg_cluster_size - 1) * rho_blended
  n_eff <- if (n_eff_global > 0) n_eff_global / design_effect else 0
  se_mu_global <- if (n_eff > 0) sqrt(sigma2_within_blended / n_eff) else sqrt(sigma2_within_blended + sigma2_between_blended)
  precision_global <- if (se_mu_global > 0) 1 / se_mu_global^2 else 0
  known_margin_votes <- sum(rep_valid[[votes_pt_col]] - rep_valid[[votes_bolso_col]], na.rm = TRUE)
  known_valid_votes <- votes_counted
  mu_used_global <- if (is.na(mu_global)) 0 else mu_global
  if (n_clusters_reporting > 0) {
    cluster_shrink <- cluster_stats %>%
      mutate(precision_own = ifelse(sigma2_within_blended > 0, n_eff_c / sigma2_within_blended, 0),
             mu_c_shrunk = (precision_own * cmean + precision_global * mu_used_global) / (precision_own + precision_global),
             var_mu_c_shrunk = 1 / (precision_own + precision_global)) %>%
      select(cd_rgi, mu_c_shrunk, var_mu_c_shrunk)
  } else {
    cluster_shrink <- tibble(cd_rgi = character(), mu_c_shrunk = numeric(), var_mu_c_shrunk = numeric())
  }
  var_mu_fallback <- if (precision_global > 0) 1 / precision_global else (sigma2_within_blended + sigma2_between_blended)
  rem <- df_not_reporting %>% filter(!is.na(turnout_2022), !is.na(.data[[margin_baseline_col]])) %>%
    left_join(cluster_shrink, by = "cd_rgi") %>%
    mutate(mu_c_shrunk = ifelse(is.na(mu_c_shrunk), mu_used_global, mu_c_shrunk),
           var_mu_c_shrunk = ifelse(is.na(var_mu_c_shrunk), var_mu_fallback, var_mu_c_shrunk),
           cd_rgi_group = ifelse(is.na(cd_rgi), paste0("__no_rgi_", codigo_tse), cd_rgi))
  remaining_valid_votes <- sum(rem$turnout_2022)
  remaining_margin_votes <- sum(rem$turnout_2022 * (rem[[margin_baseline_col]] + rem$mu_c_shrunk))
  projected_total_valid <- known_valid_votes + remaining_valid_votes
  projected_margin_votes <- known_margin_votes + remaining_margin_votes
  projected_margin_share <- if (projected_total_valid > 0) projected_margin_votes / projected_total_valid else NA_real_
  rem_var <- rem %>% group_by(cd_rgi_group) %>%
    summarise(w_c = sum(turnout_2022),
              n_eff_c = if (sum(turnout_2022) > 0) (sum(turnout_2022))^2 / sum(turnout_2022^2) else 0,
              var_mu_c = first(var_mu_c_shrunk), .groups = "drop") %>%
    mutate(contrib = if (projected_total_valid > 0) (w_c / projected_total_valid)^2 * (var_mu_c + ifelse(n_eff_c > 0, sigma2_within_blended / n_eff_c, 0)) else 0)
  var_projection <- sum(rem_var$contrib)
  se_projection <- sqrt(var_projection)
  df_t <- max(1, n_clusters_reporting - 1)
  prob_pt_leads <- if (is.na(projected_margin_share)) NA_real_ else
    if (se_projection == 0) as.numeric(projected_margin_share > 0) else pt(projected_margin_share / se_projection, df = df_t)
  tibble(n_municipios_reportando = n_mun_reporting, n_regioes_reportando = n_clusters_reporting,
         votos_contados = votes_counted,
         frac_votos_esperados_contados = if (projected_total_valid > 0) known_valid_votes / projected_total_valid else 0,
         rho_regional = rho_blended, design_effect = design_effect,
         dispersao_vs_historico = if (!is.na(sigma2_within_emp)) (sigma2_within_blended + sigma2_between_blended) / (PRIOR_SIGMA2_WITHIN_RGI + PRIOR_SIGMA2_BETWEEN_RGI) else NA_real_,
         projecao_margem_pp = projected_margin_share * 100,
         projecao_erro_padrao_pp = se_projection * 100,
         prob_pt_lidera = prob_pt_leads)
}

snapshots <- list()
for (k in 1:5) {
  reporting_k <- universe %>% filter(update_bucket <= k)
  not_reporting_k <- universe %>% filter(update_bucket > k)
  r22 <- compute_needle_probability(reporting_k, not_reporting_k, "margin_swing_vs_2022", "margin_baseline_2022",
                                     "votos_lula_2026_true", "votos_bolsonaro_2026_true")
  r18 <- compute_needle_probability(reporting_k, not_reporting_k, "margin_swing_vs_haddad_2018", "margin_baseline_2018",
                                     "votos_lula_2026_true", "votos_bolsonaro_2026_true")
  needle_lula_2022 <- if (nrow(reporting_k) > 0) weighted.mean(reporting_k$swing_lula_vs_2022, reporting_k$votos_validos_total_2026_true, na.rm = TRUE) * 100 else NA_real_
  needle_bolso_2022 <- if (nrow(reporting_k) > 0) weighted.mean(reporting_k$swing_bolsonaro_vs_2022, reporting_k$votos_validos_total_2026_true, na.rm = TRUE) * 100 else NA_real_
  needle_lula_2018 <- if (nrow(reporting_k) > 0) weighted.mean(reporting_k$swing_lula_vs_haddad_2018, reporting_k$votos_validos_total_2026_true, na.rm = TRUE) * 100 else NA_real_
  needle_bolso_2018 <- if (nrow(reporting_k) > 0) weighted.mean(reporting_k$swing_bolsonaro_vs_2018, reporting_k$votos_validos_total_2026_true, na.rm = TRUE) * 100 else NA_real_

  by_uf_k <- reporting_k %>% filter(!is.na(uf)) %>% group_by(uf) %>%
    summarise(lula_2026 = 100 * sum(votos_lula_2026_true) / sum(votos_validos_total_2026_true),
              bolsonaro_2026 = 100 * sum(votos_bolsonaro_2026_true) / sum(votos_validos_total_2026_true),
              .groups = "drop") %>%
    mutate(uf = toupper(uf))

  snapshots[[k]] <- list(
    update = k,
    status = if (k == 5) "final" else "live",
    n_municipios = nrow(reporting_k),
    pct_municipios = 100 * nrow(reporting_k) / nrow(universe),
    votos_totais = sum(reporting_k$votos_validos_total_2026_true),
    needle_lula_vs_2022 = needle_lula_2022,
    needle_bolsonaro_vs_2022 = needle_bolso_2022,
    needle_lula_vs_haddad_2018 = needle_lula_2018,
    needle_bolsonaro_vs_2018 = needle_bolso_2018,
    prob_lula_2022 = r22$prob_pt_lidera,
    prob_lula_2018 = r18$prob_pt_lidera,
    margin_projected_2022_pp = r22$projecao_margem_pp,
    margin_projected_2018_pp = r18$projecao_margem_pp,
    margin_se_2022_pp = r22$projecao_erro_padrao_pp,
    margin_se_2018_pp = r18$projecao_erro_padrao_pp,
    dispersion_ratio_2022 = r22$dispersao_vs_historico,
    dispersion_ratio_2018 = r18$dispersao_vs_historico,
    frac_votes_counted = r22$frac_votos_esperados_contados,
    rho_regional_2022 = r22$rho_regional,
    rho_regional_2018 = r18$rho_regional,
    design_effect_2022 = r22$design_effect,
    design_effect_2018 = r18$design_effect,
    n_regioes_reportando_2022 = r22$n_regioes_reportando,
    by_uf = by_uf_k
  )
  cat("\n=== UPDATE", k, "(", round(snapshots[[k]]$pct_municipios,1), "% of municípios,",
      round(100*snapshots[[k]]$frac_votes_counted,1), "% of votes) ===\n")
  cat("  margin vs 2022:", round(r22$projecao_margem_pp,2), "+-", round(r22$projecao_erro_padrao_pp,2), "pp   prob_lula:", round(r22$prob_pt_lidera,4), "\n")
  cat("  margin vs 2018:", round(r18$projecao_margem_pp,2), "+-", round(r18$projecao_erro_padrao_pp,2), "pp   prob_lula:", round(r18$prob_pt_lidera,4), "\n")
}

write_json(snapshots, file.path(OUT_DIR, "simulated_uniform_swing_updates.json"), auto_unbox = TRUE, pretty = TRUE, na = "null")
for (k in 1:5) {
  write_json(snapshots[[k]], file.path(OUT_DIR, paste0("uniform_update_", k, "_compact.json")), auto_unbox = TRUE, pretty = FALSE, na = "null")
}
cat("\nSaved simulated_uniform_swing_updates.json and uniform_update_1..5_compact.json\n")
