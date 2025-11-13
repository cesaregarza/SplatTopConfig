#!/usr/bin/env python3
"""
Update Helm image tags inside the config repository.

Usage:
    python scripts/update_config_image_tags.py \
        --values-file helm/splattop/values-prod.yaml \
        --tag v2.0.3 \
        --component fastapi --component react \
        [--set-global]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ruamel.yaml import YAML


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update component image tags in a Helm values file."
    )
    parser.add_argument(
        "--values-file",
        required=True,
        type=Path,
        help="Path to the values YAML file to update.",
    )
    parser.add_argument(
        "--tag",
        required=True,
        help="Image tag to set for the selected components (e.g. v2.0.3).",
    )
    parser.add_argument(
        "--component",
        dest="components",
        action="append",
        required=True,
        help="Component key to update (repeat for multiple components).",
    )
    parser.add_argument(
        "--set-global",
        action="store_true",
        help="Also set global.appImageTag to the provided tag.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    yaml = YAML()
    yaml.preserve_quotes = True

    try:
        with args.values_file.open("r", encoding="utf-8") as fh:
            data = yaml.load(fh) or {}
    except FileNotFoundError:
        raise SystemExit(f"Values file '{args.values_file}' does not exist.")

    updated = False

    if args.set_global:
        global_section = data.setdefault("global", {})
        if global_section.get("appImageTag") != args.tag:
            global_section["appImageTag"] = args.tag
            updated = True

    for component in args.components:
        component = component.strip()
        if not component:
            continue
        node = data.setdefault(component, {})
        image = node.setdefault("image", {})
        if image.get("tag") == args.tag:
            continue
        image["tag"] = args.tag
        updated = True

    if not updated:
        print("No changes required; values already up to date.")
        return 0

    with args.values_file.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)

    print(
        f"Updated {args.values_file} with tag {args.tag} "
        f"for components: {', '.join(args.components)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
