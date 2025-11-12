# Scripts

Utilities that were previously bundled with the app repo move here when they are infrastructure-focused or referenced by config-repo CI.

## Available

- `validate_prometheus_config.py` – renders the Prometheus ConfigMaps from the Helm chart (`helm template --show-only …`) and runs `promtool check config/rules` inside a Docker container. Example (prod values):

  ```bash
  python scripts/validate_prometheus_config.py --values helm/splattop/values-prod.yaml
  ```

  Add `--allow-missing` if you want the script to exit successfully when monitoring is disabled for a given values file.

## Adding New Scripts

1. Place them in this directory (subfolders allowed).
2. Prefer Python with no external dependencies beyond the standard library (or document the requirements in the script header).
3. If the script is referenced by CI, ensure `.github/workflows/validate.yaml` installs the prerequisites.
4. Document usage examples in this README so other contributors know how to run them.
