from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "helm" / "citrus"
DEV_VALUES = CHART_PATH / "values-dev.yaml"
RUNBOOK_PATH = (
    REPO_ROOT / "docs" / "runbooks" / "citrus-recurring-runtime-preflight.md"
)
YAML_PARSER = YAML(typ="safe")
TOPOLOGY_REVISION = "ces-850-test"
TOPOLOGY_ANNOTATION = "citrus.grace/recurring-topology-revision"
RUNTIME_ENVIRONMENT = {
    "RECURRING_RUNTIME_TOPOLOGY_REVISION": TOPOLOGY_REVISION,
    "RECURRING_RUNTIME_SCHEDULER": "kubernetes-cronjob",
}
SOURCE_REVISION_ENV = "CITRUS_EXPECTED_SOURCE_REVISION"


def _command(
    *,
    development: bool,
    runtime_enabled: bool,
    release: str | None = None,
    namespace: str | None = None,
) -> list[str]:
    release = release or ("citrus-dev" if development else "citrus")
    namespace = namespace or ("citrus-dev" if development else "default")
    environment = "development" if development else "production"
    owner = "citrus-dev" if development else "citrus"
    mode = "deny" if development else "allow"
    command = [
        "helm",
        "template",
        release,
        str(CHART_PATH),
        "--namespace",
        namespace,
        "-f",
        str(CHART_PATH / "values.yaml"),
    ]
    if development:
        command.extend(["-f", str(DEV_VALUES)])
    else:
        command.extend(["--set", "stripeSmokePromotion.enabled=false"])
    command.extend(
        [
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
            "paymentSafety.policy.revision=ces-845-test",
            "--set",
            "paymentSafety.networkPolicy.enabled=true",
            "--set-string",
            f"billingWorker.topologyRevision={TOPOLOGY_REVISION}",
            "--set-string",
            f"recurringRuntime.topologyRevision={TOPOLOGY_REVISION}",
            "--set-string",
            (
                "recurringRuntime.preflight.topologyRevision="
                f"{TOPOLOGY_REVISION}"
            ),
            "--set-string",
            (
                "recurringRuntime.health.topologyRevision="
                f"{TOPOLOGY_REVISION}"
            ),
        ]
    )
    if development:
        command.extend(
            [
                "--set-string",
                "paymentSafety.networkPolicy.database.host=db.dev.example",
            ]
        )
    if runtime_enabled:
        command.extend(
            [
                "--set",
                "billingWorker.enabled=true",
                "--set",
                "recurringRuntime.enabled=true",
                "--set",
                "recurringRuntime.preflight.enabled=true",
            ]
        )
    return command


