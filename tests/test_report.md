# Test Report

## Latest Default Safe Test Run

- Date: 2026-10-13
- Environment: Windows PowerShell, project `.venv`
- Source: User-provided terminal output
- Command: `pytest`
- Result: **Passed; no warnings reported**
- Summary: **329 passed, 3 deselected in 24.22s**

```text
........................................................................... [ 22%]
........................................................................... [ 45%]
........................................................................... [ 68%]
........................................................................... [ 91%]
.............................                                               [100%]
329 passed, 3 deselected in 24.22s
```

This run includes regression coverage for:

- `timestamp()` and `GET /timestamp`: fresh Unix epoch seconds, operation
  without correction privileges or manager mutations, and help/OpenAPI contracts.
- The timestamp endpoint in the loopback API subprocess integration test.
- Project-local pytest temp directories: cleanup after passing tests, failed
  tests, and collection errors; isolation between runs; and preservation of
  explicit `--basetemp` paths by the custom cleanup.

The three intentionally deselected tests are:

- `tests/integration/test_live_network.py::test_public_ntp_quorum_read_only`
- `tests/integration/test_privileged_clock.py::test_privileged_gate_is_open_only_on_acknowledged_disposable_host`
- `tests/integration/test_privileged_clock.py::test_real_clock_sync_and_post_verification_on_disposable_host`

The default marker filter excludes `live_network` and `privileged` tests.
This result does not verify public NTP connectivity or actual host-clock
correction.

## Previous Default Safe Test Run

- Date: 2026-10-04
- Environment: Windows PowerShell, project `.venv`
- Command: `pytest`
- Result: **Passed with one dependency deprecation warning**
- Summary: **249 passed, 3 deselected, 1 warning in 12.58s**

```text
249 passed, 3 deselected, 1 warning in 12.58s
```

This historical run reported the following dependency warning:

```text
StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is
deprecated; install `httpx2` instead.
```

The test dependency metadata now selects `httpx2`. The latest run above
reported no warnings.

## Recorded Baseline (Before the Management API Work)

- Date: 2026-10-02
- Environment: Windows PowerShell, project `.venv`
- Command: `pytest`
- Result: **Passed**
- Summary: **72 passed in 0.60s**

```text
........................................................................ [100%]
72 passed in 0.60s
```

This historical result predates the Phase 2-5 API, CLI, integration, safety,
and CI tests. It must not be treated as verification of the current working
tree; the current-tree result is recorded above.

## Test Commands

Run the default safe suite (live-network and privileged tests are deselected):

```powershell
pytest
```

Run only unit tests:

```powershell
pytest -m unit
```

Run only integration tests:

```powershell
pytest -m "integration and not live_network and not privileged"
```

