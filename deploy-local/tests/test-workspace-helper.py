import importlib.util
import dataclasses
import hashlib
import io
import json
import math
import os
import pathlib
import re
import shutil
import stat as stat_module
import sys
import subprocess
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock


HELPER_PATH = pathlib.Path(__file__).resolve().parents[1] / "workspace-helper.py"
SPEC = importlib.util.spec_from_file_location("workspace_helper", HELPER_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"cannot load helper from {HELPER_PATH}")
workspace_helper = importlib.util.module_from_spec(SPEC)
sys.modules["workspace_helper"] = workspace_helper
SPEC.loader.exec_module(workspace_helper)

ValidationError = workspace_helper.ValidationError
ConflictError = workspace_helper.ConflictError
LockError = workspace_helper.LockError
assert_unique_clone = workspace_helper.assert_unique_clone
load_inventory = workspace_helper.load_inventory
save_inventory_atomic = workspace_helper.save_inventory_atomic
validate_assignee = workspace_helper.validate_assignee
validate_vm_name = workspace_helper.validate_vm_name
workspace_lock = workspace_helper.workspace_lock
create_template = workspace_helper.create_template
TemplateRecord = workspace_helper.TemplateRecord
CloneRecord = workspace_helper.CloneRecord
ALLOW_LIVE_SYSTEMD_TESTS = os.environ.get("GUACAMOLE_ALLOW_LIVE_SYSTEMD_TESTS") == "1"


