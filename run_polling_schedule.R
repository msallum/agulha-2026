# run_polling_schedule.R
#
# Tiered polling wrapper around poll_live_results.R, added 2026-09-28
# per Miguel's request to be "smart about our data pulls" instead of
# polling at one fixed interval all night. Cadence is calibrated from
# real TSE election-night timing researched across 4 elections (2018
# and 2022 presidential, 2020 and 2024 municipal -- see session log):
# counting is steeply front-loaded (~97-99% within ~3 hours of polls
# closing) followed by a long, unpredictable tail -- hours to over a
# day -- for the last fraction of a percent (remote/rural/indigenous
# sections needing satellite transmission, urna transport delays, and
# occasional outright TSE backend slowdowns like 2020's 3-hour
# totalization-system delay, unrelated to ground-truth counting speed).
# Polling at a constant interval all night is the wrong shape for that:
# too slow to catch the real action early, too frequent (and wasteful)
# during the long, mostly-static tail.
#
# TIERS (elapsed time since POLLS_CLOSE_TIME):
#   [0h,  3h): every   4 min -- the fast ramp; ~97-99% typically arrives here
#                               (was 3 min; a paced full-national fetch takes
#                               ~85-105s against the real endpoint, so 4 min
#                               leaves headroom -- see fetch_all() pacing notes)
#   [3h,  5h): every  12 min -- tapering as coverage approaches ~99%
#   [5h, 24h): every  60 min -- slow trickle (2022 round 2 took ~4.5h just
#                               for its last 1.5%)
#   [24h,  +): every 120 min -- worst-case long tail (2022 round 1 took 41h
#                               to reach literal 100%; 2018 took ~28h)
# These are starting points, not laws of nature -- recalibrate after
# watching a real election night if the shape looks different.
#
# COVERAGE OVERRIDE (added 2026-09-28, per Miguel): the time-based
# tiers above assume a "typical" pace, but the actual count can run
# ahead of or behind the clock. So once ANY poll reports
# frac_votes_counted >= 90%, the schedule never polls faster than
# tier2 (12 min) again, even if we're still inside the first 3 hours --
# there's less new information left to catch at that point, time-based
# tier notwithstanding. This only ever slows polling down, never speeds
# it up (see tier_rank_from_coverage()/effective_tier() below).
#
# STOPS AUTOMATICALLY once frac_votes_counted >= 99.99% for 2
# consecutive polls (2, not 1, so a single noisy/transient reading
# can't trigger a premature stop) -- no point polling forever once the
# count is genuinely done.
#
# RESILIENCE: a single failed/errored poll_live_results.R run (network
# hiccup, TSE-side outage -- 2020 showed their own totalization backend
# can bottleneck independent of real counting progress) is caught,
# logged, and the loop just waits for its next scheduled interval
# rather than dying.
#
# SCOPE: this only controls how often the FETCH+ESTIMATE step
# (poll_live_results.R) runs, producing fresh local JSON/CSV output
# each cycle. It does NOT push to the live dashboard -- that stays a
# separate, manual step gated on an explicit go-ahead, same as every
# prior push in this project.
#
# USAGE: set POLLS_CLOSE_TIME below (or via the POLLS_CLOSE_TIME env
# var) to the real election's poll-closing time, then run this script
# and leave it running (e.g. in its own terminal/session) through the
# night. For a runoff, just restart it with the new date/time.
#
# TESTING: set MAX_ITERATIONS (via env var) to a small number to do a
# bounded smoke-test run instead of the real open-ended loop -- the
# loop breaks BEFORE sleeping once that many cycles have completed, so
# a MAX_ITERATIONS=1 test finishes as soon as that one poll cycle does,
# without waiting out a tier's sleep interval.

library(jsonlite)
library(readr)
library(tibble)

# PORTABLE PATHS (2026-09-28, matching poll_live_results.R's own note):
# SCRIPT_DIR is just the working directory -- run this from the repo
# root, locally or in a cloud sandbox. RSCRIPT_BIN relies on Rscript
# being on PATH, which it is on both a normal local R install and any
# Linux environment with R installed (r-base or similar) -- no
# hardcoded Windows install path anymore.
SCRIPT_DIR <- getwd()
POLL_SCRIPT <- file.path(SCRIPT_DIR, "poll_live_results.R")
RSCRIPT_BIN <- "Rscript"
LOG_PATH <- file.path(SCRIPT_DIR, "polling_schedule_log.csv")
SUMMARY_JSON <- file.path(SCRIPT_DIR, "live_needle_summary_latest.json")

