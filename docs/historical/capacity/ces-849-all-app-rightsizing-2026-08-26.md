# CES-849 all-app right-sizing evidence — 2026-08-26

The measurements below remain historical evidence. The user-approved September 7
[parallel optimization batch](cluster-optimization-batch-2026-09-07.md) supersedes
this document's one-application-at-a-time and 24-hour inter-application rollout
schedule. Independent CPU request changes can proceed together.

## Decision

Start with four one-replica, manual-sync applications and reduce only their CPU
requests:

| Argo application | Workload/container | Current | Proposed | 14-day CPU p99 | 14-day CPU max | Desired reduction |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `cegarza-blog` | `cegarza-blog/blog` | 100m | 50m | 14.1m | 111.3m | 50m |
| `splattop-blog-prod` | `splattop-blog/blog` | 100m | 50m | 2.9m | 15.2m | 50m |
| `skyquiet-server` | `skyquiet-server-api/api` | 50m | 30m | 0.2m | 0.3m | 20m |
| `spotify-hot-100` | `spotify-hot-100/web` | 50m | 30m | 0.2m | 12.0m | 20m |
| **Total** | four desired steady pods | **300m** | **160m** |  |  | **140m** |

The new requests remain above twice the observed p99 with a 30m or 50m
operational floor. CPU limits, memory requests, memory limits, replicas, and
companion workers or CronJobs do not change. Merging this change does not deploy
it: all four Argo CD Applications require an explicit manual sync.

This is a first reversible slice, not a claim that CES-849 is complete or that
the cluster has reached its current 800m requested-headroom planning target. At
the same pod population, a 140m request reduction adds exactly 140m of scheduler
headroom; it does not change allocatable CPU or guarantee application latency.

## Evidence and safety boundary

- Window: `2026-08-12T01:40:00Z` through `2026-08-26T01:40:00Z`, inclusive,
  on a five-minute grid.
- Source: an immutable copy of the Prometheus TSDB archive with SHA-256
  `bea90c70a49f24a91174628b5058e9f5107f56ab35beddb2ccaf5b087b774404`.
- The analysis ran only against a loopback-bound offline Prometheus v2.52.0
  process with no scrape, rule, alerting, remote-read, or remote-write targets.
- Queries were sequential, join-free, limited to one concurrent query, capped
  at 500,000 samples, limited to 30 seconds, and subdividable by time range.
- The complete workload collection required no subdivision. Its largest
  accepted query peak was 7,389 samples, 1.48% of the server cap.
- Pod ownership was resolved offline from separately collected owner metadata:
  18,367 pod-owner series produced no ambiguous top-level pod, ReplicaSet, or
  Job mappings.
- The normalized result contains 77 workload/container rows, including 62 in
  the user-app, user-batch, agent-platform, or cluster-platform scopes.
- CPU and memory values below are per-container, max-across-active-pods
  envelopes at each timestamp. Batch percentiles are conditional on observed
  executions; missing time is not filled with zero.

No broad or joined query was sent to live Prometheus. Zero returned OOM-reason
series is treated only as absence of observed series, not proof that no OOM
occurred. The separate CES-856 incident evidence records Prometheus exit 137 and
one restart at `2026-08-26T01:40:44Z`.

## Steady application review

Memory p99 is shown beside the current memory request because it materially
constrains the decision. A dash means the workload has no configured request.
“Later” means the measurements support a CPU candidate, but the Application is
automated or shares values with another environment and therefore is excluded
from this manual-sync slice.

