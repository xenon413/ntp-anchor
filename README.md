# ntp-anchor

Small cross-platform helpers for checking NTP clock drift and synchronizing
the system time.

This repository provides lightweight platform-specific helpers to start the
system time service and trigger an immediate time synchronization when the
measured NTP offset exceeds a threshold.

## Installation

- To install the core package:

```
pip install git+https://github.com/xenon413/ntp-anchor.git@main
```

## Development & Testing

If you want to run the test suite or modify the codebase, clone the repository locally and install the development dependencies:

# 1. Clone the repository to your computer

```
git clone https://github.com/xenon413/ntp-anchor.git
```

# 2. Install the package in editable mode with testing tools

```
pip install -e .[test]
```

# 3. Run the test suite

```
pytest
```

Example:

```python
# When installed as a package (package name may be `ntp_anchor` when
# installed via pip) you can import the top-level helpers:
from ntp_anchor import get_ntp_time_offset, check_and_sync, check_and_sync_thread

print(get_ntp_time_offset())
check_and_sync()
thread, stop_event = check_and_sync_thread(cycle=300)
# stop later:
stop_event.set()
thread.join()
```

Public API

- `get_ntp_time_offset(ntp_server: list|None = None) -> float|None`: Query a
  list of NTP servers and return the first successful offset in seconds. May
  return `None` if no server responded.
- `start_time_service() -> bool`: Ensure the platform time/service is running.
- `sync_time() -> bool`: Trigger an immediate platform-specific time sync.
- `check_and_sync(ntp_server: list|None = None, thresh: float|None = None) -> bool`:
  Measure the NTP offset and perform a sync if the offset exceeds `thresh`.
- `check_and_sync_thread(ntp_server: list|None = None, thresh: float|None = None, cycle: int|None = None) -> (thread, event)`:
  Start a background thread that runs `check_and_sync` every `cycle` seconds.

Notes

- `sync_time()` typically requires an elevated process(Administrator) to succeed.

License

See the repository `LICENSE` file for license details.
