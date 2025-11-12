# Secrets Strategy (SOPS + Age)

The config repo never stores plaintext secrets. We use SOPS with Age encryption for Kubernetes manifests and supporting automation.

## Key Management

- Generate Age key pair:
  - `age-keygen -o keys/age-private.txt`
  - Copy the public portion into `keys/age-public.txt` (tracked in git for easy reuse).
  - Store the private key in:
    - GitHub Actions secret `SOPS_AGE_KEY` (so CI can decrypt when validating manifests).
    - Kubernetes secret `argocd/sops-age-key` (if/when we wire ksops/Argo SOPS integration).
    - Optional offline backup (YubiKey/encrypted USB) or password manager entry for manual decrypts.
- Update `.sops.yaml` with the Age recipient string (currently `age16yxsawhpecdrhas2q3z246q3tjq8889m552lqhjcgf8jnt7naszqqgz8vt`) whenever keys rotate.
- Record fingerprints here for audits.

## Encrypting Files

- Convert any secret manifest to encrypted form:
  - Create/edit the plaintext template (e.g., `k8s/secrets.template.yaml`), copy it to `*.enc.yaml`, and run `SOPS_AGE_RECIPIENTS=$(cat keys/age-public.txt | tail -n1) sops --encrypt --in-place k8s/secrets.enc.yaml`.
  - Delete/ignore the plaintext copy once you’re done editing.
- Use `sops -d` or `sops k8s/secrets.enc.yaml` for temporary reads; never commit decrypted output.
- For Helm values, prefer sealed values or reference ESO SecretStore once available.

## Developer Workflow

1. Install `sops` and `age`.
2. Export the private key to `~/.config/sops/age/keys.txt` (grab it from GitHub Actions secret `SOPS_AGE_KEY` or the cluster secret if you have access).
3. `export SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt`.
4. Decrypt, edit, and re-encrypt (`sops k8s/secrets.enc.yaml` handles this interactively).
5. Run `git diff` to ensure only ciphertext changed.

## CI Usage

- Store Age private key as GitHub Actions secret (`SOPS_AGE_KEY`).
- GitHub workflow should write `$SOPS_AGE_KEY` to a temp file, set `SOPS_AGE_KEY_FILE`, run `sops -d`/`sops -e`, then securely delete the temp file.
- Block logging decrypted content:
  - Wrap commands in `set +o xtrace`.
  - Use `git-secrets`/`trufflehog` to scan PRs.
- `.sops.yaml` currently targets files matching `k8s/.*\.enc\.yaml`; extend the rule set as more encrypted artifacts appear and add CI checks to ensure no plaintext equivalents are committed.

## ESO / External Secrets (Optional)

- If we adopt External Secrets Operator later:
  - Keep SOPS for bootstrap secrets (ESO SecretStore credentials).
  - Document ESO CRDs and Example `ExternalSecret` objects here.
  - Still avoid plaintext secrets; ESO pulls from AWS/GCP/DO secret managers.

## Rotation & Recovery

- Annual key rotation:
  1. Generate new Age key pair.
  2. Add new public key to `.sops.yaml`.
  3. Re-encrypt files (`sops updatekeys -y`).
  4. Distribute new private key; retire old key after confirmation.
- Recovery drill every 6 months: assume key loss, restore from offline backup, verify decrypt → encrypt flow.

- Document each rotation/drill (date, operator) at the bottom of this file.

## In-Cluster Usage

- To let Argo/ksops decrypt directly, create the secret:

  ```bash
  kubectl -n argocd create secret generic sops-age-key \
    --from-literal=age.agekey="$SOPS_AGE_KEY"
  ```

- Reference this secret in the ksops/Argo SOPS config (TODO when we wire ksops). Rotating the key means updating the secret + GH Actions secret.

## Secrets Scanning

- GitHub Actions job `secrets-scan` runs TruffleHog on every push/PR (see `.github/workflows/ci.yaml`).
- Run the same scan locally before opening a PR:

  ```bash
  trufflehog filesystem --no-update --only-verified --fail .
  ```

- Prefer fixing/rewriting offending commits over suppressing findings. If you must suppress (rare), document the rationale here so future reviewers understand the context.
