# ntp-anchor

`ntp-anchor` measures local clock offset against a validated quorum of NTP
sources and can ask the operating system to correct the clock. It also provides
an optional, loopback-only FastAPI management service for one periodic worker.

> [!WARNING]
> Changing the host clock can reorder wall-clock timestamps, expire orders or
> credentials early, and invalidate latency measurements. The supported NTP
> queries do not authenticate a server. Treat synchronization as a privileged
> infrastructure operation: use independent sources you trust, test on a
> disposable host, and keep the trading process and management API unprivileged
> where possible.

## Requirements and supported platforms

- Python 3.12.10 or newer (package metadata covers Python 3.12-3.14).
- UDP access from the host to each configured NTP source. The default NTP port
  is `123`.
- Windows, Linux, or macOS for clock correction. Read-only NTP measurement is
  importable on other operating systems, but correction raises
  `UnsupportedOSError`.
- Administrator/root-equivalent permission for most clock-service operations.

The correction backend uses these authority-aware providers:

| Operating system | Providers tried for correction | Notes |
| --- | --- | --- |
| Windows | built-in `w32time` via `sc.exe` and `w32tm.exe` | The service is polled through numeric states until it is ready. |
| Linux | `ntpdate`, `sntp`, then `chronyd` | At least one tool that accepts the measured authorities must be installed. |
| macOS | `sntp`, `ntpdate`, `chronyd`, then `systemsetup` | Missing tools are reported as an unavailable provider, not as success. |

System commands are resolved only from trusted system directories, never run
through a shell, receive a minimal environment, have bounded output, and have a
deadline. A command returning successfully is still not sufficient: ntp-anchor
remeasures the clock before reporting synchronization success.

## Installation

From a local checkout, install only the measurement/core package:

```console
python -m pip install .
```

Install the management API and CLI as well:

```console
python -m pip install ".[api]"
```

The API extra currently selects `FastAPI>=0.142,<1`, `Pydantic>=2,<3`, and
`Uvicorn>=0.53,<1`. For development, install the API and test/quality
dependencies together:

```console
python -m pip install -e ".[api,test]"
```

After version 0.2.0 is published to your package index, pin that immutable
release for deployment rather than using a moving branch:

```console
python -m pip install "ntp-anchor[api]==0.2.0"
```

Git-based deployments must similarly record and install a reviewed full
40-character release commit SHA in their lock file. Never deploy from a moving
`@main` reference.

## Core API

New code should construct and own a `SyncManager`. The module-level helpers are
retained as compatibility adapters around one lazy process-local manager.
Importing `ntp_anchor` does not query the network, select a platform backend,
start a thread, bind a socket, or modify the clock.

### Read-only measurement

```python
from ntp_anchor import SyncConfig, measure_ntp

config = SyncConfig(
    servers=("time.cloudflare.com", "time.google.com", "pool.ntp.org"),
    quorum=2,
    query_timeout=2.0,
)
result = measure_ntp(config)

print(result.offset)          # consensus offset in seconds
print(result.servers)         # sources retained as inliers
print(result.measurements)    # validated per-source measurements
print(result.rejected)        # sanitized rejected-source metadata
print(result.queried_at)      # timezone-aware UTC datetime
```

`measure_ntp(config) -> NTPQueryResult` returns structured source and quality
metadata. `get_ntp_time_offset(...) -> float` returns only the same validated
consensus offset for compatibility. Neither returns `None`; inability to obtain
a quorum raises `NTPQueryError`. A single-server deployment must explicitly set
`quorum=1`.

### Managed checking and correction

```python
from ntp_anchor import SyncConfig, create_sync_manager

manager = create_sync_manager(
    SyncConfig(
        cycle=300.0,
        thresh=0.5,
        max_correction=5.0,
    )
)

# One immediate, serialized check. This may correct the system clock.
result = manager.check_once()
print(result.status.value)
print(result.measured_offset)
print(result.correction_attempted)

# Or own one periodic worker.
manager.start()                # idempotent
manager.set_cycle(60.0)        # wakes and reschedules the existing worker
manager.set_thresh(0.25)       # applies to the next policy decision
snapshot = manager.snapshot()  # immutable, atomic state
manager.stop()                 # signals, wakes, and joins; idempotent
```

