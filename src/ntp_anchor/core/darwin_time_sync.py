import subprocess
import shutil
import logging
from .exceptions import *
from .base_time_sync import BaseTimeSync

logger = logging.getLogger(__name__)


class DarwinTimeSync(BaseTimeSync):
    @staticmethod
    def _command_exists(cmd: str) -> bool:
        return shutil.which(cmd) is not None

    @staticmethod
    def start_time_service() -> None:
        '''
        Enable network time on macOS using `systemsetup` when available.

        Raises:
            SubprocessError: when the underlying `systemsetup` command fails.
            UnexpectedError: for other unexpected failures.
        '''
        try:
            logger.debug("checking darwin time service")
            if DarwinTimeSync._command_exists("systemsetup"):
                status = subprocess.run(["systemsetup", "-setusingnetworktime", "on"], capture_output=True, text=True)
                if status.returncode != 0:
                    raise TimeServiceCommandError(status)
                logger.debug("enabled network time via systemsetup")
                return None

            logger.warning("systemsetup not found on darwin; cannot enable network time")
            return None
        except TimeServiceCommandError:
            raise
        except Exception as e:
            logger.error(f"could not enable network time on darwin: {e}")
            raise UnexpectedError(str(e))

    @staticmethod
    def sync_time() -> None:
        '''
        Trigger an immediate time synchronization on macOS. Tries `sntp -sS`
        first, then falls back to `ntpd -q -g` if available.

        Raises:
            SubprocessError: when an invoked sync command fails.
            UnexpectedError: when no suitable sync tool is available.
        '''
        try:
            logger.debug("start darwin time sync")
            if DarwinTimeSync._command_exists("sntp"):
                status = subprocess.run(["sntp", "-sS", "pool.ntp.org"], capture_output=True, text=True)
                if status.returncode != 0:
                    raise TimeServiceCommandError(status)
                logger.info("time sync via sntp succeeded")
                return None

            if DarwinTimeSync._command_exists("ntpd"):
                status = subprocess.run(["ntpd", "-q", "-g"], capture_output=True, text=True)
                if status.returncode != 0:
                    raise TimeServiceCommandError(status)
                logger.info("time sync via ntpd succeeded")
                return None

            logger.warning("no known immediate sync tool available on darwin")
            raise UnexpectedError("no sync tool available")
        except TimeServiceCommandError:
            raise
        except Exception as e:
            logger.error(f"error during darwin time sync: {e}")
            raise UnexpectedError(str(e))
