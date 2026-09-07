# DigitalOcean Postgres Restore Runbook

This runbook restores the Mandate control database used by the live
`agent-control-plane` deployment. It is a deployment-owned operational procedure,
not Mandate authority: it does not grant rights to agents, workers, prompts,
model output, task payloads, or workload parameters.

The generic recovery contract lives in
`agent-platform/docs/postgres-restore-contract.md`. This file is the concrete
DigitalOcean and GitOps instance for GarzAICluster.

Cost-bearing restore drills and live infrastructure mutations require explicit
operator approval. Codex may update this source-controlled runbook, but must not
create, fork, delete, or repoint managed database clusters unattended.

## Current Posture

Observed on 2026-07-23 with read-only `doctl` commands against the `cegarza`
DigitalOcean context:

| Field | Value |
| --- | --- |
| Managed database name | `db-postgresql-nyc3-xscraper` |
| Managed database ID | `3adbbc39-dd07-4c0c-b172-081a247810d6` |
| Engine/version | PostgreSQL 16 |
| Region | `nyc3` |
| Nodes | 1 |
| Plan | `db-amd-1vcpu-2gb` |
| Status | online |
| Latest observed daily backup | `2026-07-23T02:29:54Z` |
| Observed backup range | `2026-07-16` through `2026-07-23` |
| Live namespace | `agent-control-plane` |
| Live Argo apps | `agent-control-plane`, `agent-control-plane-secrets` |
| Runtime Kubernetes Secret | `agent-control-plane-secrets` |
| Runtime secret source | `secrets/agent-control-plane/runtime-secret.enc.yaml` |
| Values overlay | `apps/agent-control-plane/values.yaml` |
| Chart source | `argocd/applications/agent-control-plane.yaml` |
| Public hostname | `agent-control-plane.garz.ai` |
| Current image | `registry.digitalocean.com/sendouq/agent-platform:sha-fa3afd59e3af` |

DigitalOcean managed PostgreSQL documents automatic backups and point-in-time
restore by forking a new database cluster. Restores must create a new cluster;
do not restore in place over the current writer.

## Accepted Recovery Targets

Current RPO: restore to the latest available DigitalOcean PITR point inside the
seven-day native recovery window. If the incident is discovered after that
window, the native managed-database restore path is insufficient and operators
must treat it as data loss unless a separate logical export or longer-retention
snapshot exists.

Current RTO: restore the private deployment within 4 hours from incident
declaration to a healthy `/readyz` on the restored cluster. This includes the
time to fork the managed database, update encrypted secrets, let Argo sync, run
the schema check, and verify service health.

The current API deployment is deliberately single-replica (`replicaCount: 1`),
and the chart has no API PodDisruptionBudget. The model gateway has its own PDB,
but the API, local worker, callback adapter, and git deliverer should be treated
as single-replica services in this deployment. A database restore is therefore a
service-interruption event, not a zero-downtime operation. This is acceptable for
the current private deployment and must be revisited before a production pilot.

The callback retry posture fits inside the restore window: the callback adapter
defaults to 10 attempts, exponential backoff capped at 300 seconds, and a
60-second delivery lease. Seven-day PITR covers the retry window. Audit
investigations that need recovery to a pre-incident database state must happen
inside the same seven-day native recovery window unless longer-retention backup
tooling is added outside this slice.

## Restore Procedure

### 1. Freeze Writers

Stop all Mandate writers before pointing anything at a restored database. Do not
delete the existing managed database cluster.

```bash
kubectl -n agent-control-plane scale deploy/agent-control-plane --replicas=0
kubectl -n agent-control-plane scale deploy/agent-control-plane-local-worker --replicas=0
kubectl -n agent-control-plane scale deploy/agent-control-plane-callback-adapter --replicas=0
kubectl -n agent-control-plane scale deploy/agent-control-plane-model-gateway --replicas=0
kubectl -n agent-control-plane scale deploy/agent-control-plane-git-deliverer --replicas=0
```

If Argo reverts these scales, pause auto-sync where it exists or commit a
temporary values override in this repo. The invariant is that the old and
restored clusters must not both receive Mandate writes.

### 2. Choose The Restore Point

List the live cluster and backup points:

