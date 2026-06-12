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
    raise UnexpectedError(f"failed to initialize time sync service for {OS}") from e

# setup warning for incomplete services
if service is None:
    logger.critical(f"time sync not supported on current os: {OS}")
    raise UnsupportedOSError(f"time sync not supported on current os: {OS}")

# ---------- NTP Drift Check ----------
# support cross platform
def get_ntp_time_offset(ntp_server:tuple[str, ...])->int:
    for server in ntp_server:
        try:
            logger.debug(f"getting ntp time offset with: {server}")
            c = ntplib.NTPClient()
            response = c.request(server, version=3, timeout=2)
            logger.info(f"ntp time offset: {response.offset}")
            return response.offset

        except Exception as e:
            logger.warning(f"failed to get NTP time: {e} server: {server}")
    raise NTPQueryError(list(ntp_server))


def check_and_sync(ntp_server:tuple[str, ...], thresh:float, auto_start:bool)->None:
    if auto_start:
        service.start_time_service()

    offset = get_ntp_time_offset(ntp_server)

    if abs(offset) > thresh:
        logger.info(f"offset {offset:.3f}s exceeds threshold {thresh}s. syncing...")
        service.sync_time()

    else:
        logger.debug(f"offset {offset:.3f}s within threshold {thresh}s. skip sync")

# make it simple because in our case we only need one process at a time 
def check_and_sync_thread(ntp_server:tuple[str, ...], thresh:float, cycle:float, auto_start:bool)->tuple[threading.Thread, threading.Event]:
    event = threading.Event()
    def func():
        while not event.is_set():
            try:
                check_and_sync(ntp_server, thresh, auto_start)
                event.wait(timeout=cycle)
            #TODO: except error service not start
            except TimeServicePermissionError as e:
                logger.critical(f"Permissions failure in background sync: {e}")
                break

            except Exception as e:
                logger.critical(f"time sync falied: {e}")

    thread = threading.Thread(target=func, daemon=True)
    thread.start()
    return thread, event
