# poll_live_results.R
#
# Part of the "election needle" project. Fetches TSE's live per-município
# presidential results and computes the swing against the historical
# baseline (build_historical_baseline.R) -- one snapshot per run. Meant
# to be re-run every few minutes on election night (2026-10-04).
#
# HOW THE PIPELINE WAS REVERSE-ENGINEERED (2026-09-24): TSE's live
# results site (resultados.tse.jus.br) doesn't publish a written API
# spec, so this was found by driving the actual production app in a
# browser and inspecting its own network requests, then cross-checked
# against TSE's own internal config file (oficial/comum/config/ele-c.json)
# which documents the URL template directly:
#   "tp":"u","dir":"<base>/<ambiente>/<ciclo>/<cd_eleicao>/dados/<uf>"
# i.e. per-município detail files live at:
#   {ambiente}/{ciclo}/{cd_eleicao}/dados/{uf}/{uf}{municode}-c{cargo}-e{cd_eleicao_padded6}-u.json
# Confirmed working end-to-end against TSE's own LIVE 2026 rehearsal
# environment (a public "simulado" TSE runs before the real election,
# using the exact same file structure with placeholder candidates):
#   https://resultados-sim.tse.jus.br/simulado/simulado2026/ele2026/21270/...
# NOTE: 21270 is the SIMULADO's own test election code, not the real
# one -- the real code is discovered at runtime below (see
# discover_election_code()), since TSE had not yet provisioned the real
# 2026 general election in oficial/comum/config/ele-c.json as of
# 2026-09-24 (it appears in the days immediately before the election).
# No bulk per-state file with a município breakdown exists (checked) --
# this is genuinely one HTTP request per município (~5,570 nationally).
#
# USAGE: set `use_simulado <- TRUE` below to test against the live
# rehearsal environment (works today); set it to FALSE on election night
# once the real environment is live.

library(tidyverse)
library(jsonlite)
library(curl)

# TIMING INSTRUMENTATION (added 2026-09-27, per Miguel's request to
# decompose end-to-end pipeline runtime). Marks wall-clock time at the
# boundary of each pipeline phase; a summary table is printed at the
# very end. Does NOT cover the dashboard push -- that's a separate,
# manual step (ArtifactData calls Claude makes after this script
# writes live_needle_summary_latest.json), not part of this script.
t_script_start <- Sys.time()
timing_log <- list()
mark_timing <- function(phase, from) {
  timing_log[[phase]] <<- as.numeric(difftime(Sys.time(), from, units = "secs"))
}

# PORTABLE PATHS (2026-09-28): this copy of the script lives in its own
# standalone repo (github.com/msallum/agulha-2026), separate from the
# original C:/Users/WIN10/Documents/tese/... copy (kept there as the
# local backup) -- OUT_DIR is just the working directory now, so this
# runs the same whether invoked locally (from this repo's checkout) or
# in a cloud sandbox after a fresh git clone. RAW isn't used by this
# script at all (only by build_historical_baseline.R, which needs the
# full raw multi-election TSE archive -- not included in this repo,
# that stays in the thesis project; this script only needs the three
# small DERIVED files below, which are).
OUT_DIR <- getwd()
SCRATCH <- file.path(tempdir(), "needle_poll")
dir.create(SCRATCH, showWarnings = FALSE, recursive = TRUE)

use_simulado <- as.logical(Sys.getenv("USE_SIMULADO", "TRUE"))  # flip via USE_SIMULADO=FALSE env var, or edit the default here on election night

if (use_simulado) {
  BASE <- "https://resultados-sim.tse.jus.br/simulado/simulado2026"
  CICLO <- "ele2026"
  ELECTION_CODE <- "21270"  # the simulado's own fixed test code
} else {
  BASE <- "https://resultados.tse.jus.br/oficial"
  CICLO <- "ele2026"
  ELECTION_CODE <- NULL  # discovered below
}

# ROUND=2 switches to the runoff: its election code (the first round's
# `cdt2` in TSE's catalog), the 2018/2022 RUNOFF baseline and the runoff
# regional prior (both built by build_runoff_baseline.py).
ROUND <- as.integer(Sys.getenv("ROUND", "1"))
stopifnot(ROUND %in% c(1, 2))
BASELINE_FILE <- if (ROUND == 2) "historical_baseline_municipio_2turno.csv" else "historical_baseline_municipio.csv"
PRIOR_FILE <- if (ROUND == 2) "regional_correlation_prior_2turno.json" else "regional_correlation_prior.json"
if (nzchar(Sys.getenv("ELECTION_CODE"))) ELECTION_CODE <- Sys.getenv("ELECTION_CODE")

# ---------------------------------------------------------------------
# 1. Discover the real election code (production only -- the simulado
#    uses a fixed known code). Searches TSE's own master election
#    catalog for a 2026 "Eleição Geral"/presidential entry. This WILL
#    NOT find anything until TSE provisions it, in the days before the
#    election -- run this check ahead of time (e.g. daily) rather than
#    assuming it will work on the first try.
# ---------------------------------------------------------------------
discover_election_code <- function() {
  cat("Fetching election catalog...\n")
  cfg <- fromJSON("https://resultados.tse.jus.br/oficial/comum/config/ele-c.json", flatten = FALSE)
  pl <- cfg$pl
  # Each "pl" (pleito) has an "e" list of elections; look for a 2026
  # entry whose name mentions "Geral" (general election) -- presidential
  # races run under the "Eleição Geral Federal" umbrella in TSE's system.
  # NOTE (fixed 2026-10-02): originally matched only "Geral", on the
  # assumption presidential races run under an "Eleição Geral" umbrella
  # (true in some past cycles). Confirmed against TSE's real catalog
  # that 2026 instead splits into separate "Ordinária Federal" (federal
  # offices incl. president, cd 6257), "Ordinária Estadual" (governors),
  # and "Ordinária Municipal" (mayors) pleitos -- no "Geral" entry
  # exists this cycle. Match "Federal" too so this keeps working
  # regardless of which naming convention TSE uses in a given cycle.
  hits <- list()
  for (i in seq_len(nrow(pl))) {
    e <- pl$e[[i]]
    if (is.null(e) || nrow(e) == 0) next
    matches <- grepl("2026", e$nm, ignore.case = TRUE) &
      (grepl("Geral", e$nm, ignore.case = TRUE) | grepl("Federal", e$nm, ignore.case = TRUE))
    if (any(matches)) hits[[length(hits) + 1]] <- e[matches, ]
  }
  if (length(hits) == 0) {
    stop("2026 general election not yet found in TSE's catalog -- check closer to election day.")
  }
  hits <- bind_rows(hits)
  cat("Found candidate election catalog entries:\n")
  print(hits)
  hits
}

if (is.null(ELECTION_CODE)) {
  found <- discover_election_code()
  # Prefer an entry explicitly scoped to "br" (national) and round 1
  first_round <- found %>% filter(t == "1") %>% slice(1)
  ELECTION_CODE <- if (ROUND == 2) first_round$cdt2 else first_round$cd
  cat("Using election code:", ELECTION_CODE, "\n")
}

ELECTION_CODE_PAD <- sprintf("%06d", as.integer(ELECTION_CODE))

# ---------------------------------------------------------------------
# 2. Municipality list + TSE<->IBGE crosswalk, straight from TSE's own
#    config file for THIS election (always fresh, no separate crosswalk
#    maintenance needed -- confirmed 2026-09-24 to match our existing
#    municipios_tse_ibge.csv exactly).
# ---------------------------------------------------------------------
cat("Fetching municipality config...\n")
mun_cfg_url <- paste0(BASE, "/", CICLO, "/", ELECTION_CODE, "/config/mun-e", ELECTION_CODE_PAD, "-cm.json")
mun_cfg <- fromJSON(mun_cfg_url, flatten = FALSE)

