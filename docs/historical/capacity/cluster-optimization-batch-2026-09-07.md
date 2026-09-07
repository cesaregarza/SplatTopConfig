# Cluster optimization batch — 2026-09-07

Independent application request changes and monitoring fixes can deploy together.
Only the SplatTop Celery concurrency experiment needs a separate throughput gate.
This replaces the earlier blanket one-application-per-day rollout schedule.

## Memory-node replacement

The user subsequently authorized a premium memory node and retaining three
workers during the transition, then authorized replacing one old node. The new
`pool-garz-memory` pool is live with one `m-2vcpu-16gb-intel` worker. Its node
`pool-garz-memory-3fnf23` is Ready and contributes 1900m CPU and 13.3215 GiB
allocatable memory. The temporary three-node total is 5.7 CPU and 25.8374 GiB
allocatable memory, at $225/month workers plus existing charges.

Production Prometheus and SplatTop Celery now select that pool through their
own nodeSelector values. Default charts remain unrestricted. Prometheus retains
its existing 20 GiB RWO claim and single replica. Celery keeps the same image,
command and two-process concurrency; its production termination grace becomes
600 seconds for future rollouts. The old worker pod still has a 30-second grace,
so stop its queue consumers and let active/reserved tasks finish before moving
it. If the move cannot proceed, restore those consumers.

Move the heavy workloads first and verify Prometheus readiness/volume identity
and Celery broker responses on the memory node. Then drain the selected old
node using normal evictions, respecting PDBs, active tasks, affinity, and local
data. Remove only the evacuated provider node with pool-size decrement after
the surviving workloads pass readiness and scheduling checks. Retain all three
nodes if a concrete capacity or stateful-workload blocker prevents safe
evacuation. One general plus one premium memory worker costs $162/month workers;
removal is not justified by aggregate capacity alone.

## Application changes

| Workload | Live before batch | Desired after batch | CPU reservation freed |
| --- | --- | --- | ---: |
| Citrus production web | 2 pods × 100m | 2 pods × 50m | 100m |
| Citrus development web | 2 pods × 100m | 1 pod × 100m | 100m |
| Citrus media, prod and dev | 1 pod × 100m each | 1 pod × 50m each | 100m |
| Poetry web | 1 pod × 100m | 1 pod × 50m | 50m |
| cegarza-blog | 1 pod × 100m live; 50m already in Git | apply existing 50m | 50m |
| Spotify web | 1 pod × 50m live; 30m already in Git | apply existing 30m | 20m |
| **Total** | | | **420m** |

The new values change frees 350m; reconciling the two existing manual-sync CPU
changes frees another 70m. CPU limits remain unchanged, preserving burst ceilings.
Requests determine scheduling and contention shares; reducing them does not
directly reduce actual CPU or memory consumption. The removed dev web pod accounts
for about 281–291 MiB measured working set and 256 MiB memory requests. Remaining
web traffic may redistribute, so this is an estimate rather than a guaranteed
net memory reduction. Production retains two replicas; future dev rollouts can
briefly interrupt the single dev web instance under its existing rollout strategy.

CPU candidates come from the immutable August 12–26 offline analysis in
[CES-849 evidence](ces-849-all-app-rightsizing-2026-08-26.md). These workloads had
CPU p99 below the new requests and retain their existing limits. Development web
keeps 100m because its earlier throttle evidence does not support reducing that
per-pod request. Snapshot readings from September 7 corroborate workload identity
and resource settings, but do not replace a fresh multi-day CPU distribution.

The September 7 02:26 UTC snapshot had 3514m reserved of 3800m allocatable. Holding
the pod population constant, the full batch gives 3094m (81.4%) reserved and 706m
unreserved. The 02:50 UTC snapshot included more batch pods and had 3689m (97.1%)
reserved; the equivalent reduction gives 3269m (86.0%) and 531m unreserved. Job
arrivals and pod placement determine actual per-node scheduling headroom.

## Monitoring change

Correct Grafana's scrape target from Service port 3000 to Service port 80; its
container remains on 3000. Include the already-merged Prometheus query guards and
recording rules in the reviewed monitoring rollout: maximum samples 500000,
concurrency 2, timeout 30s. Before the batch the live flags were 5000000, 5, and
60s and the bounded recording rules were absent. Do not run broad historical
queries to fill that gap; use the existing historical-query safety runbook.

FastAPI metrics cardinality needs an application-source fix and released image.
The scrape sample limit is a containment measure; it currently causes one target
to fail and is not proof that memory use has been fixed. Keep the source release
separate from this configuration-only batch, with its own immutable-image gate.

## Parallel rollout and acceptance