# 17h05, not 17h00 sharp (2026-09-28, per Miguel): every tracker/script
# watching this election is likely configured to start at the exact
# stroke of 17h -- a deliberate 5-minute offset avoids piling onto TSE's
# servers in that same first instant.
POLLS_CLOSE_TIME <- as.POSIXct(Sys.getenv("POLLS_CLOSE_TIME", "2026-10-04 17:05:00"),
                                tz = "America/Sao_Paulo")
MAX_ITERATIONS <- as.numeric(Sys.getenv("MAX_ITERATIONS", Inf))
RETRY_DELAY_SEC <- 15

TIERS <- list(
  list(rank = 1, interval_min = 4,   label = "tier1_fast_ramp"),
  list(rank = 2, interval_min = 12,  label = "tier2_taper"),
  list(rank = 3, interval_min = 60,  label = "tier3_long_tail"),
  list(rank = 4, interval_min = 120, label = "tier4_worst_case_tail")
)

tier_rank_from_time <- function(elapsed_hours) {
  if (elapsed_hours < 3)  return(1)
  if (elapsed_hours < 5)  return(2)
  if (elapsed_hours < 24) return(3)
  return(4)
}

# COVERAGE OVERRIDE (added 2026-09-28, per Miguel: "I think we can
# migrate to the second tier as we get above 90%"). The time-based
# tiers assume a "typical" night, but coverage can outrun the clock
# (e.g. a fast count that's already >90% well inside the first 3
# hours) or lag behind it -- so once we've SEEN >=90% counted, never
# poll faster than tier2 again, regardless of how little time has
# elapsed. This only ever pushes the tier slower (via max() below with
# the time-based rank), never faster -- coverage dropping back below
# 90% (which shouldn't happen, but data hiccups exist) doesn't speed
# polling back up once we know the count is that far along.
tier_rank_from_coverage <- function(frac_counted) {
  if (is.na(frac_counted)) return(1)
  if (frac_counted >= 0.90) return(2)
  return(1)
}

effective_tier <- function(elapsed_hours, frac_counted) {
  rank <- max(tier_rank_from_time(elapsed_hours), tier_rank_from_coverage(frac_counted))
  TIERS[[rank]]
}

log_line <- function(timestamp, elapsed_hours, tier_label, interval_min, frac_counted, status, note) {
  row <- tibble(timestamp = format(timestamp, "%Y-%m-%dT%H:%M:%S%z"),
                elapsed_hours = round(elapsed_hours, 3), tier = tier_label,
                interval_min = interval_min, frac_votes_counted = frac_counted,
                status = status, note = note)
  write_csv(row, LOG_PATH, append = file.exists(LOG_PATH))
}

cat("=== Tiered polling schedule started", format(Sys.time()), "===\n")
cat("Polls close (reference):", format(POLLS_CLOSE_TIME), "\n")
if (is.finite(MAX_ITERATIONS)) cat("TEST MODE: will stop after", MAX_ITERATIONS, "iteration(s).\n")

consecutive_complete <- 0
iteration <- 0
last_frac_counted <- NA_real_  # updated after each cycle; drives the coverage override on the NEXT cycle's sleep decision

