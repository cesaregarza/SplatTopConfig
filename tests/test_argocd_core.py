from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "argocd_core.py"
LOADER = SourceFileLoader("argocd_core", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None and SPEC.loader is not None
ARGO = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ARGO
SPEC.loader.exec_module(ARGO)


def completed(
    command: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class FakeRunner:
    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self.results = results
        self.commands: list[list[str]] = []
        self.kwargs: list[dict[str, object]] = []

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        self.kwargs.append(kwargs)
        if not self.results:
            raise AssertionError(f"unexpected command: {command}")
        return self.results.pop(0)


def application_json(
    *,
    resource_version: str = "10",
    reconciled_at: str = "2026-08-18T12:00:00.000000Z",
    sync: str = "Synced",
    health: str = "Healthy",
    revisions: tuple[str, ...] = ("a" * 40,),
    phase: str = "Succeeded",
    started_at: str = "old-start",
    finished_at: str = "old-finish",
    operation_revisions: tuple[str, ...] | None = None,
    automated: bool = False,
    info: tuple[tuple[str, str], ...] = (),
    resources: list[dict[str, object]] | None = None,
    strategy: str | None = "hook",
    selected_resources: bool = False,
    sync_options: tuple[str, ...] = (),
    pending: bool = False,
    refresh: str | None = None,
    conditions: tuple[str, ...] = (),
) -> str:
    operation_sync: dict[str, object] = {}
    operation_tuple = (
        operation_revisions if operation_revisions is not None else revisions
    )
    if len(operation_tuple) == 1:
        operation_sync["revision"] = operation_tuple[0]
    else:
        operation_sync["revisions"] = list(operation_tuple)
    if strategy is not None:
        operation_sync["syncStrategy"] = {strategy: {}}
    if selected_resources:
        operation_sync["resources"] = [{"kind": "ConfigMap", "name": "only-this"}]
    if sync_options:
        operation_sync["syncOptions"] = list(sync_options)
    operation = {
        "sync": operation_sync,
        "initiatedBy": {"automated": automated},
        "info": [{"name": name, "value": value} for name, value in info],
    }
    sync_result: dict[str, object] = {"resources": resources or []}
    if len(operation_tuple) == 1:
        sync_result["revision"] = operation_tuple[0]
    else:
        sync_result["revisions"] = list(operation_tuple)
    status_sync: dict[str, object] = {"status": sync}
    if len(revisions) == 1:
        status_sync["revision"] = revisions[0]
    else:
        status_sync["revisions"] = list(revisions)
    metadata: dict[str, object] = {"resourceVersion": resource_version}
    if refresh is not None:
        metadata["annotations"] = {ARGO.REFRESH_ANNOTATION: refresh}
    payload: dict[str, object] = {
        "metadata": metadata,
        "spec": {"syncPolicy": {}},
        "status": {
            "reconciledAt": reconciled_at,
            "sync": status_sync,
            "health": {"status": health},
            "conditions": [{"type": condition} for condition in conditions],
            "operationState": {
                "phase": phase,
                "startedAt": started_at,
                "finishedAt": finished_at,
                "operation": operation,
                "syncResult": sync_result,
            },
        },
    }
    if pending:
        payload["operation"] = operation
    return json.dumps(payload)


def old_operation() -> object:
    payload = json.loads(application_json())
    return ARGO.operation_snapshot(payload)


class ArgoCoreTests(unittest.TestCase):
    def test_cli_exposes_status_but_not_single_application_sync(self) -> None:
        with mock.patch.object(
            ARGO, "default_argocd_executable", return_value="argocd"
        ):
            parser = ARGO.build_parser()
        status = parser.parse_args(["status", "agent-control-plane"])
        self.assertEqual(status.operation, "status")
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["sync", "agent-control-plane"])

    def test_version_pin_accepts_exact_client_and_rejects_mismatch(self) -> None:
        valid = FakeRunner([completed([], stdout="argocd: v3.2.0+abcdef\n")])
        ARGO.validate_argocd_version("argocd", "3.2.0", runner=valid)
        invalid = FakeRunner([completed([], stdout="argocd: v3.1.9\n")])
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "does not match"):
            ARGO.validate_argocd_version("argocd", "3.2.0", runner=invalid)

    def test_temporary_kubeconfig_is_owner_only_namespaced_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config"
            source.write_text("original\n", encoding="utf-8")
            runner = FakeRunner(
                [
                    completed([], stdout="apiVersion: v1\nkind: Config\n"),
                    completed([]),
                    completed([], stdout="prod\n"),
                    completed([]),
                ]
            )
            with ARGO.core_kubeconfig(
                source,
                kubectl="kubectl",
                namespace="argocd",
                context="prod",
                runner=runner,
            ) as temporary:
                self.assertEqual(os.stat(temporary).st_mode & 0o777, 0o600)
                temporary_path = temporary
            self.assertFalse(temporary_path.exists())
            self.assertEqual(source.read_text(encoding="utf-8"), "original\n")

    def test_snapshot_preserves_multisource_operation_info_and_hook_results(
        self,
    ) -> None:
        revisions = ("1" * 40, "2" * 40, "3" * 40)
        resources = [
            {
                "kind": "Job",
                "namespace": "agent-control-plane",
                "name": "registry-overlay-restart-abc",
                "hookType": "PostSync",
                "syncPhase": "PostSync",
                "hookPhase": "Succeeded",
            }
        ]
        snapshot = ARGO.application_snapshot(
            json.loads(
                application_json(
                    revisions=revisions,
                    operation_revisions=revisions,
                    info=(("ces-395-run-id", "ces395-token"),),
                    resources=resources,
                )
            )
        )
        self.assertEqual(snapshot.revisions, revisions)
        self.assertEqual(snapshot.operation.revisions, revisions)
        self.assertIn(("ces-395-run-id", "ces395-token"), snapshot.operation.info)
        self.assertEqual(snapshot.operation.sync_strategy, "hook")
        self.assertEqual(snapshot.operation.resources[0].hook_type, "PostSync")

    def test_hard_refresh_uses_pinned_core_cli_and_returns_consumed_state(self) -> None:
        runner = FakeRunner(
            [completed([], stdout=application_json(resource_version="11"))]
        )
        _, snapshot = ARGO.hard_refresh_application(
            "agent-control-plane",
            kubeconfig=Path("/tmp/scoped"),
            argocd="/tools/argocd",
            timeout=180,
            runner=runner,
        )
        self.assertEqual(
            runner.commands[0],
            [
                "/tools/argocd",
                "--core",
                "app",
                "get",
                "agent-control-plane",
                "--hard-refresh",
                "--output",
                "json",
                "--timeout",
                "180",
            ],
        )
        self.assertEqual(runner.kwargs[0]["env"]["KUBECONFIG"], "/tmp/scoped")
        self.assertIsNone(snapshot.refresh_annotation)

    def test_single_source_sync_is_exact_full_hook_prune_with_run_token(self) -> None:
        runner = FakeRunner([completed([])])
        revision = "a" * 40
        ARGO.submit_sync(
            "agent-workloads",
            revisions=(revision,),
            run_id="ces395-run-token",
            kubeconfig=Path("/tmp/scoped"),
            argocd="argocd",
            runner=runner,
        )
        command = runner.commands[0]
        self.assertIn("--strategy", command)
        self.assertIn("hook", command)
        self.assertIn("--prune", command)
        self.assertIn("ces-395-run-id=ces395-run-token", command)
        self.assertEqual(command[-2:], ["--revision", revision])
        self.assertNotIn("--resource", command)
        self.assertNotIn("--apply-out-of-sync-only", command)

    def test_multisource_sync_pairs_every_exact_revision_with_position(self) -> None:
        runner = FakeRunner([completed([])])
        revisions = ("1" * 40, "2" * 40, "3" * 40)
        ARGO.submit_sync(
            "agent-control-plane",
            revisions=revisions,
            run_id="ces395-multi-token",
            kubeconfig=Path("/tmp/scoped"),
            argocd="argocd",
            runner=runner,
        )
        command = runner.commands[0]
        self.assertEqual(command.count("--revisions"), 3)
        self.assertEqual(command.count("--source-positions"), 3)
        self.assertEqual(
            command[-12:],
            [
                "--revisions",
                revisions[0],
                "--source-positions",
                "1",
                "--revisions",
                revisions[1],
                "--source-positions",
                "2",
                "--revisions",
                revisions[2],
                "--source-positions",
                "3",
            ],
        )
        self.assertNotIn("--revision", command)

    def test_poll_correlates_instant_terminal_manual_operation_by_run_token(
        self,
    ) -> None:
        revision = "a" * 40
        runner = FakeRunner(
            [
                completed(
                    [],
                    stdout=application_json(
                        resource_version="11",
                        started_at="same-second",
                        finished_at="same-second",
                        revisions=(revision,),
                        info=(("ces-395-run-id", "ces395-instant"),),
                    ),
                )
            ]
        )
        result = ARGO.poll_operation(
            "agent-workloads",
            before=old_operation(),
            expected_revisions=(revision,),
            expected_automated=False,
            run_id="ces395-instant",
            kubeconfig=Path("/tmp/scoped"),
            kubectl="kubectl",
            namespace="argocd",
            timeout=1,
            interval=0.01,
            runner=runner,
        )
        self.assertEqual(result.phase, "Succeeded")

    def test_poll_ignores_stale_and_unrelated_success_until_correlated_token(
        self,
    ) -> None:
        revision = "a" * 40
        runner = FakeRunner(
            [
                completed([], stdout=application_json(resource_version="10")),
                completed(
                    [],
                    stdout=application_json(
                        resource_version="11",
                        info=(("ces-395-run-id", "someone-else"),),
                    ),
                ),
                completed(
                    [],
                    stdout=application_json(
                        resource_version="12",
                        info=(("ces-395-run-id", "ces395-ours"),),
                    ),
                ),
            ]
        )
        ticks = iter([0.0, 0.1, 0.2, 0.3])
        result = ARGO.poll_operation(
            "agent-workloads",
            before=old_operation(),
            expected_revisions=(revision,),
            expected_automated=False,
            run_id="ces395-ours",
            kubeconfig=Path("/tmp/scoped"),
            kubectl="kubectl",
            namespace="argocd",
            timeout=1,
            interval=0.01,
            runner=runner,
            monotonic=lambda: next(ticks),
            sleeper=lambda _seconds: None,
        )
        self.assertIn(("ces-395-run-id", "ces395-ours"), result.info)

    def test_poll_rejects_previous_same_token_until_operation_is_new(self) -> None:
        revision = "a" * 40
        previous_payload = json.loads(
            application_json(info=(("ces-395-run-id", "ces395-repeat"),))
        )
        before = ARGO.operation_snapshot(previous_payload)
        runner = FakeRunner(
            [
                completed([], stdout=json.dumps(previous_payload)),
                completed(
                    [],
                    stdout=application_json(
                        resource_version="11",
                        started_at="new-start",
                        finished_at="new-finish",
                        info=(("ces-395-run-id", "ces395-repeat"),),
                    ),
                ),
            ]
        )
        ticks = iter([0.0, 0.1, 0.2])
        result = ARGO.poll_operation(
            "agent-workloads",
            before=before,
            expected_revisions=(revision,),
            expected_automated=False,
            run_id="ces395-repeat",
            kubeconfig=Path("/tmp/scoped"),
            kubectl="kubectl",
            namespace="argocd",
            timeout=1,
            interval=0.01,
            runner=runner,
            monotonic=lambda: next(ticks),
            sleeper=lambda _seconds: None,
        )
        self.assertEqual(result.started_at, "new-start")
        self.assertEqual(len(runner.commands), 2)

    def test_poll_rejects_replacement_after_manual_operation_was_correlated(
        self,
    ) -> None:
        runner = FakeRunner(
            [
                completed(
                    [],
                    stdout=application_json(
                        resource_version="11",
                        phase="Running",
                        info=(("ces-395-run-id", "ces395-ours"),),
                    ),
                ),
                completed(
                    [],
                    stdout=application_json(
                        resource_version="12",
                        phase="Succeeded",
                        info=(("ces-395-run-id", "ces395-other"),),
                    ),
                ),
            ]
        )
        ticks = iter([0.0, 0.1, 0.2])
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "was replaced"):
            ARGO.poll_operation(
                "agent-workloads",
                before=old_operation(),
                expected_revisions=("a" * 40,),
                expected_automated=False,
                run_id="ces395-ours",
                kubeconfig=Path("/tmp/scoped"),
                kubectl="kubectl",
                namespace="argocd",
                timeout=1,
                interval=0.01,
                runner=runner,
                monotonic=lambda: next(ticks),
                sleeper=lambda _seconds: None,
            )

    def test_poll_refuses_correlated_revision_drift(self) -> None:
        runner = FakeRunner(
            [
                completed(
                    [],
                    stdout=application_json(
                        resource_version="11",
                        revisions=("b" * 40,),
                        info=(("ces-395-run-id", "ces395-drift"),),
                    ),
                )
            ]
        )
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "revision drift"):
            ARGO.poll_operation(
                "agent-workloads",
                before=old_operation(),
                expected_revisions=("a" * 40,),
                expected_automated=False,
                run_id="ces395-drift",
                kubeconfig=Path("/tmp/scoped"),
                kubectl="kubectl",
                namespace="argocd",
                timeout=1,
                interval=0.01,
                runner=runner,
            )

    def test_poll_adopts_exact_automated_operation_after_hard_refresh(self) -> None:
        revision = "a" * 40
        runner = FakeRunner(
            [
                completed(
                    [],
                    stdout=application_json(
                        resource_version="11",
                        revisions=(revision,),
                        automated=True,
                        info=(),
                    ),
                )
            ]
        )
        result = ARGO.poll_operation(
            "agent-control-plane-registry-overlay",
            before=old_operation(),
            expected_revisions=(revision,),
            expected_automated=True,
            run_id=None,
            kubeconfig=Path("/tmp/scoped"),
            kubectl="kubectl",
            namespace="argocd",
            timeout=1,
            interval=0.01,
            runner=runner,
        )
        self.assertTrue(result.automated)
        self.assertEqual(result.revisions, (revision,))

    def test_poll_adopts_running_automated_operation_before_revisions_persist(
        self,
    ) -> None:
        revision = "a" * 40
        runner = FakeRunner(
            [
                completed(
                    [],
                    stdout=application_json(
                        resource_version="11",
                        phase="Running",
                        operation_revisions=(),
                        automated=True,
                    ),
                ),
                completed(
                    [],
                    stdout=application_json(
                        resource_version="12",
                        phase="Succeeded",
                        operation_revisions=(revision,),
                        automated=True,
                    ),
                ),
            ]
        )
        ticks = iter([0.0, 0.1, 0.2])
        result = ARGO.poll_operation(
            "agent-control-plane-registry-overlay",
            before=old_operation(),
            expected_revisions=(revision,),
            expected_automated=True,
            run_id=None,
            kubeconfig=Path("/tmp/scoped"),
            kubectl="kubectl",
            namespace="argocd",
            timeout=1,
            interval=0.01,
            runner=runner,
            monotonic=lambda: next(ticks),
            sleeper=lambda _seconds: None,
        )
        self.assertEqual(result.phase, "Succeeded")
        self.assertEqual(result.revisions, (revision,))

    def test_ready_requires_exact_multisource_tuple_and_no_pending_operation(
        self,
    ) -> None:
        revisions = ("1" * 40, "2" * 40, "3" * 40)
        runner = FakeRunner(
            [
                completed(
                    [], stdout=application_json(revisions=revisions, pending=True)
                ),
                completed(
                    [],
                    stdout=application_json(resource_version="11", revisions=revisions),
                ),
            ]
        )
        ticks = iter([0.0, 0.1, 0.2])
        result = ARGO.poll_application_ready(
            "agent-control-plane",
            expected_revisions=revisions,
            kubeconfig=Path("/tmp/scoped"),
            kubectl="kubectl",
            namespace="argocd",
            timeout=1,
            interval=0.01,
            runner=runner,
            monotonic=lambda: next(ticks),
            sleeper=lambda _seconds: None,
        )
        self.assertEqual(result.revisions, revisions)
        self.assertFalse(result.operation_present)


if __name__ == "__main__":
    unittest.main()
