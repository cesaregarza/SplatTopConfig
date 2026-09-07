from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ruamel.yaml import YAML

from scripts.update_citrus_release import (
    CapabilityReceipt,
    CitrusReleaseContractError,
    load_registry,
    update_release,
    validate_source_revision,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "helm" / "citrus" / "release-bindings.json"
SOURCE_REVISION = "a" * 40
OLD_PROD_REVISION = "b" * 40
OLD_DEV_REVISION = "c" * 40
CAPABILITIES = frozenset({"direct-order-payment-sweep"})
YAML_PARSER = YAML(typ="safe")


def _write_yaml(path: Path, document: object) -> None:
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    with path.open("w", encoding="utf-8") as destination:
        yaml.dump(document, destination)


def _load_yaml(path: Path) -> dict:
    return YAML_PARSER.load(path.read_text(encoding="utf-8"))


class SyntheticRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.chart = root / "helm" / "citrus"
        self.chart.mkdir(parents=True)
        self.registry = self.chart / "release-bindings.json"
        shutil.copyfile(REGISTRY_PATH, self.registry)
        self.values = self.chart / "values.yaml"
        self.dev_values = self.chart / "values-dev.yaml"
        self.dev_payment_values = self.chart / "values-payment-dev.yaml"
        self.dev_runtime_values = self.chart / "values-recurring-dev.yaml"
        self.prod_payment_values = self.chart / "values-payment-prod.yaml"
        _write_yaml(self.dev_runtime_values, {})
        _write_yaml(
            self.values,
            {
                "image": {
                    "repository": "registry.example/citrus",
                    "tag": OLD_PROD_REVISION,
                },
                "stripeSmokePromotion": {
                    "enabled": True,
                    "verifiedImageTag": OLD_PROD_REVISION,
                },
                "recurringRuntime": {
                    "enabled": False,
                    "expectedSourceRevision": OLD_PROD_REVISION,
                },
                "cloudflareAccess": {
                    "enabled": False,
                    "owner": "",
                    "secretName": "",
                    "verifiedImageTag": "",
                },
                "smsReconciliation": {
                    "enabled": False,
                    "suspend": True,
                    "secretName": "",
                    "verifiedImageTag": "",
                },
                "directOrderPaymentSweep": {
                    "enabled": False,
                    "suspend": True,
                    "runtimeSecretName": "",
                    "verifiedImageTag": "",
                    "offSessionMode": "legacy",
                },
                "paymentSafety": {
                    "networkMode": "",
                    "networkPolicy": {"additionalExternalEgress": []},
                },
            },
        )
        _write_yaml(
            self.dev_values,
            {
                "image": {"tag": OLD_DEV_REVISION},
                "stripeSmokePromotion": {"enabled": False},
                "recurringRuntime": {
                    "enabled": False,
                    "expectedSourceRevision": OLD_DEV_REVISION,
                },
                "smsReconciliation": {
                    "enabled": False,
                    "suspend": True,
                    "secretName": "",
                    "verifiedImageTag": "",
                },
                "paymentSafety": {
                    "enabled": True,
                    "networkMode": "deny",
                    "networkPolicy": {
                        "additionalExternalEgress": [],
                        "database": {"host": "db.dev.example"},
                    },
                },
            },
        )
        _write_yaml(
            self.dev_payment_values,
            {
                "paymentCredentials": {
                    "enabled": True,
                    "secretName": "citrus-dev-payment-credentials",
                    "webhookSecretName": "django-secrets",
                },
                "directOrderPaymentSweep": {
                    "enabled": True,
                    "suspend": False,
                    "runtimeSecretName": "citrus-dev-sweep-runtime",
                    "verifiedImageTag": OLD_DEV_REVISION,
                    "offSessionMode": "legacy",
                },
            },
        )
        _write_yaml(
            self.prod_payment_values,
            {
                "paymentCredentials": {
                    "enabled": True,
                    "secretName": "citrus-prod-payment-credentials",
                    "owner": "citrus",
                }
            },
        )

    @property
    def value_paths(self) -> tuple[Path, ...]:
        return (
            self.values,
            self.dev_values,
            self.dev_payment_values,
            self.dev_runtime_values,
            self.prod_payment_values,
        )

    def bytes(self) -> dict[Path, bytes]:
        return {path: path.read_bytes() for path in self.value_paths}

    def update(
        self,
        *,
        environment: str,
        revision: str = SOURCE_REVISION,
        capabilities: frozenset[str] = CAPABILITIES,
        receipt_revision: str | None = None,
        output: Path | None = None,
        install_file=os.replace,
    ):
        return update_release(
            repo_root=self.root,
            registry_path=self.registry,
            environment_name=environment,
            source_revision=revision,
            capability_receipt=CapabilityReceipt(
                receipt_revision or revision,
                capabilities,
            ),
            output_path_list=output or (self.root / "changed-paths"),
            install_file=install_file,
        )


class CitrusReleaseUpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = SyntheticRepository(Path(self.temporary.name))

    def test_dev_bump_updates_complete_tuple_and_only_operational_values(self) -> None:
        before = self.repo.bytes()
        before_dev = _load_yaml(self.repo.dev_values)
        before_payment = _load_yaml(self.repo.dev_payment_values)
        output = self.repo.root / "changed-paths"
        with (
            mock.patch(
                "socket.create_connection",
                side_effect=AssertionError("network access is forbidden"),
            ),
            mock.patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network access is forbidden"),
            ),
        ):
            result = self.repo.update(environment="dev", output=output)

        self.assertEqual(
            result.changed_paths,
            (
                Path("helm/citrus/values-dev.yaml"),
                Path("helm/citrus/values-payment-dev.yaml"),
            ),
        )
        self.assertEqual(
            output.read_text(encoding="utf-8"),
            "helm/citrus/values-dev.yaml\nhelm/citrus/values-payment-dev.yaml\n",
        )
        self.assertEqual(self.repo.values.read_bytes(), before[self.repo.values])
        self.assertEqual(
            self.repo.prod_payment_values.read_bytes(),
            before[self.repo.prod_payment_values],
        )

        dev = _load_yaml(self.repo.dev_values)
        payment = _load_yaml(self.repo.dev_payment_values)
        self.assertEqual(dev["image"]["tag"], SOURCE_REVISION)
        self.assertEqual(
            dev["recurringRuntime"]["expectedSourceRevision"], SOURCE_REVISION
        )
        self.assertEqual(
            payment["directOrderPaymentSweep"]["verifiedImageTag"],
            SOURCE_REVISION,
        )

        protected_dev = copy.deepcopy(dev)
        protected_dev["image"]["tag"] = OLD_DEV_REVISION
        protected_dev["recurringRuntime"]["expectedSourceRevision"] = OLD_DEV_REVISION
        self.assertEqual(protected_dev, before_dev)
        protected_payment = copy.deepcopy(payment)
        protected_payment["directOrderPaymentSweep"]["verifiedImageTag"] = (
            OLD_DEV_REVISION
        )
        self.assertEqual(protected_payment, before_payment)
        self.assertFalse(payment["directOrderPaymentSweep"]["suspend"])
        self.assertEqual(payment["directOrderPaymentSweep"]["offSessionMode"], "legacy")
        self.assertEqual(
            payment["paymentCredentials"]["secretName"],
            "citrus-dev-payment-credentials",
        )
        self.assertEqual(
            dev["paymentSafety"]["networkPolicy"]["additionalExternalEgress"],
            [],
        )

    def test_identical_second_dev_bump_is_an_operational_no_op(self) -> None:
        output = self.repo.root / "changed-paths"
        self.repo.update(environment="dev", output=output)
        before = self.repo.bytes()
        result = self.repo.update(environment="dev", output=output)
        self.assertEqual(result.changed_paths, ())
        self.assertEqual(self.repo.bytes(), before)
        self.assertEqual(output.read_bytes(), b"")

    def test_prod_bump_updates_only_applied_production_values(self) -> None:
        values = _load_yaml(self.repo.values)
        values["stripeSmokePromotion"]["enabled"] = False
        _write_yaml(self.repo.values, values)
        before = self.repo.bytes()
        before_values = _load_yaml(self.repo.values)
        output = self.repo.root / "changed-paths"
        result = self.repo.update(
            environment="prod", capabilities=frozenset(), output=output
        )
        self.assertEqual(result.changed_paths, (Path("helm/citrus/values.yaml"),))
        self.assertEqual(
            output.read_text(encoding="utf-8"), "helm/citrus/values.yaml\n"
        )
        self.assertEqual(
            self.repo.dev_values.read_bytes(), before[self.repo.dev_values]
        )
        self.assertEqual(
            self.repo.dev_payment_values.read_bytes(),
            before[self.repo.dev_payment_values],
        )
        self.assertEqual(
            self.repo.prod_payment_values.read_bytes(),
            before[self.repo.prod_payment_values],
        )
        values = _load_yaml(self.repo.values)
        self.assertEqual(values["image"]["tag"], SOURCE_REVISION)
        self.assertEqual(
            values["recurringRuntime"]["expectedSourceRevision"], SOURCE_REVISION
        )
        self.assertEqual(values["cloudflareAccess"]["verifiedImageTag"], "")
        self.assertEqual(values["smsReconciliation"]["verifiedImageTag"], "")
        self.assertEqual(values["directOrderPaymentSweep"]["verifiedImageTag"], "")
        protected = copy.deepcopy(values)
        protected["image"]["tag"] = OLD_PROD_REVISION
        protected["recurringRuntime"]["expectedSourceRevision"] = OLD_PROD_REVISION
        self.assertEqual(protected, before_values)

    def test_prod_bump_fails_closed_without_manual_smoke_attestation(self) -> None:
        before = self.repo.bytes()
        output = self.repo.root / "changed-paths"
        with self.assertRaisesRegex(
            CitrusReleaseContractError,
            "stripe-smoke-promotion-image requires manual attestation",
        ):
            self.repo.update(
                environment="prod", capabilities=frozenset(), output=output
            )
        self.assertEqual(self.repo.bytes(), before)
        self.assertFalse(output.exists())

    def test_malformed_revision_fails_before_any_write(self) -> None:
        before = self.repo.bytes()
        output = self.repo.root / "changed-paths"
        for revision in ("", "latest", "A" * 40, "a" * 39):
            with self.subTest(revision=revision):
                with self.assertRaisesRegex(
                    CitrusReleaseContractError, "40-character lowercase SHA"
                ):
                    self.repo.update(
                        environment="dev", revision=revision, output=output
                    )
                self.assertEqual(self.repo.bytes(), before)
                self.assertFalse(output.exists())

    def test_active_auto_roll_fails_without_source_capability(self) -> None:
        before = self.repo.bytes()
        output = self.repo.root / "changed-paths"
        with self.assertRaisesRegex(
            CitrusReleaseContractError,
            "requires source capability direct-order-payment-sweep",
        ):
            self.repo.update(environment="dev", capabilities=frozenset(), output=output)
        self.assertEqual(self.repo.bytes(), before)
        self.assertFalse(output.exists())

    def test_capability_receipt_for_another_revision_fails_before_write(self) -> None:
        before = self.repo.bytes()
        output = self.repo.root / "changed-paths"
        with self.assertRaisesRegex(
            CitrusReleaseContractError,
            "receipt revision must exactly match the release revision",
        ):
            self.repo.update(
                environment="dev",
                receipt_revision="d" * 40,
                output=output,
            )
        self.assertEqual(self.repo.bytes(), before)
        self.assertFalse(output.exists())

    def test_unknown_enabled_optional_binding_fails_closed(self) -> None:
        document = _load_yaml(self.repo.dev_payment_values)
        document["futureWorker"] = {
            "enabled": True,
            "verifiedImageTag": OLD_DEV_REVISION,
        }
        _write_yaml(self.repo.dev_payment_values, document)
        before = self.repo.bytes()
        output = self.repo.root / "changed-paths"
        with self.assertRaisesRegex(
            CitrusReleaseContractError,
            "unregistered optional image binding futureWorker.verifiedImageTag",
        ):
            self.repo.update(environment="dev", output=output)
        self.assertEqual(self.repo.bytes(), before)
        self.assertFalse(output.exists())

    def test_enabled_manual_attestation_binding_fails_closed(self) -> None:
        document = _load_yaml(self.repo.dev_values)
        document["smsReconciliation"]["enabled"] = True
        document["smsReconciliation"]["verifiedImageTag"] = OLD_DEV_REVISION
        _write_yaml(self.repo.dev_values, document)
        before = self.repo.bytes()
        output = self.repo.root / "changed-paths"
        with self.assertRaisesRegex(
            CitrusReleaseContractError,
            "sms-reconciliation-image requires manual attestation",
        ):
            self.repo.update(environment="dev", output=output)
        self.assertEqual(self.repo.bytes(), before)
        self.assertFalse(output.exists())

    def test_every_injected_install_failure_restores_every_original_byte(self) -> None:
        before = self.repo.bytes()
        output = self.repo.root / "changed-paths"
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at):
                calls = 0

                def fail_install(source: Path, destination: Path) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError("injected write failure")
                    os.replace(source, destination)

                with self.assertRaisesRegex(OSError, "injected write failure"):
                    self.repo.update(
                        environment="dev",
                        output=output,
                        install_file=fail_install,
                    )
                self.assertEqual(calls, fail_at)
                self.assertEqual(self.repo.bytes(), before)
                self.assertFalse(output.exists())

    def test_registry_rejects_unknown_environment_and_path_escape(self) -> None:
        output = self.repo.root / "changed-paths"
        with self.assertRaisesRegex(
            CitrusReleaseContractError, "unsupported Citrus environment"
        ):
            self.repo.update(environment="staging", output=output)

        registry = json.loads(self.repo.registry.read_text(encoding="utf-8"))
        registry["environments"]["dev"]["appliedValues"][0] = "../values.yaml"
        self.repo.registry.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(
            CitrusReleaseContractError, "exact release contract"
        ):
            self.repo.update(environment="dev", output=output)

    def test_registry_schema_typos_and_unsafe_optional_auto_roll_fail_closed(
        self,
    ) -> None:
        registry = json.loads(self.repo.registry.read_text(encoding="utf-8"))
        sweep = registry["bindings"][-1]
        sweep["requiresCapabilty"] = sweep.pop("requiresCapability")
        self.repo.registry.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(
            CitrusReleaseContractError,
            "bindings\\[5\\] contains unknown keys: requiresCapabilty",
        ):
            load_registry(self.repo.registry)

        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        registry["bindings"][-1].pop("requiresCapability")
        self.repo.registry.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(
            CitrusReleaseContractError,
            "optional auto-roll bindings must require a capability",
        ):
            load_registry(self.repo.registry)

    def test_registry_rejects_unknown_keys_and_environment_contract_drift(self) -> None:
        mutations = (
            (
                "root",
                lambda registry: registry.update({"futureSchema": True}),
                "binding registry contains unknown keys: futureSchema",
            ),
            (
                "environment",
                lambda registry: registry["environments"]["dev"].update(
                    {"futureValues": []}
                ),
                "environments.dev contains unknown keys: futureValues",
            ),
            (
                "operational-paths",
                lambda registry: registry["environments"]["dev"][
                    "operationalValues"
                ].insert(0, "helm/citrus/values.yaml"),
                "dev applied and operational values must match "
                "the exact release contract",
            ),
        )
        for name, mutate, message in mutations:
            with self.subTest(name=name):
                registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
                mutate(registry)
                self.repo.registry.write_text(json.dumps(registry), encoding="utf-8")
                with self.assertRaisesRegex(CitrusReleaseContractError, message):
                    load_registry(self.repo.registry)

    def test_source_revision_validator_is_exact(self) -> None:
        self.assertEqual(validate_source_revision(SOURCE_REVISION), SOURCE_REVISION)
        for value in (None, "", "latest", "A" * 40, "f" * 41):
            with self.subTest(value=value):
                with self.assertRaises(CitrusReleaseContractError):
                    validate_source_revision(value)


