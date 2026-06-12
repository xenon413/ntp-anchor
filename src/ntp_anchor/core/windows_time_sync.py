import subprocess
import logging
from .exceptions import *
from .base_time_sync import BaseTimeSync
logger = logging.getLogger(__name__)

class WindowsTimeSync(BaseTimeSync):
    @staticmethod
    def start_time_service()->None:
        '''
        Ensure the Windows `w32time` service is running. Attempts to start the
        service if it is not running. Raises SubprocessError on failures.
        '''
        logger.debug("checking w32time")
        status = subprocess.run(
            ["sc.exe", "query", "w32time"],
            capture_output=True, text=True
        )
        if status.returncode != 0:
            raise TimeServiceCommandError(status)
        
        logger.debug(f"query w32time stdout: {status.stdout}")

        # service statuse
        # STOPPED
        # START_PENDING
        # STOP_PENDING
        # RUNNING
        # CONTINUE_PENDING (ignore case: not pausable)
        # PAUSE_PENDING (ignore case: not pausable)
        # PAUSED (ignore case: not pausable)
        if "RUNNING" not in status.stdout:
            logger.debug("starting windows time service")
            status = subprocess.run(["sc.exe", "start", "w32time"], capture_output=True, text=True)
            # error code 1056 for ERROR_SERVICE_ALREADY_RUNNING
            if status.returncode != 0 and status.returncode != 1056:
                if status.returncode == 5:
                    raise TimeServicePermissionError(status)
                raise TimeServiceCommandError(status)
            
            logger.debug(f"start w32time stdout: {status.stdout}")

    @staticmethod
    def sync_time()->None:
        '''
        Trigger a Windows time resynchronization via `w32tm /resync`.
        Requires administrative privileges. Raises SubprocessError on failure or 
        silent NTP failures.
        '''

        logger.debug("start time sync")
        # need admin
        status = subprocess.run(["w32tm", "/resync"], capture_output=True, text=True)
        if status.returncode != 0:
            if status.returncode == 5 or status.returncode == 2147942405:
                raise TimeServicePermissionError(status)
            
            raise TimeServiceCommandError(status)
        
        if "2147942405" in status.stdout or "0x80070005" in status.stdout:
            raise TimeServicePermissionError(status)
        
        if "did not resync" in status.stdout.lower() or "no time data was available" in status.stdout.lower():
            raise TimeServiceCommandError(status)
        
        logger.info(f"time sync success: {status.stdout}")
            
#TODO: add zn support