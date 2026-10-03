# ntp-anchor Remediation and FastAPI Management Plan

## Planning Constraint

This document is the implementation roadmap only. No source code, test code, dependencies, or package configuration are changed as part of this planning step.

The implementation must preserve clear boundaries between:

1. NTP measurement and response validation.
2. Policy decisions about whether synchronization is safe.
3. Privileged platform-specific clock operations.
4. Thread-safe runtime state and background-worker lifecycle.
5. The FastAPI/Uvicorn management interface.

FastAPI, Uvicorn lifecycle control, port guarding, and persistent runtime status are new features. They do not exist in the current codebase.

## 1. Audit Summary

### Core architecture and code quality

- Platform selection currently happens at import time and is stored in mutable module globals. Unsupported synchronization platforms prevent even read-only NTP measurement from being imported.
- The package has duplicated `service` bindings, so direct service calls and `check_and_sync()` can use different backends after replacement or test injection.
- NTP measurement, threshold policy, service startup, privileged synchronization, and scheduling are tightly coupled. These responsibilities need explicit interfaces and independently testable results.
- The first successful unauthenticated NTPv3 response is accepted without quorum, outlier rejection, stratum/leap-state checks, delay/dispersion bounds, or a maximum permitted correction.
- Measurement servers are not passed through the synchronization abstraction. A host can measure against one authority and correct itself against another.
- A successful platform command is treated as successful synchronization without measuring the clock again.
- Linux provider fallback and error classification are incorrect; macOS can report success when commands are unavailable; Windows depends on English command output and does not wait for pending service states.
- Privileged subprocesses use bare executable names and have no timeouts. Command execution needs trusted path resolution, bounded output, typed failures, and deadlines.
- Public configuration is not validated. Invalid servers, ports, thresholds, cycles, timeouts, and non-finite numeric values can produce misleading failures or busy loops.

### Thread safety and state tracking

- Each `check_and_sync_thread()` call creates another daemon thread. There is no single owner, synchronization lock, or inter-process guard around a machine-global clock operation.
- The worker waits only after success. Repeated failures can immediately retry and generate CPU, network, subprocess, and logging storms.
- Permission failures terminate the worker without an observable failed state. Other errors are swallowed into logs.
- The stop event cannot interrupt an unbounded subprocess, so graceful shutdown cannot currently be guaranteed.
- There is no synchronized record of whether the worker is running, its active cycle/threshold, the latest measurement, the latest successful synchronization, or the latest outcome.

### FastAPI management interface

- A dedicated module/starter such as `ntp_anchor/api.py` is required. Importing it must not start a thread or server as a side effect.
- The interface must implement exactly the planned management routes: `/ping`, `/help`, `/start`, `/end`, `/close`, `/status`, `/set-cycle`, and `/set-thresh`.
- Route handlers must use one injected, thread-safe manager rather than mutate module globals.
- Blocking joins, NTP calls, and subprocess work must not block FastAPI's event loop.
- `/close` must request a normal Uvicorn shutdown after stopping the worker; it must never call `os._exit()`, kill the process abruptly, or leave the worker running.
- Because each Uvicorn worker would otherwise own separate in-memory state and a separate sync thread, the first implementation must enforce a single Uvicorn worker.
- Management routes, especially `/close`, must bind to loopback by default. Any non-loopback deployment must explicitly configure authentication and TLS or a trusted authenticated reverse proxy.

### Port safety

- Before Uvicorn starts, the launcher must validate the host/port and attempt to bind the designated TCP listening socket.
- If binding fails because the address is occupied, startup must stop immediately with an explicit `PortInUseError` (or equivalent), a clear host/port message, and a nonzero CLI exit code.
- Prefer retaining the successfully bound socket and passing it to Uvicorn. This reserves the port and avoids a check-then-bind race. If the chosen Uvicorn integration cannot reuse the checked socket, the actual Uvicorn bind must still be treated as authoritative and handled explicitly.
- The API TCP port and the remote NTP UDP port are different concerns. A TCP connection test must never be used to claim that an NTP server is reachable.