municipios <- map_dfr(seq_len(nrow(mun_cfg$abr)), function(i) {
  uf <- mun_cfg$abr$cd[i]
  mu <- mun_cfg$abr$mu[[i]]
  tibble(uf = tolower(uf), codigo_tse = mu$cd, codigo_ibge = mu$cdi, nome_municipio = mu$nm)
})
cat("Municipalities found:", nrow(municipios), "\n")

# ---------------------------------------------------------------------
# 3. Fetch each município's presidential result file. Uses curl's async
#    multi-download so this is genuinely concurrent (~5,570 individual
#    small static JSON files) rather than one request at a time -- pace
#    it politely (moderate concurrency, not thousands at once) since
#    this hits TSE's own servers on the busiest night of their year.
#
#    CACHING (added 2026-09-25, per Miguel's request): this script is
#    meant to be re-run repeatedly through election night, and a
#    município that TSE already reports at 100% seções apuradas has no
#    more votes left to arrive -- re-fetching it on every subsequent
#    run is pure waste. Once a município hits 100%, its result row is
#    saved to a small on-disk cache (COMPLETED_CACHE_PATH) and every
#    later run skips fetching it, pulling its (unchanging) result from
#    the cache instead. This is the main lever for keeping later-night
#    runs fast: early on almost nothing is cached (fetch ~5,570), but
#    by the final stretch most municípios are done and each run only
#    has to fetch the shrinking "still counting" remainder.
#
#    CAVEAT: "100% seções apuradas" is TSE's own completion signal and
#    is what every real-time tracker treats as final -- but in rare
#    cases (e.g. a judicial recount order) a município's result can in
#    principle still be revised after that point. This script does not
#    guard against that. To force a full refresh (e.g. if something
#    looks wrong, or you suspect a revision), just delete the cache
#    file at COMPLETED_CACHE_PATH and re-run.
#
#    The cache file name is scoped to BASE+ELECTION_CODE, so switching
#    between the simulado and the real election (or re-running against
#    a different election code) never mixes cached results across runs.
# ---------------------------------------------------------------------
build_url <- function(uf, codigo_tse) {
  paste0(BASE, "/", CICLO, "/", ELECTION_CODE, "/dados/", uf, "/",
         uf, codigo_tse, "-c0001-e", ELECTION_CODE_PAD, "-u.json")
}

CACHE_KEY <- gsub("[^A-Za-z0-9]+", "_", paste0(BASE, "_", ELECTION_CODE))
COMPLETED_CACHE_PATH <- file.path(OUT_DIR, paste0("completed_municipios_", CACHE_KEY, ".rds"))

empty_completed <- tibble(codigo_tse = character(), pct_secoes_apuradas = numeric(),
                           votos_validos_total_2026 = numeric(), votos_lula_2026 = numeric(),
                           votos_bolsonaro_2026 = numeric())
completed_cache <- if (file.exists(COMPLETED_CACHE_PATH)) readRDS(COMPLETED_CACHE_PATH) else empty_completed
cat("Completed-municípios cache:", nrow(completed_cache), "already at 100% from previous runs (",
    COMPLETED_CACHE_PATH, ").\n")

municipios <- municipios %>%
  mutate(url = build_url(uf, codigo_tse),
         dest = file.path(SCRATCH, paste0(uf, codigo_tse, ".json")))

to_fetch <- municipios %>% filter(!codigo_tse %in% completed_cache$codigo_tse)
cat("Skipping", nrow(municipios) - nrow(to_fetch), "already-completed municípios; fetching", nrow(to_fetch), "still-counting or unseen municípios.\n")

mark_timing("1_setup_catalog_and_muni_config", t_script_start)
t_fetch_start <- Sys.time()

# CONCURRENCY (fixed 2026-09-25, per Miguel's request to speed up the
# fetch): curl::new_pool()'s `host_con` argument -- max concurrent
# connections to a SINGLE host -- defaulted to 6, silently overriding
# `total_con` (set to 40) since every one of our ~5,755 requests goes
# to the exact same host.
#
# TUNING HISTORY (worth keeping -- this is not a number to casually
# bump back up without re-testing at FULL scale): a 300-600 município
# SAMPLE test showed host_con=80 running clean at ~270 req/s with zero
# failures. Applied nationally (5,755 requests) it got RATE-LIMITED
# (HTTP 429) partway through -- only 1,747 of 5,755 succeeded, and the
# server stayed in a 429 state for several minutes afterward. host_con=6
# (the original default), in contrast, had completed 5,755/5,755
# cleanly across every one of ~7 full runs earlier in this session --
# strong evidence the limit is closer to a sustained-volume/rate cap
# than a simple peak-concurrency check, and that a short sample test
# CANNOT be trusted to reveal it. Settled on host_con=40 (Miguel's
# call, 2026-09-25) as a middle ground -- confirmed clean on a full
# 5,755-município run (see session log). Do not raise this again
# without a full-scale test, not just a sample -- and note this was all
# measured against the simulado under near-zero real traffic; TSE's
# servers under real election-night load will behave differently
# regardless of our own setting.
FETCH_CONCURRENCY <- 40

# PACING (fixed 2026-10-02, after testing directly against the real
# production endpoint): curl's host_con/total_con do NOT meaningfully
# pace requests here -- the server negotiates HTTP/2, which multiplexes
# many streams per connection, so curl fires almost the entire queue
# within ~1-2 seconds regardless of the configured concurrency
# (measured: 300-request probes took 1.1-2.4s at every tested level,
# 5/10/20/40). What actually determines success is whether a recent
# burst already tripped TSE's rate limiter: once tripped, requests get
# 429'd for roughly 15-45+ seconds before it clears (one full
# 5,757-request run at host_con=40 got only 34% success -- 1,958/5,757
# -- almost entirely from this). So real pacing has to come from
# batching with an explicit pause between batches plus a 429-aware
# retry pass, not from the connection-pool size, which is kept here
# only because dropping it to 1 would serialize transfers within each
# batch for no benefit.
BATCH_SIZE <- 400
BATCH_PAUSE_SEC <- 1.5
MAX_FETCH_RETRIES <- 3
RETRY_BACKOFF_SEC <- 15

fetch_batch <- function(urls, dests, concurrency) {
  pool <- new_pool(total_con = concurrency, host_con = concurrency)
  results <- vector("list", length(urls))
  for (i in seq_along(urls)) {
    local({
      idx <- i
      dest_i <- dests[idx]
      curl_fetch_multi(urls[idx], pool = pool,
                        done = function(res) {
                          if (res$status_code == 200) writeBin(res$content, dest_i)
                          results[[idx]] <<- res$status_code
                        },
                        fail = function(msg) { results[[idx]] <<- NA_integer_ })
    })
  }
  multi_run(pool = pool)
  unlist(results)
}

