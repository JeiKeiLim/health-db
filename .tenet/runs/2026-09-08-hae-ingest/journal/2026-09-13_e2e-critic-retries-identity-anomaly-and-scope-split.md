# e2e critic retries identity anomaly and scope split

type: journal
source_job: 9bb0cc11-33ae-4314-bb71-15b6e49156c5
job_name: Integration: slice 2 full acceptance + perf
created: 2026-09-13T08:36:00.656Z

## Findings

- **confidence**: implemented-not-tested
- **evidence**: both retry outputs open with interaction-e2e role statements despite params.eval_stage=security_critic/data_integrity_critic
- **impact**: critics burned full context re-exploring and produced no rubric JSON
- **note_1**: interaction_e2e critic retry PASSED with valid rubric after PYTHONPATH=src fix (517/517 + 90/90 confirmed independently)
- **note_2**: root-cause hypothesis: tenet_retry_job(enhanced_prompt) on critic_eval jobs replaces the role-bearing prompt; dispatch then rebuilt worker context without the critic identity
- **note_3**: orchestrator response: per phases/05 §context-limit rule, splitting each into 2 reduced-scope ad-hoc critic_eval jobs with self-contained role+scope+rubric prompts instead of retrying identical a 3rd time
- **note_4**: venv env fact confirmed: UF_HIDDEN xattr on .pth files under the synced path makes site.py skip them; PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B is the only reliable invocation
