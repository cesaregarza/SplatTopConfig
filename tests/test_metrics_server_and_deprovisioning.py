from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(f"YAML mapping expected: {path}")
    return loaded


def _load_all_yaml(path: Path) -> list[dict[str, Any]]:
    return [
        doc
        for doc in YAML_PARSER.load_all(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc
    ]


def _find_doc(docs: list[dict[str, Any]], *, kind: str, name: str) -> dict[str, Any]:
    for doc in docs:
        metadata = doc.get("metadata", {})
        if doc.get("kind") == kind and metadata.get("name") == name:
            return doc
    raise AssertionError(f"{kind}/{name} not found")


class MetricsServerAndDeprovisioningTests(unittest.TestCase):
    def test_metrics_server_application_targets_kube_system(self) -> None:
        app = _load_yaml(REPO_ROOT / "argocd" / "applications" / "metrics-server.yaml")

        self.assertEqual(app["metadata"]["name"], "metrics-server")
        self.assertEqual(app["spec"]["source"]["path"], "infra/metrics-server")
        self.assertEqual(app["spec"]["destination"]["namespace"], "kube-system")
        self.assertEqual(
            app["spec"]["syncPolicy"]["automated"],
            {"prune": True, "selfHeal": True},
        )

    def test_project_allows_metrics_server_resources(self) -> None:
        project = _load_yaml(REPO_ROOT / "argocd" / "projects" / "splattop-project.yaml")

        destinations = {
            (destination["server"], destination["namespace"])
            for destination in project["spec"]["destinations"]
        }
        self.assertIn(("https://kubernetes.default.svc", "kube-system"), destinations)

        cluster_resources = {
            (resource["group"], resource["kind"])
            for resource in project["spec"]["clusterResourceWhitelist"]
        }
        self.assertIn(("apiregistration.k8s.io", "APIService"), cluster_resources)
        self.assertIn(("rbac.authorization.k8s.io", "ClusterRole"), cluster_resources)
        self.assertIn(("rbac.authorization.k8s.io", "ClusterRoleBinding"), cluster_resources)

    def test_metrics_server_manifest_is_pinned_and_doks_compatible(self) -> None:
        docs = _load_all_yaml(REPO_ROOT / "infra" / "metrics-server" / "components.yaml")
        deployment = _find_doc(docs, kind="Deployment", name="metrics-server")
        service = _find_doc(docs, kind="Service", name="metrics-server")
        api_service = _find_doc(docs, kind="APIService", name="v1beta1.metrics.k8s.io")

        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(deployment["spec"]["template"]["spec"]["nodeSelector"], {
            "kubernetes.io/os": "linux",
            "doks.digitalocean.com/node-pool": "pool-garz-ai",
        })
        self.assertEqual(
            container["image"],
            "registry.k8s.io/metrics-server/metrics-server:v0.8.1",
        )
        self.assertIn("--kubelet-insecure-tls", container["args"])
        self.assertIn(
            "--kubelet-preferred-address-types=InternalIP,ExternalIP,Hostname",
            container["args"],
        )
        self.assertEqual(service["metadata"]["namespace"], "kube-system")
        self.assertEqual(api_service["spec"]["service"]["namespace"], "kube-system")

    def test_teams_and_splatvote_applications_are_removed(self) -> None:
        removed_apps = (
            "splattop-teams-prod.yaml",
            "splattop-teams-secrets.yaml",
            "splatvote-prod.yaml",
            "splatvote-secrets.yaml",
        )
        for filename in removed_apps:
            self.assertFalse((REPO_ROOT / "argocd" / "applications" / filename).exists())


if __name__ == "__main__":
    unittest.main()