The principal public contracts are:

| API | Result |
| --- | --- |
| `SyncManager(config, backend=...)` | An explicitly owned, injectable controller. Without a backend or backend factory it remains measurement-only. |
| `create_sync_manager(config)` | An explicitly owned controller with lazy selection of the current operating-system backend. |
| `manager.measure_once()` | `NTPQueryResult`; performs no correction. |
| `manager.check_once()` | `SyncResult` with `within_threshold` or post-verified `sync_succeeded`; operational failures raise a typed exception and update state. |
| `manager.start()` / `manager.stop(timeout=None)` | An immutable `WorkerSnapshot`; duplicate calls are safe. A stop that misses its deadline raises `WorkerStopTimeoutError` and continues to report the live worker truthfully. |
| `manager.set_cycle(seconds)` | Atomically stores a finite value greater than zero and wakes a waiting worker without replacing it. The API additionally caps this at 86,400 seconds. |
| `manager.set_thresh(seconds)` | Atomically stores a finite, non-negative value no greater than `max_correction`; it does not start or restart a worker. |
| `manager.snapshot()` | The seven-field public state used by `GET /status`. |
| `manager.diagnostics()` | Sanitized internal lifecycle, failure-count, and last-error data; this is not returned by the HTTP API. |
| `check_and_sync(...)` | Compatibility wrapper returning `SyncResult`. Per-call overrides cannot be combined with an explicit `manager`. |
| `check_and_sync_thread(...)` | Compatibility wrapper returning `(thread, stop_event)`; prefer owning a manager and calling `stop()`. |
| `start_time_service()` / `sync_time()` | Legacy direct provider operations returning `None`; prefer `check_once()` because only it applies the policy and post-sync verification. |

The compatibility function signatures are:

```text
measure_ntp(config=None, *, client=None) -> NTPQueryResult
get_ntp_time_offset(
    ntp_server=None, *, ntp_port=123, timeout=2.0, quorum=2
) -> float
check_and_sync(
    ntp_server=None,
    thresh=None,
    auto_start=None,
    *,
    quorum=None,
    max_correction=None,
    manager=None,
) -> SyncResult
check_and_sync_thread(
    ntp_server=None,
    thresh=None,
    cycle=None,
    auto_start=None,
    *,
    quorum=None,
    max_correction=None,
) -> tuple[threading.Thread, threading.Event]
start_time_service() -> None
sync_time() -> None
```

`NTPQueryResult` contains `offset`, accepted `measurements`, sanitized
`rejected` records, and `queried_at`. `SyncResult` contains `status`, the
original `measurement`, optional post-sync `verification`, `checked_at`,
`synced_at`, and `correction_attempted`, plus a `measured_offset` property.

Expected failures are available through `ntp_anchor.exceptions` and derive
from `NTPAnchorError`. Important types include
`ConfigurationError`, `NTPQueryError`, `NTPResponseValidationError`,
`UnsafeCorrectionError`, `SyncVerificationError`,
`TimeServiceUnavailableError`, `TimeServicePermissionError`,
`TimeServiceCommandError`, `TimeServiceTimeoutError`, `WorkerStateError`,
`WorkerStopTimeoutError`, and `UnsupportedOSError`. Callers should not infer
success from the absence of a Boolean `False`; successful operations now return
structured results, while failures raise.

### Configuration defaults

`SyncConfig` is immutable. Invalid updates fail before changing the active
configuration.