def _default_command(*, development: bool) -> list[str]:
    command = [
        "helm",
        "template",
        "citrus-dev" if development else "citrus",
        str(CHART_PATH),
        "--namespace",
        "citrus-dev" if development else "default",
        "-f",
        str(CHART_PATH / "values.yaml"),
    ]
    if development:
        command.extend(["-f", str(DEV_VALUES)])
    return command


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")
    return subprocess.run(
        command,
        check=False,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _documents(command: list[str]) -> list[dict[str, Any]]:
    result = _run(command)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [
        document
        for document in YAML_PARSER.load_all(result.stdout)
        if isinstance(document, dict) and document
    ]


def _key(document: dict[str, Any]) -> tuple[str, str]:
    return document["kind"], document["metadata"]["name"]


def _named(
    documents: list[dict[str, Any]], kind: str, name: str
) -> dict[str, Any]:
    return next(document for document in documents if _key(document) == (kind, name))


def _pod_template(document: dict[str, Any]) -> dict[str, Any]:
    if document["kind"] in {"Deployment", "Job"}:
        return document["spec"]["template"]
    if document["kind"] == "CronJob":
        return document["spec"]["jobTemplate"]["spec"]["template"]
    raise AssertionError(f"unsupported workload kind: {document['kind']}")


def _literal_env(container: dict[str, Any]) -> dict[str, str]:
    return {
        entry["name"]: str(entry["value"])
        for entry in container.get("env", [])
        if "name" in entry and "value" in entry
    }


class CitrusRecurringPreflightContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.default_prod = _documents(_default_command(development=False))
        cls.default_dev = _documents(_default_command(development=True))
        cls.enabled_prod = _documents(
            _command(development=False, runtime_enabled=True)
        )
        cls.enabled_dev = _documents(
            _command(development=True, runtime_enabled=True)
        )
        cls.disabled_dev = _documents(
            _command(development=True, runtime_enabled=False)
        )

    def test_production_and_base_dev_renders_remain_inert(self) -> None:
        forbidden_suffixes = {
            "billing-worker",
            "recurring-preflight",
            "recurring-tick",
            "recurring-health",
        }
        for environment, documents in (
            ("production", self.default_prod),
            ("development", self.default_dev),
        ):
            with self.subTest(environment=environment):
                names = {document["metadata"]["name"] for document in documents}
                self.assertFalse(
                    any(
                        any(name.endswith(suffix) for suffix in forbidden_suffixes)
                        for name in names
                    )
                )
                rendered = json.dumps(documents, sort_keys=True)
                self.assertNotIn("RECURRING_RUNTIME_TOPOLOGY_REVISION", rendered)
                self.assertNotIn(SOURCE_REVISION_ENV, rendered)
                self.assertNotIn(TOPOLOGY_ANNOTATION, rendered)

        for application in ("citrus.yaml", "citrus-dev.yaml"):
            contents = (
                REPO_ROOT / "argocd" / "applications" / application
            ).read_text(encoding="utf-8")
            self.assertNotIn("recurringRuntime", contents)
            self.assertNotIn("billingWorker", contents)

    def test_enabled_safe_topology_is_complete_and_revision_bound(self) -> None:
        for release, documents in (
            ("citrus", self.enabled_prod),
            ("citrus-dev", self.enabled_dev),
        ):
            runtime_images: set[str] = set()
            workloads = (
                ("Deployment", f"{release}-billing-worker"),
                ("Job", f"{release}-recurring-preflight"),
                ("CronJob", f"{release}-recurring-tick"),
                ("CronJob", f"{release}-recurring-health"),
            )
            for kind, name in workloads:
                with self.subTest(release=release, workload=name):
                    workload = _named(documents, kind, name)
                    self.assertEqual(
                        workload["metadata"]["annotations"][TOPOLOGY_ANNOTATION],
                        TOPOLOGY_REVISION,
                    )
                    pod = _pod_template(workload)
                    self.assertIs(
                        pod["spec"]["automountServiceAccountToken"],
                        False,
                    )
                    self.assertEqual(
                        pod["metadata"]["annotations"][TOPOLOGY_ANNOTATION],
                        TOPOLOGY_REVISION,
                    )
                    for container in pod["spec"]["containers"]:
                        runtime_images.add(container["image"])
                        actual = _literal_env(container)
                        self.assertEqual(
                            {
                                key: actual.get(key)
                                for key in RUNTIME_ENVIRONMENT
                            },
                            RUNTIME_ENVIRONMENT,
                            container["name"],
                        )
                        image_tag = container["image"].rsplit(":", 1)[1]
                        self.assertRegex(image_tag, r"^[0-9a-f]{40}$")
                        self.assertEqual(
                            actual.get(SOURCE_REVISION_ENV),
                            image_tag,
                            container["name"],
                        )

            worker = _named(
                documents,
                "Deployment",
                f"{release}-billing-worker",
            )
            self.assertEqual(worker["spec"]["replicas"], 1)
            tick = _named(
                documents,
                "CronJob",
                f"{release}-recurring-tick",
            )
            self.assertEqual(
                tick["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                ["containers"][0]["command"],
                [
                    "python",
                    "manage.py",
                    "tick_recurring_orders",
                    "--scan-limit=100",
                    "--dispatch-limit=100",
                ],
            )

            web = _named(documents, "Deployment", release)
            web_image = web["spec"]["template"]["spec"]["containers"][0][
                "image"
            ]
            self.assertEqual(runtime_images, {web_image})

            self.assertFalse(
                any(document.get("kind") == "Secret" for document in documents)
            )

    def test_preflight_is_a_bounded_read_only_sync_gate(self) -> None:
        preflight = _named(
            self.enabled_dev,
            "Job",
            "citrus-dev-recurring-preflight",
        )
        annotations = preflight["metadata"]["annotations"]
        self.assertEqual(annotations["argocd.argoproj.io/hook"], "Sync")
        self.assertEqual(annotations["argocd.argoproj.io/sync-wave"], "2")
        self.assertEqual(
            annotations["argocd.argoproj.io/hook-delete-policy"],
            "BeforeHookCreation,HookSucceeded",
        )
        self.assertEqual(preflight["spec"]["activeDeadlineSeconds"], 240)
        self.assertEqual(preflight["spec"]["backoffLimit"], 0)
        pod = preflight["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        container = pod["containers"][0]
        self.assertEqual(
            container["command"],
            [
                "python",
                "manage.py",
                "preflight_recurring_runtime",
                "--include-broker",
                "--format=json",
            ],
        )
        self.assertFalse(
            container["securityContext"]["allowPrivilegeEscalation"]
        )
        self.assertNotIn("STRIPE_SECRET_KEY", _literal_env(container))

        migration = _named(
            self.enabled_dev,
            "Job",
            "citrus-dev-migrate-1",
        )
        worker = _named(
            self.enabled_dev,
            "Deployment",
            "citrus-dev-billing-worker",
        )
        tick = _named(
            self.enabled_dev,
            "CronJob",
            "citrus-dev-recurring-tick",
        )
        health = _named(
            self.enabled_dev,
            "CronJob",
            "citrus-dev-recurring-health",
        )
        self.assertEqual(
            [
                int(migration["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]),
                int(annotations["argocd.argoproj.io/sync-wave"]),
                int(worker["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]),
                int(tick["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]),
                int(health["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]),
            ],
            [1, 2, 3, 4, 4],
        )

    def test_enabled_safe_render_keeps_customer_and_charge_gates_off(self) -> None:
        config = _named(
            self.enabled_dev,
            "ConfigMap",
            "django-config",
        )["data"]
        self.assertEqual(config["RECURRING_BILLING_QUEUE"], "billing")
        self.assertEqual(config["RECURRING_ORDER_ENROLLMENT_MODE"], "off")
        self.assertEqual(config["RECURRING_ORDER_ENROLLMENT_ALLOWLIST"], "")
        self.assertEqual(config["RECURRING_ORDER_COHORT_MODE"], "off")
        self.assertEqual(config["RECURRING_ORDER_COHORT_ALLOWLIST"], "")
        self.assertEqual(config["RECURRING_REMINDERS_ENABLED"], "False")
        self.assertEqual(config["RECURRING_CHARGING_ENABLED"], "False")
        self.assertEqual(config["RECURRING_CHARGE_EMERGENCY_STOP"], "True")
        self.assertEqual(config["CELERY_TASK_ALWAYS_EAGER"], "False")
        self.assertEqual(config["CELERY_BROKER_URL"], "redis://citrus-redis:6379/1")

    def test_payment_boundary_selects_the_preflight_job(self) -> None:
        policy = _named(
            self.enabled_dev,
            "CiliumNetworkPolicy",
            "citrus-dev-payment-egress-batch",
        )
        expressions = policy["spec"]["endpointSelector"]["matchExpressions"]
        components = next(
            expression["values"]
            for expression in expressions
            if expression["key"] == "app.kubernetes.io/component"
        )
        self.assertIn("recurring-preflight", components)

    def test_enabled_to_disabled_render_removes_only_stateless_runtime(self) -> None:
        enabled = {_key(document): document for document in self.enabled_dev}
        disabled = {_key(document): document for document in self.disabled_dev}
        removed = set(enabled) - set(disabled)
        self.assertEqual(
            removed,
            {
                ("Deployment", "citrus-dev-billing-worker"),
                ("Job", "citrus-dev-recurring-preflight"),
                ("CronJob", "citrus-dev-recurring-tick"),
                ("CronJob", "citrus-dev-recurring-health"),
            },
        )
        self.assertFalse(set(disabled) - set(enabled))
        for key in set(enabled) & set(disabled):
            with self.subTest(resource=key):
                self.assertEqual(enabled[key], disabled[key])

        for stable in (
            ("ConfigMap", "django-config"),
            ("Job", "citrus-dev-migrate-1"),
            ("Deployment", "citrus-redis"),
            ("Service", "citrus-redis"),
        ):
            self.assertIn(stable, disabled)

    def test_partial_or_unprotected_topologies_fail_closed(self) -> None:
        cases = (
            (
                ["--set", "billingWorker.enabled=false"],
                "requires billingWorker.enabled",
            ),
            (
                ["--set", "recurringRuntime.enabled=false"],
                "requires recurringRuntime.enabled=true",
            ),
            (
                ["--set", "recurringRuntime.preflight.enabled=false"],
                "requires recurringRuntime.preflight.enabled=true",
            ),
            (
                ["--set", "recurringRuntime.health.enabled=false"],
                "requires recurringRuntime.health.enabled=true",
            ),
            (
                ["--set", "billingWorker.metrics.enabled=false"],
                "requires billingWorker.metrics.enabled",
            ),
            (
                ["--set", "paymentSafety.enabled=false"],
                "requires paymentSafety.enabled=true",
            ),
            (
                ["--set", "paymentSafety.policy.required=false"],
                "paymentSafety.policy.required",
            ),
            (
                ["--set", "paymentSafety.networkPolicy.enabled=false"],
                "paymentSafety.networkPolicy.enabled",
            ),
            (
                ["--set", "migrations.enabled=false"],
                "requires migrations.enabled=true",
            ),
            (
                ["--set", "redis.enabled=false"],
                "redis.enabled must be true",
            ),
        )
        self._assert_negative_cases(cases)

    def test_environment_and_topology_identity_fail_closed(self) -> None:
        mismatched_release = _command(
            development=True,
            runtime_enabled=True,
            release="citrus",
            namespace="citrus-dev",
        )
        mismatched_namespace = _command(
            development=True,
            runtime_enabled=True,
            namespace="default",
        )
        mismatched_prod_release = _command(
            development=False,
            runtime_enabled=True,
            release="citrus-dev",
        )
        mismatched_prod_namespace = _command(
            development=False,
            runtime_enabled=True,
            namespace="citrus-dev",
        )
        for label, command, expected in (
            (
                "development release",
                mismatched_release,
                "development payment safety requires release citrus-dev in namespace citrus-dev",
            ),
            (
                "development namespace",
                mismatched_namespace,
                "development payment safety requires release citrus-dev in namespace citrus-dev",
            ),
            (
                "production release",
                mismatched_prod_release,
                "production payment safety requires release citrus in namespace default",
            ),
            (
                "production namespace",
                mismatched_prod_namespace,
                "production payment safety requires release citrus in namespace default",
            ),
        ):
            with self.subTest(identity=label):
                result = _run(command)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(expected, result.stderr)

        cases = (
            (
                ["--set-string", "image.tag="],
                "image.tag is required",
            ),
            (
                ["--set-string", "image.tag=1234abcd"],
                "image.tag must be an exact 40-character lowercase source revision",
            ),
            (
                ["--set-string", "image.tag=latest"],
                "image.tag must be an exact 40-character lowercase source revision",
            ),
            (
                ["--set-string", f"image.tag={'A' * 40}"],
                "image.tag must be an exact 40-character lowercase source revision",
            ),
            (
                [
                    "--set-string",
                    "recurringRuntime.expectedSourceRevision=",
                ],
                "recurringRuntime.expectedSourceRevision",
            ),
            (
                [
                    "--set-string",
                    "recurringRuntime.expectedSourceRevision=1234abcd",
                ],
                "recurringRuntime.expectedSourceRevision",
            ),
            (
                [
                    "--set-string",
                    "recurringRuntime.expectedSourceRevision=latest",
                ],
                "recurringRuntime.expectedSourceRevision",
            ),
            (
                [
                    "--set-string",
                    f"recurringRuntime.expectedSourceRevision={'A' * 40}",
                ],
                "recurringRuntime.expectedSourceRevision",
            ),
            (
                [
                    "--set-string",
                    f"recurringRuntime.expectedSourceRevision={'b' * 40}",
                ],
                "recurringRuntime.expectedSourceRevision must match image.tag exactly",
            ),
            (
                ["--set-string", "recurringRuntime.topologyRevision="],
                "recurringRuntime.topologyRevision is required",
            ),
            (
                ["--set-string", "billingWorker.topologyRevision=other"],
                "topology revisions must match exactly",
            ),
            (
                [
                    "--set-string",
                    "recurringRuntime.preflight.topologyRevision=other",
                ],
                "topology revisions must match exactly",
            ),
            (
                ["--set-string", "recurringRuntime.health.topologyRevision=other"],
                "topology revisions must match exactly",
            ),
            (
                ["--set-string", "recurringRuntime.scheduler=unknown"],
                "recurringRuntime.scheduler",
            ),
        )
        self._assert_negative_cases(cases)

    def test_customer_payment_scheduler_and_ordering_matrix_fails_closed(self) -> None:
        cases = (
            (
                [
                    "--set-string",
                    "application.configData.RECURRING_BILLING_QUEUE=media",
                ],
                "RECURRING_BILLING_QUEUE must be billing",
            ),
            (
                [
                    "--set-string",
                    "application.configData.RECURRING_ORDER_ENROLLMENT_MODE=allowlist",
                ],
                "RECURRING_ORDER_ENROLLMENT_MODE must remain off",
            ),
            (
                [
                    "--set-string",
                    "application.configData.RECURRING_ORDER_ENROLLMENT_ALLOWLIST=customer@example.invalid",
                ],
                "RECURRING_ORDER_ENROLLMENT_ALLOWLIST must remain empty",
            ),
            (
                [
                    "--set-string",
                    "application.configData.RECURRING_ORDER_ENROLLMENT_ALLOWLIST= ",
                ],
                "RECURRING_ORDER_ENROLLMENT_ALLOWLIST must remain empty",
            ),
            (
                [
                    "--set-string",
                    "application.configData.RECURRING_ORDER_COHORT_MODE=all",
                ],
                "RECURRING_ORDER_COHORT_MODE must remain off",
            ),
            (
                [
                    "--set-string",
                    "application.configData.RECURRING_ORDER_COHORT_ALLOWLIST=customer@example.invalid",
                ],
                "RECURRING_ORDER_COHORT_ALLOWLIST must remain empty",
            ),
            (
                [
                    "--set-string",
                    "application.configData.RECURRING_REMINDERS_ENABLED=True",
                ],
                "RECURRING_REMINDERS_ENABLED must remain false",
            ),
            (
                [
                    "--set-string",
                    "application.configData.RECURRING_CHARGING_ENABLED=True",
                ],
                "RECURRING_CHARGING_ENABLED must remain false",
            ),
            (
                [
                    "--set-string",
                    "application.configData.RECURRING_CHARGE_EMERGENCY_STOP=False",
                ],
                "RECURRING_CHARGE_EMERGENCY_STOP must remain true",
            ),
            (
                [
                    "--set-string",
                    "application.configData.CELERY_TASK_ALWAYS_EAGER=True",
                ],
                "CELERY_TASK_ALWAYS_EAGER must remain false",
            ),
            (
                ["--set-string", "application.configData.CELERY_BROKER_URL="],
                "CELERY_BROKER_URL must select the same-release Redis billing broker",
            ),
            (
                [
                    "--set-string",
                    "application.configData.CELERY_BROKER_URL=redis://other:6379/1",
                ],
                "CELERY_BROKER_URL must select the same-release Redis billing broker",
            ),
            (
                ["--set-string", "recurringRuntime.schedule="],
                "recurringRuntime.schedule",
            ),
            (
                ["--set-string", "recurringRuntime.schedule=*/10 * * * *"],
                "recurringRuntime.schedule",
            ),
            (
                ["--set-string", "recurringRuntime.health.schedule="],
                "recurringRuntime.health.schedule",
            ),
            (
                [
                    "--set-string",
                    "recurringRuntime.health.schedule=3-59/5 * * * *",
                ],
                "recurringRuntime.health.schedule",
            ),
            (
                ["--set-string", "recurringRuntime.timeZone=America/Chicago"],
                "recurringRuntime.timeZone must be Etc/UTC",
            ),
            (
                ["--set-string", "recurringRuntime.concurrencyPolicy=Allow"],
                "recurringRuntime.concurrencyPolicy must be Forbid",
            ),
            (
                ["--set-string", "syncWaves.recurringPreflight=1"],
                "syncWaves.recurringPreflight must be after migrations",
            ),
            (
                ["--set-string", "syncWaves.recurringPreflight=3"],
                "syncWaves.recurringPreflight must be after migrations",
            ),
            (
                ["--set", "recurringRuntime.preflight.backoffLimit=1"],
                "recurringRuntime.preflight.backoffLimit must be 0",
            ),
        )
        self._assert_negative_cases(cases)

    def test_commands_and_schema_are_pinned(self) -> None:
        cases = (
            (
                [
                    "--set-json",
                    (
                        "recurringRuntime.command="
                        '["sh","-c","echo unsafe","ignored","ignored"]'
                    ),
                ],
                "recurringRuntime.command",
            ),
            (
                [
                    "--set-json",
                    (
                        "recurringRuntime.preflight.command="
                        '["python","manage.py","migrate","--noinput","unsafe"]'
                    ),
                ],
                "provider-free recurring runtime preflight",
            ),
            (
                [
                    "--set-json",
                    (
                        "recurringRuntime.health.command="
                        '["python","manage.py","check_recurring_runtime",'
                        '"--include-broker","--format=json","--no-fail"]'
                    ),
                ],
                "fail-closed recurring runtime health check",
            ),
            (
                ["--set", "billingWorker.replicas=0"],
                "billingWorker.replicas",
            ),
            (
                ["--set", "billingWorker.replicas=-1"],
                "billingWorker.replicas",
            ),
            (
                ["--set-string", "billingWorker.replicas=1"],
                "billingWorker.replicas",
            ),
        )
        self._assert_negative_cases(cases)

        schema = json.loads(
            (CHART_PATH / "values.schema.json").read_text(encoding="utf-8")
        )
        runtime = schema["properties"]["recurringRuntime"]
        self.assertEqual(
            runtime["properties"]["scheduler"]["enum"],
            ["kubernetes-cronjob"],
        )
        self.assertEqual(
            runtime["properties"]["schedule"]["const"],
            "*/5 * * * *",
        )
        self.assertEqual(
            runtime["properties"]["health"]["properties"]
            ["schedule"]["const"],
            "2-59/5 * * * *",
        )
        self.assertEqual(
            runtime["properties"]["command"]["const"],
            [
                "python",
                "manage.py",
                "tick_recurring_orders",
                "--scan-limit=100",
                "--dispatch-limit=100",
            ],
        )
        self.assertEqual(
            schema["properties"]["billingWorker"]["properties"]
            ["replicas"],
            {"type": "integer", "const": 1},
        )
        self.assertFalse(
            runtime["properties"]["preflight"]["additionalProperties"]
        )
        self.assertEqual(
            schema["properties"]["syncWaves"]["properties"]
            ["recurringPreflight"]["pattern"],
            "^-?[0-9]+$",
        )

    def test_runbook_preserves_inert_and_operator_gated_boundaries(self) -> None:
        contents = " ".join(RUNBOOK_PATH.read_text(encoding="utf-8").split())
        for required in (
            "disabled by default",
            "Never contact Stripe to validate this contract",
            "Chart rendering alone cannot make that source claim",
            "Never use Argo selective-resource sync",
            "sync the complete `citrus` or `citrus-dev` Application only",
            "Do not sync that change without explicit operator authorization",
            "remove exactly the stateless billing Deployment, preflight Job hook, tick CronJob, and health CronJob",
            "does not reverse already committed database state",
        ):
            with self.subTest(required=required):
                self.assertIn(required, contents)

    def _assert_negative_cases(
        self,
        cases: tuple[tuple[list[str], str], ...],
    ) -> None:
        for overrides, expected in cases:
            with self.subTest(expected=expected):
                result = _run(
                    [
                        *_command(development=True, runtime_enabled=True),
                        *overrides,
                    ]
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(expected, result.stderr)


class CitrusDormantDevActivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        application = YAML_PARSER.load(
            (REPO_ROOT / "argocd/applications/citrus-dev.yaml").read_text()
        )
        cls.value_files = application["spec"]["source"]["helm"]["valueFiles"]
        command = [
            "helm", "template",
            application["spec"]["source"]["helm"]["releaseName"],
            str(CHART_PATH), "--namespace",
            application["spec"]["destination"]["namespace"],
        ]
        cls.active_command = list(command)
        baseline_command = list(command)
        for filename in cls.value_files:
            cls.active_command.extend(["-f", str(CHART_PATH / filename)])
            if filename != "values-recurring-dev.yaml":
                baseline_command.extend(["-f", str(CHART_PATH / filename)])
        cls.active = {_key(item): item for item in _documents(cls.active_command)}
        cls.baseline = {_key(item): item for item in _documents(baseline_command)}
        cls.runtime_keys = {
            ("Deployment", "citrus-dev-billing-worker"),
            ("Job", "citrus-dev-recurring-preflight"),
            ("CronJob", "citrus-dev-recurring-tick"),
            ("CronJob", "citrus-dev-recurring-health"),
        }

    def test_actual_argo_overlay_is_in_the_ci_render_matrix(self) -> None:
        from scripts.check_citrus_recurring_runtime_render import _render_specs

        spec = next(item for item in _render_specs() if item.name == "citrus-runtime-dev")
        self.assertIn("values-recurring-dev.yaml", self.value_files)
        self.assertEqual(list(spec.value_files), self.value_files)
        self.assertEqual((spec.release, spec.namespace), ("citrus-dev", "citrus-dev"))

    def test_activation_and_rollback_only_change_four_stateless_resources(self) -> None:
        self.assertEqual(self.active.keys() - self.baseline.keys(), self.runtime_keys)
        self.assertFalse(self.baseline.keys() - self.active.keys())
        for key in self.baseline:
            with self.subTest(resource=key):
                self.assertEqual(self.active[key], self.baseline[key])

    def test_runtime_uses_the_actual_image_and_dormant_safety_contract(self) -> None:
        revision = YAML_PARSER.load(DEV_VALUES.read_text())["image"]["tag"]
        container_count = 0
        for key in self.runtime_keys:
            template = _pod_template(self.active[key])
            self.assertFalse(template["spec"]["automountServiceAccountToken"])
            for container in template["spec"]["containers"]:
                with self.subTest(resource=key, container=container["name"]):
                    env = _literal_env(container)
                    self.assertEqual(env[SOURCE_REVISION_ENV], revision)
                    self.assertEqual(container["image"].rsplit(":", 1)[1], revision)
                    self.assertEqual(env["RECURRING_RUNTIME_TOPOLOGY_REVISION"], "ces-850-dev-v1")
                    self.assertEqual(env["RECURRING_RUNTIME_SCHEDULER"], "kubernetes-cronjob")
                    self.assertEqual(env["PAYMENT_NETWORK_MODE"], "deny")
                    self.assertNotIn("CITRUS_STRIPE_SMOKE_RUNNER", env)
                    container_count += 1
        self.assertEqual(container_count, 5)
        config = self.active[("ConfigMap", "django-config")]["data"]
        for name, expected in {
            "RECURRING_ORDER_ENROLLMENT_MODE": "off",
            "RECURRING_ORDER_COHORT_MODE": "off",
            "RECURRING_REMINDERS_ENABLED": "False",
            "RECURRING_CHARGING_ENABLED": "False",
            "RECURRING_CHARGE_EMERGENCY_STOP": "True",
        }.items():
            self.assertEqual(config[name], expected)

    def test_actual_activation_cannot_enable_charges_or_skip_preflight(self) -> None:
        for flag, override, message in (
            ("--set-string", "application.configData.RECURRING_CHARGING_ENABLED=True", "RECURRING_CHARGING_ENABLED"),
            ("--set", "recurringRuntime.preflight.enabled=false", "preflight"),
        ):
            with self.subTest(override=override):
                result = _run([*self.active_command, flag, override])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)


if __name__ == "__main__":
    unittest.main()