### Tests, CI, and documentation

- Four current test/support files are empty; the only substantive tests are six mocked Windows subprocess cases.
- There is no coverage for network response validation, Linux/macOS providers, state transitions, concurrent API requests, graceful shutdown, dynamic settings, port collisions, or FastAPI routes.
- There is no cross-platform CI matrix, coverage gate, lint/type/security check, build smoke test, or safely gated privileged integration suite.
- The README does not match current input, return, or exception behavior and has no server startup guide, endpoint reference, security model, port guidance, or dynamic configuration examples.

## 2. Target Runtime Architecture

### Core controller

Introduce one explicitly constructed controller (working name: `SyncManager`) that wraps the refactored measurement and synchronization services. It owns:

- One worker thread and one stop event.
- A lock for lifecycle and configuration state.
- A separate single-flight lock for machine-wide sync operations.
- A wake/reconfigure signal so cycle changes can take effect without creating overlapping threads.
- An immutable snapshot returned to API callers.
- A lazy/injected platform backend rather than an import-time global.

### Required public status snapshot

`GET /status` must return an atomically captured JSON object with these fields:

```json
{
  "is_running": false,
  "cycle": 300.0,
  "thresh": 0.5,
  "last_checked_at": null,
  "last_synced_at": null,
  "last_check_status": "never",
  "last_measured_offset": null
}
```

Field semantics:

- `is_running`: true only while the managed worker is alive and has not completed a stop transition.
- `cycle`: current validated periodic interval in seconds.
- `thresh`: current validated drift threshold in seconds.
- `last_checked_at`: timezone-aware UTC ISO-8601 completion time for the latest attempted check, or null before the first check.
- `last_synced_at`: timezone-aware UTC ISO-8601 time of the latest post-verified successful synchronization, not merely command invocation.
- `last_check_status`: a stable documented value such as `never`, `checking`, `within_threshold`, `sync_succeeded`, `query_failed`, `sync_failed`, or `verification_failed`.
- `last_measured_offset`: the newest validated offset for the latest check, or null if that check produced no valid measurement. Non-finite JSON values are prohibited.

Additional internal diagnostic state, such as a sanitized last error and consecutive failure count, may be maintained, but the required status response above must remain stable.

### FastAPI application and server starter

`ntp_anchor/api.py` should expose:

- `create_app(manager, shutdown_callback)` for deterministic dependency injection and tests.
- A launcher that validates configuration, acquires the listening socket, constructs `uvicorn.Config`/`uvicorn.Server`, injects a graceful shutdown callback, and runs the server.
- `main()` as the console-script entry point.
- No import-time server, socket, thread, or clock side effects.

If the module becomes too large, request/response models and lifecycle helpers may move into an `ntp_anchor.api` package later, while preserving the public starter contract.

## 3. Sequential Implementation Phases

## Phase 1: Core State & Thread Management

### 1.1 Refactor structural boundaries

1. Separate NTP measurement, synchronization policy, platform actuation, and worker scheduling into explicit components.
2. Replace import-time backend selection with a lazy factory or injected backend.
3. Remove duplicate module-level service bindings so every call flows through the same controller.
4. Keep read-only measurement importable even when the current OS has no synchronization backend.
5. Replace ambiguous `None` results with structured measurement and synchronization results.
6. Define typed errors for invalid configuration, exhausted queries, invalid responses, unavailable providers, permission denial, command failure, command timeout, and failed post-sync verification.
7. Remove logging side effects from exception constructors; log once at an operation boundary with traceback/incident context where appropriate.

### 1.2 Add validated runtime configuration

1. Validate NTP servers as a non-empty immutable collection of non-empty hostnames/IP addresses.
2. Validate NTP UDP ports and future API TCP ports as integers from 1 through 65535; reject Booleans and non-integer values.
3. Require finite `thresh >= 0`, finite `cycle > 0`, finite positive timeouts, and a finite maximum correction greater than or equal to `thresh`.
4. Reject `NaN`, infinities, empty iterables, bare strings passed where collections are expected, and undocumented `None` values.
5. Centralize defaults so server lists, thresholds, timeouts, and cycles are not duplicated across wrappers.

