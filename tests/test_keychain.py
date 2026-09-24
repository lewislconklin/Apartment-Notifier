import unittest
from unittest.mock import MagicMock, patch

from apartment_notifier.keychain import store_password


class KeychainTests(unittest.TestCase):
    @patch("apartment_notifier.keychain.platform.system", return_value="Darwin")
    @patch("apartment_notifier.keychain.subprocess.run")
    def test_store_password_does_not_put_secret_in_process_arguments(
        self, run: MagicMock, _system: MagicMock
    ) -> None:
        store_password("Apartment Notifier Gmail", "sender@example.com", "secret")

        command = run.call_args.args[0]
        self.assertNotIn("secret", command)
        self.assertEqual("-w", command[-1])
        self.assertEqual("secret\n", run.call_args.kwargs["input"])
        self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