class CockpitContractTests(unittest.TestCase):
    def test_job_status_is_atomic_bounded_and_does_not_store_raw_error(self):
        with tempfile.TemporaryDirectory() as directory:
            status_dir = pathlib.Path(directory) / "jobs"
            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir):
                payload = workspace_helper.write_job_status(
                    "vm-new",
                    {
                        "status": "sync-failed",
                        "stage": "sync",
                        "error": {
                            "code": "SYNC_FAILED",
                            "stage": "sync",
                            "message": "secret-token=" + ("x" * 100000),
                        },
                    },
                )
                status_path = status_dir / "vm-new.json"
                self.assertTrue(status_path.is_file())
                self.assertLessEqual(status_path.stat().st_size, workspace_helper.MAX_JOB_STATUS_BYTES)
                stored = json.loads(status_path.read_text(encoding="utf-8"))
                self.assertEqual(stored["status"], "sync-failed")
                self.assertNotIn("secret-token=", stored["error"]["message"])
                self.assertEqual(payload["name"], "vm-new")
                self.assertFalse(list(status_dir.glob("*.tmp")))

    def test_start_job_uses_fixed_systemd_argv_and_no_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            save_inventory_atomic(
                {
                    "templates": [{"version": "windows11-v1", "sourceDomain": "windows11"}],
                    "clones": [],
                },
                inventory_path,
            )
            calls = []

            def runner(arguments, timeout=None):
                calls.append((list(arguments), timeout))
                if arguments[0] == "systemctl":
                    return SimpleNamespace(stdout="LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\n")
                return SimpleNamespace(stdout="Running as unit: guacamole-workspace-vm-new.service\n")

            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "run_command", runner):
                result = workspace_helper.start_workspace_job(
                    "vm-new", "USER", "demo", "windows11-v1", memory_mib=4096, vcpus=2
                )
            self.assertTrue(result["ok"])
            self.assertEqual(sum(arguments[0] == "systemd-run" for arguments, _timeout in calls), 1)
            argv = next(arguments for arguments, _timeout in calls if arguments[0] == "systemd-run")
            self.assertEqual(argv[0], "systemd-run")
            self.assertIn("--unit=guacamole-workspace-vm-new.service", argv)
            self.assertIn("--uid=root", argv)
            self.assertIn("--gid=root", argv)
            self.assertIn("--no-block", argv)
            self.assertIn("--collect", argv)
            self.assertIn("--service-type=oneshot", argv)
            self.assertIn("--property=UMask=0077", argv)
            self.assertIn("--property=KillMode=control-group", argv)
            self.assertIn("/usr/local/libexec/guacamole-workspace-helper", argv)
            self.assertIn("--job-id", argv)
            self.assertNotIn("sh", argv)
            self.assertNotIn("-c", argv)
            self.assertNotIn("secret", json.dumps(argv))

    def test_launcher_failure_with_unknown_systemd_state_is_stale_and_not_repairable(self):
        query_failures = {
            "permission": {"_query": "error", "_errorCode": "COMMAND_PERMISSION"},
            "timeout": {"_query": "error", "_errorCode": "COMMAND_TIMEOUT"},
            "dbus": {"_query": "error", "_errorCode": "DBUS_ERROR"},
            "malformed": {},
        }
        launcher_failures = {
            "nonzero": subprocess.CalledProcessError(1, ["systemd-run"]),
            "exception": OSError("systemd-run unavailable"),
        }
        for query_name, query_state in query_failures.items():
            for launcher_name, launcher_failure in launcher_failures.items():
                with self.subTest(query=query_name, launcher=launcher_name), tempfile.TemporaryDirectory() as directory:
                    root = pathlib.Path(directory)
                    inventory_path = root / "inventory.json"
                    status_dir = root / "jobs"
                    save_inventory_atomic(
                        {"templates": [], "clones": []},
                        inventory_path,
                    )
                    systemd_not_found = {"_query": "not-found", "LoadState": "not-found"}
                    with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                        workspace_helper, "JOB_STATUS_DIR", status_dir
                    ), mock.patch.object(
                        workspace_helper,
                        "_systemd_unit_state",
                        side_effect=[systemd_not_found, query_state],
                    ), mock.patch.object(
                        workspace_helper,
                        "run_command",
                        side_effect=launcher_failure,
                    ) as run:
                        first = workspace_helper.start_workspace_job(
                            "vm-launch-failure",
                            "USER",
                            "demo",
                            "windows11-v1",
                        )
                        second = workspace_helper.start_workspace_job(
                            "vm-launch-failure",
                            "USER",
                            "demo",
                            "windows11-v1",
                        )
                        repair = workspace_helper.repair_workspace_job("vm-launch-failure")
                    self.assertEqual(first["status"], "stale")
                    self.assertEqual(first["job"]["errorCode"], "JOB_SYSTEMD_STATE_UNKNOWN")
                    self.assertFalse(first.get("repairAvailable", False))
                    self.assertEqual(second["status"], "stale")
                    self.assertEqual(second["job"]["errorCode"], "JOB_SYSTEMD_STATE_UNKNOWN")
                    self.assertFalse(second.get("repairAvailable", False))
                    self.assertEqual(repair["status"], "stale")
                    self.assertEqual(repair["job"]["errorCode"], "JOB_SYSTEMD_STATE_UNKNOWN")
                    self.assertFalse(repair.get("repairAvailable", False))
                    self.assertEqual(run.call_count, 1)

    def test_launcher_failure_with_authoritative_not_found_is_repairable_start_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            save_inventory_atomic({"templates": [], "clones": []}, inventory_path)
            systemd_not_found = {"_query": "not-found", "LoadState": "not-found"}
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(
                workspace_helper,
                "_systemd_unit_state",
                side_effect=[systemd_not_found, systemd_not_found],
            ), mock.patch.object(
                workspace_helper,
                "run_command",
                side_effect=subprocess.CalledProcessError(1, ["systemd-run"]),
            ) as run:
                result = workspace_helper.start_workspace_job(
                    "vm-launch-missing",
                    "USER",
                    "demo",
                    "windows11-v1",
                )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["job"]["errorCode"], "JOB_START_FAILED")
            self.assertTrue(result.get("repairAvailable", False))
            self.assertEqual(run.call_count, 1)

    def test_list_rehydrates_ready_and_pending_workspaces_from_durable_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            save_inventory_atomic(
                {
                    "templates": [{"version": "windows11-v1", "sourceDomain": "windows11"}],
                    "clones": [
                        {
                            "name": "wtest",
                            "mac": "52:54:00:f5:a2:88",
                            "ip": "192.168.250.20",
                            "assigneeType": "USER",
                            "assigneeName": "taitt7",
                            "templateVersion": "windows11-v1",
                            "status": "ready",
                        },
                        {
                            "name": "pending-vm",
                            "mac": "52:54:00:f5:a2:89",
                            "ip": "192.168.250.21",
                            "assigneeType": "USER",
                            "assigneeName": "demo",
                            "templateVersion": "windows11-v1",
                            "status": "pending",
                        },
                    ],
                },
                inventory_path,
            )

            def domain_runner(arguments):
                return SimpleNamespace(stdout="wtest\npending-vm\n")

            def assignee_runner(arguments, timeout=None):
                return SimpleNamespace(stdout="USER\tdemo\nUSER\ttaitt7\n")

            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir):
                payload = workspace_helper.build_list_payload(
                    inventory_path=inventory_path,
                    runner=domain_runner,
                    assignee_runner=assignee_runner,
                )
            workspaces = {item["name"]: item for item in payload["workspaces"]}
            self.assertEqual(workspaces["wtest"]["status"], "ready")
            self.assertEqual(workspaces["wtest"]["ip"], "192.168.250.20")
            self.assertEqual(workspaces["wtest"]["assigneeName"], "taitt7")
            self.assertEqual(workspaces["pending-vm"]["status"], "pending")

    def test_list_respects_repair_inventory_transition_until_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            previous = {
                "name": "vm-transition", "mac": "52:54:00:20:00:31", "ip": "192.168.250.31",
                "assigneeType": "USER", "assigneeName": "demo", "templateVersion": "windows11-v1",
                "syncAttemptId": "11111111-2222-4333-8444-555555555555", "status": "sync-failed", "connectionId": 41,
            }
            desired = dict(previous, status="ready")

            def runner(arguments, timeout=None):
                del timeout
                if arguments[0] == "systemctl":
                    return SimpleNamespace(stdout="LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n")
                return SimpleNamespace(stdout="")

            def assignee_runner(arguments, timeout=None):
                del arguments, timeout
                return SimpleNamespace(stdout="")

            def list_one():
                result = workspace_helper.build_list_payload(
                    inventory_path=inventory_path, runner=runner, assignee_runner=assignee_runner
                )
                return next(item for item in result["workspaces"] if item["name"] == "vm-transition")

            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir):
                save_inventory_atomic({"templates": [], "clones": [desired]}, inventory_path)
                workspace_helper.write_job_status(
                    "vm-transition",
                    {
                        "status": "running", "stage": "sync", "result": desired,
                        "inventoryTransition": {"phase": "prepared", "previous": previous, "desired": desired},
                    },
                )
                prepared = list_one()
                self.assertEqual(prepared["status"], "syncing")
                self.assertEqual(prepared["stage"], "sync")
                self.assertFalse(prepared["repairAvailable"])

                workspace_helper.write_job_status(
                    "vm-transition",
                    {
                        "status": "running", "stage": "sync", "result": desired,
                        "inventoryTransition": {"phase": "committed", "previous": previous, "desired": desired},
                    },
                )
                committed = list_one()
                self.assertEqual(committed["status"], "ready")
                self.assertEqual(committed["stage"], "permissions")

                save_inventory_atomic({"templates": [], "clones": [previous]}, inventory_path)
                workspace_helper.write_job_status(
                    "vm-transition",
                    {
                        "status": "sync-failed", "stage": "sync", "result": previous,
                        "inventoryTransition": {"phase": "restored", "previous": previous, "desired": desired},
                    },
                )
                restored = list_one()
                self.assertEqual(restored["status"], "sync-failed")
                self.assertTrue(restored["repairAvailable"])

    def test_repair_is_idempotent_and_does_not_schedule_duplicate_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            save_inventory_atomic(
                {
                    "templates": [{"version": "windows11-v1", "sourceDomain": "windows11"}],
                    "clones": [{
                        "name": "pending-vm",
                        "mac": "52:54:00:f5:a2:89",
                        "ip": "192.168.250.21",
                        "assigneeType": "USER",
                        "assigneeName": "demo",
                        "templateVersion": "windows11-v1",
                        "status": "pending",
                    }],
                },
                inventory_path,
            )
            calls = []
            systemctl_calls = {"count": 0}

            def runner(arguments, timeout=None):
                calls.append(list(arguments))
                if arguments[0] == "systemctl":
                    systemctl_calls["count"] += 1
                    if systemctl_calls["count"] == 1:
                        return SimpleNamespace(stdout="LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\n")
                    return SimpleNamespace(stdout="LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n")
                return SimpleNamespace(stdout="Running as unit: guacamole-workspace-pending-vm.service\n")

            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "run_command", runner):
                first = workspace_helper.repair_workspace_job("pending-vm")
                second = workspace_helper.repair_workspace_job("pending-vm")
            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertEqual(sum(call[0] == "systemd-run" for call in calls), 1)
            self.assertEqual(first["jobId"], second["jobId"])
            self.assertTrue(any(call[0] == "systemd-run" and "repair" in call for call in calls))

    def test_owned_repair_sql_requires_attempt_marker_and_exact_connection_id(self):
        record = CloneRecord(
            name="vm-repair",
            mac="52:54:00:20:00:01",
            ip="192.168.250.20",
            assigneeType="USER",
            assigneeName="demo",
            templateVersion="windows11-v1",
            syncAttemptId="11111111-2222-4333-8444-555555555555",
        )
        sql = workspace_helper.build_guacamole_sync_sql(
            record,
            connection_id=14,
            require_owned_connection=True,
        )
        self.assertIn("connection.connection_id = :'sync_connection_id'::integer", sql)
        self.assertIn("attempt.attribute_value = :'sync_attempt_id'", sql)
        self.assertIn("assignee.name = :'sync_assignee_name'", sql)
        self.assertNotIn("FROM guacamole_connection\nWHERE connection_name = :'sync_name'", sql)

    def test_repair_keeps_waiting_rdp_and_never_calls_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            save_inventory_atomic(
                {"templates": [], "clones": [{
                    "name": "vm-rdp",
                    "mac": "52:54:00:20:00:01",
                    "ip": "192.168.250.20",
                    "assigneeType": "USER",
                    "assigneeName": "demo",
                    "templateVersion": "windows11-v1",
                    "syncAttemptId": "11111111-2222-4333-8444-555555555555",
                    "status": "sync-failed",
                }]},
                inventory_path,
            )
            error = workspace_helper.HelperError("RDP is not ready", code=workspace_helper.RDP_NOT_READY, stage="wait-rdp")
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "workspace_lock", lambda: nullcontext()), mock.patch.object(
                workspace_helper, "_verify_repair_live_state", side_effect=error
            ), mock.patch.object(workspace_helper, "sync_guacamole") as sync:
                with self.assertRaises(workspace_helper.HelperError):
                    workspace_helper._run_repair_job(SimpleNamespace(name="vm-rdp"), "job-vm-rdp")
                sync.assert_not_called()
                self.assertEqual(workspace_helper.read_job_status("vm-rdp")["status"], "waiting-rdp")

    def test_detached_repair_inventory_loss_is_visible_non_repairable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            status_dir = root / "jobs"
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", root / "missing-inventory.json"), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "workspace_lock", lambda: nullcontext()), mock.patch.object(
                workspace_helper, "load_inventory", return_value={"templates": [], "clones": []}
            ):
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    workspace_helper._run_repair_job(SimpleNamespace(name="vm-lost"), "job-vm-lost")
                self.assertEqual(failure.exception.code, "WORKSPACE_NOT_FOUND")
                status = workspace_helper.read_job_status("vm-lost")
                self.assertEqual(status["status"], "failed-without-inventory")
                self.assertEqual(status["errorCode"], "WORKSPACE_NOT_FOUND")
                listed = workspace_helper.build_list_payload(
                    inventory_path=root / "missing-inventory.json",
                    runner=lambda arguments: SimpleNamespace(stdout=""),
                    assignee_runner=lambda arguments, timeout=None: SimpleNamespace(stdout=""),
                )
                orphan = next(item for item in listed["workspaces"] if item.get("name") == "vm-lost")
                self.assertEqual(orphan["status"], "failed-without-inventory")
                self.assertTrue(orphan["orphaned"])
                response = workspace_helper.repair_workspace_job("vm-lost")
                self.assertFalse(response.get("repairAvailable", False))
                self.assertEqual(response["status"], "failed-without-inventory")

    def test_stale_active_job_is_reconciled_when_systemd_unit_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            status_dir = pathlib.Path(directory) / "jobs"
            status_dir.mkdir()
            status_dir.chmod(0o750)
            (status_dir / "vm-stale.json").write_text(json.dumps({
                "schema": workspace_helper.JOB_STATUS_SCHEMA,
                "jobId": "guacamole-workspace-vm-stale.service",
                "name": "vm-stale",
                "status": "running",
                "stage": "clone",
                "progress": [],
                "createdAt": workspace_helper._job_now(),
                "updatedAt": workspace_helper._job_now(),
            }), encoding="utf-8")
            (status_dir / "vm-stale.json").chmod(0o600)
            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir), mock.patch.object(
                workspace_helper, "_systemd_unit_state", return_value={"_query": "not-found", "LoadState": "not-found"}
            ):
                result = workspace_helper.list_job_statuses()
            self.assertEqual(result[0]["status"], "stale")
            self.assertEqual(result[0]["errorCode"], "JOB_UNIT_MISSING")

    def test_fresh_failed_and_terminal_units_are_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            status_dir = pathlib.Path(directory) / "jobs"
            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir):
                workspace_helper.write_job_status("vm-failed-unit", {"status": "running", "stage": "domain"})
                workspace_helper.write_job_status("vm-terminal-unit", {"status": "running", "stage": "domain"})
                with mock.patch.object(
                    workspace_helper,
                    "_systemd_unit_state",
                    side_effect=lambda unit, runner=None: (
                        {"ActiveState": "failed", "SubState": "failed", "Result": "exit-code"}
                        if "vm-failed-unit" in unit
                        else {"ActiveState": "active", "SubState": "exited", "Result": "success"}
                    ),
                ):
                    result = {item["name"]: item for item in workspace_helper.list_job_statuses()}
            self.assertEqual(result["vm-failed-unit"]["status"], "failed")
            self.assertEqual(result["vm-failed-unit"]["errorCode"], "JOB_UNIT_FAILED")
            self.assertEqual(result["vm-terminal-unit"]["status"], "stale")
            self.assertEqual(result["vm-terminal-unit"]["errorCode"], "JOB_UNIT_TERMINAL")

    def test_systemd_query_errors_are_unknown_and_block_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            status_dir = root / "jobs"
            inventory_path = root / "inventory.json"
            save_inventory_atomic({"templates": [], "clones": [{
                "name": "vm-systemd-unknown", "mac": "52:54:00:20:00:01", "ip": "192.168.250.20",
                "assigneeType": "USER", "assigneeName": "demo", "templateVersion": "windows11-v1",
                "status": "sync-failed",
            }]}, inventory_path)
            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ):
                workspace_helper.write_job_status("vm-systemd-unknown", {"status": "running"})
                with mock.patch.object(
                    workspace_helper, "_systemd_unit_state",
                    return_value={"_query": "error", "_errorCode": "COMMAND_TIMEOUT"},
                ):
                    listed = workspace_helper.list_job_statuses()
                    response = workspace_helper.repair_workspace_job("vm-systemd-unknown")
            self.assertEqual(listed[0]["status"], "stale")
            self.assertEqual(listed[0]["errorCode"], "JOB_SYSTEMD_STATE_UNKNOWN")
            self.assertFalse(response.get("repairAvailable", False))
            self.assertEqual(response["status"], "stale")

    def test_systemd_not_found_is_distinct_from_query_failure(self):
        def missing_runner(arguments, timeout=None):
            del arguments, timeout
            return SimpleNamespace(stdout="LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\n")

        def broken_runner(arguments, timeout=None):
            del arguments, timeout
            raise PermissionError("dbus denied")

        missing = workspace_helper._systemd_unit_state("guacamole-workspace-missing.service", missing_runner)
        broken = workspace_helper._systemd_unit_state("guacamole-workspace-broken.service", broken_runner)
        self.assertEqual(missing.get("_query"), "not-found")
        self.assertEqual(broken.get("_query"), "error")
        self.assertEqual(broken.get("_errorCode"), "COMMAND_FAILED")

    def test_invalid_or_future_active_ledger_timestamps_are_bounded_and_blocked(self):
        for updated_at, expected_code in (
            (None, "JOB_TIMESTAMP_MISSING"),
            ("not-a-timestamp", "JOB_TIMESTAMP_INVALID"),
            ((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "JOB_TIMESTAMP_FUTURE"),
        ):
            with self.subTest(expected_code=expected_code), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                status_dir = root / "jobs"
                status_dir.mkdir()
                status_dir.chmod(0o750)
                value = {
                    "schema": workspace_helper.JOB_STATUS_SCHEMA,
                    "jobId": "guacamole-workspace-vm-bounded.service",
                    "name": "vm-bounded",
                    "status": "running",
                    "stage": "domain",
                    "progress": [],
                    "createdAt": workspace_helper._job_now(),
                }
                if updated_at is not None:
                    value["updatedAt"] = updated_at
                path = status_dir / "vm-bounded.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                path.chmod(0o600)
                with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir), mock.patch.object(
                    workspace_helper, "_systemd_unit_state",
                    return_value={"_query": "ok", "LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success"},
                ):
                    result = workspace_helper.list_job_statuses()
                self.assertEqual(result[0]["status"], "stale")
                self.assertEqual(result[0]["errorCode"], expected_code)

    def test_empty_systemd_state_is_bounded_and_blocks_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            status_dir = pathlib.Path(directory) / "jobs"
            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir):
                workspace_helper.write_job_status("vm-empty-systemd", {"status": "running"})
                with mock.patch.object(workspace_helper, "_systemd_unit_state", return_value={}):
                    result = workspace_helper.list_job_statuses()
            self.assertEqual(result[0]["status"], "stale")
            self.assertEqual(result[0]["errorCode"], "JOB_SYSTEMD_STATE_UNKNOWN")

    @unittest.skipUnless(
        ALLOW_LIVE_SYSTEMD_TESTS and os.geteuid() == 0 and shutil.which("systemd-run") and shutil.which("systemctl") and pathlib.Path("/run/systemd/system").is_dir(),
        "requires a root systemd runtime",
    )
    def test_detached_systemd_worker_survives_launcher_return_and_fresh_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            status_dir = root / "jobs"
            status_dir.mkdir()
            status_dir.chmod(workspace_helper.JOB_STATUS_DIR_MODE)
            name = "vm-systemd-fixture"
            status_path = status_dir / f"{name}.json"
            unit = f"guacamole-workspace-fixture-{os.getpid()}-{time.time_ns()}.service"
            payload = {
                "schema": workspace_helper.JOB_STATUS_SCHEMA,
                "jobId": unit,
                "name": name,
                "status": "ready",
                "stage": "permissions",
                "progress": [],
                "createdAt": workspace_helper._job_now(),
                "updatedAt": workspace_helper._job_now(),
            }
            script = (
                "import json,os,time; time.sleep(0.25); "
                f"p={str(status_path)!r}; t=p+'.tmp'; "
                f"open(t,'w',encoding='utf-8').write(json.dumps({payload!r})); "
                "os.chmod(t,0o600); os.replace(t,p)"
            )
            argv = [
                "systemd-run", "--no-block", "--collect", f"--unit={unit}",
                "--property=UMask=0077", "/usr/bin/python3", "-c", script,
            ]
            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir):
                try:
                    workspace_helper.run_command(argv, timeout=5)
                    deadline = time.monotonic() + 5
                    observed = None
                    while time.monotonic() < deadline:
                        observed = workspace_helper.read_job_status(name)
                        if observed is not None and observed.get("status") == "ready":
                            break
                        time.sleep(0.05)
                    listed = workspace_helper.list_job_statuses()
                finally:
                    subprocess.run(["systemctl", "stop", unit], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertIsNotNone(observed)
            self.assertEqual(observed["status"], "ready")
            self.assertEqual(listed[0]["name"], name)
            self.assertEqual(listed[0]["status"], "ready")

    @unittest.skipUnless(
        ALLOW_LIVE_SYSTEMD_TESTS and os.geteuid() == 0 and shutil.which("systemd-run") and shutil.which("systemctl") and pathlib.Path("/run/systemd/system").is_dir(),
        "requires a root systemd runtime",
    )
    def test_production_scheduler_uses_detached_worker_entrypoint_and_fresh_rehydrate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            status_dir = root / "jobs"
            status_dir.mkdir()
            status_dir.chmod(workspace_helper.JOB_STATUS_DIR_MODE)
            inventory_path = root / "inventory.json"
            save_inventory_atomic({"templates": [], "clones": []}, inventory_path)
            name = "vm-scheduler-fixture"
            unit = workspace_helper._job_id_for_name(name)
            status_path = status_dir / f"{name}.json"
            payload = {
                "schema": workspace_helper.JOB_STATUS_SCHEMA,
                "jobId": unit,
                "name": name,
                "status": "ready",
                "stage": "permissions",
                "progress": [],
                "createdAt": workspace_helper._job_now(),
                "updatedAt": workspace_helper._job_now(),
            }
            worker_path = root / "detached-worker.py"
            worker_path.write_text(
                "#!/usr/bin/python3\n"
                "import json, os, time\n"
                "time.sleep(0.2)\n"
                f"path = {str(status_path)!r}\n"
                f"payload = {payload!r}\n"
                "temporary = path + '.tmp'\n"
                "with open(temporary, 'w', encoding='utf-8') as stream:\n"
                "    json.dump(payload, stream)\n"
                "os.chmod(temporary, 0o600)\n"
                "os.replace(temporary, path)\n",
                encoding="utf-8",
            )
            worker_path.chmod(0o755)
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "HELPER_EXECUTABLE", worker_path):
                try:
                    queued = workspace_helper.start_workspace_job(name, "USER", "demo", "windows11-v1")
                    self.assertEqual(queued["status"], "queued")
                    self.assertEqual(queued["jobId"], unit)
                    self.assertEqual(queued["job"]["status"], "queued")
                    deadline = time.monotonic() + 5
                    observed = None
                    while time.monotonic() < deadline:
                        observed = workspace_helper.read_job_status(name)
                        if observed is not None and observed.get("status") == "ready":
                            break
                        time.sleep(0.05)
                    listed = workspace_helper.list_job_statuses()
                finally:
                    subprocess.run(["systemctl", "stop", unit], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertIsNotNone(observed)
            self.assertEqual(observed["status"], "ready")
            self.assertEqual(listed[0]["status"], "ready")

    def test_oversized_job_file_is_ignored_before_json_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            status_dir = pathlib.Path(directory) / "jobs"
            status_dir.mkdir()
            status_dir.chmod(workspace_helper.JOB_STATUS_DIR_MODE)
            large_path = status_dir / "vm-large.json"
            large_path.write_bytes(b"{" + b"x" * workspace_helper.MAX_JOB_STATUS_BYTES)
            large_path.chmod(workspace_helper.JOB_STATUS_FILE_MODE)
            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir):
                self.assertIsNone(workspace_helper.read_job_status("vm-large"))
                self.assertEqual(workspace_helper.list_job_statuses(), [])

    def test_job_status_read_and_write_reject_final_and_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            status_dir = root / "jobs"
            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir):
                workspace_helper.write_job_status("vm-target", {"status": "pending"})
                os.symlink(status_dir / "vm-target.json", status_dir / "vm-link.json")
                self.assertIsNone(workspace_helper.read_job_status("vm-link"))
                self.assertFalse(any(item.get("name") == "vm-link" for item in workspace_helper.list_job_statuses()))

            target_dir = root / "real-jobs"
            target_dir.mkdir()
            target_dir.chmod(0o750)
            linked_dir = root / "linked-jobs"
            os.symlink(target_dir, linked_dir)
            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", linked_dir):
                self.assertIsNone(workspace_helper.read_job_status("vm-link"))
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    workspace_helper.write_job_status("vm-link", {"status": "pending"})
                self.assertEqual(failure.exception.code, "JOB_STATUS_INVALID")

    def test_job_status_rejects_substitution_between_stat_and_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            status_dir = root / "jobs"
            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir):
                workspace_helper.write_job_status("vm-race", {"status": "pending"})
                parent_fd, directory_fd = workspace_helper._open_verified_job_directory(create=False)
                original_open = workspace_helper.os.open
                target = status_dir / "vm-race.json"
                replacement = status_dir / "vm-race.old.json"
                swapped = {"done": False}

                def swap_before_open(path, flags, *args, **kwargs):
                    if kwargs.get("dir_fd") == directory_fd and path == "vm-race.json" and not swapped["done"]:
                        swapped["done"] = True
                        target.rename(replacement)
                        target.write_text("{}\n", encoding="utf-8")
                        target.chmod(workspace_helper.JOB_STATUS_FILE_MODE)
                    return original_open(path, flags, *args, **kwargs)

                try:
                    with mock.patch.object(workspace_helper.os, "open", side_effect=swap_before_open):
                        self.assertIsNone(workspace_helper._read_job_status_from_directory("vm-race", directory_fd))
                finally:
                    os.close(directory_fd)
                    os.close(parent_fd)
            self.assertTrue(swapped["done"])

    def test_repair_template_hash_and_mode_fail_before_live_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            template_path = root / "windows11-v1.qcow2"
            template_path.write_bytes(b"template-content")
            template_path.chmod(0o444)
            inventory_path = root / "inventory.json"
            save_inventory_atomic({"templates": [{
                "version": "windows11-v1", "sourceDomain": "windows11", "path": str(template_path),
                "sha256": "0" * 64, "virtualSize": 1, "createdAt": "now",
            }], "clones": [{
                "name": "vm-template", "mac": "52:54:00:20:00:01", "ip": "192.168.250.20",
                "assigneeType": "USER", "assigneeName": "demo", "templateVersion": "windows11-v1",
                "syncAttemptId": "11111111-2222-4333-8444-555555555555", "status": "sync-failed",
            }]}, inventory_path)
            record = CloneRecord(
                name="vm-template",
                mac="52:54:00:20:00:01",
                ip="192.168.250.20",
                assigneeType="USER",
                assigneeName="demo",
                templateVersion="windows11-v1",
                syncAttemptId="11111111-2222-4333-8444-555555555555",
            )
            calls = []

            def runner(arguments, timeout=None):
                calls.append(arguments)
                raise AssertionError("live checks must not run after template rejection")

            with mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), self.assertRaises(workspace_helper.HelperError) as failure:
                workspace_helper._verify_repair_live_state(record, runner, inventory_path=inventory_path)
            self.assertEqual(failure.exception.code, "TEMPLATE_HASH_INVALID")
            self.assertEqual(calls, [])
            template_path.chmod(0o644)
            save_inventory_atomic({"templates": [{
                "version": "windows11-v1", "sourceDomain": "windows11", "path": str(template_path),
                "sha256": hashlib.sha256(template_path.read_bytes()).hexdigest(), "virtualSize": 1, "createdAt": "now",
            }], "clones": [{
                "name": "vm-template", "mac": "52:54:00:20:00:01", "ip": "192.168.250.20",
                "assigneeType": "USER", "assigneeName": "demo", "templateVersion": "windows11-v1",
                "syncAttemptId": "11111111-2222-4333-8444-555555555555", "status": "sync-failed",
            }]}, inventory_path)
            with mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), self.assertRaises(workspace_helper.HelperError) as mode_failure:
                workspace_helper._verify_repair_live_state(record, runner, inventory_path=inventory_path)
            self.assertEqual(mode_failure.exception.code, "TEMPLATE_INVALID")

    def test_template_verification_requires_canonical_path_no_symlinks_and_exact_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            canonical = root / "windows11-v1.qcow2"
            alternate = root / "alternate.qcow2"
            canonical.write_bytes(b"canonical")
            alternate.write_bytes(b"canonical")
            canonical.chmod(0o444)
            alternate.chmod(0o444)
            digest = hashlib.sha256(b"canonical").hexdigest()
            record = TemplateRecord(
                version="windows11-v1", sourceDomain="windows11", path=str(alternate), sha256=digest,
                virtualSize=9, createdAt="now",
            )
            with mock.patch.object(workspace_helper, "TEMPLATES_DIR", root):
                with self.assertRaises(workspace_helper.HelperError) as noncanonical:
                    workspace_helper.verify_template_record(record, runner=lambda arguments, timeout=None: SimpleNamespace(stdout="{}"))
                self.assertEqual(noncanonical.exception.code, "TEMPLATE_INVALID")
                alternate.unlink()
                os.symlink(canonical, alternate)
                symlink_record = dataclasses.replace(record, path=str(canonical))
                canonical.unlink()
                os.symlink(alternate, canonical)
                with self.assertRaises(workspace_helper.HelperError) as symlink:
                    workspace_helper.verify_template_record(symlink_record, runner=lambda arguments, timeout=None: SimpleNamespace(stdout="{}"))
                self.assertEqual(symlink.exception.code, "TEMPLATE_INVALID")
                canonical.unlink()
                alternate.unlink()
                canonical.write_bytes(b"canonical")
                canonical.chmod(0o2444)
                mode_record = dataclasses.replace(record, path=str(canonical))
                with self.assertRaises(workspace_helper.HelperError) as mode:
                    workspace_helper.verify_template_record(mode_record, runner=lambda arguments, timeout=None: SimpleNamespace(stdout="{}"))
                self.assertEqual(mode.exception.code, "TEMPLATE_INVALID")

    def test_template_verification_rejects_backing_before_repair_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / "windows11-v1.qcow2"
            path.write_bytes(b"canonical")
            path.chmod(0o444)
            record = TemplateRecord(
                version="windows11-v1", sourceDomain="windows11", path=str(path),
                sha256=hashlib.sha256(b"canonical").hexdigest(), virtualSize=9, createdAt="now",
            )
            def runner(arguments, timeout=None):
                del timeout
                self.assertEqual(arguments[0:2], ["qemu-img", "info"])
                return SimpleNamespace(stdout=json.dumps({"filename": str(path), "backing-filename": "/tmp/base.qcow2"}))

            with mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), self.assertRaises(workspace_helper.HelperError) as failure:
                workspace_helper.verify_template_record(record, runner=runner)
            self.assertEqual(failure.exception.code, "TEMPLATE_INVALID")

    def test_repair_rdp_not_ready_through_worker_orchestration_never_syncs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            vms_dir = root / "vms"
            nvram_dir = root / "nvram"
            tpm_dir = root / "tpm"
            template_path = root / "windows11-v1.qcow2"
            template_path.write_bytes(b"immutable-template")
            template_path.chmod(0o444)
            name = "vm-rdp"
            mac = "52:54:00:20:00:01"
            ip = "192.168.250.20"
            clone_uuid = "11111111-2222-4333-8444-555555555555"
            attempt = "66666666-7777-4888-8999-000000000000"
            marker = workspace_helper._workspace_ownership_payload(
                name=name,
                template_version="windows11-v1",
                clone_uuid=clone_uuid,
                mac=mac,
                ip=ip,
                sync_attempt_id=attempt,
            )
            overlay_path = vms_dir / f"{name}.qcow2"
            nvram_path = nvram_dir / f"{name}_VARS.fd"
            overlay_path.parent.mkdir(parents=True)
            nvram_path.parent.mkdir(parents=True)
            overlay_path.write_bytes(b"overlay")
            nvram_path.write_bytes(b"nvram")
            workspace_helper._write_workspace_marker(vms_dir / workspace_helper.WORKSPACE_OWNERSHIP_DIRNAME / f"{name}.json", marker)
            workspace_helper._write_workspace_marker(workspace_helper._workspace_nvram_marker_path(nvram_path), marker)
            (tpm_dir / clone_uuid).mkdir(parents=True)
            workspace_helper._write_workspace_marker(tpm_dir / clone_uuid / workspace_helper.WORKSPACE_MARKER_FILENAME, marker)
            save_inventory_atomic(
                {
                    "templates": [{
                        "version": "windows11-v1",
                        "sourceDomain": "windows11",
                        "path": str(template_path),
                        "sha256": hashlib.sha256(template_path.read_bytes()).hexdigest(),
                        "virtualSize": template_path.stat().st_size,
                    }],
                    "clones": [{
                        "name": name, "mac": mac, "ip": ip,
                        "assigneeType": "USER", "assigneeName": "demo",
                        "templateVersion": "windows11-v1", "syncAttemptId": attempt,
                        "status": "sync-failed", "connectionId": 14,
                    }],
                },
                inventory_path,
            )
            xml = (
                f"<domain><name>{name}</name><uuid>{clone_uuid}</uuid>"
                f"<metadata><workspace managed='true' schema='{workspace_helper.WORKSPACE_OWNERSHIP_SCHEMA}' "
                f"name='{name}' template='windows11-v1'/></metadata>"
                f"<os><nvram>{nvram_path}</nvram></os><devices>"
                f"<disk><source file='{overlay_path}'/></disk>"
                f"<interface><mac address='{mac}'/><source network='{workspace_helper.NETWORK_NAME}'/></interface>"
                "</devices></domain>"
            )
            calls = []
            rdp_ready = {"value": False}

            def runner(arguments, timeout=None):
                del timeout
                calls.append(list(arguments))
                if "dominfo" in arguments:
                    return SimpleNamespace(stdout=f"Name: {name}\nUUID: {clone_uuid}\nState: running\n")
                if "dumpxml" in arguments:
                    return SimpleNamespace(stdout=xml)
                if arguments[0] == "qemu-img":
                    if "--backing-chain" not in arguments:
                        return SimpleNamespace(stdout=json.dumps({"filename": str(template_path)}))
                    return SimpleNamespace(stdout=json.dumps({"filename": str(overlay_path), "backing": {"filename": str(template_path)}}))
                if "net-dhcp-leases" in arguments:
                    return SimpleNamespace(stdout=f"{mac} {ip}\n")
                if arguments[0] == "docker":
                    if rdp_ready["value"]:
                        return SimpleNamespace(stdout="")
                    raise subprocess.CalledProcessError(1, arguments, stderr="connection refused")
                raise AssertionError(arguments)

            arguments = SimpleNamespace(name=name)
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), mock.patch.object(
                workspace_helper, "VMS_DIR", vms_dir
            ), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", nvram_dir
            ), mock.patch.object(workspace_helper, "CLONE_TPM_DIR", tpm_dir), mock.patch.object(
                workspace_helper, "workspace_lock", lambda: nullcontext()
            ), mock.patch.object(workspace_helper, "CLONE_RDP_TIMEOUT_SECONDS", 0.01), mock.patch.object(
                workspace_helper, "POLL_INTERVAL_SECONDS", 0.0
            ), mock.patch.object(workspace_helper, "sync_guacamole") as sync:
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    workspace_helper._run_repair_job(arguments, "job-vm-rdp", runner=runner)
                sync.assert_not_called()
                self.assertEqual(failure.exception.code, workspace_helper.RDP_NOT_READY)
                self.assertTrue(any(call[0] == "docker" for call in calls))
                self.assertEqual(workspace_helper.read_job_status(name)["status"], "waiting-rdp")
                rdp_ready["value"] = True

                def tamper_after_probe(*args, **kwargs):
                    del args, kwargs
                    template_path.write_bytes(b"tampered-template")
                    template_path.chmod(0o444)
                    return None

                with mock.patch.object(workspace_helper, "_probe_guacamole_sync_state", side_effect=tamper_after_probe), mock.patch.object(
                    workspace_helper, "sync_guacamole"
                ) as sync:
                    with self.assertRaises(workspace_helper.HelperError) as tamper_failure:
                        workspace_helper._run_repair_job(arguments, "job-vm-rdp", runner=runner)
                    self.assertEqual(tamper_failure.exception.code, "TEMPLATE_HASH_INVALID")
                    sync.assert_not_called()

    def _owned_repair_fixture(self, root):
        inventory_path = root / "inventory.json"
        status_dir = root / "jobs"
        record = {
            "name": "vm-owned-repair", "mac": "52:54:00:20:00:05", "ip": "192.168.250.25",
            "assigneeType": "USER", "assigneeName": "demo", "templateVersion": "windows11-v1",
            "syncAttemptId": "11111111-2222-4333-8444-555555555555", "status": "sync-failed", "connectionId": 41,
        }
        save_inventory_atomic({"templates": [], "clones": [record]}, inventory_path)
        return inventory_path, status_dir, record, SimpleNamespace(name=record["name"])

    def test_owned_repair_inventory_prewrite_failure_never_calls_db(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path, status_dir, record, arguments = self._owned_repair_fixture(pathlib.Path(directory))
            owned = {"connectionId": 41, "connectionName": "vm-owned-repair"}
            def fail_prewrite(*args, **kwargs):
                del args, kwargs
                raise OSError("inventory prewrite fault")
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "workspace_lock", lambda: nullcontext()), mock.patch.object(
                workspace_helper, "_verify_repair_live_state", return_value={}
            ), mock.patch.object(workspace_helper, "_probe_guacamole_sync_state", return_value=owned), mock.patch.object(
                workspace_helper, "save_inventory_atomic", side_effect=fail_prewrite
            ), mock.patch.object(workspace_helper, "sync_guacamole") as sync:
                with self.assertRaises(workspace_helper.HelperError) as raised:
                    workspace_helper._run_repair_job(arguments, "job-vm-owned-repair")
                status = workspace_helper.read_job_status(record["name"])
            sync.assert_not_called()
            self.assertEqual(raised.exception.code, "COMMAND_FAILED")
            self.assertEqual(load_inventory(inventory_path)["clones"][0], record)
            self.assertEqual(status["status"], "sync-failed")

    def test_owned_repair_db_rollback_restores_previous_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path, status_dir, record, arguments = self._owned_repair_fixture(pathlib.Path(directory))
            owned = {"connectionId": 41, "connectionName": "vm-owned-repair"}
            calls = []
            real_save = workspace_helper.save_inventory_atomic
            def capture_save(value, *args, **kwargs):
                calls.append(value)
                return real_save(value, *args, **kwargs)
            failure = workspace_helper.HelperError("db rollback", code="COMMAND_FAILED", stage="sync")
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "workspace_lock", lambda: nullcontext()), mock.patch.object(
                workspace_helper, "_verify_repair_live_state", return_value={}
            ), mock.patch.object(workspace_helper, "_probe_guacamole_sync_state", side_effect=[owned, None]), mock.patch.object(
                workspace_helper, "sync_guacamole", side_effect=failure
            ), mock.patch.object(workspace_helper, "save_inventory_atomic", side_effect=capture_save):
                with self.assertRaises(workspace_helper.HelperError) as raised:
                    workspace_helper._run_repair_job(arguments, "job-vm-owned-repair")
            self.assertEqual(raised.exception.code, "COMMAND_FAILED")
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["clones"][0]["status"], "ready")
            self.assertEqual(calls[1]["clones"][0], record)
            self.assertEqual(load_inventory(inventory_path)["clones"][0], record)

    def test_owned_repair_runner_error_restores_previous_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path, status_dir, record, arguments = self._owned_repair_fixture(pathlib.Path(directory))
            owned = {"connectionId": 41, "connectionName": "vm-owned-repair"}
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "workspace_lock", lambda: nullcontext()), mock.patch.object(
                workspace_helper, "_verify_repair_live_state", return_value={}
            ), mock.patch.object(workspace_helper, "_probe_guacamole_sync_state", side_effect=[owned, None]), mock.patch.object(
                workspace_helper, "sync_guacamole", side_effect=RuntimeError("runner closed")
            ):
                with self.assertRaises(workspace_helper.HelperError) as raised:
                    workspace_helper._run_repair_job(arguments, "job-vm-owned-repair")
            self.assertEqual(raised.exception.code, "COMMAND_FAILED")
            self.assertEqual(load_inventory(inventory_path)["clones"][0], record)

    def test_owned_repair_commit_has_one_prewrite_and_no_postcommit_inventory_save(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path, status_dir, record, arguments = self._owned_repair_fixture(pathlib.Path(directory))
            owned = {"connectionId": 41, "connectionName": "vm-owned-repair"}
            calls = []
            events = []
            real_save = workspace_helper.save_inventory_atomic
            real_write = workspace_helper.write_job_status
            def capture_save(value, *args, **kwargs):
                calls.append(value)
                events.append("inventory-prewrite")
                return real_save(value, *args, **kwargs)
            def capture_sync(*args, **kwargs):
                del args, kwargs
                events.append("sync-transaction")
                return {"ok": True, "clone": {"status": "ready", "connectionId": 41}}
            def capture_status(name, updates):
                transition = updates.get("inventoryTransition")
                if isinstance(transition, dict) and transition.get("phase") == "committed":
                    events.append("ledger-committed")
                return real_write(name, updates)
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "workspace_lock", lambda: nullcontext()), mock.patch.object(
                workspace_helper, "_verify_repair_live_state", return_value={}
            ), mock.patch.object(workspace_helper, "_probe_guacamole_sync_state", return_value=owned), mock.patch.object(
                workspace_helper, "sync_guacamole", side_effect=capture_sync
            ), mock.patch.object(workspace_helper, "save_inventory_atomic", side_effect=capture_save), mock.patch.object(
                workspace_helper, "write_job_status", side_effect=capture_status
            ):
                result = workspace_helper._run_repair_job(arguments, "job-vm-owned-repair")
                status = workspace_helper.read_job_status(record["name"])
            self.assertEqual(result["status"], "ready")
            self.assertEqual(len(calls), 1)
            self.assertEqual(events, ["inventory-prewrite", "sync-transaction", "ledger-committed"])
            self.assertEqual(load_inventory(inventory_path)["clones"][0]["status"], "ready")
            self.assertEqual(status["inventoryTransition"]["phase"], "committed")

    def test_owned_repair_prepared_transition_recovers_after_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path, status_dir, record, arguments = self._owned_repair_fixture(root)
            desired = dict(record, status="ready")
            save_inventory_atomic({"templates": [], "clones": [desired]}, inventory_path)
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "workspace_lock", lambda: nullcontext()), mock.patch.object(
                workspace_helper, "_probe_guacamole_sync_state", return_value=None
            ):
                workspace_helper.write_job_status(
                    record["name"],
                    {
                        "status": "running", "stage": "sync", "result": record,
                        "inventoryTransition": {"phase": "prepared", "previous": record, "desired": desired},
                    },
                )
                with self.assertRaises(workspace_helper.HelperError) as raised:
                    workspace_helper._run_repair_job(arguments, "job-vm-owned-repair")
            self.assertEqual(raised.exception.code, "SYNC_COMMIT_UNKNOWN")
            self.assertEqual(load_inventory(inventory_path)["clones"][0], record)

    def test_repair_inventory_loss_after_sync_compensates_and_never_marks_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            record = {
                "name": "vm-post-sync-loss", "mac": "52:54:00:20:00:05", "ip": "192.168.250.25",
                "assigneeType": "USER", "assigneeName": "demo", "templateVersion": "windows11-v1",
                "syncAttemptId": "11111111-2222-4333-8444-555555555555", "status": "sync-failed",
            }
            save_inventory_atomic({"templates": [], "clones": [record]}, inventory_path)
            arguments = SimpleNamespace(name="vm-post-sync-loss")
            rollback_calls = []

            def fake_sync(*args, compensation_holder=None, **kwargs):
                del args, kwargs
                compensation_holder["rollback"] = lambda: rollback_calls.append("compensated") or []
                return {"ok": True, "clone": {"name": "vm-post-sync-loss", "status": "ready", "connectionId": 77}}

            inventory_reads = iter([
                {"templates": [], "clones": [record]},
                {"templates": [], "clones": []},
                {"templates": [], "clones": []},
            ])
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "workspace_lock", lambda: nullcontext()), mock.patch.object(
                workspace_helper, "load_inventory", side_effect=lambda *args, **kwargs: next(inventory_reads)
            ), mock.patch.object(workspace_helper, "_verify_repair_live_state", return_value={}), mock.patch.object(
                workspace_helper, "_probe_guacamole_sync_state", return_value=None
            ), mock.patch.object(workspace_helper, "sync_guacamole", side_effect=fake_sync), mock.patch.object(
                workspace_helper, "_prove_no_clone_artifacts", return_value=False
            ):
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    workspace_helper._run_repair_job(arguments, "job-vm-post-sync-loss")
                status = workspace_helper.read_job_status("vm-post-sync-loss")
            self.assertEqual(failure.exception.code, "INVENTORY_INVALID")
            self.assertEqual(rollback_calls, ["compensated"])
            self.assertEqual(status["status"], "failed-without-inventory")
            self.assertNotEqual(status["status"], "ready")

    def test_worker_state_survives_f5_and_fresh_list_rehydrates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            record = {
                "name": "vm-f5", "mac": "52:54:00:20:00:02", "ip": "192.168.250.22",
                "assigneeType": "USER", "assigneeName": "demo", "templateVersion": "windows11-v1",
                "syncAttemptId": "11111111-2222-4333-8444-555555555555", "status": "ready",
            }
            save_inventory_atomic({"templates": [], "clones": [record]}, inventory_path)
            arguments = SimpleNamespace(
                name="vm-f5", assign_user="demo", assign_group=None, template="windows11-v1",
                memory_mib=4096, vcpus=2, wait_rdp_minutes=20,
            )
            worker_payload = {"ok": True, "status": "ready", "stage": "permissions", "clone": record, "progress": workspace_helper.clone_progress("ready")}
            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ), mock.patch.object(workspace_helper, "workspace_lock", lambda: nullcontext()), mock.patch.object(
                workspace_helper, "clone_workspace_and_sync", return_value=worker_payload
            ):
                workspace_helper._run_clone_job(arguments, "guacamole-workspace-vm-f5.service")
                self.assertEqual(workspace_helper.read_job_status("vm-f5")["status"], "ready")
                fresh = workspace_helper.build_list_payload(
                    inventory_path=inventory_path,
                    runner=lambda arguments: SimpleNamespace(stdout="vm-f5\n"),
                    assignee_runner=lambda arguments, timeout=None: SimpleNamespace(stdout="USER\tdemo\n"),
                )
            workspaces = {item["name"]: item for item in fresh["workspaces"]}
            self.assertEqual(workspaces["vm-f5"]["status"], "ready")
            self.assertEqual(workspaces["vm-f5"]["ip"], "192.168.250.22")

    def test_generic_clone_worker_failure_without_inventory_is_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            status_dir = root / "jobs"
            arguments = SimpleNamespace(
                name="vm-unexpected", assign_user="demo", assign_group=None, template="windows11-v1",
                memory_mib=4096, vcpus=2, wait_rdp_minutes=20,
            )
            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir), mock.patch.object(
                workspace_helper, "workspace_lock", lambda: nullcontext()
            ), mock.patch.object(workspace_helper, "load_inventory", return_value={"templates": [], "clones": []}), mock.patch.object(
                workspace_helper, "clone_workspace_and_sync", side_effect=RuntimeError("secret worker failure")
            ), mock.patch.object(workspace_helper, "_prove_no_clone_artifacts", return_value=False):
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    workspace_helper._run_clone_job(arguments, "job-vm-unexpected")
                status = workspace_helper.read_job_status("vm-unexpected")
            self.assertEqual(failure.exception.code, "COMMAND_FAILED")
            self.assertEqual(status["status"], "failed-without-inventory")
            self.assertEqual(status["errorCode"], "ARTIFACTS_UNVERIFIED")

    def test_concurrent_start_and_repair_are_idempotent_and_no_duplicate_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            save_inventory_atomic({"templates": [{"version": "windows11-v1", "sourceDomain": "windows11"}], "clones": []}, inventory_path)
            run_calls = []

            def runner(arguments, timeout=None):
                del timeout
                run_calls.append(list(arguments))
                if arguments[0] == "systemd-run":
                    return SimpleNamespace(stdout="Running as unit: guacamole-workspace-vm-race.service\n")
                if arguments[0] == "systemctl":
                    return SimpleNamespace(stdout="ActiveState=active\nSubState=running\nResult=success\n")
                raise AssertionError(arguments)

            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "run_command", runner), mock.patch.object(
                workspace_helper, "_systemd_unit_state", side_effect=lambda unit, runner=None: {"ActiveState": "active", "SubState": "running", "Result": "success"}
            ):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(
                        lambda _: workspace_helper.start_workspace_job("vm-race", "USER", "demo", "windows11-v1"),
                        (1, 2),
                    ))
                self.assertIn(workspace_helper.read_job_status("vm-race")["status"], {"queued", "running"})
            self.assertLessEqual(sum(call[0] == "systemd-run" for call in run_calls), 1)
            self.assertEqual(results[0]["jobId"], results[1]["jobId"])

            record = {
                "name": "vm-repair-race", "mac": "52:54:00:20:00:03", "ip": "192.168.250.23",
                "assigneeType": "USER", "assigneeName": "demo", "templateVersion": "windows11-v1", "status": "pending",
            }
            save_inventory_atomic({"templates": [{"version": "windows11-v1", "sourceDomain": "windows11"}], "clones": [record]}, inventory_path)
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "_systemd_unit_state", return_value={"ActiveState": "active", "SubState": "running", "Result": "success"}):
                workspace_helper.write_job_status("vm-repair-race", {"status": "queued", "operation": "clone", "templateVersion": "windows11-v1", "assigneeType": "USER", "assigneeName": "demo"})
                with ThreadPoolExecutor(max_workers=2) as pool:
                    pair = list(pool.map(
                        lambda function: function("vm-repair-race"),
                        (
                            lambda name: workspace_helper.start_workspace_job(name, "USER", "demo", "windows11-v1"),
                            workspace_helper.repair_workspace_job,
                        ),
                    ))
            self.assertEqual(pair[0]["jobId"], pair[1]["jobId"])
            self.assertEqual(pair[0]["status"], "queued")

    def test_cli_start_and_repair_share_real_lock_without_duplicate_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            lock_path = root / "workspace.lock"
            save_inventory_atomic({"templates": [{"version": "windows11-v1", "sourceDomain": "windows11"}], "clones": [{
                "name": "vm-cli-race", "mac": "52:54:00:20:00:06", "ip": "192.168.250.26",
                "assigneeType": "USER", "assigneeName": "demo", "templateVersion": "windows11-v1", "status": "pending",
            }]}, inventory_path)
            run_calls = []

            def runner(arguments, timeout=None):
                del timeout
                run_calls.append(list(arguments))
                return SimpleNamespace(stdout="Running as unit: guacamole-workspace-vm-cli-race.service\n")

            real_lock = workspace_helper.workspace_lock
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "workspace_lock", lambda: real_lock(lock_path)), mock.patch.object(
                workspace_helper, "run_command", runner
            ), mock.patch.object(
                workspace_helper, "_systemd_unit_state", return_value={"ActiveState": "active", "SubState": "running", "Result": "success"}
            ):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(
                        lambda argv: workspace_helper.main(argv),
                        (
                            ["start", "--name", "vm-cli-race", "--assign-user", "demo", "--template", "windows11-v1", "--json"],
                            ["repair", "--name", "vm-cli-race", "--json"],
                        ),
                    ))
            self.assertTrue(all(result in {0, 2} for result in results))
            self.assertIn(0, results)
            self.assertLessEqual(sum(call[0] == "systemd-run" for call in run_calls), 1)

    def test_repair_worker_rejects_foreign_same_name_row_through_sync_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            record = {
                "name": "vm-foreign-worker", "mac": "52:54:00:20:00:07", "ip": "192.168.250.27",
                "assigneeType": "USER", "assigneeName": "demo", "templateVersion": "windows11-v1",
                "syncAttemptId": "11111111-2222-4333-8444-555555555555", "status": "sync-failed",
            }
            save_inventory_atomic({"templates": [], "clones": [record]}, inventory_path)
            calls = []

            def fake_psql(arguments, sql, timeout=None):
                del arguments, timeout
                calls.append(sql)
                return SimpleNamespace(stdout=json.dumps({
                    "connectionId": 55, "connectionName": "Windows 11", "created": False,
                    "permissions": {"assignee": [], "guacadmin": []},
                }) + "\n")

            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "workspace_lock", lambda: nullcontext()), mock.patch.object(
                workspace_helper, "_verify_repair_live_state", return_value={}
            ), mock.patch.object(
                workspace_helper,
                "read_windows_credential_secret",
                return_value=workspace_helper.WindowsCredential(username="guacadmin", password="unit-test-secret"),
            ):
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    workspace_helper._run_repair_job(
                        SimpleNamespace(name="vm-foreign-worker"),
                        "job-vm-foreign-worker",
                        sync_runner=fake_psql,
                    )
                status = workspace_helper.read_job_status("vm-foreign-worker")
            self.assertEqual(failure.exception.code, "SYNC_COMMIT_UNKNOWN")
            self.assertEqual(status["status"], "sync-failed")
            self.assertGreaterEqual(len(calls), 2)
            self.assertTrue(any("sync_attempt_id" in sql for sql in calls))
            self.assertTrue(any("connection_name = :'sync_name'" in sql for sql in calls))

    def test_owned_sync_uses_bound_connection_identity_after_commit(self):
        record = CloneRecord(
            name="vm-foreign", mac="52:54:00:20:00:04", ip="192.168.250.24",
            assigneeType="USER", assigneeName="demo", templateVersion="windows11-v1",
            syncAttemptId="11111111-2222-4333-8444-555555555555",
        )
        calls = []

        def runner(arguments, sql, timeout=None):
            del timeout
            calls.append((arguments, sql))
            return SimpleNamespace(stdout=json.dumps({"connectionId": 99, "connectionName": "Windows 11", "created": True}) + "\n")

        result = workspace_helper.sync_guacamole(
            record,
            runner=runner,
            require_owned_connection=True,
            connection_id=14,
        )
        self.assertEqual(result["clone"]["status"], "ready")
        self.assertEqual(result["clone"]["connectionId"], 14)
        self.assertEqual(len(calls), 1)
        self.assertIn("connection.connection_id = :'sync_connection_id'::integer", calls[0][1])
        self.assertIn("attempt.attribute_value = :'sync_attempt_id'", calls[0][1])

    def test_inventory_is_authoritative_and_stale_ledger_cannot_create_phantom_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            save_inventory_atomic({"templates": [], "clones": [{
                "name": "vm-real", "mac": "52:54:00:20:00:01", "ip": "192.168.250.20",
                "assigneeType": "USER", "assigneeName": "demo", "templateVersion": "windows11-v1", "status": "ready",
            }]}, inventory_path)
            with mock.patch.object(workspace_helper, "JOB_STATUS_DIR", status_dir):
                workspace_helper.write_job_status("vm-real", {"status": "ready", "templateVersion": "other-template", "result": {"ip": "192.168.250.99"}})
                workspace_helper.write_job_status("vm-phantom", {"status": "ready", "result": {"ip": "192.168.250.98"}})
                workspace_helper.write_job_status("vm-queued", {
                    "status": "queued", "templateVersion": "windows11-v1", "assigneeType": "USER", "assigneeName": "demo",
                })
                payload = workspace_helper.build_list_payload(
                    inventory_path=inventory_path,
                    runner=lambda arguments: SimpleNamespace(
                        stdout=(
                            "LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n"
                            if arguments[0] == "systemctl" else "vm-real\n"
                        )
                    ),
                    assignee_runner=lambda arguments, timeout=None: SimpleNamespace(stdout="USER\tdemo\n"),
                )
            workspaces = {item["name"]: item for item in payload["workspaces"]}
            self.assertEqual(set(workspaces), {"vm-real", "vm-queued"})
            self.assertEqual(workspaces["vm-real"]["ip"], "192.168.250.20")
            self.assertEqual(workspaces["vm-queued"]["status"], "queued")

    def test_failed_without_inventory_is_non_repairable_when_artifacts_are_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            save_inventory_atomic({"templates": [], "clones": []}, inventory_path)
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "_prove_no_clone_artifacts", return_value=False):
                workspace_helper.write_job_status("vm-failed", {
                    "status": "failed", "templateVersion": "windows11-v1", "assigneeType": "USER", "assigneeName": "demo",
                })
                result = workspace_helper.start_workspace_job("vm-failed", "USER", "demo", "windows11-v1")
            self.assertEqual(result["status"], "failed-without-inventory")
            self.assertFalse(result.get("repairAvailable", False))

    def test_start_repairs_unit_conflict_idempotently_without_second_systemd_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory_path = root / "inventory.json"
            status_dir = root / "jobs"
            save_inventory_atomic({"templates": [], "clones": []}, inventory_path)
            with mock.patch.object(workspace_helper, "INVENTORY_PATH", inventory_path), mock.patch.object(
                workspace_helper, "JOB_STATUS_DIR", status_dir
            ), mock.patch.object(workspace_helper, "_prove_no_clone_artifacts", return_value=True), mock.patch.object(
                workspace_helper, "_systemd_unit_state", return_value={"ActiveState": "active", "SubState": "running"}
            ), mock.patch.object(workspace_helper, "run_command") as run:
                workspace_helper.write_job_status("vm-race", {
                    "status": "failed", "templateVersion": "windows11-v1", "assigneeType": "USER", "assigneeName": "demo",
                })
                result = workspace_helper.start_workspace_job("vm-race", "USER", "demo", "windows11-v1")
            self.assertEqual(result["status"], "running")
            run.assert_not_called()

    def test_assignee_discovery_returns_users_and_groups_from_read_only_runner(self):
        calls = []

        def runner(arguments, timeout=None):
            calls.append((arguments, timeout))
            return SimpleNamespace(stdout="USER\talice\nUSER_GROUP\tdevelopers\nUSER\tguacadmin\nINVALID\tignored\n")

        result = workspace_helper.discover_guacamole_assignees(runner)
        self.assertEqual([item["name"] for item in result["users"]], ["alice"])
        self.assertEqual([item["name"] for item in result["groups"]], ["developers"])
        self.assertEqual(len(calls), 1)
        self.assertIn("-c", calls[0][0])
        query = calls[0][0][calls[0][0].index("-c") + 1]
        self.assertIn("FROM guacamole_entity", query)
        self.assertNotRegex(query, r"\b(INSERT|UPDATE|DELETE|ALTER|DROP)\b")

    def test_list_payload_contains_authoritative_assignees_and_templates(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic(
                {
                    "templates": [{"version": "windows11-v1", "sourceDomain": "windows11"}],
                    "clones": [],
                },
                inventory_path,
            )

            def domain_runner(arguments):
                return SimpleNamespace(stdout="windows11\n")

            def assignee_runner(arguments, timeout=None):
                return SimpleNamespace(stdout="USER\talice\nUSER_GROUP\tdevelopers\n")

            payload = workspace_helper.build_list_payload(
                inventory_path=inventory_path,
                runner=domain_runner,
                assignee_runner=assignee_runner,
            )
        self.assertEqual(payload["templates"][0]["version"], "windows11-v1")
        self.assertEqual(payload["guacamoleAssignees"]["users"][0]["type"], "USER")
        self.assertEqual(payload["guacamoleAssignees"]["groups"][0]["type"], "USER_GROUP")

    def test_clone_orchestration_syncs_ready_workspace_and_reports_progress(self):
        record = CloneRecord(
            name="vm-new",
            mac="52:54:00:20:00:01",
            ip="192.168.250.21",
            assigneeType="USER_GROUP",
            assigneeName="developers",
            status="pending",
        )
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic({"templates": [], "clones": [record]}, inventory_path)
            with mock.patch.object(workspace_helper, "clone_workspace", return_value=record) as clone, mock.patch.object(
                workspace_helper,
                "sync_guacamole",
                return_value={"ok": True, "clone": {"name": "vm-new", "status": "ready", "connectionId": 17}},
            ) as sync:
                result = workspace_helper.clone_workspace_and_sync(
                    "vm-new", "USER_GROUP", "developers", "windows11-v1", runner=mock.Mock(), inventory_path=inventory_path
                )
        clone.assert_called_once()
        sync.assert_called_once()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["clone"]["ip"], "192.168.250.21")
        self.assertEqual(result["clone"]["assignee"]["type"], "USER_GROUP")
        self.assertEqual(len(result["progress"]), 7)
        self.assertTrue(all(item["status"] == "ready" for item in result["progress"]))

    def test_waiting_rdp_keeps_clone_without_guacamole_sync(self):
        record = CloneRecord(
            name="vm-new",
            mac="52:54:00:20:00:01",
            ip="192.168.250.21",
            assigneeType="USER",
            assigneeName="alice",
            status="waiting-rdp",
        )
        with mock.patch.object(workspace_helper, "clone_workspace", return_value=record), mock.patch.object(
            workspace_helper, "sync_guacamole"
        ) as sync:
            result = workspace_helper.clone_workspace_and_sync(
                "vm-new", "USER", "alice", "windows11-v1", runner=mock.Mock()
            )
        sync.assert_not_called()
        self.assertEqual(result["status"], "waiting-rdp")
        self.assertEqual(result["progress"][4]["status"], "waiting")

    def test_post_clone_sync_failure_is_persisted_for_repair(self):
        record = CloneRecord(
            name="vm-new",
            mac="52:54:00:20:00:01",
            ip="192.168.250.21",
            assigneeType="USER",
            assigneeName="alice",
            status="pending",
            templateVersion="windows11-v1",
        )
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic({"templates": [], "clones": [record]}, inventory_path)
            failure = workspace_helper.HelperError("database unavailable", code="COMMAND_FAILED", stage="sync")
            def fake_clone(*args, **kwargs):
                kwargs["_rollback_holder"]["rollback"] = lambda: []
                return record

            with mock.patch.object(workspace_helper, "clone_workspace", side_effect=fake_clone), mock.patch.object(
                workspace_helper, "sync_guacamole", side_effect=failure
            ):
                with self.assertRaises(workspace_helper.HelperError) as context:
                    workspace_helper.clone_workspace_and_sync(
                        "vm-new", "USER", "alice", "windows11-v1", runner=mock.Mock(), inventory_path=inventory_path
                    )
            self.assertIn("newly-created clone was rolled back", str(context.exception))
            persisted = load_inventory(inventory_path)
            self.assertEqual(persisted["clones"][0]["status"], "pending")

    def test_final_inventory_failure_is_not_reported_as_ready(self):
        record = CloneRecord(
            name="vm-new",
            mac="52:54:00:20:00:01",
            ip="192.168.250.21",
            assigneeType="USER",
            assigneeName="alice",
            status="pending",
            templateVersion="windows11-v1",
        )
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic({"templates": [], "clones": [record]}, inventory_path)
            original_save = workspace_helper.save_inventory_atomic
            calls = {"count": 0}

            def fail_ready_then_record_failure(inventory, target):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise workspace_helper.InventoryError("read-only inventory", replaced=False)
                return original_save(inventory, target)

            def fake_clone(*args, **kwargs):
                kwargs["_rollback_holder"]["rollback"] = lambda: []
                return record

            with mock.patch.object(workspace_helper, "clone_workspace", side_effect=fake_clone), mock.patch.object(
                workspace_helper,
                "sync_guacamole",
                return_value={"ok": True, "clone": {"name": "vm-new", "status": "ready", "connectionId": 17}},
            ), mock.patch.object(workspace_helper, "save_inventory_atomic", side_effect=fail_ready_then_record_failure):
                with self.assertRaises(workspace_helper.HelperError) as context:
                    workspace_helper.clone_workspace_and_sync(
                        "vm-new", "USER", "alice", "windows11-v1", runner=mock.Mock(), inventory_path=inventory_path
                    )
            self.assertEqual(context.exception.code, "INVENTORY_INVALID")
            persisted = load_inventory(inventory_path)
            self.assertEqual(persisted["clones"][0]["status"], "pending")

    def test_failed_compensation_retains_repair_metadata(self):
        record = CloneRecord(
            name="vm-new", mac="52:54:00:20:00:01", ip="192.168.250.21",
            assigneeType="USER", assigneeName="alice", status="pending",
            templateVersion="windows11-v1",
        )
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic({"templates": [], "clones": [record]}, inventory_path)

            def fake_clone(*args, **kwargs):
                kwargs["_rollback_holder"]["rollback"] = lambda: [
                    workspace_helper.HelperError("destroy failed", code="COMMAND_FAILED", stage="rollback-start")
                ]
                return record

            with mock.patch.object(workspace_helper, "clone_workspace", side_effect=fake_clone), mock.patch.object(
                workspace_helper,
                "sync_guacamole",
                side_effect=workspace_helper.HelperError("database unavailable", code="COMMAND_FAILED", stage="sync"),
            ):
                with self.assertRaises(workspace_helper.HelperError) as context:
                    workspace_helper.clone_workspace_and_sync(
                        "vm-new", "USER", "alice", "windows11-v1", runner=mock.Mock(), inventory_path=inventory_path
                    )
            self.assertEqual(context.exception.code, "ROLLBACK_FAILED")
            persisted = load_inventory(inventory_path)
            self.assertEqual(persisted["clones"][0]["status"], "sync-failed")
            self.assertEqual(persisted["clones"][0]["errorCode"], "ROLLBACK_FAILED")

    def test_ready_retry_failure_preserves_ready_inventory(self):
        record = CloneRecord(
            name="vm-new", mac="52:54:00:20:00:01", ip="192.168.250.21",
            assigneeType="USER", assigneeName="alice", status="ready",
            templateVersion="windows11-v1",
        )
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic({"templates": [], "clones": [record]}, inventory_path)
            with mock.patch.object(workspace_helper, "clone_workspace", return_value=record), mock.patch.object(
                workspace_helper,
                "sync_guacamole",
                side_effect=workspace_helper.HelperError("database unavailable", code="COMMAND_FAILED", stage="sync"),
            ):
                with self.assertRaises(workspace_helper.HelperError) as context:
                    workspace_helper.clone_workspace_and_sync(
                        "vm-new", "USER", "alice", "windows11-v1", runner=mock.Mock(), inventory_path=inventory_path
                    )
            self.assertIn("existing ready clone was preserved", str(context.exception))
            persisted = load_inventory(inventory_path)
            self.assertEqual(persisted["clones"][0]["status"], "ready")