fetch_all <- function(df, concurrency = FETCH_CONCURRENCY, batch_size = BATCH_SIZE,
                       batch_pause = BATCH_PAUSE_SEC, max_retries = MAX_FETCH_RETRIES,
                       retry_backoff = RETRY_BACKOFF_SEC) {
  n <- nrow(df)
  final_status <- rep(NA_integer_, n)
  pending <- seq_len(n)
  attempt <- 0
  repeat {
    attempt <- attempt + 1
    n_pending <- length(pending)
    if (n_pending == 0) break
    cat("  fetch attempt", attempt, "--", n_pending, "url(s)\n")
    batch_starts <- seq(1, n_pending, by = batch_size)
    attempt_status <- rep(NA_integer_, n_pending)
    for (bi in seq_along(batch_starts)) {
      idx_range <- batch_starts[bi]:min(batch_starts[bi] + batch_size - 1, n_pending)
      rows <- pending[idx_range]
      attempt_status[idx_range] <- fetch_batch(df$url[rows], df$dest[rows], concurrency)
      if (bi < length(batch_starts)) Sys.sleep(batch_pause)
    }
    final_status[pending] <- attempt_status
    retry_mask <- !is.na(attempt_status) & attempt_status == 429
    if (!any(retry_mask) || attempt > max_retries) break
    pending <- pending[retry_mask]
    cat("  ", sum(retry_mask), "got HTTP 429 -- backing off", retry_backoff, "s before retry\n")
    Sys.sleep(retry_backoff)
  }
  final_status
}

if (nrow(to_fetch) > 0) {
  cat("Fetching ", nrow(to_fetch), " municipality result files (concurrency=", FETCH_CONCURRENCY,
      ", batch_size=", BATCH_SIZE, ")...\n", sep = "")
  t0 <- Sys.time()
  status <- fetch_all(to_fetch)
  n429 <- sum(status == 429, na.rm = TRUE)
  nother <- sum(!is.na(status) & !status %in% c(200, 429))
  nconnfail <- sum(is.na(status))
  cat("Done in", round(difftime(Sys.time(), t0, units = "secs"), 1), "seconds. ",
      sum(status == 200, na.rm = TRUE), "of", nrow(to_fetch), "succeeded (",
      n429, "429s,", nother, "other codes,", nconnfail, "connection failures ).\n")
} else {
  cat("Nothing left to fetch -- every município is already cached as complete.\n")
}
mark_timing("2_data_collection_fetch", t_fetch_start)
t_parse_start <- Sys.time()

# ---------------------------------------------------------------------
# 4. Parse each downloaded file: total valid votes, Lula's and the
#    Bolsonaro candidate's votes, and % of sections counted so far.
#    BOLSONARO_URNA_NAME is a parameter, not hardcoded past this point --
#    set it once the real 2026 ballot name is confirmed (see header:
#    Flavio Bolsonaro is expected, but the exact NM_URNA_CANDIDATO
#    string TSE uses should be verified the same way "JAIR BOLSONARO"/
#    "LULA" were verified for 2018/2022 -- see build_historical_baseline.R).
# ---------------------------------------------------------------------
LULA_URNA_NAME <- "LULA"
BOLSONARO_URNA_NAME <- "FLAVIO BOLSONARO"  # PLACEHOLDER -- confirm on real data
# NOTE (2026-09-27): the simulado's own candidates are placeholders too --
# literally named "CANDIDATO 9995".."CANDIDATO 9981" nationally (a fixed
# fictional roster, confirmed identical across 6 municípios sampled from
# different UFs), not "LULA"/"FLAVIO BOLSONARO". So with these real names,
# votos_lula_2026/votos_bolsonaro_2026 come back NA against the simulado
# and compute_needle_probability() always takes its n_mun_reporting==0
# fast-path -- expected, and fine for testing fetch/parse timing. To
# exercise the real model-estimation code path (weighted-ANOVA rho
# decomposition + per-região shrinkage) against the simulado for timing
# purposes, temporarily swap these two constants for "CANDIDATO 9995" /
# "CANDIDATO 9987" and clear the completed-municípios cache before
# re-running -- confirmed 2026-09-27: 5,708/5,755 municípios, all 510
# regiões reporting, model estimation = 0.846s. Revert before doing that
# again, and always revert before election night.

parse_one <- function(path, codigo_tse) {
  if (!file.exists(path)) return(NULL)
  j <- tryCatch(fromJSON(path, flatten = FALSE), error = function(e) NULL)
  if (is.null(j) || is.null(j$carg) || nrow(j$carg) == 0) return(NULL)
  pres <- j$carg[j$carg$cd == "1", ]
  if (nrow(pres) == 0) return(NULL)
  # Candidates are nested: carg -> agr (coalition/federation) -> par (party) -> cand
  # (structure verified 2026-09-24 against a real município file from the
  # live 2026 simulado -- see session log / test_parse2.R in scratchpad)
  all_cand_flat <- tryCatch({
    unlist_cands <- list()
    for (a in seq_along(pres$agr[[1]]$par)) {
      parlist <- pres$agr[[1]]$par[[a]]
      for (cd in seq_len(nrow(parlist$cand[[1]]))) {
        unlist_cands[[length(unlist_cands) + 1]] <- parlist$cand[[1]][cd, ]
      }
    }
    bind_rows(unlist_cands)
  }, error = function(e) NULL)

  if (is.null(all_cand_flat) || nrow(all_cand_flat) == 0) return(NULL)

  lula_votes <- suppressWarnings(as.numeric(all_cand_flat$vap[all_cand_flat$nmu == LULA_URNA_NAME |
                                                                 all_cand_flat$nm == LULA_URNA_NAME]))
  bolso_votes <- suppressWarnings(as.numeric(all_cand_flat$vap[all_cand_flat$nmu == BOLSONARO_URNA_NAME |
                                                                  all_cand_flat$nm == BOLSONARO_URNA_NAME]))
  total_votes <- suppressWarnings(as.numeric(j$v$vnom))
  pct_counted <- suppressWarnings(as.numeric(gsub(",", ".", j$s$pst)))

  tibble(codigo_tse = codigo_tse,
         pct_secoes_apuradas = pct_counted,
         votos_validos_total_2026 = total_votes,
         votos_lula_2026 = ifelse(length(lula_votes) == 0, NA_real_, sum(lula_votes)),
         votos_bolsonaro_2026 = ifelse(length(bolso_votes) == 0, NA_real_, sum(bolso_votes)))
}

# PARALLELIZED (added 2026-09-27, per Miguel's request to speed up
# parsing): timing instrumentation showed this step taking ~111s for
# 5,755 files -- MORE than the network fetch itself (~88s) -- because
# fromJSON() + the nested agr->par->cand flattening loop run serially,
# one file at a time, in plain R. Each file is independent, so this is
# embarrassingly parallel; using base R's `parallel` package (no new
# dependency -- furrr/future aren't installed) to fan the work out
# across worker processes. detectCores()-1 leaves one core free for the
# OS/other work; this machine has 4 cores, so 3 workers. PSOCK (not
# fork) because this runs on Windows, which has no fork().
cat("Parsing downloaded files...\n")
live_new <- if (nrow(to_fetch) > 0) {
  n_workers <- max(1, parallel::detectCores() - 1)
  cl <- parallel::makeCluster(n_workers)
  parsed_list <- tryCatch({
    parallel::clusterEvalQ(cl, { library(jsonlite); library(dplyr); library(tibble) })
    parallel::clusterExport(cl, c("parse_one", "LULA_URNA_NAME", "BOLSONARO_URNA_NAME"), envir = environment())
    # clusterMap (not parLapply-over-indices) so each worker only ever
    # receives the two vectors it actually needs (dest, codigo_tse),
    # not the whole `to_fetch` data frame -- parLapply(cl, seq_len(...),
    # function(i) ... to_fetch$dest[i] ...) failed here because `to_fetch`
    # itself was never exported to the workers (object not found).
    parallel::clusterMap(cl, parse_one, to_fetch$dest, to_fetch$codigo_tse, SIMPLIFY = FALSE)
  }, finally = parallel::stopCluster(cl))
  bind_rows(parsed_list)
} else {
  empty_completed  # nothing was fetched -- reuse the same empty-but-correctly-typed schema, not a column-less tibble()
}
cat("Parsed", nrow(live_new), "of", nrow(to_fetch), "freshly-fetched municipalities.\n")

