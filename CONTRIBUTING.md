# Contributing to SplatTopConfig

Thanks for helping keep our GitOps pipeline healthy! This repo is lightweight by design; every change goes through PR review with platform ownership (see `.github/CODEOWNERS`).

## Quick Start

1. Clone both repos (`SplatTop` and `SplatTopConfig`) so you can coordinate code + config changes.
2. Read through:
   - `docs/bootstrap.md` – environment bring-up and required secrets.
   - `docs/release-workflow.md` – how images/digests move between repos.
   - `docs/secrets-strategy.md` – SOPS + Age guidance.
3. Install tooling: Helm 3, kubectl, argocd CLI, sops, age, Python 3.11+, Docker (for promtool checks).

## Making Changes

- Prefer small, focused PRs (e.g., “Bump API digest for staging”).
- Run validation locally (`helm lint`, `helm template`, `kubeconform`, `python scripts/validate_prometheus_config.py --values helm/splattop/values-prod.yaml`, `trufflehog filesystem --no-update --only-verified --fail .`) before opening a PR.
- Keep docs in sync with reality—if you introduce a new workflow or policy, update the relevant file in `docs/`.
- Never commit plaintext secrets. Use SOPS-encrypted files and run the secrets scanners (CI gate coming soon).
- If you need to decrypt `k8s/*.enc.yaml`, copy the Age private key from the GitHub Actions secret `SOPS_AGE_KEY` (or the Argo `sops-age-key` secret), save it under `~/.config/sops/age/keys.txt`, and set `SOPS_AGE_KEY_FILE` before running `sops`.

## CI & Reviews

- CI pipelines live under `.github/workflows/`. They currently lint Helm templates and run kubeconform; Prometheus + policy jobs are coming shortly.
- All PRs require at least one platform reviewer (see CODEOWNERS). Production/staging directories may require multiple approvals once branch protections are enabled.
- Automation that bumps digests will use a GitHub App account; human PRs should include context in the description (ticket link, release notes, etc.).

## Issue Labels

- `config-repo:infra` – Helm/Argo/K8s structural work.
- `config-repo:release` – digest bumps, promotion tooling, release docs.
- `config-repo:secrets` – SOPS/ESO/secret rotation tasks.

Feel free to add more labels as the workflow matures. For anything unclear, drop notes in `docs/developer-cheat-sheet.md` so future contributors have the answer.
