import subprocess
import shutil
import logging
from .exceptions import *
from .base_time_sync import BaseTimeSync

logger = logging.getLogger(__name__)


class LinuxTimeSync(BaseTimeSync):
    @staticmethod
    def _command_exists(cmd: str) -> bool:
        return shutil.which(cmd) is not None

    @staticmethod
    def start_time_service() -> None:
        '''
        Ensure a Linux time synchronization service is enabled and running.
        Tries `timedatectl`/`systemd-timesyncd` first, then common daemons
        such as `ntp` and `chronyd`.

        Raises:
            SubprocessError: when an invoked system command returns a failure.
            UnexpectedError: for other unexpected failures.
        '''
        try:
            logger.debug("checking linux time service")
            if LinuxTimeSync._command_exists("timedatectl"):
                status = subprocess.run(["timedatectl", "set-ntp", "true"], capture_output=True, text=True)
                if status.returncode != 0:
                    raise TimeServicePermissionError(status)

                logger.debug("enabled timedatectl NTP")
                if LinuxTimeSync._command_exists("systemctl"):
                    status = subprocess.run(["systemctl", "start", "systemd-timesyncd"], capture_output=True, text=True)
                    if status.returncode != 0:
                        raise TimeServicePermissionError(status)

                return None

            if LinuxTimeSync._command_exists("systemctl"):
                for svc in ("ntp", "chronyd"):
                    status = subprocess.run(["systemctl", "start", svc], capture_output=True, text=True)
                    if status.returncode != 0:
                        raise TimeServicePermissionError(status)
                return None

            logger.warning("no known time service manager found on linux")
            return None
        except TimeServicePermissionError:
            raise
        except Exception as e:
            logger.error(f"could not start linux time service: {e}")
            raise UnexpectedError(str(e))

    @staticmethod
    def sync_time() -> None:
        '''
        Trigger an immediate time synchronization on Linux. Attempts the
        following (in order): `ntpdate -u pool.ntp.org`, `chronyc makestep`,
        then enabling NTP via `timedatectl`.

        Raises:
            SubprocessError: when an invoked command fails.
            UnexpectedError: when no suitable sync tool is available or on
                             other unexpected failures.
        '''
        try:
            logger.debug("start linux time sync")
            if LinuxTimeSync._command_exists("ntpdate"):
                status = subprocess.run(["ntpdate", "-u", "pool.ntp.org"], capture_output=True, text=True)
                if status.returncode != 0:
                    raise TimeServicePermissionError(status)
                logger.info("time sync via ntpdate succeeded")
                return None

            if LinuxTimeSync._command_exists("chronyc"):
                status = subprocess.run(["chronyc", "makestep"], capture_output=True, text=True)
                if status.returncode != 0:
                    raise TimeServicePermissionError(status)
                logger.info("time sync via chronyc succeeded")
                return None

            if LinuxTimeSync._command_exists("timedatectl"):
                status = subprocess.run(["timedatectl", "set-ntp", "true"], capture_output=True, text=True)
                if status.returncode != 0:
                    raise TimeServicePermissionError(status)
                logger.info("enabled timedatectl NTP")
                return None

            logger.warning("no known immediate sync tool available on linux")
            raise UnexpectedError("no sync tool available")
        except TimeServicePermissionError:
            raise
        except Exception as e:
            logger.error(f"error during linux time sync: {e}")
            raise UnexpectedError(str(e))