### 1.3 Implement the synchronized state tracker

1. Store lifecycle, cycle, threshold, timestamps, status, and offset behind one `threading.RLock` or equivalent synchronization primitive.
2. Return immutable snapshots; never expose the mutable internal state object.
3. Update measurement fields as one atomic operation so `/status` cannot combine values from different checks.
4. Use a monotonic clock for deadlines/scheduling and timezone-aware UTC for externally reported timestamps.
5. Set `last_synced_at` only after post-sync measurement confirms convergence.
6. Preserve a sanitized internal last-error value for logging/readiness without leaking raw subprocess output through `/status`.

### 1.4 Replace the raw thread helper with a managed lifecycle

1. Implement idempotent `start()`:
   - If stopped, transition through `STARTING` to `RUNNING` and create one named worker.
   - If already running, return the current running state without creating another thread.
   - Concurrent starts must produce at most one worker.
2. Implement idempotent `stop()`:
   - Signal the stop event.
   - Wake any cycle wait immediately.
   - Join to a documented deadline.
   - Return stopped only after the thread has exited.
   - Report an explicit timeout/failure rather than claiming `is_running: false` while the worker remains alive.
3. Wait after every iteration outcome. Use capped exponential backoff and jitter for repeated errors.
4. Prevent overlap between scheduled and direct synchronization with a single-flight lock.
5. Add an OS-level lock or explicitly retain the single-process restriction before permitting multiple application processes.
6. Make every subprocess bounded so `stop()` has a calculable upper bound.

### 1.5 Support dynamic cycle and threshold changes

1. Implement atomic `set_cycle(seconds)` and `set_thresh(seconds)` controller methods.
2. Preferred cycle behavior: preserve the current worker, update the value, signal a reconfiguration event/condition, and recompute its next wait immediately.
3. If restarting is ultimately necessary, stop/join the old worker before starting one replacement and preserve whether it was active. No two workers may overlap.
4. Threshold changes apply to the next policy decision without restarting the worker.
5. Validation failure leaves the old value and thread state unchanged.

### 1.6 Carry forward time-integrity and platform fixes

1. Collect and validate complete NTP measurements rather than accept the first offset.
2. Apply the agreed quorum/median and outlier policy, validate response quality, and enforce a maximum correction guard.
3. Ensure measurement and correction authorities follow the same configured trust policy.
4. Re-measure after synchronization and return success only after convergence.
5. Introduce one hardened command runner with trusted executable paths, a minimal environment, bounded output, and timeouts.
6. Correct Linux alternative-provider selection and error classification.
7. Make missing macOS tools a typed failure and support eligible fallbacks.
8. Poll Windows pending service states and avoid using English prose as the sole success signal.

### Phase 1 deliverables

- Refactored core interfaces and typed result/error contracts.
- Thread-safe `SyncManager` and immutable state snapshot.
- Idempotent start/stop and single-flight synchronization.
- Dynamic cycle/threshold update methods.
- Bounded retry, command execution, and shutdown behavior.
- Corrected platform-provider and post-sync verification behavior.

### Phase 1 validation criteria

- Concurrent calls to `start()` create exactly one thread.
- Repeated `start()` and `stop()` calls are safe and idempotent.
- Every success and failure path updates the snapshot atomically.
- Errors wait/back off rather than hot-loop.
- Stop completes within the documented bound under mocked timeout conditions.
- Cycle updates wake the existing worker and do not duplicate it.
- Threshold updates affect the next check and do not restart a stopped worker.
- Invalid updates preserve the previous state.
- Concurrent scheduled/manual sync attempts execute at most one platform mutation.
- Successful sync results include post-sync convergence; excessive or untrusted offsets never invoke a command.

## Phase 2: FastAPI Endpoints & Port Safety Check

### 2.1 Create the dedicated API module

