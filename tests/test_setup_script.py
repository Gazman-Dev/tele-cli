from __future__ import annotations

import unittest
from pathlib import Path


class SetupScriptTests(unittest.TestCase):
    def test_setup_script_launches_app_shell_for_interactive_runs(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "setup.sh").read_text(encoding="utf-8")

        self.assertIn("is_interactive_terminal()", script)
        self.assertIn("[ -r /dev/tty ] && [ -t 1 ]", script)
        self.assertIn("launch_app_shell()", script)
        self.assertIn('< /dev/tty > /dev/tty 2> /dev/tty', script)
        self.assertIn("--no-warn-script-location", script)
        self.assertIn('if is_interactive_terminal; then', script)
        self.assertIn("launch_app_shell", script)

        interactive_index = script.index("if is_interactive_terminal; then")
        launch_index = script.index("launch_app_shell", interactive_index)
        fallback_setup_index = script.rindex("run_setup_if_needed")
        fallback_service_index = script.rindex("install_and_start_service")

        self.assertLess(interactive_index, fallback_setup_index)
        self.assertLess(launch_index, fallback_setup_index)
        self.assertLess(launch_index, fallback_service_index)

    def test_setup_script_no_longer_prompts_existing_install_in_raw_shell(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "setup.sh").read_text(encoding="utf-8")

        self.assertIn("WAS_INSTALLED=0", script)
        self.assertIn("if is_installed; then", script)
        self.assertNotIn("prompt_existing_install_action()", script)
        self.assertNotIn("Press Enter to update it, or type uninstall to remove it", script)


if __name__ == "__main__":
    unittest.main()