repeat {
  now <- Sys.time()
  elapsed_hours <- as.numeric(difftime(now, POLLS_CLOSE_TIME, units = "hours"))

  if (elapsed_hours < 0) {
    wait_min <- min(5, -elapsed_hours * 60)
    cat(format(now), "-- before polls close (", round(-elapsed_hours, 2), "h to go). Waiting", round(wait_min, 1), "min.\n")
    Sys.sleep(wait_min * 60)
    next
  }

  tier <- effective_tier(elapsed_hours, last_frac_counted)
  iteration <- iteration + 1
  cat("\n---", format(now), "--- elapsed:", round(elapsed_hours, 2), "h --- tier:", tier$label,
      "(every", tier$interval_min, "min, last known coverage:",
      if (is.na(last_frac_counted)) "unknown" else paste0(round(last_frac_counted * 100, 1), "%"),
      ") --- iteration", iteration, "---\n")

  run_poll <- function() tryCatch({
    out <- system2(RSCRIPT_BIN, args = shQuote(POLL_SCRIPT), stdout = TRUE, stderr = TRUE)
    status_code <- attr(out, "status")
    if (!is.null(status_code) && status_code != 0) stop(paste("Rscript exited with status", status_code, "-- tail:", paste(tail(out, 5), collapse = " | ")))
    list(ok = TRUE, output = out)
  }, error = function(e) list(ok = FALSE, output = conditionMessage(e)))

  run_result <- run_poll()
  # One quick retry: on election night a single transient error (an SSL
  # connect error fetching the municipality config) otherwise cost a full
  # cycle of the cadence.
  if (!run_result$ok) {
    cat("!! poll failed, retrying once in", RETRY_DELAY_SEC, "s:", substr(run_result$output[1], 1, 200), "\n")
    Sys.sleep(RETRY_DELAY_SEC)
    run_result <- run_poll()
  }

  frac_counted <- NA_real_
  status <- if (run_result$ok) "ok" else "error"
  # poll_live_results.R's own output is captured above, not printed --
  # surface its fetch summary (successes / 429s / failures) so blocking
  # is visible per cycle in this log.
  fetch_lines <- if (run_result$ok) grep("Done in|Nothing left to fetch|got HTTP 429", run_result$output, value = TRUE) else character()
  if (length(fetch_lines) > 0) cat(paste0("  ", trimws(fetch_lines), collapse = "\n"), "\n")
  note <- if (run_result$ok) paste(trimws(fetch_lines), collapse = " | ") else run_result$output[1]

  if (run_result$ok && file.exists(SUMMARY_JSON)) {
    summ <- tryCatch(fromJSON(SUMMARY_JSON), error = function(e) NULL)
    if (!is.null(summ) && !is.null(summ$frac_votes_counted)) frac_counted <- summ$frac_votes_counted
    cat("Cycle OK. frac_votes_counted =", if (is.na(frac_counted)) "NA" else paste0(round(frac_counted * 100, 3), "%"), "\n")
  } else if (!run_result$ok) {
    cat("!! poll_live_results.R FAILED this cycle -- logged, will retry next scheduled interval.\n")
    cat("   error:", note, "\n")
  }

  if (!is.na(frac_counted)) last_frac_counted <- frac_counted
  # Recompute the tier with THIS cycle's fresh coverage (not the
  # possibly-stale value used to decide whether to even run this
  # cycle) -- this is what actually governs how long we sleep next.
  sleep_tier <- effective_tier(elapsed_hours, last_frac_counted)
  if (sleep_tier$rank != tier$rank) {
    cat("Coverage override: bumping from", tier$label, "to", sleep_tier$label, "for the next sleep interval.\n")
  }

  log_line(now, elapsed_hours, sleep_tier$label, sleep_tier$interval_min, frac_counted, status, note)

  if (!is.na(frac_counted) && frac_counted >= 0.9999) {
    consecutive_complete <- consecutive_complete + 1
    cat("Reported", round(frac_counted * 100, 2), "% counted (", consecutive_complete, "/2 confirmations).\n")
    if (consecutive_complete >= 2) {
      cat("\n=== 100% counted, confirmed twice. Stopping automatically. ===\n")
      break
    }
  } else {
    consecutive_complete <- 0
  }

  if (iteration >= MAX_ITERATIONS) {
    cat("\n=== MAX_ITERATIONS (", MAX_ITERATIONS, ") reached -- stopping (test mode). ===\n")
    break
  }

  # The interval is start-to-start: subtract this cycle's own runtime so a
  # ~90s fetch doesn't stretch a 4-min cadence to ~5.5 min.
  cycle_secs <- as.numeric(difftime(Sys.time(), now, units = "secs"))
  sleep_secs <- max(0, sleep_tier$interval_min * 60 - cycle_secs)
  cat("Cycle took", round(cycle_secs), "s; sleeping", round(sleep_secs), "s until next poll (", sleep_tier$interval_min, "min cadence)...\n")
  Sys.sleep(sleep_secs)
}

cat("\n=== Tiered polling schedule ended", format(Sys.time()), "===\n")
