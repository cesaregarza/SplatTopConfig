# Agent Control Plane Registry Overlay

This app installs the production registry overlay for live `agent-workloads` worker-service paths.

The overlay is authored as ordinary source files under `registry/` and assembled into the `agent-control-plane-registry-overlay` ConfigMap by the small Helm chart in this directory. Do not hand-edit a rendered ConfigMap literal; keep changes in these files instead:

- `registry/workload_imports.yaml` imports deployment-pinned workload manifests and image digests for `data.workspace_probe`, `opencode.proposer`, and `opencode.apply_executor`.
- `registry/policy.prod.yaml` carries the production binding/budget overlay for the imported capabilities and synthetic smoke actor.
- `registry/evals.yaml` mirrors the pinned `agent-platform` eval registry and adds deployment smoke suites for the OpenCode proposer/apply imports.
- `registry/imports/*.json` are immutable `WorkloadManifestV1` payloads captured from the `agent-workloads` release pins in `apps/agent-workloads/values.yaml`.
- `registry/imports/*.jsonl` are the overlay-owned smoke datasets referenced by the imported manifests.

## Runtime mount contract

The generated ConfigMap keeps the existing runtime shape:

- `workload_imports.yaml`, `policy.prod.yaml`, and `evals.yaml` are mounted into `/app/registries`.
- Every other ConfigMap key is mounted under `/app/registries/imports`.
- `scripts/check_agent_control_plane_registry_compat.py` materializes the same shape before asking the pinned `agent-platform` checkout to build `RegistrySnapshot.from_repo(environment="prod")`.
- In pull-request CI, the same script compares the rendered ConfigMap data against
  the base branch's last-known-good ConfigMap, semantically for YAML/JSON/JSONL
  values, so readability-only edits cannot silently drift production authority.

## Sync behavior

The registry-overlay Application auto-syncs merged overlay changes, with prune and self-heal enabled and no project-level time restriction. `agent-workloads` intentionally remains manual-sync: the wave annotations order child Application objects only during a root sync and do not serialize the two child controllers on an ordinary release re-pin. Automating both independently could start a release-scoped ServiceAccount before Core loads its mapping. The overlay therefore reconciles first, including the retained `previous_release` subject, and the operator activates the workload only after this app is Synced and Healthy.

The control plane builds its `RegistrySnapshot` once at boot and never re-reads the mounted overlay, so every overlay change requires a restart of each present control-plane Deployment to take effect. Before that restart, the separate `Sync` hook Job in `templates/rollout-strategy-hook.yaml` applies `maxSurge: 0` and `maxUnavailable: 1` to the present singleton RollingUpdate Deployments. The callback adapter is an optional retired workload: a successful `kubectl get --ignore-not-found` with empty output records it as safely absent, while any required Deployment absence or API failure remains fatal. Present targets retain the full singleton checks, and the shared-RWO model gateway remains a singleton with `Recreate`; the hook never changes the CES-352 required affinity or the gateway strategy. This accepts a brief bounded singleton outage instead of requiring an unschedulable surge pod on an affinity-constrained, request-saturated node.

After the strategy hook succeeds, the existing `PostSync` hook Job in `templates/restart-hook.yaml`, using the ServiceAccount/Role in `templates/restart-rbac.yaml` with mutation restricted to the five named Deployments and list-only pod observation, processes the present Deployments one at a time. It performs the same exact optional callback presence check at the start of each iteration and skips only that callback when confirmed absent. For each present Deployment it captures the existing component pods, issues the restart, waits for the new generation to become fully updated, ready, and available, and then waits for every captured pod to disappear before proceeding. Rollout and old-pod drain share one 240-second budget, and every Kubernetes API call uses only the seconds remaining in that budget. A timeout or API error names the Deployment and phase in the hook log and fails the sync without retrying earlier restarts. Argo deletes successful generated hooks; failed Jobs and their logs remain for up to 24 hours for diagnosis.

The chart is intentionally a single Argo CD source. Kustomize cannot transform a resource that has only `metadata.generateName`, and the former Kustomize-plus-raw-directory Application could be synced through Argo's legacy singular-source operation shape. That operation rendered only the first source, omitted the hook from `syncResult`, and still reported success. Helm preserves the native `generateName` Jobs while making the ConfigMap, RBAC, and hooks an indivisible render.

The rollout order is API, model gateway, optional callback adapter, git deliverer, then local worker. Restarting the API while the live model gateway anchors its required affinity keeps the shared RWO auth volume node-local; restarting the gateway immediately afterward lets it prefer the newly healthy API node. The remaining boot-cached consumers do not start until that core pair is healthy, and the hook does not overlap their old pods' termination windows. This bounds the choreography to one Deployment's replacement and terminating pods at a time, but it does not replace CES-352's required hard topology guarantee for the shared RWO volume.

`kustomization.yaml` remains only as a local render-equivalence input for existing source-file tooling. CI renders the actual Helm Application source and requires the ConfigMap, scoped RBAC, generated rollout-strategy Sync Job, and generated restart PostSync Job to appear together.

The Argo Application intentionally does not set `ApplyOutOfSyncOnly=true`; selective syncs skip hooks, which would skip the strategy and restart jobs and leave the control plane serving the previous boot-cached registry snapshot.

## Authority notes

The imported manifests are data, not dispatch authority. Mandate still loads the overlay through registry validators, and dispatch still requires a policy grant, admission, a matching workload identity claim, lease projection, output-gate processing, and audit.

`agent_workloads.opencode_propose` is proposal-only reversible-staging authority. It receives only a per-job model-gateway leased token through the worker claim response, and its diff is released as metadata-only `opencode_proposal` artifact metadata.

`agent_workloads.opencode_apply` is consequential authority and remains behind `admin_confirm`. The apply worker is a separate `executor: true` `capability_worker`, not a hosted harness. It receives no model gateway URL, provider credentials, Git credentials, or database credentials.

The Core chart renders the same `RollingUpdate|0|1` strategy from production's
`rollingUpdate` values. Argo compares the complete strategy without ignore
rules. The Sync hook remains an idempotent guard before registry reloads; the
model gateway retains its independent chart-owned `Recreate` policy.
