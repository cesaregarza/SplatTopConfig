from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

from ruamel.yaml import YAML


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm/garz-observability"
YAML_PARSER = YAML(typ="safe")


def render(template: str, *overrides: str) -> dict[str, dict]:
    result = subprocess.run(
        [
            "helm", "template", "garz-observability", str(CHART),
            "--namespace", "monitoring", "-f", str(CHART / "values-prod.yaml"),
            "--show-only", f"templates/{template}", *overrides,
        ],
        check=True, capture_output=True, text=True, cwd=ROOT,
    )
    return {
        item["metadata"]["name"]: item
        for item in YAML_PARSER.load_all(result.stdout) if item
    }


@unittest.skipUnless(shutil.which("helm"), "helm is required")
class CitrusMetricsScrapePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.enabled = render("monitoring-networkpolicies.yaml")
        cls.disabled = render(
            "monitoring-networkpolicies.yaml", "--set",
            "monitoring.networkPolicies.prometheus.citrusDevMetrics=false",
        )

    def test_only_one_namespace_pod_and_port_flow_is_added(self) -> None:
        enabled = self.enabled["prometheus-ingress-egress"]["spec"]
        disabled = self.disabled["prometheus-ingress-egress"]["spec"]
        self.assertEqual(
            enabled["podSelector"],
            {"matchLabels": {"app.kubernetes.io/component": "prometheus"}},
        )
        self.assertEqual(enabled["ingress"], disabled["ingress"])
        added = [rule for rule in enabled["egress"] if rule not in disabled["egress"]]
        port = YAML_PARSER.load((ROOT / "helm/citrus/values.yaml").read_text())[
            "billingWorker"
        ]["metrics"]["port"]
        self.assertEqual(added, [{
            "to": [{
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "citrus-dev"}
                },
                "podSelector": {"matchLabels": {"app": "citrus-billing-worker"}},
            }],
            "ports": [{"protocol": "TCP", "port": port}],
        }])
        self.assertEqual(
            [rule for rule in enabled["egress"] if rule not in added],
            disabled["egress"],
        )

    def test_other_network_policies_and_default_opt_in_are_unchanged(self) -> None:
        self.assertEqual(self.enabled.keys(), self.disabled.keys())
        for name in self.enabled.keys() - {"prometheus-ingress-egress"}:
            with self.subTest(policy=name):
                self.assertEqual(self.enabled[name], self.disabled[name])
        defaults = YAML_PARSER.load((CHART / "values.yaml").read_text())
        self.assertFalse(defaults["monitoring"]["networkPolicies"]["prometheus"]["citrusDevMetrics"])

    def test_pod_scrape_retains_labels_used_by_recurring_alerts(self) -> None:
        configmap = render("monitoring-prometheus-configmap.yaml")["prometheus-config"]
        config = YAML_PARSER.load(configmap["data"]["prometheus.yml"])
        job = next(job for job in config["scrape_configs"] if job["job_name"] == "kubernetes-pods")
        labels = {rule.get("target_label"): rule for rule in job["relabel_configs"]}
        for label, source in (
            ("namespace", "__meta_kubernetes_namespace"),
            ("pod", "__meta_kubernetes_pod_name"),
        ):
            self.assertEqual(labels[label]["source_labels"], [source])
            self.assertEqual(labels[label]["action"], "replace")


if __name__ == "__main__":
    unittest.main()
