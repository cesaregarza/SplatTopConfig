#!/usr/bin/env python3
"""Atomically update the complete active Citrus release tuple."""

from __future__ import annotations

import argparse
import copy
import io
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOURCE_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SUPPORTED_POLICIES = frozenset({"auto-roll", "manual-attestation"})
DEFAULT_REGISTRY = Path("helm/citrus/release-bindings.json")
REGISTRY_ROOT_KEYS = frozenset({"schemaVersion", "environments", "bindings"})
ENVIRONMENT_KEYS = frozenset({"appliedValues", "operationalValues"})
BINDING_KEYS = frozenset(
    {"name", "valuePath", "policy", "enabledPath", "requiresCapability"}
)
CAPABILITY_RECEIPT_KEYS = frozenset({"schemaVersion", "sourceRevision", "capabilities"})
EXPECTED_ENVIRONMENT_PATHS = {
    "dev": (
        (
            Path("helm/citrus/values.yaml"),
            Path("helm/citrus/values-dev.yaml"),
            Path("helm/citrus/values-payment-dev.yaml"),
            Path("helm/citrus/values-recurring-dev.yaml"),
        ),
        frozenset(
            {
                Path("helm/citrus/values-dev.yaml"),
                Path("helm/citrus/values-payment-dev.yaml"),
            }
        ),
    ),
    "prod": (
        (Path("helm/citrus/values.yaml"),),
        frozenset({Path("helm/citrus/values.yaml")}),
    ),
}


class CitrusReleaseContractError(RuntimeError):
    """Raised before a malformed or partial release tuple can be written."""


@dataclass(frozen=True)
class Binding:
    name: str
    value_path: tuple[str, ...]
    policy: str
    enabled_path: tuple[str, ...] | None = None
    required_capability: str | None = None


@dataclass(frozen=True)
class Environment:
    name: str
    applied_values: tuple[Path, ...]
    operational_values: frozenset[Path]


@dataclass(frozen=True)
class Registry:
    environments: Mapping[str, Environment]
    bindings: tuple[Binding, ...]


@dataclass(frozen=True)
class CapabilityReceipt:
    source_revision: str
    capabilities: frozenset[str]


@dataclass(frozen=True)
class UpdateResult:
    changed_paths: tuple[Path, ...]


def validate_source_revision(value: object) -> str:
    revision = str(value or "").strip()
    if not SOURCE_REVISION_PATTERN.fullmatch(revision):
        raise CitrusReleaseContractError(
            "source revision must be an exact 40-character lowercase SHA"
        )
    return revision


