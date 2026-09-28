# simulate_bolsonaro_random_bump.R
#
# Third synthetic test scenario, requested by Miguel (2026-09-25):
# Bolsonaro (Flavio, 2026) gets an average +5pp bump to his 2022 vote
# share (raised from an initial +1.5pp per Miguel's follow-up request),
# drawn INDEPENDENTLY per município from Normal(mean=5, variance=4, i.e.
# sd=2pp). The bump is taken proportionally from EVERY candidate's 2022
# share (Lula included) to free up room for it, per Miguel's correction
# to the original (wrong) construction -- see share_lula_2026_true /
# share_bolsonaro_2026_true below.
#
# WHY THIS ONE IS INTERESTING METHODOLOGICALLY: scenario 1 (uniform
# +1pp) had ZERO município-to-município noise; scenario 2 (swapped
# 2022 votes) had large, but ELECTORALLY STRUCTURED heterogeneity
# (real 2022 geography). This one has real per-município noise, but
# it is drawn i.i.d. -- NO regional structure at all baked into the
# data-generating process, unlike the historical 2018->2022 prior
# (rho ~ 0.82) which assumes strong região-imediata correlation. This
# is a genuine test of whether the model's empirical rho estimate
# correctly pulls DOWN toward the (near-zero) true value as more
# reports come in, rather than staying anchored to the historical
# prior's higher rho -- something scenarios 1 and 2 couldn't test
# (scenario 1 had no variance to estimate rho from at all; scenario 2's
# real 2022 geography happens to be regionally structured too).
#
# STATUS: per Miguel's instruction, this script only GENERATES the 5
# update snapshots (deterministic given the seed) and saves them --
# nothing gets pushed to the live dashboard until he says to start.

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
  filter(!is.na(share_pt_2022), !is.na(share_bolsonaro_2022), !is.na(turnout_2022), turnout_2022 > 0)