# Move any newly-100%-complete município into the on-disk cache so the
# NEXT run skips it; anything still short of 100% stays "live" (fetched
# fresh again next run). `live` is the full national picture for THIS
# run: cached-complete (untouched, no fetch needed) + freshly-fetched
# (complete or still counting).
newly_completed <- live_new %>% filter(!is.na(pct_secoes_apuradas), pct_secoes_apuradas >= 100)
if (nrow(newly_completed) > 0) {
  completed_cache <- bind_rows(completed_cache, newly_completed)
  saveRDS(completed_cache, COMPLETED_CACHE_PATH)
  cat(nrow(newly_completed), "município(s) just reached 100% -- added to cache (now", nrow(completed_cache), "total).\n")
}
still_counting <- live_new %>% filter(is.na(pct_secoes_apuradas) | pct_secoes_apuradas < 100)
live <- bind_rows(completed_cache, still_counting)

cat("Parsed", nrow(live), "of", nrow(municipios), "municipalities (", nrow(completed_cache), "from cache,", nrow(still_counting), "freshly fetched still counting ).\n")
mark_timing("3_parse_downloaded_files", t_parse_start)
t_join_start <- Sys.time()

# ---------------------------------------------------------------------
# 5. Join to the historical baseline and compute swing. Start from the
#    FULL municipality list (not just `live`), so a município whose file
#    failed to fetch/parse still shows up as "not yet reporting" with its
#    known turnout_2022 -- needed for the projection in step 6, which
#    must account for every município, reporting or not.
# ---------------------------------------------------------------------
baseline <- read_csv(file.path(OUT_DIR, BASELINE_FILE), col_types = cols(.default = "c")) %>%
  mutate(share = as.numeric(share), votos_validos_total = as.numeric(votos_validos_total))

# PT lineage: Haddad ran in 2018 (Lula was barred that year), Lula ran
# in 2022 and again in 2026 -- both baseline years are useful reference
# points for the same 2026 PT candidate. Bolsonaro lineage: Jair in 2018
# and 2022, Flávio in 2026 -- symmetric structure, see build_historical_
# baseline.R's header note.
pt_2018 <- baseline %>% filter(ano == 2018, linhagem == "PT") %>% select(codigo_tse, share_pt_2018 = share)
pt_2022 <- baseline %>% filter(ano == 2022, linhagem == "PT") %>%
  select(codigo_tse, share_pt_2022 = share, turnout_2022 = votos_validos_total)
bolso_2018 <- baseline %>% filter(ano == 2018, linhagem == "Bolsonaro") %>% select(codigo_tse, share_bolsonaro_2018 = share)
bolso_2022 <- baseline %>% filter(ano == 2022, linhagem == "Bolsonaro") %>% select(codigo_tse, share_bolsonaro_2022 = share)

# Região imediata (IBGE census geography) for the regional-correlation
# model in step 6 -- see build_historical_baseline.R's header note on
# regional_correlation_prior.json for why this specific clustering
# variable was chosen.
regional_clusters <- read_csv(file.path(OUT_DIR, "municipio_regional_clusters.csv"), col_types = cols(.default = "c")) %>%
  select(codigo_tse, cd_rgi)

comparison <- municipios %>%
  select(codigo_tse, uf) %>%
  left_join(live, by = "codigo_tse") %>%
  left_join(regional_clusters, by = "codigo_tse") %>%
  mutate(share_lula_2026 = votos_lula_2026 / votos_validos_total_2026,
         share_bolsonaro_2026 = votos_bolsonaro_2026 / votos_validos_total_2026) %>%
  left_join(pt_2018, by = "codigo_tse") %>%
  left_join(pt_2022, by = "codigo_tse") %>%
  left_join(bolso_2022, by = "codigo_tse") %>%
  left_join(bolso_2018, by = "codigo_tse") %>%
  mutate(
    swing_lula_vs_haddad_2018 = share_lula_2026 - share_pt_2018,
    swing_lula_vs_2022 = share_lula_2026 - share_pt_2022,
    swing_bolsonaro_vs_2022 = share_bolsonaro_2026 - share_bolsonaro_2022,
    swing_bolsonaro_vs_2018 = share_bolsonaro_2026 - share_bolsonaro_2018,
    # Município-level PT-minus-Bolsonaro MARGIN swing -- the probability
    # model below extrapolates and measures dispersion on THIS, rather
    # than on each candidate's swing separately, so a shared shock (e.g.
    # both losing ground to a third candidate) isn't double-counted as
    # "noise" in two places.
    margin_swing_vs_2022 = swing_lula_vs_2022 - swing_bolsonaro_vs_2022,
    margin_swing_vs_haddad_2018 = swing_lula_vs_haddad_2018 - swing_bolsonaro_vs_2018,
    margin_baseline_2022 = share_pt_2022 - share_bolsonaro_2022,
    margin_baseline_2018 = share_pt_2018 - share_bolsonaro_2018
  )

reporting <- comparison %>% filter(!is.na(pct_secoes_apuradas), pct_secoes_apuradas > 0)
not_reporting <- comparison %>% filter(is.na(pct_secoes_apuradas) | pct_secoes_apuradas == 0)
cat("\nMunicípios with SOME results in:", nrow(reporting), "of", nrow(comparison), "\n")
mark_timing("4_join_baseline_and_wrangle", t_join_start)
t_model_start <- Sys.time()

if (nrow(reporting) > 0) {
  needle <- reporting %>%
    summarise(
      n_municipios_reportando = n(),
      votos_totais_contados = sum(votos_validos_total_2026, na.rm = TRUE),
      needle_lula_vs_haddad_2018 = weighted.mean(swing_lula_vs_haddad_2018, votos_validos_total_2026, na.rm = TRUE),
      needle_lula_vs_2022 = weighted.mean(swing_lula_vs_2022, votos_validos_total_2026, na.rm = TRUE),
      needle_bolsonaro_vs_2022 = weighted.mean(swing_bolsonaro_vs_2022, votos_validos_total_2026, na.rm = TRUE),
      needle_bolsonaro_vs_2018 = weighted.mean(swing_bolsonaro_vs_2018, votos_validos_total_2026, na.rm = TRUE)
    )
  cat("\n=== NEEDLE (vote-weighted average swing, reporting municípios only) ===\n")
  print(needle)
}

# BY-STATE BREAKDOWN (added 2026-09-28 -- was missing entirely; the
# dashboard's by-state table (renderUfTable() in dashboard.html) only
# updates when doc.by_uf is present, and this script never computed
# or exported it, unlike the simulation test scripts which always had
# it. `uf` now survives the join above (comparison %>% select(codigo_tse, uf))
# specifically so this can be computed.
# Share of each state's expected valid votes counted so far: a município's
# expected total is its 2022 valid votes until it reaches 100% (then what
# was counted), never less than what's already counted.
uf_counted <- comparison %>%
  filter(!is.na(uf)) %>%
  mutate(counted = coalesce(votos_validos_total_2026, 0),
         expected = ifelse(!is.na(pct_secoes_apuradas) & pct_secoes_apuradas >= 100, counted,
                           pmax(counted, coalesce(turnout_2022, 0)))) %>%
  group_by(uf) %>%
  summarise(pct_counted = if (sum(expected) > 0) 100 * sum(counted) / sum(expected) else 0, .groups = "drop") %>%
  mutate(uf = toupper(uf))