1. Add `ntp_anchor/api.py` with an application factory and server starter.
2. Inject one `SyncManager` into routes through app state or FastAPI dependencies.
3. Inject a narrow shutdown callback/protocol so route tests never need a real Uvicorn server.
4. Do not start the worker automatically on import. If auto-start is later supported, it must be an explicit launcher option handled through FastAPI lifespan.
5. Use lifespan shutdown as a final safety net that stops and joins the worker.
6. Keep blocking stop/join or system operations off the async event loop by using synchronous handlers/threadpool execution or explicit offloading.

### 2.2 Implement the endpoint contract

#### `GET /ping`

- Purpose: side-effect-free liveness check.
- Parameters: none.
- Success: `200 OK`.
- Exact response:

```json
{"status": "pong"}
```

#### `GET /help`

- Purpose: self-documenting route inventory.
- Parameters: none.
- Success: `200 OK`.
- Return a stable structure such as:

```json
{
  "routes": [
    {
      "path": "/start",
      "method": "POST",
      "parameters": [],
      "description": "Start periodic NTP checking."
    }
  ]
}
```

- Build the response from route metadata or a single shared endpoint registry so it cannot silently drift from the actual routes.
- List every required management route, HTTP method, request parameter/body field, constraints, and description. The generated FastAPI `/docs` and `/openapi.json` may coexist but do not replace `/help`.

#### `POST /start`

- Purpose: start periodic background checking using the current cycle and threshold.
- Parameters: none.
- Success: `200 OK`.
- If stopped, start one worker and return `{"is_running": true}`.
- If already running, make no change and return the same `200 OK` response.
- Concurrent requests must remain idempotent and create one worker.
- Startup failure must return the mapped error and must not claim the worker is running.

#### `POST /end`

- Purpose: gracefully stop the background worker through its event.
- Parameters: none.
- Success: `200 OK`.
- If running, signal, wake, join, and then return `{"is_running": false}`.
- If already stopped, make no change and return the same `200 OK` response.
- If the worker cannot stop within its deadline, return an explicit timeout/error; do not return false while the thread is alive.

#### `POST /close`

- Purpose: gracefully stop the worker, then shut down Uvicorn.
- Parameters: none.
- Stop/join the worker first if it is running.
- On successful stop, respond with:

```json
{"is_running": false, "is_closing": true}
```

- Schedule the injected Uvicorn shutdown signal only after the HTTP response can be flushed.
- Set `uvicorn.Server.should_exit = true` (through the injected lifecycle adapter) and let FastAPI lifespan cleanup run.
- Never call `sys.exit()` inside the route, `os._exit()`, task-kill commands, or abrupt process termination.
- Make repeated/concurrent close requests safe and ensure the shutdown callback is invoked at most once.
- Restrict this endpoint to the configured trusted access boundary.

#### `GET /status`

- Purpose: return one atomic state snapshot.
- Parameters: none.
- Success: `200 OK`.
- Return exactly the required state fields:
  - `is_running`
  - `cycle`
  - `thresh`
  - `last_checked_at`
  - `last_synced_at`
  - `last_check_status`
  - `last_measured_offset`
- Define Pydantic response models so timestamps, nullability, finite floats, and field names remain stable.

#### `POST /set-cycle`

- Purpose: update the worker interval in seconds.
- JSON request:

```json
{"cycle": 60.0}
```

- Require a finite number greater than zero and enforce a documented upper bound.
- Success: `200 OK` with the accepted cycle and running state, for example `{"cycle": 60.0, "is_running": true}`.
- When running, preserve the active lifecycle and wake/reconfigure the worker immediately. If the implementation uses restart semantics, it must atomically stop/join/restart exactly one worker.
- When stopped, store the setting without starting a worker.

#### `POST /set-thresh`

- Purpose: update the drift threshold in seconds.
- JSON request:

```json
{"thresh": 0.25}
```

- Require a finite non-negative number that does not exceed the configured maximum correction.
- Success: `200 OK` with the accepted threshold and running state, for example `{"thresh": 0.25, "is_running": true}`.
- Apply atomically to the next check; do not restart the worker solely for a threshold update.

