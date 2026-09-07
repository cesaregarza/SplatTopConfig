# Scripts

Utilities that were previously bundled with the app repo move here when they are infrastructure-focused or referenced by config-repo CI. Use [uv](https://docs.astral.sh/uv/) (`uv run python ...`) so the dependencies defined in `pyproject.toml` are installed automatically. Scripts automatically load variables from `.env` (override with `SPLATTOPCONFIG_ENV_FILE`) before reading other environment values.

## Available

- `verify_manifest_delta.py` – verifies an exact, value-aware delta between two
  multi-document Kubernetes manifests without printing changed values. It
  rejects duplicate resources and inventory changes and writes a receipt with
  resource identities and changed paths:

  ```bash
  uv run python scripts/verify_manifest_delta.py \
    --before /tmp/before.yaml --after /tmp/after.yaml \
    --expected /tmp/expected.json --output /tmp/verified-delta.json
  ```

- `update_citrus_release.py` – applies one exact lowercase 40-hex Citrus source
  revision to every active image binding owned by the selected environment. The
  binding registry at `helm/citrus/release-bindings.json` mirrors the Argo value
  file order, separates applied files from writable operational files, and
  gives each optional receipt an `auto-roll` or `manual-attestation` policy.
  The command validates the complete plan before an all-or-none write and emits
  only changed operational paths for exact Git staging:

  ```bash
  uv run python scripts/update_citrus_release.py \
    --environment dev \
    --source-revision <full-citrus-source-sha> \
    --capabilities-file /tmp/citrus-release-capabilities.json \
    --output-path-list /tmp/garzaicluster-values-files
  ```

  The Citrus source workflow creates a revision-bound capability receipt with
  a static, zero-network command check. The updater requires that receipt to
  name the exact release SHA. An enabled manual-attestation binding, an unknown
  optional image receipt, a missing required capability, or a partial write
  fails without advancing the release tuple. Production does not apply or
  modify `values-payment-prod.yaml`.

- `mandate_deploy_train.py` – the CES-395 interim `mandate up` command for the
  complete Mandate GitOps train. It uses the bounded `argocd_core.py`
  primitives through an owner-only temporary kubeconfig, enforces the client
  version in `argocd-client-version.txt`, and requires the exact current
  GarzAICluster `main` SHA:

  ```bash
  uv run python scripts/mandate_deploy_train.py \
    --kubeconfig ~/.kube/config \
    --context do-nyc3-k8s-nyc3-garz-ai \
    --apply \
    --confirm-sha <full-garzaicluster-main-sha>
  ```

  Before any refresh, the command requires a clean checkout at that SHA,
  confirms the canonical GarzAICluster origin `main` has not moved, runs the
  control-plane release-pin and
  workload-identity/SOPS digest gates, resolves the published skill image to an
  immutable registry digest, checks all seven complete live Application specs,
  rejects operation overlap, reads back the exact production context, and
  proves the bounded verifier template and Job capacity are available. It then
  processes `splattop-root` followed by exactly:
  control-plane/workload secrets → skills → registry overlay → control plane →
  workers. Every Application performs its own hard-refresh-and-observe before
  an exact automated operation is adopted or an exact-revision manual sync is
  submitted; it must settle before the next Application is refreshed. Any
  preflight Application or semantic-bundle drift forces a correlated full Hook
  replay with pruning enabled across all seven Applications in this order. An
  early exact automated sync is settled but never allowed to skip its later
  canonical manual replay.
  Remote `main` is guarded again before every stage, and the skill tag must
  still resolve to the preflight digest immediately before the skills stage.
  Late drift first exposed by a hard refresh cannot trigger a lone downstream
  sync: the command re-preflights and restarts the complete force-replay pass.

  The skills stage compares the complete content-addressed desired bundle with
  live `mandate-skill-packs`, so an ignored ConfigMap data difference cannot be
  mistaken for a no-op. The registry-overlay operation must be a full Hook sync
  and retain successful receipts for both the rollout-strategy Sync hook and
  ordered boot-cache restart PostSync hook.

  When a sync occurred, the final stage clones the bounded
  `cronjob/agent-control-plane-synthetic-live-verify` Job template and changes
  only the verifier command to landed CES-368 `mandate verify --format json`.
  It parses exact stage/status/journey evidence and requires
  `model_call.finished`; raw rendered environment or logs are never printed.
  The Job keeps the exact 480-second active deadline and the client adds only a
  fixed 30-second controller-settlement grace. A
  fully reconciled rerun submits no sync or verification Job and ends with
  `stage=deploy-train,result=no-op`. The command never re-mints identity, edits
  a Secret, changes an Argo sync window, or directly restarts a Deployment.

- `argocd_core.py` – pinned Argo CD 3.2.0 core-mode primitives used by the
  deploy train. It keeps the temporary kubeconfig, JSON snapshot parsing,
  exact-revision Hook sync submission, unique operation correlation, and
  race-safe polling in one reusable owner. Its command-line surface exposes
  read-only Application status only; mutation is owned by the complete Mandate
  deploy train.

- `onboard_bot_secret.py` – scaffolds/ encrypts a Discord bot token under `secrets/bots/<bot>/token.enc.yaml`. Example:

  ```bash
  uv run python scripts/onboard_bot_secret.py my-cool-bot "DISCORD_TOKEN"
  # or set BOT_TOKEN in .env and omit the argument:
  uv run python scripts/onboard_bot_secret.py my-cool-bot
  ```

  Commit the resulting `.enc.yaml` and let the `splattop-bot-*-secret` ApplicationSet sync it.

- `provision_bot_db.py` – connects to Postgres via `psql`, creates a schema + login limited to that schema, grants a read-only role (default: `readonly`) usage/select on the schema with default privileges, and (by default) gives both roles SELECT/USAGE on the shared `common` schema. Optionally write the secret manifest:

  ```bash
  BOT_DB_ADMIN_URL=postgresql://admin:***@private-db:25060/xscraper?sslmode=require \
    uv run python scripts/provision_bot_db.py my-cool-bot \
      --secret-file secrets/bots/my-cool-bot/db-secret.enc.yaml
  sops --encrypt --in-place secrets/bots/my-cool-bot/db-secret.enc.yaml
  ```

- `provision_agent_control_plane_secrets.py` – provisions the Agent Control
  Plane Postgres schema/role, generates service tokens, and writes encrypted
  runtime and registry secrets under `secrets/agent-control-plane/`. It loads
  `.env` and expects `BOT_DB_ADMIN_URL` plus `DO_REGISTRY_READ_TOKEN` unless
  explicit flags are supplied:

  ```bash
  SOPS_AGE_KEY_FILE=keys/age-private.txt \
    uv run python scripts/provision_agent_control_plane_secrets.py
  ```

- `provision_agent_control_plane_readonly_sql.py` – creates or rotates only the
  Agent Control Plane read-only SQL broker role and stores the selected database
  URL in the encrypted runtime secret without rotating service tokens. The
  default `bots` target writes `AGENT_PLATFORM_READONLY_SQL_DATABASE_URL`; the
  `xscraper_analytical` target writes
  `AGENT_PLATFORM_READONLY_SQL_ANALYTICAL_DATABASE_URL`. It grants `CONNECT`,
  schema `USAGE`, and `SELECT` on the configured relations only:

  ```bash
  SOPS_AGE_KEY_FILE=keys/age-private.txt \
    uv run python scripts/provision_agent_control_plane_readonly_sql.py
  ```

  ```bash
  SOPS_AGE_KEY_FILE=keys/age-private.txt \
    AGENT_CONTROL_PLANE_READONLY_SQL_TARGET=xscraper_analytical \
    XSCRAPER_DB_ADMIN_URL=postgresql://admin:***@private-db:25060/xscraper?sslmode=require \
    uv run python scripts/provision_agent_control_plane_readonly_sql.py
  ```

- `provision_agent_workloads_secrets.py` – provisions the Agent Workloads
  workspace schema/role, writes the worker-service token into both the
  Agent Workloads runtime secret and the Agent Control Plane runtime secret,
  and encrypts the namespace registry pull secret:

  ```bash
  SOPS_AGE_KEY_FILE=keys/age-private.txt \
    uv run python scripts/provision_agent_workloads_secrets.py
  ```

  Use `--read-schema <schema>` only when a workload needs explicit read-only
  source-schema access in addition to owning the `agent_workloads` schema.

- `validate_prometheus_config.py` – renders the Prometheus ConfigMaps from the Helm chart (`helm template --show-only …`) and runs `promtool check config/rules` inside a Docker container. Example (prod values):

  ```bash
  uv run python scripts/validate_prometheus_config.py --values helm/garz-observability/values-prod.yaml
  ```

  Add `--allow-missing` if you want the script to exit successfully when monitoring is disabled for a given values file.

- `check_agent_control_plane_registry_compat.py` – materializes the live
  Agent Control Plane registry overlay into a checked-out `agent-platform`
  source tree and builds the pinned revision's
  `RegistrySnapshot.from_repo(environment="prod")`. Use this when a registry
  overlay or policy change must be proven compatible with the deployed Mandate
  binary:

  ```bash
  uv run python scripts/check_agent_control_plane_registry_compat.py \
    --agent-platform-repo ../agent-platform
  ```

  The `agent-platform` checkout must be at the exact `targetRevision` declared
  in `argocd/applications/agent-control-plane.yaml`. Use
  `--print-target-revision` to retrieve that SHA for automation.

- `check_agent_control_plane_registry_overlay_render.py` – renders the actual
  single-source registry-overlay Helm chart and requires the ConfigMap, scoped
  RBAC, generated rollout-strategy Sync Job, and generated restart PostSync Job
  to appear together. It also renders the local Kustomize source-file view and
  compares both ConfigMaps with the committed golden:

  ```bash
  uv run python scripts/check_agent_control_plane_registry_overlay_render.py
  ```

- `check_agent_control_plane_config_coherence.py` – validates each configured
  synthetic live-verify journey against the effective production policy, the
  pinned `agent-platform` capability/result/event contracts, the deployment
  registry overlay, and the manifest extracted from the deployed skills bundle.
  The CI job reuses the provider-pin gate's pinned source checkout and DOCR
  authentication:

  ```bash
  crane export \
    registry.digitalocean.com/sendouq/agent-workloads-skills:main - \
    | tar -xO skill-bundle/manifest.json > /tmp/skills-manifest.json
  uv run python scripts/check_agent_control_plane_config_coherence.py \
    --agent-platform-repo ../agent-platform \
    --skills-manifest /tmp/skills-manifest.json \
    --skills-manifest-source \
    registry.digitalocean.com/sendouq/agent-workloads-skills:main:/skill-bundle/manifest.json
  ```

- `mandate_apply.py` – plans or writes a local CES-123
  `MandateWorkloadEnablement` document into deployment-owned files only. Dry-run
  is the default; `--write` edits files for a normal PR. It never reads or
  writes secret values, never mutates live Kubernetes objects, and reports SOPS
  or NetworkPolicy work as operator gaps:

  ```bash
  uv run python scripts/mandate_apply.py enablement.yaml --repo-root .
  uv run python scripts/mandate_apply.py enablement.yaml \
    --repo-root . \
    --write \
    --output-pr-body .git/mandate-apply-pr-body.md
  ```

  See `docs/mandate-apply.md` for the document schema and boundaries.

- `bootstrap_bot.py` – scaffolds a bot entry (apps/bots YAML), secrets folder (README/kustomization/ksops), and copies the DB CA into the shared chart. Examples:

  ```bash
  uv run python scripts/bootstrap_bot.py my-cool-bot \
    --chart-path apps/agent-8s \
    --values-file apps/agent-8s/values.dev.yaml
  ```

  Follow up with `onboard_bot_secret.py` and `provision_bot_db.py` to generate encrypted secrets.

## Adding New Scripts

1. Place them in this directory (subfolders allowed).
2. Prefer Python with no external dependencies beyond the standard library (or document the requirements in the script header).
3. If the script is referenced by CI, ensure `.github/workflows/validate.yaml` installs the prerequisites.
4. Document usage examples in this README so other contributors know how to run them.
