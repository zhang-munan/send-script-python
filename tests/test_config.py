import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adb_sms_worker.config import Settings, load_dotenv


class ConfigTests(unittest.TestCase):
    def test_dotenv_does_not_override_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("DB_HOST=file-host\nDB_USERNAME='worker'\n", encoding="utf-8")
            with patch.dict(os.environ, {"DB_HOST": "env-host"}, clear=True):
                load_dotenv(path)
                self.assertEqual(os.environ["DB_HOST"], "env-host")
                self.assertEqual(os.environ["DB_USERNAME"], "worker")

    def test_serials_and_sim_slot(self):
        with patch.dict(
            os.environ,
            {"DB_USERNAME": "u", "ADB_DEVICE_SERIALS": "one, two", "SIM_SLOT": "2"},
            clear=True,
        ):
            settings = Settings.from_env("/does/not/exist")
            self.assertEqual(settings.device_serials, ("one", "two"))
            self.assertEqual(settings.sim_slot, 2)
            self.assertEqual(settings.db_timezone, "+08:00")

    def test_rejects_invalid_database_timezone(self):
        with patch.dict(
            os.environ,
            {"DB_USERNAME": "u", "DB_TIMEZONE": "+00:00"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "DB_TIMEZONE"):
                Settings.from_env("/does/not/exist")


if __name__ == "__main__":
    unittest.main()
