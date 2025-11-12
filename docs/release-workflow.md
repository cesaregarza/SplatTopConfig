# Release & Promotion Workflow

This doc describes the target-state flow once the config repo owns deployments. Every deploy should be reproducible, digest-pinned, and traceable via PR history.

## Overview

1. App repo merge triggers CI → build images per service → publish tags & digests → emit `component-tags.json`.
2. Automation (GitHub App or Actions workflow) consumes the artifact and opens a PR in this repo that bumps only the affected services’ digests/tags under `helm/splattop/values-*.yaml`.
3. Config repo CI validates manifests (helm lint/template, kubeconform, Prometheus rule checks, optional OPA/Kyverno tests).
4. Review + merge rules:
   - Dev: auto-merge OK after CI.
   - Staging/Prod: require human review (platform DRI) + green CI.
5. Post-merge, Argo syncs dev automatically; staging/prod either auto-sync with gates or require manual sync, depending on `argo-operations.md`.

## Artifacts & Signals

- `component-tags.json` structure:

```json
{
  "api":    { "tag": "1.7.3", "sha": "abc1234", "digest": "sha256:..." },
  "web":    { "tag": "2.4.0", "sha": "abc1234", "digest": "sha256:..." },
  "worker": { "tag": "0.9.1", "sha": "abc1234", "digest": "sha256:..." }
}
```

- Stored as a workflow artifact plus job summary for humans.
- Automation references this file to update `values-{env}.yaml`. Tags remain in the values files for readability, but Argo deploys by digest.

## Rollbacks

1. Identify the last-good config repo commit (or tag).
2. `git revert` the digest bump commit (never rebuild images).
3. Merge the revert PR (staging/prod still require review).
4. Trigger Argo sync (manual for prod if required). Verify:
   - `argocd app wait splattop-prod --health`.
   - `kubectl get pods -n prod` to ensure rollout completes.
5. Document the incident in `docs/argo-operations.md` (game day log) + retro issue.

Goal: rollback ≤ 5 minutes from revert merge to healthy status.

## Hotfix Path

1. Cherry-pick/apply fix in app repo; merge into main (or hotfix branch).
2. CI builds only impacted services and updates `component-tags.json`.
3. Automation opens “Hotfix” PR here bumping relevant digests.
4. Staging review is mandatory but can be expedited (pager/on-call).
5. After merge, manually trigger prod Argo sync (or rely on auto with confirmation).
6. Follow up with a postmortem + ensure tests cover the regression.

## Policy Guardrails

- No post-merge mutations in app repo (`main` must match built images; i18n copies happen pre-build or in Dockerfile).
- Config repo merges only via reviewed PRs (no direct push to `main`).
- CI enforces digest-only manifests (`conftest` / Kyverno tests output error on mutable tags).
- CODEOWNERS require platform review for `envs/staging/**` and `envs/prod/**`.
- Bot PRs must label themselves (e.g., `automation:release-bump`) for auditability.

## Verification After Each Deploy

- [ ] Argo shows `Synced` & `Healthy`.
- [ ] `kubectl get deployment <svc>` shows new digest.
- [ ] Synthetic checks (smoke tests) pass.
- [ ] Update release log (GitHub release notes or `docs/release-log.md` TBD).

If any verification fails, run the rollback steps above and document findings.
