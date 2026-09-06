# Argo Operations Handbook

This guide covers day-to-day management of Argo CD once it tracks the config repo.

## AppProjects & RBAC

- The shared `splattop` AppProject governs production, Citrus dev, and
  infrastructure Applications. Its manifest lives at
  `argocd/projects/splattop-project.yaml`.
- The project pins its allowed repositories and explicitly enumerates every
  permitted in-cluster destination namespace.
- Resource whitelists mirror the previous settings so Helm can continue to manage monitoring/cluster objects required by prod.
- The project has no time-based sync window. Applications that declare
  automated sync may reconcile continuously; manual Applications remain gated
  until an operator starts their sync.
- Only the `splattop-admins` group is bound (role `proj:splattop:admin`). Set `policy.default: role:readonly` in `argocd-rbac-cm` so casual logins stay read-only.
- AppProject manifests are not reconciled by `splattop-root`. Review
  `kubectl diff -f argocd/projects/splattop-project.yaml`, confirm no automated
  Application has queued work, then apply the reviewed manifest with
  `kubectl apply -f argocd/projects/splattop-project.yaml` rather than editing
  the object in the UI.

## Sync Policies

| Environment | Sync Policy | Notes |
| ----------- | ----------- | ----- |
| prod        | declared per Application | Automated apps reconcile continuously. `agent-workloads` and other manual apps remain operator-gated; wait for the automated registry overlay to become Synced and Healthy before manually syncing dependent workloads. |

Application-specific details are codified under `argocd/applications/`; update
the owning manifest rather than flipping settings in the UI.

## Mandate Deploy Train

Use `scripts/mandate_deploy_train.py` for an authorized Mandate production
reconcile. Do not issue separate `argocd app sync` commands for the seven
Applications in this train: that reintroduces the missed-overlay and
refresh/sync race classes from CES-382.

The command has four hard boundaries:

1. Before any Argo mutation, it requires a clean checkout whose `HEAD` and
   canonical `cesaregarza/GarzAICluster` origin `main` equal the full confirmed
   SHA. It runs the existing
   release-pin and workload-identity/SOPS digest gates, validates the desired
   published skill bundle from a registry-digest-pinned image, compares the
   complete live Application specs (including sync options), and refuses any
   pending, Running, or Terminating operation. It also proves that no scheduled
   or on-demand verifier Job is nonterminal and that the CronJob template can
   produce the exact bounded `mandate verify --format json` Job. The required
   production kube context is selected and read back exactly.
2. It treats `splattop-root` as the Application-spec precondition, then
   serializes secrets → skills → registry overlay → control plane → workers.
   Each Application completes hard-refresh → observed revision → adoption of an
   exact automated operation or a correlated exact-revision manual sync →
   Synced/Healthy settlement before the next Application is refreshed. This is
   stage-serial because refreshing an automated Application can itself start a
   sync. If any Application or the semantic skill bundle differs during
   preflight, every Application receives a correlated full Hook sync in this
   canonical order, even when an early exact automated operation already
   settled it. Remote `main` is rechecked at the final mutation boundary and
   before each Application; the mutable skill tag is rechecked against its
   originally resolved digest immediately before the skills stage.
   If a hard refresh exposes drift that cached preflight state hid, the command
   submits no lone downstream sync: it re-runs the read-only preflight and
   restarts the entire canonical pass in forced-replay mode. Correlated manual
   replays use a full Hook sync with pruning enabled so reviewed resource
   deletions are applied along with updates.
3. It compares the complete content-addressed desired skill bundle with the
   live `mandate-skill-packs` ConfigMap. Argo `Synced` alone is not accepted,
   because that ConfigMap's data is intentionally ignored by Argo. The
   registry-overlay receipt separately requires the rollout-strategy Sync hook
   and ordered restart PostSync hook to have succeeded.
4. After a changed train, it clones the bounded synthetic-live-verify CronJob
   template but runs the landed CES-368 command `mandate verify --format json`.
   It preserves the probe environment, principal, journeys, ServiceAccount,
   image, resources, security context, and deadlines, then parses exact
   `deployment-smoke` and `readonly-query-skill-digests` PASS results including
   `model_call.finished` before emitting final stage-named success. The Job's
   exact 480-second active deadline is the execution budget; the client allows
   only a fixed 30-second controller-settlement grace.

Argo requires manually initiated syncs of auto-sync Applications to use their
configured ref (for this train, `main`). The runner checks remote main at the
submission boundary and again after completion, and accepts only the correlated
operation's exact resolved commit. Manual Applications still receive immutable
commit overrides. Keep release merges serialized: these checks detect a branch
moving between validation and server-side resolution and stop the train, but
cannot prevent an automated Application from applying that concurrent change.
Automation policies and Hook execution remain enabled.

The mutation form requires both `--apply` and `--confirm-sha` with the full
40-character SHA for the checked-out and remote `main`:

```bash
uv run python scripts/mandate_deploy_train.py \
  --kubeconfig ~/.kube/config \
  --context do-nyc3-k8s-nyc3-garz-ai \
  --apply \
  --confirm-sha <full-garzaicluster-main-sha>
```

A fully reconciled rerun still performs the read-only/preflight and hard-refresh
checks, submits no sync, creates no verification Job, and emits a stage-named
`no-op` receipt. Low-level status inspection remains read-only:

```bash
python3 scripts/argocd_core.py \
  --kubeconfig ~/.kube/config \
  --context do-nyc3-k8s-nyc3-garz-ai \
  status agent-control-plane-registry-overlay
```

This productizes the choreography only. Merging it does not authorize running
the command, changing a sync window, re-minting workload identity, or bypassing
the normal production operator gate.

