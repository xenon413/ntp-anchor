from ntp_anchor import (
    start_time_service, 
    sync_time,
    get_ntp_time_offset,
    check_and_sync,
    check_and_sync_thread
)

def test_start_time_service():
    assert start_time_service(), "start time service failed"

def test_sync_time():
    assert sync_time(), "time sync failed"

def test_get_ntp_time_offset():
    assert get_ntp_time_offset() is not None, "get_ntp_time offset failed"

def test_check_and_sync():
    assert check_and_sync(), "check and sync failed"