```bash
doctl databases list --format ID,Name,Engine,Version,Region,Size,Status
doctl databases backups 3adbbc39-dd07-4c0c-b172-081a247810d6 --format Created,Size
```

Use the latest transaction for infrastructure failure recovery. Use an explicit
timestamp for accidental deletion, bad migration, or operator error recovery.

### 3. Restore To A New Cluster

Restore to a new managed database cluster. Omitting
`--restore-from-timestamp` restores the latest available point.

```bash
RESTORE_NAME="mandate-restore-$(date -u +%Y%m%d%H%M)"
SOURCE_CLUSTER_ID="3adbbc39-dd07-4c0c-b172-081a247810d6"

doctl databases fork "$RESTORE_NAME" \
  --restore-from-cluster-id "$SOURCE_CLUSTER_ID" \
  --wait
```

For a point-in-time restore:

```bash
doctl databases fork "$RESTORE_NAME" \
  --restore-from-cluster-id "$SOURCE_CLUSTER_ID" \
  --restore-from-timestamp "2026-06-21 02:23:04 +0000 UTC" \
  --wait
```

Keep the original cluster online until the restored deployment is verified and
the owner approves cleanup.

### 4. Repoint Encrypted Secrets

Update only Postgres connection strings in the deployment repo's encrypted
runtime secret. Do not rotate service tokens as part of database restore unless
token compromise is part of the incident.

```bash
cd /path/to/GarzAICluster
export SOPS_AGE_KEY_FILE=keys/age-private.txt
sops secrets/agent-control-plane/runtime-secret.enc.yaml
```

Set `AGENT_PLATFORM_DATABASE_URL` to the restored cluster's private connection
string for the existing Mandate app role. If the restored read-only role is on
the same managed cluster, update `AGENT_PLATFORM_READONLY_SQL_DATABASE_URL` to
the restored cluster as well. Preserve the database name, schema, and role
posture. If the restored cluster does not contain the expected app/read-only
roles, stop and repair the restored cluster with deployment tooling before
syncing the application.

Commit the encrypted secret change and sync secrets:

```bash
argocd app sync agent-control-plane-secrets
argocd app wait agent-control-plane-secrets --health --sync --timeout 300
```

### 5. Check Schema Without Mutating It

Run the check-only schema command against the restored database before scaling
service pods. This command calls `assert_postgres_schema_current` and fails if
tracked migrations are pending or if an applied migration checksum drifted. It
does not apply DDL.

```bash
SCHEMA_CHECK_JOB="mandate-schema-check-$(date -u +%Y%m%d%H%M%S)"

kubectl -n agent-control-plane apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${SCHEMA_CHECK_JOB}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      imagePullSecrets:
        - name: regcred
      containers:
        - name: schema-check
          image: registry.digitalocean.com/sendouq/agent-platform:sha-fa3afd59e3af
          envFrom:
            - secretRef:
                name: agent-control-plane-secrets
          command: ["mandate-postgres-schema-check"]
EOF

kubectl -n agent-control-plane wait --for=condition=complete "job/${SCHEMA_CHECK_JOB}" --timeout=300s
kubectl -n agent-control-plane logs "job/${SCHEMA_CHECK_JOB}"
kubectl -n agent-control-plane delete "job/${SCHEMA_CHECK_JOB}"
```

If migrations are pending, stop the restore and decide whether to run
`mandate-migrate` with the current image or to roll the app image forward/back
to one compatible with the restored ledger.

### 6. Verify Rollback Compatibility Before Argo Reverts

Before reverting the Argo app to a prior image, prove the prior image can run
against the already-expanded schema:

1. Confirm the prior image contains every migration already present in
   `agent_platform.schema_migrations`.
2. Run `mandate-postgres-schema-check` with that prior image against the
   restored database.
3. Confirm `/readyz` in a one-off pod or temporary deployment before allowing
   Argo to roll the live deployment.

Do not run reversal DDL to make an old image fit. Use a reviewed forward
migration or restore to an earlier point inside the PITR window.

### 7. Resume Service

Sync the application and wait for health:

