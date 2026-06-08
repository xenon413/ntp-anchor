import threading
from .core_logic import (
    service,
    get_ntp_time_offset,
    check_and_sync,
    check_and_sync_thread
)

def get_ntp_time_offset(
        ntp_server:tuple[str, ...]=("pool.ntp.org", "time.google.com", "time2.google.com")
    )->int|None:
    '''
    Query NTP servers and return the time offset in seconds for the first
    responsive server. Returns the offset as a number or `None` if all
    servers fail.
    '''
    return get_ntp_time_offset(ntp_server)

def start_time_service()->bool:
    '''
    Ensure the platform-specific time service is running. Returns `True` on
    success or if already running, `False` on error.
    '''
    return service.start_time_service()

def sync_time()->bool:
    '''
    Trigger an immediate platform-specific time synchronization. Returns
    `True` on success, `False` otherwise.
    '''
    return service.sync_time()

def check_and_sync(
        ntp_server:tuple[str, ...]=("pool.ntp.org", "time.google.com", "time2.google.com"), 
        thresh:float=0.5
    )->bool:
    '''
    Check NTP offset against `thresh` and perform a sync if the offset exceeds
    the threshold. Returns `True` on success or if already within threshold,
    `False` on failure.
    '''
    return check_and_sync(ntp_server, thresh)

def check_and_sync_thread(
        ntp_server:tuple[str, ...]=("pool.ntp.org", "time.google.com", "time2.google.com"), 
        thresh:float=0.5, 
        cycle:float=300.0
    )->tuple[threading.Thread, threading.Event]:
    '''
    Start a background thread that periodically runs `check_and_sync` every
    `cycle` seconds. Returns `(thread, stop_event)` where setting the event
    stops the background loop.
    '''
    return check_and_sync_thread(ntp_server, thresh, cycle)

__all__ = [
    "start_time_service", 
    "sync_time", 
    "get_ntp_time_offset", 
    "check_and_sync",
    "check_and_sync_thread"
]
