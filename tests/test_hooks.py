import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from internal.Agent.config import (
    _CONFIG_DIR,
    HooksConfig,
    get_config,
    get_config_dir,
    init_config,
)


class HooksConfigTest(unittest.TestCase):
    def tearDown(self):
        init_config(_CONFIG_DIR)

    def test_defaults_and_active_config_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / "config.yaml").write_text("{}\n", encoding="utf-8")

            init_config(config_dir)

            self.assertEqual(get_config().hooks.config_path, "")
            self.assertEqual(get_config().hooks.default_timeout, 10)
            self.assertEqual(get_config().hooks.stop_max_continuations, 5)
            self.assertEqual(get_config_dir(), config_dir.resolve())

    def test_explicit_relative_hook_path_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / "config.yaml").write_text(
                "hooks:\n  config_path: configs/hooks.json\n",
                encoding="utf-8",
            )

            init_config(config_dir)

            self.assertEqual(get_config().hooks.config_path, "configs/hooks.json")

    def test_timeout_must_be_positive(self):
        with self.assertRaises(ValidationError):
            HooksConfig(default_timeout=0)

    def test_stop_continuation_limit_cannot_be_negative(self):
        with self.assertRaises(ValidationError):
            HooksConfig(stop_max_continuations=-1)


if __name__ == "__main__":
    unittest.main()
