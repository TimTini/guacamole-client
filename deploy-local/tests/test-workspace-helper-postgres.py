"""Disposable PostgreSQL contract test for managed Guacamole credentials.

Run from WSL with Docker available.  The Compose project uses tmpfs for the
database and is always removed in ``finally``; it never connects to the local
Guacamole Compose project or its connection 14.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import tempfile
import time
import uuid


ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "workspace-helper.py"
SCHEMA_PATH = ROOT / "data" / "postgres-init" / "001-guacamole.sql"
SPEC = importlib.util.spec_from_file_location("workspace_helper", HELPER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load helper from {HELPER_PATH}")
workspace_helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workspace_helper)


PROJECT = f"guac-credential-test-{uuid.uuid4().hex[:12]}"
OLD_SECRET = "old\"password\\value"
NEW_SECRET = "new\"password\\value"


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_PATH), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
    )


def assert_disposable_cleanup() -> None:
    for command in (
        ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}", "--format", "{{.ID}}"],
        ["docker", "volume", "ls", "--filter", f"label=com.docker.compose.project={PROJECT}", "--format", "{{.Name}}"],
    ):
        completed = subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        assert completed.stdout.strip() == "", completed.stdout


def query(sql: str) -> str:
    # The SQL is supplied separately so shell quoting cannot alter it.
    try:
        completed = subprocess.run(
            [
                "docker", "compose", "-f", str(COMPOSE_PATH), "exec", "-T",
                "postgres", "psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1",
                "-U", "guacamole_user", "-d", "guacamole_db",
            ],
            input=sql,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"disposable PostgreSQL query failed: {exc.stderr}") from exc
    return completed.stdout.strip()


def wait_for_postgres() -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        completed = compose(
            "exec", "-T", "postgres", "pg_isready", "-U", "guacamole_user", "-d", "guacamole_db",
            check=False,
        )
        if completed.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("disposable PostgreSQL did not become ready")


def seed_database() -> None:
    query(
        """
INSERT INTO guacamole_entity (entity_id, name, type)
VALUES (2, 'demo', 'USER');
INSERT INTO guacamole_connection (connection_id, connection_name, parent_id, protocol)
VALUES
    (14, 'wtest', NULL, 'rdp'),
    (41, 'adopt-escape', NULL, 'rdp');
SELECT setval('guacamole_entity_entity_id_seq', 2, true);
SELECT setval('guacamole_connection_connection_id_seq', 41, true);
INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
VALUES
    (14, 'hostname', '192.168.250.14'), (14, 'port', '3389'),
    (14, 'security', 'any'), (14, 'ignore-cert', 'true'),
    (41, 'hostname', '192.168.250.41'), (41, 'port', '3389'),
    (41, 'security', 'any'), (41, 'ignore-cert', 'true'),
    (41, 'username', 'old-user'), (41, 'password', 'old"password\\value');
INSERT INTO guacamole_connection_attribute (connection_id, attribute_name, attribute_value)
VALUES (41, 'org.apache.guacamole.workspace.sync.assignee-entity', '2');
INSERT INTO guacamole_connection_permission (entity_id, connection_id, permission)
VALUES
    (2, 41, 'READ'),
    (1, 41, 'READ'), (1, 41, 'UPDATE'), (1, 41, 'DELETE'), (1, 41, 'ADMINISTER');
"""
    )


def connection14_fingerprint() -> str:
    return query(
        """
SELECT md5(COALESCE(string_agg(value, E'\\n' ORDER BY value), ''))
FROM (
    SELECT connection_name || '|' || COALESCE(parent_id::text, '') || '|' || protocol AS value
    FROM guacamole_connection WHERE connection_id = 14
    UNION ALL
    SELECT parameter_name || '|' || parameter_value
    FROM guacamole_connection_parameter WHERE connection_id = 14
    UNION ALL
    SELECT attribute_name || '|' || attribute_value
    FROM guacamole_connection_attribute WHERE connection_id = 14
    UNION ALL
    SELECT entity_id::text || '|' || permission::text
    FROM guacamole_connection_permission WHERE connection_id = 14
) AS values;
"""
    )


def assert_connection_contract(connection_id: int, attempt_id: str) -> None:
    assert query(
        f"""
SELECT count(*)::text || '|' || string_agg(parameter_name, ',' ORDER BY parameter_name)
FROM guacamole_connection_parameter
WHERE connection_id = {connection_id};
"""
    ) == "6|hostname,ignore-cert,password,port,security,username"
    assert query(
        f"""
SELECT (count(*) FILTER (WHERE parameter_name IN ('username', 'password')) = 2
    AND bool_and(parameter_value <> '') FILTER (WHERE parameter_name IN ('username', 'password')))
