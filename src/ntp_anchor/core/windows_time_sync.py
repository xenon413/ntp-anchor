import subprocess
import logging

logger = logging.getLogger(__name__)

class WindowsTimeSync:
    # ---------- Windows Time Service ----------
    @staticmethod
    def start_time_service()->bool:
        '''
        Ensure the Windows `w32time` service is running. Attempts to start the
        service if it is not running. Returns `True` on success, `False` on
        error.
        '''
        try:
            logger.debug("checking w32time")
            status = subprocess.run(
                ["sc", "query", "w32time"],
                capture_output=True, text=True
            )
            logger.debug(f"w32time status: {status.stdout}")

            if "RUNNING" not in status.stdout:
                logger.debug("starting windows time service")
                res = subprocess.run(["sc", "start", "w32time"], capture_output=True, text=True)
                logger.debug(f"start windows time service result: {res}")
            return True
        except Exception as e:
            logger.warning(f"could not check/start service: {e}")
            return False

    # need admin
    @staticmethod
    def sync_time()->bool:
        '''
        Trigger a Windows time resynchronization via `w32tm /resync`.
        Requires administrative privileges. Returns `True` when the command
        reports success, otherwise `False`.
        '''
        try:
            logger.debug("start time sync")
            result = subprocess.run(["w32tm", "/resync"], capture_output=True, text=True)
            if result.returncode == 0 and ("成功" in result.stdout or "success" in result.stdout.lower()):
                logger.info(f"time sync success: {result.stdout}")
                return True
            else:
                logger.warning(f"time sync failed: {result.stderr.strip()}")
                return False
            
        except Exception as e:
            # TODO: rise permission error when no permission
            logger.warning(f"error during sync: {e}")
            return False