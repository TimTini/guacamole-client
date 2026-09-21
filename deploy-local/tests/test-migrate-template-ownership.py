import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace


SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "migrate-template-ownership.py"
SPEC = importlib.util.spec_from_file_location("migrate_template_ownership", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"cannot load migration script from {SCRIPT_PATH}")
migration = importlib.util.module_from_spec(SPEC)
sys.modules["migrate_template_ownership"] = migration
SPEC.loader.exec_module(migration)


class FakeReadOnlyRunner:
    def __init__(self, template_path: pathlib.Path, clone_root: pathlib.Path):
        self.template_path = template_path
        self.clone_root = clone_root
        self.backing_payload = {"virtual-size": 1024}
        self.clone_backing = str(template_path)

    def __call__(self, arguments):
        if arguments[0] == "qemu-img":
            return SimpleNamespace(stdout=json.dumps(self.backing_payload))
        if arguments[0] == "virsh":
            xml = f"""
<domain>
  <devices>
    <disk device='disk'>
      <source file='{self.clone_root / 'wtest.qcow2'}'/>
      <backingStore><source file='{self.clone_backing}'/></backingStore>
    </disk>
  </devices>
</domain>
"""
            return SimpleNamespace(stdout=xml)
        raise AssertionError(f"unexpected read-only command: {arguments!r}")


class MigrationFixtureTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "requires root for descriptor ownership fixture")
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        self.template_root = self.root / "templates"
        self.clone_root = self.root / "clones"
        self.template_root.mkdir()
        self.clone_root.mkdir()
        self.template_path = self.template_root / "windows11-v1.qcow2"
        self.inventory_path = self.template_root / "inventory.json"
        self.template_path.write_bytes(b"fixture-template-content")
        os.chown(self.template_path, 64055, 993)
        os.chmod(self.template_path, 0o444)
        digest = hashlib.sha256(self.template_path.read_bytes()).hexdigest()
        self.inventory = {
            "templates": [{
                "version": "windows11-v1",
                "path": str(self.template_path),
                "sha256": digest,
            }],
            "clones": [{"name": "wtest", "templateVersion": "windows11-v1"}],
        }
        self.inventory_path.write_text(json.dumps(self.inventory, sort_keys=True) + "\n", encoding="utf-8")
        self.runner = FakeReadOnlyRunner(self.template_path, self.clone_root)
        self.fchown_calls = []

    def tearDown(self):
        self.directory.cleanup()

    def context(self, **overrides):
        values = {
            "version": "windows11-v1",
            "template_root": self.template_root,
            "template_path": self.template_path,
            "inventory_path": self.inventory_path,
            "clone_root": self.clone_root,
            "run_command": self.runner,
        }
        values.update(overrides)
        return migration.MigrationContext(**values)

    def assert_owner(self, uid, gid):
        metadata = self.template_path.stat()
        self.assertEqual((metadata.st_uid, metadata.st_gid), (uid, gid))

    def test_success_uses_descriptor_fchown_and_preserves_content_and_inventory(self):
        original_content = self.template_path.read_bytes()
        original_inventory = self.inventory_path.read_bytes()

        def fchown(descriptor, uid, gid):
            self.fchown_calls.append((descriptor, uid, gid))
            os.fchown(descriptor, uid, gid)

        result = migration.migrate(self.context(fchown=fchown))

        self.assertEqual(len(self.fchown_calls), 1)
        self.assertEqual(self.fchown_calls[0][1:], (0, 0))
        self.assert_owner(0, 0)
        self.assertEqual(self.template_path.read_bytes(), original_content)
        self.assertEqual(hashlib.sha256(self.template_path.read_bytes()).hexdigest(), result["after"]["sha256"])
        self.assertEqual(self.inventory_path.read_bytes(), original_inventory)
        self.assertEqual(result["before"]["owner"], "64055:993")
        self.assertEqual(result["after"]["owner"], "0:0")

    def test_inventory_path_must_be_canonical(self):
        self.inventory["templates"][0]["path"] = str(self.template_root / "alias.qcow2")
        self.inventory_path.write_text(json.dumps(self.inventory), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            migration.migrate(self.context())
        self.assert_owner(64055, 993)

    def test_canonical_template_symlink_is_rejected(self):
        target = self.root / "outside.qcow2"
        target.write_bytes(self.template_path.read_bytes())
        os.chown(target, 64055, 993)
        os.chmod(target, 0o444)
        self.template_path.unlink()
        self.template_path.symlink_to(target)
        with self.assertRaises(RuntimeError):
            migration.migrate(self.context())
        self.assertEqual(len(self.fchown_calls), 0)

    def test_wrong_mode_is_rejected(self):
        os.chmod(self.template_path, 0o644)
        with self.assertRaises(RuntimeError):
            migration.migrate(self.context())
        self.assert_owner(64055, 993)

    def test_wrong_hash_is_rejected(self):
        self.inventory["templates"][0]["sha256"] = "0" * 64
        self.inventory_path.write_text(json.dumps(self.inventory), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            migration.migrate(self.context())
        self.assert_owner(64055, 993)

    def test_backing_file_is_rejected(self):
        self.runner.backing_payload = {"virtual-size": 1024, "backing-filename": "/tmp/base.qcow2"}
        with self.assertRaises(RuntimeError):
            migration.migrate(self.context())
        self.assert_owner(64055, 993)

    def test_dependent_clone_backing_mismatch_is_rejected(self):
        self.runner.clone_backing = str(self.root / "wrong.qcow2")
        with self.assertRaises(RuntimeError):
            migration.migrate(self.context())
        self.assert_owner(64055, 993)

    def test_fchown_failure_does_not_change_content_or_inventory(self):
        original_content = self.template_path.read_bytes()
        original_inventory = self.inventory_path.read_bytes()

        def failing_fchown(_descriptor, _uid, _gid):
            raise OSError("fixture chown denied")

        with self.assertRaises(OSError):
            migration.migrate(self.context(fchown=failing_fchown))
        self.assert_owner(64055, 993)
        self.assertEqual(self.template_path.read_bytes(), original_content)
        self.assertEqual(self.inventory_path.read_bytes(), original_inventory)

    def test_inventory_change_after_fchown_is_detected(self):
        original_content = self.template_path.read_bytes()

        def change_inventory():
            changed = json.loads(self.inventory_path.read_text(encoding="utf-8"))
            changed["templates"][0]["sha256"] = "f" * 64
            self.inventory_path.write_text(json.dumps(changed), encoding="utf-8")

        with self.assertRaises(RuntimeError):
            migration.migrate(self.context(after_chown=change_inventory))
        self.assert_owner(0, 0)
        self.assertEqual(self.template_path.read_bytes(), original_content)

    def test_post_fchown_verification_failure_is_reported(self):
        original_content = self.template_path.read_bytes()
        original_inventory = self.inventory_path.read_bytes()

        def drift_mode():
            os.chmod(self.template_path, 0o644)

        with self.assertRaises(RuntimeError):
            migration.migrate(self.context(after_chown=drift_mode))
        self.assert_owner(0, 0)
        self.assertEqual(self.template_path.read_bytes(), original_content)
        self.assertEqual(self.inventory_path.read_bytes(), original_inventory)


if __name__ == "__main__":
    unittest.main()