| Field | Default | Rule or meaning |
| --- | ---: | --- |
| `servers` | three public sources | Non-empty tuple of 1-16 unique hostnames or IP addresses. Bare strings, schemes, paths, whitespace, and duplicates are rejected. |
| `ntp_port` | `123` | Integer 1-65,535. Measurement may use another UDP port, but platform correction is refused for a non-standard port. |
| `ntp_version` | `3` | Integer `3` or `4`; a response must match. |
| `quorum` | `2` | Integer between 1 and the number of unique servers. |
| `thresh` | `0.5` s | Finite, non-negative correction threshold. |
| `cycle` | `300.0` s | Finite and greater than zero. |
| `query_timeout` | `2.0` s | Positive deadline per NTP source. |
| `max_delay` | `1.0` s | Maximum response delay and absolute root delay. |
| `max_dispersion` | `1.0` s | Maximum root dispersion. |
| `max_reference_age` | `86400.0` s | Maximum age of the source reference timestamp. |
| `max_sample_deviation` | `0.25` s | Maximum distance from the first-pass median before a sample is an outlier. |
| `max_correction` | `5.0` s | Hard correction guard; must be at least `thresh`. |
| `max_stratum` | `15` | Highest accepted synchronized stratum. |
| `verification_attempts` | `3` | Post-command measurement attempts. |
| `verification_interval` | `0.25` s | Delay between verification attempts. |
| `auto_start_service` | `true` | Prepare the selected provider before requesting correction. |
| `command_timeout` | `10.0` s | Positive deadline for platform commands. |
| `retry_base_delay` / `retry_max_delay` | `1.0` / `60.0` s | Bounds for exponential failure backoff. |
| `retry_jitter` | `0.2` | Jitter fraction from 0 through 1. |
| `single_flight_timeout` | `5.0` s | Maximum wait for the process-wide measurement/correction lock. |
| `stop_timeout` | `90.0` s | Default worker join deadline. |

Booleans are rejected where numbers or ports are expected, and all numeric
values must be finite. `NaN`, positive/negative infinity, empty source lists,
and undocumented `None` values are invalid.

## Time-integrity and lifecycle model

For each measurement, ntp-anchor queries every configured authority once. It
rejects malformed or non-finite data, excessive delay/dispersion, leap state 3,
unsynchronized or excessive strata, mismatched protocol versions, non-server
mode, stale/future references, and inconsistent exchange timestamps. It then:

1. Requires the configured quorum of valid responses.
2. Computes their median offset.
3. Rejects samples farther than `max_sample_deviation` from that median.
4. Requires the quorum again and uses the remaining median as consensus.
5. Makes no correction when `abs(offset) <= thresh`.
6. Refuses correction when the consensus, or too many candidate authorities,
   exceed `max_correction`.
7. Passes the validated inlier authorities to the platform provider.
8. Remeasures and sets `last_synced_at` only after the offset converges within
   the threshold.

This policy reduces accidental bad-source influence; it does not add
cryptographic authentication and does not protect against compromised sources
that agree with one another. Prefer independently operated, explicitly chosen
servers rather than relying only on a shared DNS pool.

One named daemon worker belongs to each manager. Concurrent `start()` calls
create at most one thread. NTP measurement, provider preparation, clock
mutation, and verification share a process-wide single-flight lock, including
across manager instances. After success the worker waits for `cycle`; after any
failure it uses capped exponential backoff and jitter, so failures do not hot
loop. A cycle update wakes the current wait. A threshold update made during a
check applies to the next check because each operation captures an immutable
configuration at its start.

`stop()` wakes scheduled and verification waits and joins to a deadline. NTP
queries and system commands are independently bounded, but a timed-out stop
does not pretend the worker is gone. API lifespan shutdown, Ctrl+C/service
termination, and `POST /close` all use the same idempotent cleanup path.

The package coordinates one process only. The API therefore supports exactly
one Uvicorn worker and no auto-reload. Do not create multiple processes that can
change the same host clock until an external machine-wide lock is added.

### Trading-system considerations

ntp-anchor does not promise whether the selected operating-system provider will
step or slew. A step can make wall time jump forward or backward; a slew makes
wall time run temporarily faster or slower. Use a monotonic clock for elapsed
durations and deadlines, retain venue/exchange timestamps separately, and
perform correction during a controlled maintenance or trading pause. A recent
`last_synced_at` means post-sync verification succeeded at that instant, not
that subsequent drift is impossible.