by_uf <- if (nrow(reporting) > 0) {
  reporting %>%
    filter(!is.na(uf), !is.na(votos_validos_total_2026), votos_validos_total_2026 > 0) %>%
    group_by(uf) %>%
    summarise(lula_2026 = 100 * sum(votos_lula_2026, na.rm = TRUE) / sum(votos_validos_total_2026, na.rm = TRUE),
              bolsonaro_2026 = 100 * sum(votos_bolsonaro_2026, na.rm = TRUE) / sum(votos_validos_total_2026, na.rm = TRUE),
              .groups = "drop") %>%
    mutate(uf = toupper(uf)) %>%
    left_join(uf_counted, by = "uf")
} else {
  tibble(uf = character(), lula_2026 = numeric(), bolsonaro_2026 = numeric())
}

# ---------------------------------------------------------------------
# 6. Probabilistic needle: is PT (Lula) or the Bolsonaro lineage ahead
#    NATIONALLY once still-uncounted municípios are accounted for, and
#    how confident should that call be? Two distinct sources of
#    uncertainty are combined, per Miguel's request (2026-09-25):
#
#      (a) how FEW votes are counted so far -- mechanically shrinks as
#          the night goes on (more of the total is "known", less is
#          "projected").
#      (b) how CONSISTENT the observed município-level swing has been --
#          if municípios that have already reported are swinging by very
#          different amounts from EACH OTHER (not just from the
#          historical baseline), that is itself evidence the 2018/2022
#          "prior" is a less reliable guide to what's still to come this
#          cycle, and uncertainty should rise even if the vote-weighted
#          AVERAGE swing looks unremarkable on its own.
#
#    METHOD (a standard "swing-based projection", the same basic idea
#    real election needles use, kept as simple as is defensible):
#      1. mu = vote-weighted mean of the observed margin swing
#         (share_PT - share_Bolsonaro, vs. baseline year b) across
#         municípios that have reported so far.
#      2. sigma2_emp = the vote-weighted VARIANCE of that same swing --
#         how much reporting municípios disagree with each other.
#      3. sigma2_emp is shrunk (Bayesian/empirical-Bayes style) toward
#         the historical 2018->2022 dispersion computed once in
#         build_historical_baseline.R (swing_volatility_prior.json), so
#         the estimate isn't wildly unstable from a handful of
#         municípios. PRIOR_STRENGTH_K0 sets how many "effective"
#         reporting municípios it takes before the live data outweighs
#         that historical prior -- a tunable judgment call, not derived.
#      4. Effective sample size (n_eff) uses Kish's formula on VOTE
#         weights, not a simple município count -- so one giant city
#         reporting doesn't look like broad, representative geographic
#         coverage; a few small towns spread across several states can
#         carry more real information than one metropolis.
#      5. Each still-uncounted município is projected as its own
#         historical baseline margin + mu, weighted by its 2022 turnout
#         (a proxy for expected 2026 turnout -- population change since
#         2022 is not modeled).
#      6. The projected national margin's variance combines uncertainty
#         in mu itself (a shared/systematic error affecting every
#         uncounted município the same way) with leftover idiosyncratic
#         município-to-município noise (partially averaged out across
#         many uncounted municípios via the same Kish n_eff logic) --
#         both scaled by how large a share of the total vote is still
#         uncounted.
#      7. P(PT/Lula-lineage leads Bolsonaro-lineage nationally) =
#         Phi(projected_margin / se_projected), i.e. a normal
#         approximation around the projection.
#
#    REGIONAL CORRELATION (added 2026-09-25, per Miguel's request):
#    the version above treated every still-uncounted município as an
#    independent draw around the single national mu -- but a município's
#    swing is strongly correlated with its NEIGHBORS' (historically,
#    IBGE's região imediata -- the census geography grouping each
#    município with the ~10 others it shares a local economy with --
#    explains ~82% of the município-level variance in 2018->2022 margin
#    swing; see regional_correlation_prior.json). Two consequences,
#    both now modeled explicitly instead of assumed away:
#      (a) reporting municípios concentrated in a FEW regiões carry much
#          less real geographic information than the same COUNT spread
#          across many regiões would -- Kish's n_eff on vote weights
#          alone can't tell those apart (it only sees weight
#          concentration, not which regions the weight comes from), so
#          a "design effect" correction (standard in cluster-sampling
#          survey statistics) is applied on top of it.
#      (b) a região that IS reporting, and swinging differently from the
#          national trend, should pull its own still-uncounted neighbors
#          toward ITS trend -- not the flat national one. Every região
#          gets its own swing estimate (its own observed swing, shrunk
#          toward the national mu by how much local data it actually
#          has); a região with zero reports simply falls back to the
#          national estimate.
#
#    METHOD, building on the swing-based projection above:
#      1. mu_global, as before, but its variance decomposed by região
#         imediata (cd_rgi) via a weighted one-way ANOVA into a
#         BETWEEN-região component (regiões' own averages differing from
#         the national one) and a WITHIN-região component (municípios
#         differing from their own região's average) -- both shrunk
#         (empirical-Bayes) toward the historical 2018->2022 decomposition,
#         analogous to how sigma2_emp was shrunk toward a single prior
#         before, just split into two components now.
#      2. rho = between / (between+within) -- how strongly neighboring
#         municípios actually move together. Feeds a design-effect
#         correction: n_eff_corrected = n_eff_raw / (1 + (avg reporting
#         municípios per reporting região - 1) * rho).
#      3. Per-região shrunk swing mu_c: precision-weighted average of
#         that região's own observed swing and the (design-effect-
#         corrected) national mu_global -- classic normal-normal
#         conjugate shrinkage, so a região with lots of its own reports
#         trusts itself, one with few or none defers to the national
#         estimate.
#      4. Each still-uncounted município is projected using its OWN
#         região's mu_c (not the flat national mu). The projected
#         national margin's variance sums each região's remaining-vote
#         contribution independently (regiões are numerous -- ~510
#         nationally -- and geographically bounded, so treating THEM as
#         independent, while municípios WITHIN one stay correlated via
#         the shared mu_c, is a defensible middle ground -- not the same
#         as assuming full município-level independence).
#
#    HONEST LIMITATIONS: (i) this is a two-level hierarchy (município
#    within região within nation) -- a real vote-counting process also
#    has state-level and time-of-day effects this doesn't separately
#    model, though much of that should already be captured by regiões
#    correlating with their state; (ii) rho is calibrated from a single
#    historical interval (2018->2022), same caveat as the dispersion
#    prior; (iii) still only models the Lula-vs-Bolsonaro-lineage horse
#    race, not a formal >50% outright-win threshold.
# ---------------------------------------------------------------------
regional_prior <- fromJSON(file.path(OUT_DIR, PRIOR_FILE))
PRIOR_SIGMA2_WITHIN_RGI <- regional_prior$prior_sigma2_within_rgi
PRIOR_SIGMA2_BETWEEN_RGI <- regional_prior$prior_sigma2_between_rgi
PRIOR_STRENGTH_K0 <- 30  # pseudo-count: ~30 "effective" (vote-weighted, Kish) reporting municípios before empirical WITHIN-região dispersion outweighs the 2018->2022 historical prior
K0_BETWEEN <- 15  # pseudo-count: ~15 reporting regiões before empirical BETWEEN-região dispersion outweighs the historical prior -- regiões, not municípios, are the relevant "sample size" here

