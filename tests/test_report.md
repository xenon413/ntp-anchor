# Test Report

## Latest Default Safe Test Run

- Date: 2026-10-04
- Environment: Windows PowerShell, project `.venv`
- Command: `pytest`
- Result: **Passed with one dependency deprecation warning**
- Summary: **249 passed, 3 deselected, 1 warning in 12.58s**

```text
249 passed, 3 deselected, 1 warning in 12.58s
```

The three deselected tests are the intentionally opt-in `live_network` and
`privileged` cases. The warning was:

```text
StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is
deprecated; install `httpx2` instead.
```

The test dependency metadata now selects `httpx2`; refresh the editable test
installation before the next run so the active virtual environment receives
it.

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

