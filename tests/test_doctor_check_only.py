"""An operator can inspect daemon status without starting or repairing it."""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("forge_doctor", Path(__file__).resolve().parents[1] / "tools/forge.py")
forge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(forge)


class DoctorCheckOnlyTests(unittest.TestCase):
    def invoke(self, status):
        with patch.object(forge, "_require_executable", return_value="/fictional/forge"), patch.object(forge.subprocess, "run", return_value=status) as process, patch.object(forge, "call") as rpc, patch.object(forge.os, "chmod") as chmod, patch.object(forge.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()) as output:
            forge.doctor(check_only=True)
            rpc.assert_not_called(); chmod.assert_not_called(); sleep.assert_not_called()
            self.assertEqual(process.call_args_list[0].args[0], ["/fictional/forge", "status"])
            self.assertEqual(process.call_count, 1)
            return output.getvalue()

    def test_running_status_is_not_reported_as_terminal_acceptance(self):
        output = self.invoke(subprocess.CompletedProcess([], 0, "Forge running", ""))
        self.assertIn("terminal creation not checked", output)

    def test_stopped_status_never_starts_a_daemon(self):
        with self.assertRaises(SystemExit) as stopped:
            self.invoke(subprocess.CompletedProcess([], 0, "not running", ""))
        self.assertIn("no start", str(stopped.exception))

    def test_unknown_status_is_not_a_success(self):
        with self.assertRaises(SystemExit):
            self.invoke(subprocess.CompletedProcess([], 1, "", "status unavailable"))

    def test_cli_forwards_the_explicit_check_only_option(self):
        with patch.object(sys, "argv", ["forge.py", "doctor", "--check-only"]), patch.object(forge, "doctor") as doctor:
            forge.main()
            doctor.assert_called_once_with(check_only=True)

    def test_stopped_path_issues_only_the_status_command(self):
        status = subprocess.CompletedProcess([], 0, "not running", "")
        with patch.object(forge, "_require_executable", return_value="/fictional/forge"), patch.object(forge.subprocess, "run", return_value=status) as process, patch.object(forge, "call") as rpc, patch.object(forge.os, "chmod") as chmod, patch.object(forge.time, "sleep") as sleep:
            with self.assertRaises(SystemExit):
                forge.doctor(check_only=True)
            self.assertEqual(process.call_count, 1)
            self.assertEqual(process.call_args.args[0], ["/fictional/forge", "status"])
            rpc.assert_not_called(); chmod.assert_not_called(); sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