# UNCERTAINTY INFLATION + MONTE CARLO PARAMETER PROPAGATION (added
# 2026-09-27, per Miguel: "This is excessive certainty in general...
# we need to add more uncertainty someway" -- followed up with "lets
# make SE wider as well... doubling the uncertainty"). Two separate
# changes, both applied below:
#   (1) UNCERTAINTY_INFLATION_FACTOR multiplies sigma2_within_blended
#       and sigma2_between_blended by 2 BEFORE anything downstream uses
#       them -- rho_blended is unaffected (both components scale
#       equally, so their ratio doesn't move), only the absolute
#       magnitude of the modeled dispersion doubles. This is a blunt,
#       explicit hedge: Miguel judged the historical-prior-calibrated
#       variance too confident and asked to double it outright, not a
#       value derived from further calibration.
#   (2) sigma2_within_blended/sigma2_between_blended (after inflation)
#       are no longer treated as the TRUE variances -- they set the
#       MEAN of an inverse-gamma posterior (shape tied to the same
#       PRIOR_STRENGTH_K0/K0_BETWEEN pseudo-counts already used to
#       blend them), N_DRAWS_MC values are drawn from each, and every
#       downstream quantity (per-região shrinkage, national projection,
#       final probability) is recomputed per draw and averaged --
#       fully vectorized as matrix ops across draws x régiões, no
#       per-draw R-level loop (prototyped + timed in mc_prototype.R,
#       2026-09-27: 2,000 draws added ~0.3s per call at full national
#       load, i.e. negligible next to the ~88s fetch). This replaces
#       the previous Student's-t end-of-pipeline patch (df_t below is
#       now only an informational field, not used in the probability
#       calc) with a properly propagated posterior-predictive estimate
#       that folds in uncertainty about sigma2/rho THEMSELVES, not just
#       the number of régiões reporting.
# Parameterized on the SE multiplier directly (what Miguel actually
# asked for: "make SE wider... doubling the uncertainty"), not on the
# variance -- variance and SE don't scale 1:1 (SE = sqrt(variance)), so
# doubling sigma2_within_blended/sigma2_between_blended only widens SE
# by sqrt(2) ~= 1.41x, confirmed empirically (test_inflation_effect.R,
# 2026-09-27: SE ratio measured at exactly 1.4142 for a variance
# factor of 2). To get SE itself 2x wider, the VARIANCE factor must be
# 2^2 = 4.
SE_INFLATION_FACTOR <- 2
SYS_ERROR_K_PP <- 1.8  # calibrated on 2026-10-04 1st round; see compute_needle_probability()
UNCERTAINTY_INFLATION_FACTOR <- SE_INFLATION_FACTOR^2  # = 4; applied to sigma2_within_blended/sigma2_between_blended below
N_DRAWS_MC <- 2000