def _parse_value_path(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise CitrusReleaseContractError(f"{field} must be a dotted string")
    parts = tuple(value.split("."))
    if not parts or any(not part for part in parts):
        raise CitrusReleaseContractError(f"{field} must be a dotted string")
    return parts


def _require_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CitrusReleaseContractError(f"{field} must be a mapping")
    return value


def _reject_unknown_keys(
    value: Mapping[str, Any], *, field: str, allowed: frozenset[str]
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise CitrusReleaseContractError(
            f"{field} contains unknown keys: {', '.join(sorted(unknown))}"
        )


def _require_string_list(value: object, *, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(item, str)
            or not item
            or any(character in item for character in ("\x00", "\r", "\n"))
            for item in value
        )
    ):
        raise CitrusReleaseContractError(f"{field} must be a non-empty string array")
    if len(set(value)) != len(value):
        raise CitrusReleaseContractError(f"{field} must not contain duplicates")
    return tuple(value)


def load_registry(path: Path) -> Registry:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CitrusReleaseContractError(
            f"unable to read binding registry {path}: {exc}"
        ) from exc
    root = _require_mapping(raw, field="binding registry")
    _reject_unknown_keys(root, field="binding registry", allowed=REGISTRY_ROOT_KEYS)
    if root.get("schemaVersion") != 1:
        raise CitrusReleaseContractError("binding registry schemaVersion must be 1")

    raw_environments = _require_mapping(root.get("environments"), field="environments")
    if set(raw_environments) != {"dev", "prod"}:
        raise CitrusReleaseContractError(
            "binding registry environments must be exactly dev and prod"
        )
    environments: dict[str, Environment] = {}
    for name, raw_environment in raw_environments.items():
        environment = _require_mapping(raw_environment, field=f"environments.{name}")
        _reject_unknown_keys(
            environment,
            field=f"environments.{name}",
            allowed=ENVIRONMENT_KEYS,
        )
        applied = tuple(
            Path(value)
            for value in _require_string_list(
                environment.get("appliedValues"),
                field=f"environments.{name}.appliedValues",
            )
        )
        operational = frozenset(
            Path(value)
            for value in _require_string_list(
                environment.get("operationalValues"),
                field=f"environments.{name}.operationalValues",
            )
        )
        if not operational.issubset(applied):
            raise CitrusReleaseContractError(
                f"{name} operational values must be a subset of applied values"
            )
        expected_applied, expected_operational = EXPECTED_ENVIRONMENT_PATHS[name]
        if applied != expected_applied or operational != expected_operational:
            raise CitrusReleaseContractError(
                f"{name} applied and operational values must match "
                "the exact release contract"
            )
        environments[name] = Environment(name, applied, operational)

    raw_bindings = root.get("bindings")
    if not isinstance(raw_bindings, list) or not raw_bindings:
        raise CitrusReleaseContractError("bindings must be a non-empty array")
    bindings: list[Binding] = []
    names: set[str] = set()
    value_paths: set[tuple[str, ...]] = set()
    for index, raw_binding in enumerate(raw_bindings):
        field = f"bindings[{index}]"
        binding = _require_mapping(raw_binding, field=field)
        _reject_unknown_keys(binding, field=field, allowed=BINDING_KEYS)
        name = binding.get("name")
        if not isinstance(name, str) or not name:
            raise CitrusReleaseContractError(f"{field}.name must be a string")
        if name in names:
            raise CitrusReleaseContractError(f"duplicate binding name: {name}")
        names.add(name)
        value_path = _parse_value_path(
            binding.get("valuePath"), field=f"{field}.valuePath"
        )
        if value_path in value_paths:
            raise CitrusReleaseContractError(
                f"duplicate binding valuePath: {'.'.join(value_path)}"
            )
        value_paths.add(value_path)
        policy = binding.get("policy")
        if policy not in SUPPORTED_POLICIES:
            raise CitrusReleaseContractError(
                f"{field}.policy must be auto-roll or manual-attestation"
            )
        enabled_value = binding.get("enabledPath")
        enabled_path = (
            _parse_value_path(enabled_value, field=f"{field}.enabledPath")
            if enabled_value is not None
            else None
        )
        capability = binding.get("requiresCapability")
        if capability is not None and (
            not isinstance(capability, str) or not capability
        ):
            raise CitrusReleaseContractError(
                f"{field}.requiresCapability must be a non-empty string"
            )
        if policy == "manual-attestation" and enabled_path is None:
            raise CitrusReleaseContractError(
                f"{field} manual-attestation bindings must be optional"
            )
        if policy == "manual-attestation" and capability is not None:
            raise CitrusReleaseContractError(
                f"{field} manual-attestation bindings cannot require a capability"
            )
        if policy == "auto-roll" and enabled_path is not None and capability is None:
            raise CitrusReleaseContractError(
                f"{field} optional auto-roll bindings must require a capability"
            )
        bindings.append(
            Binding(
                name=name,
                value_path=value_path,
                policy=policy,
                enabled_path=enabled_path,
                required_capability=capability,
            )
        )
    return Registry(environments=environments, bindings=tuple(bindings))


def load_capability_receipt(path: Path) -> CapabilityReceipt:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CitrusReleaseContractError(
            f"unable to read source capability receipt {path}: {exc}"
        ) from exc
    document = _require_mapping(raw, field="source capability receipt")
    _reject_unknown_keys(
        document,
        field="source capability receipt",
        allowed=CAPABILITY_RECEIPT_KEYS,
    )
    if document.get("schemaVersion") != 1:
        raise CitrusReleaseContractError(
            "source capability receipt schemaVersion must be 1"
        )
    source_revision = validate_source_revision(document.get("sourceRevision"))
    values = document.get("capabilities")
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value for value in values
    ):
        raise CitrusReleaseContractError(
            "source capability receipt capabilities must be a string array"
        )
    if len(set(values)) != len(values):
        raise CitrusReleaseContractError(
            "source capability receipt capabilities must not contain duplicates"
        )
    return CapabilityReceipt(source_revision, frozenset(values))