Where possible, run the trading engine and API without clock-setting rights and
delegate correction to a narrowly scoped local system service. Limit that
service to approved executables and NTP authorities. Never grant a
network-exposed web process broad administrator/root access.

## Management API

The API is optional. Its launcher defaults to `127.0.0.1:8000`, pre-binds and
retains the TCP listening socket, and hands that same socket to Uvicorn. The
worker is stopped when the server exits. Importing `ntp_anchor.api` has no
server, socket, thread, argument-parsing, or clock side effects.

### Starting the server

The console script and module invocation use the same configuration path:

```console
ntp-anchor-api --host 127.0.0.1 --port 8000
```

```console
python -m ntp_anchor.api --host 127.0.0.1 --port 8000
```

A more explicit launch can repeat `--ntp-server`:

```console
ntp-anchor-api --cycle 60 --thresh 0.25 \
  --ntp-server time.cloudflare.com \
  --ntp-server time.google.com \
  --ntp-server pool.ntp.org \
  --quorum 2 --log-level info
```

Command-line values take precedence over environment values, which take
precedence over defaults. An option supplied on the CLI replaces the
corresponding environment value; repeatable CLI `--ntp-server` values replace
the complete comma-separated environment list.

| CLI option | Environment variable | Default | Validation |
| --- | --- | --- | --- |
| `--host IP` | `NTP_ANCHOR_API_HOST` | `127.0.0.1` | Explicit, unbracketed loopback IPv4 or IPv6 address only. |
| `--port PORT` | `NTP_ANCHOR_API_PORT` | `8000` | Integer 1-65,535; this is the API TCP port. |
| `--cycle SECONDS` | `NTP_ANCHOR_CYCLE` | `300.0` | Finite number greater than 0 and no greater than 86,400. |
| `--thresh SECONDS` | `NTP_ANCHOR_THRESH` | `0.5` | Finite number from 0 through the default `max_correction` of 5.0. |
| `--ntp-server HOST` (repeatable) | `NTP_ANCHOR_NTP_SERVERS` (comma-separated) | `pool.ntp.org,time.google.com,time.cloudflare.com` | 1-16 unique, non-empty hostnames/IP addresses; no scheme, path, URI brackets, or embedded whitespace. Spaces around comma-separated environment entries are trimmed. |
| `--ntp-port PORT` | `NTP_ANCHOR_NTP_PORT` | `123` | Integer 1-65,535; this is the remote NTP UDP port. |
| `--quorum COUNT` | `NTP_ANCHOR_QUORUM` | `2` | Integer from 1 through the number of configured servers. |
| `--log-level LEVEL` | `NTP_ANCHOR_LOG_LEVEL` | `info` | One of `critical`, `error`, `warning`, `info`, `debug`, or `trace`. |
| `--version` | none | installed package version | Prints the version and exits without binding a socket. |

For example:

```console
NTP_ANCHOR_API_PORT=8010 NTP_ANCHOR_CYCLE=60 ntp-anchor-api
```

On PowerShell:

```powershell
$env:NTP_ANCHOR_API_PORT = "8010"
$env:NTP_ANCHOR_CYCLE = "60"
ntp-anchor-api
```

All values are validated before any socket is bound or worker is created. Exit
codes are stable for service managers and scripts:

| Exit code | Meaning |
| ---: | --- |
| `0` | Clean server shutdown or `--version`/help output. |
| `1` | Unexpected startup/server failure. |
| `2` | Invalid CLI or environment configuration. |
| `3` | API dependencies are missing; install `ntp-anchor[api]`. |
| `4` | The requested API TCP address is already occupied. |
| `130` | Keyboard interrupt. |

The server starts with the synchronization worker stopped. Call `POST /start`
when periodic checks should begin; merely launching or importing the API never
changes the clock.

