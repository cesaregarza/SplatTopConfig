from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
POOL = {"doks.digitalocean.com/node-pool": "pool-garz-memory"}


def render(chart: str, production: bool) -> list[dict]:
    command = ["helm", "template", "splattop-prod", str(ROOT / "helm" / chart)]
    if production:
        command += ["-f", str(ROOT / "helm" / chart / "values-prod.yaml")]
    elif chart == "garz-observability":
        command += ["--set", "monitoring.prometheus.enabled=true"]
    output = subprocess.run(command, check=True, capture_output=True, text=True).stdout
    return [doc for doc in YAML(typ="safe").load_all(output) if doc]


@unittest.skipUnless(shutil.which("helm"), "helm is required")
class MemoryPoolPlacementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = {
            (chart, prod): render(chart, prod)
            for chart in ("splattop", "garz-observability")
            for prod in (False, True)
        }

    def target(self, chart: str, prod: bool, suffix: str) -> dict:
        matches = [doc for doc in self.documents[chart, prod]
                   if doc["metadata"]["name"] == "splattop-prod-" + suffix
                   and doc["kind"] in ("Deployment", "StatefulSet")]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_only_selected_production_workloads_require_memory_pool(self) -> None:
        for chart, suffix in (("splattop", "celery-worker"),
                              ("garz-observability", "prometheus")):
            with self.subTest(chart=chart):
                target = self.target(chart, True, suffix)
                self.assertEqual(target["spec"]["template"]["spec"]["nodeSelector"], POOL)
                for doc in self.documents[chart, True]:
                    if doc is target or doc["kind"] not in ("Deployment", "StatefulSet", "Job"):
                        continue
                    selector = doc["spec"]["template"]["spec"].get("nodeSelector", {})
                    self.assertNotIn("doks.digitalocean.com/node-pool", selector)

    def test_defaults_remain_schedulable_without_a_memory_pool(self) -> None:
        for chart, suffix in (("splattop", "celery-worker"),
                              ("garz-observability", "prometheus")):
            with self.subTest(chart=chart):
                self.assertNotIn("nodeSelector", self.target(chart, False, suffix)["spec"]["template"]["spec"])

    def test_general_pool_preferences_allow_fallback(self) -> None:
        for chart, suffix in (("splattop", "fastapi"),):
            with self.subTest(chart=chart):
                pod = self.target(chart, True, suffix)["spec"]["template"]["spec"]
                self.assertNotIn("nodeSelector", pod)
                affinity = pod["affinity"]["nodeAffinity"]
                self.assertNotIn("requiredDuringSchedulingIgnoredDuringExecution", affinity)
                self.assertEqual(affinity["preferredDuringSchedulingIgnoredDuringExecution"], [{
                    "weight": 100,
                    "preference": {"matchExpressions": [{
                        "key": "doks.digitalocean.com/node-pool",
                        "operator": "In", "values": ["pool-garz-ai"],
                    }]},
                }])

    def test_small_monitoring_service_reserves_worker_surge_headroom(self) -> None:
        pod = self.target("garz-observability", True, "alertmanager")["spec"]["template"]["spec"]
        self.assertEqual(pod["affinity"]["nodeAffinity"], {
            "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{
                "matchExpressions": [{"key": "doks.digitalocean.com/node-pool",
                                      "operator": "In", "values": ["pool-garz-ai"]}],
            }]},
        })

    def test_worker_grace_changes_without_concurrency_or_command_changes(self) -> None:
        base = self.target("splattop", False, "celery-worker")["spec"]["template"]["spec"]
        prod = self.target("splattop", True, "celery-worker")["spec"]["template"]["spec"]
        self.assertEqual(base["terminationGracePeriodSeconds"], 30)
        self.assertEqual(prod["terminationGracePeriodSeconds"], 600)
        self.assertEqual(base["containers"][0]["command"], prod["containers"][0]["command"])

    def test_prometheus_keeps_its_existing_single_writer_volume(self) -> None:
        prometheus = self.target("garz-observability", True, "prometheus")
        self.assertEqual(prometheus["spec"]["replicas"], 1)
        claim = prometheus["spec"]["volumeClaimTemplates"][0]
        self.assertEqual(claim["metadata"]["name"], "prometheus-data")
        self.assertEqual(claim["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(claim["spec"]["resources"]["requests"]["storage"], "20Gi")
