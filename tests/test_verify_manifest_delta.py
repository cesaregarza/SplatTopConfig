from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verify_manifest_delta import VerificationError, verify


class VerifyManifestDeltaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write(self, name: str, content: str) -> Path:
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def expected(self, records: list[dict]) -> Path:
        return self.write("expected.json", json.dumps(records))

    def test_accepts_exact_delta_and_receipt_uses_identity_paths(self) -> None:
        before = self.write("before.yaml", "kind: ConfigMap\nmetadata: {name: sample, namespace: demo}\ndata: {mode: old}\n")
        after = self.write("after.yaml", "kind: ConfigMap\nmetadata: {name: sample, namespace: demo}\ndata: {mode: new}\n")
        expected = self.expected([{"resource": "demo/ConfigMap/sample", "path": "/data/mode", "before": "old", "after": "new"}])
        self.assertEqual(verify(before, after, expected), [{"resource": "demo/ConfigMap/sample", "path": "/data/mode"}])

    def test_rejects_unexpected_delta(self) -> None:
        before = self.write("before.yaml", "kind: ConfigMap\nmetadata: {name: sample}\ndata: {mode: old}\n")
        after = self.write("after.yaml", "kind: ConfigMap\nmetadata: {name: sample}\ndata: {mode: new}\n")
        with self.assertRaisesRegex(VerificationError, "does not match"):
            verify(before, after, self.expected([]))

    def test_distinguishes_missing_from_explicit_null(self) -> None:
        before = self.write("before.yaml", "kind: ConfigMap\nmetadata: {name: sample}\ndata: {}\n")
        after = self.write("after.yaml", "kind: ConfigMap\nmetadata: {name: sample}\ndata: {value: null}\n")
        expected = self.expected([{"resource": "/ConfigMap/sample", "path": "/data/value", "before_present": False, "after": None}])
        self.assertEqual(verify(before, after, expected)[0]["resource"], "/ConfigMap/sample")

    def test_rejects_inventory_change(self) -> None:
        before = self.write("before.yaml", "kind: ConfigMap\nmetadata: {name: sample}\n")
        after = self.write("after.yaml", "---\nkind: ConfigMap\nmetadata: {name: sample}\n---\nkind: Secret\nmetadata: {name: added}\n")
        with self.assertRaisesRegex(VerificationError, "inventory"):
            verify(before, after, self.expected([]))

    def test_rejects_duplicate_identity(self) -> None:
        duplicate = self.write("duplicate.yaml", "---\nkind: ConfigMap\nmetadata: {name: sample}\n---\nkind: ConfigMap\nmetadata: {name: sample}\n")
        with self.assertRaisesRegex(VerificationError, "duplicate resource"):
            verify(duplicate, duplicate, self.expected([]))

    def test_supports_json_pointer_escaped_keys(self) -> None:
        before = self.write("before.yaml", "kind: ConfigMap\nmetadata: {name: sample}\ndata: {'a/b~c': old}\n")
        after = self.write("after.yaml", "kind: ConfigMap\nmetadata: {name: sample}\ndata: {'a/b~c': new}\n")
        expected = self.expected([{"resource": "/ConfigMap/sample", "path": "/data/a~1b~0c", "before": "old", "after": "new"}])
        self.assertEqual(len(verify(before, after, expected)), 1)

    def test_rejects_duplicate_expected(self) -> None:
        before = self.write("before.yaml", "kind: ConfigMap\nmetadata: {name: sample}\ndata: {mode: old}\n")
        after = self.write("after.yaml", "kind: ConfigMap\nmetadata: {name: sample}\ndata: {mode: new}\n")
        record = {"resource": "/ConfigMap/sample", "path": "/data/mode", "before": "old", "after": "new"}
        with self.assertRaisesRegex(VerificationError, "duplicate expected"):
            verify(before, after, self.expected([record, record]))

    def test_rejects_wrong_expected_value(self) -> None:
        before = self.write("before.yaml", "kind: ConfigMap\nmetadata: {name: sample}\ndata: {mode: old}\n")
        after = self.write("after.yaml", "kind: ConfigMap\nmetadata: {name: sample}\ndata: {mode: new}\n")
        expected = self.expected([{"resource": "/ConfigMap/sample", "path": "/data/mode", "before": "wrong", "after": "new"}])
        with self.assertRaisesRegex(VerificationError, "delta mismatch"):
            verify(before, after, expected)

    def test_rejects_malformed_expected_contracts(self) -> None:
        manifest = self.write("manifest.yaml", "kind: ConfigMap\nmetadata: {name: sample}\n")
        valid = {"resource": "/ConfigMap/sample", "path": "/data", "before": None, "after": None}
        for patch in ({"resource": "//sample"}, {"resource": "/ConfigMap/"},
                      {"path": "/data/~2"}, {"before_present": "false"}):
            with self.subTest(patch=patch), self.assertRaises(VerificationError):
                verify(manifest, manifest, self.expected([valid | patch]))
        for key in ("before", "after"):
            record = valid.copy()
            del record[key]
            with self.subTest(missing=key), self.assertRaises(VerificationError):
                verify(manifest, manifest, self.expected([record]))


if __name__ == "__main__":
    unittest.main()
