import ntplib
import platform
import logging
import threading
from .exceptions import *

OS = platform.system().lower()

logger = logging.getLogger(__name__)
service = None
try:
    if OS == "windows":
        from .windows_time_sync import WindowsTimeSync
        service = WindowsTimeSync

    elif OS == "linux":
        from .linux_time_sync import LinuxTimeSync
        service = LinuxTimeSync

    elif OS == "darwin":
        from .darwin_time_sync import DarwinTimeSync
        service = DarwinTimeSync
        
    elif OS == "java":
        service = None
except Exception as e:
    logger.critical(f"failed to initialize time sync service for {OS}: {e}")
    raise UnexpectedError(f"failed to initialize time sync service for {OS}") from e

# setup warning for incomplete services
if service is None:
    logger.critical(f"time sync not supported on current os: {OS}")
    raise UnsupportedOSError(f"time sync not supported on current os: {OS}")

# ---------- NTP Drift Check ----------
# support cross platform
def get_ntp_time_offset(ntp_server:tuple[str])->int|None:
    '''
    Query provided NTP servers sequentially and return the first successful
    time offset in seconds. Returns the offset as a float or `None` if all
    servers fail.
    '''
    for server in ntp_server:
        try:
            logger.debug(f"getting ntp time offset with: {server}")
            c = ntplib.NTPClient()
            response = c.request(server, version=3, timeout=2)
            logger.debug(f"ntp time offset: {response.offset}")
            return response.offset

        except Exception as e:
            logger.warning(f"failed to get NTP time: {e} server: {server}")

    return None

def check_and_sync(ntp_server:tuple[str], thresh:float)->bool:
    '''
    Check the system time against NTP servers and synchronize the system
    clock if the measured offset exceeds `thresh` seconds. Returns `True`
    if time is within threshold or synchronization succeeded, `False`
    otherwise.
    '''
    try:
        res = service.start_time_service()
        if res is False:
            logger.warning("failed to start time service")
            return False
        
        offset = get_ntp_time_offset(ntp_server)
        if offset is None:
            logger.warning("failed to get ntp offset")
            return False
        
        if abs(offset) > thresh:
            logger.info(f"offset {offset:.3f}s exceeds threshold {thresh}s. syncing...")
            res = service.sync_time()
            return res
        else:
            return True
    except Exception as e:
        logger.critical(f"unexpected error: {e}")
        raise UnexpectedError from e

# make it simple because in our case we only need one process at a time 
def check_and_sync_thread(ntp_server:tuple[str], thresh:float, cycle:float)->tuple[threading.Thread, threading.Event]:
    '''
    Launch and return a background thread that periodically calls
    `check_and_sync(ntp_server, thresh)` every `cycle` seconds. Returns a
    `(thread, event)` tuple; set the returned `event` to stop the loop.
    '''
    event = threading.Event()
    def func():
        while not event.is_set():
            try:
                check_and_sync(ntp_server, thresh)
                event.wait(timeout=cycle)
            except:
                pass
    thread = threading.Thread(target=func, daemon=True)
    thread.start()
    return thread, event

