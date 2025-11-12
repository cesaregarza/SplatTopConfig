# SplatTopConfig

This repository will be the canonical source for Kubernetes/Argo configuration once the split from the application repo completes.

- `helm/` – Helm charts (per service + optional umbrella) copied from the app repo.
- `argocd/` – Argo CD Applications/Projects that will be repointed to this repo during cutover.
- `k8s/` – Raw manifests and overlays (monitoring stack, ingress, secrets templates, etc.).
- `docs/` – Canonical runbooks (`bootstrap`, `release-workflow`, `argo-operations`, `secrets-strategy`, cheat sheets).
- `scripts/` – Shared tooling (e.g., Prometheus rule validation) refactored for the config repo.

## Current Status

- ✅ Initial asset sync (Helm, Argo, k8s).
- ✅ Base documentation (`docs/*.md`), CODEOWNERS, CONTRIBUTING guide, and repo-level `.gitignore`.
- ✅ Prometheus validation script migrated and now renders ConfigMaps via `helm template` before running promtool.
- ✅ Secrets scaffolding in place (`.sops.yaml`, `keys/age-public.txt`, `k8s/secrets.enc.yaml`, TruffleHog CI job).
- 🚧 CI workflows, secrets automation, and digest bump bot are being tracked in `docs/config_repo_split_plan.md`.
