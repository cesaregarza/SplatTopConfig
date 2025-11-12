# Infra Docs Overview

These documents capture the canonical workflows for the SplatTop config repository. Read them in roughly this order when contributing here:

1. `bootstrap.md` – bring-up steps for a fresh cluster + Argo that points to this repo.
2. `release-workflow.md` – how images flow from the app repo into environment value bumps, including hotfixes/rollbacks.
3. `argo-operations.md` – AppProject guardrails, policy enforcement, and day-to-day Argo duties.
4. `secrets-strategy.md` – SOPS + Age plan, rotation, and CI/Dev ergonomics.
5. `developer-cheat-sheet.md` – quick reference for common commands/tasks after the split.
6. `k8s/secrets.template.yaml` → `k8s/secrets.enc.yaml` – example flow for encrypted secrets.

Supplemental references:

- `docs/config_repo_split_plan.md` (in the app repo) – migration backlog until cutover completes.
- GitHub issues labeled `config-repo` – track outstanding automation (CI PR bump bot, policy enforcement, etc.).