compute_needle_probability <- function(df_reporting, df_not_reporting, margin_swing_col, margin_baseline_col,
                                        votes_pt_col = "votos_lula_2026", votes_bolso_col = "votos_bolsonaro_2026",
                                        prior_scale = 1, threshold = 0) {
  # prior_scale/threshold let the same model project one candidate's SHARE
  # (prior variance scaled to share units) and P(share > threshold).
  PRIOR_SIGMA2_WITHIN_RGI <- PRIOR_SIGMA2_WITHIN_RGI * prior_scale
  PRIOR_SIGMA2_BETWEEN_RGI <- PRIOR_SIGMA2_BETWEEN_RGI * prior_scale
  rep_valid <- df_reporting %>%
    filter(!is.na(.data[[margin_swing_col]]), !is.na(votos_validos_total_2026), votos_validos_total_2026 > 0)
  # A município can show pct_secoes_apuradas > 0 (so it lands in
  # `df_reporting`) yet still have no usable margin_swing -- a name-
  # match miss, a malformed file, or (as with the simulado's placeholder
  # candidates) a genuinely absent signal. Such a município must NOT
  # just vanish from both pools: fold it into the extrapolation pool
  # (using its own historical turnout/baseline, exactly like a genuinely
  # not-yet-reporting município) so its uncertainty is still counted --
  # otherwise the projected SE can silently collapse toward 0 simply
  # because a chunk of the country dropped out of the calculation.
  rep_invalid <- df_reporting %>%
    filter(is.na(.data[[margin_swing_col]]) | is.na(votos_validos_total_2026) | votos_validos_total_2026 <= 0)
  df_not_reporting <- bind_rows(df_not_reporting, rep_invalid)

  w <- rep_valid$votos_validos_total_2026
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

    cluster_stats <- rep_valid %>%
      filter(!is.na(cd_rgi)) %>%
      group_by(cd_rgi) %>%
      summarise(cmean = weighted.mean(.data[[margin_swing_col]], votos_validos_total_2026),
                cweight = sum(votos_validos_total_2026),
                n_eff_c = (sum(votos_validos_total_2026))^2 / sum(votos_validos_total_2026^2),
                cn = n(), .groups = "drop")
    n_clusters_reporting <- nrow(cluster_stats)

    if (n_clusters_reporting >= 2) {
      sigma2_between_emp_raw <- sum(cluster_stats$cweight * (cluster_stats$cmean - mu_global)^2) / sum(cluster_stats$cweight)
      within_join <- rep_valid %>% filter(!is.na(cd_rgi)) %>%
        left_join(cluster_stats %>% select(cd_rgi, cmean), by = "cd_rgi")
      sigma2_within_emp <- sum(within_join$votos_validos_total_2026 * (within_join[[margin_swing_col]] - within_join$cmean)^2) / sum(within_join$votos_validos_total_2026)
      # BIAS CORRECTION (added 2026-09-25, per Miguel's request): the raw
      # between-cluster estimate is inflated even under TRUE independence,
      # since a cluster's mean has its own sampling variance
      # (sigma2_within/n_eff_c) that isn't part of the real between-
      # cluster signal -- classic small-group ANOVA bias, and régiões
      # imediatas (~11 municípios average) are small enough for it to
      # matter. Same correction as build_historical_baseline.R's prior
      # calibration; without it, rho stays inflated even in a scenario
      # with NO true regional structure (confirmed via a synthetic i.i.d.
      # test -- see session log, 2026-09-25).
      bias_correction_factor <- sum(cluster_stats$cweight / cluster_stats$n_eff_c) / sum(cluster_stats$cweight)
      sigma2_between_emp <- max(0, sigma2_between_emp_raw - sigma2_within_emp * bias_correction_factor)
    } else {
      # Not enough distinct regiões reporting yet to separate the two
      # components -- pool everything as "within" (conservative: the
      # between-região component then relies entirely on the historical
      # prior, since we have no live evidence about it yet).
      sigma2_between_emp <- NA_real_
      sigma2_within_emp <- if (n_mun_reporting > 1) sum(w * (x - mu_global)^2) / sum(w) else NA_real_
    }
  }

  sigma2_within_blended <- if (is.na(sigma2_within_emp)) PRIOR_SIGMA2_WITHIN_RGI else
    (n_eff_global * sigma2_within_emp + PRIOR_STRENGTH_K0 * PRIOR_SIGMA2_WITHIN_RGI) / (n_eff_global + PRIOR_STRENGTH_K0)
  sigma2_between_blended <- if (is.na(sigma2_between_emp)) PRIOR_SIGMA2_BETWEEN_RGI else
    (n_clusters_reporting * sigma2_between_emp + K0_BETWEEN * PRIOR_SIGMA2_BETWEEN_RGI) / (n_clusters_reporting + K0_BETWEEN)

  # DOUBLE THE UNCERTAINTY (see UNCERTAINTY_INFLATION_FACTOR note above)
  # -- applied to both components equally so rho_blended (their ratio)
  # is unchanged; only the absolute dispersion doubles.
  sigma2_within_blended <- sigma2_within_blended * UNCERTAINTY_INFLATION_FACTOR
  sigma2_between_blended <- sigma2_between_blended * UNCERTAINTY_INFLATION_FACTOR

  rho_blended <- sigma2_between_blended / (sigma2_between_blended + sigma2_within_blended)

  avg_cluster_size <- if (n_mun_reporting > 0 && n_clusters_reporting > 0) n_mun_reporting / n_clusters_reporting else 1
  design_effect <- 1 + (avg_cluster_size - 1) * rho_blended
  n_eff <- if (n_eff_global > 0) n_eff_global / design_effect else 0
  se_mu_global <- if (n_eff > 0) sqrt(sigma2_within_blended / n_eff) else sqrt(sigma2_within_blended + sigma2_between_blended)
  precision_global <- if (se_mu_global > 0) 1 / se_mu_global^2 else 0

  known_margin_votes <- sum(rep_valid[[votes_pt_col]] - rep_valid[[votes_bolso_col]], na.rm = TRUE)
  known_valid_votes <- votes_counted
  mu_used_global <- if (is.na(mu_global)) 0 else mu_global

  # df_t kept as an informational field only (see note above) -- no
  # longer feeds the probability calc, that's now the MC layer below.
  df_t <- max(1, n_clusters_reporting - 1)

  # Votes still to come: every município with no results yet, PLUS the
  # uncounted remainder of partially counted ones (2022 votes minus what's
  # counted, zero once at 100%). Fixed 2026-10-04: the remainder used to be
  # dropped, so a município reporting one section left the projection and
  # the expected-vote denominator almost entirely (e.g. São Paulo city at
  # 1% counted counted as done), roughly halving the expected total.
  rep_remainder <- rep_valid %>%
    mutate(turnout_2022 = ifelse(pct_secoes_apuradas >= 100, 0,
                                 pmax(0, turnout_2022 - votos_validos_total_2026))) %>%
    filter(turnout_2022 > 0)
  rem <- bind_rows(df_not_reporting, rep_remainder) %>%
    filter(!is.na(turnout_2022), !is.na(.data[[margin_baseline_col]])) %>%
    mutate(cd_rgi_group = ifelse(is.na(cd_rgi), paste0("__no_rgi_", codigo_tse), cd_rgi))

  remaining_valid_votes <- sum(rem$turnout_2022)
  projected_total_valid <- known_valid_votes + remaining_valid_votes
  baseline_term_fixed <- sum(rem$turnout_2022 * rem[[margin_baseline_col]])  # draw-independent part of the projection

  safe_div <- function(num, denom) ifelse(denom > 0, num / denom, 0)

  rem_grouped <- rem %>%
    group_by(cd_rgi_group) %>%
    summarise(
      w_c = sum(turnout_2022),
      n_eff_c = if (sum(turnout_2022) > 0) (sum(turnout_2022))^2 / sum(turnout_2022^2) else 0,
      cd_rgi_val = first(cd_rgi),
      .groups = "drop"
    )
  # Groups (régiões) whose OWN reporting municípios gave us cluster_stats
  # get draw-dependent shrinkage below; every other group (no reports in
  # that região yet, or missing cd_rgi entirely) falls back to the
  # national mu_used_global for its center, but still gets draw-
  # dependent uncertainty (var_mu_fallback = 1/precision_global_draws).
  is_instats <- rem_grouped$cd_rgi_val %in% cluster_stats$cd_rgi
  instats_groups <- rem_grouped[is_instats, ]
  fallback_groups <- rem_grouped[!is_instats, ]
  idx_map <- match(instats_groups$cd_rgi_val, cluster_stats$cd_rgi)

  # --- POSTERIOR DRAWS: sigma2_*_blended (already doubled above) set
  #     the posterior MEAN; PRIOR_STRENGTH_K0/K0_BETWEEN (plus how much
  #     live data has come in) set how tightly concentrated the
  #     posterior is around that mean -- more reporting régiões => less
  #     draw-to-draw spread, same as the existing blending logic already
  #     implies. alpha > 1 always (K0/2 = 15 or 7.5 at minimum), so beta
  #     is always well-defined. ---
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

  # --- per-região (reporting) shrinkage: n_draws x n_regiões matrices ---
  precision_own_mat <- outer(1 / sigma2_within_draws, cluster_stats$n_eff_c)
  cmean_mat <- matrix(cluster_stats$cmean, nrow = N_DRAWS_MC, ncol = n_clusters_reporting, byrow = TRUE)
  numerator <- precision_own_mat * cmean_mat + precision_global_draws * mu_used_global
  denominator <- precision_own_mat + precision_global_draws
  mu_c_shrunk_mat <- numerator / denominator
  var_mu_c_shrunk_mat <- 1 / denominator

  # --- instats groups' contribution to the projection (matrix-vector
  #     products across all draws at once -- no per-draw loop) ---
  term2_instats_draws <- as.vector(mu_c_shrunk_mat[, idx_map, drop = FALSE] %*% instats_groups$w_c)
  w_over_total_sq_instats <- if (projected_total_valid > 0) (instats_groups$w_c / projected_total_valid)^2 else rep(0, nrow(instats_groups))
  term_a_instats_draws <- as.vector(var_mu_c_shrunk_mat[, idx_map, drop = FALSE] %*% w_over_total_sq_instats)
  C_instats <- sum(safe_div(w_over_total_sq_instats, instats_groups$n_eff_c))
  contrib_instats_draws <- term_a_instats_draws + sigma2_within_draws * C_instats

  # --- fallback groups: mu_c_shrunk == mu_used_global for every draw
  #     (fixed), only their uncertainty term varies by draw ---
  term2_fallback_fixed <- sum(fallback_groups$w_c) * mu_used_global
  w_over_total_sq_fallback <- if (projected_total_valid > 0) (fallback_groups$w_c / projected_total_valid)^2 else rep(0, nrow(fallback_groups))
  A_fallback <- sum(w_over_total_sq_fallback)
  B_fallback <- sum(safe_div(w_over_total_sq_fallback, fallback_groups$n_eff_c))
  # Unreported regions: the global-mean error is shared by all of them, so it
  # enters as (sum w/T)^2, and each region's own deviation from the global
  # mean adds sigma2_between independently (fixed 2026-10-04: both were
  # missing/understated, collapsing the SE once the global mean firmed up).
  share_fallback <- if (projected_total_valid > 0) sum(fallback_groups$w_c) / projected_total_valid else 0
  contrib_fallback_draws <- share_fallback^2 / precision_global_draws +
    A_fallback * sigma2_between_draws + B_fallback * sigma2_within_draws

  remaining_margin_votes_draws <- baseline_term_fixed + term2_instats_draws + term2_fallback_fixed
  var_projection_draws <- pmax(contrib_instats_draws + contrib_fallback_draws, 0)
  # Systematic reporting-order error: on 2026-10-04 the projection leaned
  # toward Bolsonaro all night (late-counted votes were more pro-Lula than
  # their region's swing predicted), with an error of ~1.8pp x sqrt(share
  # still uncounted) -- 2-7x the sampling SE above, which never covered the
  # final result. Added as independent variance; prior_scale converts it
  # to share units for the single-candidate share projection.
  frac_counted_now <- if (projected_total_valid > 0) known_valid_votes / projected_total_valid else 0
  var_projection_draws <- var_projection_draws + prior_scale * (SYS_ERROR_K_PP / 100)^2 * max(0, 1 - frac_counted_now)
  projected_margin_share_draws <- if (projected_total_valid > 0)
    (known_margin_votes + remaining_margin_votes_draws) / projected_total_valid else rep(NA_real_, N_DRAWS_MC)

  # Posterior-predictive probability: average, over draws, of the
  # normal-CDF probability PT leads GIVEN that draw's (sigma2_within,
  # sigma2_between) -- a proper mixture, not a single plug-in estimate.
  # Reported margin/SE use the law of total variance (within-draw
  # expected variance + between-draw variance of the mean) so both
  # sources of uncertainty show up in projecao_erro_padrao_pp.
  prob_pt_leads <- if (projected_total_valid <= 0) NA_real_ else
    mean(pnorm((projected_margin_share_draws - threshold) / sqrt(pmax(var_projection_draws, .Machine$double.eps))))
  projected_margin_share <- if (projected_total_valid > 0) mean(projected_margin_share_draws) else NA_real_
  var_projection <- if (projected_total_valid > 0) mean(var_projection_draws) + var(projected_margin_share_draws) else NA_real_
  se_projection <- sqrt(var_projection)

  tibble(
    n_municipios_reportando = n_mun_reporting,
    n_regioes_reportando = n_clusters_reporting,
    votos_contados = votes_counted,
    frac_votos_esperados_contados = if (projected_total_valid > 0) known_valid_votes / projected_total_valid else 0,
    mean_swing_margem_pp = mu_global * 100,
    rho_regional = rho_blended,
    design_effect = design_effect,
    sigma_empirica_pp = if (!is.na(sigma2_within_emp)) sqrt(sigma2_within_blended + sigma2_between_blended) * 100 else NA_real_,
    dispersao_vs_historico = if (!is.na(sigma2_within_emp)) (sigma2_within_blended + sigma2_between_blended) / (PRIOR_SIGMA2_WITHIN_RGI + PRIOR_SIGMA2_BETWEEN_RGI) else NA_real_,
    projecao_margem_pp = projected_margin_share * 100,
    projecao_erro_padrao_pp = se_projection * 100,
    graus_liberdade_t = df_t,
    mc_n_draws = N_DRAWS_MC,
    se_inflation_factor = SE_INFLATION_FACTOR,
    prob_pt_lidera = prob_pt_leads
  )
}