class CloneIdempotenceTests(unittest.TestCase):
    def test_completed_matching_clone_is_idempotent(self):
        class ReadOnlyRunner:
            def __call__(self, arguments, *, timeout=None):
                if "dominfo" in arguments:
                    return SimpleNamespace(stdout="Name: vm-new\nPersistent: yes\n")
                if "dumpxml" in arguments:
                    return SimpleNamespace(stdout=(
                        "<domain><name>vm-new</name><devices>"
                        "<interface><mac address='52:54:00:20:00:01'/>"
                        "<source network='guac-nat'/></interface></devices></domain>"
                    ))
                if "net-dhcp-leases" in arguments:
                    return SimpleNamespace(stdout="52:54:00:20:00:01 192.168.250.21\n")
                if arguments[0] == "qemu-img" and "info" in arguments:
                    return SimpleNamespace(stdout=json.dumps({"filename": str(image)}))
                raise AssertionError(arguments)

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            image = root / "windows11-v1.qcow2"
            image.write_bytes(b"template")
            image.chmod(0o444)
            record = {
                "name": "vm-new",
                "mac": "52:54:00:20:00:01",
                "ip": "192.168.250.21",
                "assigneeType": "USER",
                "assigneeName": "alice",
                "status": "ready",
                "templateVersion": "windows11-v1",
            }
            inventory_path = root / "inventory.json"
            save_inventory_atomic({
                "templates": [{
                    "version": "windows11-v1", "sourceDomain": "windows11", "path": str(image),
                    "sha256": hashlib.sha256(b"template").hexdigest(), "virtualSize": 8,
                }],
                "clones": [record],
            }, inventory_path)
            with mock.patch.object(workspace_helper, "TEMPLATES_DIR", root):
                returned = workspace_helper.clone_workspace(
                    "vm-new", "USER", "alice", "windows11-v1", runner=ReadOnlyRunner(), inventory_path=inventory_path
                )
            self.assertEqual(returned.to_dict(), record)

    def test_same_name_incomplete_clone_remains_a_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            image = root / "windows11-v1.qcow2"
            image.write_bytes(b"template")
            image.chmod(0o444)
            inventory_path = root / "inventory.json"
            save_inventory_atomic({
                "templates": [{
                    "version": "windows11-v1", "sourceDomain": "windows11", "path": str(image),
                    "sha256": hashlib.sha256(b"template").hexdigest(), "virtualSize": 8,
                }],
                "clones": [{
                    "name": "vm-new", "mac": "52:54:00:20:00:01", "ip": "192.168.250.21",
                    "assigneeType": "USER", "assigneeName": "alice", "status": "pending",
                    "templateVersion": "windows11-v1",
                }],
            }, inventory_path)
            with mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), self.assertRaises(ConflictError) as context:
                workspace_helper.clone_workspace(
                    "vm-new", "USER", "alice", "windows11-v1",
                    runner=lambda arguments: SimpleNamespace(stdout=json.dumps({"filename": str(image)})),
                    inventory_path=inventory_path,
                )
            self.assertEqual(context.exception.code, "CLONE_CONFLICT")

    def test_mutating_commands_require_root_without_touching_state(self):
        with mock.patch.object(workspace_helper.os, "geteuid", return_value=1000), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output:
            for arguments in (
                ["create-template", "--source", "windows11", "--version", "windows11-v2"],
                [
                    "clone",
                    "--name",
                    "vm-new",
                    "--assign-user",
                    "alice",
                    "--template",
                    "windows11-v1",
                ],
                ["sync", "--all"],
            ):
                self.assertEqual(workspace_helper.main(arguments), 2)
                payload = json.loads(output.getvalue().splitlines()[-1])
                self.assertEqual(payload["code"], "PRIVILEGE_REQUIRED")

    def test_progress_emitter_returns_structured_stage_updates(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            emit = workspace_helper._make_progress_emitter()
            emit("validation", "ready")
            emit("disk-overlay", "ready")
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([message["type"] for message in messages], ["progress", "progress"])
        self.assertEqual(messages[-1]["stage"], "disk-overlay")
        self.assertEqual(messages[-1]["progress"][0]["status"], "ready")
        self.assertEqual(messages[-1]["progress"][1]["status"], "ready")


class FakeCommandRunner:
    """A deterministic runner that models the template lifecycle commands."""

    def __init__(self, root: pathlib.Path, *, fail_stage: str | None = None):
        self.root = root
        self.fail_stage = fail_stage
        self.source_path = root / "windows11.qcow2"
        self.nvram_path = root / "OVMF_VARS_4M.ms.fd"
        self.nvram_path.write_bytes(b"nvram")
        self.fail_stages: set[str] = set()
        self.events: list[str] = []
        self.domstate_calls = 0
        self.recovery_mode = False
        self.recovery_states: list[str] = ["shut off"]
        self.socket_available = True
        self.temp_path: pathlib.Path | None = None
        self.final_path: pathlib.Path | None = None
        self.conversion_owner: tuple[int, int] | None = None
        self.converted_owner: tuple[int, int] | None = None
        self.post_publish_drift: str | None = None

    def should_fail(self, stage: str) -> bool:
        return self.fail_stage == stage or stage in self.fail_stages

    def __call__(self, arguments):
        command = arguments[0]
        if command == "virsh" and "dominfo" in arguments:
            self.events.append("validate-source")
            return SimpleNamespace(stdout=getattr(
                self,
                "dominfo_output",
                "Name: windows11\nState: running\nPersistent: yes\n",
            ))
        if command == "virsh" and "dumpxml" in arguments:
            default_xml = f"""
<domain>
  <name>windows11</name>
  <os><nvram>{self.nvram_path}</nvram></os>
  <devices>
    <disk device='disk'><driver type='qcow2'/><source file='{self.source_path}'/></disk>
    <interface type='network'><source network='guac-nat'/><model type='e1000e'/></interface>
    <tpm><backend type='external'><source type='unix' path='/run/guacamole-vm-windows11/swtpm.sock'/></backend></tpm>
  </devices>
</domain>
"""
            return SimpleNamespace(stdout=getattr(self, "xml_output", default_xml))
        if command == "virsh" and "net-info" in arguments:
            return SimpleNamespace(stdout=getattr(
                self,
                "net_info_output",
                "Name: guac-nat\nActive: yes\nPersistent: yes\n",
            ))
        if command == "virsh" and "domblkinfo" in arguments:
            return SimpleNamespace(stdout="Capacity: 1073741824\nAllocation: 1048576\nPhysical: 1048576\n")
        if command == "virsh" and "shutdown" in arguments:
            self.events.append("shutdown-source")
            return SimpleNamespace(stdout="Domain windows11 is being shutdown\n")
        if command == "virsh" and "domstate" in arguments:
            self.domstate_calls += 1
            if not self.recovery_mode and self.domstate_calls == 1:
                self.events.append("wait-shutoff")
                return SimpleNamespace(stdout="shut off\n")
            recovery_index = self.domstate_calls - 1 if self.recovery_mode else self.domstate_calls - 2
            recovery_index = max(recovery_index, 0)
            recovery_index = min(recovery_index, len(self.recovery_states) - 1)
            return SimpleNamespace(stdout=self.recovery_states[recovery_index] + "\n")
        if command == "systemctl" and "show" in arguments:
            return SimpleNamespace(stdout=getattr(self, "tpm_state_output", "active\n"))
        if command == "systemctl" and "cat" in arguments:
            return SimpleNamespace(stdout=getattr(
                self,
                "tpm_unit_output",
                f"ExecStart=/usr/bin/swtpm socket --tpm2 --tpmstate dir={workspace_helper.SOURCE_TPM_STATE_PATH} --ctrl type=unixio,path={workspace_helper.SOURCE_TPM_SOCKET}\n",
            ))
        if command == "test" and "-S" in arguments:
            if not self.socket_available:
                raise RuntimeError("TPM socket missing")
            return SimpleNamespace(stdout="")
        if command == "systemctl" and "is-active" in arguments:
            self.events.append("check-unlocked")
            return SimpleNamespace(stdout="inactive\n")
        if command == "systemctl" and ("start" in arguments or "stop" in arguments):
            return SimpleNamespace(stdout="")
        if command == "qemu-img" and "check" in arguments:
            if str(self.root / "windows11.qcow2") in arguments:
                self.events.append("qemu-img-check-source")
            else:
                self.events.append("check-temp")
            return SimpleNamespace(stdout="No errors were found on the image.\n")
        if command == "qemu-img" and "convert" in arguments:
            self.events.append("convert-temp")
            self.temp_path = pathlib.Path(arguments[-1])
            self.temp_path.write_bytes(b"template")
            if self.conversion_owner is not None:
                os.chown(self.temp_path, *self.conversion_owner)
                metadata = self.temp_path.stat()
                self.converted_owner = (metadata.st_uid, metadata.st_gid)
            if self.should_fail("convert-temp"):
                raise RuntimeError("convert failed")
            return SimpleNamespace(stdout="")
        if command == "sha256sum":
            self.events.append("hash-temp")
            digest = hashlib.sha256(self.temp_path.read_bytes()).hexdigest()
            return SimpleNamespace(stdout=(digest + "  "
                                           f"{arguments[-1]}\n"))
        if command == "qemu-img" and "info" in arguments:
            return SimpleNamespace(stdout=json.dumps({"virtual-size": 1073741824}))
        if command == "chmod":
            self.events.append("chmod-readonly")
            os.chmod(arguments[-1], 0o444)
            return SimpleNamespace(stdout="")
        if command == "chown":
            self.events.append("chown-root")
            if self.should_fail("chown-root"):
                raise RuntimeError("chown failed")
            os.chown(arguments[-1], 0, 0)
            return SimpleNamespace(stdout="")
        if command == "mv":
            self.events.append("rename-publish")
            self.final_path = pathlib.Path(arguments[-1])
            if self.should_fail("rename-publish-collision"):
                self.final_path.write_bytes(b"external")
                raise RuntimeError("destination appeared during publish")
            if self.should_fail("rename-publish-no-clobber"):
                self.final_path.write_bytes(b"external")
                return SimpleNamespace(stdout="")
            os.replace(arguments[-2], arguments[-1])
            if self.post_publish_drift == "owner":
                os.chown(self.final_path, 65534, 65534)
            elif self.post_publish_drift == "mode":
                os.chmod(self.final_path, 0o644)
            return SimpleNamespace(stdout="")
        if command == "virsh" and "start" in arguments:
            self.events.append("start-source")
            if self.should_fail("start-source"):
                raise RuntimeError("source start failed")
            return SimpleNamespace(stdout="Domain windows11 started\n")
        if command == "docker" and ("exec" in arguments or "run" in arguments):
            if "psql" in arguments:
                return SimpleNamespace(stdout=getattr(
                    self,
                    "guac_connection_output",
                    "2|Windows 11|rdp|match|match\n",
                ))
            self.events.append("wait-source-rdp")
            if self.should_fail("wait-source-rdp"):
                raise RuntimeError("RDP probe failed")
            return SimpleNamespace(stdout="")
        raise AssertionError(f"unexpected command: {arguments!r}")

    def record_stage(self, stage: str):
        self.events.append(stage)


class TemplateTransactionTests(unittest.TestCase):
    def _paths(self, directory: str):
        root = pathlib.Path(directory)
        source = root / "windows11.qcow2"
        source.write_bytes(b"source")
        return root, source

    def test_creates_template_in_transactional_stage_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            runner = FakeCommandRunner(root)
            inventory_path = root / "inventory.json"
            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "TEMPLATES_DIR", root
            ), mock.patch.object(workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ), mock.patch.object(
                workspace_helper, "wait_for_rdp", side_effect=lambda _runner, _source: runner.events.append("wait-source-rdp")
            ):
                result = create_template("windows11", "windows11-v1", runner)

            self.assertEqual(runner.events, [
                "validate-source", "shutdown-source", "wait-shutoff", "check-unlocked",
                "qemu-img-check-source", "convert-temp", "check-temp", "hash-temp",
                "chown-root", "chmod-readonly", "rename-publish", "save-inventory", "start-source",
                "wait-source-rdp"
            ])
            self.assertEqual(result.sourceDomain, "windows11")
            self.assertEqual(result.version, "windows11-v1")
            self.assertTrue((root / "windows11-v1.qcow2").exists())
            self.assertFalse((root / "windows11-v1.qcow2.partial").exists())
            self.assertEqual((root / "windows11-v1.qcow2").stat().st_mode & 0o777, 0o444)
            self.assertEqual(load_inventory(inventory_path)["templates"], [result.to_dict()])

    def test_non_root_conversion_is_chowned_before_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            runner = FakeCommandRunner(root)
            runner.conversion_owner = (64055, 993)  # live libvirt-qemu:kvm owner
            inventory_path = root / "inventory.json"
            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "TEMPLATES_DIR", root
            ), mock.patch.object(workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ), mock.patch.object(
                workspace_helper, "wait_for_rdp", side_effect=lambda _runner, _source: runner.events.append("wait-source-rdp")
            ):
                result = create_template("windows11", "windows11-v1", runner)

            published = root / "windows11-v1.qcow2"
            metadata = published.stat()
            self.assertEqual(runner.converted_owner, (64055, 993))
            self.assertEqual((metadata.st_uid, metadata.st_gid), (0, 0))
            self.assertEqual(metadata.st_mode & 0o7777, 0o444)
            self.assertIn("chown-root", runner.events)
            self.assertEqual(load_inventory(inventory_path)["templates"], [result.to_dict()])

    def test_chown_failure_rolls_back_without_inventory_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            runner = FakeCommandRunner(root, fail_stage="chown-root")
            inventory_path = root / "inventory.json"
            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "TEMPLATES_DIR", root
            ), mock.patch.object(workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ):
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    create_template("windows11", "windows11-v1", runner)

            self.assertEqual(failure.exception.code, "COMMAND_FAILED")
            self.assertEqual(failure.exception.stage, "chown-root")
            self.assertFalse((root / "windows11-v1.qcow2.partial").exists())
            self.assertFalse((root / "windows11-v1.qcow2").exists())
            self.assertFalse(inventory_path.exists())

    def test_post_publish_owner_or_mode_drift_rolls_back_before_inventory_write(self):
        for drift in ("owner", "mode"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                root, source = self._paths(directory)
                runner = FakeCommandRunner(root)
                runner.post_publish_drift = drift
                inventory_path = root / "inventory.json"
                with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                    workspace_helper, "TEMPLATES_DIR", root
                ), mock.patch.object(workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path), mock.patch.object(
                    workspace_helper, "INVENTORY_PATH", inventory_path
                ):
                    with self.assertRaises(workspace_helper.HelperError) as failure:
                        create_template("windows11", "windows11-v1", runner)

                self.assertEqual(failure.exception.code, "TEMPLATE_INVALID")
                self.assertEqual(failure.exception.stage, "rename-publish")
                self.assertFalse((root / "windows11-v1.qcow2.partial").exists())
                self.assertFalse((root / "windows11-v1.qcow2").exists())
                self.assertFalse(inventory_path.exists())

    def test_conversion_failure_rolls_back_and_restarts_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            runner = FakeCommandRunner(root, fail_stage="convert-temp")
            inventory_path = root / "inventory.json"
            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "TEMPLATES_DIR", root
            ), mock.patch.object(workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ):
                with self.assertRaises(workspace_helper.HelperError):
                    create_template("windows11", "windows11-v1", runner)

            self.assertFalse((root / "windows11-v1.qcow2.partial").exists())
            self.assertFalse((root / "windows11-v1.qcow2").exists())
            self.assertIn("start-source", runner.events)

    def test_inventory_failure_rolls_back_and_restarts_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            runner = FakeCommandRunner(root)
            inventory_path = root / "inventory.json"
            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "TEMPLATES_DIR", root
            ), mock.patch.object(workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ), mock.patch.object(
                workspace_helper, "save_inventory_atomic", side_effect=workspace_helper.InventoryError("save failed")
            ):
                with self.assertRaises(workspace_helper.InventoryError):
                    create_template("windows11", "windows11-v1", runner)

            self.assertFalse((root / "windows11-v1.qcow2.partial").exists())
            self.assertFalse((root / "windows11-v1.qcow2").exists())
            self.assertIn("start-source", runner.events)

    def test_existing_version_fails_before_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            final_path = root / "windows11-v1.qcow2"
            final_path.write_bytes(b"existing")
            runner = FakeCommandRunner(root)
            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "TEMPLATES_DIR", root
            ), mock.patch.object(workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", root / "inventory.json"
            ):
                with self.assertRaises(workspace_helper.ConflictError):
                    create_template("windows11", "windows11-v1", runner)
            self.assertEqual(runner.events, [])

    def test_running_what_if_checks_source_and_storage_without_lifecycle_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            runner = FakeCommandRunner(root)
            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "TEMPLATES_DIR", root
            ), mock.patch.object(workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", root / "inventory.json"
            ):
                self.assertIsNone(create_template("windows11", "windows11-v1", runner, what_if=True))
            self.assertEqual(runner.events, ["validate-source"])
            self.assertFalse((root / "windows11-v1.qcow2").exists())
            self.assertFalse((root / "windows11-v1.qcow2.partial").exists())
            self.assertFalse((root / "inventory.json").exists())

    def test_preflight_requires_persistent_source_assets_and_network(self):
        cases = (
            "nonpersistent",
            "missing-nvram",
            "inactive-network",
            "inactive-tpm",
            "missing-tpm-socket",
            "missing-tpm-state",
            "invalid-tpm-contract",
            "invalid-guacamole-connection",
            "invalid-guacamole-protocol",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, source = self._paths(directory)
                runner = FakeCommandRunner(root)
                if case == "nonpersistent":
                    runner.dominfo_output = "Name: windows11\nState: running\nPersistent: no\n"
                elif case == "missing-nvram":
                    runner.nvram_path.unlink()
                elif case == "inactive-network":
                    runner.net_info_output = "Name: guac-nat\nActive: no\nPersistent: yes\n"
                elif case == "inactive-tpm":
                    runner.tpm_state_output = "inactive\n"
                elif case == "missing-tpm-socket":
                    runner.socket_available = False
                elif case == "missing-tpm-state":
                    pass
                elif case == "invalid-tpm-contract":
                    runner.tpm_unit_output = "ExecStart=/usr/bin/swtpm socket --tpm2\n"
                elif case == "invalid-guacamole-connection":
                    runner.guac_connection_output = "2|Windows 11|rdp|mismatch|match\n"
                else:
                    runner.guac_connection_output = "2|Windows 11|ssh|match|match\n"
                before_paths = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
                tpm_state_path = root / "missing-tpm-state"
                tpm_state_patch = mock.patch.object(
                    workspace_helper, "SOURCE_TPM_STATE_PATH", tpm_state_path
                ) if case == "missing-tpm-state" else mock.patch.object(
                    workspace_helper, "SOURCE_TPM_STATE_PATH", workspace_helper.SOURCE_TPM_STATE_PATH
                )
                with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                    workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path
                ), mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), mock.patch.object(
                    workspace_helper, "INVENTORY_PATH", root / "inventory.json"
                ), tpm_state_patch:
                    with self.assertRaises(workspace_helper.HelperError) as failure:
                        create_template("windows11", "windows11-v1", runner, what_if=True)
                expected = {
                    "nonpersistent": "SOURCE_NOT_PERSISTENT",
                    "missing-nvram": "SOURCE_ASSET_MISSING",
                    "inactive-network": "SOURCE_NETWORK_INVALID",
                    "inactive-tpm": "TPM_SERVICE_INVALID",
                    "missing-tpm-socket": "TPM_SOCKET_INVALID",
                    "missing-tpm-state": "TPM_STATE_INVALID",
                    "invalid-tpm-contract": "TPM_SERVICE_INVALID",
                    "invalid-guacamole-connection": "GUAC_CONNECTION_INVALID",
                    "invalid-guacamole-protocol": "GUAC_CONNECTION_INVALID",
                }[case]
                self.assertEqual(failure.exception.code, expected)
                self.assertEqual(runner.events, ["validate-source"])
                self.assertEqual(
                    sorted(path.relative_to(root).as_posix() for path in root.rglob("*")),
                    before_paths,
                )

    def test_shutdown_timeout_after_request_still_recovers_source(self):
        for recovery_states in (("in shutdown", "shut off"), ("running", "shut off")):
            with self.subTest(recovery_states=recovery_states), tempfile.TemporaryDirectory() as directory:
                root, source = self._paths(directory)
                base_runner = FakeCommandRunner(root)
                base_runner.recovery_states = list(recovery_states)
                inventory_path = root / "inventory.json"

                def timeout_shutdown(arguments):
                    if arguments[0] == "virsh" and "shutdown" in arguments:
                        base_runner.recovery_mode = True
                        base_runner.events.append("shutdown-source")
                        raise subprocess.TimeoutExpired(arguments, 0.1)
                    return base_runner(arguments)

                with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                    workspace_helper, "SOURCE_NVRAM_PATH", base_runner.nvram_path
                ), mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), mock.patch.object(
                    workspace_helper, "INVENTORY_PATH", inventory_path
                ):
                    with self.assertRaises(workspace_helper.HelperError) as failure:
                        create_template("windows11", "windows11-v1", timeout_shutdown)

                self.assertEqual(failure.exception.code, "COMMAND_TIMEOUT")
                self.assertEqual(failure.exception.stage, "shutdown-source")
                self.assertIn("start-source", base_runner.events)
                self.assertIn("wait-source-rdp", base_runner.events)
                self.assertFalse((root / "windows11-v1.qcow2.partial").exists())
                self.assertFalse((root / "windows11-v1.qcow2").exists())
                self.assertFalse(inventory_path.exists())

    def test_keyboard_interrupt_after_shutdown_request_still_recovers_source(self):
        for recovery_states in (("in shutdown", "shut off"), ("running", "shut off")):
            with self.subTest(recovery_states=recovery_states), tempfile.TemporaryDirectory() as directory:
                root, source = self._paths(directory)
                base_runner = FakeCommandRunner(root)
                base_runner.recovery_states = list(recovery_states)
                inventory_path = root / "inventory.json"

                def interrupt_shutdown(arguments):
                    if arguments[0] == "virsh" and "shutdown" in arguments:
                        base_runner.recovery_mode = True
                        base_runner.events.append("shutdown-source")
                        raise KeyboardInterrupt()
                    return base_runner(arguments)

                with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                    workspace_helper, "SOURCE_NVRAM_PATH", base_runner.nvram_path
                ), mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), mock.patch.object(
                    workspace_helper, "INVENTORY_PATH", inventory_path
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        create_template("windows11", "windows11-v1", interrupt_shutdown)

                self.assertIn("start-source", base_runner.events)
                self.assertIn("wait-source-rdp", base_runner.events)
                self.assertFalse((root / "windows11-v1.qcow2.partial").exists())
                self.assertFalse((root / "windows11-v1.qcow2").exists())
                self.assertFalse(inventory_path.exists())

    def test_recovery_state_poll_has_a_bounded_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            base_runner = FakeCommandRunner(root)
            base_runner.recovery_states = ["running"]
            inventory_path = root / "inventory.json"

            def timeout_shutdown(arguments):
                if arguments[0] == "virsh" and "shutdown" in arguments:
                    base_runner.recovery_mode = True
                    raise subprocess.TimeoutExpired(arguments, 0.1)
                return base_runner(arguments)

            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "SOURCE_NVRAM_PATH", base_runner.nvram_path
            ), mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ), mock.patch.object(workspace_helper, "SHUTDOWN_TIMEOUT_SECONDS", 0.01), mock.patch.object(
                workspace_helper, "POLL_INTERVAL_SECONDS", 0
            ):
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    create_template("windows11", "windows11-v1", timeout_shutdown)

            self.assertEqual(failure.exception.code, "RECOVERY_FAILED")
            self.assertIn("did not reach shut off during recovery", failure.exception.message)
            self.assertNotIn("start-source", base_runner.events)
            self.assertNotIn("wait-source-rdp", base_runner.events)

    def test_primary_and_recovery_failure_are_both_reported_and_rolled_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            runner = FakeCommandRunner(root, fail_stage="convert-temp")
            runner.fail_stages.add("start-source")
            inventory_path = root / "inventory.json"
            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path
            ), mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ):
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    create_template("windows11", "windows11-v1", runner)
            self.assertEqual(failure.exception.code, "RECOVERY_FAILED")
            self.assertEqual(failure.exception.stage, "recovery")
            self.assertIn("convert-temp", failure.exception.message)
            self.assertIn("start-source", failure.exception.message)
            self.assertFalse((root / "windows11-v1.qcow2.partial").exists())
            self.assertFalse((root / "windows11-v1.qcow2").exists())
            self.assertFalse(inventory_path.exists())

    def test_rdp_recovery_failure_rolls_back_published_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            runner = FakeCommandRunner(root)
            inventory_path = root / "inventory.json"
            rdp_failure = workspace_helper.HelperError(
                "RDP probe failed", code="RDP_NOT_READY", stage="wait-source-rdp"
            )
            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path
            ), mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ), mock.patch.object(workspace_helper, "wait_for_rdp", side_effect=rdp_failure):
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    create_template("windows11", "windows11-v1", runner)
            self.assertEqual(failure.exception.code, "RECOVERY_FAILED")
            self.assertEqual(failure.exception.stage, "recovery")
            self.assertFalse((root / "windows11-v1.qcow2.partial").exists())
            self.assertFalse((root / "windows11-v1.qcow2").exists())
            self.assertFalse(inventory_path.exists())

    def test_inventory_failure_after_replace_restores_previous_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            runner = FakeCommandRunner(root)
            inventory_path = root / "inventory.json"
            previous_inventory = {"templates": [], "clones": []}
            save_inventory_atomic(previous_inventory, inventory_path)
            real_fsync = os.fsync
            fsync_calls = 0

            def fail_directory_fsync(descriptor):
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError("directory fsync failed after replace")
                return real_fsync(descriptor)

            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path
            ), mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ), mock.patch.object(workspace_helper.os, "fsync", side_effect=fail_directory_fsync):
                with self.assertRaises(workspace_helper.InventoryError):
                    create_template("windows11", "windows11-v1", runner)
            self.assertEqual(load_inventory(inventory_path), previous_inventory)
            self.assertFalse((root / "windows11-v1.qcow2.partial").exists())
            self.assertFalse((root / "windows11-v1.qcow2").exists())
            self.assertIn("start-source", runner.events)

    def test_external_final_collision_is_preserved_during_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            runner = FakeCommandRunner(root, fail_stage="rename-publish-no-clobber")
            inventory_path = root / "inventory.json"
            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path
            ), mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ):
                with self.assertRaises(workspace_helper.HelperError):
                    create_template("windows11", "windows11-v1", runner)
            self.assertEqual((root / "windows11-v1.qcow2").read_bytes(), b"external")
            self.assertFalse((root / "windows11-v1.qcow2.partial").exists())
            self.assertFalse(inventory_path.exists())

    def test_runner_timeout_is_mapped_to_polling_stage_and_recovery_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            base_runner = FakeCommandRunner(root)
            inventory_path = root / "inventory.json"

            timeout_remaining = 1

            def timeout_runner(arguments):
                nonlocal timeout_remaining
                if arguments[0] == "virsh" and "domstate" in arguments and timeout_remaining:
                    timeout_remaining -= 1
                    base_runner.recovery_mode = True
                    raise subprocess.TimeoutExpired(arguments, 0.1)
                return base_runner(arguments)

            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "SOURCE_NVRAM_PATH", base_runner.nvram_path
            ), mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ):
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    create_template("windows11", "windows11-v1", timeout_runner)
            self.assertEqual(failure.exception.code, "COMMAND_TIMEOUT")
            self.assertEqual(failure.exception.stage, "wait-shutoff")
            self.assertIn("start-source", base_runner.events)

    def test_rollback_cleanup_failure_preserves_primary_and_recovery_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            runner = FakeCommandRunner(root)
            runner.fail_stages.add("start-source")
            inventory_path = root / "inventory.json"
            real_remove = workspace_helper._remove_if_present

            def fail_final_cleanup(path):
                if pathlib.Path(path) == root / "windows11-v1.qcow2":
                    raise OSError("final cleanup failed")
                return real_remove(path)

            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "SOURCE_NVRAM_PATH", runner.nvram_path
            ), mock.patch.object(workspace_helper, "TEMPLATES_DIR", root), mock.patch.object(
                workspace_helper, "INVENTORY_PATH", inventory_path
            ), mock.patch.object(
                workspace_helper,
                "save_inventory_atomic",
                side_effect=workspace_helper.InventoryError("manifest save failed", replaced=True),
            ), mock.patch.object(workspace_helper, "_remove_if_present", side_effect=fail_final_cleanup):
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    create_template("windows11", "windows11-v1", runner)

            self.assertEqual(failure.exception.code, "ROLLBACK_FAILED")
            self.assertEqual(failure.exception.stage, "rollback")
            self.assertIn("final cleanup failed", failure.exception.message)
            self.assertIn("primary=manifest save failed", failure.exception.message)
            self.assertIn("recovery=start-source command failed", failure.exception.message)
            self.assertTrue((root / "windows11-v1.qcow2").exists())

    def test_default_runner_receives_timeout_budget(self):
        timeout_error = subprocess.TimeoutExpired(["virsh", "domstate"], 0.5)
        with mock.patch.object(workspace_helper.subprocess, "run", side_effect=timeout_error) as process:
            with self.assertRaises(workspace_helper.HelperError) as failure:
                workspace_helper._run_stage(
                    workspace_helper.run_command,
                    ["virsh", "domstate"],
                    "wait-shutoff",
                    timeout=0.5,
                )
        self.assertEqual(failure.exception.code, "COMMAND_TIMEOUT")
        self.assertEqual(failure.exception.stage, "wait-shutoff")
        self.assertEqual(process.call_args.kwargs["timeout"], 0.5)

    def test_non_windows11_source_is_rejected_before_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeCommandRunner(pathlib.Path(directory))
            with self.assertRaises(ValidationError):
                create_template("windows11-02", "windows11-v1", runner)
            self.assertEqual(runner.events, [])

    def test_active_tpm_unit_is_rejected_before_source_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)
            runner = FakeCommandRunner(root)

            def active_tpm(arguments):
                if arguments[0] == "systemctl":
                    runner.events.append("check-unlocked")
                    return SimpleNamespace(stdout="active\n")
                return runner(arguments)

            with mock.patch.object(workspace_helper, "SOURCE_DISK_PATH", source), mock.patch.object(
                workspace_helper, "TEMPLATES_DIR", root
            ), mock.patch.object(workspace_helper, "INVENTORY_PATH", root / "inventory.json"), mock.patch.object(
                workspace_helper, "_source_disk_is_held", return_value=False
            ):
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    workspace_helper._check_source_unlocked(source, active_tpm)
            self.assertEqual(failure.exception.code, "TPM_STILL_ACTIVE")

    def test_failed_tpm_state_query_is_not_treated_as_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)

            def failed_tpm(arguments):
                if arguments[0] == "systemctl":
                    error = subprocess.CalledProcessError(3, arguments, output="", stderr="dbus unavailable")
                    raise error
                raise AssertionError(arguments)

            with mock.patch.object(workspace_helper, "_source_disk_is_held", return_value=False):
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    workspace_helper._check_source_unlocked(source, failed_tpm)
            self.assertEqual(failure.exception.code, "TPM_STATE_UNKNOWN")

    def test_inactive_tpm_exit_status_is_accepted_when_state_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source = self._paths(directory)

            def inactive_tpm(arguments):
                if arguments[0] == "systemctl":
                    raise subprocess.CalledProcessError(3, arguments, output="inactive\n", stderr="")
                raise AssertionError(arguments)

            with mock.patch.object(workspace_helper, "_source_disk_is_held", return_value=False):
                workspace_helper._check_source_unlocked(source, inactive_tpm)