| Namespace | Workload/container | CPU request | CPU p99 / max | Memory request | Memory p99 / max | p99 throttle | Decision |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| `cegarza-blog` | `cegarza-blog/blog` | 100m | 14.1m / 111.3m | 256Mi | 272.5Mi / 272.6Mi | 0.5% | first slice: 50m CPU |
| `citrus-dev` | `citrus-dev/django` | 100m | 5.0m / 62.3m | 256Mi | 284.3Mi / 393.6Mi | 10.7% | hold: throttling and memory |
| `citrus-dev` | `citrus-dev-media-worker/media-worker` | 100m | 1.3m / 20.5m | 256Mi | 153.2Mi / 169.0Mi | 0.0% | later: 50m CPU candidate |
| `citrus-dev` | `citrus-redis/redis` | 50m | 5.9m / 6.0m | 64Mi | 14.4Mi / 16.6Mi | 3.9% | hold: throttling |
| `default` | `citrus/django` | 100m | 1.5m / 27.2m | 256Mi | 302.2Mi / 304.4Mi | 0.2% | later: 50m CPU candidate; memory hold |
| `default` | `citrus-media-worker/media-worker` | 100m | 1.3m / 19.7m | 256Mi | 224.9Mi / 226.0Mi | 0.0% | later: 50m CPU candidate |
| `default` | `citrus-redis/redis` | 50m | 5.9m / 6.0m | 64Mi | 10.5Mi / 16.0Mi | 3.8% | hold: throttling |
| `default` | `skyquiet-server-api/api` | 50m | 0.2m / 0.3m | 128Mi | 45.1Mi / 45.4Mi | 0.0% | first slice: 30m CPU |
| `default` | `skyquiet-server-worker/worker` | 50m | 0.2m / 1.3m | 128Mi | 56.1Mi / 58.0Mi | 0.0% | hold: restart observed |
| `default` | `splattop-blog/blog` | 100m | 2.9m / 15.2m | 256Mi | 259.6Mi / 259.9Mi | 0.0% | first slice: 50m CPU |
| `default` | `splattop-prod-celery-beat/celery-beat` | 10m | 0.1m / 3.2m | 32Mi | 46.5Mi / 47.2Mi | 63.6% | hold: request floor, throttling, memory |
| `default` | `splattop-prod-celery-worker/celery-worker` | — | 257.9m / 313.2m | — | 1,527.4Mi / 1,667.6Mi | — | initial-request review: 520m CPU candidate |
| `default` | `splattop-prod-fastapi/fastapi` | — | 210.0m / 330.0m | — | 387.1Mi / 559.3Mi | — | initial-request review: 430m CPU candidate per pod |
| `default` | `splattop-prod-react/react` | — | 0.1m / 0.3m | — | 9.3Mi / 9.5Mi | — | initial-request review: 30m CPU candidate per pod |
| `default` | `splattop-prod-redis/redis` | — | 10.7m / 20.5m | — | 495.3Mi / 514.4Mi | — | initial-request review: 30m CPU candidate |
| `default` | `spotify-hot-100/web` | 50m | 0.2m / 12.0m | 128Mi | 15.9Mi / 16.1Mi | 0.0% | first slice: 30m CPU |
| `garz-ai` | `garz-ai/web` | 25m | 1.9m / 5.1m | 64Mi | 41.2Mi / 41.3Mi | 0.0% | hold: already below 30m floor |
| `poetry` | `poetry/web` | 100m | 0.7m / 20.7m | 256Mi | 380.0Mi / 380.3Mi | 0.0% | later: 50m CPU candidate; memory hold |
| `splattop-bot-agent-8s` | `agent-8s/bot` | 100m | 6.0m / 6.6m | 128Mi | 60.7Mi / 61.5Mi | 0.2% | later: 50m CPU candidate |
| `vanity-hosts` | `vanity-hosts-vanity-hosts/vanity-hosts` | — | 0.0m / 0.0m | — | 2.9Mi / 2.9Mi | — | initial-request review: 30m CPU candidate |

The four missing-request SplatTop services plus Vanity Hosts would add about
1.5 CPU cores at their provisional per-pod candidates and current desired
replicas. That is a scheduling-capacity increase, not reclaimed headroom, so it
must be evaluated as its own capacity-aware slice. The observed SplatTop Celery
worker memory envelope also requires a dedicated memory and limit decision.

The SkyQuiet worker and Spotify refresh CronJob are also deliberately unchanged.
The worker had a restart increase during the window. The refresh job had only
two observed executions, and its observed CPU envelope reached about 111m,
already above its 50m request; sparse batch evidence does not support lowering
that request.

Agent and cluster-platform workloads were profiled but are intentionally not
changed here. Several agent-control-plane sidecars are already at a 25m request
and showed high p99 throttling, while Prometheus itself has separate CES-856
memory-risk controls. Provider-managed DaemonSets and DigitalOcean components
are not owned by this GitOps repository.

## Candidate rule

The offline candidate generator permits a staged CPU reduction only when all of
the following are true:

1. The container is a steady user Deployment active at the window end.
2. At least seven days of CPU history and at least 99% CPU-to-memory active-slot
   coverage are present.
3. No restart increase is observed and p99 CPU throttling is at most 1%.
4. The candidate is at least twice CPU p99, rounded up to 10m with a 25m floor.
5. One slice removes no more than 50% of the current request.

This rule produces review inputs, not deployment authorization. It does not
derive memory reductions from working-set data, and it does not lower CPU or
memory limits.

## Manual rollout and rollback gates

After review and merge, sync one Application at a time only with explicit
authorization. For each Application:

1. Record the pre-sync revision, pod request/limit render, node requested
   headroom, pod restarts, Ready state, application latency/error indicators,
   and CPU/memory/throttling baselines.
2. Sync only the named Application; do not bulk-sync the four Applications.
3. Confirm the Deployment becomes Healthy/Ready without a restart loop,
   scheduling delay, OOM, or application regression.
4. Observe at least 24 hours and one representative traffic or release cycle
   before advancing to the next Application. Retain a longer observation window
   when the application's meaningful cycle is longer.
5. Revert the exact values commit and manually sync that same Application if
   health, latency, error rate, scheduling, restart, memory, or CPU-contention
   signals regress. The unchanged 500m CPU and 512Mi memory limits are not a
   reason to skip rollback.

Do not merge this evidence with a capacity purchase, node removal, broad live
historical query, or automated Argo rollout. Those actions require separate
authorization and evidence.