result_2022 <- compute_needle_probability(reporting, not_reporting, "margin_swing_vs_2022", "margin_baseline_2022")
result_2018 <- compute_needle_probability(reporting, not_reporting, "margin_swing_vs_haddad_2018", "margin_baseline_2018")
# First-round question: Bolsonaro's projected share of ALL valid votes and
# P(> 50%). Same model, applied to his own share swing; votes_bolso_col is a
# zero column so "known margin votes" are just his counted votes. Prior
# variance scaled to share units (a margin swing is ~2x a share swing).
result_bolso_share <- compute_needle_probability(reporting %>% mutate(zero_votes = 0), not_reporting,
                                                 "swing_bolsonaro_vs_2022", "share_bolsonaro_2022",
                                                 votes_pt_col = "votos_bolsonaro_2026", votes_bolso_col = "zero_votes",
                                                 prior_scale = 0.25, threshold = 0.5)

cat("\n=== PROBABILISTIC NEEDLE -- baseline 2022 ===\n"); print(result_2022)
cat("\n=== PROBABILISTIC NEEDLE -- baseline 2018 (Haddad) ===\n"); print(result_2018)
mark_timing("5_model_estimation", t_model_start)
t_write_start <- Sys.time()

summary_out <- list(
  round = ROUND,
  status = "live",
  updated_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%OS3Z", tz = "UTC"),
  n_municipios = nrow(reporting),
  pct_municipios = 100 * nrow(reporting) / nrow(comparison),
  votos_totais = sum(reporting$votos_validos_total_2026, na.rm = TRUE),
  needle_lula_vs_haddad_2018 = if (nrow(reporting) > 0) needle$needle_lula_vs_haddad_2018 * 100 else NA_real_,
  needle_lula_vs_2022 = if (nrow(reporting) > 0) needle$needle_lula_vs_2022 * 100 else NA_real_,
  needle_bolsonaro_vs_2022 = if (nrow(reporting) > 0) needle$needle_bolsonaro_vs_2022 * 100 else NA_real_,
  needle_bolsonaro_vs_2018 = if (nrow(reporting) > 0) needle$needle_bolsonaro_vs_2018 * 100 else NA_real_,
  prob_lula_2022 = result_2022$prob_pt_lidera,
  prob_lula_2018 = result_2018$prob_pt_lidera,
  margin_projected_2022_pp = result_2022$projecao_margem_pp,
  margin_projected_2018_pp = result_2018$projecao_margem_pp,
  margin_se_2022_pp = result_2022$projecao_erro_padrao_pp,
  margin_se_2018_pp = result_2018$projecao_erro_padrao_pp,
  dispersion_ratio_2022 = result_2022$dispersao_vs_historico,
  dispersion_ratio_2018 = result_2018$dispersao_vs_historico,
  frac_votes_counted = result_2022$frac_votos_esperados_contados,
  rho_regional_2022 = result_2022$rho_regional,
  rho_regional_2018 = result_2018$rho_regional,
  design_effect_2022 = result_2022$design_effect,
  design_effect_2018 = result_2018$design_effect,
  n_regioes_reportando_2022 = result_2022$n_regioes_reportando,
  bolsonaro_share_projected_pct = result_bolso_share$projecao_margem_pp,
  bolsonaro_share_se_pp = result_bolso_share$projecao_erro_padrao_pp,
  prob_bolsonaro_first_round = result_bolso_share$prob_pt_lidera,
  by_uf = by_uf
)
write_json(summary_out, file.path(OUT_DIR, "live_needle_summary_latest.json"), auto_unbox = TRUE, pretty = TRUE, na = "null")
cat("\nSaved probabilistic summary to live_needle_summary_latest.json (use this to update the dashboard's DB doc)\n")

# ---------------------------------------------------------------------
# 7. Append this run to the persistent history log (added 2026-09-25,
#    per Miguel's request for a visualization of how the prediction
#    evolves as data comes in). Every run of this script adds one row;
#    across many runs through election night this builds the full
#    trajectory of the projection and its confidence interval -- see
#    plot_needle_history.R. Scoped to CACHE_KEY (same as the completed-
#    municípios cache) so simulado test runs and the real election never
#    mix into the same history file.
# ---------------------------------------------------------------------
# by_uf is a nested per-state breakdown (a list-column once run through
# as_tibble()) -- belongs in the JSON pushed to the dashboard, not in
# this scalar per-run history CSV, and write_csv() can't serialize a
# list column anyway (broke here 2026-09-28 when by_uf was first added).
history_row <- as_tibble(summary_out[setdiff(names(summary_out), "by_uf")]) %>% rename(run_timestamp = updated_at)
HISTORY_PATH <- file.path(OUT_DIR, paste0("needle_history_", CACHE_KEY, ".csv"))
write_csv(history_row, HISTORY_PATH, append = file.exists(HISTORY_PATH))
cat("Appended run to", HISTORY_PATH, "\n")

timestamp <- format(Sys.time(), "%Y%m%d_%H%M%S")
write_csv(comparison, file.path(OUT_DIR, paste0("live_snapshot_", timestamp, ".csv")))
write_csv(comparison, file.path(OUT_DIR, "live_snapshot_latest.csv"))
cat("\nSaved snapshot to live_snapshot_", timestamp, ".csv (and _latest.csv)\n", sep = "")

mark_timing("6_local_write", t_write_start)
mark_timing("TOTAL_script", t_script_start)

cat("\n=== TIMING BREAKDOWN (this run) ===\n")
timing_tbl <- tibble(phase = names(timing_log), seconds = round(unlist(timing_log), 3))
print(timing_tbl, n = 20)
write_csv(timing_tbl, file.path(OUT_DIR, paste0("timing_", CACHE_KEY, "_", timestamp, ".csv")))
cat("NOTE: does not include the dashboard push (ArtifactData calls made separately after this script finishes).\n")
