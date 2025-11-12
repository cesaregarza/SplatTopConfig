# Argo Operations Handbook

This guide covers day-to-day management of Argo CD once it tracks the config repo.

## AppProjects & RBAC

- One AppProject per environment (`dev`, `staging`, `prod`, `monitoring`).
- Each project must specify:
  - `sourceRepos`: whitelist `git@github.com:SplatTop/SplatTopConfig.git`.
  - `destinations`: namespace + cluster combinations the project may touch.
  - `clusterResourceWhitelist` / `namespaceResourceBlacklist` as needed.
- Store manifests under `argocd/projects/` and apply via GitOps (no manual Argo edits).
- Review AppProjects quarterly to make sure new namespaces/services are captured.

## Sync Policies

| Environment | Sync Policy | Notes |
| ----------- | ----------- | ----- |
| dev         | auto-sync + self-heal + prune | fast feedback; tolerate drift fixes automatically. |
| staging     | auto-sync with `syncOptions: ['Validate=true']`; require manual approval for _prod_ directories touched in same PR. |
| prod        | manual sync or auto-sync with `syncWaves` + required checks (pager-duty ack). |

Document the final policy choice in `argocd/applications/*.yaml`.

## Repository & Registry Credentials

1. **Config repo**
   - Deploy key with read-only permissions.
   - Added via `argocd repo add`.
2. **Container registry**
   - Store DOCR credentials as Kubernetes secrets in each namespace.
   - Reference via `imagePullSecrets` in Helm values.
3. **Helm repos / OCI charts (if used)**
   - Document auth + mirror strategy.

Keep renewal dates in `developer-cheat-sheet.md` or a shared calendar.

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
