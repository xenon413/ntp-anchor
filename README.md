# ntp-anchor

Small cross-platform helpers for checking NTP clock drift and synchronizing
the system time.

This repository provides lightweight platform-specific helpers to start the
system time service and trigger an immediate time synchronization when the
measured NTP offset exceeds a threshold.

Quick usage

- Install runtime dependencies:

```
python -m pip install -r requirements.txt
```

- Run the test suite (optional):

```
python -m pip install pytest
pytest -q
```

Example (after installing the package or running from the project root):

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

- Windows: `sync_time()` invokes `w32tm /resync` and typically requires an
  elevated process to succeed.
- The codebase uses Python 3.10+ union type syntax (e.g. `X | None`). Python
  3.10 or newer is recommended.

License

See the repository `LICENSE` file for license details.
