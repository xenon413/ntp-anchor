import subprocess
import shutil
import logging

logger = logging.getLogger(__name__)


class LinuxTimeSync:
    @staticmethod
    def _command_exists(cmd: str) -> bool:
        return shutil.which(cmd) is not None

    @staticmethod
    def start_time_service() -> bool:
        '''
        Attempt to enable and start a time synchronization service on Linux.
        Tries `timedatectl`/`systemd-timesyncd`, then falls back to starting
        common services (`ntp`, `chronyd`). Returns True on success.
        '''
        try:
            if LinuxTimeSync._command_exists("timedatectl"):
                subprocess.run(["timedatectl", "set-ntp", "true"], capture_output=True, text=True)
                if LinuxTimeSync._command_exists("systemctl"):
                    subprocess.run(["systemctl", "start", "systemd-timesyncd"], capture_output=True, text=True)
                return True

            if LinuxTimeSync._command_exists("systemctl"):
                # Try starting common daemons; if they're not present the calls
                # will simply fail silently.
                for svc in ("ntp", "chronyd"):
                    subprocess.run(["systemctl", "start", svc], capture_output=True, text=True)
                return True

            logger.warning("no known time service manager found on linux")
            return True
        except Exception as e:
            logger.warning(f"could not start linux time service: {e}")
            return False

    @staticmethod
    def sync_time() -> bool:
        '''
        Attempt an immediate time synchronization on Linux. Prefers `ntpdate`
        if available, then `chronyc makestep`, or falls back to enabling NTP
        via `timedatectl`.
        '''
        try:
            if LinuxTimeSync._command_exists("ntpdate"):
                result = subprocess.run(["ntpdate", "-u", "pool.ntp.org"], capture_output=True, text=True)
                return result.returncode == 0

            if LinuxTimeSync._command_exists("chronyc"):
                subprocess.run(["chronyc", "makestep"], capture_output=True, text=True)
                return True

            if LinuxTimeSync._command_exists("timedatectl"):
                subprocess.run(["timedatectl", "set-ntp", "true"], capture_output=True, text=True)
                return True

            logger.warning("no known immediate sync tool available on linux")
            return False
        except Exception as e:
            logger.warning(f"error during linux time sync: {e}")
            return False