### 2.3 Define API errors and concurrency behavior

1. Return FastAPI/Pydantic `422` responses for invalid JSON, missing fields, wrong types, out-of-range values, `NaN`, and infinity.
2. Use `409 Conflict` only for genuine lifecycle/operation conflicts; normal duplicate `/start` and `/end` calls remain `200`.
3. Use `503` or `504` for unavailable backends and bounded operation/shutdown timeouts.
4. Return opaque `500` errors for unexpected failures and log an incident identifier server-side.
5. Never expose command lines, raw stderr/stdout, local paths, or tracebacks in JSON responses.
6. Serialize lifecycle mutations through the manager lock and use immutable snapshots for status reads.

### 2.4 Implement the pre-launch port guard

1. Validate bind host and port before constructing/running Uvicorn.
2. Attempt to create and bind the correct IPv4/IPv6 TCP socket with duplicate-listener reuse disabled.
3. On `EADDRINUSE`/the platform-equivalent collision:
   - Close any partially created socket.
   - Raise an explicit typed port-collision error containing the safe host/port.
   - Print a concise CLI error.
   - Exit nonzero without starting Uvicorn or the sync worker.
4. Prefer to keep the successful socket open and pass it to `uvicorn.Server.run(sockets=[...])`, eliminating the time-of-check/time-of-use window.
5. If socket handoff proves incompatible on a supported platform, retain the pre-check for diagnostics but still catch and translate the authoritative Uvicorn bind failure.
6. Test Windows and POSIX socket behavior; do not use settings such as `SO_REUSEPORT` that allow unintended duplicate listeners.
7. Keep NTP UDP reachability separate: validate its port, then use a bounded protocol request instead of a TCP port probe.

### 2.5 Secure server lifecycle defaults

1. Default to `127.0.0.1` and one Uvicorn worker.
2. Refuse or clearly fail configuration requesting more than one worker until inter-process state and locking exist.
3. Disable production auto-reload because reload supervisors can create duplicate process lifecycle effects.
4. Require explicit authentication/TLS or a trusted authenticated reverse proxy before allowing non-loopback binding.
5. Ensure Ctrl+C, service-manager termination, `/close`, and unexpected server shutdown all run the same idempotent manager cleanup.

### Phase 2 deliverables

- Dedicated FastAPI application factory and Uvicorn starter.
- All eight required endpoints and Pydantic request/response models.
- Graceful server shutdown adapter for `/close`.
- Pre-bound TCP port guard and explicit collision error.
- Loopback/single-worker safety defaults.

### Phase 2 validation criteria

- Every route returns the specified success payload and status code.
- `/help` accurately lists all required paths, methods, inputs, and descriptions.
- Duplicate/concurrent `/start` requests create one thread and return true.
- Duplicate/concurrent `/end` requests leave no live worker and return false.
- `/close` stops the worker, returns its response, signals shutdown once, and permits lifespan cleanup.
- `/status` never returns a torn snapshot during concurrent updates.
- Running and stopped cycle updates preserve their respective lifecycle states.
- Threshold changes take effect without spawning a thread.
- Invalid update requests are rejected without changing state.
- An occupied port prevents server and worker startup and produces the explicit collision error.
- A successful pre-bind socket remains reserved until Uvicorn owns/closes it.
- Liveness remains responsive while a mocked background check is blocked within its configured deadline.

## Phase 3: Dependencies & Entry Points

### 3.1 Update package metadata

1. Add compatible, bounded FastAPI and Uvicorn versions to `pyproject.toml`.
2. Prefer an `api` optional dependency group so read-only/core library users do not inherit a web stack:

```toml
[project.optional-dependencies]
api = [
    "fastapi>=<approved-minimum>,<<next-breaking-version>",
    "uvicorn>=<approved-minimum>,<<next-breaking-version>"
]
```

3. Add API test dependencies such as HTTPX/TestClient support to the existing test extra.
4. Document and test both core-only and `[api]` installations.
5. If product requirements instead make the web server mandatory, move both packages to runtime dependencies explicitly; do not leave the install contract ambiguous.

