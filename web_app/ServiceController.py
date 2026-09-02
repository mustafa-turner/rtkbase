import subprocess
import time


class ServiceControllerError(RuntimeError):
    """Raised when systemctl cannot complete a service operation."""


class ServiceController(object):
    """Read and control systemd units through synchronous systemctl calls."""

    STATE_PROPERTIES = (
        "Id", "ActiveState", "SubState", "Result", "User", "NRestarts")
    CACHE_SECONDS = 1.5

    def __init__(self, unit):
        """
            param: unit: a systemd unit name (ie str2str_tcp.service...)
        """
        self.unit_name = unit
        self._state = {}
        self._state_time = 0.0

    @staticmethod
    def _raise_for_result(command, result):
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ServiceControllerError(
                "{} failed{}".format(
                    " ".join(command), ": " + detail if detail else ""))

    @staticmethod
    def _parse_show_output(output):
        records = []
        for block in output.strip().split("\n\n"):
            record = {}
            for line in block.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    record[key] = value
            if record:
                records.append(record)
        return records

    @classmethod
    def _show_arguments(cls):
        return ["--property={}".format(name)
                for name in cls.STATE_PROPERTIES]

    def _run_systemctl(self, *arguments, **kwargs):
        """Run one synchronous systemctl operation without a shell."""
        check = kwargs.pop("check", True)
        if kwargs:
            raise TypeError("Unexpected keyword arguments: {}".format(
                ", ".join(kwargs)))
        command = ["systemctl"] + list(arguments) + [self.unit_name]
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, check=False)
        if check:
            self._raise_for_result(command, result)
        return result

    def refresh(self, force=False):
        """Refresh this unit's cached properties."""
        now = time.monotonic()
        if (not force and self._state and
                now - self._state_time < self.CACHE_SECONDS):
            return
        result = self._run_systemctl(
            "show", *self._show_arguments(), "--no-pager")
        records = self._parse_show_output(result.stdout)
        if not records:
            raise ServiceControllerError(
                "systemctl returned no state for {}".format(self.unit_name))
        self._state = records[0]
        self._state_time = now

    @classmethod
    def refresh_all(cls, controllers):
        """Refresh several controllers with one systemctl process."""
        controllers = list(controllers)
        if not controllers:
            return
        command = (["systemctl", "show"] + cls._show_arguments() +
                   ["--no-pager"] +
                   [controller.unit_name for controller in controllers])
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, check=False)
        cls._raise_for_result(command, result)
        states = {record.get("Id"): record
                  for record in cls._parse_show_output(result.stdout)}
        now = time.monotonic()
        for controller in controllers:
            state = states.get(controller.unit_name)
            if state is not None:
                controller._state = state
                controller._state_time = now

    def _property(self, name):
        self.refresh()
        return self._state.get(name, "")

    def _invalidate(self):
        self._state = {}
        self._state_time = 0.0

    def isActive(self):
        return self.active_state() in ("active", "activating")

    def active_state(self):
        """Return systemd's current ActiveState as text."""
        return self._property("ActiveState")

    def wait_for_inactive(self, timeout=10.0, interval=0.1):
        """Wait for a stop job to finish, rather than only being queued."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.active_state() in ("inactive", "failed"):
                return True
            time.sleep(interval)
        return self.active_state() in ("inactive", "failed")

    def get_nrestart(self):
        """Get the number of restarts since the last service startup."""
        return int(self._property("NRestarts") or 0)

    def get_result(self):
        """Get the unit return status."""
        return self._property("Result")

    def getUser(self):
        return self._property("User")

    def status(self):
        """Get the unit SubState (running, dead, auto-restart, etc.)."""
        return self._property("SubState")

    def start(self):
        """Reset failures, enable the unit and start it."""
        self._run_systemctl("reset-failed", check=False)
        self._run_systemctl("enable")
        result = self._run_systemctl("start")
        self._invalidate()
        return result

    def stop(self):
        """Stop and disable the unit."""
        result = self._run_systemctl("disable", "--now")
        self._invalidate()
        return result

    def restart(self):
        """Restart the unit."""
        result = self._run_systemctl("restart")
        self._invalidate()
        return result
