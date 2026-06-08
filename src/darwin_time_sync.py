import subprocess
import shutil
import logging

logger = logging.getLogger(__name__)


class DarwinTimeSync:
    @staticmethod
    def _command_exists(cmd: str) -> bool:
        return shutil.which(cmd) is not None

    @staticmethod
    def start_time_service() -> bool:
        '''
        Enable network time on macOS using `systemsetup` if available. Returns
        True on success.
        '''
        try:
            if DarwinTimeSync._command_exists("systemsetup"):
                # systemsetup typically requires sudo when changing system settings.
                subprocess.run(["systemsetup", "-setusingnetworktime", "on"], capture_output=True, text=True)
                return True

            logger.warning("systemsetup not found on darwin; cannot enable network time")
            return True
        except Exception as e:
            logger.warning(f"could not enable network time on darwin: {e}")
            return False

    @staticmethod
    def sync_time() -> bool:
        '''
        Trigger an immediate time sync on macOS. Tries `sntp -sS` first, then
        falls back to invoking `ntpd -q -g` if available.
        '''
        try:
            if DarwinTimeSync._command_exists("sntp"):
                result = subprocess.run(["sntp", "-sS", "pool.ntp.org"], capture_output=True, text=True)
                return result.returncode == 0

            if DarwinTimeSync._command_exists("ntpd"):
                result = subprocess.run(["ntpd", "-q", "-g"], capture_output=True, text=True)
                return result.returncode == 0

            logger.warning("no known immediate sync tool available on darwin")
            return False
        except Exception as e:
            logger.warning(f"error during darwin time sync: {e}")
            return False