FROM guacamole_connection_parameter
WHERE connection_id = {connection_id};
"""
    ) == "t"
    assert query(
        f"""
SELECT count(*) = 1
FROM guacamole_connection_attribute
WHERE connection_id = {connection_id}
  AND attribute_name = 'org.apache.guacamole.workspace.sync.attempt'
  AND attribute_value = '{attempt_id}';
"""
    ) == "t"
    assert query(
        f"""
SELECT count(*) = 1
FROM guacamole_connection_attribute
WHERE connection_id = {connection_id}
  AND attribute_name = 'org.apache.guacamole.workspace.sync.assignee-entity'
  AND attribute_value = '2';
"""
    ) == "t"
    assert query(
        f"""
SELECT COALESCE(string_agg(entity_id::text || ':' || permission::text, ',' ORDER BY entity_id, permission::text), '')
    = '1:ADMINISTER,1:DELETE,1:READ,1:UPDATE,2:READ'
FROM guacamole_connection_permission
WHERE connection_id = {connection_id};
"""
    ) == "t"


def assert_secret_replaced(connection_id: int, expected: str, old: str) -> None:
    expected_literal = "$credential$" + expected + "$credential$"
    old_literal = "$credential$" + old + "$credential$"
    assert query(
        f"""
SELECT count(*) FILTER (WHERE parameter_name = 'password') = 1
   AND count(*) FILTER (WHERE parameter_name = 'password' AND parameter_value = {expected_literal}) = 1
   AND count(*) FILTER (WHERE parameter_name = 'password' AND parameter_value = {old_literal}) = 0
FROM guacamole_connection_parameter
WHERE connection_id = {connection_id};
"""
    ) == "t"


def main() -> None:
    global COMPOSE_PATH
    with tempfile.TemporaryDirectory(prefix="guac-credential-test-") as directory:
        COMPOSE_PATH = pathlib.Path(directory) / "compose.yaml"
        COMPOSE_PATH.write_text(
            """name: %s
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: guacamole_db
      POSTGRES_USER: guacamole_user
      POSTGRES_PASSWORD: disposable-only
    tmpfs:
      - /var/lib/postgresql/data
    volumes:
      - %s:/docker-entrypoint-initdb.d/001-guacamole.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U guacamole_user -d guacamole_db"]
      interval: 1s
      timeout: 2s
      retries: 90