### Security boundary and port safety

The built-in launcher accepts any explicit loopback IPv4 or IPv6 address, one
worker, and no reload mode; its default is `127.0.0.1`. It does not provide
application-layer authentication or TLS. Do not expose it through port
forwarding or a permissive proxy. A future/custom non-loopback deployment must
put authentication and TLS, or a trusted authenticated reverse proxy, in front
of every route and must inject an equally strict `/close` authorization policy.

Loopback binding is an access boundary, not user authentication: every local
process that can reach the port can call the routes. Apply host firewall and
process-account controls even for a loopback deployment.

`POST /close` has an additional peer-address check and is loopback-only by
default. It stops the worker before sending a response and requests Uvicorn
shutdown only after the response body can be flushed. It never calls
`sys.exit()`, `os._exit()`, or an operating-system task killer.

Before Uvicorn starts, the launcher validates the address and reserves the TCP
port with duplicate-listener reuse disabled. An occupied address raises
`PortInUseError`, prints the safe host/port, and exits with code 4 without
starting a worker. This API TCP port is unrelated to NTP reachability: NTP uses
bounded UDP protocol requests, and a successful TCP connection test says
nothing about an NTP server.

FastAPI's generated `/docs` and `/openapi.json` remain available. `/help` is the
stable management-route inventory used by this package.

### Endpoint summary

| Method and path | Purpose | Side effect |
| --- | --- | --- |
| `GET /ping` | Liveness | None. |
| `GET /help` | List the eight management routes and constraints | None. |
| `POST /start` | Start periodic checking | Creates at most one worker. |
| `POST /end` | Gracefully stop periodic checking | Signals, wakes, and joins the worker. |
| `POST /close` | Stop the worker and gracefully close Uvicorn | Terminates the management process after flushing the response. |
| `GET /status` | Return one atomic public snapshot | None. |
| `POST /set-cycle` | Update the periodic interval | Stores the value and wakes a waiting worker. |
| `POST /set-thresh` | Update the drift threshold | Stores the value for the next policy decision. |

Common failures are `422` for malformed or invalid request data, `409` when a
lifecycle mutation races with server shutdown, `503` for an unavailable worker
or platform operation, `504` for a bounded stop/operation timeout, and an opaque
`500` with a server-side incident ID for an unexpected failure. JSON errors do
not expose command lines, subprocess output, local paths, or tracebacks.

#### `GET /ping`

Side-effect-free liveness check. It takes no parameters and remains independent
of worker and NTP state.

```console
curl -s http://127.0.0.1:8000/ping
```

```json
{"status":"pong"}
```

#### `GET /help`

Returns all management routes from the same registry used to register them.
With the default `max_correction=5.0`, the exact data is:

```console
curl -s http://127.0.0.1:8000/help
```

```json
{
  "routes": [
    {"path":"/ping","method":"GET","parameters":[],"description":"Check whether the management API is responsive."},
    {"path":"/help","method":"GET","parameters":[],"description":"List all management routes and their request constraints."},
    {"path":"/start","method":"POST","parameters":[],"description":"Start periodic NTP checking."},
    {"path":"/end","method":"POST","parameters":[],"description":"Gracefully stop periodic NTP checking."},
    {"path":"/close","method":"POST","parameters":[],"description":"Stop periodic checking and gracefully close the management server from a trusted client."},
    {"path":"/status","method":"GET","parameters":[],"description":"Return one atomic NTP worker state snapshot."},
    {"path":"/set-cycle","method":"POST","parameters":[{"name":"cycle","location":"body","required":true,"type":"number","constraints":{"finite":true,"exclusive_minimum":0.0,"maximum":86400.0}}],"description":"Update the periodic NTP check interval."},
    {"path":"/set-thresh","method":"POST","parameters":[{"name":"thresh","location":"body","required":true,"type":"number","constraints":{"finite":true,"minimum":0.0,"maximum":5.0}}],"description":"Update the clock-drift threshold for the next check."}
  ]
}
```

