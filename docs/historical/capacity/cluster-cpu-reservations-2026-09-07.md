# CPU reservation correction, September 7, 2026

The memory node had 1,772m of 1,900m CPU reserved after the node replacement, while SplatTop FastAPI and Celery reserved zero CPU. This change reduces oversized monitoring requests and adds baseline reservations for the applications. FastAPI prefers the general-purpose pool with fallback to other eligible nodes. Alertmanager and external-dns require that pool to reserve room on the memory node for Celery's rolling updates.

| Workload | Previous request | New request | Historical CPU p99 |
| --- | ---: | ---: | ---: |
| Prometheus | 250m | 150m | 82m |
| Grafana | 100m | 25m | 2m |
| Alertmanager | 50m | 25m | 1m |
| metrics-server | 100m | 25m | 3m |
| external-dns | 50m | 25m | 2m |
| FastAPI, each of two pods | 0 | 150m | 210m |
| SplatTop Celery worker | 0 | 150m | 258m |
| React, each of two pods | 0 | 5m | <1m |
| vanity-hosts | 0 | 5m | <1m |

Requests reserve a scheduling baseline and affect CPU shares under contention. Existing CPU limits stay unchanged; none are added to FastAPI or Celery. These applications can use idle CPU above their requests. FastAPI's historical median was 138m and p95 189m; Celery's median was 112m and p95 243m. Their 150m requests cover typical baseline use, not sustained p95 demand under full contention. See [Kubernetes resource management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/).

The historical evidence is the offline August 12–26 analysis documented in [CES-849](ces-849-all-app-rightsizing-2026-08-26.md), archive SHA256 `bea90c70a49f24a91174628b5058e9f5107f56ab35beddb2ccaf5b087b774404`. Its CPU series are the maximum across active pods at each timestamp; they are not independent per-pod percentile distributions. Each workload above has fourteen days of CPU coverage. A separate guarded live recording from September 7, 04:10–04:30 UTC contained five points between 728m and 857m cluster CPU use. That short window is a current cross-check, not a peak-load guarantee. No broad live historical Prometheus queries were used.

Steady-state requests change from 3,094m to 3,259m of 3,800m allocatable: reclaim 300m, add 465m of previously missing reservations. The intended placement is approximately 1,727m on the memory node and 1,532m on the general node. Jobs and rolling surge can change those per-node totals. Neither node capacity nor spending changes.

During the initial rollout, the scheduler kept Alertmanager and external-dns on the memory node despite weight-100 soft preferences. Requiring the general pool for these two 25m services moves 50m of reservations away from the worker. The general pool has one node: losing it temporarily makes these services unschedulable until that pool returns. This explicit placement tradeoff keeps a 150m Celery surge pod schedulable in steady state. Reserve the pinned Celery worker first, then roll the flexible FastAPI/React deployments so they cannot take its CPU slot during the transition.

## Deployment and verification

Base/rollback configuration: `9350a6c5c102cee361defb18f59e1499d71d09ae`. Reconcile the reductions first, with independent deployments together, then the SplatTop additions. Argo applications `garz-observability` and `splattop-prod` require explicit sync of the exact reviewed revision and selected workloads; use dry-run first and no prune or hooks. Automated metrics-server, external-dns, and vanity-hosts reconcile normally.

Before rolling the worker, cancel consumption only for its exact current hostname and wait for active, reserved, and scheduled counts to reach zero. Resume the old consumer if the rollout aborts. Verify the replacement consumes `celery`, responds to ping, and still uses concurrency two. Preserve the Prometheus/Grafana PVC identities and verify Alertmanager has no silences to lose before its emptyDir-backed restart.

Validation uses production Argo release names and overlay stacks, Helm 3.14.0 lint/render, kubeconform 0.6.7, focused contract tests, and an exact allowlist of rendered changes via `scripts/verify_manifest_delta.py`. The verifier fails on unexpected resource identity or field changes and does not print manifest values. Hosted CI runs the full contract suite; local Hermes tests and broad checks are prohibited by the operator's resource boundary.

Live acceptance: selected rollouts ready, no stranded Pending pods, both nodes Ready without pressure, Prometheus healthy with query protections retained, expected requests and placement visible, and public application routes healthy. Compare metrics after startup settles. Rollback must remove the new application requests before restoring larger monitoring requests, to avoid transient scheduling exhaustion.

## Remaining ownership and state constraints

SplatTop Redis still lacks a CPU request and has ephemeral storage. A template change would restart it and lose state; first establish persistence and a safe data migration. Argo CD and cert-manager controllers also lack CPU requests, but their installation owner is outside this repository. DOKS-managed agents and DNS account for about 642m per node and need a provider-supported tuning route; see [DigitalOcean's managed components guidance](https://docs.digitalocean.com/products/kubernetes/details/managed/). These are explicit remaining gaps, not covered by this batch.