### 3.2 Add CLI entry points

1. Add a console script such as:

```toml
[project.scripts]
ntp-anchor-api = "ntp_anchor.api:main"
```

2. Support explicit options for at least:
   - `--host`
   - `--port`
   - `--cycle`
   - `--thresh`
   - repeatable/configured NTP servers
   - log level
3. Define precedence among CLI arguments, environment variables, and defaults.
4. Validate every option before binding a socket or creating a worker.
5. If API dependencies are missing, fail with a concise instruction to install `ntp-anchor[api]` rather than an opaque import traceback.
6. Optionally support `python -m ntp_anchor.api` through the same `main()` path so behavior cannot diverge.
7. Ensure CLI startup instantiates one manager, one app, one retained socket, and one Uvicorn server.

### 3.3 Package and process behavior

1. Ensure importing `ntp_anchor` or `ntp_anchor.api` never parses CLI arguments, binds a port, starts a worker, or mutates system time.
2. Expose package version metadata for status/logging without duplicating the version string.
3. Add supported Python/OS metadata and ensure dependency versions are covered by CI.
4. Define clear nonzero exit codes for invalid configuration, missing API dependencies, port collision, and unexpected server startup failure.

### Phase 3 deliverables

- FastAPI/Uvicorn and API-test dependency declarations.
- `ntp-anchor-api` console entry point and optional module invocation.
- Validated CLI/environment configuration path.
- Defined startup failure/exit-code behavior.
- Clean core-only and API-enabled package builds.

### Phase 3 validation criteria

- A clean core-only install can import and use measurement APIs.
- An API-enabled install can launch the CLI and serve `/ping`.
- Missing API extras produce the documented actionable error.
- CLI invalid values fail before socket/thread creation.
- CLI port collision returns the documented nonzero exit.
- Import smoke tests prove there are no server, socket, thread, or clock side effects.
- Built sdist/wheel installs expose the same console script and behavior as editable installs.

## Phase 4: Comprehensive Test Suite (`pytest`)

### 4.1 Unit-test layout

Populate or add:

- `tests/unit/test_network_ntp.py`: query exhaustion, validated response fields, quorum/median, outliers, maximum correction, and per-server errors.
- `tests/unit/test_config.py`: cycle, threshold, timeout, server, API TCP port, and NTP UDP port boundaries.
- `tests/unit/test_sync_manager.py`: atomic snapshots, state transitions, start/end idempotency, reconfiguration, backoff, single-flight locking, and bounded shutdown.
- `tests/unit/test_core_logic.py`: policy boundaries and post-sync verification.
- `tests/unit/test_command_runner.py`: absolute resolution, missing commands, permissions, output limits, nonzero exits, and timeouts.
- `tests/unit/test_linux_time_sync.py`: alternative provider selection and fallback.
- `tests/unit/test_darwin_time_sync.py`: unavailable tools, permission errors, and fallback.
- Extend `tests/unit/test_windows_time_sync.py`: pending states, readiness timeout, stopped service, stderr failures, and localization-independent results.
- `tests/unit/test_api.py`: full route and lifecycle contract.
- `tests/unit/test_port_safety.py`: pre-bind success, collision, cleanup, socket handoff, IPv4/IPv6, and invalid configuration.

### 4.2 Required endpoint test matrix

| Route | Required cases |
| --- | --- |
| `GET /ping` | Exact `{"status": "pong"}`; no manager/network side effects |
| `GET /help` | All eight routes; correct methods, body fields, constraints, descriptions; no stale entries |
| `POST /start` | Stopped, already running, simultaneous requests, thread-start failure |
| `POST /end` | Running, already stopped, simultaneous requests, join timeout |
| `POST /close` | Worker running/stopped, response before shutdown, callback once, cleanup failure |
| `GET /status` | Initial state, in-progress state, successful check, sync success, every failure status, concurrent reads |
| `POST /set-cycle` | Running/stopped, immediate wake, min/max boundaries, wrong type, zero, negative, NaN/infinity |
| `POST /set-thresh` | Running/stopped, next-check visibility, min/max boundaries, wrong type, negative, NaN/infinity |

