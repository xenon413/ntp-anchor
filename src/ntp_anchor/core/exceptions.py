import logging
import subprocess
logger = logging.getLogger(__name__)

class NTPAnchorError(Exception):
    '''Base exception for all errors raised by the ntp-anchor package.'''
    
    def __init__(self, msg:str="NTPAnchorError", *args, **kwargs):
        super().__init__(msg, *args, **kwargs)
        logger.error(msg)

class UnsupportedOSError(NTPAnchorError):
    '''Raised when the package is executed on an unsupported operating system.'''

class UnexpectedError(NTPAnchorError):
    '''Raised when an unexpected error occurs.'''

class TimeServiceCommandError(NTPAnchorError):
    '''Raised when a Windows subprocess execution fails or returns an error code.'''
    
    def __init__(self, status:subprocess.CompletedProcess, *args, **kwargs):
        # 1. Unpack and format the command run from status.args
        if isinstance(status.args, list):
            cmd_string = " ".join(status.args)
        else:
            cmd_string = str(status.args)
        
        # 2. Clean up the output streams safely
        clean_stderr = status.stderr.strip() if status.stderr else "No error details provided."
        clean_stdout = status.stdout.strip() if status.stdout else "None"
        
        # 3. Build the structured message using the object's properties
        custom_message = (
            f"\n[Subprocess Execution Failure]\n"
            f"  ↳ Command Run: '{cmd_string}'\n"
            f"  ↳ Exit Code:   {status.returncode}\n"
            f"  ↳ OS Details:  {clean_stderr}\n"
            f"  ↳ OS Output:   {clean_stdout}"
        )
        
        super().__init__(custom_message, *args, **kwargs)

class NTPQueryError(NTPAnchorError):
    '''Raised when unable to query or calculate time offset from any configured NTP servers.'''
    
    def __init__(self, attempted_servers: list[str], *args, **kwargs):
        servers_str = ", ".join(attempted_servers)
        custom_message = (
            f"\n[NTP Query Failure]\n"
            f"  ↳ Status:  All pool queries exhausted.\n"
            f"  ↳ Servers: [{servers_str}]\n"
            f"  ↳ Reason:  Failed to fetch network time data from all nodes."
        )
        super().__init__(custom_message, *args, **kwargs)

class TimeServicePermissionError(NTPAnchorError, PermissionError):
    """
    Raised when the orchestrator lacks administrative privileges 
    to modify system time services.
    """
    def __init__(self, status:subprocess.CompletedProcess, *args, **kwargs):
        super().__init__(f"Access Denied for {" ".join(status.args)}. Please ensure you are running as an Administrator", *args, **kwargs)