set.seed(9001)
universe <- universe %>%
  mutate(
    margin_baseline_2022 = share_pt_2022 - share_bolsonaro_2022,
    margin_baseline_2018 = share_pt_2018 - share_bolsonaro_2018,
    # THE SCENARIO (corrected 2026-09-25, per Miguel): Bolsonaro's share
    # gets an i.i.d. per-município bump ~ N(5, 4) in pp -- but shares
    # must sum to 100%, so that bump cannot simply be ADDED on top of
    # his 2022 share while leaving Lula's share untouched (the original,
    # WRONG version here) -- that silently pulls the whole bump out of
    # an untracked "other candidates" pool while leaving Lula frozen,
    # which is not what "Bolsonaro gains X points" should mean. Correct
    # construction: shrink EVERY candidate's 2022 share proportionally
    # by (1 - bump) to free up exactly `bump` share of the electorate,
    # then hand that entire freed-up room to Bolsonaro. This keeps
    # lula_2026 + bolsonaro_2026 + (everyone else, shrunk the same way)
    # summing to exactly 1 for any bump, positive or negative (about
    # 23% of municípios draw a negative bump, i.e. Bolsonaro LOSES share
    # there -- the same formula handles that correctly too: everyone
    # else's share scales UP to absorb what Bolsonaro gave back).
    bolsonaro_bump_pp = rnorm(n(), mean = 5, sd = 2),
    bump_frac = bolsonaro_bump_pp / 100,
    votos_validos_total_2026_true = turnout_2022,
    share_lula_2026_true = pmin(1, pmax(0, share_pt_2022 * (1 - bump_frac))),
    share_bolsonaro_2026_true = pmin(1, pmax(0, share_bolsonaro_2022 * (1 - bump_frac) + bump_frac)),
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
cat("Realized (sample) mean bump:", round(mean(universe$bolsonaro_bump_pp), 3),
    "pp, sd:", round(sd(universe$bolsonaro_bump_pp), 3), "pp (target: mean 5, sd 2)\n")
cat("Sanity check -- national (vote-weighted) margin:\n")
nat_margin_2022 <- 100 * (sum(universe$share_pt_2022 * universe$turnout_2022) - sum(universe$share_bolsonaro_2022 * universe$turnout_2022)) / sum(universe$turnout_2022)
nat_margin_2026 <- 100 * (sum(universe$votos_lula_2026_true) - sum(universe$votos_bolsonaro_2026_true)) / sum(universe$votos_validos_total_2026_true)
# With the proportional-shrink construction, margin_swing_i = -b_i*(margin_baseline_i + 1)
# (not simply -b_i), since Lula's share also shrinks by the same factor -- so the
# expected national shift is close to, but not exactly, -5pp.
expected_shift <- -5 * (1 + nat_margin_2022 / 100)
cat("  2022 national margin:", round(nat_margin_2022, 2), "pp\n")
cat("  2026 (simulated) national margin:", round(nat_margin_2026, 2), "pp  (expected ~", round(nat_margin_2022 + expected_shift, 2), "pp)\n")

set.seed(2468)  # separate seed for the reveal order, independent of the bump draws
universe <- universe %>% slice_sample(prop = 1) %>%
  mutate(cum_turnout = cumsum(turnout_2022), cum_share = cum_turnout / sum(turnout_2022),
         update_bucket = pmin(5, ceiling(cum_share / 0.2)))
cat("\nPopulation share per bucket:\n")
print(universe %>% group_by(update_bucket) %>% summarise(pct_pop = round(100*sum(turnout_2022)/sum(universe$turnout_2022),1)))

regional_prior <- fromJSON(file.path(OUT_DIR, "regional_correlation_prior.json"))
PRIOR_SIGMA2_WITHIN_RGI <- regional_prior$prior_sigma2_within_rgi
PRIOR_SIGMA2_BETWEEN_RGI <- regional_prior$prior_sigma2_between_rgi
PRIOR_STRENGTH_K0 <- 30
K0_BETWEEN <- 15

# MONTE CARLO UNCERTAINTY PROPAGATION + SE DOUBLING (added 2026-09-27,
# mirrored from poll_live_results.R -- see that script's header comment
# near these same constants for the full rationale). SE_INFLATION_FACTOR
# is what Miguel actually asked for (SE itself 2x wider); since SE scales
# as sqrt(variance), the variance multiplier applied to sigma2_within/
# between_blended is SE_INFLATION_FACTOR^2, confirmed empirically to
# produce exactly the intended SE ratio (test_inflation_effect.R).
SE_INFLATION_FACTOR <- 2
UNCERTAINTY_INFLATION_FACTOR <- SE_INFLATION_FACTOR^2
N_DRAWS_MC <- 2000

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

  sigma2_within_blended <- sigma2_within_blended * UNCERTAINTY_INFLATION_FACTOR
  sigma2_between_blended <- sigma2_between_blended * UNCERTAINTY_INFLATION_FACTOR

  rho_blended <- sigma2_between_blended / (sigma2_between_blended + sigma2_within_blended)
  avg_cluster_size <- if (n_mun_reporting > 0 && n_clusters_reporting > 0) n_mun_reporting / n_clusters_reporting else 1
  design_effect <- 1 + (avg_cluster_size - 1) * rho_blended
  known_margin_votes <- sum(rep_valid[[votes_pt_col]] - rep_valid[[votes_bolso_col]], na.rm = TRUE)
  known_valid_votes <- votes_counted
  mu_used_global <- if (is.na(mu_global)) 0 else mu_global
  df_t <- max(1, n_clusters_reporting - 1)  # informational only now

  rem <- df_not_reporting %>% filter(!is.na(turnout_2022), !is.na(.data[[margin_baseline_col]])) %>%
    mutate(cd_rgi_group = ifelse(is.na(cd_rgi), paste0("__no_rgi_", codigo_tse), cd_rgi))
  remaining_valid_votes <- sum(rem$turnout_2022)
  projected_total_valid <- known_valid_votes + remaining_valid_votes
  baseline_term_fixed <- sum(rem$turnout_2022 * rem[[margin_baseline_col]])

  safe_div <- function(num, denom) ifelse(denom > 0, num / denom, 0)
  rem_grouped <- rem %>% group_by(cd_rgi_group) %>%
    summarise(w_c = sum(turnout_2022),
              n_eff_c = if (sum(turnout_2022) > 0) (sum(turnout_2022))^2 / sum(turnout_2022^2) else 0,
              cd_rgi_val = first(cd_rgi), .groups = "drop")
  is_instats <- rem_grouped$cd_rgi_val %in% cluster_stats$cd_rgi
  instats_groups <- rem_grouped[is_instats, ]
  fallback_groups <- rem_grouped[!is_instats, ]
  idx_map <- match(instats_groups$cd_rgi_val, cluster_stats$cd_rgi)

  alpha_w <- (PRIOR_STRENGTH_K0 + n_eff_global) / 2
  beta_w  <- sigma2_within_blended * (alpha_w - 1)
  alpha_b <- (K0_BETWEEN + n_clusters_reporting) / 2
  beta_b  <- sigma2_between_blended * (alpha_b - 1)
  sigma2_within_draws  <- 1 / rgamma(N_DRAWS_MC, shape = alpha_w, rate = beta_w)
  sigma2_between_draws <- 1 / rgamma(N_DRAWS_MC, shape = alpha_b, rate = beta_b)

  rho_draws <- sigma2_between_draws / (sigma2_between_draws + sigma2_within_draws)
  design_effect_draws <- 1 + (avg_cluster_size - 1) * rho_draws
  n_eff_draws <- if (n_eff_global > 0) n_eff_global / design_effect_draws else rep(0, N_DRAWS_MC)
  se_mu_global_draws <- if (n_eff_global > 0) sqrt(sigma2_within_draws / n_eff_draws) else
    sqrt(sigma2_within_draws + sigma2_between_draws)
  precision_global_draws <- 1 / se_mu_global_draws^2

  precision_own_mat <- outer(1 / sigma2_within_draws, cluster_stats$n_eff_c)
  cmean_mat <- matrix(cluster_stats$cmean, nrow = N_DRAWS_MC, ncol = n_clusters_reporting, byrow = TRUE)
  numerator <- precision_own_mat * cmean_mat + precision_global_draws * mu_used_global
  denominator <- precision_own_mat + precision_global_draws
  mu_c_shrunk_mat <- numerator / denominator
  var_mu_c_shrunk_mat <- 1 / denominator

  term2_instats_draws <- as.vector(mu_c_shrunk_mat[, idx_map, drop = FALSE] %*% instats_groups$w_c)
  w_over_total_sq_instats <- if (projected_total_valid > 0) (instats_groups$w_c / projected_total_valid)^2 else rep(0, nrow(instats_groups))
  term_a_instats_draws <- as.vector(var_mu_c_shrunk_mat[, idx_map, drop = FALSE] %*% w_over_total_sq_instats)
  C_instats <- sum(safe_div(w_over_total_sq_instats, instats_groups$n_eff_c))
  contrib_instats_draws <- term_a_instats_draws + sigma2_within_draws * C_instats

  term2_fallback_fixed <- sum(fallback_groups$w_c) * mu_used_global
  w_over_total_sq_fallback <- if (projected_total_valid > 0) (fallback_groups$w_c / projected_total_valid)^2 else rep(0, nrow(fallback_groups))
  A_fallback <- sum(w_over_total_sq_fallback)
  B_fallback <- sum(safe_div(w_over_total_sq_fallback, fallback_groups$n_eff_c))
  contrib_fallback_draws <- A_fallback / precision_global_draws + B_fallback * sigma2_within_draws

  remaining_margin_votes_draws <- baseline_term_fixed + term2_instats_draws + term2_fallback_fixed
  var_projection_draws <- pmax(contrib_instats_draws + contrib_fallback_draws, 0)
  projected_margin_share_draws <- if (projected_total_valid > 0)
    (known_margin_votes + remaining_margin_votes_draws) / projected_total_valid else rep(NA_real_, N_DRAWS_MC)

  prob_pt_leads <- if (projected_total_valid <= 0) NA_real_ else
    mean(pnorm(projected_margin_share_draws / sqrt(pmax(var_projection_draws, .Machine$double.eps))))
  projected_margin_share <- if (projected_total_valid > 0) mean(projected_margin_share_draws) else NA_real_
  var_projection <- if (projected_total_valid > 0) mean(var_projection_draws) + var(projected_margin_share_draws) else NA_real_
  se_projection <- sqrt(var_projection)

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
  cat("  rho_regional (2022):", round(r22$rho_regional,3), " (historical prior: 0.816 -- should drift DOWN toward the true near-zero value as data comes in)\n")
  cat("  dispersao_vs_historico (2022):", round(r22$dispersao_vs_historico,3), "\n")
}

write_json(snapshots, file.path(OUT_DIR, "simulated_bolsonaro_bump_updates.json"), auto_unbox = TRUE, pretty = TRUE, na = "null")
for (k in 1:5) {
  write_json(snapshots[[k]], file.path(OUT_DIR, paste0("bump_update_", k, "_compact.json")), auto_unbox = TRUE, pretty = FALSE, na = "null")
}
cat("\nSaved simulated_bolsonaro_bump_updates.json and bump_update_1..5_compact.json.\nNothing pushed to the dashboard yet -- waiting for the go-ahead.\n")
