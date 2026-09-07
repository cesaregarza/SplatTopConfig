from __future__ import annotations

import shutil
import subprocess
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")


@dataclass(frozen=True)
class SizingTarget:
    application: str
    chart: str
    release: str
    namespace: str
    value_files: tuple[str, ...]
    deployment: str
    container: str
    baseline_cpu_mcores: int
    cpu_request: str
    memory_request: str
    cpu_limit: str
    memory_limit: str
    replicas: int = 1


TARGETS = (
    SizingTarget(
        application="cegarza-blog",
        chart="helm/cegarza-blog",
        release="cegarza-blog",
        namespace="cegarza-blog",
        value_files=(
            "helm/cegarza-blog/values.yaml",
            "helm/cegarza-blog/values-cegarza.yaml",
        ),
        deployment="cegarza-blog",
        container="blog",
        baseline_cpu_mcores=100,
        cpu_request="50m",
        memory_request="256Mi",
        cpu_limit="500m",
        memory_limit="512Mi",
    ),
    SizingTarget(
        application="splattop-blog-prod",
        chart="helm/splattop-blog",
        release="splattop-blog",
        namespace="default",
        value_files=(
            "helm/splattop-blog/values.yaml",
            "helm/splattop-blog/values-prod.yaml",
        ),
        deployment="splattop-blog",
        container="blog",
        baseline_cpu_mcores=100,
        cpu_request="50m",
        memory_request="256Mi",
        cpu_limit="500m",
        memory_limit="512Mi",
    ),
    SizingTarget(
        application="skyquiet-server",
        chart="helm/skyquiet-server",
        release="skyquiet-server",
        namespace="default",
        value_files=("helm/skyquiet-server/values.yaml",),
        deployment="skyquiet-server-api",
        container="api",
        baseline_cpu_mcores=50,
        cpu_request="30m",
        memory_request="128Mi",
        cpu_limit="500m",
        memory_limit="512Mi",
        replicas=0,
    ),
    SizingTarget(
        application="spotify-hot-100",
        chart="helm/spotify-hot-100",
        release="spotify-hot-100",
        namespace="default",
        value_files=("helm/spotify-hot-100/values.yaml",),
        deployment="spotify-hot-100",
        container="web",
        baseline_cpu_mcores=50,
        cpu_request="30m",
        memory_request="128Mi",
        cpu_limit="500m",
        memory_limit="512Mi",
    ),
)


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = YAML_PARSER.load(path)
    if not isinstance(payload, dict):
        raise AssertionError(f"{path} must contain one YAML mapping")
    return payload


def _render(target: SizingTarget) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")
    command = [
        "helm",
        "template",
        target.release,
        target.chart,
        "--namespace",
        target.namespace,
    ]
    for value_file in target.value_files:
        command.extend(("-f", value_file))
    result = subprocess.run(
        command,
        check=True,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return [
        document
        for document in YAML_PARSER.load_all(result.stdout)
        if isinstance(document, dict) and document
    ]


def _deployment(
    documents: list[dict[str, Any]], name: str
) -> dict[str, Any]:
    matches = [
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == name
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one Deployment/{name}, found {len(matches)}"
        )
    return matches[0]


def _container(
    deployment: dict[str, Any], name: str
) -> dict[str, Any]:
    matches = [
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == name
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one container {name}, found {len(matches)}"
        )
    return matches[0]


def _mcores(value: str) -> int:
    if not value.endswith("m"):
        raise AssertionError(f"expected an integer millicore value, got {value!r}")
    return int(value[:-1])


class ManualSyncCpuRightsizingTests(unittest.TestCase):
    def test_rendered_requests_and_current_replicas_match(self) -> None:
        for target in TARGETS:
            with self.subTest(application=target.application):
                deployment = _deployment(_render(target), target.deployment)
                self.assertEqual(deployment["spec"]["replicas"], target.replicas)
                resources = _container(deployment, target.container)["resources"]
                self.assertEqual(
                    resources,
                    {
                        "requests": {
                            "cpu": target.cpu_request,
                            "memory": target.memory_request,
                        },
                        "limits": {
                            "cpu": target.cpu_limit,
                            "memory": target.memory_limit,
                        },
                    },
                )

    def test_every_target_requires_an_explicit_manual_sync(self) -> None:
        for target in TARGETS:
            with self.subTest(application=target.application):
                application = _load_yaml(
                    REPO_ROOT
                    / "argocd"
                    / "applications"
                    / f"{target.application}.yaml"
                )
                sync_policy = application["spec"].get("syncPolicy", {})
                self.assertIsNone(sync_policy.get("automated"))

    def test_historical_per_replica_cpu_reduction_is_exactly_140m(self) -> None:
        reduction = sum(
            target.baseline_cpu_mcores - _mcores(target.cpu_request)
            for target in TARGETS
        )
        self.assertEqual(reduction, 140)

    def test_spotify_chart_is_in_the_helm_kubeconform_matrix(self) -> None:
        workflow = _load_yaml(REPO_ROOT / ".github" / "workflows" / "ci.yaml")
        matrix = workflow["jobs"]["helm-and-kubeconform"]["strategy"]["matrix"][
            "chart"
        ]
        spotify = [entry for entry in matrix if entry["name"] == "spotify-hot-100"]
        self.assertEqual(
            spotify,
            [
                {
                    "name": "spotify-hot-100",
                    "path": "helm/spotify-hot-100",
                    "release": "spotify-hot-100",
                    "prod_values": "helm/spotify-hot-100/values.yaml",
                }
            ],
        )

    def test_companion_workloads_are_not_changed_by_the_slice(self) -> None:
        skyquiet = next(
            target for target in TARGETS if target.application == "skyquiet-server"
        )
        skyquiet_worker = _container(
            _deployment(_render(skyquiet), "skyquiet-server-worker"), "worker"
        )
        self.assertEqual(
            _deployment(_render(skyquiet), "skyquiet-server-worker")["spec"]["replicas"],
            0,
        )
        self.assertEqual(
            skyquiet_worker["resources"]["requests"],
            {"cpu": "50m", "memory": "128Mi"},
        )

        spotify = next(
            target for target in TARGETS if target.application == "spotify-hot-100"
        )
        refresh_jobs = [
            document
            for document in _render(spotify)
            if document.get("kind") == "CronJob"
        ]
        self.assertEqual(len(refresh_jobs), 1)
        refresh_resources = refresh_jobs[0]["spec"]["jobTemplate"]["spec"][
            "template"
        ]["spec"]["containers"][0]["resources"]
        self.assertEqual(
            refresh_resources["requests"],
            {"cpu": "50m", "memory": "128Mi"},
        )


if __name__ == "__main__":
    unittest.main()
