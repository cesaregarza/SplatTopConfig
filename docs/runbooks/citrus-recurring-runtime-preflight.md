# Citrus recurring runtime preflight and rollback render

## Current status

The base chart keeps the CES-850 runtime disabled by default. The development Argo
Application selects `values-recurring-dev.yaml` to stage the complete dormant
runtime at topology revision `ces-850-dev-v1`. Production does not select this
overlay. Merging that activation change allows the automated development sync
to create the runtime, so it requires an explicit activation review.

Enrollment, cohort, reminders and charging remain off, the emergency stop stays
on, and the development Cilium boundary continues denying provider egress.
The source image and its expected revision come from the existing atomic
release bindings in `values-dev.yaml`; the activation overlay adds no image pin.
Never contact Stripe to validate this contract.

The pre-activation read-only check on source
`8b0f675d46c8dc66e30fc9a90ae401605bb4ff0f` passed at
`2026-09-07T00:33:02Z` with the proposed topology, scheduler and expected-source
environment projected only into the inspection process. It found zero pending
migrations, zero recurring rows or violations and billing queue depth zero.
This proves the source preflight, not controller rollout or live idle behavior.

The worker requests 75m CPU and 256Mi memory including its metrics sidecar.
Each preflight/tick/health Job requests 50m CPU and 128Mi. Recheck scheduling
headroom immediately before activation; pending unrelated workloads must not be
displaced or resized as part of this change. After the complete Application
sync, retain the hook result, worker readiness, idle tick/health results and
metrics evidence before closing CES-850. CES-715 still owns customer rollout.

The metrics sidecar runs Django and needs headroom beyond its resident process:
it requests 128Mi memory with a 256Mi limit. Collect its metrics through
Prometheus or from the billing-worker container. Run additional Django
management commands in the worker or bounded Jobs so diagnostic processes do
not compete with the metrics server inside the sidecar's memory limit.

## Enabled-safe render contract

Requesting any one recurring component requires the complete topology:

- the billing worker and its metrics sidecar;
- the read-only recurring preflight Job;
- the recurring tick CronJob; and
- the recurring health CronJob.

The chart renders that topology only when the same release also enables the
CES-845 payment-safety Cilium boundary. Development is bound to release
`citrus-dev`, namespace `citrus-dev`, owner `citrus-dev`, and deny mode.
Production is bound to release `citrus`, namespace `default`, owner `citrus`,
and explicit allow mode. An unknown or mixed tuple fails Helm rendering.

Each component declares the same nonsecret
`RECURRING_RUNTIME_TOPOLOGY_REVISION` and scheduler value
`kubernetes-cronjob`. The revision is also an annotation on every workload and
Pod template. Empty, malformed, or inconsistent revisions fail rendering.

The image identity is a separate immutable contract. `image.tag` and
`recurringRuntime.expectedSourceRevision` must both be exact 40-character
lowercase commit SHAs and must match byte-for-byte before any recurring
component renders. GitOps injects the expected revision only into the five
runtime containers. The source preflight then compares it with the independent
revision file baked into the image by the trusted Citrus build; missing,
malformed, mutable (`latest`), or mismatched identities fail closed.

The chart also fails closed unless migrations, Redis, the worker, metrics,
schedules, health check, and preflight are present. The billing queue must be
`billing`, eager Celery execution must remain off, enrollment and cohort modes
must remain off, their allowlists must remain empty, reminders and charging
must remain disabled, and the charge emergency stop must remain enabled.

## Ordered preflight

The provider-free source command is pinned to:

```text
python manage.py preflight_recurring_runtime --include-broker --format=json
```

The Job is an Argo `Sync` hook with no Kubernetes ServiceAccount token,
`backoffLimit: 0`, and a bounded deadline. Its sync wave is strictly after the
migration hook and strictly before the billing Deployment and both recurring
CronJobs:

```text
migrations (1) -> recurring preflight (2) -> billing worker (3) -> tick/health (4)
```

The Job receives only configuration references and value-free runtime
attestation. A later source release must prove that the command performs local
database and broker reads without application writes, network discovery, HTTP,
or provider mutation. Chart rendering alone cannot make that source claim.

Never use Argo selective-resource sync for this topology: selective sync skips
hooks and can bypass the preflight Job. Under separately authorized rollout,
sync the complete `citrus` or `citrus-dev` Application only so migrations,
preflight, worker, tick, and health retain their declared ordering.

## Provider-free review

Use synthetic values and local rendering only. The CI workflow renders named
development and production safe topologies, runs Helm lint and strict
kubeconform, and executes the Python negative matrix. Hostnames in Helm values
are never resolved by these checks. Rendered files contain Secret references,
never Secret objects or decrypted values.

Run the same repository-owned render contract locally with an empty disposable
output directory:

```bash
citrus_render_dir="$(mktemp -d)"
scripts/check_citrus_recurring_runtime_render.py \
  --chart helm/citrus \
  --output-dir "$citrus_render_dir" \
  --format json
```

Reviewers must verify all of the following on the exact signed commit:

1. production and base dev renders omit all four recurring workloads;
2. enabled-safe dev and production renders contain the complete topology;
3. all workload and container topology revisions match;
4. the five runtime containers receive the same expected source revision as
   their immutable image tag;
5. the preflight batch label is selected by the CES-845 policy;
6. every unsafe matrix case fails Helm rendering; and
7. the actual Argo dev overlay adds only the four recurring resources, retains
   every customer/payment gate, and binds all five containers to the same image;
8. strict kubeconform accepts every generated manifest while skipping only the
   Cilium CRD whose schema is not in the default Kubernetes catalog.

The first activation is deliberately single-consumer: `billingWorker.replicas`
must be exactly `1`. The chart also pins the tick command to
`python manage.py tick_recurring_orders --scan-limit=100 --dispatch-limit=100`;
arbitrary command overrides fail rendering before a controller can be created.
The tick and health schedules are likewise pinned to `*/5 * * * *` and
`2-59/5 * * * *`; alternate schedules fail rendering. Every runtime Pod disables
automatic Kubernetes ServiceAccount-token mounting.

## Rollback render

Rollback reverts the activation as one reviewed change: remove
`values-recurring-dev.yaml` from the development Application's `valueFiles` and
from dev `appliedValues` in `helm/citrus/release-bindings.json`; restore the
matching path contract in `scripts/update_citrus_release.py` and the prior CI
render matrix and activation tests together. The three runtime enable flags
then return to their base defaults. Keep the current operational image bindings
and other overlays in place, including subsequent release updates. Review and
sync the complete Application so pruning removes the stateless runtime.
Do not sync that change without explicit operator authorization.

The automated render comparison requires the enabled-to-disabled delta to
remove exactly the stateless billing Deployment, preflight Job hook, tick
CronJob, and health CronJob. Every common resource must remain byte-for-byte
equivalent after YAML parsing. In particular, the ConfigMap gates, immutable
image tuple, migration Job, Redis Deployment and Service, Secret/PVC
references, Cilium boundary, and schema/state ownership remain unchanged.

Disabling controllers does not reverse already committed database state and is
not evidence that a live rollback succeeded. Keep customer/payment behavior
flags off and the emergency stop on, preserve the payment-safety boundary, and
record the source image, GitOps revision, topology revision, Argo operation,
controller disappearance, and post-rollback health under separately authorized
live acceptance.