The threshold maximum reflects the manager's configured `max_correction`, so a
non-default manager changes that number in both `/help` and OpenAPI.

#### `POST /start`

Starts periodic checking with the current settings. It has no body. Calls are
idempotent; repeated or simultaneous requests return `200` and create at most
one worker. A worker-start failure returns `503`; a start after closing begins
returns `409`.

```console
curl -s -X POST http://127.0.0.1:8000/start
```

```json
{"is_running":true}
```

#### `POST /end`

Signals, wakes, and joins the worker. It has no body. Calls are idempotent;
already-stopped and simultaneous requests return the same `200` response. A
missed join deadline returns `504` and never falsely reports `false`.

```console
curl -s -X POST http://127.0.0.1:8000/end
```

```json
{"is_running":false}
```

#### `POST /close`

Stops the worker, returns the response below, and then requests graceful server
shutdown. The shutdown callback is invoked at most once across concurrent
requests. The default authorization policy returns `403` to a non-loopback
peer; a stop timeout returns `504`. Treat this as a privileged administrative
operation and restrict it even when the other read-only routes are proxied.

```console
curl -s -X POST http://127.0.0.1:8000/close
```

```json
{"is_running":false,"is_closing":true}
```

#### `GET /status`

Returns exactly one atomically captured seven-field snapshot. It does not
include raw diagnostics or errors.

```console
curl -s http://127.0.0.1:8000/status
```

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

#### `POST /set-cycle`

Accepts one strict JSON number greater than zero and no greater than 86,400
seconds. Numeric strings, Booleans, extra fields, `NaN`, and infinities return
`422`. A valid update does not start a stopped worker. For a running worker it
wakes and reschedules the same thread. A mutation after closing begins returns
`409`.

```console
curl -s -X POST http://127.0.0.1:8000/set-cycle \
  -H "Content-Type: application/json" \
  -d '{"cycle":60.0}'
```

```json
{"cycle":60.0,"is_running":true}
```

#### `POST /set-thresh`

Accepts one strict, finite, non-negative JSON number no greater than the
manager's `max_correction`. It applies atomically to the next check and does not
restart or start a worker. Wrong types, extra fields, negative values, `NaN`,
infinities, and values above the correction guard return `422`; a mutation
after closing begins returns `409`.

```console
curl -s -X POST http://127.0.0.1:8000/set-thresh \
  -H "Content-Type: application/json" \
  -d '{"thresh":0.25}'
```

```json
{"thresh":0.25,"is_running":true}
```

### Status values and examples

`last_check_status` is one of:

| Value | Meaning |
| --- | --- |
| `never` | No check has begun. |
| `checking` | A check is in progress; the current check has not published an offset yet. |
| `within_threshold` | Consensus was within `thresh`; no correction was attempted. |
| `sync_succeeded` | Correction was requested and a later measurement converged within `thresh`. |
| `query_failed` | No trustworthy quorum was obtained. |
| `operation_busy` | The process-wide single-flight lock was unavailable before its deadline. |
| `unsafe_correction` | The measured correction exceeded the configured safety policy. |
| `sync_failed` | Provider preparation or the correction command failed. |
| `verification_failed` | The correction command ran, but remeasurement did not prove convergence. |
| `cancelled` | A worker stop interrupted an active check at a safe cancellation point. |
| `worker_failed` | Worker creation or the worker loop failed unexpectedly. |

Timestamps are timezone-aware UTC ISO 8601 values. `last_checked_at` is the
completion time of the latest attempt. `last_synced_at` is preserved across
later checks and changes only after verified success. `last_measured_offset` is
the newest validated offset for that check, or `null` when no valid measurement
was produced. Non-finite numbers are never emitted.

Representative running check:

```json
{"is_running":true,"cycle":300.0,"thresh":0.5,"last_checked_at":null,"last_synced_at":null,"last_check_status":"checking","last_measured_offset":null}
```

Representative successful check without correction:

