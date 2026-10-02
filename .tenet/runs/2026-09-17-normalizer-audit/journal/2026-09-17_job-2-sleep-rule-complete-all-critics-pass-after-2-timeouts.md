# job-2 sleep rule complete all critics pass after 2 timeouts

type: journal
source_job: bad96b04-b031-425c-bfe9-6c71b2c9caac
job_name: Sleep value rule + in-bed split + loosened recognition
created: 2026-09-17T12:02:31.427Z

## Findings

- **commits**: ["4f67aa4","67f9722"]
- **job**: job-2
- **lesson**: The core sleep bug fix: totalSleep→asleep→c+d+r→sleep_in_bed→sleep_no_value precedence implemented in normalize.py; loosened {date,asleep}/{date,sleepStart} recognition in payloads.py. Attempts 1-2 hit the 2h wall-clock cap while running broad acceptance suites (worker died mid-run, but incremental commits landed the work). Attempt 3 = verification-only with narrow pytest selections passed in 171s: 8/8 sleep acceptance classes, 169/169 unit. LESSON FOR LOCAL TIER: instruct workers to commit incrementally AND scope their test runs to their DAG class only - broad suite runs under concurrent pg load time out.
- **retries**: 3
- **run**: 2026-09-17-normalizer-audit
- **status**: complete
