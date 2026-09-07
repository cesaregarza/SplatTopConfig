from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from ruamel.yaml import YAML


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm" / "citrus"
HELM = os.environ.get(
    "HELM_BIN",
    "helm",
)
YAML_PARSER = YAML(typ="safe")


@unittest.skipUnless(shutil.which(HELM) or Path(HELM).exists(), "helm is required")
class CitrusRedisPersistenceTests(unittest.TestCase):
    def render(self, *values: str, expect_success: bool = True):
        command = [HELM, "template", "citrus", str(CHART)]
        for value in values:
            command.extend(["-f", str(CHART / value)])
        if "values-dev.yaml" in values:
            command[2] = "citrus-dev"
            command.extend(["--namespace", "citrus-dev"])
            for value in ("values-payment-dev.yaml", "values-recurring-dev.yaml"):
                if value not in values:
                    command.extend(["-f", str(CHART / value)])
        result = subprocess.run(command, capture_output=True, text=True)
        if expect_success:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result, [doc for doc in YAML_PARSER.load_all(result.stdout) if doc]

    @staticmethod
    def redis_resources(documents):
        deployment = next(
            doc
            for doc in documents
            if doc.get("kind") == "Deployment"
            and doc.get("metadata", {}).get("name") == "citrus-redis"
        )
        pvc = next(
            (doc for doc in documents if doc.get("kind") == "PersistentVolumeClaim"),
            None,
        )
        return deployment, pvc

    def test_production_and_development_enable_the_same_retained_redis_pvc(self):
        for values in ((), ("values-dev.yaml",)):
            with self.subTest(values=values or ("base",)):
                _, documents = self.render(*values)
                deployment, pvc = self.redis_resources(documents)
                pod = deployment["spec"]["template"]["spec"]
                self.assertEqual(deployment["spec"]["replicas"], 1)
                self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
                self.assertEqual(pod["nodeSelector"], {
                    "doks.digitalocean.com/node-pool": "pool-garz-memory",
                })
                self.assertEqual(pod["securityContext"]["fsGroup"], 999)
                self.assertEqual(pod["securityContext"]["fsGroupChangePolicy"], "OnRootMismatch")
                self.assertIsNotNone(pvc)
                self.assertEqual(pvc["metadata"]["name"], "citrus-redis-data")
                self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "1Gi")
                self.assertEqual(pvc["spec"]["storageClassName"], "do-block-storage-retain")
                self.assertEqual(pvc["metadata"]["annotations"], {
                    "argocd.argoproj.io/sync-options": "Prune=false,Delete=false",
                    "helm.sh/resource-policy": "keep",
                })

    def test_pvc_and_mount_are_the_same_claim(self):
        _, documents = self.render()
        deployment, pvc = self.redis_resources(documents)
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        mount = container["volumeMounts"][0]
        volume = deployment["spec"]["template"]["spec"]["volumes"][0]
        self.assertEqual(mount, {"name": "redis-data", "mountPath": "/data"})
        self.assertEqual(volume["name"], mount["name"])
        self.assertEqual(volume["persistentVolumeClaim"]["claimName"], pvc["metadata"]["name"])

    def test_persistence_disabled_omits_storage_specific_resources(self):
        result = subprocess.run(
            [HELM, "template", "citrus", str(CHART), "--set", "redis.persistence.enabled=false"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = [doc for doc in YAML_PARSER.load_all(result.stdout) if doc]
        deployment, pvc = self.redis_resources(documents)
        pod = deployment["spec"]["template"]["spec"]
        self.assertIsNone(pvc)
        self.assertNotIn("strategy", deployment["spec"])
        self.assertNotIn("volumes", pod)
        self.assertNotIn("volumeMounts", pod["containers"][0])
        self.assertNotIn("fsGroup", pod.get("securityContext", {}))

    def test_persistent_redis_rejects_multiple_replicas(self):
        result = subprocess.run(
            [
                HELM,
                "template",
                "citrus",
                str(CHART),
                "--set",
                "redis.replicas=2",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("redis.persistence.enabled requires redis.replicas", result.stderr)


if __name__ == "__main__":
    unittest.main()
