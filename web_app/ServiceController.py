import os
import time
from pystemd.systemd1 import Unit
from pystemd.systemd1 import Manager

class ServiceController(object):
    """
        A simple wrapper around pystemd to manage systemd services
    """
    
    manager = Manager(_autoload=True)

    def __init__(self, unit):
        """
            param: unit: a systemd unit name (ie str2str_tcp.service...)
        """
        self.unit = Unit(bytes(unit, 'utf-8'), _autoload=True)
        
    def isActive(self):
        if self.unit.Unit.ActiveState == b'active':
            return True
        elif self.unit.Unit.ActiveState == b'activating':
            #TODO manage this transitionnal state differently
            return True
        else:
            return False

    def active_state(self):
        """Return systemd's current ActiveState as text."""
        return self.unit.Unit.ActiveState.decode()

    def wait_for_inactive(self, timeout=10.0, interval=0.1):
        """Wait for a stop job to finish, rather than only being queued."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.active_state() in ("inactive", "failed"):
                return True
            time.sleep(interval)
        return self.active_state() in ("inactive", "failed")
    
    def get_nrestart(self):
        """
            Get the number of restarts since the last service startup
        """
        return self.unit.Service.NRestarts

    def get_result(self):
        """
            Get the unit return status.
            success => it's ok
            exit-code => str2str doesn't start successfully
            We can read a success between the startup and the first error
        """
        if "org.freedesktop.systemd1.Service" in self.unit._interfaces:
            return self.unit.Service.Result.decode()
        elif "org.freedesktop.systemd1.Timer" in self.unit._interfaces:
            return self.unit.Timer.Result.decode()

    def getUser(self):
        return self.unit.Service.User.decode()
    
    def status(self):
        """
            get the unit status:
            auto-restart: the service will restart later
            start: the service is starting
            running; the service is running
        """
        return (self.unit.Unit.SubState).decode()

    def start(self):
        self.manager.Manager.EnableUnitFiles(self.unit.Unit.Names, False, True)
        return self.unit.Unit.Start(b'replace')
        
    def stop(self):
        self.manager.Manager.DisableUnitFiles(self.unit.Unit.Names, False)
        return self.unit.Unit.Stop(b'replace')
        
    def restart(self):
        return self.unit.Unit.Restart(b'replace')
