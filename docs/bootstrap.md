# Bootstrap Guide

Use this runbook when standing up a brand-new cluster or when rehydrating Argo to point at this config repo.

## Prerequisites

- GitHub repo: `SplatTopConfig` (private) with `main` branch.
- GitHub App or fine-grained PAT that has `contents:write` on this repo only (used by automation).
- Container registry credentials (DigitalOcean registry) and S3/DO Spaces credentials for i18n sync jobs.
- Age key pair for SOPS (public key checked into the repo, private key stored offline + in 1Password).

## One-Time Setup

1. **Clone repos locally**
   - `git clone git@github.com:SplatTop/SplatTop.git`
   - `git clone git@github.com:SplatTop/SplatTopConfig.git`
2. **Install tooling**
   - Helm, kubectl, argocd CLI, sops, age, jq, yq.
3. **Create deploy key for Argo**
   - Generate read-only SSH key.
   - Add public key in GitHub (`Settings → Deploy keys → Allow read access`).
   - Store private key as a Kubernetes secret `argocd-repo-creds`.
4. **Provision registry pull secrets + SOPS key**
   - Create `dockerconfigjson` secret in target namespaces (`kubectl create secret docker-registry splattop-registry ...`).
   - Reference the secret in `values-*.yaml` (if needed) and `argocd/` manifests.
   - Upload the Age private key to GitHub (`Settings → Secrets → Actions → New secret → SOPS_AGE_KEY`) and, if using ksops, create `kubectl -n argocd create secret generic sops-age-key --from-literal=age.agekey="$SOPS_AGE_KEY"`.
5. **Install Argo CD (if not already)**
   - Apply upstream manifests or helm install.
   - Login (`argocd admin initial-password`) and change the admin password (store in `.env` per plan).
6. **Register this config repo**
   - `argocd repo add git@github.com:SplatTop/SplatTopConfig.git --ssh-private-key-path ~/.ssh/splattop-config`
   - Verify Argo can list charts: `argocd repo list`.
7. **Apply AppProjects**
   - `kubectl apply -f argocd/projects/`.
   - Confirm restrictions (allowed destinations, namespaces, resource allow lists) align with the plan.
8. **Apply the production Application**
   - `kubectl apply -f argocd/applications/splattop-prod.yaml`.
   - Ensure `repoURL` references this repo and `targetRevision: main`.
9. **Seed secrets**
   - Populate `k8s/secrets.template.yaml` with environment-specific data, copy to `k8s/secrets.enc.yaml`, and run `SOPS_AGE_RECIPIENTS=$(tail -n1 keys/age-public.txt) sops --encrypt --in-place k8s/secrets.enc.yaml`.
   - Store the plaintext template outside git or delete it after encrypting.

## Validation Checklist

- [ ] `argocd app list` shows the `splattop-prod` app in `Synced` state using this repo.
- [ ] `argocd app get splattop-prod` displays the expected digest pinned images.
- [ ] `helm lint helm/splattop` passes locally.
- [ ] `kubeconform` (or `kubectl apply --dry-run=server`) succeeds for each env overlay.
- [ ] `sops -d` round-trip verified (developers can decrypt; CI cannot log plaintext).
- [ ] Documented deploy key fingerprint + Age key fingerprint in `secrets-strategy.md`.

When all boxes are checked, record the date/operator at the top of this file for traceability.