class CitrusReleaseRepositoryContractTests(unittest.TestCase):
    def test_release_tool_dependency_is_aged_exact_and_hash_locked(self) -> None:
        requirements = (
            REPO_ROOT / "scripts" / "requirements-citrus-release.txt"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            requirements,
            "ruamel.yaml==0.18.16 \\\n"
            "    --hash=sha256:048f26d64245bae57a4f9ef6feb5b552"
            "a386830ef7a826f235ffb804c59efbba\n",
        )

    def test_registry_matches_the_actual_argo_value_file_sets(self) -> None:
        registry = load_registry(REGISTRY_PATH)
        for environment, application in (
            ("dev", "citrus-dev.yaml"),
            ("prod", "citrus.yaml"),
        ):
            with self.subTest(environment=environment):
                document = _load_yaml(
                    REPO_ROOT / "argocd" / "applications" / application
                )
                argo_values = tuple(
                    Path("helm/citrus") / value
                    for value in document["spec"]["source"]["helm"]["valueFiles"]
                )
                self.assertEqual(
                    registry.environments[environment].applied_values,
                    argo_values,
                )

    def test_real_values_dev_plan_changes_only_operational_tuple_owners(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chart = root / "helm" / "citrus"
            chart.mkdir(parents=True)
            for name in (
                "release-bindings.json",
                "values.yaml",
                "values-dev.yaml",
                "values-payment-dev.yaml",
                "values-recurring-dev.yaml",
                "values-payment-prod.yaml",
            ):
                shutil.copyfile(REPO_ROOT / "helm" / "citrus" / name, chart / name)
            paths = {
                name: (chart / name).read_bytes()
                for name in (
                    "values.yaml",
                    "values-dev.yaml",
                    "values-payment-dev.yaml",
                    "values-recurring-dev.yaml",
                    "values-payment-prod.yaml",
                )
            }
            revision = "e" * 40
            output = root / "changed-paths"
            result = update_release(
                repo_root=root,
                registry_path=chart / "release-bindings.json",
                environment_name="dev",
                source_revision=revision,
                capability_receipt=CapabilityReceipt(revision, CAPABILITIES),
                output_path_list=output,
            )
            self.assertEqual(
                result.changed_paths,
                (
                    Path("helm/citrus/values-dev.yaml"),
                    Path("helm/citrus/values-payment-dev.yaml"),
                ),
            )
            self.assertEqual((chart / "values.yaml").read_bytes(), paths["values.yaml"])
            self.assertEqual(
                (chart / "values-recurring-dev.yaml").read_bytes(),
                paths["values-recurring-dev.yaml"],
            )
            self.assertEqual(
                (chart / "values-payment-prod.yaml").read_bytes(),
                paths["values-payment-prod.yaml"],
            )
            dev = _load_yaml(chart / "values-dev.yaml")
            payment = _load_yaml(chart / "values-payment-dev.yaml")
            self.assertIn(
                f"tag: {revision} # allow-latest",
                (chart / "values-dev.yaml").read_text(encoding="utf-8"),
            )
            self.assertEqual(dev["image"]["tag"], revision)
            self.assertEqual(
                dev["recurringRuntime"]["expectedSourceRevision"], revision
            )
            self.assertEqual(
                payment["directOrderPaymentSweep"]["verifiedImageTag"], revision
            )
            self.assertTrue(payment["directOrderPaymentSweep"]["enabled"])
            self.assertFalse(payment["directOrderPaymentSweep"]["suspend"])
            self.assertEqual(
                payment["directOrderPaymentSweep"]["runtimeSecretName"],
                "citrus-dev-sweep-runtime",
            )
            self.assertEqual(
                payment["directOrderPaymentSweep"]["offSessionMode"], "legacy"
            )
            self.assertEqual(
                dev["paymentSafety"]["networkPolicy"]["additionalExternalEgress"],
                [],
            )


if __name__ == "__main__":
    unittest.main()
