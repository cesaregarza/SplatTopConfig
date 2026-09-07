from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "helm" / "citrus"
DEV_VALUES = CHART_PATH / "values-dev.yaml"
YAML_PARSER = YAML(typ="safe")
EXPECTED_COMMAND = (
    "exec gunicorn Mycore.wsgi:application --bind 0.0.0.0:8000 "
    "--worker-class gthread --workers 2 --threads 4 --timeout 60 "
    "--access-logfile -"
)


def _render_web_deployment(*, dev: bool) -> dict[str, Any]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")

    release = "citrus-dev" if dev else "citrus"
    command = [
        "helm",
        "template",
        release,
        str(CHART_PATH),
        "--namespace",
        release,
    ]
    if dev:
        command.extend(["-f", str(DEV_VALUES)])

    result = subprocess.run(
        command,
        check=True,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    deployments = [
        document
        for document in YAML_PARSER.load_all(result.stdout)
        if isinstance(document, dict)
        and document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == release
    ]
    if len(deployments) != 1:
        raise AssertionError(
            f"expected exactly one web Deployment/{release}, found {len(deployments)}"
        )
    return deployments[0]


class CitrusGunicornCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.deployments = {
            "prod": _render_web_deployment(dev=False),
            "dev": _render_web_deployment(dev=True),
        }

    def test_prod_and_dev_render_the_bounded_gthread_policy(self) -> None:
        for environment, deployment in self.deployments.items():
            with self.subTest(environment=environment):
                self.assertEqual(
                    deployment["spec"]["replicas"], 1 if environment == "dev" else 2
                )
                container = deployment["spec"]["template"]["spec"]["containers"][0]
                self.assertEqual(container["command"], ["sh", "-c"])
                self.assertEqual(container["args"], [EXPECTED_COMMAND])


if __name__ == "__main__":
    unittest.main()
