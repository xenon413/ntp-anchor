import threading

DEFAULT_NTP_SERVER = ["pool.ntp.org", "time.google.com", "time2.google.com"]
DEFAULT_THRESH = 0.5 # max diff before sync
DEFAULT_CYCLE = 300 # cycle in seconds

def get_ntp_time_offset(ntp_server:list|None=None)->int|None:
    '''
    Query NTP servers and return the time offset in seconds for the first
    responsive server. Returns the offset as a number or `None` if all
    servers fail.
    '''
    from .core_logic import get_ntp_time_offset
    ntp_server = ntp_server or DEFAULT_NTP_SERVER
    return get_ntp_time_offset(ntp_server)

def start_time_service()->bool:
    '''
    Ensure the platform-specific time service is running. Returns `True` on
    success or if already running, `False` on error.
    '''
    from .core_logic import service
    return service.start_time_service()

def sync_time()->bool:
    '''
    Trigger an immediate platform-specific time synchronization. Returns
    `True` on success, `False` otherwise.
    '''
    from .core_logic import service
    return service.sync_time()

def check_and_sync(ntp_server:list|None=None, thresh:int|None=None)->bool:
    '''
    Check NTP offset against `thresh` and perform a sync if the offset exceeds
    the threshold. Returns `True` on success or if already within threshold,
    `False` on failure.
    '''
    from .core_logic import check_and_sync
    ntp_server = ntp_server or DEFAULT_NTP_SERVER
    thresh = DEFAULT_THRESH if thresh is None else thresh
    return check_and_sync(ntp_server, thresh)

def check_and_sync_thread(ntp_server:list|None=None, thresh:float|None=None, cycle:float|None=None)->tuple[threading.Thread, threading.Event]:
    '''
    Start a background thread that periodically runs `check_and_sync` every
    `cycle` seconds. Returns `(thread, stop_event)` where setting the event
    stops the background loop.
    '''
    from .core_logic import check_and_sync_thread
    ntp_server = ntp_server or DEFAULT_NTP_SERVER
    thresh = DEFAULT_THRESH if thresh is None else thresh
    cycle = DEFAULT_CYCLE if cycle is None else cycle
    return check_and_sync_thread(ntp_server, thresh, cycle)

__all__ = [
    "start_time_service", 
    "sync_time", 
    "get_ntp_time_offset", 
    "check_and_sync",
    "check_and_sync_thread"
]
