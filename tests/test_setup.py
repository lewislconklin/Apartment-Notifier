from pathlib import Path
import os
import platform
import subprocess
import sys
import unittest


ROOT = Path(__file__).parents[1]


@unittest.skipUnless(platform.system() == "Darwin", "macOS setup test")
class SetupScriptTests(unittest.TestCase):
    def test_check_mode_finds_compatible_python_without_installing(self) -> None:
        environment = os.environ.copy()
        environment["APARTMENT_NOTIFIER_PYTHON"] = sys.executable
        result = subprocess.run(
            [str(ROOT / "setup-macos.sh"), "--check"],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertIn("Setup prerequisites are satisfied", result.stdout)
        self.assertTrue(os.access(ROOT / "setup-macos.sh", os.X_OK))
        self.assertTrue(os.access(ROOT / "apartment-notifier", os.X_OK))


if __name__ == "__main__":
    unittest.main()
