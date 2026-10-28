"""Regression checks for commands executed inside the deployment container."""

import unittest
import re
from pathlib import Path
from unittest.mock import patch

from tools.migrate_to_aws import _configure_application_role


ROOT = Path(__file__).resolve().parents[1]


class DeploymentCommandTests(unittest.TestCase):
    def test_deployment_scripts_only_request_declared_stack_outputs(self):
        template = (ROOT / "deployment" / "cloudformation.yaml").read_text()
        outputs = template.split("\nOutputs:\n", 1)[1]
        declared = set(re.findall(r"^  ([A-Za-z0-9]+):", outputs, re.MULTILINE))
        scripts = "\n".join(
            (ROOT / "deployment" / name).read_text()
            for name in ("deploy.sh", "approve-review.sh", "start-tunnel.sh")
        )
        requested = set(re.findall(r"\$\(output ([A-Za-z0-9]+)\)", scripts))

        self.assertEqual(requested - declared, set())

    def test_container_tools_run_as_modules(self):
        deploy = (ROOT / "deployment" / "deploy.sh").read_text()
        approval = (ROOT / "deployment" / "approve-review.sh").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()

        self.assertNotIn("python tools/", deploy)
        self.assertNotIn("python tools/", approval)
        self.assertIn("aws ecs run-task", deploy)
        self.assertIn("aws ecs wait services-stable", deploy)
        self.assertIn("python -m tools.aws_runtime approve", approval)
        self.assertIn('"-m", "tools.runtime"', compose)
        self.assertIn('"--host", "0.0.0.0"', compose)

    def test_tunnel_uses_direct_port_forwarding_for_the_target_container(self):
        tunnel = (ROOT / "deployment" / "start-tunnel.sh").read_text()

        self.assertIn(
            "--document-name AWS-StartPortForwardingSession \\",
            tunnel,
        )
        self.assertNotIn("AWS-StartPortForwardingSessionToRemoteHost", tunnel)
        self.assertNotIn('\\"host\\"', tunnel)

    def test_database_role_password_is_safely_composed_for_postgres_ddl(self):
        class Result:
            def fetchone(self):
                return (1,)

        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, query, parameters=None):
                rendered = query.as_string(None) if hasattr(query, "as_string") else query
                self.calls.append((rendered, parameters))
                return Result()

        connection = Connection()
        secrets = {
            "username": "fnol_app",
            "password": "quoted'password",
        }
        with patch("tools.migrate_to_aws.boto3.client") as client:
            client.return_value.get_secret_value.return_value = {
                "SecretString": __import__("json").dumps(secrets)
            }
            _configure_application_role(connection, "secret-arn")

        alter_role = next(call for call in connection.calls if "ALTER ROLE" in call[0])
        self.assertEqual(
            alter_role,
            ('ALTER ROLE "fnol_app" PASSWORD \'quoted\'\'password\'', None),
        )

    def test_database_role_rejects_non_allowlisted_username(self):
        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, query, parameters=None):
                self.calls.append((query, parameters))
                raise AssertionError("execute must not run for a rejected username")

        connection = Connection()
        secrets = {
            "username": "evil;drop",
            "password": "irrelevant",
        }
        with patch("tools.migrate_to_aws.boto3.client") as client:
            client.return_value.get_secret_value.return_value = {
                "SecretString": __import__("json").dumps(secrets)
            }
            with self.assertRaises(ValueError) as raised:
                _configure_application_role(connection, "secret-arn")

        self.assertIn("fnol_app", str(raised.exception))
        self.assertEqual(connection.calls, [])


if __name__ == "__main__":
    unittest.main()