```bash
argocd app sync agent-control-plane
argocd app wait agent-control-plane --health --sync --timeout 600

kubectl -n agent-control-plane rollout status deploy/agent-control-plane
kubectl -n agent-control-plane rollout status deploy/agent-control-plane-local-worker
kubectl -n agent-control-plane rollout status deploy/agent-control-plane-callback-adapter
kubectl -n agent-control-plane rollout status deploy/agent-control-plane-model-gateway
kubectl -n agent-control-plane rollout status deploy/agent-control-plane-git-deliverer
```

Verify the public and in-cluster health surfaces:

```bash
kubectl -n agent-control-plane port-forward svc/agent-control-plane 18080:80
curl -fsS http://127.0.0.1:18080/readyz
curl -fsS https://agent-control-plane.garz.ai/readyz
curl -fsS https://agent-control-plane.garz.ai/healthz
```

Run one low-risk governed smoke through the existing private admin path. The
safe target is `mandate.deploy.smoke`; `approval.probe` is also acceptable when
the live Discord approval path is the thing being verified. Confirm the job is
accepted, leased, completed or correctly approval-gated, output-gated, audited,
and delivered through callbacks.

### 8. Close Out

After the restored deployment is healthy:

1. Record the restore in the drill ledger below.
2. Preserve the old cluster until the operator has exported any forensic data
   needed for the incident review.
3. Delete the scratch/old cluster only after the owner explicitly approves.
4. Record any RPO/RTO miss as a production-readiness follow-up.

## Hash-Chain Semantics

Snapshot and PITR restores preserve the `agent_platform.run_events` rows and
their `previous_event_hash` / `event_hash` chain exactly as of the restore point.
Chain verification should continue to pass for every retained run whose events
were included in the restored snapshot.

Partial or row-level recovery is different. If an operator manually copies rows
between clusters, deletes audit rows, or reconstructs only part of a run, the
audit hash chain is no longer automatically continuous. The expected behavior is:

1. Do not silently splice rows into the live audit chain.
2. Verify the restored subset with `audit.digest` or the hash-chain verifier.
3. Record the recovery boundary in the incident notes, including source cluster,
   restore timestamp, copied tables, and first affected `run_id`.
4. Re-anchor future trust by treating the post-recovery event stream as a new
   operational epoch in the incident report. The code does not yet persist an
   external anchor; do not claim cryptographic continuity across manual row-level
   recovery.

## Drill Ledger

| Date | Type | Operator | Restore point | Schema check | `/readyz` | Smoke task | Elapsed RTO | Cleanup |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-06-24 | Read-only posture check | Codex via `doctl` | No restore created; confirmed live cluster metadata and daily backups from 2026-06-17 through 2026-06-24. | Not run | Not run | Not run | Not measured | No scratch resource created |
| 2026-07-23 | Rehearsed isolated PITR scratch restore | Codex with explicit operator approval from Cesar | Latest available PITR from `db-postgresql-nyc3-xscraper`; scratch cluster `mandate-restore-ces49-20260723t074131z` (`e97cbca4-fcc6-41d6-8d99-925702e19260`). | Passed: `mandate-postgres-schema-check` reported migrations current. | Passed on an isolated API copy: `{"ok":true,"environment":"prod"}`. | Passed: `mandate.deploy.smoke` job `job_3e5f312cdb3a4b2bb2340a64e3d2f684` reached `result.released` with accepted/progress/succeeded callbacks. | 21m51s from fork request to completed smoke; target is 4h. | Deleted the scratch database and all labeled temporary Kubernetes Deployments, Service, Secret, Jobs, and NetworkPolicy; production stayed ready and was never repointed. |

Live scratch-restore drill status: completed on 2026-07-23. Future rehearsals
must add a new row with the scratch cluster name, restore point, schema-check
result, `/readyz` result, smoke-task result, elapsed RTO, and cleanup outcome.

## References

- DigitalOcean PostgreSQL restore docs:
  `https://docs.digitalocean.com/products/databases/postgresql/how-to/restore-from-backups/`
- DigitalOcean PostgreSQL limits:
  `https://docs.digitalocean.com/products/databases/postgresql/details/limits/`
- Generic Mandate restore contract:
  `agent-platform/docs/postgres-restore-contract.md`
- Deployment values:
  `apps/agent-control-plane/values.yaml`
- Deployment applications:
  `argocd/applications/agent-control-plane.yaml` and
  `argocd/applications/agent-control-plane-secrets.yaml`