""" % (PROJECT, SCHEMA_PATH.as_posix()),
            encoding="utf-8",
        )
        secret_path = pathlib.Path(directory) / "windows11_guacadmin_password"
        workspace_helper.COMPOSE_FILE = COMPOSE_PATH
        workspace_helper.WINDOWS_CREDENTIAL_SECRET_PATH = secret_path
        try:
            compose("up", "-d", "--wait", "postgres")
            wait_for_postgres()
            seed_database()
            before_14 = connection14_fingerprint()
            workspace_helper.write_windows_credential_secret(NEW_SECRET, secret_path)

            create_record = workspace_helper.CloneRecord(
                name="create-escape", mac="52:54:00:20:00:81", ip="192.168.250.81",
                assigneeType="USER", assigneeName="demo", syncAttemptId=str(uuid.uuid4()),
            )
            captured: dict[str, object] = {}
            real_run = workspace_helper.subprocess.run

            def capture_run(*args, **kwargs):
                captured["argv"] = list(args[0])
                captured["input"] = kwargs.get("input", "")
                captured.setdefault("inputs", []).append(str(kwargs.get("input", "")))
                captured.setdefault("argvs", []).append(list(args[0]))
                try:
                    return real_run(*args, **kwargs)
                except subprocess.CalledProcessError as exc:
                    raise RuntimeError(f"disposable PostgreSQL sync failed: {exc.stderr}") from exc

            workspace_helper.subprocess.run = capture_run
            try:
                created = workspace_helper.sync_guacamole(
                    create_record, runner=workspace_helper.run_psql_sync,
                    require_new_connection=True, include_windows_credentials=True,
                )
            finally:
                workspace_helper.subprocess.run = real_run
            assert created["clone"]["status"] == "ready"
            assert json.dumps(created).find(NEW_SECRET) < 0
            escaped_new_secret = '"' + NEW_SECRET.replace('"', '""') + '"'
            assert escaped_new_secret in str(captured["input"])
            assert NEW_SECRET not in str(captured["argv"])
            assert escaped_new_secret not in str(captured["input"]).split("COPY _sync_secure_values", 1)[0]
            created_id = int(query("SELECT connection_id FROM guacamole_connection WHERE connection_name = 'create-escape';"))
            assert_connection_contract(created_id, create_record.syncAttemptId)
            assert_secret_replaced(created_id, NEW_SECRET, OLD_SECRET)
            assert query("SELECT count(*) FROM guacamole_connection WHERE connection_name = 'create-escape';") == "1"

            rollback_record = workspace_helper.CloneRecord(
                name="rollback-escape", mac="52:54:00:20:00:82", ip="192.168.250.82",
                assigneeType="USER", assigneeName="demo", syncAttemptId=str(uuid.uuid4()),
            )
            rolled_back = workspace_helper.sync_guacamole(
                rollback_record, runner=workspace_helper.run_psql_sync,
                transaction_end="ROLLBACK", require_new_connection=True,
                include_windows_credentials=True,
            )
            assert rolled_back["clone"]["status"] == "rolled-back"
            assert query("SELECT count(*) FROM guacamole_connection WHERE connection_name = 'rollback-escape';") == "0"

            conflict_record = workspace_helper.CloneRecord(
                name="create-escape", mac="52:54:00:20:00:83", ip="192.168.250.83",
                assigneeType="USER", assigneeName="demo", syncAttemptId=str(uuid.uuid4()),
            )
            try:
                workspace_helper.sync_guacamole(
                    conflict_record, runner=workspace_helper.run_psql_sync,
                    require_new_connection=True, include_windows_credentials=True,
                )
            except workspace_helper.HelperError:
                pass
            else:
                raise AssertionError("new connection conflict was accepted")
            assert query("SELECT count(*) FROM guacamole_connection WHERE connection_name = 'create-escape';") == "1"

            adopt_record = workspace_helper.CloneRecord(
                name="adopt-escape", mac="52:54:00:20:00:84", ip="192.168.250.41",
                assigneeType="USER", assigneeName="demo", syncAttemptId=str(uuid.uuid4()),
            )
            captured["inputs"] = []
            captured["argvs"] = []
            real_run = workspace_helper.subprocess.run
            workspace_helper.subprocess.run = capture_run
            try:
                adopted = workspace_helper.sync_guacamole(
                    adopt_record, runner=workspace_helper.run_psql_sync,
                    connection_id=41, adopt_existing_connection=True,
                    include_windows_credentials=True,
                )
            finally:
                workspace_helper.subprocess.run = real_run
            assert adopted["clone"]["status"] == "ready"
            assert adopted["clone"]["connectionId"] == 41
            assert_connection_contract(41, adopt_record.syncAttemptId)
            assert_secret_replaced(41, NEW_SECRET, OLD_SECRET)
            adoption_sqls = [str(item) for item in captured["inputs"]]
            adoption_argvs = [" ".join(str(item) for item in argv) for argv in captured["argvs"]]
            adoption_sql = next(sql for sql in adoption_sqls if "COPY _sync_secure_values" in sql)
            adoption_argv = " ".join(adoption_argvs)
            assert escaped_new_secret in adoption_sql
            assert OLD_SECRET.replace('"', '""') not in adoption_sql
            assert NEW_SECRET not in adoption_argv and OLD_SECRET not in adoption_argv

            repair_record = workspace_helper.CloneRecord(
                name="adopt-escape", mac="52:54:00:20:00:84", ip="192.168.250.41",
                assigneeType="USER", assigneeName="demo", syncAttemptId=adopt_record.syncAttemptId,
            )
            captured["inputs"] = []
            captured["argvs"] = []
            real_run = workspace_helper.subprocess.run
            workspace_helper.subprocess.run = capture_run
            try:
                repaired = workspace_helper.sync_guacamole(
                    repair_record, runner=workspace_helper.run_psql_sync,
                    connection_id=41, require_owned_connection=True,
                    include_windows_credentials=True,
                )
            finally:
                workspace_helper.subprocess.run = real_run
            assert repaired["clone"]["status"] == "ready"
            assert repaired["clone"]["connectionId"] == 41
            assert_connection_contract(41, repair_record.syncAttemptId)
            assert_secret_replaced(41, NEW_SECRET, OLD_SECRET)
            repair_sqls = [str(item) for item in captured["inputs"]]
            repair_argvs = [" ".join(str(item) for item in argv) for argv in captured["argvs"]]
            repair_sql = next(sql for sql in repair_sqls if "COPY _sync_secure_values" in sql)
            assert escaped_new_secret in repair_sql
            assert all(NEW_SECRET not in argv and OLD_SECRET not in argv for argv in repair_argvs)
            assert OLD_SECRET not in json.dumps(adopted)
            assert connection14_fingerprint() == before_14
        finally:
            cleanup = compose("down", "-v", "--remove-orphans", check=False)
            assert cleanup.returncode == 0, cleanup.stderr
            assert_disposable_cleanup()


if __name__ == "__main__":
    main()
    print("POSTGRES_CREDENTIAL_INTEGRATION_PASS")
