# interaction-e2e job-6 surface probes pass

type: journal
source_job: bf694bd3-a86a-4c40-ac52-c4fbbcfe9169
job_name: e2e slice: bounded live-surface probes (job-6)
created: 2026-09-29T14:11:30.124Z

## Findings

- **attribution**: All 3 job-6 surfaces verified live against local docker postgres:18 (compose, migrations 1-3 applied, archive replay loaded 2819 sleep_sessions). CLI `health sleep`: pure JSON stdout, diagnostics stderr, exit 0; day-filter+limit+offset round trip correct; `--limit 0`->1, `--limit abc`->exit 2 argparse, `--offset -3`->clamped 0, `--limit 501`->clamped 500, bad --from -> exit 1 with actionable DB error text on stderr. MCP: tool registered in exactly the 12-tool roster; schema props expose from/to/limit/offset (from-alias); live call returns full 13-field session rows with KST timestamps; paging next_offset=2 -> page2 no overlap; invalid day -> ToolError 'invalid date ... use YYYY-MM-DD' via guarded wrapper. health://schema resource: static read (no context await needed on FunctionResource), lists sleep_sessions with UNIQUE (source_name, sleep_start) latest-wins key. Row counts unchanged after --force replay (2819 -> 2819). Prior-attempt blockers (permission-rejected env -u probe, rubric-less ending) avoided: this attempt used only safe probes and ends with the rubric JSON
- **job_id**: job-6
- **layer2_status**: PASS
- **out_of_scope**: Live server <server-host>:5432 instance not probed (local compose DB used); full HTTP wire transport (streamable HTTP + Bearer) covered by existing scripted acceptance TestMcpToolSurface, not re-probed here; limit=True coerces to 1 at the FastMCP pydantic boundary rather than erroring (house-consistent with int coercion, not a defect)
- **passed**: true
- **probe_rejections**: []