## Repository & Registry Credentials

1. **Config repo**
   - Create a read-only deploy key dedicated to Argo (`argocd-repo-garzaicluster` secret in the `argocd` namespace).
   - Reference it from `argocd-cm.repositories` so no developer PATs are needed inside the control plane.
2. **Container registry**
   - Mirror the existing DOCR `regcred` into the `argocd` namespace for metadata lookups, and keep per-namespace pull secrets for workloads.
   - Document the `kubectl create secret docker-registry ...` command used plus the rotation owner/date.
3. **Helm repos / OCI charts (if used)**
   - Capture auth + mirror strategy in this repo before onboarding any external chart.

Keep renewal dates in `developer-cheat-sheet.md` or a shared calendar.

## Argo UI Exposure

- `argo.splat.top` is the public entry point for the Argo CD UI/API. The DNS record already maps to the nginx ingress load balancer; keep it updated if the LB IP changes.
- TLS is provisioned by cert-manager via `k8s/argocd/certificate.yaml` (secret `argo-splat-top-tls`, issuer `letsencrypt-prod`). Reapply it after issuer/cluster moves.
- The ingress at `k8s/argocd/ingress.yaml` fronts `svc/argocd-server` with HTTPS pass-through. Apply this manifest whenever the controller name or annotations need to change.
- Once the ingress is reachable, patch `argocd-cm` with `data.url: https://argo.splat.top` so CLI logins and links point at the new hostname.

## Policy Enforcement

- Kyverno/Gatekeeper policies (post-cutover):
  - Deny mutable image tags.
  - Require CPU/memory requests & limits.
  - Optionally require cosign signatures once signing is enabled.
- Config repo CI runs `conftest test` mirroring these policies; Argo admission enforces live state.

## Monitoring & Alerts

- Enable Argo metrics service (`argocd-metrics`).
- Alert on:
  - Sync failures > 10 minutes.
  - Applications OutOfSync for prod namespaces.
  - Failed auto-syncs (expose via Prometheus rule).
- Capture alert runbooks (who responds, expected actions) in this file.

## Game Day / Drills

- Quarterly exercise:
  1. Deploy change via normal workflow.
  2. Introduce controlled failure (e.g., bad config).
  3. Detect via alerts.
  4. Roll back using config repo revert.
  5. Document findings + update docs/tests.

Record outcomes (date, scenario, owner) at the bottom of this file for traceability.

## In-Cluster Secret Decryption (SOPS/ksops)

- Ensure the Age private key is present as `sops-age-key` in the `argocd` namespace (`age.agekey` key).
- Argo CD 3.2 only honors kustomize flags from `argocd-cm.data.kustomize.buildOptions` (or per-app build options), so apply `k8s/argocd/argocd-cm-ksops-patch.yaml` to inject `--enable-alpha-plugins --enable-exec`; `argocd-cmd-params-cm` does nothing for kustomize flags.
- Patch repo-server to install ksops + sops and mount the key:  
  `kubectl patch deploy argocd-repo-server -n argocd --type strategic --patch-file k8s/argocd/repo-server-ksops-patch.yaml`
- The `bots-secrets` ApplicationSet simply renders `kustomization.yaml` + `ksops.yaml`; it relies on the global build options above rather than setting `enableAlphaPlugins` per app.
- If you need the CMP/plugin-server variant instead of plain kustomize+KSOPS, the full recipe lives in `docs/ksops-llm-response.md`.
- Rotate keys by updating the `sops-age-key` secret and reapplying the patch (or rolling repo-server) to ensure the new key is mounted.

## Bot Read-Only DB Access

When a Discord bot maintainer needs database reads without touching the FastAPI service:

1. **Encrypt their Discord token** via the helper script:

   ```bash
   uv run python scripts/onboard_bot_secret.py <bot-name> "<discord-token>"
   ```

   This writes `secrets/bots/<bot-name>/token.enc.yaml`, which the `bots-secrets` ApplicationSet syncs automatically.
   Store the token in `.env` as `BOT_TOKEN` to avoid passing it on the command line.

2. **Provision their schema + secret** with the helper script (it connects via `psql`, creates the schema/role, and optionally writes the Kubernetes Secret manifest):

   ```bash
   BOT_DB_ADMIN_URL="postgresql://admin:***@private-db:25060/xscraper?sslmode=require" \
     uv run python scripts/provision_bot_db.py <bot-name>
   ```

   - `BOT_DB_ADMIN_URL` (or `--admin-url`) must point at a superuser/owner account inside the cluster’s Postgres instance.
   - The script constrains the new role to its own schema (`bot_<bot-name>`) and prints the generated connection string for auditing.
   - Secrets auto-encrypt via SOPS when available, and Argo decrypts them in-cluster via ksops (Age key mounted in repo-server).
   - Scripts automatically load secrets from `.env` (or the path in `SPLATTOPCONFIG_ENV_FILE`) before falling back to interactive prompts, so keeping `BOT_DB_ADMIN_URL` there works without exporting it every time.

3. **Flip the network permission** inside `apps/bots/<bot>.yaml` so the `bot-netpol` chart opens only the needed egress:

   ```yaml
   permissions:
     postgres: false
     prometheus: false
     dbReadOnlyVPC: true
   ```

After the PR merges, Argo CD renders the new encrypted secrets (via the `splattop-bot-*-secret` ApplicationSet) and deploys the updated sandbox NetworkPolicy (via `splattop-bot-*-netpol`). Developers simply mount `bot-token` / `bot-db-readonly` inside their chart and can read from the DigitalOcean VPC-scoped Postgres instance.
