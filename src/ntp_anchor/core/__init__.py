import threading
from .core_logic import (
    service,
    get_ntp_time_offset as _get_ntp_time_offset,
    check_and_sync as _check_and_sync,
    check_and_sync_thread as _check_and_sync_thread
)

# for type hint
from .base_time_sync import BaseTimeSync
from typing import Type
service:Type[BaseTimeSync]

def get_ntp_time_offset(
        ntp_server:tuple[str, ...]=("pool.ntp.org", "time.google.com", "time2.google.com")
    )->int:
    '''
    Query NTP servers and return the time offset in seconds for the first
    responsive server.

    Raises:
        NTPQueryError: if all configured NTP servers fail to respond.

    Returns:
        The offset in seconds as a float.
    '''
    return _get_ntp_time_offset(ntp_server)

def start_time_service()->None:
    '''
    Ensure the platform-specific time service is running.

    Raises:
        SubprocessError: if a required system command fails.
        UnexpectedError: on other unexpected failures.

    Returns:
        None on success.
    '''
    return service.start_time_service()

def sync_time()->None:
    '''
    Trigger an immediate platform-specific time synchronization.

    Raises:
        SubprocessError: if the sync command fails.
        UnexpectedError: if no suitable sync tool is available or on other
                         unexpected failures.

    Returns:
        None on success.
    '''
    return service.sync_time()

def check_and_sync(
        ntp_server:tuple[str, ...]=("pool.ntp.org", "time.google.com", "time2.google.com"), 
        thresh:float=0.5,
        auto_start:bool=True
    )->None:
    '''
    Check NTP offset against `thresh` and perform a sync if the offset exceeds
    the threshold.

    Raises:
        NTPQueryError: if NTP servers cannot be queried.
        SubprocessError/UnexpectedError: if starting the service or syncing fails.

    Returns:
        None on success.
    '''
    return _check_and_sync(ntp_server, thresh, auto_start)

def check_and_sync_thread(
        ntp_server:tuple[str, ...]=("pool.ntp.org", "time.google.com", "time2.google.com"), 
        thresh:float=0.5, 
        cycle:float=300.0,
        auto_start:bool=True
    )->tuple[threading.Thread, threading.Event]:
    '''
    Start a background thread that periodically runs `check_and_sync` every
    `cycle` seconds. Returns `(thread, stop_event)` where setting the event
    stops the background loop.

    The background loop logs failures but does not raise exceptions out of
    the thread; callers can stop the thread via the returned event.
    '''
    return _check_and_sync_thread(ntp_server, thresh, cycle, auto_start)