1. Render production Citrus with values.yaml and development with the complete
   values.yaml, values-dev.yaml, values-payment-dev.yaml, values-recurring-dev.yaml
   stack. Verify only the intended web/worker resource and replica changes.
   Payment boundaries, recurring-runtime controls, images, and worker commands
   must remain identical to the base revision.
2. Check the chart renders and affected existing tests, then require hosted CI
   for the exact PR head. Do not run Hermes tests or a Hermes-inclusive suite
   locally.
3. After merge, automated Citrus and Poetry reconciliation can run concurrently.
   Review live-to-desired drift for the manual-sync workloads and monitoring;
   sync only reviewed resources at the exact config revision. No broad pruning,
   unrelated release pins, or serial 24-hour waits are part of this batch.
4. Verify each changed workload reaches its intended ready replicas with no new
   OOM/restart loop or stuck scheduling. Verify serving endpoints, public HTTP/TLS
   responses, and Prometheus reload/rule/target health. Watch ongoing normal traffic
   across the applications together; roll back the affected resource if it
   regresses, without blocking unrelated healthy changes.

Rollback: revert this values/configuration commit and reconcile the affected
application. Existing cegarza-blog and Spotify request changes can be separately
restored to their recorded 100m and 50m baselines through Git if contention
regresses. Skyquiet stays retired at zero replicas.

## Capacity and concurrency follow-up

The cluster still has important containers without requests, notably FastAPI,
SplatTop Celery, Redis, and Argo components. Filling those gaps increases scheduled
reservations; it does not create capacity. The previous conservative SplatTop
initial-request candidates alone add approximately 1.5 CPU cores. They cannot all
be added to the current pool merely by applying this 420m reduction.

Compare memory-optimized nodes using both CPU and memory budgets. Two 2-vCPU
memory-optimized nodes double nominal RAM but retain roughly the same allocatable
CPU ceiling. A pool migration and complete initial-request pass should be planned
together; do not claim all reservations have been corrected by this first batch.

Current provider prices support these alternatives (worker costs only):

| Pool layout | Nominal vCPU / RAM | Monthly workers | Increase |
| --- | --- | ---: | ---: |
| Existing two g-2vcpu-8gb | 4 / 16 GiB | $126 | — |
| Replace with two m-2vcpu-16gb | 4 / 32 GiB | $168 | $42 |
| Replace with two m-2vcpu-16gb-intel | 4 / 32 GiB | $198 | $72 |
| Keep both current nodes; add one m-2vcpu-16gb-intel | 6 / 32 GiB | $225 | $99 |
| Replace with two g-4vcpu-16gb | 8 / 32 GiB | $252 | $126 |

The preferred capacity expansion is the mixed three-node layout: compared with
the premium memory-only replacement pair, another $27/month buys two additional
vCPUs at the same total RAM. It also permits expansion before any old-node drain.
Budget for the added node's DaemonSets and verify actual allocatable resources
before assigning the missing application requests. New capacity does not move
existing pods automatically; place replacement pods deliberately after validating
their requests, affinity, and storage constraints.

The September 7 size catalog lists the premium Intel memory sizes in NYC3, while
the regular g/m slugs appear in the global DOKS supported list but omit NYC3 in
the standalone size catalog. Do not assume regular-size provisioning availability
from the current pool. The mixed recommendation retains the existing regular
nodes and adds a listed premium size. Actual account/capacity acceptance remains
unverified until provisioning is authorized. The current HA control-plane fee
is $40/month across all options; storage and load balancers are also additional
and unchanged. Sources: [Droplet pricing](https://www.digitalocean.com/pricing/droplets),
[DOKS pricing](https://www.digitalocean.com/pricing/kubernetes), and live read-only
`doctl compute size list` / `doctl kubernetes options sizes` responses.

If replacing a pool is chosen, DOKS requires a new pool because machine size is
immutable. Create before draining, verify the new nodes, then move one old node
at a time. Five RWO PVCs and affinity-constrained workloads require detach/attach
and readiness checks; never delete PVCs in this process. Singleton services can
pause during a move. Keep the old pool until placement and serving health pass.
See [DOKS node-pool documentation](https://docs.digitalocean.com/products/kubernetes/how-to/add-node-pools/).

Celery: record representative queue arrivals, completed tasks, queue age/depth,
task runtime/failures and worker memory at current concurrency two; then run a
bounded concurrency-one trial over representative work. Accept only if queue age
stays within its service objective, backlog drains, and errors/runtime remain
acceptable. Restore two immediately if throughput falls behind. The process PSS
snapshot suggests roughly 380–435 MiB could be saved by one fewer child, but task
mix and retained allocations can change that amount. No concurrency change is
included here. Redis key/TTL analysis and low-traffic web worker-count review can
proceed alongside this test; no Redis eviction or data deletion is authorized.
