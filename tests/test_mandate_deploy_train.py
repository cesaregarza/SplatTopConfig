from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "mandate_deploy_train.py"
LOADER = SourceFileLoader("mandate_deploy_train", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None and SPEC.loader is not None
TRAIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TRAIN
SPEC.loader.exec_module(TRAIN)
ARGO = TRAIN.argo


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


def valid_skill_data(
    *, source_commit: str = "a" * 40, content: str = "# Skill\n"
) -> dict[str, str]:
    skill = {
        "id": "example",
        "version": "1.0.0",
        "path": "example.md",
        "digest": f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
        "requires": [],
    }
    descriptor = {
        "schema_version": "agent-control-plane-skill-bundle-content.v1",
        "skills": [skill],
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    manifest = {
        "schema_version": "agent-control-plane-skill-bundle.v1",
        "bundle_digest": digest,
        "source": {"repo": "cesaregarza/agent-workloads", "commit": source_commit},
        "skill_digests": {"example": skill["digest"]},
        "skills": [skill],
    }
    return {
        "manifest.json": json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        "example.md": content,
    }


def operation(
    *,
    resource_version: str = "11",
    phase: str = "Succeeded",
    revisions: tuple[str, ...] = ("a" * 40,),
    automated: bool = False,
    run_id: str | None = "ces395-test",
    resources: tuple[object, ...] = (),
    selected_resources: bool = False,
    strategy: str | None = "hook",
    sync_options: tuple[str, ...] = (),
) -> object:
    info = ((TRAIN.RUN_INFO_NAME, run_id),) if run_id is not None else ()
    return ARGO.OperationSnapshot(
        resource_version=resource_version,
        phase=phase,
        started_at="new-start",
        finished_at="new-finish",
        revisions=revisions,
        automated=automated,
        initiator=None,
        message="successfully synced",
        resources=resources,
        info=info,
        selected_resources=selected_resources,
        sync_strategy=strategy,
        sync_options=sync_options,
    )


def snapshot(
    *,
    revisions: tuple[str, ...] = ("a" * 40,),
    resource_version: str = "11",
    reconciled_at: str = "2026-08-18T12:00:01Z",
    sync: str = "Synced",
    health: str = "Healthy",
    op: object | None = None,
    pending: bool = False,
) -> object:
    return ARGO.ApplicationSnapshot(
        resource_version=resource_version,
        reconciled_at=reconciled_at,
        refresh_annotation=None,
        sync_status=sync,
        health_status=health,
        revisions=revisions,
        condition_types=(),
        operation=op or operation(revisions=revisions),
        operation_present=pending,
        automated_sync=False,
    )


def contracts(sha: str) -> dict[str, object]:
    return {
        name: TRAIN.ApplicationContract(
            name=name,
            identity={"name": name},
            resolved_revisions=(sha,),
            automated=False,
        )
        for name in (TRAIN.ROOT_APPLICATION, *TRAIN.MANAGED_APPLICATIONS)
    }


def ready_snapshots(sha: str) -> dict[str, object]:
    return {
        name: snapshot(revisions=(sha,))
        for name in (TRAIN.ROOT_APPLICATION, *TRAIN.MANAGED_APPLICATIONS)
    }


def journey_contracts() -> tuple[object, ...]:
    return (
        TRAIN.JourneyContract(
            "deployment-smoke",
            "mandate.deploy.smoke",
            "succeeded",
            ("run.created",),
        ),
        TRAIN.JourneyContract(
            "readonly-query-skill-digests",
            "agent_workloads.readonly_query",
            "succeeded",
            ("model_call.finished",),
        ),
    )


def valid_verify_report() -> str:
    return json.dumps(
        {
            "ok": True,
            "results": [
                {
                    "journey_id": "deployment-smoke",
                    "capability_id": "mandate.deploy.smoke",
                    "ok": True,
                    "stage": "complete",
                    "job_id": "job-1",
                    "status": "succeeded",
                    "message": "passed",
                    "callbacks": [],
                    "events": ["run.created"],
                },
                {
                    "journey_id": "readonly-query-skill-digests",
                    "capability_id": "agent_workloads.readonly_query",
                    "ok": True,
                    "stage": "complete",
                    "job_id": "job-2",
                    "status": "succeeded",
                    "message": "passed",
                    "callbacks": [],
                    "events": ["model_call.finished"],
                },
            ],
        }
    )


class MandateDeployTrainTests(unittest.TestCase):
    def test_canonical_plan_mechanically_prevents_missed_overlay(self) -> None:
        applications = TRAIN.validate_stage_plan()
        positions = {
            application: index for index, application in enumerate(applications)
        }
        self.assertLess(
            positions[TRAIN.OVERLAY_APPLICATION], positions["agent-control-plane"]
        )
        self.assertLess(
            positions[TRAIN.OVERLAY_APPLICATION], positions["agent-workloads"]
        )
        reordered = (
            TRAIN.STAGES[0],
            TRAIN.STAGES[1],
            TRAIN.STAGES[3],
            TRAIN.STAGES[2],
            TRAIN.STAGES[4],
        )
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "must precede"):
            TRAIN.validate_stage_plan(reordered)

    def test_authorization_requires_apply_and_exact_full_sha(self) -> None:
        sha = "a" * 40
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "requires --apply"):
            TRAIN.validate_authorization(False, sha)
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "exact full"):
            TRAIN.validate_authorization(True, "abc")
        self.assertEqual(TRAIN.validate_authorization(True, sha), sha)
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "exact production"):
            TRAIN.validate_production_context("some-other-context")
        self.assertEqual(
            TRAIN.validate_production_context(TRAIN.PRODUCTION_CONTEXT),
            TRAIN.PRODUCTION_CONTEXT,
        )

    def test_auto_sync_ref_submission_preserves_manual_immutable_pins(self) -> None:
        contracts = TRAIN.load_application_contracts(ROOT, "a" * 40)
        for contract in contracts.values():
            with self.subTest(application=contract.name):
                expected = ("main",) if contract.automated else contract.resolved_revisions
                self.assertEqual(TRAIN.submission_revisions(contract), expected)
                self.assertNotIn("main", contract.resolved_revisions)

    def test_explicitly_disabled_auto_sync_uses_manual_semantics(self) -> None:
        self.assertFalse(TRAIN._identity_automated({
            "spec": {"syncPolicy": {"automated": {"enabled": False}}}
        }))
        self.assertTrue(TRAIN._identity_automated({
            "spec": {"syncPolicy": {"automated": {"enabled": True}}}
        }))

    def test_application_contract_pins_all_garz_main_sources_and_multisource_order(
        self,
    ) -> None:
        sha = "a" * 40
        loaded = TRAIN.load_application_contracts(ROOT, sha)
        self.assertEqual(loaded[TRAIN.ROOT_APPLICATION].resolved_revisions, (sha,))
        self.assertEqual(
            loaded["agent-control-plane"].resolved_revisions,
            (
                "fa3afd59e3afe9e55c79387521bd6099da89f97e",
                sha,
                sha,
            ),
        )

    def test_live_application_identity_rejects_sync_option_drift(self) -> None:
        sha = "a" * 40
        contract = TRAIN.load_application_contracts(ROOT, sha)[
            TRAIN.OVERLAY_APPLICATION
        ]
        spec = json.loads(json.dumps(contract.identity["spec"]))
        spec["syncPolicy"]["syncOptions"].append("ApplyOutOfSyncOnly=true")
        payload = {
            "metadata": {"name": TRAIN.OVERLAY_APPLICATION},
            "spec": spec,
        }
        with self.assertRaisesRegex(
            ARGO.ArgoCoreError, r"fields=spec\.syncPolicy\.syncOptions"
        ):
            TRAIN.validate_live_application(contract, payload)

    def test_release_preflight_refuses_moving_remote_main(self) -> None:
        sha = "a" * 40
        runner = FakeRunner(
            [
                completed([], stdout=f"{ROOT}\n"),
                completed([], stdout=f"{sha}\n"),
                completed([], stdout=""),
                completed(
                    [], stdout="https://github.com/cesaregarza/GarzAICluster.git\n"
                ),
                completed([], stdout=f"{'b' * 40}\trefs/heads/main\n"),
            ]
        )
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "remote main moved"):
            TRAIN.validate_release_checkout(ROOT, sha, git="git", runner=runner)

    def test_remote_guard_refuses_noncanonical_origin_before_ls_remote(self) -> None:
        runner = FakeRunner([completed([], stdout="git@github.com:someone/fork.git\n")])
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "canonical origin mismatch"):
            TRAIN.validate_remote_main(
                ROOT,
                "a" * 40,
                git="git",
                runner=runner,
                stage="test",
            )
        self.assertEqual(len(runner.commands), 1)

    def test_digest_gate_names_mismatch_and_remint_before_any_argo_call(self) -> None:
        mismatch = "MANDATE_WORKLOAD_IDENTITY_TOKEN code_digest expected sha256:aaa got sha256:bbb"
        runner = FakeRunner(
            [
                completed([], stdout="release pin coherent"),
                completed([], returncode=1, stderr=mismatch),
            ]
        )
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "authorized remint"):
            TRAIN.run_drift_gates(ROOT, uv="uv", runner=runner)
        self.assertFalse(any("argocd" in command for command in runner.commands))
        self.assertIn("--check", runner.commands[1])

    def test_skill_bundle_digest_excludes_source_commit_but_covers_every_file(
        self,
    ) -> None:
        first = TRAIN.validate_skill_bundle_data(
            valid_skill_data(source_commit="a" * 40), "first"
        )
        second = TRAIN.validate_skill_bundle_data(
            valid_skill_data(source_commit="b" * 40), "second"
        )
        self.assertEqual(first.digest, second.digest)
        tampered = valid_skill_data()
        tampered["example.md"] = "changed"
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "content digest mismatch"):
            TRAIN.validate_skill_bundle_data(tampered, "tampered")

    def test_skill_bundle_refuses_extra_configmap_data_key(self) -> None:
        data = valid_skill_data()
        data["stale.md"] = "stale"
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "key set drifted"):
            TRAIN.validate_skill_bundle_data(data, "live")

    def test_skill_image_guard_refuses_tag_digest_movement(self) -> None:
        digest = "sha256:" + "a" * 64
        desired = TRAIN.SkillBundle(
            valid_skill_data(),
            "sha256:" + "b" * 64,
            1,
            "c" * 40,
            digest,
            f"registry.example/skills@{digest}",
        )
        stable = FakeRunner([completed([], stdout=f"{digest}\n")])
        TRAIN.recheck_skill_image_digest(desired, crane="crane", runner=stable)
        moved = FakeRunner([completed([], stdout=f"sha256:{'d' * 64}\n")])
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "tag-moved"):
            TRAIN.recheck_skill_image_digest(desired, crane="crane", runner=moved)

    def test_hard_refresh_requires_observed_reconciled_and_exact_revision(self) -> None:
        before = snapshot(resource_version="10", reconciled_at="2026-08-18T12:00:00Z")
        cached = snapshot(resource_version="11", reconciled_at="2026-08-18T12:00:00Z")
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "reconciled-at-not-advanced"):
            TRAIN.validate_hard_refresh("app", before, cached, ("a" * 40,))
        drift = snapshot(revisions=("b" * 40,))
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "revision-drift"):
            TRAIN.validate_hard_refresh("app", before, drift, ("a" * 40,))

    def test_overlay_requires_both_exact_persisted_hook_results(self) -> None:
        rollout = ARGO.OperationResource(
            "Job",
            "agent-control-plane",
            "registry-overlay-rollout-strategy-abc",
            "Sync",
            "Sync",
            "Succeeded",
            None,
            None,
        )
        restart = ARGO.OperationResource(
            "Job",
            "agent-control-plane",
            "registry-overlay-restart-def",
            "PostSync",
            "PostSync",
            "Succeeded",
            None,
            None,
        )
        TRAIN.assert_overlay_hooks(operation(resources=(rollout, restart)))
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "hook-evidence-missing"):
            TRAIN.assert_overlay_hooks(operation(resources=(rollout,)))

    def test_full_hook_assertion_rejects_selective_and_skills_apply_out_of_sync(
        self,
    ) -> None:
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "selective-sync-refused"):
            TRAIN.assert_full_hook_operation(
                TRAIN.OVERLAY_APPLICATION, operation(selected_resources=True)
            )
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "apply-out-of-sync-only"):
            TRAIN.assert_full_hook_operation(
                TRAIN.SKILLS_APPLICATION,
                operation(sync_options=("ApplyOutOfSyncOnly=true",)),
            )

    def test_force_mode_allows_passive_settlement_and_running_exact_auto(self) -> None:
        baseline_operation = operation(resource_version="10")
        progressing = snapshot(
            resource_version="10",
            health="Progressing",
            op=baseline_operation,
        )
        healthy = snapshot(
            resource_version="11",
            health="Healthy",
            op=baseline_operation,
        )
        self.assertFalse(
            TRAIN.assert_preflight_state_unchanged(
                "app",
                progressing,
                healthy,
                force_sync=True,
                expected_revisions=("a" * 40,),
            )
        )
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "preflight-state-moved"):
            TRAIN.assert_preflight_state_unchanged(
                "app",
                progressing,
                healthy,
                force_sync=False,
                expected_revisions=("a" * 40,),
            )
        running = snapshot(
            resource_version="12",
            op=operation(
                resource_version="12",
                phase="Running",
                revisions=(),
                automated=True,
                run_id=None,
            ),
            pending=True,
        )
        self.assertTrue(
            TRAIN.assert_preflight_state_unchanged(
                "app",
                progressing,
                running,
                force_sync=True,
                expected_revisions=("a" * 40,),
            )
        )

    def test_verify_budget_rejects_short_and_oversized_values(self) -> None:
        for timeout in (1.0, 481.0, 3600.0):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(
                    ARGO.ArgoCoreError, "exactly match the bounded"
                ):
                    TRAIN.validate_verify_timeout(timeout)

    def test_verify_job_patch_preserves_spec_and_only_changes_named_command_args(
        self,
    ) -> None:
        template = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "old", "labels": {"keep": "yes"}},
            "spec": {
                "backoffLimit": 1,
                "activeDeadlineSeconds": 480,
                "ttlSecondsAfterFinished": 300,
                "template": {
                    "spec": {
                        "serviceAccountName": "agent-control-plane",
                        "containers": [
                            {
                                "name": TRAIN.VERIFY_CONTAINER,
                                "image": "image@sha256:abc",
                                "command": ["mandate-synthetic-live-verify"],
                                "args": ["--http-timeout-seconds", "15"],
                                "env": [
                                    {
                                        "name": "TOKEN",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": "runtime",
                                                "key": "TOKEN",
                                            }
                                        },
                                    }
                                ],
                                "resources": {"requests": {"cpu": "10m"}},
                            }
                        ],
                        "volumes": [{"name": "tmp", "emptyDir": {}}],
                    }
                },
            },
        }
        patched = TRAIN.build_verify_job_payload(
            template, "verify-new", "ces395-verify-new"
        )
        container = patched["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["mandate", "verify"])
        self.assertEqual(
            container["args"],
            ["--http-timeout-seconds", "15", "--format", "json"],
        )
        self.assertEqual(patched["spec"]["activeDeadlineSeconds"], 480)
        self.assertEqual(
            container["env"],
            template["spec"]["template"]["spec"]["containers"][0]["env"],
        )
        self.assertEqual(template["metadata"]["name"], "old")
        self.assertEqual(
            patched["metadata"]["labels"][TRAIN.RUN_INFO_NAME],
            "ces395-verify-new",
        )

    def test_verify_overlap_detects_unlabeled_owned_pending_job(self) -> None:
        jobs = {
            "items": [
                {
                    "metadata": {
                        "name": f"{TRAIN.VERIFY_CRONJOB}-12345678",
                        "ownerReferences": [
                            {"kind": "CronJob", "name": TRAIN.VERIFY_CRONJOB}
                        ],
                    },
                    "status": {},
                }
            ]
        }
        runner = FakeRunner([completed([], stdout=json.dumps(jobs))])
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "nonterminal-job-overlap"):
            TRAIN.ensure_no_active_verify_job(
                kubeconfig=Path("/tmp/scoped"),
                kubectl="kubectl",
                runner=runner,
            )
        self.assertNotIn("--selector", runner.commands[0])

    def test_verify_parser_accepts_exact_complete_journeys_and_event(self) -> None:
        report = TRAIN.parse_verify_report(valid_verify_report(), journey_contracts())
        self.assertTrue(report["ok"])

    def test_verify_parser_refuses_invalid_json_config_failure_and_missing_event(
        self,
    ) -> None:
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "invalid-json"):
            TRAIN.parse_verify_report("not-json", journey_contracts())
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "verify_stage=config"):
            TRAIN.parse_verify_report(
                json.dumps({"ok": False, "stage": "config", "message": "bad"}),
                journey_contracts(),
            )
        missing = json.loads(valid_verify_report())
        missing["results"][1]["events"] = []
        with self.assertRaisesRegex(
            ARGO.ArgoCoreError, "missing events=model_call.finished"
        ):
            TRAIN.parse_verify_report(json.dumps(missing), journey_contracts())

    def test_verify_parser_refuses_missing_journey_named_stage_and_status(self) -> None:
        missing = json.loads(valid_verify_report())
        missing["results"].pop()
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "journey set mismatch"):
            TRAIN.parse_verify_report(json.dumps(missing), journey_contracts())
        failed = json.loads(valid_verify_report())
        failed["results"][0]["stage"] = "audit"
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "did not PASS stage=complete"):
            TRAIN.parse_verify_report(json.dumps(failed), journey_contracts())
        status = json.loads(valid_verify_report())
        status["results"][0]["status"] = "failed"
        with self.assertRaisesRegex(ARGO.ArgoCoreError, "status mismatch"):
            TRAIN.parse_verify_report(json.dumps(status), journey_contracts())

    def test_mandate_verify_clones_template_runs_landed_cli_and_parses_json(
        self,
    ) -> None:
        template = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "rendered"},
            "spec": {
                "activeDeadlineSeconds": 480,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": TRAIN.VERIFY_CONTAINER,
                                "command": ["mandate-synthetic-live-verify"],
                                "args": ["--http-timeout-seconds", "15"],
                                "env": [{"name": "PRESERVED", "value": "yes"}],
                            }
                        ]
                    }
                },
            },
        }
        pods = {
            "items": [
                {
                    "metadata": {
                        "name": "verify-pod",
                        "creationTimestamp": "2026-08-18T12:00:01Z",
                    },
                    "status": {"phase": "Succeeded"},
                }
            ]
        }
        runner = FakeRunner(
            [
                completed([], stdout=json.dumps({"items": []})),
                completed([], stdout=json.dumps(template)),
                completed([]),
                completed([]),
                completed([], stdout=json.dumps(pods)),
                completed([], stdout=valid_verify_report()),
            ]
        )
        output = io.StringIO()
        with redirect_stdout(output):
            job = TRAIN.run_mandate_verify(
                journey_contracts(),
                kubeconfig=Path("/tmp/scoped"),
                kubectl="kubectl",
                timeout=480,
                runner=runner,
                now=lambda: TRAIN.datetime(
                    2026, 8, 18, 12, 34, 56, tzinfo=TRAIN.timezone.utc
                ),
                run_id="ces395-verifytest",
            )
        self.assertTrue(
            job.startswith("agent-control-plane-postdeploy-20260818123456-")
        )
        self.assertIn(
            "--from=cronjob/agent-control-plane-synthetic-live-verify",
            runner.commands[1],
        )
        created = json.loads(str(runner.kwargs[2]["input"]))
        container = created["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["mandate", "verify"])
        self.assertEqual(container["args"][-2:], ["--format", "json"])
        self.assertEqual(
            created["metadata"]["labels"][TRAIN.RUN_INFO_NAME],
            "ces395-verifytest",
        )
        self.assertNotIn(valid_verify_report(), output.getvalue())
        receipts = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(receipts[-1]["result"], "succeeded")

    def test_mandate_verify_timeout_is_stage_named_and_stops_before_logs(self) -> None:
        template = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "rendered"},
            "spec": {
                "activeDeadlineSeconds": 480,
                "template": {
                    "spec": {
                        "containers": [
                            {"name": TRAIN.VERIFY_CONTAINER, "command": ["old"]}
                        ]
                    }
                },
            },
        }
        runner = FakeRunner(
            [
                completed([], stdout=json.dumps({"items": []})),
                completed([], stdout=json.dumps(template)),
                completed([]),
                completed([], returncode=1, stderr="timed out"),
            ]
        )
        with self.assertRaisesRegex(
            ARGO.ArgoCoreError, "stage=mandate-verify phase=wait"
        ):
            TRAIN.run_mandate_verify(
                journey_contracts(),
                kubeconfig=Path("/tmp/scoped"),
                kubectl="kubectl",
                timeout=480,
                runner=runner,
                run_id="ces395-timeout",
            )
        self.assertEqual(len(runner.commands), 4)

    def test_reconcile_validates_observed_hard_refresh_before_submit_sync(self) -> None:
        events: list[str] = []
        desired = TRAIN.validate_skill_bundle_data(valid_skill_data(), "desired")
        before = snapshot(resource_version="10", reconciled_at="2026-08-18T12:00:00Z")
        refreshed = snapshot(
            resource_version="11", reconciled_at="2026-08-18T12:00:01Z"
        )
        contract = TRAIN.load_application_contracts(ROOT, "a" * 40)[TRAIN.ROOT_APPLICATION]
        validate_hard_refresh = TRAIN.validate_hard_refresh

        def record_hard_refresh(
            *args: object, **kwargs: object
        ) -> tuple[object, object]:
            events.append("hard-refresh")
            return {}, refreshed

        def record_validation(*args: object, **kwargs: object) -> None:
            events.append("validate-hard-refresh")
            validate_hard_refresh(*args, **kwargs)

        def record_submit(*args: object, **kwargs: object) -> None:
            self.assertEqual(kwargs["revisions"], ("main",))
            events.append("submit-sync")

        with (
            mock.patch.object(TRAIN.argo, "read_application_payload", return_value={}),
            mock.patch.object(
                TRAIN, "validate_live_application", side_effect=[before, refreshed, refreshed]
            ),
            mock.patch.object(
                TRAIN.argo,
                "hard_refresh_application",
                side_effect=record_hard_refresh,
            ),
            mock.patch.object(
                TRAIN, "validate_hard_refresh", side_effect=record_validation
            ),
            mock.patch.object(TRAIN, "_adopt_automated_operation", return_value=None),
            mock.patch.object(
                TRAIN.argo, "read_application_snapshot", return_value=refreshed
            ),
            mock.patch.object(TRAIN.argo, "submit_sync", side_effect=record_submit),
            mock.patch.object(TRAIN.argo, "poll_operation", return_value=operation()) as poll,
            mock.patch.object(
                TRAIN.argo, "poll_application_ready", return_value=refreshed
            ),
            redirect_stdout(io.StringIO()),
        ):
            result = TRAIN.reconcile_application(
                contract,
                stage="control-plane",
                invocation_id="ces395-test",
                desired_skills=desired,
                preflight_snapshot=before,
                preflight_skill_digest=None,
                force_sync=True,
                kubeconfig=Path("/tmp/scoped"),
                argocd="argocd",
                kubectl="kubectl",
                namespace="argocd",
                refresh_timeout=1,
                operation_timeout=1,
                adoption_timeout=0,
                interval=0.01,
                revision_guard=lambda: events.append("revision-guard"),
            )

        self.assertEqual(result, "manual")
        self.assertEqual(
            events,
            ["hard-refresh", "validate-hard-refresh", "revision-guard", "submit-sync", "revision-guard"],
        )
        self.assertEqual(poll.call_args.kwargs["expected_revisions"], ("a" * 40,))
        self.assertIs(poll.call_args.kwargs["expected_automated"], False)

    def test_sync_boundary_drift_refuses_submission_or_final_acceptance(self) -> None:
        for failure in ("spec", "before-submit", "after-completion"):
            with self.subTest(failure=failure):
                current = snapshot()
                contract = TRAIN.load_application_contracts(ROOT, "a" * 40)[TRAIN.ROOT_APPLICATION]
                error = ARGO.ArgoCoreError("release drift")
                validations = [current, current, error if failure == "spec" else current]
                guard = mock.Mock(side_effect=[None, error] if failure == "after-completion" else
                                  [error] if failure == "before-submit" else None)
                with (
                    mock.patch.object(TRAIN.argo, "read_application_payload", return_value={}),
                    mock.patch.object(TRAIN, "validate_live_application", side_effect=validations),
                    mock.patch.object(TRAIN.argo, "hard_refresh_application", return_value=({}, current)),
                    mock.patch.object(TRAIN, "validate_hard_refresh"),
                    mock.patch.object(TRAIN, "_adopt_automated_operation", return_value=None),
                    mock.patch.object(TRAIN.argo, "read_application_snapshot", return_value=current),
                    mock.patch.object(TRAIN.argo, "submit_sync") as submit,
                    mock.patch.object(TRAIN.argo, "poll_operation", return_value=operation()),
                    mock.patch.object(TRAIN.argo, "poll_application_ready") as ready,
                    redirect_stdout(io.StringIO()),
                ):
                    with self.assertRaisesRegex(ARGO.ArgoCoreError, "release drift"):
                        TRAIN.reconcile_application(
                            contract,
                            stage="application-specs",
                            invocation_id="ces395-test",
                            desired_skills=TRAIN.validate_skill_bundle_data(valid_skill_data(), "desired"),
                            preflight_snapshot=current,
                            preflight_skill_digest=None,
                            force_sync=True,
                            kubeconfig=Path("/tmp/scoped"),
                            argocd="argocd",
                            kubectl="kubectl",
                            namespace="argocd",
                            refresh_timeout=1,
                            operation_timeout=1,
                            adoption_timeout=0,
                            interval=0.01,
                            revision_guard=guard,
                        )
                    self.assertEqual(submit.call_count, int(failure == "after-completion"))
                    ready.assert_not_called()

    def test_semantic_skill_drift_forces_sync_even_when_argo_is_synced(self) -> None:
        desired = TRAIN.validate_skill_bundle_data(valid_skill_data(), "desired")
        live_drifted = TRAIN.SkillBundle({}, "sha256:" + "b" * 64, 1, "c" * 40)
        current = snapshot()
        contract = TRAIN.ApplicationContract(
            TRAIN.SKILLS_APPLICATION,
            {"name": TRAIN.SKILLS_APPLICATION},
            ("a" * 40,),
            False,
        )
        with (
            mock.patch.object(TRAIN.argo, "read_application_payload", return_value={}),
            mock.patch.object(
                TRAIN,
                "validate_live_application",
                side_effect=[current, current, current],
            ),
            mock.patch.object(
                TRAIN.argo,
                "hard_refresh_application",
                return_value=({}, current),
            ),
            mock.patch.object(TRAIN, "validate_hard_refresh"),
            mock.patch.object(
                TRAIN,
                "read_live_skill_bundle",
                side_effect=[live_drifted, live_drifted, desired],
            ),
            mock.patch.object(TRAIN, "_adopt_automated_operation", return_value=None),
            mock.patch.object(
                TRAIN.argo, "read_application_snapshot", return_value=current
            ),
            mock.patch.object(TRAIN.argo, "submit_sync") as submit,
            mock.patch.object(TRAIN.argo, "poll_operation", return_value=operation()),
            mock.patch.object(
                TRAIN.argo, "poll_application_ready", return_value=current
            ),
            redirect_stdout(io.StringIO()),
        ):
            result = TRAIN.reconcile_application(
                contract,
                stage="skills",
                invocation_id="ces395-test",
                desired_skills=desired,
                preflight_snapshot=current,
                preflight_skill_digest=live_drifted.digest,
                force_sync=True,
                kubeconfig=Path("/tmp/scoped"),
                argocd="argocd",
                kubectl="kubectl",
                namespace="argocd",
                refresh_timeout=1,
                operation_timeout=1,
                adoption_timeout=0,
                interval=0.01,
                revision_guard=lambda: None,
            )
        self.assertEqual(result, "manual")
        submit.assert_called_once()

    def test_late_hard_refresh_drift_refuses_lone_application_sync(self) -> None:
        desired = TRAIN.validate_skill_bundle_data(valid_skill_data(), "desired")
        before = snapshot(resource_version="10")
        refreshed = snapshot(
            resource_version="12",
            reconciled_at="2026-08-18T12:00:02Z",
            sync="OutOfSync",
        )
        contract = TRAIN.ApplicationContract(
            "agent-control-plane",
            {"name": "agent-control-plane"},
            ("a" * 40,),
            False,
        )
        with (
            mock.patch.object(TRAIN.argo, "read_application_payload", return_value={}),
            mock.patch.object(
                TRAIN, "validate_live_application", side_effect=[before, refreshed, refreshed]
            ),
            mock.patch.object(
                TRAIN.argo,
                "hard_refresh_application",
                return_value=({}, refreshed),
            ),
            mock.patch.object(TRAIN, "validate_hard_refresh"),
            mock.patch.object(TRAIN, "_adopt_automated_operation", return_value=None),
            mock.patch.object(
                TRAIN.argo, "read_application_snapshot", return_value=refreshed
            ),
            mock.patch.object(TRAIN.argo, "submit_sync") as submit,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(TRAIN.LateDriftDetected, "late-hard-refresh"):
                TRAIN.reconcile_application(
                    contract,
                    stage="control-plane",
                    invocation_id="ces395-test",
                    desired_skills=desired,
                    preflight_snapshot=before,
                    preflight_skill_digest=None,
                    force_sync=False,
                    kubeconfig=Path("/tmp/scoped"),
                    argocd="argocd",
                    kubectl="kubectl",
                    namespace="argocd",
                    refresh_timeout=1,
                    operation_timeout=1,
                    adoption_timeout=0,
                    interval=0.01,
                    revision_guard=lambda: None,
                )
        submit.assert_not_called()

    def test_any_preflight_drift_forces_full_canonical_replay(self) -> None:
        sha = "a" * 40
        bundle = TRAIN.validate_skill_bundle_data(valid_skill_data(), "desired")
        snapshots = ready_snapshots(sha)
        snapshots["agent-control-plane-secrets"] = snapshot(
            revisions=(sha,), sync="OutOfSync"
        )
        with (
            mock.patch.object(TRAIN, "validate_release_checkout"),
            mock.patch.object(
                TRAIN, "load_application_contracts", return_value=contracts(sha)
            ),
            mock.patch.object(
                TRAIN, "load_journey_contracts", return_value=journey_contracts()
            ),
            mock.patch.object(TRAIN, "run_drift_gates"),
            mock.patch.object(TRAIN, "read_desired_skill_bundle", return_value=bundle),
            mock.patch.object(
                TRAIN, "preflight_live_applications", return_value=snapshots
            ),
            mock.patch.object(TRAIN, "read_live_skill_bundle", return_value=bundle),
            mock.patch.object(TRAIN, "preflight_mandate_verify"),
            mock.patch.object(TRAIN, "validate_remote_main"),
            mock.patch.object(TRAIN, "recheck_skill_image_digest"),
            mock.patch.object(
                TRAIN, "reconcile_application", return_value="manual"
            ) as reconcile,
            mock.patch.object(TRAIN, "run_mandate_verify") as verify,
            redirect_stdout(io.StringIO()),
        ):
            result = TRAIN.run_deploy_train(
                repo_root=ROOT,
                release_sha=sha,
                kubeconfig=Path("/tmp/scoped"),
                argocd="argocd",
                kubectl="kubectl",
                uv="uv",
                git="git",
                crane="crane",
                namespace="argocd",
                refresh_timeout=1,
                operation_timeout=1,
                adoption_timeout=0,
                verify_timeout=480,
                interval=0.01,
                invocation_id="ces395-test",
            )
        self.assertEqual(result, "succeeded")
        self.assertEqual(reconcile.call_count, 1 + len(TRAIN.MANAGED_APPLICATIONS))
        self.assertTrue(
            all(call.kwargs["force_sync"] is True for call in reconcile.call_args_list)
        )
        self.assertEqual(
            [call.args[0].name for call in reconcile.call_args_list],
            [TRAIN.ROOT_APPLICATION, *TRAIN.MANAGED_APPLICATIONS],
        )
        verify.assert_called_once()

    def test_late_control_plane_drift_restarts_full_train_with_overlay(self) -> None:
        sha = "a" * 40
        bundle = TRAIN.validate_skill_bundle_data(valid_skill_data(), "desired")
        late = TRAIN.LateDriftDetected("late CP drift")
        first_pass = ["skipped"] * 5 + [late]
        second_pass = ["manual"] * (1 + len(TRAIN.MANAGED_APPLICATIONS))
        with (
            mock.patch.object(TRAIN, "validate_release_checkout"),
            mock.patch.object(
                TRAIN, "load_application_contracts", return_value=contracts(sha)
            ),
            mock.patch.object(
                TRAIN, "load_journey_contracts", return_value=journey_contracts()
            ),
            mock.patch.object(TRAIN, "run_drift_gates"),
            mock.patch.object(TRAIN, "read_desired_skill_bundle", return_value=bundle),
            mock.patch.object(
                TRAIN,
                "preflight_live_applications",
                return_value=ready_snapshots(sha),
            ),
            mock.patch.object(TRAIN, "read_live_skill_bundle", return_value=bundle),
            mock.patch.object(TRAIN, "preflight_mandate_verify"),
            mock.patch.object(TRAIN, "validate_remote_main"),
            mock.patch.object(TRAIN, "recheck_skill_image_digest"),
            mock.patch.object(
                TRAIN,
                "reconcile_application",
                side_effect=[*first_pass, *second_pass],
            ) as reconcile,
            mock.patch.object(TRAIN, "run_mandate_verify") as verify,
            redirect_stdout(io.StringIO()),
        ):
            result = TRAIN.run_deploy_train(
                repo_root=ROOT,
                release_sha=sha,
                kubeconfig=Path("/tmp/scoped"),
                argocd="argocd",
                kubectl="kubectl",
                uv="uv",
                git="git",
                crane="crane",
                namespace="argocd",
                refresh_timeout=1,
                operation_timeout=1,
                adoption_timeout=0,
                verify_timeout=480,
                interval=0.01,
                invocation_id="ces395-test",
            )
        self.assertEqual(result, "succeeded")
        calls = reconcile.call_args_list
        self.assertEqual(len(calls), 6 + 1 + len(TRAIN.MANAGED_APPLICATIONS))
        restart_calls = calls[6:]
        self.assertEqual(
            [call.args[0].name for call in restart_calls],
            [TRAIN.ROOT_APPLICATION, *TRAIN.MANAGED_APPLICATIONS],
        )
        self.assertTrue(all(call.kwargs["force_sync"] for call in restart_calls))
        restart_names = [call.args[0].name for call in restart_calls]
        self.assertLess(
            restart_names.index(TRAIN.OVERLAY_APPLICATION),
            restart_names.index("agent-control-plane"),
        )
        verify.assert_called_once()

    def test_early_overlay_auto_sync_is_settled_then_manually_replayed(self) -> None:
        revision = "a" * 40
        baseline = snapshot(
            resource_version="10",
            op=operation(resource_version="10", run_id=None),
        )
        early_operation = operation(resource_version="12", automated=True, run_id=None)
        early = snapshot(resource_version="12", op=early_operation)
        refreshed = snapshot(
            resource_version="13",
            reconciled_at="2026-08-18T12:00:02Z",
            op=early_operation,
        )
        rollout = ARGO.OperationResource(
            "Job",
            "agent-control-plane",
            "registry-overlay-rollout-strategy-force",
            "Sync",
            "Sync",
            "Succeeded",
            None,
            None,
        )
        restart = ARGO.OperationResource(
            "Job",
            "agent-control-plane",
            "registry-overlay-restart-force",
            "PostSync",
            "PostSync",
            "Succeeded",
            None,
            None,
        )
        manual = operation(resources=(rollout, restart))
        contract = TRAIN.ApplicationContract(
            TRAIN.OVERLAY_APPLICATION,
            TRAIN.load_application_contracts(ROOT, revision)[TRAIN.OVERLAY_APPLICATION].identity,
            (revision,),
            True,
        )
        with (
            mock.patch.object(TRAIN.argo, "read_application_payload", return_value={}),
            mock.patch.object(
                TRAIN, "validate_live_application", side_effect=[early, refreshed, refreshed]
            ),
            mock.patch.object(
                TRAIN.argo,
                "hard_refresh_application",
                return_value=({}, refreshed),
            ),
            mock.patch.object(TRAIN, "validate_hard_refresh"),
            mock.patch.object(TRAIN, "_adopt_automated_operation", return_value=None),
            mock.patch.object(
                TRAIN.argo, "read_application_snapshot", return_value=refreshed
            ),
            mock.patch.object(
                TRAIN.argo,
                "poll_operation",
                side_effect=[early_operation, manual],
            ) as poll_operation,
            mock.patch.object(
                TRAIN.argo,
                "poll_application_ready",
                side_effect=[early, refreshed, refreshed],
            ),
            mock.patch.object(TRAIN.argo, "submit_sync") as submit,
            redirect_stdout(io.StringIO()),
        ):
            result = TRAIN.reconcile_application(
                contract,
                stage="registry-overlay",
                invocation_id="ces395-test",
                desired_skills=TRAIN.validate_skill_bundle_data(
                    valid_skill_data(), "desired"
                ),
                preflight_snapshot=baseline,
                preflight_skill_digest=None,
                force_sync=True,
                kubeconfig=Path("/tmp/scoped"),
                argocd="argocd",
                kubectl="kubectl",
                namespace="argocd",
                refresh_timeout=1,
                operation_timeout=1,
                adoption_timeout=0,
                interval=0.01,
                revision_guard=lambda: None,
            )
        self.assertEqual(result, "manual")
        self.assertEqual(poll_operation.call_count, 2)
        submit.assert_called_once()

    def test_fully_reconciled_fixture_rerun_is_clean_no_op_and_skips_verify(
        self,
    ) -> None:
        sha = "a" * 40
        bundle = TRAIN.validate_skill_bundle_data(valid_skill_data(), "desired")
        output = io.StringIO()
        with (
            mock.patch.object(TRAIN, "validate_release_checkout"),
            mock.patch.object(
                TRAIN, "load_application_contracts", return_value=contracts(sha)
            ),
            mock.patch.object(
                TRAIN, "load_journey_contracts", return_value=journey_contracts()
            ),
            mock.patch.object(TRAIN, "run_drift_gates"),
            mock.patch.object(TRAIN, "read_desired_skill_bundle", return_value=bundle),
            mock.patch.object(
                TRAIN,
                "preflight_live_applications",
                return_value=ready_snapshots(sha),
            ),
            mock.patch.object(TRAIN, "read_live_skill_bundle", return_value=bundle),
            mock.patch.object(TRAIN, "preflight_mandate_verify"),
            mock.patch.object(TRAIN, "validate_remote_main"),
            mock.patch.object(TRAIN, "recheck_skill_image_digest"),
            mock.patch.object(
                TRAIN, "reconcile_application", return_value="skipped"
            ) as reconcile,
            mock.patch.object(TRAIN, "run_mandate_verify") as verify,
            redirect_stdout(output),
        ):
            result = TRAIN.run_deploy_train(
                repo_root=ROOT,
                release_sha=sha,
                kubeconfig=Path("/tmp/scoped"),
                argocd="argocd",
                kubectl="kubectl",
                uv="uv",
                git="git",
                crane="crane",
                namespace="argocd",
                refresh_timeout=1,
                operation_timeout=1,
                adoption_timeout=0,
                verify_timeout=480,
                interval=0.01,
                invocation_id="ces395-test",
            )
        self.assertEqual(result, "no-op")
        self.assertEqual(reconcile.call_count, 1 + len(TRAIN.MANAGED_APPLICATIONS))
        verify.assert_not_called()
        receipts = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            receipts[-2],
            {"reason": "no-changes", "result": "skipped", "stage": "mandate-verify"},
        )
        self.assertEqual(receipts[-1]["result"], "no-op")

    def test_main_movement_at_final_boundary_cannot_pass_verification(self) -> None:
        for stage, outcome in (("final-pre-verification", "manual"),
                               ("final-pre-verification", "skipped"),
                               ("final-acceptance", "manual")):
            with self.subTest(stage=stage, outcome=outcome):
                sha = "a" * 40
                bundle = TRAIN.validate_skill_bundle_data(valid_skill_data(), "desired")
                output = io.StringIO()
                def guard(*args: object, **kwargs: object) -> None:
                    if kwargs["stage"] == stage:
                        raise ARGO.ArgoCoreError("main moved")
                names = ("validate_release_checkout", "load_application_contracts",
                         "load_journey_contracts", "run_drift_gates", "read_desired_skill_bundle",
                         "preflight_live_applications", "read_live_skill_bundle",
                         "preflight_mandate_verify", "validate_remote_main",
                         "recheck_skill_image_digest", "reconcile_application", "run_mandate_verify")
                with mock.patch.multiple(TRAIN, **{name: mock.DEFAULT for name in names}) as mocks, redirect_stdout(output):
                    mocks["load_application_contracts"].return_value = contracts(sha)
                    mocks["load_journey_contracts"].return_value = journey_contracts()
                    mocks["read_desired_skill_bundle"].return_value = bundle
                    mocks["read_live_skill_bundle"].return_value = bundle
                    mocks["preflight_live_applications"].return_value = ready_snapshots(sha)
                    mocks["validate_remote_main"].side_effect = guard
                    mocks["reconcile_application"].return_value = outcome
                    with self.assertRaisesRegex(ARGO.ArgoCoreError, "main moved"):
                        TRAIN.run_deploy_train(
                            repo_root=ROOT, release_sha=sha, kubeconfig=Path("/tmp/scoped"),
                            argocd="argocd", kubectl="kubectl", uv="uv", git="git", crane="crane",
                            namespace="argocd", refresh_timeout=1, operation_timeout=1,
                            adoption_timeout=0, verify_timeout=480, interval=0.01,
                            invocation_id="ces395-test",
                        )
                    self.assertEqual(mocks["run_mandate_verify"].call_count, int(stage == "final-acceptance"))
                receipts = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertFalse(any(item.get("stage") == "deploy-train" for item in receipts))

    def test_failure_stops_every_downstream_stage(self) -> None:
        sha = "a" * 40
        bundle = TRAIN.validate_skill_bundle_data(valid_skill_data(), "desired")
        failure = ARGO.ArgoCoreError("stage=secrets failed")
        with (
            mock.patch.object(TRAIN, "validate_release_checkout"),
            mock.patch.object(
                TRAIN, "load_application_contracts", return_value=contracts(sha)
            ),
            mock.patch.object(
                TRAIN, "load_journey_contracts", return_value=journey_contracts()
            ),
            mock.patch.object(TRAIN, "run_drift_gates"),
            mock.patch.object(TRAIN, "read_desired_skill_bundle", return_value=bundle),
            mock.patch.object(
                TRAIN,
                "preflight_live_applications",
                return_value=ready_snapshots(sha),
            ),
            mock.patch.object(TRAIN, "read_live_skill_bundle", return_value=bundle),
            mock.patch.object(TRAIN, "preflight_mandate_verify"),
            mock.patch.object(TRAIN, "validate_remote_main"),
            mock.patch.object(TRAIN, "recheck_skill_image_digest"),
            mock.patch.object(
                TRAIN,
                "reconcile_application",
                side_effect=["skipped", failure],
            ) as reconcile,
        ):
            with self.assertRaisesRegex(ARGO.ArgoCoreError, "stage=secrets"):
                TRAIN.run_deploy_train(
                    repo_root=ROOT,
                    release_sha=sha,
                    kubeconfig=Path("/tmp/scoped"),
                    argocd="argocd",
                    kubectl="kubectl",
                    uv="uv",
                    git="git",
                    crane="crane",
                    namespace="argocd",
                    refresh_timeout=1,
                    operation_timeout=1,
                    adoption_timeout=0,
                    verify_timeout=480,
                    interval=0.01,
                    invocation_id="ces395-test",
                )
        self.assertEqual(reconcile.call_count, 2)

    def test_verifier_preflight_failure_causes_zero_argo_refresh(self) -> None:
        sha = "a" * 40
        bundle = TRAIN.validate_skill_bundle_data(valid_skill_data(), "desired")
        with (
            mock.patch.object(TRAIN, "validate_release_checkout"),
            mock.patch.object(
                TRAIN, "load_application_contracts", return_value=contracts(sha)
            ),
            mock.patch.object(
                TRAIN, "load_journey_contracts", return_value=journey_contracts()
            ),
            mock.patch.object(TRAIN, "run_drift_gates"),
            mock.patch.object(TRAIN, "read_desired_skill_bundle", return_value=bundle),
            mock.patch.object(
                TRAIN,
                "preflight_live_applications",
                return_value=ready_snapshots(sha),
            ),
            mock.patch.object(TRAIN, "read_live_skill_bundle", return_value=bundle),
            mock.patch.object(
                TRAIN,
                "preflight_mandate_verify",
                side_effect=ARGO.ArgoCoreError("verifier unavailable"),
            ),
            mock.patch.object(TRAIN, "validate_remote_main") as remote_guard,
            mock.patch.object(TRAIN, "reconcile_application") as reconcile,
        ):
            with self.assertRaisesRegex(ARGO.ArgoCoreError, "verifier unavailable"):
                TRAIN.run_deploy_train(
                    repo_root=ROOT,
                    release_sha=sha,
                    kubeconfig=Path("/tmp/scoped"),
                    argocd="argocd",
                    kubectl="kubectl",
                    uv="uv",
                    git="git",
                    crane="crane",
                    namespace="argocd",
                    refresh_timeout=1,
                    operation_timeout=1,
                    adoption_timeout=0,
                    verify_timeout=480,
                    interval=0.01,
                    invocation_id="ces395-test",
                )
        remote_guard.assert_not_called()
        reconcile.assert_not_called()

    def test_final_remote_main_guard_failure_causes_zero_argo_refresh(self) -> None:
        sha = "a" * 40
        bundle = TRAIN.validate_skill_bundle_data(valid_skill_data(), "desired")
        with (
            mock.patch.object(TRAIN, "validate_release_checkout"),
            mock.patch.object(
                TRAIN, "load_application_contracts", return_value=contracts(sha)
            ),
            mock.patch.object(
                TRAIN, "load_journey_contracts", return_value=journey_contracts()
            ),
            mock.patch.object(TRAIN, "run_drift_gates"),
            mock.patch.object(TRAIN, "read_desired_skill_bundle", return_value=bundle),
            mock.patch.object(
                TRAIN,
                "preflight_live_applications",
                return_value=ready_snapshots(sha),
            ),
            mock.patch.object(TRAIN, "read_live_skill_bundle", return_value=bundle),
            mock.patch.object(TRAIN, "preflight_mandate_verify"),
            mock.patch.object(
                TRAIN,
                "validate_remote_main",
                side_effect=ARGO.ArgoCoreError("remote main moved"),
            ),
            mock.patch.object(TRAIN, "reconcile_application") as reconcile,
        ):
            with self.assertRaisesRegex(ARGO.ArgoCoreError, "remote main moved"):
                TRAIN.run_deploy_train(
                    repo_root=ROOT,
                    release_sha=sha,
                    kubeconfig=Path("/tmp/scoped"),
                    argocd="argocd",
                    kubectl="kubectl",
                    uv="uv",
                    git="git",
                    crane="crane",
                    namespace="argocd",
                    refresh_timeout=1,
                    operation_timeout=1,
                    adoption_timeout=0,
                    verify_timeout=480,
                    interval=0.01,
                    invocation_id="ces395-test",
                )
        reconcile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
