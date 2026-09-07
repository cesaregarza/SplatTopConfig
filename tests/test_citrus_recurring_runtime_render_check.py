from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.check_citrus_recurring_runtime_render import (
    ContractError,
    _active_dev_revision,
    run_contract,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "helm" / "citrus"


class CitrusRecurringRuntimeRenderCheckTests(unittest.TestCase):
    def test_active_revision_comes_from_the_effective_render(self) -> None:
        revision = "a" * 40
        rendered = "\n".join(
            (
                f'image: "registry.digitalocean.com/sendouq/citrus:{revision}"',
                f'image: "registry.digitalocean.com/sendouq/citrus:{revision}"',
                'image: "redis:7.2.4-alpine"',
            )
        )
        self.assertEqual(_active_dev_revision(rendered), revision)

    def test_active_revision_rejects_missing_malformed_or_mixed_images(self) -> None:
        cases = {
            "missing": 'image: "redis:7.2.4-alpine"',
            "malformed": ('image: "registry.digitalocean.com/sendouq/citrus:latest"'),
            "mixed": "\n".join(
                (
                    'image: "registry.digitalocean.com/sendouq/citrus:'
                    + "a" * 40
                    + '"',
                    'image: "registry.digitalocean.com/sendouq/citrus:'
                    + "b" * 40
                    + '"',
                )
            ),
        }
        for name, rendered in cases.items():
            with self.subTest(name=name), self.assertRaises(ContractError):
                _active_dev_revision(rendered)

    def test_checker_renders_the_complete_synthetic_matrix(self) -> None:
        helm = shutil.which("helm")
        if helm is None:
            self.skipTest("helm is required for chart render tests")
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "rendered"
            receipt = run_contract(
                chart=CHART_PATH,
                output_dir=output_dir,
                helm=helm,
                lint=False,
            )
            self.assertEqual(receipt["result"], "succeeded")
            self.assertEqual(receipt["render_count"], 7)
            self.assertEqual(
                {path.name for path in output_dir.glob("*.yaml")},
                {
                    "citrus-prod.yaml",
                    "citrus-dev.yaml",
                    "citrus-runtime-dev.yaml",
                    "citrus-payment-prod.yaml",
                    "citrus-payment-dev.yaml",
                    "citrus-payment-safety-prod.yaml",
                    "citrus-payment-safety-dev.yaml",
                },
            )

    def test_checker_rejects_a_nonempty_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "stale.yaml").write_text("stale\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "must be empty"):
                run_contract(
                    chart=CHART_PATH,
                    output_dir=output_dir,
                    helm="helm",
                    lint=False,
                )


if __name__ == "__main__":
    unittest.main()