```json
{"is_running":true,"cycle":300.0,"thresh":0.5,"last_checked_at":"2026-10-03T18:00:00Z","last_synced_at":null,"last_check_status":"within_threshold","last_measured_offset":0.012}
```

Representative post-verified synchronization:

```json
{"is_running":true,"cycle":300.0,"thresh":0.5,"last_checked_at":"2026-10-03T18:05:00Z","last_synced_at":"2026-10-03T18:05:00Z","last_check_status":"sync_succeeded","last_measured_offset":0.004}
```

Representative failed query after an earlier successful synchronization:

```json
{"is_running":true,"cycle":300.0,"thresh":0.5,"last_checked_at":"2026-10-03T18:10:00Z","last_synced_at":"2026-10-03T18:05:00Z","last_check_status":"query_failed","last_measured_offset":null}
```

### Python client example

Install HTTPX if the calling environment does not already provide it:

```console
python -m pip install "httpx>=0.28,<1"
```

Then manage a local server through the same public contract:

```python
import httpx

with httpx.Client(base_url="http://127.0.0.1:8000", timeout=10.0) as client:
    print(client.get("/ping").raise_for_status().json())
    print(client.post("/start").raise_for_status().json())
    print(client.get("/status").raise_for_status().json())
    print(
        client.post("/set-cycle", json={"cycle": 60.0})
        .raise_for_status()
        .json()
    )
    print(
        client.post("/set-thresh", json={"thresh": 0.25})
        .raise_for_status()
        .json()
    )
    print(client.post("/end").raise_for_status().json())
    # This response is flushed before the management process shuts down.
    print(client.post("/close").raise_for_status().json())
```

## Troubleshooting

### The API address is already in use

For example, the CLI reports
`error: API TCP address 127.0.0.1:8000 is already in use.` and exits with code
4. Stop the other listener or choose another loopback port. The guard fails
before Uvicorn or the sync worker starts. Do not diagnose this with an NTP
server probe: the API listener is TCP, while NTP source traffic is UDP.

### A setting is rejected

CLI/environment configuration is validated before a socket is bound and exits
with code 2 on failure. HTTP setters accept JSON numbers, not strings; reject
Booleans and non-finite values; cap `cycle` at 86,400; and require
`0 <= thresh <= max_correction`. A failed update preserves the prior
configuration and running/stopped state.

### Clock correction reports insufficient privileges

Read-only measurement normally needs no elevation, but operating-system time
services generally do. Prefer a narrowly scoped privileged service rather than
running the whole API or trading engine as Administrator/root. Confirm that the
selected provider is installed and allowed by host policy.

### NTP sources are unavailable

Check DNS, outbound UDP, firewall rules, the NTP port, and whether enough
independent sources can satisfy `quorum`. The worker records `query_failed` and
backs off; it does not spin continuously. A TCP port probe is not an NTP health
check.

### Synchronization does not verify

`verification_failed` means a provider command completed but later consensus
remained outside `thresh`, or a trustworthy verification quorum could not be
obtained. Check provider logs, source agreement, permissions, the threshold,
and the command/query deadlines. Do not treat command exit code zero as proof
that the clock is synchronized.

### The API command or module cannot import FastAPI/Pydantic/Uvicorn

The web stack is optional. Both startup forms provide missing-extra guidance;
the console command exits with code 3. Install it in the active interpreter:

```console
python -m pip install ".[api]"
```

Then confirm that `python` and `ntp-anchor-api` resolve to the same environment.

## Contributing and testing

Install the complete development environment first:

```console
python -m pip install -e ".[api,test]"
```

The pytest markers separate safe deterministic coverage from tests requiring
external state:

| Marker | Scope and default behavior |
| --- | --- |
| `unit` | Isolated unit/API tests using injected fakes; runs by default. |
| `integration` | Deterministic local integration tests; runs by default. |
| `server_integration` | Loopback subprocess and real local socket lifecycle tests; runs by default. |
| `live_network` | Contacts live NTP infrastructure; deselected by default and guarded when selected. |
| `privileged` | May change the host clock; deselected by default and guarded when selected. |

