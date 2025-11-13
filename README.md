# SplatTopConfig

This repository tracks the Kubernetes and Argo CD configuration for SplatTop. All manifests, Helm charts, and GitOps policies that control the cluster land here rather than in the application codebase.

- `helm/` – per-service charts plus the umbrella chart that deploys the full stack. Values files cover local, default, and production overlays.
- `argocd/` – the production AppProject and Application definitions used by Argo CD, along with documentation for the sync/RBAC model.
- `k8s/` – legacy manifests, monitoring stack definitions, ingress resources, and secrets templates that are gradually being converted into Helm values.
- `docs/` – operational runbooks (bootstrap, release workflow, Argo operations, secrets strategy, developer cheat sheet) and planning notes.
- `scripts/` – shared tooling, currently centered around Prometheus rule validation and ancillary helpers referenced in the docs.

## Current State

- Charts, raw manifests, and Argo CD assets have been migrated from the application repository and now live here exclusively.
- Repository scaffolding is complete: documentation set, CODEOWNERS, CONTRIBUTING guide, and base `.gitignore`.
- `scripts/validate_prometheus_config.py` renders the Helm chart before invoking `promtool` to ensure production rules stay valid.
- Secret management primitives are wired up (SOPS config, Age public key, encrypted secret templates, TruffleHog job).
- Remaining automation work (CI workflows, secret rotation helpers, digest bumping) is tracked in `docs/config_repo_split_plan.md`.