class ValidationTests(unittest.TestCase):
    def test_rejects_shell_metacharacters_before_commands(self):
        for value in ("x;id", "$(id)", "../x", "x y", "x'OR'1"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_vm_name(value)

    def test_accepts_safe_vm_names_and_assignees(self):
        for value in (
            "Windows_Template.Test-01",
            "vm-",
            "vm.",
            "vm_",
            "A" + "_" * 63,
        ):
            with self.subTest(value=value):
                self.assertEqual(validate_vm_name(value), value)
        self.assertEqual(validate_assignee("USER", "demo"), ("USER", "demo"))
        self.assertEqual(validate_assignee("USER_GROUP", "user@example.com"), ("USER_GROUP", "user@example.com"))
        self.assertEqual(validate_assignee("USER", "u" + "a" * 126 + "@"), ("USER", "u" + "a" * 126 + "@"))

    def test_validation_boundaries_and_required_assignee_fields(self):
        with self.assertRaises(ValidationError):
            validate_vm_name("A" + "_" * 64)
        with self.assertRaises(ValidationError):
            validate_assignee("USER", "u" + "a" * 127 + "@")
        with self.assertRaises(ValidationError):
            validate_assignee("USER")

    def test_atomic_inventory_rejects_duplicate_ip_and_mac(self):
        inventory = {
            "clones": [
                {"name": "vm-a", "ip": "192.168.250.20", "mac": "52:54:00:20:00:01"}
            ]
        }
        with self.assertRaises(ConflictError):
            assert_unique_clone(inventory, "vm-b", "192.168.250.20", "52:54:00:20:00:02")
        with self.assertRaises(ConflictError):
            assert_unique_clone(inventory, "vm-b", "192.168.250.21", "52:54:00:20:00:01")

    def test_inventory_round_trip_uses_target_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "inventory.json"
            inventory = {
                "templates": [],
                "clones": [{"name": "vm-a", "ip": "192.168.250.20", "mac": "52:54:00:20:00:01"}],
            }
            save_inventory_atomic(inventory, path)
            self.assertEqual(load_inventory(path), inventory)
            self.assertEqual(list(path.parent.glob(".inventory.json.*.tmp")), [])

    def test_inventory_allowlist_drops_unknown_and_secret_like_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic(
                {
                    "templates": [],
                    "unexpected": "PRIVATE-KEY-MATERIAL",
                    "clones": [{
                        "name": "vm-a",
                        "mac": "52:54:00:20:00:01",
                        "ip": "192.168.250.20",
                        "password": "gateway-secret",
                        "parameters": {"token": "alice-secret"},
                    }],
                },
                path,
            )
            payload = load_inventory(path)
        serialized = json.dumps(payload)
        for secret in ("PRIVATE-KEY-MATERIAL", "gateway-secret", "alice-secret"):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("unexpected", payload)
        self.assertNotIn("password", payload["clones"][0])
        self.assertNotIn("parameters", payload["clones"][0])

    def test_workspace_lock_is_exclusive_and_non_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = pathlib.Path(directory) / "workspace.lock"
            with workspace_lock(lock_path):
                with self.assertRaises(LockError):
                    with workspace_lock(lock_path):
                        pass


class GuacamoleSyncTests(unittest.TestCase):
    def _record(self, **overrides):
        value = {
            "name": "vm-new",
            "mac": "52:54:00:20:00:01",
            "ip": "192.168.250.55",
            "assigneeType": "USER",
            "assigneeName": "demo",
            "status": "waiting-rdp",
            "syncAttemptId": "11111111-2222-4333-8444-555555555555",
        }
        value.update(overrides)
        return value

    def test_generated_sql_rejects_hostile_name_and_assignee(self):
        for value in ("vm;drop", "$(id)", "vm name"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                workspace_helper.build_guacamole_sync_sql(self._record(name=value))
        with self.assertRaises(ValidationError):
            workspace_helper.build_guacamole_sync_sql(self._record(assigneeName="demo;drop"))

    def test_generated_sql_has_only_safe_rdp_parameters(self):
        sql = workspace_helper.build_guacamole_sync_sql(self._record())
        expected = {"hostname", "port", "security", "ignore-cert"}
        parameter_values = set(__import__("re").findall(
            r"\('([a-z][a-z-]*)',\s*(?::'sync_ip'|'[a-z0-9-]+')\)", sql
        ))
        self.assertEqual(parameter_values, expected)
        for forbidden in ("username", "password", "gateway", "token", "hash"):
            self.assertNotIn(forbidden, sql.lower())
        self.assertIn("entity_id = (SELECT entity_id FROM _sync_assignee)", sql)
        self.assertIn("permission <> 'READ'", sql)

    def test_managed_clone_sql_allowlists_windows_credential_parameters(self):
        sql = workspace_helper.build_guacamole_sync_sql(
            self._record(),
            require_new_connection=True,
            include_windows_credentials=True,
        )
        parameter_values = set(re.findall(
            r"\('([a-z][a-z-]*)',\s*(?::'sync_ip'|\(SELECT value FROM _sync_secure_values WHERE name = '[a-z]+'\)|'[a-z0-9-]+')\)",
            sql,
        ))
        self.assertEqual(
            parameter_values,
            {"hostname", "port", "security", "ignore-cert", "username", "password"},
        )
        self.assertIn("'username', (SELECT value FROM _sync_secure_values WHERE name = 'username')", sql)
        self.assertIn("'password', (SELECT value FROM _sync_secure_values WHERE name = 'password')", sql)
        self.assertIn("-- GUACAMOLE_SECURE_CREDENTIALS", sql)

    def test_existing_connection_credential_update_requires_managed_markers(self):
        sql = workspace_helper.build_guacamole_sync_sql(
            self._record(),
            include_windows_credentials=True,
        )
        self.assertIn("attempt.attribute_name = 'org.apache.guacamole.workspace.sync.attempt'", sql)
        self.assertIn("assignee_marker.attribute_name = 'org.apache.guacamole.workspace.sync.assignee-entity'", sql)
        self.assertNotIn("WHERE connection_name = :'sync_name' AND parent_id IS NULL;", sql)

    def test_windows_credentials_fail_closed_before_psql_when_secret_is_missing(self):
        calls = []

        def runner(arguments, sql, *, timeout=None):
            del timeout
            calls.append((arguments, sql))
            raise AssertionError("psql must not run without the Windows secret")

        with tempfile.TemporaryDirectory() as directory:
            secret_path = pathlib.Path(directory) / "windows-guacadmin-password"
            with mock.patch.object(workspace_helper, "WINDOWS_CREDENTIAL_SECRET_PATH", secret_path):
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    workspace_helper.sync_guacamole(
                        self._record(),
                        runner=runner,
                        require_new_connection=True,
                        include_windows_credentials=True,
                    )
        self.assertEqual(failure.exception.code, "CREDENTIAL_SECRET_MISSING")
        self.assertEqual(calls, [])

    def test_windows_secret_replacement_is_atomic_and_not_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "windows-guacadmin-password"
            workspace_helper.write_windows_credential_secret("first-secret", path)
            workspace_helper.write_windows_credential_secret("second-secret", path)
            self.assertEqual(workspace_helper.read_windows_credential_secret(path).password, "second-secret")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            self.assertNotIn("second-secret", json.dumps(workspace_helper.read_windows_credential_secret(path).to_public_dict()))

    def test_psql_credential_is_sent_on_stdin_and_never_in_argv(self):
        captured = {}

        def fake_run(arguments, **kwargs):
            captured["arguments"] = list(arguments)
            captured["input"] = kwargs["input"]
            return SimpleNamespace(stdout="", stderr="")

        with mock.patch.object(workspace_helper.subprocess, "run", side_effect=fake_run):
            workspace_helper.run_psql_sync(
                ["docker", "compose", "exec", "-T", "postgres", "psql"],
                "BEGIN;\n-- GUACAMOLE_SECURE_CREDENTIALS\nSELECT 1;\n",
                secure_values={"username": "guacadmin", "password": "stdin-secret"},
            )
        self.assertNotIn("stdin-secret", " ".join(captured["arguments"]))
        self.assertIn("stdin-secret", captured["input"])
        self.assertNotIn("stdin-secret", captured["input"].split("COPY _sync_secure_values", 1)[0])
        self.assertNotIn("'stdin-secret'", captured["input"])

    def test_credential_create_branch_inserts_and_claims_exactly_one_new_row(self):
        sql = workspace_helper.build_guacamole_sync_sql(
            self._record(), require_new_connection=True, include_windows_credentials=True
        )
        self.assertIn("INSERT INTO guacamole_connection (connection_name, parent_id, protocol)", sql)
        self.assertIn("INSERT INTO _sync_created (connection_id)", sql)
        self.assertIn("SELECT connection_id FROM _sync_created", sql)
        self.assertIn("created_connections <> 1", sql)
        self.assertIn("GUACAMOLE_SECURE_CREDENTIALS", sql)

    def test_credential_enabled_create_runs_secure_transport_and_returns_created_row(self):
        captured = {}
        result_line = json.dumps({
            "connectionId": 91,
            "connectionName": "vm-new",
            "created": True,
            "credentialsPresent": True,
            "permissions": {
                "assignee": ["READ"],
                "guacadmin": ["READ", "UPDATE", "DELETE", "ADMINISTER"],
            },
        })

        def fake_run(arguments, **kwargs):
            captured["arguments"] = list(arguments)
            captured["input"] = kwargs["input"]
            return SimpleNamespace(stdout=result_line + "\n", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "password"
            with mock.patch.object(workspace_helper, "WINDOWS_CREDENTIAL_SECRET_PATH", path):
                workspace_helper.write_windows_credential_secret("create-secret", path)
                with mock.patch.object(workspace_helper.subprocess, "run", side_effect=fake_run):
                    result = workspace_helper.sync_guacamole(
                        self._record(), require_new_connection=True, include_windows_credentials=True,
                    )
        self.assertEqual(result["clone"]["connectionId"], 91)
        self.assertIn("INSERT INTO guacamole_connection", captured["input"])
        self.assertIn("create-secret", captured["input"])
        self.assertNotIn("create-secret", " ".join(captured["arguments"]))
        self.assertNotIn("'create-secret'", captured["input"])

    def test_secret_rejects_empty_newline_and_oversized_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "windows-guacadmin-password"
            for value in ("", "line\nfeed", "x" * workspace_helper.WINDOWS_CREDENTIAL_MAX_BYTES):
                with self.subTest(value=repr(value)), self.assertRaises(workspace_helper.CredentialSecretError):
                    workspace_helper.write_windows_credential_secret(value, path)
                self.assertFalse(path.exists())

    def test_production_secret_path_ignores_arbitrary_environment_override(self):
        with mock.patch.dict(os.environ, {"GUACAMOLE_WINDOWS_CREDENTIAL_SECRET": "C:\\temp\\secret.txt"}, clear=False):
            self.assertEqual(
                workspace_helper._configured_windows_credential_secret_path(),
                workspace_helper._CANONICAL_WINDOWS_CREDENTIAL_SECRET_PATH,
            )

    @unittest.skipUnless(os.name == "posix", "descriptor mode and symlink test requires POSIX")
    def test_secret_requires_exact_parent_and_file_modes_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            parent = root / "secrets"
            parent.mkdir()
            os.chmod(parent, 0o755)
            path = parent / "password"
            with self.assertRaises(workspace_helper.CredentialSecretError):
                workspace_helper.write_windows_credential_secret("safe", path)
            os.chmod(parent, 0o700)
            workspace_helper.write_windows_credential_secret("safe", path)
            self.assertEqual(path.stat().st_mode & 0o7777, 0o600)
            path.unlink()
            target = root / "target"
            target.write_text("safe\n", encoding="utf-8")
            path.symlink_to(target)
            with self.assertRaises(workspace_helper.CredentialSecretError):
                workspace_helper.read_windows_credential_secret(path)

    def test_secure_transport_quotes_and_backslashes_are_copy_data_not_sql(self):
        captured = {}

        def fake_run(arguments, **kwargs):
            captured["arguments"] = list(arguments)
            captured["input"] = kwargs["input"]
            return SimpleNamespace(stdout="", stderr="")

        password = 'q"uote\\slash'
        with mock.patch.object(workspace_helper.subprocess, "run", side_effect=fake_run):
            workspace_helper.run_psql_sync(
                ["psql"],
                "BEGIN;\n-- GUACAMOLE_SECURE_CREDENTIALS\nSELECT 1;\n",
                secure_values={"username": "guacadmin", "password": password},
            )
        self.assertNotIn(password, " ".join(captured["arguments"]))
        sql_prefix, copy_data = captured["input"].split("COPY _sync_secure_values", 1)
        self.assertNotIn(password, sql_prefix)
        self.assertIn('password,"q""uote\\slash"', copy_data)

    def test_secure_transport_coexists_with_ordinary_psql_variables(self):
        captured = {}

        def fake_run(arguments, **kwargs):
            captured["input"] = kwargs["input"]
            return SimpleNamespace(stdout="", stderr="")

        with mock.patch.object(workspace_helper.subprocess, "run", side_effect=fake_run):
            workspace_helper.run_psql_sync(
                ["psql"],
                "BEGIN;\n-- GUACAMOLE_SECURE_CREDENTIALS\nSELECT :'sync_name';\n",
                psql_variables={"sync_name": "wtest"},
                secure_values={"username": "guacadmin", "password": "combined-secret"},
            )
        self.assertIn("\\set sync_name 'wtest'", captured["input"])
        self.assertIn("COPY _sync_secure_values", captured["input"])
        self.assertIn("combined-secret", captured["input"])

    def test_adoption_sql_requires_exact_identity_and_absent_attempt_marker(self):
        sql = workspace_helper.build_guacamole_sync_sql(
            dict(self._record(name="wtest")),
            connection_id=14,
            adopt_existing_connection=True,
            include_windows_credentials=True,
        )
        self.assertIn("connection.connection_id = :'sync_connection_id'::integer", sql)
        self.assertIn("connection.connection_name = :'sync_name'", sql)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("attribute_name = 'org.apache.guacamole.workspace.sync.attempt'", sql)
        self.assertIn("INSERT INTO guacamole_connection_attribute", sql)
        preflight = workspace_helper.build_guacamole_adoption_preflight_sql(14)
        self.assertIn("c.parent_id IS NULL", preflight)
        self.assertIn("connectionName", preflight)
        self.assertIn("adminPermissionsComplete", preflight)
        self.assertIn("attemptMarkerAbsent", preflight)
        self.assertIn("The managed Guacamole parameter allowlist is invalid", sql)

    def test_real_adoption_sync_passes_connection_id_and_keeps_secret_in_copy_data(self):
        captured = {}
        result_line = json.dumps({
            "connectionId": 14,
            "connectionName": "wtest",
            "created": False,
            "credentialsPresent": True,
            "permissions": {
                "assignee": ["READ"],
                "guacadmin": ["READ", "UPDATE", "DELETE", "ADMINISTER"],
            },
        })

        def fake_run(arguments, **kwargs):
            captured["arguments"] = list(arguments)
            captured["input"] = kwargs["input"]
            return SimpleNamespace(stdout=result_line + "\n", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "password"
            with mock.patch.object(workspace_helper, "WINDOWS_CREDENTIAL_SECRET_PATH", path):
                workspace_helper.write_windows_credential_secret("adopt-secret", path)
                with mock.patch.object(workspace_helper.subprocess, "run", side_effect=fake_run):
                    result = workspace_helper.sync_guacamole(
                        self._record(name="wtest"),
                        runner=workspace_helper.run_psql_sync,
                        connection_id=14,
                        include_windows_credentials=True,
                        adopt_existing_connection=True,
                    )
        self.assertEqual(result["clone"]["connectionId"], 14)
        self.assertIn("sync_connection_id=14", captured["arguments"])
        self.assertNotIn("adopt-secret", " ".join(captured["arguments"]))
        self.assertIn("adopt-secret", captured["input"])
        self.assertNotIn("adopt-secret", captured["input"].split("COPY _sync_secure_values", 1)[0])

    def test_adoption_preflight_conflict_does_not_start_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic(
                {"schema": "guacamole-workspace-v1", "templates": [], "clones": [
                    dict(self._record(name="wtest"), connectionId=14),
                    dict(self._record(name="other"), connectionId=14),
                ]},
                inventory_path,
            )
            with self.assertRaises(ConflictError):
                workspace_helper.adopt_guacamole_connection(
                    "wtest", 14, inventory_path=inventory_path,
                )

    def test_adoption_preflight_rejects_child_row_before_secret_or_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic(
                {"schema": "guacamole-workspace-v1", "templates": [], "clones": [dict(self._record(name="wtest")).copy()]},
                inventory_path,
            )
            calls = []

            def preflight(arguments, sql, *, timeout=None):
                del arguments, timeout
                calls.append(sql)
                return SimpleNamespace(stdout=json.dumps({
                    "eligible": False,
                    "connectionId": 14,
                    "connectionName": "wtest",
                    "protocol": "rdp",
                    "hostnameMatches": True,
                    "portMatches": True,
                    "assigneeMatches": True,
                    "adminPermissionsComplete": True,
                    "conflictingConnectionAbsent": True,
                    "attemptMarkerAbsent": True,
                }) + "\n")

            with mock.patch.object(workspace_helper, "read_windows_credential_secret") as read_secret, mock.patch.object(
                workspace_helper, "sync_guacamole"
            ) as sync:
                with self.assertRaises(ConflictError):
                    workspace_helper.adopt_guacamole_connection(
                        "wtest", 14, inventory_path=inventory_path, preflight_runner=preflight,
                    )
            self.assertEqual(len(calls), 1)
            self.assertIn("c.parent_id IS NULL", calls[0])
            read_secret.assert_not_called()
            sync.assert_not_called()

    def test_adoption_happy_path_requires_preflight_then_secret_and_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic(
                {"schema": "guacamole-workspace-v1", "templates": [], "clones": [dict(self._record(name="wtest")).copy()]},
                inventory_path,
            )
            calls = []

            def preflight(arguments, sql, *, timeout=None):
                calls.append(("preflight", sql))
                return SimpleNamespace(stdout='{"eligible":true,"connectionId":14}\n')

            def fake_sync(*args, **kwargs):
                calls.append(("sync", kwargs))
                return {"ok": True, "clone": {"status": "ready", "connectionId": 14}}

            with mock.patch.object(workspace_helper, "read_windows_credential_secret", return_value=SimpleNamespace(username="guacadmin", password="unused")), mock.patch.object(workspace_helper, "sync_guacamole", side_effect=fake_sync):
                result = workspace_helper.adopt_guacamole_connection(
                    "wtest", 14, inventory_path=inventory_path, preflight_runner=preflight,
                )
            self.assertEqual(result["clone"]["connectionId"], 14)
            self.assertEqual([item[0] for item in calls], ["preflight", "sync"])

    def test_adoption_sync_failure_leaves_inventory_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            original = {"schema": "guacamole-workspace-v1", "templates": [], "clones": [dict(self._record(name="wtest"))]}
            save_inventory_atomic(original, inventory_path)

            def preflight(arguments, sql, *, timeout=None):
                return SimpleNamespace(stdout='{"eligible":true,"connectionId":14}\n')

            with mock.patch.object(workspace_helper, "read_windows_credential_secret", return_value=SimpleNamespace(username="guacadmin", password="unused")), mock.patch.object(
                workspace_helper, "sync_guacamole", side_effect=workspace_helper.HelperError("db failed", code="SYNC_INVALID", stage="sync")
            ):
                with self.assertRaises(workspace_helper.HelperError):
                    workspace_helper.adopt_guacamole_connection(
                        "wtest", 14, inventory_path=inventory_path, preflight_runner=preflight,
                    )
            self.assertEqual(load_inventory(inventory_path)["clones"], original["clones"])

    def test_adoption_commit_uses_server_contract_without_post_commit_failure(self):
        result_line = json.dumps({
            "connectionId": 14,
            "connectionName": "wtest",
            "created": False,
            "credentialsPresent": True,
            "permissions": {"assignee": [], "guacadmin": []},
        })

        def fake_run(arguments, **kwargs):
            del arguments, kwargs
            return SimpleNamespace(stdout=result_line + "\n", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            original = {"schema": "guacamole-workspace-v1", "templates": [], "clones": [dict(self._record(name="wtest"))]}
            save_inventory_atomic(original, inventory_path)
            expected = load_inventory(inventory_path)
            secret_path = pathlib.Path(directory) / "password"
            with mock.patch.object(workspace_helper, "WINDOWS_CREDENTIAL_SECRET_PATH", secret_path), mock.patch.object(
                workspace_helper.subprocess, "run", side_effect=fake_run
            ):
                workspace_helper.write_windows_credential_secret("adoption-validation-secret", secret_path)
                result = workspace_helper.sync_guacamole(
                    self._record(name="wtest"),
                    inventory_path=inventory_path,
                    runner=workspace_helper.run_psql_sync,
                    connection_id=14,
                    include_windows_credentials=True,
                    adopt_existing_connection=True,
                )
            self.assertEqual(result["clone"]["status"], "ready")
            self.assertEqual(result["clone"]["connectionId"], 14)
            self.assertEqual(load_inventory(inventory_path), expected)

    def test_installed_worker_publication_contains_credential_feature_and_secret_exclusion(self):
        installer = (HELPER_PATH.parent / "libvirt.ps1").read_text(encoding="utf-8")
        source = HELPER_PATH.read_text(encoding="utf-8")
        self.assertIn("set-windows-credential", installer)
        self.assertIn("/var/lib/guacamole-workspace/secrets/windows11_guacadmin_password", installer)
        self.assertNotIn("/mnt/h/RemoteWorkspaces/guacamole-client/runtime/secrets", installer)
        self.assertIn("set-windows-credential", source)
        self.assertIn("GUACAMOLE_SECURE_CREDENTIALS", source)

    def test_windows_secret_path_is_outside_maintenance_bundle_and_gitignored(self):
        secret_path = pathlib.Path(workspace_helper.WINDOWS_CREDENTIAL_SECRET_PATH)
        self.assertEqual(str(secret_path), "/var/lib/guacamole-workspace/secrets/windows11_guacadmin_password")
        self.assertNotIn("mnt", secret_path.parts)
        self.assertNotIn("runtime", secret_path.parts)
        gitignore = (HELPER_PATH.parents[1] / ".gitignore").read_text(encoding="utf-8")
        self.assertRegex(gitignore, r"(?m)^runtime/secrets/$")
        self.assertRegex(gitignore, r"(?m)^deploy-local/\*\*/windows11_guacadmin_password$")
        self.assertRegex(gitignore, r"(?m)^deploy-local/secrets/\*\.txt$")
        exporter = (HELPER_PATH.parent / "export-maintenance-bundle.ps1").read_text(encoding="utf-8")
        self.assertIn("secrets", exporter)
        self.assertIn("runtime", exporter)

    def test_production_secret_contract_is_ext4_posix_and_has_no_drvfs_migration(self):
        source = HELPER_PATH.read_text(encoding="utf-8")
        installer = (HELPER_PATH.parent / "libvirt.ps1").read_text(encoding="utf-8")
        self.assertIn("/var/lib/guacamole-workspace/secrets/windows11_guacadmin_password", source)
        self.assertIn("secret_root='/var/lib/guacamole-workspace/secrets'", installer)
        self.assertNotIn("/mnt/h/RemoteWorkspaces/guacamole-client/runtime/secrets", source)
        self.assertNotIn("/mnt/h/RemoteWorkspaces/guacamole-client/runtime/secrets", installer)
        self.assertNotIn("shutil.copy", source)
        self.assertNotIn("migrate", source.lower())

    def test_sync_rejects_excess_direct_assignee_permissions(self):
        class Runner:
            def __call__(self, arguments, sql, *, timeout=None):
                return SimpleNamespace(stdout=(
                    '{"connectionId":17,"permissions":{"assignee":["READ","UPDATE"],'
                    '"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]}}\n'
                ))

        with self.assertRaises(workspace_helper.HelperError) as context:
            workspace_helper.sync_guacamole(self._record(), runner=Runner(), transaction_end="ROLLBACK")
        self.assertEqual(context.exception.code, "SYNC_PERMISSIONS_INVALID")

    def test_sync_commit_uses_server_permission_contract_without_snapshot(self):
        class Runner:
            def __init__(self):
                self.calls = []

            def __call__(self, arguments, sql, *, timeout=None):
                self.calls.append((list(arguments), sql))
                if "GUACAMOLE_RESTORE_CREATED" in sql:
                    return SimpleNamespace(stdout='{"compensated":true,"deleted":true,"ownership":"owned"}\n')
                return SimpleNamespace(stdout=(
                    '{"connectionId":17,"permissions":{"assignee":["READ","UPDATE"],'
                    '"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]},'
                    '"created":true}\n'
                ))

        runner = Runner()
        result = workspace_helper.sync_guacamole(self._record(), runner=runner, require_new_connection=True)
        self.assertEqual(result["clone"]["status"], "ready")
        self.assertEqual(len(runner.calls), 1)
        self.assertNotIn("sync_snapshot", " ".join(runner.calls[0][0]).lower())

    def test_sync_validates_before_commit_and_never_snapshots_parameters(self):
        sql = workspace_helper.build_guacamole_sync_sql(self._record(), require_new_connection=True)
        self.assertNotIn("\\gset", sql)
        self.assertNotIn("sync_snapshot", sql.lower())
        self.assertIn("created_connections <> 1", sql)
        self.assertIn("permission = ANY", sql)
        self.assertNotIn("'parameter_name', parameter_name", sql)
        self.assertIn("COMMIT", sql)

        restore_sql = workspace_helper.build_guacamole_restore_sql(
            CloneRecord(
                name="vm-new", mac="52:54:00:20:00:01", ip="192.168.250.55",
                assigneeType="USER", assigneeName="demo", status="pending",
                syncAttemptId="11111111-2222-4333-8444-555555555555",
            ),
            17,
        )
        self.assertIn("_sync_owned", restore_sql)
        self.assertNotIn("jsonb_to_recordset", restore_sql)
        self.assertNotIn("sync_snapshot", restore_sql.lower())

    def test_compensation_is_idempotent_and_retries_after_failure(self):
        calls = []

        def runner(arguments, sql, *, timeout=None):
            calls.append((list(arguments), sql))
            if len(calls) == 1:
                raise RuntimeError("temporary database failure")
            return SimpleNamespace(stdout='{"compensated":true,"deleted":false,"ownership":"absent"}\n')

        callback = workspace_helper._guacamole_compensation(
            CloneRecord(
                name="vm-new", mac="52:54:00:20:00:01", ip="192.168.250.55",
                assigneeType="USER", assigneeName="demo", status="pending",
                syncAttemptId="11111111-2222-4333-8444-555555555555",
            ),
            {"connectionId": 17, "created": True},
            runner,
            5,
        )
        self.assertEqual(len(callback()), 1)
        self.assertEqual(callback(), [])
        self.assertEqual(len(callback()), 0)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("sync_snapshot", " ".join(calls[1][0]).lower())

    def test_compensation_parser_accepts_only_consistent_confirmation_states(self):
        valid = {
            ("owned", True, True),
            ("absent", True, False),
            ("mismatch", False, False),
        }
        for ownership in ("owned", "absent", "mismatch"):
            for compensated in (False, True):
                for deleted in (False, True):
                    output = json.dumps({
                        "compensated": compensated,
                        "deleted": deleted,
                        "ownership": ownership,
                    })
                    with self.subTest(ownership=ownership, compensated=compensated, deleted=deleted):
                        if (ownership, compensated, deleted) in valid:
                            self.assertEqual(
                                workspace_helper._parse_compensation_result(output),
                                {
                                    "compensated": compensated,
                                    "deleted": deleted,
                                    "ownership": ownership,
                                },
                            )
                        else:
                            with self.assertRaises(workspace_helper.HelperError) as context:
                                workspace_helper._parse_compensation_result(output)
                            self.assertEqual(context.exception.code, "SYNC_COMPENSATION_UNCONFIRMED")

        for output in (
            "{}",
            '{"compensated":true,"deleted":false}',
            '{"compensated":true,"deleted":false,"ownership":"mismatch","extra":"unknown"}',
            '{"compensated":"true","deleted":false,"ownership":"mismatch"}',
        ):
            with self.subTest(output=output):
                with self.assertRaises(workspace_helper.HelperError) as context:
                    workspace_helper._parse_compensation_result(output)
            self.assertEqual(context.exception.code, "SYNC_COMPENSATION_UNCONFIRMED")

    def test_compensation_callback_rejects_confirmed_mismatch(self):
        calls = []

        def runner(arguments, sql, *, timeout=None):
            calls.append(sql)
            return SimpleNamespace(stdout='{"compensated":true,"deleted":false,"ownership":"mismatch"}\n')

        callback = workspace_helper._guacamole_compensation(
            CloneRecord(
                name="vm-new", mac="52:54:00:20:00:01", ip="192.168.250.55",
                assigneeType="USER", assigneeName="demo", status="pending",
                syncAttemptId="11111111-2222-4333-8444-555555555555",
            ),
            {"connectionId": 17, "created": True},
            runner,
            5,
        )
        errors = callback()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "SYNC_COMPENSATION_UNCONFIRMED")
        self.assertEqual(len(calls), 1)

    def test_sync_never_serializes_secret_parameter_values(self):
        secret_values = ("alice-secret", "PRIVATE-KEY-MATERIAL", "gateway-secret")

        class Runner:
            def __init__(self):
                self.arguments = None
                self.sql = None

            def __call__(self, arguments, sql, *, timeout=None):
                self.arguments = list(arguments)
                self.sql = sql
                return SimpleNamespace(stdout=(
                    '{"connectionId":17,"created":true,"permissions":'
                    '{"assignee":["READ"],"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]}}\n'
                ))

        runner = Runner()
        result = workspace_helper.sync_guacamole(
            self._record(), runner=runner, require_new_connection=True,
        )
        payload = json.dumps(result)
        captured = " ".join(runner.arguments) + (runner.sql or "")
        for secret in secret_values:
            self.assertNotIn(secret, payload)
            self.assertNotIn(secret, captured)
        self.assertNotIn("sync_snapshot", captured.lower())

    def test_lost_commit_output_is_reconciled_without_rollback_snapshot(self):
        calls = []

        def runner(arguments, sql, *, timeout=None):
            calls.append((list(arguments), sql))
            if "WHERE connection.connection_name = :'sync_name'" in sql:
                return SimpleNamespace(stdout=(
                    '{"connectionId":17,"permissions":{"assignee":["READ"],'
                    '"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]},"created":true}\n'
                ))
            raise RuntimeError("transport closed after commit")

        holder = {}
        result = workspace_helper.sync_guacamole(
            self._record(), runner=runner, require_new_connection=True,
            compensation_holder=holder,
        )
        self.assertEqual(result["clone"]["status"], "ready")
        self.assertTrue(callable(holder.get("rollback")))
        self.assertEqual(len(calls), 2)
        self.assertNotIn("sync_snapshot", " ".join(calls[0][0]).lower())
        self.assertNotIn("sync_snapshot", calls[0][1].lower())

    def test_lost_result_with_preexisting_name_is_not_claimed_or_compensated(self):
        calls = []

        def runner(arguments, sql, *, timeout=None):
            calls.append((list(arguments), sql))
            if "FROM guacamole_connection AS connection" in sql:
                return SimpleNamespace(stdout=(
                    '{"connectionId":17,"permissions":{"assignee":["READ"],'
                    '"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"],"created":true}}\n'
                ))
            raise RuntimeError("transport closed after commit")

        with self.assertRaises(workspace_helper.HelperError) as context:
            workspace_helper.sync_guacamole(
                self._record(syncAttemptId="11111111-2222-4333-8444-555555555555"),
                runner=runner,
                require_new_connection=True,
                unknown_commit_on_failure=True,
            )
        self.assertEqual(context.exception.code, "SYNC_COMMIT_UNKNOWN")
        self.assertEqual(len(calls), 2)

    def test_all_clone_lost_result_persists_sync_failed_repair_state(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic({"templates": [], "clones": [self._record()]}, inventory_path)

            def runner(arguments, sql, *, timeout=None):
                if "FROM guacamole_connection AS connection" in sql:
                    return SimpleNamespace(stdout=(
                        '{"connectionId":99,"connectionName":"vm-new",'
                        '"permissions":{"assignee":["READ"],'
                        '"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]}}\n'
                    ))
                raise RuntimeError("result lost")

            with self.assertRaises(workspace_helper.HelperError) as context:
                workspace_helper.sync_guacamole(
                    all_clones=True,
                    inventory_path=inventory_path,
                    runner=runner,
                    require_new_connection=True,
                )
            self.assertEqual(context.exception.code, "SYNC_COMMIT_UNKNOWN")
            persisted = load_inventory(inventory_path)["clones"][0]
            self.assertEqual(persisted["status"], "sync-failed")
            self.assertEqual(persisted["errorCode"], "SYNC_COMMIT_UNKNOWN")
            self.assertNotIn("result lost", json.dumps(persisted))

    def test_compensation_retry_after_process_restart_accepts_confirmed_absence(self):
        calls = []

        def runner(arguments, sql, *, timeout=None):
            calls.append(sql)
            if len(calls) == 1:
                raise RuntimeError("connection result lost after delete")
            return SimpleNamespace(stdout=(
                '{"compensated":true,"deleted":false,"ownership":"absent"}\n'
            ))

        record = CloneRecord(
            name="vm-new", mac="52:54:00:20:00:01", ip="192.168.250.55",
            assigneeType="USER", assigneeName="demo", status="pending",
            syncAttemptId="11111111-2222-4333-8444-555555555555",
        )
        first = workspace_helper._guacamole_compensation(
            record, {"connectionId": 17, "created": True}, runner, 5,
        )
        self.assertEqual(len(first()), 1)
        second = workspace_helper._guacamole_compensation(
            record, {"connectionId": 17, "created": True}, runner, 5,
        )
        self.assertEqual(second(), [])
        self.assertEqual(len(calls), 2)

    def test_unknown_sync_result_fields_are_excluded_from_result(self):
        class Runner:
            def __call__(self, arguments, sql, *, timeout=None):
                return SimpleNamespace(stdout=(
                    '{"connectionId":17,"connectionName":"vm-new","created":true,'
                    '"password":"PRIVATE-KEY-MATERIAL","parameters":{"password":"gateway-secret"},'
                    '"unexpected":"alice-secret","permissions":{"assignee":["READ"],'
                    '"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]}}\n'
                ))

        result = workspace_helper.sync_guacamole(
            self._record(syncAttemptId="11111111-2222-4333-8444-555555555555"),
            runner=Runner(),
            require_new_connection=True,
        )
        serialized = json.dumps(result)
        for secret in ("PRIVATE-KEY-MATERIAL", "gateway-secret", "alice-secret"):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("password", result["clone"])
        self.assertNotIn("unexpected", result["clone"])

    def test_user_guacadmin_cannot_be_a_clone_assignee(self):
        with self.assertRaises(ValidationError):
            workspace_helper.build_guacamole_sync_sql(self._record(assigneeName="guacadmin"))

    def test_user_and_group_assignments_are_parameterized(self):
        for assignee_type in ("USER", "USER_GROUP"):
            with self.subTest(assignee_type=assignee_type):
                sql = workspace_helper.build_guacamole_sync_sql(
                    self._record(assigneeType=assignee_type, assigneeName="developers")
                )
                self.assertIn(":'sync_assignee_type'", sql)
                self.assertIn(":'sync_assignee_name'", sql)
                self.assertNotIn("developers", sql)

    def test_rerun_and_stale_hostname_repair_preserve_unrelated_permissions(self):
        sql = workspace_helper.build_guacamole_sync_sql(self._record())
        self.assertIn("ON CONFLICT (connection_id, parameter_name) DO UPDATE", sql)
        self.assertIn("ON CONFLICT (entity_id, connection_id, permission) DO NOTHING", sql)
        self.assertIn("parameter_name NOT IN ('hostname', 'port', 'security', 'ignore-cert')", sql)
        self.assertIn("org.apache.guacamole.workspace.sync.assignee-entity", sql)
        self.assertIn("entity_id = (SELECT entity_id FROM _sync_previous)", sql)
        self.assertIn("entity_id = (SELECT entity_id FROM _sync_admin)", sql)

    def test_duplicate_inventory_names_are_rejected_before_database_work(self):
        class Runner:
            def __call__(self, *args, **kwargs):
                raise AssertionError("duplicate inventory must fail before psql")

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic(
                {"templates": [], "clones": [self._record(), self._record(assigneeName="developers")]},
                inventory_path,
            )
            with self.assertRaises(ConflictError):
                workspace_helper.sync_guacamole(
                    all_clones=True,
                    inventory_path=inventory_path,
                    runner=Runner(),
                )

    def test_missing_inventory_assignee_is_admin_only_without_sql(self):
        result = workspace_helper.sync_guacamole(
            self._record(assigneeType=None, assigneeName=None),
            what_if=True,
        )
        self.assertEqual(result["clone"]["status"], "ADMIN_ONLY")
        self.assertIsNone(result["clone"]["connectionId"])

    def test_sync_passes_sql_on_stdin_and_values_as_psql_variables(self):
        class Runner:
            def __init__(self):
                self.arguments = None
                self.sql = None

            def __call__(self, arguments, sql, *, timeout=None):
                self.arguments = list(arguments)
                self.sql = sql
                return SimpleNamespace(stdout='{"connectionId":17,"permissions":{"assignee":["READ"],"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]}}\n')

        runner = Runner()
        result = workspace_helper.sync_guacamole(self._record(), runner=runner)
        self.assertEqual(result["clone"]["status"], "ready")
        self.assertEqual(result["clone"]["connectionId"], 17)
        psql_start = runner.arguments.index("psql")
        self.assertEqual(
            runner.arguments[psql_start : psql_start + 5],
            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1"],
        )
        self.assertIn("-v", runner.arguments)
        variable_args = set(runner.arguments[runner.arguments.index("-v") + 1 :])
        self.assertIn("ON_ERROR_STOP=1", variable_args)
        self.assertIn("sync_name=vm-new", variable_args)
        self.assertIn("sync_ip=192.168.250.55", variable_args)
        self.assertIn("sync_assignee_type=USER", variable_args)
        self.assertIn("sync_assignee_name=demo", variable_args)
        for forbidden in ("username", "password", "gateway", "token", "hash"):
            self.assertNotIn(forbidden, json.dumps(result).lower())
            self.assertNotIn(forbidden, " ".join(runner.arguments).lower())
            self.assertNotIn(forbidden, (runner.sql or "").lower())

    def test_sync_all_uses_inventory_only_and_dry_run_does_not_call_runner(self):
        class Runner:
            def __call__(self, *args, **kwargs):
                raise AssertionError("dry-run must not invoke psql")

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic(
                {"templates": [], "clones": [self._record(assigneeType=None, assigneeName=None)]},
                inventory_path,
            )
            result = workspace_helper.sync_guacamole(
                all_clones=True,
                inventory_path=inventory_path,
                what_if=True,
                runner=Runner(),
            )
        self.assertEqual(result["clones"][0]["status"], "ADMIN_ONLY")

    def test_assigned_dry_run_does_not_call_runner(self):
        class Runner:
            def __call__(self, *args, **kwargs):
                raise AssertionError("assigned dry-run must not invoke psql")

        result = workspace_helper.sync_guacamole(
            self._record(),
            what_if=True,
            runner=Runner(),
        )
        self.assertEqual(result["clone"]["status"], "ready")

    def test_rollback_result_is_not_ready_and_does_not_write_inventory(self):
        class Runner:
            def __call__(self, arguments, sql, *, timeout=None):
                return SimpleNamespace(stdout=(
                    '{"connectionId":17,"permissions":{"assignee":["READ"],'
                    '"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]}}\n'
                ))

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic({"templates": [], "clones": [self._record()]}, inventory_path)
            before = inventory_path.read_bytes()
            result = workspace_helper.sync_guacamole(
                all_clones=True,
                inventory_path=inventory_path,
                runner=Runner(),
                transaction_end="ROLLBACK",
            )
            self.assertEqual(result["clones"][0]["status"], "rolled-back")
            self.assertEqual(inventory_path.read_bytes(), before)

    def test_batch_persists_first_commit_before_second_failure(self):
        class Runner:
            def __init__(self):
                self.calls = 0

            def __call__(self, arguments, sql, *, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(stdout='{"connectionId":17,"permissions":{"assignee":["READ"],"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]}}\n')
                raise RuntimeError("second sync failed")

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            second = self._record(name="vm-second", ip="192.168.250.56", mac="52:54:00:20:00:02")
            save_inventory_atomic({"templates": [], "clones": [self._record(), second]}, inventory_path)
            with self.assertRaises(workspace_helper.HelperError):
                workspace_helper.sync_guacamole(
                    all_clones=True,
                    inventory_path=inventory_path,
                    runner=Runner(),
                )
            persisted = load_inventory(inventory_path)
            self.assertEqual(persisted["clones"][0]["status"], "ready")
            self.assertEqual(persisted["clones"][1]["status"], "sync-failed")

    def test_sync_failed_record_repairs_to_ready(self):
        class Runner:
            def __call__(self, arguments, sql, *, timeout=None):
                return SimpleNamespace(stdout=(
                    '{"connectionId":17,"permissions":{"assignee":["READ"],'
                    '"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]}}\n'
                ))

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = pathlib.Path(directory) / "inventory.json"
            save_inventory_atomic({"templates": [], "clones": [self._record(status="sync-failed")]}, inventory_path)
            result = workspace_helper.sync_guacamole(
                all_clones=True, inventory_path=inventory_path, runner=Runner()
            )
            self.assertEqual(result["clones"][0]["status"], "ready")
            self.assertEqual(load_inventory(inventory_path)["clones"][0]["status"], "ready")

    def test_sync_verifies_only_declared_domain_identity_and_dhcp(self):
        class SqlRunner:
            def __call__(self, arguments, sql, *, timeout=None):
                return SimpleNamespace(stdout='{"connectionId":18,"permissions":{"assignee":["READ"],"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]}}\n')

        class LibvirtRunner:
            def __call__(self, arguments, *, timeout=None):
                if "dominfo" in arguments:
                    return SimpleNamespace(stdout="Name: vm-new\nPersistent: yes\n")
                if "dumpxml" in arguments:
                    return SimpleNamespace(stdout=(
                        "<domain><name>vm-new</name><devices>"
                        "<interface><mac address='52:54:00:20:00:01'/>"
                        "<source network='guac-nat'/></interface></devices></domain>"
                    ))
                if "net-dhcp-leases" in arguments:
                    return SimpleNamespace(stdout="52:54:00:20:00:01 192.168.250.55\n")
                raise AssertionError(arguments)

        result = workspace_helper.sync_guacamole(
            self._record(),
            runner=SqlRunner(),
            libvirt_runner=LibvirtRunner(),
            verify_live=True,
        )
        self.assertEqual(result["clone"]["status"], "ready")

    def test_rdp_probe_surfaces_missing_docker_network(self):
        calls = []

        def runner(arguments):
            calls.append(arguments)
            raise subprocess.CalledProcessError(
                1,
                arguments,
                output="",
                stderr="network guacamole-local_default not found",
            )

        with self.assertRaises(workspace_helper.HelperError) as context:
            workspace_helper.wait_for_clone_rdp("192.168.250.21", runner=runner, timeout_seconds=5)
        self.assertEqual(context.exception.code, "COMMAND_FAILED")
        self.assertEqual(len(calls), 1)

    def test_rdp_probe_surfaces_exit_one_docker_failures(self):
        for diagnostic in (
            "pull access denied for busybox",
            "manifest unknown",
            "permission denied while connecting to the Docker daemon socket",
            "error response from daemon",
        ):
            calls = []

            def runner(arguments, diagnostic=diagnostic):
                calls.append(arguments)
                raise subprocess.CalledProcessError(1, arguments, output="", stderr=diagnostic)

            with self.subTest(diagnostic=diagnostic), self.assertRaises(workspace_helper.HelperError) as context:
                workspace_helper.wait_for_clone_rdp("192.168.250.21", runner=runner, timeout_seconds=5)
            self.assertEqual(context.exception.code, "COMMAND_FAILED")
            self.assertEqual(len(calls), 1)

    def test_rdp_probe_does_not_retry_silent_non_probe_exit_one(self):
        calls = []

        def runner(arguments):
            calls.append(arguments)
            raise subprocess.CalledProcessError(1, ["docker", "run", "--rm", "broken-image"])

        with self.assertRaises(workspace_helper.HelperError) as context:
            workspace_helper.wait_for_clone_rdp("192.168.250.21", runner=runner, timeout_seconds=5)
        self.assertEqual(context.exception.code, "COMMAND_FAILED")
        self.assertEqual(len(calls), 1)

    def test_rdp_probe_retries_silent_tcp_probe_exit_one(self):
        calls = []

        def runner(arguments):
            calls.append(arguments)
            raise subprocess.CalledProcessError(1, arguments, output="", stderr="")

        with self.assertRaises(workspace_helper.HelperError) as context:
            workspace_helper.wait_for_clone_rdp("192.168.250.21", runner=runner, timeout_seconds=0.01)
        self.assertEqual(context.exception.code, workspace_helper.RDP_NOT_READY)
        self.assertGreaterEqual(len(calls), 1)


class CloneAllocationTests(unittest.TestCase):
    def test_allocator_skips_reserved_leased_and_inventory_addresses(self):
        class ReadOnlyRunner:
            def __call__(self, arguments):
                if arguments[0] == "virsh" and "net-dumpxml" in arguments:
                    return SimpleNamespace(stdout=(
                        "<network><ip address='192.168.250.1'><dhcp>"
                        "<host mac='52:54:00:20:00:01' name='old' ip='192.168.250.20'/>"
                        "</dhcp></ip></network>"
                    ))
                if arguments[0] == "virsh" and "net-dhcp-leases" in arguments:
                    return SimpleNamespace(stdout=(
                        " Expiry Time           MAC address         Protocol   IP address   Hostname   Client ID or DUID\n"
                        " 2026-09-20 10:00:00   52:54:00:20:00:02   ipv4       192.168.250.21/24 old2      -\n"
                    ))
                raise AssertionError(arguments)

        inventory = {"templates": [], "clones": [{"name": "old3", "ip": "192.168.250.22", "mac": "52:54:00:20:00:03"}]}
        self.assertEqual(workspace_helper.allocate_clone_ip(inventory, ReadOnlyRunner()), "192.168.250.23")

    def test_clone_xml_has_new_identity_and_managed_tpm(self):
        source_uuid = "11111111-2222-4333-8444-555555555554"
        clone_uuid = "11111111-2222-4333-8444-555555555555"
        xml = workspace_helper.render_clone_xml(
            name="vm-new",
            clone_uuid=clone_uuid,
            mac="52:54:00:aa:bb:cc",
            disk_path="/var/lib/guacamole-vms/vm-new.qcow2",
            nvram_path="/var/lib/libvirt/qemu/nvram/vm-new_VARS.fd",
            memory_mib=4096,
            vcpus=2,
        )
        root = ET.fromstring(xml)
        self.assertEqual(root.findtext("uuid"), clone_uuid)
        self.assertNotEqual(root.findtext("uuid"), source_uuid)
        self.assertEqual(root.find("./os").get("firmware"), "efi")
        workspace_metadata = next(
            element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "workspace"
        )
        self.assertEqual(workspace_metadata.attrib["managed"], "true")
        self.assertEqual(workspace_metadata.attrib["schema"], workspace_helper.WORKSPACE_OWNERSHIP_SCHEMA)
        self.assertEqual(workspace_metadata.attrib["name"], "vm-new")
        self.assertEqual(workspace_metadata.attrib["template"], "windows11-v1")
        backend = root.find("./devices/tpm/backend")
        self.assertEqual(backend.attrib, {"type": "emulator", "version": "2.0"})
        self.assertEqual(root.find("./devices/interface/source").get("network"), "guac-nat")
        self.assertEqual(root.find("./os/type").get("machine"), "q35")
        self.assertEqual(root.find("./os/nvram").get("template"), "/usr/share/OVMF/OVMF_VARS_4M.ms.fd")
        self.assertEqual(root.findtext("./os/nvram"), "/var/lib/libvirt/qemu/nvram/vm-new_VARS.fd")
        self.assertNotIn("windows11_VARS", xml)
        self.assertNotIn("/run/guacamole-vm-windows11/swtpm.sock", xml)

    def test_rollback_ledger_runs_only_owned_entries_in_reverse_order(self):
        events = []
        ledger = workspace_helper.RollbackLedger()
        ledger.add("overlay", lambda: events.append("remove-overlay"))
        ledger.add("dhcp", lambda: events.append("remove-dhcp"))
        ledger.add("domain", lambda: events.append("undefine-domain"))
        ledger.rollback()
        self.assertEqual(events, ["undefine-domain", "remove-dhcp", "remove-overlay"])

    def test_rdp_timeout_retains_waiting_clone_record(self):
        self.assertTrue(hasattr(workspace_helper, "RDP_NOT_READY"))

    def test_clone_helper_rejects_out_of_bounds_and_non_finite_values(self):
        for memory in (0, 1048577):
            with self.subTest(memory=memory), self.assertRaises(ValidationError):
                workspace_helper.clone_workspace("vm-new", "USER", "demo", "windows11-v1", memory_mib=memory)
        for vcpus in (0, 257):
            with self.subTest(vcpus=vcpus), self.assertRaises(ValidationError):
                workspace_helper.clone_workspace("vm-new", "USER", "demo", "windows11-v1", vcpus=vcpus)
        for wait in (float("nan"), float("inf"), float("-inf"), -0.1, 1440.1, 10**1000, True):
            with self.subTest(wait=wait), self.assertRaises(ValidationError):
                workspace_helper.clone_workspace("vm-new", "USER", "demo", "windows11-v1", wait_rdp_minutes=wait)


class CloneTransactionTests(unittest.TestCase):
    def setUp(self):
        # Clone transaction tests use deterministic fake runners.  Keep the
        # production fail-closed secret lookup intact while supplying the
        # fixture credential that credential-enabled create flows require.
        credential_patcher = mock.patch.object(
            workspace_helper,
            "read_windows_credential_secret",
            return_value=workspace_helper.WindowsCredential(
                username="guacadmin",
                password="unit-test-secret",
            ),
        )
        credential_patcher.start()
        self.addCleanup(credential_patcher.stop)
        validation_patcher = mock.patch.object(workspace_helper, "_validate_sync_credentials")
        validation_patcher.start()
        self.addCleanup(validation_patcher.stop)

    class Runner:
        def __init__(
            self,
            root: pathlib.Path,
            *,
            fail_stage: str | None = None,
            lease_ready: bool = True,
            rdp_ready: bool = True,
            timeout_on_lease: bool = False,
            fail_cleanup: bool = False,
            fail_domstate: bool | str = False,
            fail_dominfo: bool | str = False,
        ):
            self.root = root
            self.fail_stage = fail_stage
            self.lease_ready = lease_ready
            self.rdp_ready = rdp_ready
            self.timeout_on_lease = timeout_on_lease
            self.fail_cleanup = fail_cleanup
            self.fail_domstate = fail_domstate
            self.fail_dominfo = fail_dominfo
            self.failed = False
            self.events: list[str] = []
            self.docker_argv: list[list[str]] = []
            self.commands: list[list[str]] = []
            self.live_hosts: list[tuple[str, str, str]] = [("old", "52:54:00:20:00:01", "192.168.250.20")]
            self.config_hosts: list[tuple[str, str, str]] = [("old", "52:54:00:20:00:01", "192.168.250.20")]
            self.domain_names = ""
            self.domain_xmls: dict[str, str] = {}
            self.defined = False
            self.started = False

        def _fail(self, stage: str):
            if self.fail_stage == stage and not self.failed:
                self.failed = True
                raise RuntimeError(f"{stage} failed")

        @staticmethod
        def _domain_failure(arguments, failure):
            if failure == "access-denied":
                raise subprocess.CalledProcessError(
                    1,
                    arguments,
                    output="",
                    stderr="error: failed to get domain 'vm-new': Permission denied",
                )
            if failure == "transport":
                raise subprocess.CalledProcessError(
                    1,
                    arguments,
                    output="",
                    stderr="error: failed to get domain 'vm-new': Connection refused",
                )
            raise RuntimeError("libvirt domain inspection transport failed")

        def __call__(self, arguments):
            self.commands.append(list(arguments))
            command = arguments[0]
            if command == "virsh" and "list" in arguments and "--name" in arguments:
                return SimpleNamespace(stdout=self.domain_names)
            if command == "virsh" and "dumpxml" in arguments:
                domain_name = arguments[arguments.index("dumpxml") + 1]
                xml_path = self.root / "vms" / f"{domain_name}.xml"
                if self.defined and xml_path.exists():
                    return SimpleNamespace(stdout=xml_path.read_text(encoding="utf-8"))
                if "--inactive" in arguments:
                    domain_name = f"{domain_name}:inactive"
                return SimpleNamespace(stdout=self.domain_xmls.get(domain_name, "<domain><uuid>aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa</uuid><devices/></domain>"))
            if command == "virsh" and "dominfo" in arguments:
                if self.fail_dominfo and self.defined:
                    self._domain_failure(arguments, self.fail_dominfo)
                domain_name = arguments[-1]
                if domain_name in self.domain_names.splitlines():
                    return SimpleNamespace(stdout=f"Name: {domain_name}\nState: running\nPersistent: yes\n")
                if self.defined:
                    return SimpleNamespace(stdout="Name: vm-new\nState: shut off\nPersistent: yes\n")
                raise subprocess.CalledProcessError(
                    1,
                    arguments,
                    output="",
                    stderr="error: Domain not found",
                )
            if command == "virsh" and "net-dumpxml" in arguments:
                self.events.append("net-dumpxml")
                hosts = self.config_hosts if "--inactive" in arguments else self.live_hosts
                rendered = "".join(
                    f"<host mac='{mac}' name='{name}' ip='{ip}'/>" for name, mac, ip in hosts
                )
                return SimpleNamespace(stdout=f"<network><ip address='192.168.250.1'><dhcp>{rendered}</dhcp></ip></network>")
            if command == "virsh" and "net-dhcp-leases" in arguments:
                self.events.append("net-dhcp-leases")
                if self.timeout_on_lease and self.started:
                    raise subprocess.TimeoutExpired(arguments, 0.01)
                if self.live_hosts and self.lease_ready:
                    name, mac, ip = self.live_hosts[-1]
                    return SimpleNamespace(stdout=(
                        "Expiry Time MAC address Protocol IP address Hostname Client ID or DUID\n"
                        f"never {mac} ipv4 {ip}/24 {name} -\n"
                    ))
                return SimpleNamespace(stdout="Expiry Time MAC address Protocol IP address Hostname Client ID or DUID\n")
            if command == "virsh" and "net-update" in arguments:
                action = "add" if "add" in arguments else "delete"
                self.events.append(f"dhcp-{action}")
                host = ET.fromstring(arguments[arguments.index("ip-dhcp-host") + 1])
                value = (host.get("name"), host.get("mac"), host.get("ip"))
                states = []
                if "--live" in arguments:
                    states.append(self.live_hosts)
                if "--config" in arguments:
                    states.append(self.config_hosts)
                for state in states:
                    if action == "add" and value not in state:
                        state.append(value)
                    if action == "delete":
                        if self.fail_cleanup:
                            raise RuntimeError("dhcp cleanup failed")
                        while value in state:
                            state.remove(value)
                self._fail("dhcp")
                return SimpleNamespace(stdout="")
            if command == "qemu-img" and "info" in arguments:
                return SimpleNamespace(stdout=json.dumps({"filename": str(self.root / "windows11-v1.qcow2")}))
            if command == "qemu-img" and "create" in arguments:
                self.events.append("overlay")
                pathlib.Path(arguments[-1]).write_bytes(b"overlay")
                self._fail("overlay")
                return SimpleNamespace(stdout="")
            if command == "install":
                self.events.append("nvram")
                pathlib.Path(arguments[-1]).write_bytes(b"nvram")
                os.chmod(arguments[-1], 0o660)
                return SimpleNamespace(stdout="")
            if command == "stat":
                return SimpleNamespace(stdout="libvirt-qemu:libvirt-qemu:660\n")
            if command == "chmod":
                os.chmod(arguments[-1], 0o644)
                return SimpleNamespace(stdout="")
            if command == "virsh" and "define" in arguments:
                self.events.append("define")
                self.defined = True
                self._fail("define")
                return SimpleNamespace(stdout="")
            if command == "virsh" and "start" in arguments:
                self.events.append("start")
                self.started = True
                self._fail("start")
                return SimpleNamespace(stdout="")
            if command == "virsh" and "destroy" in arguments:
                self.events.append("destroy")
                self.started = False
                return SimpleNamespace(stdout="")
            if command == "virsh" and "domstate" in arguments:
                if self.fail_domstate and self.defined:
                    self._domain_failure(arguments, self.fail_domstate)
                return SimpleNamespace(stdout="running\n" if self.started else "shut off\n")
            if command == "virsh" and "undefine" in arguments:
                self.events.append("undefine")
                self.defined = False
                return SimpleNamespace(stdout="")
            if command == "docker":
                self.events.append("rdp")
                self.docker_argv.append(list(arguments))
                if not self.rdp_ready:
                    raise subprocess.CalledProcessError(1, arguments, output="", stderr="Connection refused")
                return SimpleNamespace(stdout="")
            raise AssertionError(arguments)

    def _setup(self, directory: str):
        root = pathlib.Path(directory)
        templates_patcher = mock.patch.object(workspace_helper, "TEMPLATES_DIR", root)
        templates_patcher.start()
        self.addCleanup(templates_patcher.stop)
        template = root / "windows11-v1.qcow2"
        template.write_bytes(b"template")
        template.chmod(0o444)
        inventory_path = root / "inventory.json"
        save_inventory_atomic({"templates": [
            TemplateRecord(
                version="windows11-v1", sourceDomain="windows11", path=str(template),
                sha256=hashlib.sha256(b"template").hexdigest(), virtualSize=7, createdAt="now",
            )
        ], "clones": []}, inventory_path)
        return root, inventory_path

    def test_success_creates_overlay_domain_dhcp_and_pending_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root)
            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"):
                (root / "nvram").mkdir()
                (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                result = workspace_helper.clone_workspace(
                    "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                    inventory_path=inventory_path, wait_rdp_minutes=1,
                )
            self.assertEqual(result.status, "pending")
            self.assertTrue((root / "vms" / "vm-new.qcow2").exists())
            self.assertEqual(load_inventory(inventory_path)["clones"][0]["ip"], "192.168.250.21")
            self.assertRegex(
                load_inventory(inventory_path)["clones"][0]["syncAttemptId"],
                r"^[0-9a-f-]{36}$",
            )
            self.assertIn("start", runner.events)
            install_command = next(command for command in runner.commands if command[0] == "install")
            self.assertEqual(install_command[-1], str(root / "nvram" / "vm-new_VARS.fd"))
            self.assertEqual(install_command[1:7], ["-o", "libvirt-qemu", "-g", "libvirt-qemu", "-m", "660"])
            self.assertEqual((root / "nvram" / "vm-new_VARS.fd").stat().st_mode & 0o777, 0o660)

    def test_post_rdp_sync_failure_retains_current_clone_for_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root)

            def failing_sync(arguments, sql, *, timeout=None):
                raise workspace_helper.HelperError("database unavailable", code="COMMAND_FAILED", stage="sync")

            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"):
                (root / "nvram").mkdir()
                (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                with self.assertRaises(workspace_helper.HelperError) as context:
                    workspace_helper.clone_workspace_and_sync(
                        "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                        inventory_path=inventory_path, wait_rdp_minutes=1, sync_runner=failing_sync,
                    )
            self.assertEqual(context.exception.code, "SYNC_COMMIT_UNKNOWN")
            self.assertIn("retained for repair", str(context.exception))
            self.assertNotIn("destroy", runner.events)
            self.assertNotIn("undefine", runner.events)
            self.assertTrue((root / "vms" / "vm-new.qcow2").exists())
            self.assertTrue((root / "nvram" / "vm-new_VARS.fd").exists())
            self.assertEqual(load_inventory(inventory_path)["clones"][0]["status"], "sync-failed")

    def test_existing_guacamole_connection_is_rejected_before_clone_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root)
            preflight_calls = []

            def guacamole_preflight(arguments, sql, *, timeout=None):
                preflight_calls.append((list(arguments), sql))
                self.assertIn("SELECT count(*)", sql)
                self.assertNotIn("parameter_value", sql)
                return SimpleNamespace(stdout="1\n")

            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"):
                (root / "nvram").mkdir()
                (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                with self.assertRaises(ConflictError) as context:
                    workspace_helper.clone_workspace(
                        "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                        guacamole_preflight_runner=guacamole_preflight,
                        inventory_path=inventory_path, wait_rdp_minutes=1,
                    )
            self.assertEqual(context.exception.code, "GUAC_CONNECTION_CONFLICT")
            self.assertEqual(len(preflight_calls), 1)
            self.assertEqual(runner.events, [])
            self.assertFalse((root / "vms" / "vm-new.qcow2").exists())
            self.assertEqual(load_inventory(inventory_path)["clones"], [])


    def test_preexisting_name_race_with_lost_result_retains_vm_for_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root)
            sync_calls = []

            def guacamole_preflight(arguments, sql, *, timeout=None):
                self.assertIn("SELECT count(*)", sql)
                return SimpleNamespace(stdout="0\n")

            def lost_sync(arguments, sql, *, timeout=None):
                sync_calls.append(sql)
                if "FROM guacamole_connection AS connection" in sql:
                    return SimpleNamespace(stdout=(
                        '{"connectionId":99,"connectionName":"vm-new",'
                        '"permissions":{"assignee":["READ"],'
                        '"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]}}\n'
                    ))
                raise RuntimeError("transport closed after a conflicting connection committed")

            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"):
                (root / "nvram").mkdir()
                (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                with self.assertRaises(workspace_helper.HelperError) as context:
                    workspace_helper.clone_workspace_and_sync(
                        "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                        inventory_path=inventory_path, wait_rdp_minutes=1,
                        sync_runner=lost_sync, guacamole_preflight_runner=guacamole_preflight,
                    )

            self.assertEqual(context.exception.code, "SYNC_COMMIT_UNKNOWN")
            self.assertEqual(len(sync_calls), 2)
            self.assertNotIn("destroy", runner.events)
            self.assertNotIn("undefine", runner.events)
            self.assertTrue((root / "vms" / "vm-new.qcow2").exists())
            self.assertEqual(load_inventory(inventory_path)["clones"][0]["status"], "sync-failed")

    def test_same_name_row_without_attempt_marker_is_never_claimed_or_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root)
            sync_calls = []

            def guacamole_preflight(arguments, sql, *, timeout=None):
                return SimpleNamespace(stdout="0\n")

            def raced_sync(arguments, sql, *, timeout=None):
                sync_calls.append(sql)
                if "FROM guacamole_connection AS connection" in sql:
                    # The same-name row exists, but it has no workflow attempt marker.
                    self.assertIn("attempt.attribute_name = 'org.apache.guacamole.workspace.sync.attempt'", sql)
                    self.assertIn("attempt.attribute_value = :'sync_attempt_id'", sql)
                    return SimpleNamespace(stdout="")
                raise RuntimeError("transport closed after a pre-existing same-name row won the race")

            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"):
                (root / "nvram").mkdir()
                (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                with self.assertRaises(workspace_helper.HelperError) as context:
                    workspace_helper.clone_workspace_and_sync(
                        "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                        inventory_path=inventory_path, wait_rdp_minutes=1,
                        sync_runner=raced_sync, guacamole_preflight_runner=guacamole_preflight,
                    )

            self.assertEqual(context.exception.code, "SYNC_COMMIT_UNKNOWN")
            self.assertEqual(len(sync_calls), 2)
            self.assertFalse(any("GUACAMOLE_RESTORE_CREATED" in sql for sql in sync_calls))
            self.assertNotIn("destroy", runner.events)
            self.assertNotIn("undefine", runner.events)
            self.assertTrue((root / "vms" / "vm-new.qcow2").exists())
            self.assertEqual(load_inventory(inventory_path)["clones"][0]["status"], "sync-failed")

    def test_unknown_guacamole_commit_keeps_clone_for_repair_instead_of_orphaning_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root)

            def lost_connection(arguments, sql, *, timeout=None):
                raise RuntimeError("database transport closed")

            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"):
                (root / "nvram").mkdir()
                (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                with self.assertRaises(workspace_helper.HelperError) as context:
                    workspace_helper.clone_workspace_and_sync(
                        "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                        inventory_path=inventory_path, wait_rdp_minutes=1,
                        sync_runner=lost_connection, unknown_commit_on_failure=True,
                    )
            self.assertEqual(context.exception.code, "SYNC_COMMIT_UNKNOWN")
            self.assertIn("retained for repair", str(context.exception))
            self.assertNotIn("destroy", runner.events)
            self.assertTrue((root / "vms" / "vm-new.qcow2").exists())
            self.assertEqual(load_inventory(inventory_path)["clones"][0]["status"], "sync-failed")

    def test_final_inventory_failure_restores_guacamole_before_clone_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root)
            guacamole_calls = []

            def committed_sync(arguments, sql, *, timeout=None):
                runner.events.append("guac-restore" if "GUACAMOLE_RESTORE_CREATED" in sql else "guac-commit")
                guacamole_calls.append(sql)
                if "GUACAMOLE_RESTORE_CREATED" in sql:
                    return SimpleNamespace(stdout='{"compensated":true,"deleted":true,"ownership":"owned"}\n')
                return SimpleNamespace(stdout=(
                    '{"connectionId":17,"permissions":{"assignee":["READ"],'
                    '"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]},'
                    '"created":true}\n'
                ))

            original_save = workspace_helper.save_inventory_atomic
            save_calls = {"count": 0}

            def fail_final_inventory(inventory, target):
                save_calls["count"] += 1
                if save_calls["count"] == 2:
                    raise workspace_helper.InventoryError("final inventory is read-only", replaced=False)
                return original_save(inventory, target)

            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"), mock.patch.object(
                workspace_helper, "save_inventory_atomic", side_effect=fail_final_inventory
            ):
                (root / "nvram").mkdir()
                (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                with self.assertRaises(workspace_helper.HelperError) as context:
                    workspace_helper.clone_workspace_and_sync(
                        "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                        inventory_path=inventory_path, wait_rdp_minutes=1, sync_runner=committed_sync,
                    )

            self.assertEqual(context.exception.code, "INVENTORY_INVALID")
            self.assertIn("newly-created clone was rolled back", str(context.exception))
            self.assertTrue(any("GUACAMOLE_RESTORE_CREATED" in sql for sql in guacamole_calls))
            self.assertLess(runner.events.index("guac-restore"), runner.events.index("destroy"))
            self.assertIn("destroy", runner.events)
            self.assertFalse((root / "vms" / "vm-new.qcow2").exists())
            self.assertFalse((root / "nvram" / "vm-new_VARS.fd").exists())
            self.assertFalse(runner.defined)
            self.assertEqual(load_inventory(inventory_path)["clones"], [])

    def test_failed_compensation_preserves_vm_and_marks_sync_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root)
            guacamole_calls = []

            def committed_sync(arguments, sql, *, timeout=None):
                guacamole_calls.append(sql)
                if "GUACAMOLE_RESTORE_CREATED" in sql:
                    raise RuntimeError("database unavailable while confirming delete")
                return SimpleNamespace(stdout=(
                    '{"connectionId":17,"connectionName":"vm-new","created":true,'
                    '"permissions":{"assignee":["READ"],'
                    '"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]}}\n'
                ))

            original_save = workspace_helper.save_inventory_atomic
            save_calls = {"count": 0}

            def fail_final_inventory(inventory, target):
                save_calls["count"] += 1
                if save_calls["count"] == 2:
                    raise workspace_helper.InventoryError("final inventory is read-only", replaced=False)
                return original_save(inventory, target)

            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"), mock.patch.object(
                workspace_helper, "save_inventory_atomic", side_effect=fail_final_inventory
            ):
                (root / "nvram").mkdir()
                (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                with self.assertRaises(workspace_helper.HelperError) as context:
                    workspace_helper.clone_workspace_and_sync(
                        "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                        inventory_path=inventory_path, wait_rdp_minutes=1, sync_runner=committed_sync,
                    )

            self.assertEqual(context.exception.code, "ROLLBACK_FAILED")
            self.assertTrue(any("GUACAMOLE_RESTORE_CREATED" in sql for sql in guacamole_calls))
            self.assertNotIn("destroy", runner.events)
            self.assertTrue((root / "vms" / "vm-new.qcow2").exists())
            self.assertTrue((root / "nvram" / "vm-new_VARS.fd").exists())
            self.assertTrue(runner.defined)
            self.assertEqual(load_inventory(inventory_path)["clones"][0]["status"], "sync-failed")

    def test_contradictory_compensation_confirmation_preserves_vm_and_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root)
            guacamole_calls = []

            def committed_sync(arguments, sql, *, timeout=None):
                guacamole_calls.append(sql)
                if "GUACAMOLE_RESTORE_CREATED" in sql:
                    return SimpleNamespace(stdout='{"compensated":true,"deleted":false,"ownership":"mismatch"}\n')
                return SimpleNamespace(stdout=(
                    '{"connectionId":17,"connectionName":"vm-new","created":true,'
                    '"permissions":{"assignee":["READ"],'
                    '"guacadmin":["READ","UPDATE","DELETE","ADMINISTER"]}}\n'
                ))

            original_save = workspace_helper.save_inventory_atomic
            save_calls = {"count": 0}

            def fail_final_inventory(inventory, target):
                save_calls["count"] += 1
                if save_calls["count"] == 2:
                    raise workspace_helper.InventoryError("final inventory is read-only", replaced=False)
                return original_save(inventory, target)

            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"), mock.patch.object(
                workspace_helper, "save_inventory_atomic", side_effect=fail_final_inventory
            ):
                (root / "nvram").mkdir()
                (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                with self.assertRaises(workspace_helper.HelperError) as context:
                    workspace_helper.clone_workspace_and_sync(
                        "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                        inventory_path=inventory_path, wait_rdp_minutes=1, sync_runner=committed_sync,
                    )

            self.assertEqual(context.exception.code, "ROLLBACK_FAILED")
            self.assertTrue(any("GUACAMOLE_RESTORE_CREATED" in sql for sql in guacamole_calls))
            self.assertNotIn("destroy", runner.events)
            self.assertNotIn("undefine", runner.events)
            self.assertTrue((root / "vms" / "vm-new.qcow2").exists())
            self.assertTrue((root / "nvram" / "vm-new_VARS.fd").exists())
            self.assertTrue(runner.defined)
            self.assertEqual(load_inventory(inventory_path)["clones"][0]["status"], "sync-failed")


    def test_failed_publication_restores_previous_targets_and_pointer(self):
        installer_path = HELPER_PATH.parent / "libvirt.ps1"
        installer_source = installer_path.read_text(encoding="utf-8")
        match = re.search(r"\$installScript = @'\n(set -eu\npackage_source=.*?)\n'@", installer_source, re.S)
        self.assertIsNotNone(match)
        script = match.group(1)

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            package_source = root / "source-package"
            bundle_source = root / "source-bundle"
            tpm_source = root / "source-tpm"
            initializer_source = root / "initialize-windows-auth.ps1"
            package_source.mkdir()
            (bundle_source / "libvirt" / "domains").mkdir(parents=True)
            tpm_source.mkdir()
            for name, content in {
                "manifest.json": "{}\n",
                "index.html": "<link href='workspace-templates.css?v=source'><script src='workspace-templates.js?v=source'></script>\n",
                "workspace-templates.js": "console.log('new');\n",
                "workspace-templates.css": "body{}\n",
            }.items():
                (package_source / name).write_text(content, encoding="utf-8")
            (bundle_source / "workspace-helper.py").write_text(
                "# set-windows-credential /var/lib/guacamole-workspace/secrets/windows11_guacadmin_password\n",
                encoding="utf-8",
            )
            (bundle_source / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
            (bundle_source / "libvirt" / "domains" / "windows-clone.xml.template").write_text(
                "<domain/>\n", encoding="utf-8"
            )
            initializer_source.write_text(
                "& /usr/local/libexec/guacamole-workspace-helper set-windows-credential\n",
                encoding="utf-8",
            )

            target_root = root / "usr" / "local"
            initializer_source.write_text(
                f"& {target_root}/libexec/guacamole-workspace-helper set-windows-credential\n",
                encoding="utf-8",
            )
            package_target = target_root / "share" / "cockpit" / "workspace_templates"
            bundle_target = target_root / "libexec" / "guacamole-workspace"
            helper_target = target_root / "libexec" / "guacamole-workspace-helper"
            package_target.mkdir(parents=True)
            (package_target / "manifest.json").write_text("old-package\n", encoding="utf-8")
            (bundle_target / "libvirt" / "domains").mkdir(parents=True)
            (bundle_target / "compose.yaml").write_text("old-bundle\n", encoding="utf-8")
            helper_target.parent.mkdir(parents=True, exist_ok=True)
            helper_target.write_text("old-helper\n", encoding="utf-8")
            old_release = target_root / "libexec" / "guacamole-workspace-release.v1.old"
            old_release.mkdir(parents=True)
            current_link = target_root / "libexec" / "guacamole-workspace-release.current"
            current_link.symlink_to(old_release, target_is_directory=True)

            fake_bin = root / "bin"
            fake_bin.mkdir()

            replacements = {
                "__PACKAGE_SOURCE__": str(package_source),
                "__HELPER_SOURCE__": str(bundle_source / "workspace-helper.py"),
                "__COMPOSE_SOURCE__": str(bundle_source / "compose.yaml"),
                "__XML_SOURCE__": str(bundle_source / "libvirt" / "domains" / "windows-clone.xml.template"),
                "__TPM_STATE_SOURCE__": str(tpm_source),
                "__INITIALIZER_SOURCE__": str(initializer_source),
                "__VERSION__": "2",
            }
            for marker, value in replacements.items():
                script = script.replace(marker, value)
            script = script.replace("/usr/local", str(target_root))
            script = script.replace(
                "secret_root='/var/lib/guacamole-workspace/secrets'",
                f"secret_root='{root / 'runtime' / 'secrets'}'",
            )
            script = script.replace("/run/lock", str(root / "run" / "lock"))
            base_script = script
            failure_script = script.replace(
                'mv -Tf "$package_link" "$package_target"',
                'mv -Tf "$package_link" "$package_target"\nexit 97',
                1,
            )
            result = subprocess.run(
                ["sh", "-c", failure_script],
                env={"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 97, result.stderr + result.stdout)
            self.assertEqual((package_target / "manifest.json").read_text(encoding="utf-8"), "old-package\n")
            self.assertEqual((bundle_target / "compose.yaml").read_text(encoding="utf-8"), "old-bundle\n")
            self.assertEqual(helper_target.read_text(encoding="utf-8"), "old-helper\n")
            self.assertEqual(os.path.realpath(current_link), str(old_release))
            secret_root = root / "runtime" / "secrets"
            self.assertTrue(secret_root.is_dir())
            secret_stat = secret_root.stat()
            self.assertEqual((secret_stat.st_uid, secret_stat.st_gid), (0, 0))
            self.assertEqual(stat_module.S_IMODE(secret_stat.st_mode), 0o700)

            def publish_current() -> tuple[str, str]:
                published = subprocess.run(
                    ["sh", "-c", base_script],
                    env={"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(published.returncode, 0, published.stderr)
                selected_release = pathlib.Path(os.path.realpath(current_link))
                manifest = (selected_release / "RELEASE").read_text(encoding="utf-8")
                cache_match = re.search(r"^cacheKey=([0-9a-f]+)$", manifest, re.MULTILINE)
                self.assertIsNotNone(cache_match)
                cache_key = cache_match.group(1)
                index = (selected_release / "package" / "index.html").read_text(encoding="utf-8")
                self.assertIn(f"workspace-templates.css?v={cache_key}", index)
                self.assertIn(f"workspace-templates.js?v={cache_key}", index)
                return str(selected_release), cache_key

            first_release, first_cache_key = publish_current()
            (package_source / "workspace-templates.js").write_text(
                "console.log('changed');\n", encoding="utf-8"
            )
            second_release, second_cache_key = publish_current()
            self.assertNotEqual(second_release, first_release)
            self.assertNotEqual(second_cache_key, first_cache_key)

    def test_publication_backup_pointer_and_bridge_failures_preserve_old_release(self):
        installer_path = HELPER_PATH.parent / "libvirt.ps1"
        installer_source = installer_path.read_text(encoding="utf-8")
        match = re.search(r"\$installScript = @'\n(set -eu\npackage_source=.*?)\n'@", installer_source, re.S)
        self.assertIsNotNone(match)
        script_template = match.group(1)

        def prepare(root: pathlib.Path, *, current_regular: bool = False):
            package_source = root / "source-package"
            bundle_source = root / "source-bundle"
            tpm_source = root / "source-tpm"
            initializer_source = root / "initialize-windows-auth.ps1"
            package_source.mkdir()
            (bundle_source / "libvirt" / "domains").mkdir(parents=True)
            tpm_source.mkdir()
            for name, content in {
                "manifest.json": "{}\n",
                "index.html": "<link href='workspace-templates.css?v=source'><script src='workspace-templates.js?v=source'></script>\n",
                "workspace-templates.js": "console.log('new');\n",
                "workspace-templates.css": "body{}\n",
            }.items():
                (package_source / name).write_text(content, encoding="utf-8")
            (bundle_source / "workspace-helper.py").write_text(
                "# set-windows-credential /var/lib/guacamole-workspace/secrets/windows11_guacadmin_password\n",
                encoding="utf-8",
            )
            (bundle_source / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
            (bundle_source / "libvirt" / "domains" / "windows-clone.xml.template").write_text(
                "<domain/>\n", encoding="utf-8"
            )

            target_root = root / "usr" / "local"
            initializer_source.write_text(
                f"& {target_root}/libexec/guacamole-workspace-helper set-windows-credential\n",
                encoding="utf-8",
            )
            package_target = target_root / "share" / "cockpit" / "workspace_templates"
            bundle_target = target_root / "libexec" / "guacamole-workspace"
            helper_target = target_root / "libexec" / "guacamole-workspace-helper"
            package_target.mkdir(parents=True)
            (package_target / "manifest.json").write_text("old-package\n", encoding="utf-8")
            bundle_target.mkdir(parents=True)
            (bundle_target / "compose.yaml").write_text("old-bundle\n", encoding="utf-8")
            helper_target.parent.mkdir(parents=True, exist_ok=True)
            helper_target.write_text("old-helper\n", encoding="utf-8")
            old_release = target_root / "libexec" / "guacamole-workspace-release.v1.old"
            old_release.mkdir(parents=True)
            current_link = target_root / "libexec" / "guacamole-workspace-release.current"
            if current_regular:
                current_link.write_text("old-pointer\n", encoding="utf-8")
            else:
                current_link.symlink_to(old_release, target_is_directory=True)

            fake_bin = root / "bin"
            fake_bin.mkdir()
            mv = fake_bin / "mv"
            mv.write_text(
                "#!/bin/sh\n"
                "if [ -n \"${FAIL_TRACE:-}\" ]; then printf 'mv:%s\\n' \"$*\" >> \"$FAIL_TRACE\"; fi\n"
                "if [ \"$1\" = \"-T\" ] && [ \"$2\" = \"--\" ]; then source=\"$3\"; dest=\"$4\"; "
                "else source=\"$2\"; dest=\"$3\"; fi\n"
                "case \"${FAIL_PUBLISH_COMMAND:-}\" in\n"
                "  mv-backup) case \"$dest\" in *.legacy.*) exit 97;; esac;;\n"
                "  mv-swap) case \"$source\" in *.new.*) exit 97;; esac;;\n"
                "  mv-pointer-backup) case \"$dest\" in *release.current.legacy.*) exit 97;; esac;;\n"
                "  mv-pointer-swap) case \"$source\" in *release.current.new.*) exit 97;; esac;;\n"
                "esac\n"
                "exec /bin/mv \"$@\"\n",
                encoding="utf-8",
            )
            mv.chmod(0o755)
            readlink = fake_bin / "readlink"
            readlink.write_text(
                "#!/bin/sh\n"
                "if [ -n \"${FAIL_TRACE:-}\" ]; then printf 'readlink:%s\\n' \"$*\" >> \"$FAIL_TRACE\"; fi\n"
                "if [ \"${FAIL_PUBLISH_COMMAND:-}\" = \"readlink\" ]; then exit 97; fi\n"
                "exec /usr/bin/readlink \"$@\"\n",
                encoding="utf-8",
            )
            readlink.chmod(0o755)
            bridge = fake_bin / "cockpit-bridge"
            bridge.write_text(
                "#!/bin/sh\n"
                "if [ -n \"${FAIL_TRACE:-}\" ]; then printf 'bridge:%s\\n' \"$*\" >> \"$FAIL_TRACE\"; fi\n"
                "if [ \"${FAIL_PUBLISH_COMMAND:-}\" = \"bridge\" ]; then exit 97; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            bridge.chmod(0o755)
            replacements = {
                "__PACKAGE_SOURCE__": str(package_source),
                "__HELPER_SOURCE__": str(bundle_source / "workspace-helper.py"),
                "__COMPOSE_SOURCE__": str(bundle_source / "compose.yaml"),
                "__XML_SOURCE__": str(bundle_source / "libvirt" / "domains" / "windows-clone.xml.template"),
                "__TPM_STATE_SOURCE__": str(tpm_source),
                "__INITIALIZER_SOURCE__": str(initializer_source),
                "__VERSION__": "2",
            }
            script = script_template
            for marker, value in replacements.items():
                script = script.replace(marker, value)
            script = script.replace("/usr/local", str(target_root))
            script = script.replace(
                "secret_root='/var/lib/guacamole-workspace/secrets'",
                f"secret_root='{root / 'runtime' / 'secrets'}'",
            )
            script = script.replace("/run/lock", str(root / "run" / "lock"))
            return script, fake_bin, package_target, bundle_target, helper_target, current_link, old_release

        injections = {
            "actual package backup mv": "mv-backup",
            "actual package swap mv": "mv-swap",
            "actual current pointer backup mv": "mv-pointer-backup",
            "actual current pointer swap mv": "mv-pointer-swap",
            "actual readlink": "readlink",
            "actual cockpit bridge": "bridge",
        }
        for label, failure_mode in injections.items():
            with self.subTest(phase=label), tempfile.TemporaryDirectory() as directory:
                script, fake_bin, package_target, bundle_target, helper_target, current_link, old_release = prepare(
                    pathlib.Path(directory), current_regular=("pointer" in label and "backup" in label)
                )
                result = subprocess.run(
                    ["sh", "-c", script],
                    env={
                        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                        "FAIL_PUBLISH_COMMAND": failure_mode,
                        "FAIL_TRACE": str(pathlib.Path(directory) / "commands.log"),
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                trace = (pathlib.Path(directory) / "commands.log").read_text(encoding="utf-8") if (pathlib.Path(directory) / "commands.log").exists() else ""
                self.assertEqual(result.returncode, 97, result.stderr + "\n" + trace)
                self.assertEqual((package_target / "manifest.json").read_text(encoding="utf-8"), "old-package\n")
                self.assertEqual((bundle_target / "compose.yaml").read_text(encoding="utf-8"), "old-bundle\n")
                self.assertEqual(helper_target.read_text(encoding="utf-8"), "old-helper\n")
                if "current pointer backup" in label:
                    self.assertEqual(current_link.read_text(encoding="utf-8"), "old-pointer\n")
                else:
                    self.assertEqual(os.path.realpath(current_link), str(old_release))
                self.assertFalse(list(current_link.parent.glob("guacamole-workspace-release.v2.*")))

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            script, fake_bin, package_target, bundle_target, helper_target, current_link, old_release = prepare(root)
            (package_target / "manifest.json").unlink()
            package_target.rmdir()
            package_target.symlink_to(old_release, target_is_directory=True)
            injected = script.replace(
                'package_old_link="$(readlink "$package_target")"',
                'package_old_link="$(false)"',
                1,
            )
            result = subprocess.run(
                ["sh", "-c", injected],
                env={"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(package_target.is_symlink())
            self.assertEqual(os.path.realpath(package_target), str(old_release))
            self.assertEqual(os.path.realpath(current_link), str(old_release))

    def test_failures_roll_back_only_current_clone_resources(self):
        for stage in ("overlay", "dhcp", "define", "start"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root, inventory_path = self._setup(directory)
                runner = self.Runner(root, fail_stage=stage)
                with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                    workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
                ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"):
                    (root / "nvram").mkdir()
                    (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                    with self.assertRaises(workspace_helper.HelperError):
                        workspace_helper.clone_workspace(
                            "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                            inventory_path=inventory_path, wait_rdp_minutes=1,
                        )
                self.assertFalse((root / "vms" / "vm-new.qcow2").exists())
                self.assertFalse((root / "nvram" / "vm-new_VARS.fd").exists())
                self.assertFalse((root / "vms" / "vm-new.xml").exists())
                self.assertEqual(runner.live_hosts, [("old", "52:54:00:20:00:01", "192.168.250.20")])
                self.assertEqual(runner.config_hosts, [("old", "52:54:00:20:00:01", "192.168.250.20")])
                self.assertFalse(runner.defined)
                self.assertFalse(runner.started)
                self.assertEqual(load_inventory(inventory_path)["clones"], [])
                if stage == "start":
                    destroy_index = runner.events.index("destroy")
                    dhcp_delete_index = runner.events.index("dhcp-delete")
                    undefine_index = runner.events.index("undefine")
                    self.assertLess(destroy_index, dhcp_delete_index)
                    self.assertLess(dhcp_delete_index, undefine_index)

    def test_rdp_timeout_keeps_valid_clone_as_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root, lease_ready=True, rdp_ready=False)
            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"
            ), mock.patch.object(workspace_helper, "POLL_INTERVAL_SECONDS", 0):
                (root / "nvram").mkdir()
                (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                result = workspace_helper.clone_workspace(
                    "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                    inventory_path=inventory_path, wait_rdp_minutes=0.001,
                )
            self.assertEqual(result.status, "waiting-rdp")
            self.assertTrue((root / "vms" / "vm-new.qcow2").exists())
            self.assertEqual(load_inventory(inventory_path)["clones"][0]["status"], "waiting-rdp")
            self.assertEqual(runner.docker_argv[0][0:5], ["docker", "run", "--rm", "--network", "guacamole-local_default"])
            self.assertEqual(runner.docker_argv[0][-2:], ["192.168.250.21", "3389"])

    def test_lease_command_timeout_retains_booting_clone_and_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root, lease_ready=True, timeout_on_lease=True)
            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"), mock.patch.object(
                workspace_helper, "POLL_INTERVAL_SECONDS", 0
            ):
                (root / "nvram").mkdir()
                (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                result = workspace_helper.clone_workspace(
                    "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                    inventory_path=inventory_path, wait_rdp_minutes=0.001,
                )
            self.assertEqual(result.status, "waiting-rdp")
            self.assertTrue((root / "vms" / "vm-new.qcow2").exists())
            self.assertTrue((root / "nvram" / "vm-new_VARS.fd").exists())
            self.assertTrue(runner.defined)
            self.assertEqual(runner.live_hosts[-1][0], "vm-new")
            self.assertEqual(load_inventory(inventory_path)["clones"][0]["status"], "waiting-rdp")
            self.assertNotIn("destroy", runner.events)
            self.assertNotIn("undefine", runner.events)

    def test_domain_only_mac_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root)
            runner.domain_names = "domain-only\n"
            runner.domain_xmls["domain-only"] = (
                "<domain><uuid>bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb</uuid><devices>"
                "<interface><mac address='52:54:00:aa:bb:cc'/></interface>"
                "</devices></domain>"
            )
            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"), mock.patch.object(
                workspace_helper.secrets, "token_bytes", return_value=b"\xaa\xbb\xcc"
            ):
                (root / "nvram").mkdir()
                (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                with self.assertRaises(ConflictError) as failure:
                    workspace_helper.clone_workspace(
                        "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                        inventory_path=inventory_path, what_if=True,
                    )
            self.assertEqual(failure.exception.code, "MAC_EXHAUSTED")

    def test_running_persistent_domain_checks_inactive_mac_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root)
            runner.domain_names = "persistent\n"
            runner.domain_xmls["persistent"] = (
                "<domain><uuid>bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb</uuid><devices>"
                "<interface><mac address='52:54:00:aa:bb:cc'/></interface>"
                "</devices></domain>"
            )
            runner.domain_xmls["persistent:inactive"] = (
                "<domain><uuid>bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb</uuid><devices>"
                "<interface><mac address='52:54:00:dd:ee:ff'/></interface>"
                "</devices></domain>"
            )
            with mock.patch.object(workspace_helper.secrets, "token_bytes", return_value=b"\xdd\xee\xff"):
                with self.assertRaises(ConflictError) as failure:
                    workspace_helper.clone_workspace(
                        "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                        inventory_path=inventory_path, what_if=True,
                    )
            self.assertEqual(failure.exception.code, "MAC_EXHAUSTED")

    def test_stale_same_name_dhcp_reservation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root)
            runner.live_hosts.append(("vm-new", "52:54:00:aa:bb:cc", "192.168.250.33"))
            with self.assertRaises(ConflictError) as failure:
                workspace_helper.clone_workspace(
                    "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                    inventory_path=inventory_path, what_if=True,
                )
            self.assertEqual(failure.exception.code, "DHCP_CONFLICT")

    def test_dhcp_cleanup_failure_is_reported_after_primary_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root, fail_stage="start", fail_cleanup=True)
            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"):
                (root / "nvram").mkdir()
                (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                with self.assertRaises(workspace_helper.HelperError) as failure:
                    workspace_helper.clone_workspace(
                        "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                        inventory_path=inventory_path, wait_rdp_minutes=1,
                    )
            self.assertEqual(failure.exception.code, "ROLLBACK_FAILED")

    def test_domstate_transport_failure_is_reported_by_rollback(self):
        for failure_kind in ("access-denied", "transport"):
            with self.subTest(failure_kind=failure_kind), tempfile.TemporaryDirectory() as directory:
                root, inventory_path = self._setup(directory)
                runner = self.Runner(root, fail_stage="start", fail_domstate=failure_kind)
                with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                    workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
                ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"):
                    (root / "nvram").mkdir()
                    (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                    with self.assertRaises(workspace_helper.HelperError) as failure:
                        workspace_helper.clone_workspace(
                            "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                            inventory_path=inventory_path, wait_rdp_minutes=1,
                        )
                self.assertEqual(failure.exception.code, "ROLLBACK_FAILED")

    def test_domain_missing_suppression_requires_explicit_not_found(self):
        def runner_with(stderr):
            def runner(arguments):
                raise subprocess.CalledProcessError(1, arguments, output="", stderr=stderr)

            return runner

        self.assertFalse(
            workspace_helper._clone_domain_exists(
                "vm-new", runner_with("error: failed to get domain 'vm-new': Domain not found")
            )
        )
        with self.assertRaises(workspace_helper.HelperError):
            workspace_helper._clone_domain_exists(
                "vm-new", runner_with("error: failed to get domain 'vm-new': Permission denied")
            )

    def test_dominfo_transport_failure_is_reported_before_undefine(self):
        for failure_kind in ("access-denied", "transport"):
            with self.subTest(failure_kind=failure_kind), tempfile.TemporaryDirectory() as directory:
                root, inventory_path = self._setup(directory)
                runner = self.Runner(root, fail_stage="start", fail_dominfo=failure_kind)
                with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                    workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
                ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"):
                    (root / "nvram").mkdir()
                    (root / "OVMF_VARS.fd").write_bytes(b"nvram-template")
                    with self.assertRaises(workspace_helper.HelperError) as failure:
                        workspace_helper.clone_workspace(
                            "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                            inventory_path=inventory_path, wait_rdp_minutes=1,
                        )
                self.assertEqual(failure.exception.code, "ROLLBACK_FAILED")

    def test_clone_what_if_is_read_only_and_does_not_create_storage_or_lifecycle_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path = self._setup(directory)
            runner = self.Runner(root)
            with mock.patch.object(workspace_helper, "VMS_DIR", root / "vms"), mock.patch.object(
                workspace_helper, "CLONE_NVRAM_DIR", root / "nvram"
            ), mock.patch.object(workspace_helper, "CLONE_NVRAM_TEMPLATE_PATH", root / "OVMF_VARS.fd"):
                result = workspace_helper.clone_workspace(
                    "vm-new", "USER", "demo", "windows11-v1", runner=runner,
                    inventory_path=inventory_path, what_if=True,
                )
            self.assertIsNone(result)
            self.assertFalse((root / "vms").exists())
            self.assertFalse((root / "nvram").exists())
            self.assertFalse(any(event in runner.events for event in ("overlay", "nvram", "define", "dhcp-add", "start", "rdp")))
            self.assertEqual(load_inventory(inventory_path)["clones"], [])

class TemplateRetirementTests(unittest.TestCase):
    template_xml = """<domain><name>windows11</name><uuid>11111111-2222-4333-8444-555555555555</uuid><devices><disk><source file='/var/lib/guacamole-vm-windows11/windows11.qcow2'/></disk></devices></domain>"""
    network_xml = """<network><ip address='192.168.250.1'><dhcp><host name='windows11' mac='52:54:00:11:11:01' ip='192.168.250.11'/><host name='windows11-02' mac='52:54:00:42:a3:27' ip='192.168.250.12'/></dhcp></ip></network>"""

    class Runner:
        def __init__(self, template_path, *, qemu_output=None, guacamole_output="", domains="windows11\n", network_xml=None, domain_xmls=None):
            self.template_path = str(template_path)
            self.qemu_output = qemu_output
            self.guacamole_output = guacamole_output
            self.domains = domains
            self.network_xml = network_xml or TemplateRetirementTests.network_xml
            self.domain_xmls = domain_xmls or {}
            self.calls = []

        def __call__(self, arguments, timeout=None):
            self.calls.append(list(arguments))
            command = arguments[0]
            if command == "virsh" and "list" in arguments and "--name" in arguments:
                return SimpleNamespace(stdout=self.domains)
            if command == "virsh" and "dumpxml" in arguments:
                name = arguments[-1]
                return SimpleNamespace(stdout=self.domain_xmls.get(name, TemplateRetirementTests.template_xml))
            if command == "virsh" and "net-dumpxml" in arguments:
                return SimpleNamespace(stdout=self.network_xml)
            if command == "qemu-img" and "info" in arguments:
                if "--backing-chain" not in arguments:
                    return SimpleNamespace(stdout=json.dumps({"filename": self.template_path}))
                if self.qemu_output is None:
                    raise AssertionError("unexpected qemu-img backing inspection")
                return SimpleNamespace(stdout=self.qemu_output)
            if command == "docker" and "psql" in arguments:
                return SimpleNamespace(stdout=self.guacamole_output)
            raise AssertionError(arguments)

    def _setup(self, directory, *, clones=None):
        root = pathlib.Path(directory)
        templates_patcher = mock.patch.object(workspace_helper, "TEMPLATES_DIR", root)
        templates_patcher.start()
        self.addCleanup(templates_patcher.stop)
        template_path = root / "windows11-v1.qcow2"
        template_path.write_bytes(b"template")
        template_path.chmod(0o444)
        inventory_path = root / "inventory.json"
        save_inventory_atomic(
            {
                "templates": [{
                    "version": "windows11-v1",
                    "sourceDomain": "windows11",
                    "path": str(template_path),
                    "sha256": hashlib.sha256(b"template").hexdigest(),
                    "virtualSize": 100,
                    "createdAt": "2026-09-20T00:00:00Z",
                }],
                "clones": clones or [],
            },
            inventory_path,
        )
        return root, template_path, inventory_path

    def _patched_paths(self, root):
        return mock.patch.multiple(
            workspace_helper,
            TEMPLATES_DIR=root,
            VMS_DIR=root / "vms",
            CLONE_NVRAM_DIR=root / "nvram",
            CLONE_TPM_DIR=root / "swtpm",
        )

    def _add_template(self, inventory_path, root, version):
        image = root / f"{version}.qcow2"
        image.write_bytes(version.encode("ascii"))
        image.chmod(0o444)
        inventory = load_inventory(inventory_path)
        inventory["templates"].append({
            "version": version,
            "sourceDomain": "windows11",
            "path": str(image),
            "sha256": hashlib.sha256(version.encode("ascii")).hexdigest(),
            "virtualSize": 100,
            "createdAt": "2026-09-20T00:00:00Z",
        })
        save_inventory_atomic(inventory, inventory_path)

    def test_template_delete_blocks_inventory_dependent_clone_without_mutation(self):
        clone = {
            "name": "windows-template-test-01",
            "mac": "52:54:00:aa:bb:01",
            "ip": "192.168.250.20",
            "assigneeType": "USER",
            "assigneeName": "demo",
            "status": "ready",
            "templateVersion": "windows11-v1",
        }
        with tempfile.TemporaryDirectory() as directory:
            root, _template_path, inventory_path = self._setup(directory, clones=[clone])
            runner = self.Runner(root / "windows11-v1.qcow2")
            with self._patched_paths(root):
                result = workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "TEMPLATE_IN_USE")
            self.assertIn({"kind": "inventory", "name": "windows-template-test-01"}, result["blockers"])
            self.assertFalse(any("destroy" in call or "undefine" in call for call in runner.calls))

    def test_template_delete_blocks_orphan_overlay_and_guacamole_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root, template_path, inventory_path = self._setup(directory)
            overlay = root / "vms" / "windows-template-test-01.qcow2"
            overlay.parent.mkdir()
            overlay.write_bytes(b"overlay")
            qemu_output = json.dumps([
                {"filename": str(overlay)},
                {"filename": str(template_path)},
            ])
            runner = self.Runner(
                template_path,
                qemu_output=qemu_output,
                guacamole_output="13\twindows-template-test-01\trdp\t192.168.250.20\t\n",
            )
            with self._patched_paths(root):
                result = workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertFalse(result["ok"])
            kinds = {item["kind"] for item in result["blockers"]}
            self.assertIn("overlay", kinds)
            self.assertIn("guacamole", kinds)

    def test_template_delete_fails_closed_on_ambiguous_backing_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _template_path, inventory_path = self._setup(directory)
            overlay = root / "vms" / "windows-template-test-01.qcow2"
            overlay.parent.mkdir()
            overlay.write_bytes(b"overlay")
            runner = self.Runner(_template_path, qemu_output="not-json")
            with self._patched_paths(root), self.assertRaises(workspace_helper.HelperError) as failure:
                workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertEqual(failure.exception.code, "RETIRE_CHECK_FAILED")

    def test_template_delete_discovers_scoped_orphans_outside_inventory(self):
        orphan_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        network_xml = """<network><ip address='192.168.250.1'><dhcp><host name='windows11' mac='52:54:00:11:11:01' ip='192.168.250.11'/><host name='windows-template-test-01' mac='52:54:00:aa:bb:01' ip='192.168.250.20'/></dhcp></ip></network>"""
        with tempfile.TemporaryDirectory() as directory:
            root, template_path, inventory_path = self._setup(directory)
            (root / "nvram").mkdir()
            (root / "nvram" / "windows-template-test-01_VARS.fd").write_bytes(b"nvram")
            tpm = root / "swtpm" / orphan_uuid
            (tpm / "tpm2").mkdir(parents=True)
            workspace_helper._write_workspace_marker(
                tpm / workspace_helper.WORKSPACE_MARKER_FILENAME,
                workspace_helper._workspace_ownership_payload(
                    name="windows-template-test-02",
                    template_version="windows11-v1",
                    clone_uuid=orphan_uuid,
                    mac="52:54:00:aa:bb:02",
                    ip="192.168.250.22",
                    sync_attempt_id="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
                ),
            )
            runner = self.Runner(
                template_path,
                domains="windows11\nwindows-template-test-01\n",
                network_xml=network_xml,
                guacamole_output="13\twindows-template-test-01\trdp\t192.168.250.20\t11111111-2222-4333-8444-555555555555\n",
            )
            with self._patched_paths(root):
                result = workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertFalse(result["ok"])
            kinds = {item["kind"] for item in result["blockers"]}
            self.assertTrue({"domain", "nvram", "tpm-ownership", "dhcp-live", "dhcp-config", "guacamole-workflow"} <= kinds)
            self.assertFalse(any(any(token in call for token in ("destroy", "undefine", "rm", "unlink", "delete")) for call in runner.calls))

    def test_template_delete_blocks_arbitrary_name_residuals_without_inventory_domain_or_overlay(self):
        name = "windows-work-02"
        clone_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        attempt_id = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        mac = "52:54:00:aa:bb:02"
        ip = "192.168.250.20"
        network_xml = f"""<network><ip address='192.168.250.1'><dhcp><host name='windows11' mac='52:54:00:11:11:01' ip='192.168.250.11'/><host name='{name}' mac='{mac}' ip='{ip}'/></dhcp></ip></network>"""
        with tempfile.TemporaryDirectory() as directory:
            root, template_path, inventory_path = self._setup(directory)
            nvram = root / "nvram" / f"{name}_VARS.fd"
            nvram.parent.mkdir()
            nvram.write_bytes(b"residual-nvram")
            tpm = root / "swtpm" / clone_uuid
            (tpm / "tpm2").mkdir(parents=True)
            payload = workspace_helper._workspace_ownership_payload(
                name=name,
                template_version="windows11-v1",
                clone_uuid=clone_uuid,
                mac=mac,
                ip=ip,
                sync_attempt_id=attempt_id,
            )
            workspace_helper._write_workspace_marker(
                workspace_helper._workspace_nvram_marker_path(nvram), payload
            )
            workspace_helper._write_workspace_marker(tpm / workspace_helper.WORKSPACE_MARKER_FILENAME, payload)
            runner = self.Runner(
                template_path,
                network_xml=network_xml,
                guacamole_output=f"13\t{name}\trdp\t{ip}\t{attempt_id}\n",
            )
            with self._patched_paths(root):
                result = workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertFalse(result["ok"])
            kinds = {item["kind"] for item in result["blockers"]}
            self.assertTrue({"nvram-ownership", "tpm-ownership", "dhcp-live", "dhcp-config", "guacamole-workflow"} <= kinds)
            self.assertEqual(result["blockers"][0]["name"], name)

    def test_template_delete_allows_clean_version_and_has_no_mutating_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _template_path, inventory_path = self._setup(directory)
            runner = self.Runner(_template_path)
            with self._patched_paths(root):
                result = workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertEqual(result, {"ok": True, "version": "windows11-v1", "deletable": True, "blockers": []})
            self.assertFalse(any(any(token in call for token in ("destroy", "undefine", "rm", "unlink", "delete")) for call in runner.calls))

    def test_template_delete_fails_closed_when_template_file_is_missing_or_hash_mismatched(self):
        for mutation, expected_code in (("missing", "TEMPLATE_MISSING"), ("hash", "TEMPLATE_HASH_INVALID")):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root, template_path, inventory_path = self._setup(directory)
                if mutation == "missing":
                    template_path.unlink()
                else:
                    template_path.write_bytes(b"changed-template")
                runner = self.Runner(template_path)
                with self._patched_paths(root), self.assertRaises(workspace_helper.HelperError) as failure:
                    workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
                self.assertEqual(failure.exception.code, expected_code)
                self.assertFalse(any(any(token in call for token in ("destroy", "undefine", "rm", "unlink", "delete")) for call in runner.calls))

    def test_template_delete_fails_closed_on_malformed_ownership_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root, template_path, inventory_path = self._setup(directory)
            marker_root = root / "vms" / workspace_helper.WORKSPACE_OWNERSHIP_DIRNAME
            marker_root.mkdir(parents=True)
            (marker_root / "windows-work-02.json").write_text("{}\n", encoding="utf-8")
            runner = self.Runner(template_path)
            with self._patched_paths(root), self.assertRaises(workspace_helper.HelperError) as failure:
                workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertEqual(failure.exception.code, "RETIRE_CHECK_FAILED")

    def test_template_delete_ignores_foreign_template_dhcp_and_guacamole_owners(self):
        name = "windows-work-foreign"
        clone_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeee01"
        attempt_id = "bbbbbbbb-cccc-4ddd-8eee-fffffffffff1"
        mac = "52:54:00:aa:bb:21"
        ip = "192.168.250.21"
        network_xml = f"<network><ip address='192.168.250.1'><dhcp><host name='windows11' mac='52:54:00:11:11:01' ip='192.168.250.11'/><host name='{name}' mac='{mac}' ip='{ip}'/></dhcp></ip></network>"
        with tempfile.TemporaryDirectory() as directory:
            root, template_path, inventory_path = self._setup(directory)
            self._add_template(inventory_path, root, "windows11-v2")
            payload = workspace_helper._workspace_ownership_payload(
                name=name,
                template_version="windows11-v2",
                clone_uuid=clone_uuid,
                mac=mac,
                ip=ip,
                sync_attempt_id=attempt_id,
            )
            marker_root = root / "vms" / workspace_helper.WORKSPACE_OWNERSHIP_DIRNAME
            workspace_helper._write_workspace_marker(marker_root / f"{name}.json", payload)
            runner = self.Runner(
                template_path,
                network_xml=network_xml,
                guacamole_output=f"13\t{name}\trdp\t{ip}\t{attempt_id}\n",
            )
            with self._patched_paths(root):
                result = workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["blockers"], [])

    def test_template_delete_refuses_unowned_pool_dhcp_and_guacamole_state(self):
        name = "windows-work-02"
        guac_name = "windows-work-03"
        mac = "52:54:00:aa:bb:22"
        ip = "192.168.250.22"
        network_xml = f"<network><ip address='192.168.250.1'><dhcp><host name='windows11' mac='52:54:00:11:11:01' ip='192.168.250.11'/><host name='{name}' mac='{mac}' ip='{ip}'/></dhcp></ip></network>"
        with tempfile.TemporaryDirectory() as directory:
            root, template_path, inventory_path = self._setup(directory)
            runner = self.Runner(template_path, network_xml=network_xml, guacamole_output=f"13\t{guac_name}\trdp\t{ip}\t\n")
            with self._patched_paths(root):
                result = workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "TEMPLATE_RETIREMENT_AMBIGUOUS")
            self.assertGreaterEqual(sum(item["kind"] == "ownership-ambiguous" for item in result["blockers"]), 2)

    def test_template_delete_refuses_unmarked_orphan_tpm_and_arbitrary_nvram_subpath(self):
        orphan_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeee02"
        with tempfile.TemporaryDirectory() as directory:
            root, template_path, inventory_path = self._setup(directory)
            (root / "swtpm" / "nested" / orphan_uuid / "tpm2").mkdir(parents=True)
            (root / "nvram" / "nested").mkdir(parents=True)
            (root / "nvram" / "nested" / "windows-work-02_VARS.fd").write_bytes(b"orphan")
            runner = self.Runner(template_path)
            with self._patched_paths(root):
                result = workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "TEMPLATE_RETIREMENT_AMBIGUOUS")
            kinds = {item["kind"] for item in result["blockers"]}
            self.assertIn("ownership-ambiguous", kinds)
            self.assertIn(orphan_uuid, {item["name"] for item in result["blockers"]})

    def test_template_delete_rejects_corrupt_tpm_or_nvram_markers(self):
        orphan_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeee03"
        with tempfile.TemporaryDirectory() as directory:
            root, template_path, inventory_path = self._setup(directory)
            tpm = root / "swtpm" / orphan_uuid
            (tpm / "tpm2").mkdir(parents=True)
            (tpm / workspace_helper.WORKSPACE_MARKER_FILENAME).write_text("not-json\n", encoding="utf-8")
            runner = self.Runner(template_path)
            with self._patched_paths(root), self.assertRaises(workspace_helper.HelperError) as failure:
                workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertEqual(failure.exception.code, "RETIRE_CHECK_FAILED")

        with tempfile.TemporaryDirectory() as directory:
            root, template_path, inventory_path = self._setup(directory)
            nvram = root / "nvram" / "windows-work-02_VARS.fd"
            nvram.parent.mkdir(parents=True)
            nvram.write_bytes(b"orphan")
            (root / "nvram" / f"{nvram.name}{workspace_helper.WORKSPACE_NVRAM_MARKER_SUFFIX}").write_text("{}\n", encoding="utf-8")
            runner = self.Runner(template_path)
            with self._patched_paths(root), self.assertRaises(workspace_helper.HelperError) as failure:
                workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertEqual(failure.exception.code, "RETIRE_CHECK_FAILED")

    def test_template_delete_allows_clean_unrelated_domain_uuid_nvram_and_tpm(self):
        baseline_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeee04"
        with tempfile.TemporaryDirectory() as directory:
            root, template_path, inventory_path = self._setup(directory)
            nvram = root / "nvram" / "windows11-02_VARS.fd"
            nvram.parent.mkdir(parents=True)
            nvram.write_bytes(b"baseline")
            (root / "swtpm" / "nested" / baseline_uuid / "tpm2").mkdir(parents=True)
            baseline_xml = f"<domain><name>windows11-02</name><uuid>{baseline_uuid}</uuid><os><nvram>{nvram}</nvram></os><devices><disk><source file='/var/lib/baseline/windows11-02.qcow2'/></disk></devices></domain>"
            runner = self.Runner(
                template_path,
                domains="windows11\nwindows11-02\n",
                domain_xmls={"windows11-02": baseline_xml},
            )
            with self._patched_paths(root):
                result = workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["blockers"], [])

    def test_template_delete_rejects_unexpected_ownership_directory_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root, template_path, inventory_path = self._setup(directory)
            marker_root = root / "vms" / workspace_helper.WORKSPACE_OWNERSHIP_DIRNAME
            marker_root.mkdir(parents=True)
            (marker_root / "stale.tmp").write_text("stale\n", encoding="utf-8")
            runner = self.Runner(template_path)
            with self._patched_paths(root), self.assertRaises(workspace_helper.HelperError) as failure:
                workspace_helper.check_template_delete("windows11-v1", runner=runner, inventory_path=inventory_path)
            self.assertEqual(failure.exception.code, "RETIRE_CHECK_FAILED")


if __name__ == "__main__":
    unittest.main()
