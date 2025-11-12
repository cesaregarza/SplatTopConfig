# Developer Cheat Sheet

Quick reference for engineers touching the config repo after the split.

## Cloning & Layout

```bash
git clone git@github.com:SplatTop/SplatTop.git
git clone git@github.com:SplatTop/SplatTopConfig.git
tree -L 1 SplatTopConfig
```

- `helm/` – charts per service (api, react, celery, monitoring).
- `argocd/` – Applications, AppProjects.
- `k8s/` – legacy manifests (gradually retired or converted to Helm values) plus encrypted secrets (`*.enc.yaml`).
- `docs/` – the files in this directory (start with `README.md`).

## Validating Changes Locally

```bash
cd SplatTopConfig
helm lint helm/splattop
helm template helm/splattop -f helm/splattop/values-dev.yaml > /tmp/dev.yaml
kubeconform -strict /tmp/dev.yaml
python scripts/validate_prometheus_config.py --values helm/splattop/values-prod.yaml
trufflehog filesystem --no-update --only-verified --fail .
```

## Coordinating with App Repo

- Rebuild images only if you touch code/dockerfiles; config-only changes never trigger builds.
- Pull `component-tags.json` artifact from the latest app CI run when testing release bumps.
- Use `scripts/bump_release_version.py --dry-run` (app repo) to preview upcoming tags.

## Common Tasks

| Task | Command/Notes |
| ---- | ------------- |
| Update image digest | Edit `helm/splattop/values-<env>.yaml` (per service) + run `make validate` (TODO). |
| Add new service | Add Helm template, register in `values*.yaml`, update `argocd/applications/*.yaml`, run lint/template. |
| Rotate Age keys | Follow `docs/secrets-strategy.md`. |
| Check Argo status | `argocd app list`, `argocd app get splattop-prod`. |
| Rollback | See `docs/release-workflow.md#Rollbacks`. |

## Troubleshooting

- **App repo CI didn’t emit `component-tags.json`:** rerun workflow with `components` input or rebuild locally (`make docker-build COMPONENT=api`), then craft the JSON manually.
- **Argo stuck OutOfSync:** run `argocd app diff <app>`; if the diff matches your PR, sync after approval. If not, check for manual cluster drift (should be rare post-cutover).
- **Secrets won’t decrypt:** ensure `SOPS_AGE_KEY_FILE` points to a file that contains the private key (copy from GitHub secret `SOPS_AGE_KEY` or the Argo `sops-age-key` secret) and that the file has Unix newlines (`dos2unix` sometimes needed).

## Editing Secrets

```bash
cd SplatTopConfig
printf '%s\n' "$SOPS_AGE_KEY" > ~/.config/sops/age/keys.txt  # contents from GH secret
export SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt
sops k8s/secrets.enc.yaml
```

- After editing, re-run `trufflehog filesystem --no-update --only-verified --fail .` to ensure nothing plaintext leaked.

Append tips here as you discover new sharp edges.
