import pathlib
import subprocess
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "web_app"))
import ServiceController as service_controller


class ServiceControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = service_controller.ServiceController("demo.service")

    @staticmethod
    def completed(command, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(
            command, returncode, stdout=stdout, stderr=stderr)

    def test_start_resets_enables_and_starts_unit(self):
        with mock.patch.object(
                service_controller.subprocess, "run",
                side_effect=lambda command, **unused: self.completed(command)) as run:
            self.controller.start()

        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["systemctl", "reset-failed", "demo.service"],
            ["systemctl", "enable", "demo.service"],
            ["systemctl", "start", "demo.service"],
        ])

    def test_stop_disables_and_stops_unit_synchronously(self):
        with mock.patch.object(
                service_controller.subprocess, "run",
                return_value=self.completed([])) as run:
            self.controller.stop()

        self.assertEqual(
            run.call_args.args[0],
            ["systemctl", "disable", "--now", "demo.service"])

    def test_refresh_all_uses_one_process_and_updates_each_unit(self):
        other = service_controller.ServiceController("other.timer")
        output = (
            "Id=demo.service\nActiveState=active\nSubState=running\n"
            "Result=success\nUser=rtkbase\nNRestarts=0\n\n"
            "Id=other.timer\nActiveState=active\nSubState=waiting\n"
            "Result=success\nUser=\nNRestarts=0\n")
        with mock.patch.object(
                service_controller.subprocess, "run",
                return_value=self.completed([], stdout=output)) as run:
            service_controller.ServiceController.refresh_all(
                [self.controller, other])

        self.assertEqual(run.call_count, 1)
        self.assertTrue(self.controller.isActive())
        self.assertEqual(self.controller.status(), "running")
        self.assertEqual(other.status(), "waiting")

    def test_systemctl_failure_is_reported(self):
        with mock.patch.object(
                service_controller.subprocess, "run",
                return_value=self.completed(
                    [], returncode=1, stderr="permission denied")):
            with self.assertRaisesRegex(
                    service_controller.ServiceControllerError,
                    "permission denied"):
                self.controller.restart()


if __name__ == "__main__":
    unittest.main()
