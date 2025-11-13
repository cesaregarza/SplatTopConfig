# Argo Operations Handbook

This guide covers day-to-day management of Argo CD once it tracks the config repo.

## AppProjects & RBAC

- Production is the only environment managed from this repo today. The manifest lives at `argocd/projects/splattop-project.yaml`.
- The project pins `sourceRepos` to `https://github.com/cesaregarza/SplatTopConfig` and limits destinations to the `default` (app) and `monitoring` namespaces on the in-cluster API server.
- Resource whitelists mirror the previous settings so Helm can continue to manage monitoring/cluster objects required by prod.
- A weekday sync window (Mon–Fri, cron `0 15 * * 1-5`, `duration: 11h`) blocks off-hours deploys.
- Only the `splattop-admins` group is bound (role `proj:splattop:admin`). Set `policy.default: role:readonly` in `argocd-rbac-cm` so casual logins stay read-only.
- Apply project changes via GitOps (`kubectl apply -f argocd/projects`) rather than editing the object in the UI.

## Sync Policies

| Environment | Sync Policy | Notes |
| ----------- | ----------- | ----- |
| prod        | manual sync only | `ApplyOutOfSyncOnly=true`, `RespectIgnoreDifferences=true`, namespace autocreation for monitoring objects, and the weekday sync window above. |

All of the details are codified inside `argocd/applications/splattop-prod.yaml`; update that manifest rather than flipping settings in the UI.

## Repository & Registry Credentials

1. **Config repo**
   - Create a read-only deploy key dedicated to Argo (`argocd-repo-splattopconfig` secret in the `argocd` namespace).
   - Reference it from `argocd-cm.repositories` so no developer PATs are needed inside the control plane.
2. **Container registry**
   - Mirror the existing DOCR `regcred` into the `argocd` namespace for metadata lookups, and keep per-namespace pull secrets for workloads.
   - Document the `kubectl create secret docker-registry ...` command used plus the rotation owner/date.
3. **Helm repos / OCI charts (if used)**
   - Capture auth + mirror strategy in this repo before onboarding any external chart.

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
