from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from apartment_notifier.launchd import LABEL, launch_agent_definition


class LaunchdTests(unittest.TestCase):
    def test_definition_uses_supplied_project_python_config_and_logs(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "Apartment Notifier"
            python = project / ".venv" / "bin" / "python"
            config = root / "Application Support" / "notifier.toml"
            stdout = root / "Logs" / "notifier.log"
            stderr = root / "Logs" / "notifier-error.log"
            definition = launch_agent_definition(
                project_directory=project,
                python_executable=python,
                config_path=config,
                stdout_path=stdout,
                stderr_path=stderr,
            )

        self.assertEqual(LABEL, definition["Label"])
        self.assertEqual(
            [
                str(python.resolve()),
                "-m",
                "apartment_notifier",
                "--config",
                str(config.resolve()),
                "run",
            ],
            definition["ProgramArguments"],
        )
        self.assertEqual(str(project.resolve()), definition["WorkingDirectory"])
        self.assertTrue(definition["RunAtLoad"])
        self.assertEqual({"SuccessfulExit": False}, definition["KeepAlive"])


if __name__ == "__main__":
    unittest.main()