Use an injected fake manager and shutdown adapter for route-unit tests. A unit test for `/close` must never terminate the pytest process.

### 4.3 Concurrency and state tests

1. Use barriers/events rather than timing-dependent sleeps wherever possible.
2. Prove many simultaneous starts create exactly one worker.
3. Prove many simultaneous stop/close calls perform one bounded join and one server shutdown signal.
4. Read `/status` while another thread updates all fields and verify snapshots are internally consistent.
5. Change cycle while the worker is waiting and verify it wakes/recomputes without overlap.
6. Change threshold during a check and document whether the current or next policy decision owns the update; test that exact rule.
7. Hold the sync lock with a fake operation and prove a second request is rejected/serialized as designed.
8. Simulate command timeout and verify stop eventually completes within its upper bound.

### 4.4 Port and server lifecycle integration tests

1. Hold a real local listening socket, launch the CLI against that port, and assert explicit refusal/nonzero exit.
2. Verify a successful guard passes its retained socket to Uvicorn and no competing process can bind it.
3. Start the API in a subprocess on a controlled loopback port, call `/ping`, then `/close`, and assert graceful process exit.
4. Repeat `/close` safely without allowing it to terminate the parent pytest runner.
5. Send Ctrl+C/termination to the subprocess and verify the worker is stopped by lifespan cleanup.
6. Verify a TCP port check is never used for NTP UDP health.

### 4.5 Network and privileged integration tests

1. Prefer a local fake NTP responder for deterministic packet-level integration tests.
2. Mark live-network tests explicitly and require an opt-in environment flag.
3. Keep real clock-changing tests disabled by default and require:
   - Explicit marker selection.
   - A dedicated environment flag.
   - A disposable isolated runner/VM.
   - Appropriate privileges.
4. Test platform adapters independently without starting the FastAPI server unless the integration case specifically covers the full stack.
5. Confirm ordinary `pytest` cannot mutate the host clock or require elevation.

### 4.6 CI quality gates

1. Run supported Python versions on Windows, Linux, and macOS.
2. Run default non-privileged unit/API tests on every matrix job.
3. Enforce formatting, linting, type checking, security scanning, and an agreed branch-coverage floor for core/controller/API logic.
4. Build sdist/wheel, install into a clean environment, and smoke-test imports and the CLI.
5. Add a separate manually triggered, isolated workflow for privileged integration tests.
6. Fail CI if route/OpenAPI/help contracts diverge or if default pytest collection includes clock-changing tests.

### Phase 4 deliverables

- Comprehensive deterministic pytest suite.
- FastAPI route and concurrency coverage.
- Real port-collision and graceful-shutdown integration coverage.
- Cross-platform non-privileged CI and separately gated privileged validation.
- Coverage/static/build/release safety gates.

### Phase 4 validation criteria

- All route responses, status codes, schemas, and side effects match this plan.
- Thread/concurrency tests pass repeatedly without sleeps or flakes.
- Default tests perform no external network access or clock mutation.
- Port collision and `/close` are proven with real subprocess integration.
- CI passes on every supported OS/Python combination.
- Built artifacts pass clean-install import and CLI smoke tests.

## Phase 5: Documentation (`README.md`)

### 5.1 Correct the core package documentation

1. Replace inaccurate signatures, accepted inputs, return values, and exception claims with the implemented structured contracts.
2. Document supported operating systems, Python versions, provider requirements, and privilege requirements.
3. Explain source trust, response validation, quorum/outlier policy, maximum correction, post-sync verification, and step/slew implications for trading timestamps.
4. Explain thread lifecycle, single-flight synchronization, retry/backoff, state meanings, and graceful shutdown.
5. Recommend keeping the trading/API process unprivileged and delegating privileged correction through a narrowly controlled system service where possible.

### 5.2 Add installation and startup guides

