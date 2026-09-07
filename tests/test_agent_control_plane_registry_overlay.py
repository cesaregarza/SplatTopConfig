from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from scripts.check_agent_control_plane_registry_overlay_render import (
    render_registry_overlay_application,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_OVERLAY_DIR = REPO_ROOT / "apps" / "agent-control-plane-registry-overlay"
REGISTRY_OVERLAY_CONFIGMAP_NAME = "agent-control-plane-registry-overlay"
REGISTRY_OVERLAY_RESTART_HOOK_PATH = (
    REGISTRY_OVERLAY_DIR / "templates" / "restart-hook.yaml"
)
REGISTRY_OVERLAY_ROLLOUT_STRATEGY_HOOK_PATH = (
    REGISTRY_OVERLAY_DIR / "templates" / "rollout-strategy-hook.yaml"
)
REGISTRY_OVERLAY_RESTART_RBAC_PATH = (
    REGISTRY_OVERLAY_DIR / "templates" / "restart-rbac.yaml"
)
REGISTRY_OVERLAY_RESTART_ORDER = [
    "agent-control-plane",
    "agent-control-plane-model-gateway",
    "agent-control-plane-callback-adapter",
    "agent-control-plane-git-deliverer",
    "agent-control-plane-local-worker",
]
REGISTRY_OVERLAY_ROLLING_DEPLOYMENTS = [
    "agent-control-plane",
    "agent-control-plane-callback-adapter",
    "agent-control-plane-git-deliverer",
    "agent-control-plane-local-worker",
]
REGISTRY_OVERLAY_COMPONENTS = {
    "agent-control-plane": "api",
    "agent-control-plane-model-gateway": "model-gateway",
    "agent-control-plane-callback-adapter": "callback-adapter",
    "agent-control-plane-git-deliverer": "git-deliverer",
    "agent-control-plane-local-worker": "local-worker",
}
SKILL_BUNDLE_DIR = REPO_ROOT / "apps" / "agent-control-plane-skills"
YAML_PARSER = YAML(typ="safe")
SYNTHETIC_LIVE_VERIFY_MIN_SETTLEMENT_MARGIN_SECONDS = 30
SYNTHETIC_LIVE_VERIFY_JOB_STARTUP_HEADROOM_SECONDS = 30


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text())
    if not isinstance(loaded, dict):
        raise AssertionError(f"YAML mapping expected: {path}")
    return loaded


def _load_yaml_documents(path: Path) -> list[dict[str, Any]]:
    loaded = [
        document
        for document in YAML_PARSER.load_all(path.read_text())
        if isinstance(document, dict)
    ]
    if not loaded:
        raise AssertionError(f"YAML documents expected: {path}")
    return loaded


def _load_registry_overlay_data() -> dict[str, str]:
    legacy_configmap_path = REGISTRY_OVERLAY_DIR / "configmap.yaml"
    if legacy_configmap_path.exists():
        configmap = _load_yaml(legacy_configmap_path)
        return configmap["data"]

    kustomization = _load_yaml(REGISTRY_OVERLAY_DIR / "kustomization.yaml")
    generators = kustomization.get("configMapGenerator") or []
    generator = next(
        item
        for item in generators
        if isinstance(item, dict)
        and item.get("name") == REGISTRY_OVERLAY_CONFIGMAP_NAME
    )
    data: dict[str, str] = {}
    for file_spec in generator["files"]:
        key, relative_path = file_spec.split("=", 1)
        data[key] = (REGISTRY_OVERLAY_DIR / relative_path).read_text()
    return data


class AgentControlPlaneRegistryOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = _load_registry_overlay_data()
        cls.control_plane_values = _load_yaml(
            REPO_ROOT / "apps" / "agent-control-plane" / "values.yaml"
        )
        cls.control_plane_secret = _load_yaml(
            REPO_ROOT
            / "secrets"
            / "agent-control-plane"
            / "runtime-secret.enc.yaml"
        )
        cls.agent_workloads_values = _load_yaml(
            REPO_ROOT / "apps" / "agent-workloads" / "values.yaml"
        )
        cls.control_plane_application = _load_yaml(
            REPO_ROOT / "argocd" / "applications" / "agent-control-plane.yaml"
        )
        cls.registry_overlay_application = _load_yaml(
            REPO_ROOT
            / "argocd"
            / "applications"
            / "agent-control-plane-registry-overlay.yaml"
        )
        cls.agent_workloads_application = _load_yaml(
            REPO_ROOT / "argocd" / "applications" / "agent-workloads.yaml"
        )
        cls.control_plane_skills_application = _load_yaml(
            REPO_ROOT / "argocd" / "applications" / "agent-control-plane-skills.yaml"
        )
        cls.control_plane_skills_kustomization = _load_yaml(
            SKILL_BUNDLE_DIR / "kustomization.yaml"
        )
        cls.control_plane_skills_configmap = _load_yaml(
            SKILL_BUNDLE_DIR / "configmap.yaml"
        )
        cls.control_plane_skills_rbac = _load_yaml_documents(
            SKILL_BUNDLE_DIR / "materialize-rbac.yaml"
        )
        cls.control_plane_skills_job = _load_yaml(
            SKILL_BUNDLE_DIR / "materialize-job.yaml"
        )
        cls.control_plane_skills_cronjob = _load_yaml(
            SKILL_BUNDLE_DIR / "materialize-cronjob.yaml"
        )
        cls.splattop_project = _load_yaml(
            REPO_ROOT / "argocd" / "projects" / "splattop-project.yaml"
        )
        cls.registry_overlay_restart_hook = _load_yaml(
            REGISTRY_OVERLAY_RESTART_HOOK_PATH
        )
        cls.registry_overlay_rollout_strategy_hook = _load_yaml(
            REGISTRY_OVERLAY_ROLLOUT_STRATEGY_HOOK_PATH
        )
        cls.registry_overlay_restart_rbac = _load_yaml_documents(
            REGISTRY_OVERLAY_RESTART_RBAC_PATH
        )
        cls.model_gateway_controls = _load_yaml(
            REPO_ROOT
            / "apps"
            / "agent-control-plane-runtime-controls"
            / "configmap.yaml"
        )

    def test_registry_overlay_is_authored_as_single_source_helm_chart(self) -> None:
        kustomization = _load_yaml(REGISTRY_OVERLAY_DIR / "kustomization.yaml")
        self.assertFalse((REGISTRY_OVERLAY_DIR / "configmap.yaml").exists())
        chart = _load_yaml(REGISTRY_OVERLAY_DIR / "Chart.yaml")
        self.assertEqual(chart["name"], "agent-control-plane-registry-overlay")
        self.assertEqual(kustomization["kind"], "Kustomization")
        self.assertEqual(
            kustomization["resources"], ["templates/restart-rbac.yaml"]
        )
        self.assertTrue(REGISTRY_OVERLAY_RESTART_HOOK_PATH.exists())
        self.assertTrue(REGISTRY_OVERLAY_ROLLOUT_STRATEGY_HOOK_PATH.exists())
        self.assertTrue(REGISTRY_OVERLAY_RESTART_RBAC_PATH.exists())
        self.assertFalse(any((REGISTRY_OVERLAY_DIR / "hooks").glob("*.yaml")))
        self.assertTrue(kustomization["generatorOptions"]["disableNameSuffixHash"])

        generator = next(
            item
            for item in kustomization["configMapGenerator"]
            if item["name"] == REGISTRY_OVERLAY_CONFIGMAP_NAME
        )
        self.assertEqual(
            set(self.data),
            {
                "workload_imports.yaml",
                "policy.prod.yaml",
                "evals.yaml",
                "agent-data.workspace_probe.json",
                "agent-opencode.proposer.json",
                "agent-opencode.apply_executor.json",
                "opencode_proposer_smoke.jsonl",
                "opencode_apply_smoke.jsonl",
            },
        )
        self.assertEqual(len(generator["files"]), len(self.data))

    def test_readonly_query_skills_sync_from_published_bundle_consumer(self) -> None:
        imports = YAML_PARSER.load(self.data["workload_imports.yaml"])
        imports_by_id = {entry["id"]: entry for entry in imports["imports"]}
        readonly_query = imports_by_id["data.workspace_probe"]["capabilities"][
            "agent_workloads.readonly_query"
        ]
        self.assertEqual(
            readonly_query["skills"],
            ["xscraper-schema", "xscraper-glossary"],
        )
        self.assertEqual(readonly_query["broker_bounds"]["max_runtime_seconds"], 120)
        self.assertEqual(readonly_query["broker_bounds"]["statement_timeout_ms"], 20000)

        skills = self.control_plane_values["skills"]
        self.assertEqual(
            skills,
            {
                "enabled": True,
                "configMapName": "mandate-skill-packs",
                "mountPath": "/var/lib/mandate/skills",
            },
        )

        self.assertFalse(
            (
                REPO_ROOT / "argocd" / "applications" / "agent-workloads-skills.yaml"
            ).exists()
        )
        skills_app = self.control_plane_skills_application
        self.assertEqual(skills_app["spec"]["project"], "splattop")
        self.assertEqual(
            skills_app["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"],
            "8",
        )
        self.assertEqual(
            skills_app["spec"]["source"],
            {
                "repoURL": "https://github.com/cesaregarza/GarzAICluster",
                "targetRevision": "main",
                "path": "apps/agent-control-plane-skills",
            },
        )
        self.assertEqual(
            skills_app["spec"]["destination"]["namespace"],
            "agent-control-plane",
        )
        self.assertEqual(
            skills_app["spec"]["syncPolicy"]["automated"],
            {"prune": True, "selfHeal": True},
        )
        self.assertEqual(
            skills_app["spec"]["ignoreDifferences"],
            [
                {
                    "kind": "ConfigMap",
                    "name": "mandate-skill-packs",
                    "namespace": "agent-control-plane",
                    "jsonPointers": ["/data"],
                }
            ],
        )
        self.assertNotIn(
            "https://github.com/cesaregarza/agent-workloads",
            self.splattop_project["spec"]["sourceRepos"],
        )

        self.assertEqual(
            set(self.control_plane_skills_kustomization["resources"]),
            {
                "configmap.yaml",
                "materialize-rbac.yaml",
                "materialize-job.yaml",
                "materialize-cronjob.yaml",
            },
        )
        self.assertEqual(
            self.control_plane_skills_configmap["metadata"]["name"],
            "mandate-skill-packs",
        )
        self.assertEqual(
            self.control_plane_skills_configmap["data"],
            {},
        )
        self.assertEqual(
            self.control_plane_skills_configmap["metadata"]["labels"]["garz.ai/source"],
            "ci-published-skill-bundle",
        )
        self.assertFalse(any((SKILL_BUNDLE_DIR / "bundle").glob("*")))

        role = next(
            document
            for document in self.control_plane_skills_rbac
            if document["kind"] == "Role"
        )
        self.assertEqual(role["rules"], [
            {
                "apiGroups": [""],
                "resources": ["configmaps"],
                "resourceNames": ["mandate-skill-packs"],
                "verbs": ["get", "patch", "update"],
            }
        ])
        self.assertNotIn("create", role["rules"][0]["verbs"])
        self.assertNotIn("delete", role["rules"][0]["verbs"])

        self._assert_skill_bundle_materializer_pod(
            self.control_plane_skills_job["spec"]["template"]["spec"]
        )
        cron_spec = self.control_plane_skills_cronjob["spec"]
        self.assertEqual(cron_spec["schedule"], "*/5 * * * *")
        self.assertEqual(cron_spec["concurrencyPolicy"], "Forbid")
        self._assert_skill_bundle_materializer_pod(
            cron_spec["jobTemplate"]["spec"]["template"]["spec"]
        )

    def _assert_skill_bundle_materializer_pod(self, pod_spec: dict[str, Any]) -> None:
        self.assertEqual(pod_spec["serviceAccountName"], "mandate-skill-bundle-sync")
        self.assertEqual(pod_spec["imagePullSecrets"], [{"name": "regcred"}])
        init_container = pod_spec["initContainers"][0]
        self.assertEqual(init_container["name"], "bundle")
        self.assertEqual(
            init_container["image"],
            "registry.digitalocean.com/sendouq/agent-workloads-skills:main",
        )
        self.assertEqual(init_container["imagePullPolicy"], "Always")

        materializer = pod_spec["containers"][0]
        script = materializer["command"][-1]
        self.assertIn("sha256sum -c SHA256SUMS", script)
        self.assertIn("agent-control-plane-skill-bundle.v1", script)
        self.assertIn("create configmap mandate-skill-packs", script)
        self.assertIn("--dry-run=client -o 'jsonpath={.data}'", script)
        self.assertIn('"op":"add","path":"/data"', script)
        self.assertIn("--type=json --patch-file=/tmp/skill-data-patch.json", script)
        self.assertNotIn("--server-side", script)
        self.assertNotIn("--force-conflicts", script)
        self.assertIn("--field-manager=mandate-skill-bundle-sync", script)
        self.assertNotIn("kubectl create -f", script)

    def test_registry_overlay_restart_hook_is_fail_closed_and_least_privilege(
        self,
    ) -> None:
        annotations = self.registry_overlay_restart_hook["metadata"]["annotations"]
        self.assertEqual(annotations["argocd.argoproj.io/hook"], "PostSync")
        self.assertEqual(
            annotations["argocd.argoproj.io/hook-delete-policy"],
            "HookSucceeded",
        )
        self.assertEqual(
            self.registry_overlay_restart_hook["metadata"]["generateName"],
            "registry-overlay-restart-",
        )
        self.assertNotIn("name", self.registry_overlay_restart_hook["metadata"])
        self.assertEqual(self.registry_overlay_restart_hook["spec"]["backoffLimit"], 0)
        self.assertEqual(
            self.registry_overlay_restart_hook["spec"]["ttlSecondsAfterFinished"],
            86400,
        )
        self.assertEqual(
            self.registry_overlay_restart_hook["spec"]["activeDeadlineSeconds"],
            1500,
        )

        pod_spec = self.registry_overlay_restart_hook["spec"]["template"]["spec"]
        restart_container = pod_spec["containers"][0]
        self.assertEqual(
            restart_container["env"],
            [
                {"name": "HOME", "value": "/tmp"},
                {
                    "name": "KUBERNETES_SERVICE_HOST",
                    "value": "kubernetes.default.svc",
                },
                {"name": "KUBERNETES_SERVICE_PORT", "value": "443"},
            ],
        )
        self.assertEqual(
            restart_container["volumeMounts"],
            [{"name": "tmp", "mountPath": "/tmp"}],
        )
        self.assertEqual(pod_spec["volumes"], [{"name": "tmp", "emptyDir": {}}])

        restart_script = restart_container["command"][-1]
        self.assertEqual(
            restart_script.split('deployments="', 1)[1].split('"', 1)[0].split(),
            REGISTRY_OVERLAY_RESTART_ORDER,
        )
        self.assertNotIn("rollout status", restart_script)
        self.assertIn('get "deployment/$deployment"', restart_script)
        restart_role = next(
            document
            for document in self.registry_overlay_restart_rbac
            if document["kind"] == "Role"
        )
        self.assertEqual(
            restart_role["rules"],
            [
                {
                    "apiGroups": ["apps"],
                    "resources": ["deployments"],
                    "resourceNames": REGISTRY_OVERLAY_RESTART_ORDER,
                    "verbs": ["get", "patch"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["pods"],
                    "verbs": ["list"],
                },
            ],
        )

    def test_registry_overlay_application_is_one_indivisible_helm_source(self) -> None:
        self.assertNotIn("sources", self.registry_overlay_application["spec"])
        self.assertEqual(
            self.registry_overlay_application["spec"]["source"],
            {
                "repoURL": "https://github.com/cesaregarza/GarzAICluster",
                "targetRevision": "main",
                "path": "apps/agent-control-plane-registry-overlay",
                "helm": {"releaseName": "agent-control-plane-registry-overlay"},
            },
        )

        self.assertEqual(
            self.registry_overlay_application["spec"]["syncPolicy"]["automated"],
            {"prune": True, "selfHeal": True},
        )
        sync_options = set(
            self.registry_overlay_application["spec"]["syncPolicy"].get(
                "syncOptions", []
            )
        )
        self.assertIn("CreateNamespace=true", sync_options)
        self.assertNotIn("ApplyOutOfSyncOnly=true", sync_options)
        self.assertNotIn(
            "automated",
            self.agent_workloads_application["spec"]["syncPolicy"],
            "workloads must stay manual until a real overlay-first dependency exists",
        )
        self.assertNotIn(
            "syncWindows",
            self.splattop_project["spec"],
            "per-Application sync policy must be the only deployment gate",
        )

    def test_complete_application_render_contains_strategy_and_restart_hooks(
        self,
    ) -> None:
        helm = shutil.which("helm")
        if helm is None:
            self.skipTest("helm is required for complete Application render test")

        documents = render_registry_overlay_application(
            repo_root=REPO_ROOT,
            helm=helm,
        )
        hooks = {
            document.get("metadata", {}).get("generateName"): document
            for document in documents
            if document.get("kind") == "Job"
        }
        self.assertEqual(len(documents), 6)
        self.assertEqual(
            set(hooks),
            {
                "registry-overlay-rollout-strategy-",
                "registry-overlay-restart-",
            },
        )
        self.assertEqual(
            hooks["registry-overlay-rollout-strategy-"]["metadata"]["annotations"],
            {
                "argocd.argoproj.io/hook": "Sync",
                "argocd.argoproj.io/sync-wave": "1",
                "argocd.argoproj.io/hook-delete-policy": "HookSucceeded",
            },
        )
        self.assertEqual(
            hooks["registry-overlay-restart-"]["metadata"]["annotations"][
                "argocd.argoproj.io/hook"
            ],
            "PostSync",
        )

    def test_rollout_strategy_hook_prepares_capacity_safe_singleton_restarts(
        self,
    ) -> None:
        hook = self.registry_overlay_rollout_strategy_hook
        self.assertEqual(
            hook["metadata"]["generateName"],
            "registry-overlay-rollout-strategy-",
        )
        self.assertNotIn("name", hook["metadata"])
        self.assertEqual(
            hook["metadata"]["annotations"],
            {
                "argocd.argoproj.io/hook": "Sync",
                "argocd.argoproj.io/sync-wave": "1",
                "argocd.argoproj.io/hook-delete-policy": "HookSucceeded",
            },
        )
        self.assertEqual(hook["spec"]["backoffLimit"], 0)
        self.assertEqual(hook["spec"]["ttlSecondsAfterFinished"], 86400)
        self.assertEqual(hook["spec"]["activeDeadlineSeconds"], 300)

        pod_spec = hook["spec"]["template"]["spec"]
        self.assertEqual(pod_spec["serviceAccountName"], "registry-overlay-restart")
        self.assertEqual(pod_spec["restartPolicy"], "Never")
        container = pod_spec["containers"][0]
        self.assertEqual(container["name"], "rollout-strategy")
        self.assertEqual(
            container["image"],
            "alpine/k8s:1.33.4@sha256:"
            "b0523f0a244ddc4c8e055aa335c040d3d78b3ead5528f4544395f7f9f69c7b68",
        )
        script = container["command"][-1]
        self.assertEqual(container["command"], ["/bin/sh", "-ec", script])
        self.assertEqual(
            container["securityContext"],
            {
                "runAsNonRoot": True,
                "runAsUser": 65534,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
        )
        self.assertEqual(
            container["resources"],
            {
                "requests": {"cpu": "10m", "memory": "32Mi"},
                "limits": {"cpu": "100m", "memory": "64Mi"},
            },
        )
        self.assertEqual(
            script.split('rolling_deployments="', 1)[1]
            .split('"', 1)[0]
            .split(),
            REGISTRY_OVERLAY_ROLLING_DEPLOYMENTS,
        )
        self.assertIn(
            'strategy_patch=\'{"spec":{"strategy":{"type":"RollingUpdate",'
            '"rollingUpdate":{"maxSurge":0,"maxUnavailable":1}}}}\'',
            script,
        )
        self.assertIn(
            'gateway_deployment="agent-control-plane-model-gateway"',
            script,
        )
        self.assertIn(
            "expected=<generation>|<same-generation>|1|1|1|1|1|Recreate",
            script,
        )
        self.assertNotIn("rollout restart", script)

        self.assertNotIn("ignoreDifferences", self.control_plane_application["spec"])
        self.assertNotIn(
            "RespectIgnoreDifferences=true",
            self.control_plane_application["spec"]["syncPolicy"]["syncOptions"],
        )

        result, calls = self._run_rollout_strategy_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        for deployment in REGISTRY_OVERLAY_ROLLING_DEPLOYMENTS:
            self.assertTrue(
                any(
                    f"patch deployment/{deployment}" in call
                    and '"maxSurge":0,"maxUnavailable":1' in call
                    for call in calls
                ),
                deployment,
            )
        self.assertFalse(
            any(
                "patch deployment/agent-control-plane-model-gateway" in call
                for call in calls
            )
        )
        self.assertIn(
            "phase=complete result=succeeded rolling_deployments=4 "
            "gateway_strategy=Recreate",
            result.stdout,
        )

    def test_rollout_strategy_hook_fails_before_patch_if_rwo_gateway_drifts(
        self,
    ) -> None:
        result, calls = self._run_rollout_strategy_script(
            gateway_state="7|7|1|1|1|1|1|RollingUpdate"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reason=unsafe-rwo-state", result.stderr)
        self.assertFalse(any(" patch " in f" {call} " for call in calls))

    def test_rollout_strategy_hook_skips_confirmed_absent_optional_callback(
        self,
    ) -> None:
        optional = "agent-control-plane-callback-adapter"
        result, calls = self._run_rollout_strategy_script(
            absent_deployment=optional,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"deployment={optional} phase=presence result=skipped "
            "reason=optional-deployment-absent",
            result.stdout,
        )
        self.assertFalse(
            any(optional in call and " patch " in f" {call} " for call in calls)
        )
        self.assertIn(
            "phase=complete result=succeeded rolling_deployments=3",
            result.stdout,
        )

    def test_rollout_strategy_hook_fails_on_absent_required_deployment(self) -> None:
        required = "agent-control-plane"
        result, calls = self._run_rollout_strategy_script(
            absent_deployment=required,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"deployment={required} phase=presence result=failed "
            "reason=deployment-not-found",
            result.stderr,
        )
        self.assertFalse(any(" patch " in f" {call} " for call in calls))

    def test_rollout_strategy_hook_fails_on_optional_callback_api_error(self) -> None:
        optional = "agent-control-plane-callback-adapter"
        result, calls = self._run_rollout_strategy_script(
            fail_match=f"get deployment/{optional} --ignore-not-found",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"deployment={optional} phase=presence result=failed "
            "kubectl_exit_code=70",
            result.stderr,
        )
        self.assertFalse(any(" patch " in f" {call} " for call in calls))

    def test_rollout_strategy_hook_keeps_present_callback_checks_and_patch(self) -> None:
        optional = "agent-control-plane-callback-adapter"
        result, calls = self._run_rollout_strategy_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"-n agent-control-plane get deployment/{optional} "
            "--ignore-not-found -o name",
            calls,
        )
        callback_calls = [call for call in calls if optional in call]
        self.assertTrue(any("get deployment/" + optional in call for call in callback_calls))
        self.assertTrue(any("patch deployment/" + optional in call for call in callback_calls))
        self.assertTrue(any("jsonpath={.spec.strategy.type}" in call for call in callback_calls))

    def test_rollout_strategy_hook_rejects_unsettled_target_before_any_patch(
        self,
    ) -> None:
        result, calls = self._run_rollout_strategy_script(
            rolling_state="8|7|1|2|1|1|1"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reason=deployment-not-settled", result.stderr)
        self.assertFalse(any(" patch " in f" {call} " for call in calls))

    def test_rollout_strategy_hook_rejects_non_singleton_before_any_patch(
        self,
    ) -> None:
        result, calls = self._run_rollout_strategy_script(
            rolling_state="7|7|2|2|2|2|2"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reason=deployment-not-settled", result.stderr)
        self.assertFalse(any(" patch " in f" {call} " for call in calls))

    def test_rollout_strategy_hook_stops_on_inspection_error(self) -> None:
        result, calls = self._run_rollout_strategy_script(
            fail_match="get deployment/agent-control-plane -o jsonpath=",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "deployment=agent-control-plane phase=inspect result=failed "
            "kubectl_exit_code=70",
            result.stderr,
        )
        self.assertFalse(any(" patch " in f" {call} " for call in calls))

    def test_rollout_strategy_hook_stops_before_later_targets_on_patch_failure(
        self,
    ) -> None:
        failed_deployment = "agent-control-plane-callback-adapter"
        result, calls = self._run_rollout_strategy_script(
            fail_match=f"patch deployment/{failed_deployment}",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"deployment={failed_deployment} phase=patch result=failed",
            result.stderr,
        )
        self.assertFalse(
            any(
                "deployment/agent-control-plane-git-deliverer" in call
                and " patch " in f" {call} "
                for call in calls
            )
        )

    def test_rollout_strategy_hook_fails_on_post_patch_verify_mismatch(
        self,
    ) -> None:
        result, calls = self._run_rollout_strategy_script(
            strategy_state="RollingUpdate|1|0",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("phase=verify result=failed", result.stderr)
        patch_calls = [call for call in calls if " patch " in f" {call} "]
        self.assertEqual(len(patch_calls), 1)

    def _run_rollout_strategy_script(
        self,
        *,
        rolling_state: str = "7|7|1|1|1|1|1",
        gateway_state: str = "7|7|1|1|1|1|1|Recreate",
        strategy_state: str = "RollingUpdate|0|1",
        fail_match: str = "",
        absent_deployment: str = "",
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        script = self.registry_overlay_rollout_strategy_hook["spec"]["template"]["spec"][
            "containers"
        ][0]["command"][-1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            kubectl_log = tmp_path / "kubectl.log"
            fake_kubectl = tmp_path / "kubectl"
            fake_kubectl.write_text(
                """#!/bin/sh
printf '%s\\n' "$*" >> "$KUBECTL_LOG"
if [ -n "$FAIL_MATCH" ]; then
  case "$*" in
    *"$FAIL_MATCH"*) exit 70 ;;
  esac
fi
case "$3" in
  get)
    if [ "$5" = "--ignore-not-found" ] && [ "$7" = "name" ]; then
      deployment="${4#deployment/}"
      if [ "$deployment" = "$ABSENT_DEPLOYMENT" ]; then
        exit 0
      fi
      printf 'deployment.apps/%s' "$deployment"
      exit 0
    fi
    case "$6" in
      'jsonpath={.metadata.generation}|{.status.observedGeneration}|{.spec.replicas}|{.status.replicas}|{.status.updatedReplicas}|{.status.readyReplicas}|{.status.availableReplicas}')
        printf '%s' "$ROLLING_STATE"
        ;;
      'jsonpath={.metadata.generation}|{.status.observedGeneration}|{.spec.replicas}|{.status.replicas}|{.status.updatedReplicas}|{.status.readyReplicas}|{.status.availableReplicas}|{.spec.strategy.type}')
        printf '%s' "$GATEWAY_STATE"
        ;;
      'jsonpath={.spec.strategy.type}|{.spec.strategy.rollingUpdate.maxSurge}|{.spec.strategy.rollingUpdate.maxUnavailable}')
        printf '%s' "$STRATEGY_STATE"
        ;;
      *)
        exit 64
        ;;
    esac
    ;;
  patch)
    printf 'patched\\n'
    ;;
  *)
    exit 65
    ;;
esac
""",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{tmp_path}:{env['PATH']}",
                    "KUBECTL_LOG": str(kubectl_log),
                    "ROLLING_STATE": rolling_state,
                    "GATEWAY_STATE": gateway_state,
                    "STRATEGY_STATE": strategy_state,
                    "FAIL_MATCH": fail_match,
                    "ABSENT_DEPLOYMENT": absent_deployment,
                    "API_TIMEOUT_SECONDS": "1",
                }
            )
            result = subprocess.run(
                ["/bin/sh", "-ec", script],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            calls = (
                kubectl_log.read_text(encoding="utf-8").splitlines()
                if kubectl_log.exists()
                else []
            )
        return result, calls

    def test_restart_script_waits_for_each_deployment_before_restarting_next(
        self,
    ) -> None:
        restart_script = self.registry_overlay_restart_hook["spec"]["template"]["spec"][
            "containers"
        ][0]["command"][-1]
        result, calls = self._run_restart_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        expected_calls = []
        for deployment in REGISTRY_OVERLAY_RESTART_ORDER:
            component = REGISTRY_OVERLAY_COMPONENTS[deployment]
            presence = (
                "-n agent-control-plane get "
                f"deployment/{deployment} --ignore-not-found -o name"
            )
            pod_list = (
                "-n agent-control-plane get pods -l "
                "app.kubernetes.io/name=agent-control-plane,"
                "app.kubernetes.io/instance=agent-control-plane,"
                f"app.kubernetes.io/component={component} -o "
                'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}'
            )
            expected_calls.extend(
                [
                    presence,
                    pod_list,
                    (
                        "-n agent-control-plane rollout restart "
                        f"deployment/{deployment}"
                    ),
                    (
                        "-n agent-control-plane get "
                        f"deployment/{deployment} -o "
                        "jsonpath={.metadata.generation}|"
                        "{.status.observedGeneration}|{.spec.replicas}|"
                        "{.status.replicas}|{.status.updatedReplicas}|"
                        "{.status.readyReplicas}|{.status.availableReplicas}"
                    ),
                    pod_list,
                ]
            )
        self.assertEqual(calls, expected_calls)
        self.assertIn("phase=complete result=succeeded deployments=5", result.stdout)
        self.assertIn(
            'wait_for_rollout "$deployment" "$deadline"', restart_script
        )
        self.assertIn(
            'wait_for_old_pods_deleted "$deployment" "$component" '
            '"$old_pods" "$deadline"',
            restart_script,
        )
        self.assertIn('timeout "${remaining_seconds}s" kubectl "$@"', restart_script)
        self.assertNotIn("--request-timeout=", restart_script)
        self.assertEqual(
            restart_script.count(
                'deadline="$(( $(date +%s) + rollout_timeout_seconds ))"'
            ),
            1,
            "each iteration must share one deadline for presence, rollout, and drain",
        )

    def test_restart_script_skips_confirmed_absent_optional_callback(self) -> None:
        optional = "agent-control-plane-callback-adapter"
        result, calls = self._run_restart_script(absent_deployment=optional)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"deployment={optional} phase=presence result=skipped "
            "reason=optional-deployment-absent",
            result.stdout,
        )
        self.assertFalse(any(f"rollout restart deployment/{optional}" in call for call in calls))
        self.assertIn("phase=complete result=succeeded deployments=4", result.stdout)

    def test_restart_script_fails_on_absent_required_deployment(self) -> None:
        required = "agent-control-plane"
        result, calls = self._run_restart_script(absent_deployment=required)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"deployment={required} phase=presence result=failed "
            "reason=deployment-not-found",
            result.stderr,
        )
        self.assertFalse(any("rollout restart" in call for call in calls))

    def test_restart_script_fails_on_optional_callback_api_error(self) -> None:
        optional = "agent-control-plane-callback-adapter"
        result, calls = self._run_restart_script(
            fail_match=f"get deployment/{optional} --ignore-not-found",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"deployment={optional} phase=presence result=failed "
            "reason=api-error kubectl_exit_code=70",
            result.stderr,
        )
        self.assertFalse(any(f"rollout restart deployment/{optional}" in call for call in calls))

    def test_restart_script_timeout_names_deployment_and_stops_later_restarts(
        self,
    ) -> None:
        failed = "agent-control-plane-model-gateway"
        result, calls = self._run_restart_script(fail_wait_deployment=failed)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"deployment={failed} phase=wait result=failed reason=timeout",
            result.stderr,
        )
        self.assertFalse(
            any("agent-control-plane-callback-adapter" in call for call in calls)
        )

    def test_restart_script_restart_error_names_deployment_and_stops(self) -> None:
        failed = "agent-control-plane-git-deliverer"
        result, calls = self._run_restart_script(fail_restart_deployment=failed)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"deployment={failed} phase=restart result=failed",
            result.stderr,
        )
        self.assertIn("reason=api-error kubectl_exit_code=17", result.stderr)
        self.assertFalse(any("agent-control-plane-local-worker" in call for call in calls))

    def test_restart_script_bounds_hanging_capture_and_stops(self) -> None:
        failed = "agent-control-plane-model-gateway"
        result, calls = self._run_restart_script(
            hang_capture_deployment=failed,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"deployment={failed} phase=capture result=failed reason=timeout",
            result.stderr,
        )
        self.assertTrue(
            any(
                "app.kubernetes.io/component=model-gateway" in call
                for call in calls
            )
        )
        self.assertFalse(
            any(f"rollout restart deployment/{failed}" in call for call in calls)
        )
        self.assertFalse(
            any("agent-control-plane-callback-adapter" in call for call in calls)
        )

    def test_restart_script_waits_for_lingering_old_pod_and_stops(self) -> None:
        failed = "agent-control-plane-model-gateway"
        component = REGISTRY_OVERLAY_COMPONENTS[failed]
        result, calls = self._run_restart_script(
            linger_old_pod_deployment=failed,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"deployment={failed} phase=drain result=failed reason=timeout "
            f"remaining_old_pods=1 names={component}-old",
            result.stderr,
        )
        self.assertFalse(
            any("agent-control-plane-callback-adapter" in call for call in calls)
        )

    def _run_restart_script(
        self,
        *,
        fail_wait_deployment: str = "",
        fail_restart_deployment: str = "",
        linger_old_pod_deployment: str = "",
        hang_capture_deployment: str = "",
        absent_deployment: str = "",
        fail_match: str = "",
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        restart_script = self.registry_overlay_restart_hook["spec"]["template"]["spec"][
            "containers"
        ][0]["command"][-1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            kubectl_log = tmp_path / "kubectl.log"
            deadline_expired = tmp_path / "deadline-expired"
            fake_date = tmp_path / "date"
            fake_date.write_text(
                """#!/bin/sh
if [ -f "$DEADLINE_EXPIRED_FILE" ]; then
  printf '1001\\n'
else
  printf '1000\\n'
fi
""",
                encoding="utf-8",
            )
            fake_date.chmod(0o755)
            fake_kubectl = tmp_path / "kubectl"
            fake_kubectl.write_text(
                """#!/bin/sh
printf '%s\\n' "$*" >> "$KUBECTL_LOG"
if [ -n "$FAIL_MATCH" ]; then
  case "$*" in
    *"$FAIL_MATCH"*) exit 70 ;;
  esac
fi
if [ "$5" = "--ignore-not-found" ] && [ "$7" = "name" ]; then
  deployment="${4#deployment/}"
  if [ "$deployment" = "$ABSENT_DEPLOYMENT" ]; then
    exit 0
  fi
  printf 'deployment.apps/%s' "$deployment"
  exit 0
fi
case "$3" in
  rollout)
    deployment="${5#deployment/}"
    if [ "$deployment" = "$FAIL_RESTART_DEPLOYMENT" ]; then
      exit 17
    fi
    exit 0
    ;;
  get)
    case "$4" in
      deployment/*)
        deployment="${4#deployment/}"
        if [ "$deployment" = "$FAIL_WAIT_DEPLOYMENT" ]; then
          : > "$DEADLINE_EXPIRED_FILE"
          printf '2|1|1|1|0|0|0'
        else
          printf '2|2|1|1|1|1|1'
        fi
        exit 0
        ;;
      pods)
        selector="$6"
        component="${selector##*app.kubernetes.io/component=}"
        state_file="$POD_LIST_STATE_DIR/$component"
        pod_list_count=0
        if [ -f "$state_file" ]; then
          IFS= read -r pod_list_count < "$state_file"
        fi
        if [ "$component" = "$HANG_CAPTURE_COMPONENT" ] \
          && [ "$pod_list_count" -eq 0 ]; then
          trap ': > "$DEADLINE_EXPIRED_FILE"; exit 28' TERM
          sleep 5
          exit 28
        fi
        pod_list_count="$((pod_list_count + 1))"
        printf '%s\\n' "$pod_list_count" > "$state_file"
        if [ "$component" = "$LINGERING_OLD_POD_COMPONENT" ] \
          && [ "$pod_list_count" -gt 1 ]; then
          : > "$DEADLINE_EXPIRED_FILE"
        fi
        if [ "$pod_list_count" -eq 1 ] \
          || [ "$component" = "$LINGERING_OLD_POD_COMPONENT" ]; then
          printf '%s-old\\n' "$component"
        fi
        exit 0
        ;;
    esac
    exit 64
    ;;
esac
exit 64
""",
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{tmp_path}:{env['PATH']}",
                    "KUBECTL_LOG": str(kubectl_log),
                    "FAIL_WAIT_DEPLOYMENT": fail_wait_deployment,
                    "FAIL_RESTART_DEPLOYMENT": fail_restart_deployment,
                    "ABSENT_DEPLOYMENT": absent_deployment,
                    "FAIL_MATCH": fail_match,
                    "DEADLINE_EXPIRED_FILE": str(deadline_expired),
                    "HANG_CAPTURE_COMPONENT": REGISTRY_OVERLAY_COMPONENTS.get(
                        hang_capture_deployment, ""
                    ),
                    "LINGERING_OLD_POD_COMPONENT": REGISTRY_OVERLAY_COMPONENTS.get(
                        linger_old_pod_deployment, ""
                    ),
                    "POD_LIST_STATE_DIR": str(tmp_path),
                    "ROLLOUT_TIMEOUT_SECONDS": "1",
                    "ROLLOUT_POLL_SECONDS": "0",
                }
            )
            result = subprocess.run(
                ["/bin/sh", "-ec", restart_script],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            calls = (
                kubectl_log.read_text(encoding="utf-8").splitlines()
                if kubectl_log.exists()
                else []
            )
        return result, calls

    def test_ci_runs_complete_registry_overlay_application_render_gate(self) -> None:
        workflow = _load_yaml(REPO_ROOT / ".github" / "workflows" / "ci.yaml")
        job = workflow["jobs"]["agent-control-plane-registry-overlay-render"]
        run_steps = [
            step.get("run", "")
            for step in job["steps"]
            if isinstance(step, dict) and "run" in step
        ]
        uses_steps = [
            step.get("uses", "")
            for step in job["steps"]
            if isinstance(step, dict) and "uses" in step
        ]

        self.assertIn("azure/setup-helm@v4", uses_steps)
        self.assertTrue(
            any("kustomize version" in step for step in run_steps),
            "render job must retain the source-file Kustomize equivalence check",
        )
        self.assertTrue(
            any(
                "scripts/check_agent_control_plane_registry_overlay_render.py" in step
                for step in run_steps
            ),
            "render job must run the complete Application render gate",
        )
        chart_matrix = workflow["jobs"]["helm-and-kubeconform"]["strategy"][
            "matrix"
        ]["chart"]
        self.assertIn(
            {
                "name": "agent-control-plane-registry-overlay",
                "path": "apps/agent-control-plane-registry-overlay",
                "release": "agent-control-plane-registry-overlay",
                "prod_values": "apps/agent-control-plane-registry-overlay/values.yaml",
            },
            chart_matrix,
        )

    def test_opencode_proposer_import_is_overlay_pinned_and_proposal_only(self) -> None:
        imports = YAML_PARSER.load(self.data["workload_imports.yaml"])
        imports_by_id = {entry["id"]: entry for entry in imports["imports"]}
        release_pin = self.agent_workloads_values["mandateReleasePins"][
            "opencode.proposer"
        ]

        opencode = imports_by_id["opencode.proposer"]
        self.assertEqual(
            opencode["manifest_path"],
            "registries/imports/agent-opencode.proposer.json",
        )
        self.assertEqual(opencode["manifest_digest"], release_pin["manifestDigest"])
        self.assertEqual(opencode["image_digest"], release_pin["imageDigest"])
        self.assertEqual(opencode["agent"]["execution_posture"], "hosted_harness")
        self.assertIs(opencode["agent"]["model_gateway_token"], True)
        self.assertEqual(opencode["agent"]["network_access"], "broker_only")

        capability = opencode["capabilities"]["agent_workloads.opencode_propose"]
        self.assertEqual(capability["model_bounds"]["allowed_tier"], "fast")
        self.assertEqual(
            capability["model_bounds"]["allowed_profiles"],
            ["openai.gpt-5.6-luna"],
        )
        self.assertEqual(capability["model_bounds"]["max_cost_usd"], 0.25)
        self.assertEqual(capability["session_authority_budget"]["max_operations"], 100)
        self.assertEqual(
            capability["disclosure"]["artifact_classes_allowed"],
            ["opencode_proposal"],
        )
        self.assertEqual(
            capability["artifacts"],
            {"allowed": True, "broker_required": False},
        )

        orchestrate = opencode["capabilities"]["agent_workloads.opencode_orchestrate"]
        self.assertEqual(
            orchestrate["result_contract"]["output_schema"],
            "agent_workloads_opencode_orchestrate_result_v1",
        )
        released_fields = set(orchestrate["result_contract"]["released_result_fields"])
        self.assertIn("delegated_capability_id", released_fields)
        self.assertNotIn("capability_id", released_fields)

        manifest = json.loads(self.data["agent-opencode.proposer.json"])
        self.assertEqual(manifest["id"], "opencode.proposer")
        self.assertEqual(manifest["digest"], release_pin["manifestDigest"])
        self.assertEqual(manifest["code_digest"], release_pin["codeDigest"])
        self.assertEqual(manifest["image"]["digest"], release_pin["imageDigest"])
        self.assertEqual(
            manifest["capability_metadata"]["agent_workloads.opencode_propose"],
            {"consequence_class": "reversible_staging"},
        )

    def test_opencode_apply_import_is_executor_only_and_admin_confirmed(self) -> None:
        imports = YAML_PARSER.load(self.data["workload_imports.yaml"])
        imports_by_id = {entry["id"]: entry for entry in imports["imports"]}
        release_pin = self.agent_workloads_values["mandateReleasePins"][
            "opencode.apply_executor"
        ]

        opencode_apply = imports_by_id["opencode.apply_executor"]
        self.assertEqual(
            opencode_apply["manifest_path"],
            "registries/imports/agent-opencode.apply_executor.json",
        )
        self.assertEqual(
            opencode_apply["manifest_digest"], release_pin["manifestDigest"]
        )
        self.assertEqual(opencode_apply["image_digest"], release_pin["imageDigest"])
        self.assertEqual(
            opencode_apply["agent"]["execution_posture"],
            "capability_worker",
        )
        self.assertEqual(opencode_apply["agent"]["network_access"], "broker_only")
        self.assertIs(opencode_apply["agent"]["executor"], True)
        self.assertNotIn("model_gateway_token", opencode_apply["agent"])

        capability = opencode_apply["capabilities"]["agent_workloads.opencode_apply"]
        self.assertEqual(capability["approval_mode"], "admin_confirm")
        self.assertEqual(
            capability["output_schema"],
            "agent_workloads_opencode_apply_result_v1",
        )
        self.assertEqual(capability["session_authority_budget"]["max_operations"], 1)
        self.assertEqual(
            capability["session_authority_budget"]["influence"],
            "principal",
        )
        self.assertEqual(
            capability["artifacts"],
            {"allowed": True, "broker_required": False},
        )
        self.assertNotIn("model_bounds", capability)
        self.assertEqual(
            capability["disclosure"]["artifact_classes_allowed"],
            ["opencode_apply_result"],
        )
        self.assertEqual(
            capability["disclosure"]["max_confidentiality_level_out"],
            "customer_visible",
        )
        self.assertIs(
            capability["disclosure"]["require_output_redaction_pass"],
            True,
        )
        self.assertIs(capability["disclosure"]["require_result_schema"], True)
        self.assertEqual(
            capability["result_contract"]["output_schema"],
            "agent_workloads_opencode_apply_result_v1",
        )
        released_fields = set(capability["result_contract"]["released_result_fields"])
        self.assertGreaterEqual(
            released_fields,
            {
                "output_text",
                "operation_status",
                "action_id",
                "branch",
                "commit_sha",
                "applied_diff_sha256",
                "proposal_diff_sha256",
                "changed_files",
                "base_repo",
                "base_ref_name",
                "base_commit_sha",
                "base_tree_sha",
            },
        )
        self.assertTrue(
            released_fields.isdisjoint(
                {
                    "diff",
                    "patch",
                    "unified_diff",
                    "remote_ref",
                    "pull_request_url",
                    "pr_url",
                    "delivery_status",
                }
            )
        )
        self.assertEqual(
            capability["disclosure_summary"]["artifact_classes_allowed"],
            ["opencode_apply_result"],
        )
        self.assertIn(
            "Remote ref and PR URL arrive only through the deliverer callback after "
            "confirmed write.",
            capability["limitations"],
        )

        manifest = json.loads(self.data["agent-opencode.apply_executor.json"])
        self.assertEqual(manifest["id"], "opencode.apply_executor")
        self.assertEqual(manifest["digest"], release_pin["manifestDigest"])
        self.assertEqual(manifest["code_digest"], release_pin["codeDigest"])
        self.assertEqual(manifest["image"]["digest"], release_pin["imageDigest"])
        self.assertEqual(
            manifest["capability_metadata"]["agent_workloads.opencode_apply"],
            {"consequence_class": "consequential"},
        )
        self.assertEqual(manifest["evals"]["required"], ["eval.opencode_apply_smoke"])

    def test_opencode_policy_and_eval_overlay_are_mounted(self) -> None:
        policy = YAML_PARSER.load(self.data["policy.prod.yaml"])
        bindings_by_id = {item["id"]: item for item in policy["bindings"]}
        binding = bindings_by_id["private-admin-controlled-capabilities"]
        synthetic_binding = bindings_by_id["synthetic-live-verify-probe"]

        self.assertEqual(
            binding["surface_identifiers"],
            {
                "guild_id": "1523242748822425750",
                "channel_id": "1523242750043226234",
            },
        )
        self.assertEqual(
            synthetic_binding["users"]["authorized"],
            ["mandate-live-probe"],
        )
        self.assertEqual(
            synthetic_binding["principal"],
            {
                "issuer": "synthetic",
                "subject_kind": "service",
                "tenant_id": "garzai-prod",
            },
        )
        self.assertEqual(
            synthetic_binding["capabilities"]["allow"],
            ["mandate.deploy.smoke", "agent_workloads.readonly_query"],
        )
        self.assertEqual(synthetic_binding["users"].get("admins"), [])
        self.assertNotIn("approval_overrides", synthetic_binding["capabilities"])

        self.assertIn(
            "agent_workloads.opencode_propose",
            binding["capabilities"]["allow"],
        )
        self.assertIn(
            "agent_workloads.opencode_apply",
            binding["capabilities"]["allow"],
        )
        self.assertEqual(
            binding["capabilities"]["approval_overrides"][
                "agent_workloads.opencode_apply"
            ],
            "admin_confirm",
        )
        self.assertEqual(policy["defaults"]["max_cost_usd_per_job"], 10.0)
        self.assertEqual(policy["defaults"]["max_runtime_seconds_per_job"], 60)
        self.assertEqual(
            policy["defaults"]["max_runtime_seconds_per_capability"],
            {
                "agent_workloads.readonly_query": 180,
                "agent_workloads.opencode_propose": 900,
                "agent_workloads.opencode_task": 900,
                "agent_workloads.opencode_orchestrate": 900,
                "agent_workloads.opencode_apply": 300,
            },
        )
        self.assertEqual(
            policy["defaults"]["aggregate_budget"]["per_capability_daily_usd"][
                "agent_workloads.opencode_propose"
            ],
            10.0,
        )
        self.assertEqual(
            policy["defaults"]["aggregate_budget"]["per_capability_daily_usd"][
                "agent_workloads.opencode_apply"
            ],
            50.0,
        )

        evals = YAML_PARSER.load(self.data["evals.yaml"])
        evals_by_id = {entry["id"]: entry for entry in evals["eval_suites"]}
        self.assertIn("eval.task_echo_smoke", evals_by_id)
        self.assertEqual(
            evals_by_id["eval.readonly_sql_safety"]["applies_to"],
            ["data.readonly_sql"],
        )
        self.assertEqual(
            evals_by_id["eval.opencode_proposer_smoke"]["dataset"],
            "registries/imports/opencode_proposer_smoke.jsonl",
        )
        self.assertEqual(
            evals_by_id["eval.opencode_apply_smoke"]["dataset"],
            "registries/imports/opencode_apply_smoke.jsonl",
        )
        self.assertIn("opencode_proposer_smoke.jsonl", self.data)
        self.assertIn("opencode_apply_smoke.jsonl", self.data)

        mounts = {
            mount["mountPath"]: mount
            for mount in self.control_plane_values["extraVolumeMounts"]
        }
        self.assertEqual(
            mounts["/app/registries/evals.yaml"]["subPath"],
            "evals.yaml",
        )

    def test_synthetic_live_verify_runs_internal_deployment_and_readonly_probes(
        self,
    ) -> None:
        synthetic = self.control_plane_values["syntheticLiveVerify"]

        self.assertTrue(synthetic["enabled"])
        self.assertEqual(synthetic["schedule"], "*/5 * * * *")
        self.assertEqual(synthetic["baseUrl"], "https://agent-control-plane.garz.ai")
        self.assertEqual(
            synthetic["principal"],
            {
                "issuer": "synthetic",
                "subject_id": "mandate-live-probe",
                "subject_kind": "service",
                "tenant_id": "garzai-prod",
                "roles": ["synthetic_probe"],
                "provenance": {
                    "source": "synthetic-live-verify",
                    "identifiers": {
                        "tenant_ref": "garzai-prod",
                        "subject_ref": "mandate-live-probe",
                    },
                },
            },
        )
        self.assertEqual(
            synthetic["deliveryTarget"],
            {
                "kind": "internal",
                "target_ref": "synthetic-live-verify",
                "message_ref": "scheduled-synthetic-live-verify",
            },
        )
        self.assertEqual(
            synthetic["surfaceContext"],
            {
                "source": "synthetic-live-verify",
                "surface_ref": "synthetic-live-verify",
                "adapter_provenance": {
                    "source": "synthetic-live-verify",
                    "identifiers": {"probe": "scheduled"},
                },
            },
        )
        self.assertEqual(
            synthetic["trustedContext"],
            {"entitlements": [], "attachment_authorities": []},
        )
        journeys = {journey["id"]: journey for journey in synthetic["journeys"]}
        self.assertEqual(
            set(journeys),
            {"deployment-smoke", "readonly-query-skill-digests"},
        )
        self.assertEqual(
            journeys["deployment-smoke"]["required_result_fields"],
            ["output_text", "schema_version"],
        )
        readonly_query = journeys["readonly-query-skill-digests"]
        self.assertEqual(
            readonly_query["capability_id"],
            "agent_workloads.readonly_query",
        )
        imports = YAML_PARSER.load(self.data["workload_imports.yaml"])
        workspace_probe = next(
            entry
            for entry in imports["imports"]
            if entry["id"] == "data.workspace_probe"
        )
        self.assertIs(workspace_probe["agent"]["model_call"], True)
        self.assertFalse(readonly_query.get("run_local_worker", False))
        self.assertIs(
            workspace_probe["capabilities"]["agent_workloads.readonly_query"][
                "model_bounds"
            ]["required"],
            True,
        )
        self.assertEqual(readonly_query["required_result_fields"], ["output_text"])
        self.assertEqual(
            readonly_query["required_event_types"],
            [
                "run.created",
                "policy.evaluated",
                "model_call.finished",
                "tool.started",
                "result.released",
            ],
        )
        self.assertEqual(
            readonly_query["required_skill_ids"],
            ["xscraper-schema", "xscraper-glossary"],
        )

    def test_synthetic_live_verify_runtime_budgets_and_deadline_are_coherent(
        self,
    ) -> None:
        policy = YAML_PARSER.load(self.data["policy.prod.yaml"])
        defaults = policy["defaults"]
        synthetic = self.control_plane_values["syntheticLiveVerify"]
        journey_specs = synthetic["journeys"]
        journeys = {
            journey["capability_id"]: journey
            for journey in journey_specs
        }

        global_runtime = defaults["max_runtime_seconds_per_job"]
        readonly_runtime = defaults["max_runtime_seconds_per_capability"][
            "agent_workloads.readonly_query"
        ]
        self.assertEqual(global_runtime, 60)
        self.assertEqual(
            journeys["mandate.deploy.smoke"]["max_runtime_seconds"],
            global_runtime,
        )
        self.assertEqual(readonly_runtime, 180)
        self.assertEqual(
            journeys["agent_workloads.readonly_query"]["max_runtime_seconds"],
            readonly_runtime,
        )

        for journey in journey_specs:
            self.assertGreaterEqual(
                journey["timeout_seconds"] - journey["max_runtime_seconds"],
                SYNTHETIC_LIVE_VERIFY_MIN_SETTLEMENT_MARGIN_SECONDS,
            )
        sequential_timeout_budget = sum(
            journey["timeout_seconds"] for journey in journey_specs
        )

        args = synthetic["args"]
        http_timeout_index = args.index("--http-timeout-seconds") + 1
        http_timeout_seconds = int(args[http_timeout_index])
        control_evidence_budget = 0
        for journey in journey_specs:
            # Submission and one status request can sit outside the journey's
            # polling deadline. Worker dispatch, callbacks, and audit evidence
            # each add another bounded request when that stage is configured.
            bounded_requests = 2
            bounded_requests += int(journey.get("run_local_worker", False))
            bounded_requests += int(
                bool(journey.get("required_callback_types", ("runtime-default",)))
            )
            bounded_requests += int(
                bool(journey.get("required_event_types", ("runtime-default",)))
            )
            control_evidence_budget += bounded_requests * http_timeout_seconds

        required_deadline = (
            sequential_timeout_budget
            + control_evidence_budget
            + SYNTHETIC_LIVE_VERIFY_JOB_STARTUP_HEADROOM_SECONDS
        )
        active_deadline = synthetic["activeDeadlineSeconds"]
        self.assertEqual(active_deadline, 480)
        self.assertGreaterEqual(active_deadline, required_deadline)

    def test_prometheus_alerts_on_failed_synthetic_live_verify_job(self) -> None:
        rules_template = (
            REPO_ROOT
            / "helm"
            / "garz-observability"
            / "templates"
            / "monitoring-prometheus-rules-configmap.yaml"
        ).read_text()

        self.assertIn("MandateSyntheticLiveVerifyFailed", rules_template)
        self.assertIn(
            'owner_name="agent-control-plane-synthetic-live-verify"',
            rules_template,
        )
        self.assertIn(
            'kube_job_status_failed{namespace="agent-control-plane"}',
            rules_template,
        )
        self.assertIn("kube_job_owner", rules_template)

    def test_hosted_harness_safe_floor_and_token_handoff_are_configured(self) -> None:
        values = self.control_plane_values
        self.assertIn(
            "AGENT_PLATFORM_MODEL_GATEWAY_TOKEN_SECRET",
            values["secretKeys"],
        )
        subjects = json.loads(
            values["env"]["AGENT_PLATFORM_WORKLOAD_IDENTITY_ALLOWED_SUBJECTS_JSON"]
        )
        self.assertIn("opencode.proposer", subjects["worker_service"])
        self.assertIn("opencode.apply_executor", subjects["worker_service"])

        env = values["env"]
        self.assertEqual(
            env["AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_EVIDENCE"],
            "deployment_attestation",
        )
        for key in (
            "AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_EGRESS_JAIL",
            "AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_NO_AMBIENT_CREDENTIALS",
            "AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_MODEL_GATEWAY",
            "AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_COST_CUTOFF",
            "AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_CONSEQUENCE_ENFORCEMENT",
            "AGENT_PLATFORM_HOSTED_HARNESS_SAFE_FLOOR_AUDIT",
        ):
            self.assertEqual(env[key], "true")

    def test_trusted_edge_token_is_wired_with_legacy_alias(self) -> None:
        values = self.control_plane_values
        secret_data = self.control_plane_secret["stringData"]

        self.assertIn("AGENT_PLATFORM_TRUSTED_EDGE_TOKEN", values["secretKeys"])
        self.assertIn("AGENT_PLATFORM_OPENCLAW_TOKEN", values["secretKeys"])
        self.assertIn("AGENT_PLATFORM_TRUSTED_EDGE_TOKEN", secret_data)
        self.assertIn("AGENT_PLATFORM_OPENCLAW_TOKEN", secret_data)

    def test_git_deliverer_is_configured_for_mandate_sandbox_only(self) -> None:
        values = self.control_plane_values
        deliverer = values["gitDeliverer"]

        self.assertTrue(deliverer["enabled"])
        self.assertEqual(deliverer["replicaCount"], 1)
        self.assertEqual(
            deliverer["secretKeys"],
            ["AGENT_PLATFORM_GIT_DELIVERY_GITHUB_TOKEN"],
        )
        self.assertNotIn(
            "AGENT_PLATFORM_GIT_DELIVERY_GITHUB_TOKEN",
            values["secretKeys"],
        )
        self.assertEqual(deliverer["targetRepo"], "cesaregarza/mandate-sandbox")
        self.assertEqual(
            deliverer["remoteUrl"],
            "https://github.com/cesaregarza/mandate-sandbox",
        )
        self.assertEqual(deliverer["baseRef"], "main")
        self.assertEqual(
            deliverer["allowedBaseRepos"],
            ["https://github.com/cesaregarza/mandate-sandbox"],
        )
        self.assertIn(
            "AGENT_PLATFORM_GIT_DELIVERY_GITHUB_TOKEN",
            self.control_plane_secret["stringData"],
        )

    def test_model_gateway_kill_switch_and_revocation_files_are_wired(self) -> None:
        values = self.control_plane_values
        env = values["env"]
        self.assertEqual(
            env["AGENT_PLATFORM_MODEL_GATEWAY_KILL_SWITCH_FILE"],
            "/app/model-gateway-controls/kill-switch",
        )
        self.assertEqual(
            env["AGENT_PLATFORM_MODEL_GATEWAY_REVOCATION_FILE"],
            "/app/model-gateway-controls/revocations.txt",
        )

        volumes = {volume["name"]: volume for volume in values["extraVolumes"]}
        self.assertEqual(
            volumes["model-gateway-controls"]["configMap"]["name"],
            "agent-control-plane-model-gateway-controls",
        )

        mounts = {mount["name"]: mount for mount in values["extraVolumeMounts"]}
        controls_mount = mounts["model-gateway-controls"]
        self.assertEqual(controls_mount["mountPath"], "/app/model-gateway-controls")
        self.assertTrue(controls_mount["readOnly"])
        self.assertNotIn("subPath", controls_mount)

        configmap = self.model_gateway_controls
        self.assertEqual(configmap["kind"], "ConfigMap")
        self.assertEqual(
            configmap["metadata"]["name"],
            "agent-control-plane-model-gateway-controls",
        )
        self.assertEqual(configmap["metadata"]["namespace"], "agent-control-plane")
        self.assertEqual(
            configmap["metadata"]["annotations"][
                "mandate.cesaregarza.io/operator-editable"
            ],
            "true",
        )
        self.assertNotIn("kill-switch", configmap["data"])
        self.assertIn("revocations.txt", configmap["data"])

        raw_sources = [
            source
            for source in self.control_plane_application["spec"]["sources"]
            if source.get("path") == "apps/agent-control-plane-runtime-controls"
        ]
        self.assertEqual(len(raw_sources), 1)
        self.assertEqual(
            raw_sources[0]["repoURL"],
            "https://github.com/cesaregarza/GarzAICluster",
        )

    def test_model_gateway_provider_pins_are_scoped_to_gateway_process(self) -> None:
        values = self.control_plane_values
        api_pins = json.loads(values["env"]["AGENT_PLATFORM_PROVIDER_DIGEST_PINS_JSON"])
        gateway_pins = json.loads(
            values["modelGateway"]["env"]["AGENT_PLATFORM_PROVIDER_DIGEST_PINS_JSON"]
        )

        self.assertEqual(
            set(api_pins),
            {"model_gateway", "readonly-sql-broker"},
        )
        self.assertEqual(
            set(gateway_pins),
            {"model_gateway"},
        )
        self.assertEqual(api_pins["model_gateway"], gateway_pins["model_gateway"])
        self.assertEqual(
            api_pins["readonly-sql-broker"]["digest"],
            "sha256:73203a3ff8309cb762966f7559abf871f46a239c3136e7a5658eb069f52066c1",
        )
        self.assertEqual(
            gateway_pins["model_gateway"],
            {
                "digest": (
                    "sha256:"
                    "2010eb78000580e9bc9ad74b57b70500318014a397193133005fae153b39c336"
                ),
                "protocol": "model_gateway",
            },
        )

    def test_control_plane_pin_understands_opencode_executor_imports(self) -> None:
        sources = self.control_plane_application["spec"]["sources"]
        mandate_source = next(
            source
            for source in sources
            if source["repoURL"] == "git@github.com:cesaregarza/agent-platform.git"
        )
        target_revision = mandate_source["targetRevision"]
        self.assertEqual(
            self.control_plane_values["image"]["tag"],
            f"sha-{target_revision[:12]}",
        )


if __name__ == "__main__":
    unittest.main()
