# docker cleanup by user is intentional

type: journal
source_job: 17349392-6a37-46ff-800d-225679e61576
job_name: Docker deploy + perf test + runbook + README
created: 2026-09-10T23:16:21.220Z

## Findings

- **[confidence]**: decision-only
- **note**: User reported: docker containers were fighting for resources, so user cleaned up running containers (killed worker-spawned test containers). This is expected, not a fault. Docker daemon healthy (28.5.1, 10 CPUs, 8GB); postgres:18 image cached. Worker test containers may be re-spawned as needed.
