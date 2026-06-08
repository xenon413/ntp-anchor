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
            status = subprocess.run(
                ["sc", "query", "w32time"],
                capture_output=True, text=True
            )
            if "RUNNING" not in status.stdout:
                logger.debug("starting windows time service...")
                subprocess.run(["sc", "start", "w32time"], capture_output=True, text=True)
                # TODO: check if start success
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
            result = subprocess.run(["w32tm", "/resync"], capture_output=True, text=True)
            if result.returncode == 0 and ("成功" in result.stdout or "success" in result.stdout.lower()):
                logger.info("time sync success")
                return True
            else:
                logger.warning(f"time sync failed: {result.stderr.strip()}")
                return False
            
        except Exception as e:
            logger.warning(f"error during sync: {e}")
            return False