def _resolve_repo_path(repo_root: Path, relative: Path) -> Path:
    if relative.is_absolute():
        raise CitrusReleaseContractError(
            f"registry path must be repository-relative: {relative}"
        )
    root = repo_root.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CitrusReleaseContractError(
            f"registry path escapes repository root: {relative}"
        ) from exc
    return resolved


def _get_path(document: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = document
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            raise CitrusReleaseContractError(
                f"values path is missing: {'.'.join(path)}"
            )
        current = current[part]
    return current


def _has_path(document: Mapping[str, Any], path: Sequence[str]) -> bool:
    current: Any = document
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return False
        current = current[part]
    return True


def _set_path(
    document: MutableMapping[str, Any], path: Sequence[str], value: str
) -> None:
    current: Any = document
    for part in path[:-1]:
        if not isinstance(current, MutableMapping) or part not in current:
            raise CitrusReleaseContractError(
                f"values path is missing: {'.'.join(path)}"
            )
        current = current[part]
    if not isinstance(current, MutableMapping) or path[-1] not in current:
        raise CitrusReleaseContractError(f"values path is missing: {'.'.join(path)}")
    current[path[-1]] = value


def _deep_merge(base: MutableMapping[str, Any], overlay: Mapping[str, Any]) -> None:
    for key, value in overlay.items():
        existing = base.get(key)
        if isinstance(existing, MutableMapping) and isinstance(value, Mapping):
            _deep_merge(existing, value)
        else:
            base[key] = copy.deepcopy(value)


def _verified_image_paths(
    document: Mapping[str, Any], prefix: tuple[str, ...] = ()
) -> tuple[tuple[tuple[str, ...], bool], ...]:
    found: list[tuple[tuple[str, ...], bool]] = []
    for key, value in document.items():
        path = (*prefix, str(key))
        if key == "verifiedImageTag":
            found.append((path, document.get("enabled") is True))
        if isinstance(value, Mapping):
            found.extend(_verified_image_paths(value, path))
    return tuple(found)


def _dump_yaml(document: Mapping[str, Any]) -> bytes:
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    destination = io.StringIO()
    yaml.dump(document, destination)
    return destination.getvalue().encode("utf-8")


def _load_yaml(path: Path) -> MutableMapping[str, Any]:
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    try:
        with path.open("r", encoding="utf-8") as source:
            document = yaml.load(source) or {}
    except OSError as exc:
        raise CitrusReleaseContractError(
            f"unable to read applied values {path}: {exc}"
        ) from exc
    if not isinstance(document, MutableMapping):
        raise CitrusReleaseContractError(f"values document must be a mapping: {path}")
    return document


def _atomic_write(
    changes: Sequence[tuple[Path, bytes]],
    *,
    install_file: Callable[[Path, Path], None] = os.replace,
) -> None:
    """Install a multi-file plan and restore every original on any failure."""

    if not changes:
        return
    paths = [path for path, _ in changes]
    if len(set(paths)) != len(paths):
        raise CitrusReleaseContractError("atomic write plan contains duplicate paths")
    staged: list[tuple[Path, Path, Path | None]] = []
    attempted: list[tuple[Path, Path | None]] = []
    try:
        for path, contents in changes:
            if not path.parent.is_dir():
                raise CitrusReleaseContractError(
                    f"output parent directory does not exist: {path.parent}"
                )
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
            ) as temporary:
                temporary.write(contents)
                temporary.flush()
                os.fsync(temporary.fileno())
                staged_path = Path(temporary.name)
            os.chmod(staged_path, mode)
            backup_path: Path | None = None
            if path.exists():
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=path.parent,
                    prefix=f".{path.name}.backup.",
                    delete=False,
                ) as backup:
                    backup.write(path.read_bytes())
                    backup.flush()
                    os.fsync(backup.fileno())
                    backup_path = Path(backup.name)
                os.chmod(backup_path, mode)
            staged.append((path, staged_path, backup_path))

        for path, staged_path, backup_path in staged:
            attempted.append((path, backup_path))
            install_file(staged_path, path)
    except Exception:
        for path, backup_path in reversed(attempted):
            if backup_path is None:
                path.unlink(missing_ok=True)
            elif backup_path.exists():
                os.replace(backup_path, path)
        raise
    finally:
        for _, staged_path, backup_path in staged:
            staged_path.unlink(missing_ok=True)
            if backup_path is not None:
                backup_path.unlink(missing_ok=True)