Run the default non-privileged suite, or a narrower safe group:

```console
pytest
pytest -m unit
pytest tests/unit/test_api.py tests/unit/test_cli.py tests/unit/test_port_safety.py
pytest -m "(integration or server_integration) and not live_network and not privileged"
```

The third command isolates management API, CLI, and port-safety contract tests;
the fourth isolates local integration and server-subprocess coverage.

Ordinary `pytest` never opts into the public network or host clock mutation.
To run live-network tests, supply the flag and environment acknowledgment:

```console
NTP_ANCHOR_RUN_LIVE_NETWORK=1 pytest --run-live-network \
  -m "live_network and not privileged"
```

PowerShell equivalent:

```powershell
$env:NTP_ANCHOR_RUN_LIVE_NETWORK = "1"
pytest --run-live-network -m "live_network and not privileged"
```

Privileged tests are for a disposable, isolated runner/VM with suitable
permissions. First verify that the privileged collection guard opens only with
the exact acknowledgment:

```console
NTP_ANCHOR_RUN_PRIVILEGED=I_UNDERSTAND_THIS_CHANGES_SYSTEM_TIME \
  NTP_ANCHOR_DISPOSABLE_HOST=1 \
  pytest --run-privileged -m "privileged and not live_network"
```

PowerShell equivalent:

```powershell
$env:NTP_ANCHOR_RUN_PRIVILEGED = "I_UNDERSTAND_THIS_CHANGES_SYSTEM_TIME"
$env:NTP_ANCHOR_DISPOSABLE_HOST = "1"
pytest --run-privileged -m "privileged and not live_network"
```

The real clock-change test is additionally marked `live_network` and requires
a second execution acknowledgment. To run it, provide every guard and select
the intersection of both markers:

```console
NTP_ANCHOR_RUN_PRIVILEGED=I_UNDERSTAND_THIS_CHANGES_SYSTEM_TIME \
  NTP_ANCHOR_EXECUTE_CLOCK_MUTATION=I_UNDERSTAND_THIS_CHANGES_SYSTEM_TIME \
  NTP_ANCHOR_DISPOSABLE_HOST=1 \
  NTP_ANCHOR_RUN_LIVE_NETWORK=1 \
  pytest --run-privileged --run-live-network \
    -m "privileged and live_network"
```

PowerShell equivalent:

```powershell
$ack = "I_UNDERSTAND_THIS_CHANGES_SYSTEM_TIME"
$env:NTP_ANCHOR_RUN_PRIVILEGED = $ack
$env:NTP_ANCHOR_EXECUTE_CLOCK_MUTATION = $ack
$env:NTP_ANCHOR_DISPOSABLE_HOST = "1"
$env:NTP_ANCHOR_RUN_LIVE_NETWORK = "1"
pytest --run-privileged --run-live-network -m "privileged and live_network"
```

Run the local equivalents of the CI quality gates before opening a change:

```console
ruff format --check src tests
ruff check src tests
mypy src/ntp_anchor
bandit -q --severity-level high -r src/ntp_anchor
pytest --cov=ntp_anchor --cov-branch --cov-report=term-missing --cov-fail-under=80
python -m build
```

CI runs non-privileged tests across supported Python versions on Windows,
Linux, and macOS, and performs formatting, lint, type, security, coverage, and
package-build checks. Privileged validation is a separate manually dispatched
workflow restricted to a self-hosted runner carrying the
`ntp-anchor-disposable` label and the protected `privileged-clock-testing`
environment. The build job installs the sdist and wheel into clean virtual
environments and smoke-tests core import, `ntp-anchor-api --version`, and an
API-enabled `/ping` request.

Default tests must never contact the public network, change the host clock, or
require elevation. Use events/barriers and injected transports/backends for
deterministic concurrency and lifecycle tests; reserve real network or clock
operations for explicitly selected, guarded suites.

## License

[MIT](LICENSE)
