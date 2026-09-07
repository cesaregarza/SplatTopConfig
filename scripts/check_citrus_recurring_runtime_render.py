#!/usr/bin/env python3
"""Render and verify the inert and enabled-safe Citrus runtime matrix."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHART = REPO_ROOT / "helm" / "citrus"


class ContractError(RuntimeError):
    """Raised when a render violates the local release contract."""


@dataclass(frozen=True)
class RenderSpec:
    name: str
    release: str
    namespace: str
    value_files: tuple[str, ...]
    arguments: tuple[str, ...] = ()


def _payment_arguments(*, development: bool, runtime: bool) -> tuple[str, ...]:
    environment = "development" if development else "production"
    owner = "citrus-dev" if development else "citrus"
    mode = "deny" if development else "allow"
    arguments = [
        "--set",
        "paymentSafety.enabled=true",
        "--set-string",
        f"paymentSafety.environment={environment}",
        "--set-string",
        f"paymentSafety.owner={owner}",
        "--set-string",
        f"paymentSafety.networkMode={mode}",
        "--set",
        "paymentSafety.policy.required=true",
        "--set-string",
        "paymentSafety.policy.provider=cilium",
        "--set-string",
        "paymentSafety.policy.revision=ces-845-ci",
        "--set",
        "paymentSafety.networkPolicy.enabled=true",
    ]
    if development:
        arguments.extend(
            [
                "--set-string",
                "paymentSafety.networkPolicy.database.host=db.dev.example",
            ]
        )
    if runtime:
        arguments.extend(
            [
                "--set",
                "billingWorker.enabled=true",
                "--set-string",
                "billingWorker.topologyRevision=ces-850-ci",
                "--set",
                "recurringRuntime.enabled=true",
                "--set-string",
                "recurringRuntime.topologyRevision=ces-850-ci",
                "--set",
                "recurringRuntime.preflight.enabled=true",
                "--set-string",
                "recurringRuntime.preflight.topologyRevision=ces-850-ci",
                "--set-string",
                "recurringRuntime.health.topologyRevision=ces-850-ci",
            ]
        )
    return tuple(arguments)


def _render_specs() -> tuple[RenderSpec, ...]:
    return (
        RenderSpec("citrus-prod", "citrus", "default", ("values.yaml",)),
        RenderSpec(
            "citrus-dev",
            "citrus-dev",
            "citrus-dev",
            ("values.yaml", "values-dev.yaml", "values-payment-dev.yaml"),
        ),
        RenderSpec(
            "citrus-runtime-dev",
            "citrus-dev",
            "citrus-dev",
            (
                "values.yaml",
                "values-dev.yaml",
                "values-payment-dev.yaml",
                "values-recurring-dev.yaml",
            ),
        ),
        RenderSpec(
            "citrus-payment-prod",
            "citrus",
            "default",
            ("values.yaml", "values-payment-prod.yaml"),
            _payment_arguments(development=False, runtime=False),
        ),
        RenderSpec(
            "citrus-payment-dev",
            "citrus-dev",
            "citrus-dev",
            ("values.yaml", "values-dev.yaml", "values-payment-dev.yaml"),
            _payment_arguments(development=True, runtime=False),
        ),
        RenderSpec(
            "citrus-payment-safety-dev",
            "citrus-dev",
            "citrus-dev",
            ("values.yaml", "values-dev.yaml"),
            _payment_arguments(development=True, runtime=True),
        ),
        RenderSpec(
            "citrus-payment-safety-prod",
            "citrus",
            "default",
            ("values.yaml",),
            _payment_arguments(development=False, runtime=True),
        ),
    )


def _run(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ContractError(f"unable to execute {command[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise ContractError(f"command failed with status {result.returncode}: {detail}")
    return result


def _require(contents: str, expected: str, *, render: str) -> None:
    if expected not in contents:
        raise ContractError(f"{render} is missing required marker: {expected}")


def _active_dev_revision(rendered_dev: str) -> str:
    image_tags = set(
        re.findall(
            r'\bimage:\s*["\']?registry\.digitalocean\.com/'
            r'sendouq/citrus:([^"\'\s]+)',
            rendered_dev,
        )
    )
    if len(image_tags) != 1:
        raise ContractError(
            "active dev render must contain exactly one unique Citrus image tag"
        )
    image_revision = next(iter(image_tags))
    if not re.fullmatch(r"[0-9a-f]{40}", image_revision):
        raise ContractError("active dev image tag must be a lowercase 40-hex SHA")
    return image_revision


def _verify_renders(rendered: dict[str, str], *, active_dev_revision: str) -> None:
    prod = rendered["citrus-prod"]
    dev = rendered["citrus-dev"]
    safe_dev = rendered["citrus-payment-safety-dev"]
    safe_prod = rendered["citrus-payment-safety-prod"]
    runtime_dev = rendered["citrus-runtime-dev"]

    for marker in (
        "app.kubernetes.io/instance: citrus\n",
        'ALLOWED_HOSTS: "citrus-grace.com,www.citrus-grace.com"',
        "app.kubernetes.io/component: migrations",
        "app: citrus-web",
        "app.kubernetes.io/component: media-worker",
        "app.kubernetes.io/component: media-requeue",
        "app.kubernetes.io/component: media-gc",
    ):
        _require(prod, marker, render="citrus-prod")
    for marker in (
        "app.kubernetes.io/instance: citrus-dev\n",
        'SITE_NAME: "Citrus Grace Dev"',
        "    - host: dev.citrus-grace.com",
    ):
        _require(dev, marker, render="citrus-dev")

    for name, contents in (("citrus-prod", prod), ("citrus-dev", dev)):
        for forbidden in (
            "app.kubernetes.io/component: billing-worker",
            "app.kubernetes.io/component: recurring-preflight",
            "app.kubernetes.io/component: recurring-tick",
            "app.kubernetes.io/component: recurring-health",
            "CITRUS_EXPECTED_SOURCE_REVISION",
            "RECURRING_RUNTIME_TOPOLOGY_REVISION",
        ):
            if forbidden in contents:
                raise ContractError(
                    f"{name} must keep disabled runtime marker absent: {forbidden}"
                )

    for forbidden in (
        "PAYMENT_NETWORK_MODE",
        "citrus.grace/payment-egress-boundary",
        "kind: CiliumNetworkPolicy",
    ):
        if forbidden in prod:
            raise ContractError(
                f"citrus-prod must keep payment safety marker absent: {forbidden}"
            )

    for marker in (
        "kind: CiliumNetworkPolicy",
        "citrus.grace/payment-egress-boundary: enabled",
        'citrus.grace/payment-egress-policy-revision: "ces-845-dev-v1"',
        "app.kubernetes.io/component: direct-order-payment-sweep",
        "  suspend: false",
        (f'citrus.grace/verified-image-tag: "{active_dev_revision}"'),
        "name: citrus-dev-sweep-runtime",
        'value: "development"',
        'value: "deny"',
    ):
        _require(dev, marker, render="citrus-dev")
    if dev.count("name: PAYMENT_EGRESS_POLICY_REVISION") != 6:
        raise ContractError("citrus-dev must attest exactly 6 active Citrus containers")
    if dev.count('citrus.grace/payment-egress-policy-revision: "ces-845-dev-v1"') != 4:
        raise ContractError(
            "citrus-dev must retain exactly 4 active payment policy receipts"
        )

    for marker in (
        "kind: CiliumNetworkPolicy",
        "app.kubernetes.io/component: billing-worker",
        "app.kubernetes.io/component: recurring-preflight",
        "app.kubernetes.io/component: recurring-tick",
        "app.kubernetes.io/component: recurring-health",
        'citrus.grace/recurring-topology-revision: "ces-850-ci"',
        'value: "deny"',
        'matchName: "db.dev.example"',
        'matchName: "citrus-media-dev.nyc3.digitaloceanspaces.com"',
        'citrus.grace/payment-egress-policy-revision: "ces-845-ci"',
    ):
        _require(safe_dev, marker, render="citrus-payment-safety-dev")

    if safe_dev.count("name: PAYMENT_EGRESS_POLICY_REVISION") != 10:
        raise ContractError(
            "citrus-payment-safety-dev must attest exactly 10 Citrus containers"
        )
    if safe_dev.count("name: RECURRING_RUNTIME_TOPOLOGY_REVISION") != 5:
        raise ContractError(
            "citrus-payment-safety-dev must attest exactly 5 runtime containers"
        )
    if safe_dev.count("name: CITRUS_EXPECTED_SOURCE_REVISION") != 5:
        raise ContractError(
            "citrus-payment-safety-dev must bind exactly 5 runtime containers "
            "to the expected source revision"
        )
    if safe_prod.count("name: CITRUS_EXPECTED_SOURCE_REVISION") != 5:
        raise ContractError(
            "citrus-payment-safety-prod must bind exactly 5 runtime containers "
            "to the expected source revision"
        )
    if re.search(r"matchName:.*stripe\.(?:com|network)", safe_dev, re.IGNORECASE):
        raise ContractError(
            "citrus-payment-safety-dev must omit every Stripe destination"
        )

    for marker in ("kind: CiliumNetworkPolicy", "- all", 'value: "allow"'):
        _require(safe_prod, marker, render="citrus-payment-safety-prod")

    if _active_dev_revision(runtime_dev) != active_dev_revision:
        raise ContractError("citrus-runtime-dev must retain the active dev image")
    for component in (
        "billing-worker", "recurring-preflight", "recurring-tick", "recurring-health"
    ):
        _require(
            runtime_dev,
            f"app.kubernetes.io/component: {component}",
            render="citrus-runtime-dev",
        )
    for name, count in (
        ("PAYMENT_EGRESS_POLICY_REVISION", 11),
        ("RECURRING_RUNTIME_TOPOLOGY_REVISION", 5),
        ("CITRUS_EXPECTED_SOURCE_REVISION", 5),
    ):
        if runtime_dev.count(f"name: {name}") != count:
            raise ContractError(
                f"citrus-runtime-dev must project {name} into exactly {count} containers"
            )
    if re.search(r"matchName:.*stripe\.(?:com|network)", runtime_dev, re.IGNORECASE):
        raise ContractError("citrus-runtime-dev must omit every Stripe destination")

    for name, contents in rendered.items():
        if re.search(r"^kind:\s+Secret\s*$", contents, re.MULTILINE):
            raise ContractError(f"{name} must never render a Secret object")


def run_contract(
    *,
    chart: Path,
    output_dir: Path,
    helm: str,
    lint: bool,
) -> dict[str, object]:
    chart = chart.resolve()
    if not (chart / "Chart.yaml").is_file():
        raise ContractError(f"Citrus chart not found: {chart}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ContractError(f"output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    version = _run([helm, "version", "--short"], cwd=REPO_ROOT).stdout.strip()
    if lint:
        _run([helm, "lint", str(chart)], cwd=REPO_ROOT)

    rendered: dict[str, str] = {}
    for spec in _render_specs():
        command = [
            helm,
            "template",
            spec.release,
            str(chart),
            "--namespace",
            spec.namespace,
        ]
        for value_file in spec.value_files:
            command.extend(["-f", str(chart / value_file)])
        command.extend(spec.arguments)
        contents = _run(command, cwd=REPO_ROOT).stdout
        if not contents.strip():
            raise ContractError(f"{spec.name} rendered no resources")
        (output_dir / f"{spec.name}.yaml").write_text(
            contents,
            encoding="utf-8",
        )
        rendered[spec.name] = contents

    _verify_renders(
        rendered,
        active_dev_revision=_active_dev_revision(rendered["citrus-dev"]),
    )
    return {
        "gate": "citrus-recurring-runtime-render",
        "helm_version": version,
        "render_count": len(rendered),
        "result": "succeeded",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", type=Path, default=DEFAULT_CHART)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--helm", default="helm")
    parser.add_argument("--skip-lint", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = run_contract(
            chart=args.chart,
            output_dir=args.output_dir,
            helm=args.helm,
            lint=not args.skip_lint,
        )
    except ContractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    else:
        print(
            "Citrus recurring runtime render contract passed: "
            f"{receipt['render_count']} renders with {receipt['helm_version']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