1. Show core-only and API-enabled installation commands, including `.[api]` if the optional-extra design is selected.
2. Document CLI and `python -m` startup examples.
3. Document every CLI option, environment variable, default, validation rule, and precedence.
4. State that the default bind is loopback and the initial implementation supports exactly one Uvicorn worker.
5. Document authentication/TLS expectations before non-loopback exposure.
6. Explain the pre-launch port guard, explicit occupied-port error, and distinction between API TCP ports and NTP UDP reachability.

### 5.3 Add the endpoint reference

For every endpoint, document:

- HTTP method and path.
- Purpose and side effects.
- Request body/parameters and validation.
- Exact success response example.
- Relevant error statuses.
- Idempotency and concurrency semantics.
- Security restrictions.

Include all eight endpoints:

- `GET /ping`
- `GET /help`
- `POST /start`
- `POST /end`
- `POST /close`
- `GET /status`
- `POST /set-cycle`
- `POST /set-thresh`

### 5.4 Add runnable examples

1. Add `curl` examples for every endpoint, including JSON bodies and content type for both setters.
2. Add Python examples using a documented client library for ping, start, status, settings changes, end, and close.
3. Show representative initial, running, successful-check, synchronized, and failed `/status` payloads.
4. Warn that `/close` shuts down the management process and should be restricted.
5. Include troubleshooting examples for occupied ports, invalid settings, insufficient OS privileges, unavailable NTP sources, failed post-sync verification, and missing API extras.

### 5.5 Add contributor/testing guidance

1. Document unit, API, live-network, and privileged test commands separately.
2. Explain pytest markers and the environment guards required for integration tests.
3. Provide the local equivalents of CI formatting, lint, type, security, coverage, and build checks.
4. Replace mutable `@main` installation guidance with a tagged release or pinned revision.

### Phase 5 deliverables

- Accurate core API and operational safety documentation.
- Complete FastAPI startup/configuration/endpoint reference.
- Runnable `curl` and Python examples.
- Port-collision, security, privilege, state, and troubleshooting guidance.
- Reproducible development and test instructions.

### Phase 5 validation criteria

- Every README command and code example runs against the built package.
- Every endpoint example matches the tested Pydantic schema and exact route behavior.
- `/help`, OpenAPI, tests, and the README list the same methods, parameters, and descriptions.
- Installation instructions work in clean core-only and API-enabled environments.
- A new operator can start the server, detect a port conflict, inspect status, change cycle/threshold, start/end the worker, and close the server without reading source code.
- Security, privilege, single-worker, time-integrity, and clock-mutation warnings are prominent.

## 4. Final Acceptance Checklist

- [ ] The core no longer relies on duplicated import-time mutable service state.
- [ ] NTP responses and all numeric/port configuration are validated.
- [ ] Unsafe corrections are blocked and successful syncs are verified afterward.
- [ ] One thread-safe manager owns one worker and one clock mutation at a time.
- [ ] Runtime state fields are atomically updated and accurately returned by `/status`.
- [ ] Cycle and threshold changes are validated, atomic, and preserve the intended running/stopped state.
- [ ] All eight required FastAPI routes implement their documented idempotency and response contracts.
- [ ] `/close` stops the worker and requests graceful Uvicorn shutdown without abrupt process termination.
- [ ] A retained pre-bound socket prevents port-collision startup and reduces bind races.
- [ ] The server defaults to loopback and one Uvicorn worker.
- [ ] Core-only and API-enabled installations, entry points, and missing-extra errors are tested.
- [ ] Default pytest and CI runs never change the host clock or require privileges.
- [ ] Cross-platform tests, API/concurrency tests, real port-collision tests, and subprocess shutdown tests pass.
- [ ] README examples and endpoint documentation match tested behavior.

## Definition of Done

The roadmap is complete only when the management interface can safely start, observe, reconfigure, stop, and close a single background synchronization worker; port collisions are rejected before server startup; time synchronization is validated and post-verified; every blocking operation has a deadline; default tests never mutate the host clock; and the package, CLI, API, tests, and README all describe the same behavior.

