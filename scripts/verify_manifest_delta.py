#!/usr/bin/env python3
"""Verify that two multi-document Kubernetes manifests contain an exact delta."""
from __future__ import annotations

import argparse
import json
import sys
import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

_MISSING = object()
_YAML = YAML(typ="safe")


class VerificationError(ValueError):
    """A manifest or expected-delta contract is invalid or does not match."""


def _identity(document: dict[str, Any], source: Path) -> str:
    kind = document.get("kind")
    metadata = document.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    namespace = metadata.get("namespace", "") if isinstance(metadata, dict) else ""
    if not isinstance(kind, str) or not kind:
        raise VerificationError(f"{source}: document has no valid kind")
    if not isinstance(name, str) or not name:
        raise VerificationError(f"{source}: document has no valid metadata.name")
    if namespace is None:
        namespace = ""
    if not isinstance(namespace, str):
        raise VerificationError(f"{source}: document has no valid metadata.namespace")
    return f"{namespace}/{kind}/{name}"


def load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    """Load a YAML stream and reject malformed or duplicate resource identities."""
    try:
        inventory: dict[str, dict[str, Any]] = {}
        for document in _YAML.load_all(path.read_text(encoding="utf-8")):
            if document is None:
                continue
            if not isinstance(document, dict):
                raise VerificationError(f"{path}: each document must be a mapping")
            identity = _identity(document, path)
            if identity in inventory:
                raise VerificationError(f"{path}: duplicate resource {identity}")
            inventory[identity] = document
        return inventory
    except VerificationError:
        raise
    except Exception as exc:
        raise VerificationError(f"{path}: invalid YAML") from exc


def _path_parts(path: str) -> list[str]:
    if path == "/":
        return []
    if not isinstance(path, str) or not path.startswith("/") or path.endswith("/"):
        raise VerificationError(f"expected entry has invalid path {path!r}")
    parts = path[1:].split("/")
    if any(not part for part in parts):
        raise VerificationError(f"expected entry has invalid path {path!r}")
    decoded = []
    for part in parts:
        if re.search(r"~(?![01])", part):
            raise VerificationError(f"expected entry has invalid path {path!r}")
        decoded.append(part.replace("~1", "/").replace("~0", "~"))
    return decoded


def _pointer_escape(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _at(document: Any, path: str) -> tuple[bool, Any]:
    current = document
    for part in _path_parts(path):
        if isinstance(current, dict):
            if part not in current:
                return False, _MISSING
            current = current[part]
        elif isinstance(current, list):
            try:
                index = int(part)
            except ValueError:
                return False, _MISSING
            if index < 0 or index >= len(current):
                return False, _MISSING
            current = current[index]
        else:
            return False, _MISSING
    return True, current


def _same_value(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_value(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return left == right


def _diff(before: Any, after: Any, path: str = "") -> list[str]:
    if before is _MISSING or after is _MISSING or type(before) is not type(after):
        return [path or "/"]
    if isinstance(before, dict):
        paths: list[str] = []
        for key in sorted(before.keys() | after.keys(), key=str):
            child = f"{path}/{_pointer_escape(key)}" if path else f"/{_pointer_escape(key)}"
            paths.extend(_diff(before.get(key, _MISSING), after.get(key, _MISSING), child))
        return paths
    if isinstance(before, list):
        if len(before) != len(after):
            return [path or "/"]
        paths: list[str] = []
        for index, (left, right) in enumerate(zip(before, after)):
            child = f"{path}/{index}" if path else f"/{index}"
            paths.extend(_diff(left, right, child))
        return paths
    return [] if _same_value(before, after) else [path or "/"]


def _expected(path: Path) -> list[dict[str, Any]]:
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise VerificationError(f"{path}: invalid JSON") from exc
    if not isinstance(records, list):
        raise VerificationError(f"{path}: expected JSON must be a list")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            raise VerificationError(f"{path}: expected entries must be objects")
        resource = record.get("resource")
        delta_path = record.get("path")
        if not isinstance(resource, str) or resource.count("/") != 2:
            raise VerificationError(f"{path}: expected entry has invalid resource")
        if not resource.split("/")[1] or not resource.split("/")[2]:
            raise VerificationError(f"{path}: expected entry has invalid resource")
        if not isinstance(delta_path, str):
            raise VerificationError(f"{path}: expected entry has invalid path")
        _path_parts(delta_path)
        key = (resource, delta_path)
        if key in seen:
            raise VerificationError(f"{path}: duplicate expected delta {resource}{delta_path}")
        seen.add(key)
        before_present = record.get("before_present", True)
        after_present = record.get("after_present", True)
        if not isinstance(before_present, bool) or not isinstance(after_present, bool):
            raise VerificationError(f"{path}: presence flags must be boolean")
        if before_present and "before" not in record:
            raise VerificationError(f"{path}: matching entry must include before")
        if after_present and "after" not in record:
            raise VerificationError(f"{path}: matching entry must include after")
        result.append({"resource": resource, "path": delta_path, "before_present": before_present,
                       "after_present": after_present, "before": record.get("before"), "after": record.get("after")})
    return result


def verify(before_path: Path, after_path: Path, expected_path: Path) -> list[dict[str, str]]:
    before = load_manifest(before_path)
    after = load_manifest(after_path)
    if set(before) != set(after):
        raise VerificationError("resource inventory changed")
    expected = _expected(expected_path)
    expected_by_key = {(entry["resource"], entry["path"]): entry for entry in expected}
    actual_paths: set[tuple[str, str]] = set()
    for resource in sorted(before):
        for delta_path in _diff(before[resource], after[resource]):
            actual_paths.add((resource, delta_path))
    if actual_paths != set(expected_by_key):
        raise VerificationError("manifest delta does not match expected entries")
    for resource, delta_path in sorted(actual_paths):
        entry = expected_by_key[(resource, delta_path)]
        before_present, before_value = _at(before[resource], delta_path)
        after_present, after_value = _at(after[resource], delta_path)
        if before_present != entry["before_present"] or after_present != entry["after_present"]:
            raise VerificationError(f"delta mismatch at {resource}{delta_path}")
        if before_present and not _same_value(before_value, entry["before"]):
            raise VerificationError(f"delta mismatch at {resource}{delta_path}")
        if after_present and not _same_value(after_value, entry["after"]):
            raise VerificationError(f"delta mismatch at {resource}{delta_path}")
    return [{"resource": resource, "path": delta_path} for resource, delta_path in sorted(actual_paths)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = verify(args.before, args.after, args.expected)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        payload = {"verified_changes": receipt, "verified_count": len(receipt)}
        args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except VerificationError as exc:
        print(f"manifest delta verification failed: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("manifest delta verification failed: cannot read or write input/output", file=sys.stderr)
        return 1
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