def update_release(
    *,
    repo_root: Path,
    registry_path: Path,
    environment_name: str,
    source_revision: str,
    capability_receipt: CapabilityReceipt,
    output_path_list: Path,
    install_file: Callable[[Path, Path], None] = os.replace,
) -> UpdateResult:
    revision = validate_source_revision(source_revision)
    if capability_receipt.source_revision != revision:
        raise CitrusReleaseContractError(
            "source capability receipt revision must exactly match the release "
            f"revision {revision}"
        )
    root = repo_root.resolve()
    output_path_list = output_path_list.resolve()
    registry = load_registry(registry_path)
    if environment_name not in registry.environments:
        raise CitrusReleaseContractError(
            f"unsupported Citrus environment: {environment_name}"
        )
    environment = registry.environments[environment_name]

    relative_paths = environment.applied_values
    absolute_paths = tuple(_resolve_repo_path(root, path) for path in relative_paths)
    documents = {
        relative: _load_yaml(absolute)
        for relative, absolute in zip(relative_paths, absolute_paths, strict=True)
    }
    effective: MutableMapping[str, Any] = {}
    for relative in relative_paths:
        _deep_merge(effective, documents[relative])

    registered_optional = {
        binding.value_path
        for binding in registry.bindings
        if binding.enabled_path is not None
    }
    for value_path, enabled in _verified_image_paths(effective):
        if value_path not in registered_optional:
            state = "enabled" if enabled else "disabled"
            raise CitrusReleaseContractError(
                f"unregistered optional image binding {'.'.join(value_path)} is {state}"
            )

    planned = {path: copy.deepcopy(document) for path, document in documents.items()}
    changed_relative: set[Path] = set()
    for binding in registry.bindings:
        if binding.enabled_path is not None:
            enabled = _get_path(effective, binding.enabled_path)
            if not isinstance(enabled, bool):
                raise CitrusReleaseContractError(
                    f"{'.'.join(binding.enabled_path)} must be boolean"
                )
            if not enabled:
                continue
            if binding.policy == "manual-attestation":
                raise CitrusReleaseContractError(
                    f"enabled binding {binding.name} requires manual attestation"
                )
        if (
            binding.required_capability is not None
            and binding.required_capability not in capability_receipt.capabilities
        ):
            raise CitrusReleaseContractError(
                f"enabled binding {binding.name} requires source capability "
                f"{binding.required_capability}"
            )

        owner: Path | None = None
        for relative in relative_paths:
            if _has_path(documents[relative], binding.value_path):
                owner = relative
        if owner is None:
            raise CitrusReleaseContractError(
                f"binding path is missing: {'.'.join(binding.value_path)}"
            )
        if owner not in environment.operational_values:
            raise CitrusReleaseContractError(
                f"binding {binding.name} is owned by non-operational values "
                f"path {owner} for {environment_name}"
            )
        current = _get_path(planned[owner], binding.value_path)
        if current != revision:
            _set_path(planned[owner], binding.value_path, revision)
            changed_relative.add(owner)

    changed_paths = tuple(
        relative for relative in relative_paths if relative in changed_relative
    )
    path_output = "".join(f"{path.as_posix()}\n" for path in changed_paths).encode(
        "utf-8"
    )
    writes: list[tuple[Path, bytes]] = []
    for relative, absolute in zip(relative_paths, absolute_paths, strict=True):
        if relative in changed_relative:
            writes.append((absolute, _dump_yaml(planned[relative])))
    if output_path_list in absolute_paths:
        raise CitrusReleaseContractError(
            "output path list must not overwrite an applied values file"
        )
    if not output_path_list.exists() or output_path_list.read_bytes() != path_output:
        writes.append((output_path_list, path_output))
    _atomic_write(writes, install_file=install_file)
    return UpdateResult(changed_paths=changed_paths)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=("dev", "prod"))
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--capabilities-file", required=True, type=Path)
    parser.add_argument("--output-path-list", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = (args.repo_root or Path(__file__).resolve().parents[1]).resolve()
    registry_path = args.registry
    if not registry_path.is_absolute():
        registry_path = repo_root / registry_path
    try:
        update_release(
            repo_root=repo_root,
            registry_path=registry_path,
            environment_name=args.environment,
            source_revision=args.source_revision,
            capability_receipt=load_capability_receipt(args.capabilities_file),
            output_path_list=args.output_path_list.resolve(),
        )
    except (CitrusReleaseContractError, OSError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
