#!/usr/bin/env python3
"""Bounded Argo CD core-mode primitives with race-safe operation polling."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


VERSION_FILE = Path(__file__).with_name("argocd-client-version.txt")
VERSION_PATTERN = re.compile(r"\bv?(\d+\.\d+\.\d+)(?:[-+][^\s]+)?\b")
FAILURE_PHASES = {"Error", "Failed", "Terminating"}
ACTIVE_PHASES = {"Running", "Terminating"}
REFRESH_ANNOTATION = "argocd.argoproj.io/refresh"

Runner = Callable[..., subprocess.CompletedProcess[str]]


class ArgoCoreError(RuntimeError):
    """An operator-facing Argo core-mode operation failed safely."""


@dataclass(frozen=True)
class OperationResource:
    kind: str
    namespace: str
    name: str
    hook_type: str | None
    sync_phase: str | None
    hook_phase: str | None
    status: str | None
    message: str | None


@dataclass(frozen=True)
class OperationSnapshot:
    resource_version: str | None
    phase: str | None
    started_at: str | None
    finished_at: str | None
    revisions: tuple[str, ...]
    automated: bool
    initiator: str | None
    message: str | None
    resources: tuple[OperationResource, ...]
    info: tuple[tuple[str, str], ...]
    selected_resources: bool
    sync_strategy: str | None
    sync_options: tuple[str, ...]

    def fingerprint(self) -> tuple[object, ...]:
        return (
            self.phase,
            self.started_at,
            self.finished_at,
            self.revisions,
            self.automated,
            self.initiator,
            self.message,
            self.resources,
            self.info,
            self.selected_resources,
            self.sync_strategy,
            self.sync_options,
        )


@dataclass(frozen=True)
class ApplicationSnapshot:
    resource_version: str | None
    reconciled_at: str | None
    refresh_annotation: str | None
    sync_status: str | None
    health_status: str | None
    revisions: tuple[str, ...]
    condition_types: tuple[str, ...]
    operation: OperationSnapshot
    operation_present: bool
    automated_sync: bool


def run_command(
    command: Sequence[str],
    *,
    runner: Runner = subprocess.run,
    environment: dict[str, str] | None = None,
    working_directory: Path | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(command),
        text=True,
        capture_output=True,
        check=False,
        env=environment,
        cwd=str(working_directory) if working_directory is not None else None,
        input=input_text,
    )


def command_failure(
    label: str, result: subprocess.CompletedProcess[str]
) -> ArgoCoreError:
    # stdout can contain command payloads (including rendered Kubernetes
    # objects). Keep operator errors on the diagnostic channel only.
    detail = (result.stderr or "no diagnostic output").strip()
    if len(detail) > 1000:
        detail = f"{detail[:997]}..."
    return ArgoCoreError(f"{label} exited {result.returncode}: {detail}")


def resolve_executable(value: str) -> str:
    if os.sep in value:
        path = Path(value).expanduser().resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise ArgoCoreError(f"executable is missing or not runnable: {path}")
    resolved = shutil.which(value)
    if resolved is None:
        raise ArgoCoreError(
            f"{value} is not on PATH; supply its exact path with the corresponding CLI option"
        )
    return resolved


def validate_argocd_version(
    argocd: str,
    expected: str,
    *,
    runner: Runner = subprocess.run,
) -> None:
    result = run_command([argocd, "version", "--client", "--short"], runner=runner)
    if result.returncode != 0:
        raise command_failure("argocd client version check", result)
    match = VERSION_PATTERN.search(result.stdout)
    if match is None:
        raise ArgoCoreError(
            "argocd client version output was not recognized; expected a semantic version"
        )
    actual = match.group(1)
    if actual != expected:
        raise ArgoCoreError(
            f"argocd client {actual} does not match the repository pin {expected}"
        )


@contextmanager
def core_kubeconfig(
    source: Path,
    *,
    kubectl: str,
    namespace: str,
    context: str | None,
    runner: Runner = subprocess.run,
) -> Iterator[Path]:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ArgoCoreError(f"kubeconfig does not exist: {source}")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="argocd-core-kubeconfig.",
            suffix=".yaml",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, 0o600)
            flattened = run_command(
                [
                    kubectl,
                    "--kubeconfig",
                    str(source),
                    "config",
                    "view",
                    "--raw",
                    "--flatten",
                ],
                runner=runner,
            )
            if flattened.returncode != 0:
                raise command_failure("kubectl kubeconfig flatten", flattened)
            temporary.write(flattened.stdout)
            temporary.flush()
            os.fsync(temporary.fileno())

        if context:
            selected = run_command(
                [
                    kubectl,
                    "--kubeconfig",
                    str(temporary_path),
                    "config",
                    "use-context",
                    context,
                ],
                runner=runner,
            )
            if selected.returncode != 0:
                raise command_failure("kubectl context selection", selected)
            current = run_command(
                [
                    kubectl,
                    "--kubeconfig",
                    str(temporary_path),
                    "config",
                    "current-context",
                ],
                runner=runner,
            )
            if current.returncode != 0:
                raise command_failure("kubectl current context readback", current)
            if current.stdout.strip() != context:
                raise ArgoCoreError(
                    "kubectl context readback mismatch: "
                    f"expected={context} actual={current.stdout.strip() or 'missing'}"
                )

        namespaced = run_command(
            [
                kubectl,
                "--kubeconfig",
                str(temporary_path),
                "config",
                "set-context",
                "--current",
                "--namespace",
                namespace,
            ],
            runner=runner,
        )
        if namespaced.returncode != 0:
            raise command_failure("kubectl Argo namespace selection", namespaced)
        yield temporary_path
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _revision_tuple(value: Mapping[str, Any]) -> tuple[str, ...]:
    revisions = value.get("revisions")
    if isinstance(revisions, list) and all(
        isinstance(revision, str) and revision for revision in revisions
    ):
        return tuple(revisions)
    revision = value.get("revision")
    return (revision,) if isinstance(revision, str) and revision else ()


def operation_snapshot(payload: Mapping[str, Any]) -> OperationSnapshot:
    metadata = _mapping(payload.get("metadata"))
    status = _mapping(payload.get("status"))
    state = _mapping(status.get("operationState"))
    operation = _mapping(state.get("operation"))
    sync = _mapping(operation.get("sync"))
    sync_result = _mapping(state.get("syncResult"))
    revisions = _revision_tuple(sync_result) or _revision_tuple(sync)
    initiated_by = _mapping(operation.get("initiatedBy"))
    resources: list[OperationResource] = []
    raw_resources = sync_result.get("resources")
    if isinstance(raw_resources, list):
        for item in raw_resources:
            resource = _mapping(item)
            resources.append(
                OperationResource(
                    kind=str(resource.get("kind") or ""),
                    namespace=str(resource.get("namespace") or ""),
                    name=str(resource.get("name") or ""),
                    hook_type=(
                        str(resource["hookType"])
                        if isinstance(resource.get("hookType"), str)
                        else None
                    ),
                    sync_phase=(
                        str(resource["syncPhase"])
                        if isinstance(resource.get("syncPhase"), str)
                        else None
                    ),
                    hook_phase=(
                        str(resource["hookPhase"])
                        if isinstance(resource.get("hookPhase"), str)
                        else None
                    ),
                    status=(
                        str(resource["status"])
                        if isinstance(resource.get("status"), str)
                        else None
                    ),
                    message=(
                        str(resource["message"])
                        if isinstance(resource.get("message"), str)
                        else None
                    ),
                )
            )
    info: list[tuple[str, str]] = []
    raw_info = operation.get("info")
    if isinstance(raw_info, list):
        for raw_item in raw_info:
            item = _mapping(raw_item)
            if isinstance(item.get("name"), str) and isinstance(item.get("value"), str):
                info.append((str(item["name"]), str(item["value"])))
    strategy = _mapping(sync.get("syncStrategy"))
    if isinstance(strategy.get("hook"), dict):
        sync_strategy = "hook"
    elif isinstance(strategy.get("apply"), dict):
        sync_strategy = "apply"
    else:
        sync_strategy = None
    raw_sync_options = sync.get("syncOptions")
    sync_options = (
        tuple(str(option) for option in raw_sync_options if isinstance(option, str))
        if isinstance(raw_sync_options, list)
        else ()
    )
    return OperationSnapshot(
        resource_version=(
            str(metadata["resourceVersion"])
            if metadata.get("resourceVersion") is not None
            else None
        ),
        phase=str(state["phase"]) if isinstance(state.get("phase"), str) else None,
        started_at=(
            str(state["startedAt"]) if isinstance(state.get("startedAt"), str) else None
        ),
        finished_at=(
            str(state["finishedAt"])
            if isinstance(state.get("finishedAt"), str)
            else None
        ),
        revisions=revisions,
        automated=initiated_by.get("automated") is True,
        initiator=(
            str(initiated_by["username"])
            if isinstance(initiated_by.get("username"), str)
            else None
        ),
        message=str(state["message"])
        if isinstance(state.get("message"), str)
        else None,
        resources=tuple(resources),
        info=tuple(info),
        selected_resources=isinstance(sync.get("resources"), list)
        and bool(sync.get("resources")),
        sync_strategy=sync_strategy,
        sync_options=sync_options,
    )


def application_snapshot(payload: Mapping[str, Any]) -> ApplicationSnapshot:
    metadata = _mapping(payload.get("metadata"))
    annotations = _mapping(metadata.get("annotations"))
    status = _mapping(payload.get("status"))
    sync = _mapping(status.get("sync"))
    health = _mapping(status.get("health"))
    spec = _mapping(payload.get("spec"))
    sync_policy = _mapping(spec.get("syncPolicy"))
    raw_conditions = status.get("conditions")
    condition_types: list[str] = []
    if isinstance(raw_conditions, list):
        for raw_condition in raw_conditions:
            condition = _mapping(raw_condition)
            if isinstance(condition.get("type"), str):
                condition_types.append(str(condition["type"]))
    raw_operation = payload.get("operation")
    return ApplicationSnapshot(
        resource_version=(
            str(metadata["resourceVersion"])
            if metadata.get("resourceVersion") is not None
            else None
        ),
        reconciled_at=(
            str(status["reconciledAt"])
            if isinstance(status.get("reconciledAt"), str)
            else None
        ),
        refresh_annotation=(
            str(annotations[REFRESH_ANNOTATION])
            if isinstance(annotations.get(REFRESH_ANNOTATION), str)
            else None
        ),
        sync_status=str(sync["status"])
        if isinstance(sync.get("status"), str)
        else None,
        health_status=(
            str(health["status"]) if isinstance(health.get("status"), str) else None
        ),
        revisions=_revision_tuple(sync),
        condition_types=tuple(condition_types),
        operation=operation_snapshot(payload),
        operation_present=isinstance(raw_operation, dict) and bool(raw_operation),
        automated_sync=isinstance(sync_policy.get("automated"), dict),
    )


def parse_application_json(raw: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ArgoCoreError(f"{label} returned invalid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ArgoCoreError(f"{label} returned a non-object JSON value")
    return payload


def read_application_payload(
    application: str,
    *,
    kubeconfig: Path,
    kubectl: str,
    namespace: str,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    result = run_command(
        [
            kubectl,
            "--kubeconfig",
            str(kubeconfig),
            "--namespace",
            namespace,
            "get",
            "application.argoproj.io",
            application,
            "--output",
            "json",
        ],
        runner=runner,
    )
    if result.returncode != 0:
        raise command_failure(f"read Argo Application {application}", result)
    return parse_application_json(result.stdout, f"Argo Application {application}")


def read_application_snapshot(
    application: str,
    *,
    kubeconfig: Path,
    kubectl: str,
    namespace: str,
    runner: Runner = subprocess.run,
) -> ApplicationSnapshot:
    return application_snapshot(
        read_application_payload(
            application,
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            namespace=namespace,
            runner=runner,
        )
    )


def hard_refresh_application(
    application: str,
    *,
    kubeconfig: Path,
    argocd: str,
    timeout: float,
    runner: Runner = subprocess.run,
) -> tuple[dict[str, Any], ApplicationSnapshot]:
    environment = os.environ.copy()
    environment["KUBECONFIG"] = str(kubeconfig)
    result = run_command(
        [
            argocd,
            "--core",
            "app",
            "get",
            application,
            "--hard-refresh",
            "--output",
            "json",
            "--timeout",
            str(max(1, math.ceil(timeout))),
        ],
        runner=runner,
        environment=environment,
    )
    if result.returncode != 0:
        raise command_failure(f"hard refresh Argo Application {application}", result)
    payload = parse_application_json(result.stdout, f"hard refresh {application}")
    return payload, application_snapshot(payload)


def submit_sync(
    application: str,
    *,
    revisions: Sequence[str],
    run_id: str,
    kubeconfig: Path,
    argocd: str,
    runner: Runner = subprocess.run,
) -> None:
    exact_revisions = tuple(revisions)
    if not exact_revisions or any(not revision for revision in exact_revisions):
        raise ArgoCoreError(f"exact sync revisions are required for {application}")
    if not re.fullmatch(r"[a-z0-9-]{8,80}", run_id):
        raise ArgoCoreError("CES-395 run id must be a non-secret lowercase token")
    command = [
        argocd,
        "--core",
        "app",
        "sync",
        application,
        "--async",
        "--strategy",
        "hook",
        "--prune",
        "--info",
        f"ces-395-run-id={run_id}",
    ]
    if len(exact_revisions) == 1:
        command.extend(["--revision", exact_revisions[0]])
    else:
        for position, revision in enumerate(exact_revisions, start=1):
            command.extend(
                ["--revisions", revision, "--source-positions", str(position)]
            )
    environment = os.environ.copy()
    environment["KUBECONFIG"] = str(kubeconfig)
    result = run_command(
        command,
        runner=runner,
        environment=environment,
    )
    if result.returncode != 0:
        raise command_failure(f"submit async Argo sync for {application}", result)


def resource_version_advanced(before: str | None, current: str | None) -> bool:
    if before is None or current is None:
        return before != current
    try:
        return int(current) > int(before)
    except ValueError:
        return current != before


def is_new_operation(before: OperationSnapshot, current: OperationSnapshot) -> bool:
    return (
        resource_version_advanced(before.resource_version, current.resource_version)
        and current.fingerprint() != before.fingerprint()
    )


def poll_operation(
    application: str,
    *,
    before: OperationSnapshot,
    expected_revisions: Sequence[str],
    expected_automated: bool,
    run_id: str | None,
    kubeconfig: Path,
    kubectl: str,
    namespace: str,
    timeout: float,
    interval: float,
    runner: Runner = subprocess.run,
    initial: ApplicationSnapshot | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> OperationSnapshot:
    deadline = monotonic() + timeout
    expected = tuple(expected_revisions)
    observed_new = False
    pinned_started_at: str | None = None
    pending = initial
    last = before
    while True:
        current_application = pending or read_application_snapshot(
            application,
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            namespace=namespace,
            runner=runner,
        )
        pending = None
        current = current_application.operation
        last = current
        new_operation = is_new_operation(before, current)
        correlated = new_operation and (
            ("ces-395-run-id", run_id) in current.info if run_id is not None else True
        )
        if not observed_new and correlated:
            if run_id is None and current.started_at is None:
                raise ArgoCoreError(
                    f"new automated Argo operation for {application} lacks startedAt; "
                    "cannot pin correlation identity"
                )
            observed_new = True
            pinned_started_at = current.started_at
        if observed_new:
            if (
                run_id is not None
                and (
                    "ces-395-run-id",
                    run_id,
                )
                not in current.info
            ):
                raise ArgoCoreError(
                    f"correlated Argo operation for {application} was replaced; "
                    "run token no longer matches"
                )
            if pinned_started_at is None and current.started_at is not None:
                pinned_started_at = current.started_at
            elif (
                pinned_started_at is not None
                and current.started_at != pinned_started_at
            ):
                raise ArgoCoreError(
                    f"correlated Argo operation for {application} was replaced; "
                    "startedAt changed"
                )
            if current.automated is not expected_automated:
                expected_label = "automated" if expected_automated else "manual"
                raise ArgoCoreError(
                    f"Argo operation for {application} has the wrong initiator; "
                    f"expected {expected_label}"
                )
            if current.revisions and current.revisions != expected:
                raise ArgoCoreError(
                    f"Argo operation revision drift for {application}: "
                    f"expected={expected} actual={current.revisions}"
                )
            if current.phase == "Succeeded":
                if current.revisions != expected:
                    raise ArgoCoreError(
                        f"Argo operation for {application} completed without the exact "
                        f"revision tuple: expected={expected} actual={current.revisions or 'missing'}"
                    )
                return current
            if current.phase in FAILURE_PHASES:
                raise ArgoCoreError(
                    f"Argo sync for {application} ended in phase {current.phase}: "
                    f"{current.message or 'no operation message'}"
                )
        if monotonic() >= deadline:
            raise ArgoCoreError(
                f"timed out after {timeout:g}s waiting for a new {application} operation; "
                f"last phase was {last.phase or 'unknown'}"
            )
        sleeper(interval)


def poll_application_ready(
    application: str,
    *,
    expected_revisions: Sequence[str],
    kubeconfig: Path,
    kubectl: str,
    namespace: str,
    timeout: float,
    interval: float,
    runner: Runner = subprocess.run,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> ApplicationSnapshot:
    deadline = monotonic() + timeout
    expected = tuple(expected_revisions)
    last: ApplicationSnapshot | None = None
    while True:
        current = read_application_snapshot(
            application,
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            namespace=namespace,
            runner=runner,
        )
        last = current
        if current.operation.phase in FAILURE_PHASES:
            raise ArgoCoreError(
                f"Argo Application {application} operation ended in "
                f"phase {current.operation.phase}"
            )
        error_conditions = sorted(
            condition
            for condition in current.condition_types
            if condition in {"ComparisonError", "InvalidSpecError"}
        )
        if error_conditions:
            raise ArgoCoreError(
                f"Argo Application {application} has blocking conditions: "
                + ",".join(error_conditions)
            )
        if (
            not current.operation_present
            and current.operation.phase not in ACTIVE_PHASES
            and current.sync_status == "Synced"
            and current.health_status == "Healthy"
            and current.revisions == expected
        ):
            return current
        if current.health_status in {"Degraded", "Missing"}:
            raise ArgoCoreError(
                f"Argo Application {application} is {current.health_status}; "
                "refusing to continue the deploy train"
            )
        if monotonic() >= deadline:
            raise ArgoCoreError(
                f"timed out after {timeout:g}s waiting for {application} to become "
                f"Synced/Healthy at {expected}; last sync={last.sync_status or 'unknown'} "
                f"health={last.health_status or 'unknown'} "
                f"revisions={last.revisions or 'missing'}"
            )
        sleeper(interval)


def pinned_version() -> str:
    try:
        value = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ArgoCoreError(
            f"could not read Argo client version pin {VERSION_FILE}: {error}"
        ) from error
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ArgoCoreError(f"invalid Argo client version pin in {VERSION_FILE}")
    return value


def default_argocd_executable() -> str:
    managed = Path("/root/dev/.tools") / f"argocd-v{pinned_version()}" / "argocd"
    return str(managed) if managed.is_file() else "argocd"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect bounded Argo CD core-mode Application status. "
            "Use mandate_deploy_train.py for the CES-395 production choreography."
        )
    )
    parser.add_argument(
        "--kubeconfig", type=Path, default=Path.home() / ".kube" / "config"
    )
    parser.add_argument("--context")
    parser.add_argument("--namespace", default="argocd")
    parser.add_argument("--argocd", default=default_argocd_executable())
    parser.add_argument("--kubectl", default="kubectl")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("application")
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        argocd = resolve_executable(args.argocd)
        kubectl = resolve_executable(args.kubectl)
        validate_argocd_version(argocd, pinned_version())
        with core_kubeconfig(
            args.kubeconfig,
            kubectl=kubectl,
            namespace=args.namespace,
            context=args.context,
        ) as kubeconfig:
            environment = os.environ.copy()
            environment["KUBECONFIG"] = str(kubeconfig)
            result = run_command(
                [argocd, "--core", "app", "get", args.application],
                environment=environment,
            )
            if result.returncode != 0:
                raise command_failure(
                    f"read Argo status for {args.application}", result
                )
            sys.stdout.write(result.stdout)
            return 0
    except ArgoCoreError as error:
        print(f"argocd_core: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
