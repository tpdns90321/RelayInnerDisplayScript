# Spec 22. Looking Glass Backend and Preflight

## Context / Problem

Looking Glass addresses a different use case than SPICE or VNC. It is intended for GPU-passthrough virtual machines that already have the required shared-memory and guest-host components configured. That makes it attractive for low-latency desktop use, but it also changes the support boundary substantially:

- the VM must already be prepared for GPU passthrough
- shared memory must already exist on the host
- the guest-side Looking Glass host application must already be installed and functioning

This relay project should not absorb that whole setup lifecycle into its installer in the first iteration. The practical next step is a preflight-only backend: the relay can select Looking Glass from config, verify the host-side prerequisites it can see, launch the client inside the kiosk session, and fail clearly when those prerequisites are not met.

## Goals / Non-goals

Goals:

- Add `console_backend = "looking-glass"` as an implemented runtime path.
- Launch `looking-glass-client` fullscreen inside the existing Cage session.
- Validate visible host-side prerequisites before launch.
- Reuse the existing waiting/reconnect/degraded control model.
- Keep the backend narrow enough that operators still own guest-side and passthrough setup.

Non-goals:

- Automating GPU passthrough, VFIO, or VM hardware reconfiguration.
- Automating installation of the guest-side Looking Glass host application.
- Loading kernel modules or creating `/dev/kvmfr*` devices automatically.
- Replacing the current SPICE backend as the default relay path.
- Exposing every Looking Glass client option through relay config.

## User stories

- As an operator, I can choose Looking Glass for a VM that is already configured for passthrough and shared memory.
- As an operator, if the required host-side shared-memory device is missing, I get a clear degraded reason instead of a blank screen.
- As a maintainer, I can support Looking Glass without taking ownership of guest provisioning and passthrough automation.
- As a user at the monitor, I still get fullscreen single-application behavior and reconnect handling consistent with the other backends.

## Public API / Interfaces

Relay configuration:

```toml
[target]
vmid = 101
node_name = "auto"
guest_os = "windows"
console_backend = "looking-glass"

[console]
artifact_dir = "/run/relayinner-display/console"

[console.looking_glass]
binary = "looking-glass-client"
shm_file = "/dev/kvmfr0"
renderer = "auto"
fullscreen = true
disable_host_screensaver = true
spice_enabled = true
```

Config rules:

- `binary` defaults to `looking-glass-client`
- `shm_file` is required and must be an absolute path
- `renderer` accepts `auto`, `egl`, or `opengl`
- `fullscreen` defaults to `true`
- `disable_host_screensaver` defaults to `true`
- `spice_enabled` defaults to `true`; when set to `false`, the relay passes the client flag that disables SPICE support

Launch contract:

- default launch command:

```text
looking-glass-client -F -S -g auto -f /dev/kvmfr0
```

- if `spice_enabled = false`, append `-s`
- if `fullscreen = false`, omit `-F`
- if `disable_host_screensaver = false`, omit `-S`

Daemon preflight responsibilities:

- verify that the configured binary exists
- verify that `shm_file` exists at startup
- verify that the session user can read and open `shm_file`
- verify that the VM is running before attempting to launch the client

Preflight scope limits:

- the relay does not verify guest-side host-application health
- the relay does not verify guest-side capture interface selection
- the relay does not edit Proxmox VM hardware to add IVSHMEM or GPU passthrough devices
- the relay does not synthesize a separate Looking Glass client config file in v1

Daemon-to-session launch contract:

- `{"type":"connect_console","backend":"looking-glass","launcher":"looking-glass-client","argv":["looking-glass-client","-F","-S","-g","auto","-f","/dev/kvmfr0"]}`

Session contract:

- The session launches exactly one `looking-glass-client` child.
- Unexpected exit reports `{"type":"console_exited","backend":"looking-glass","code":...,"signal":...}`.
- Intentional disconnect when the VM stops suppresses the exit event exactly as it does for the other backends.

Operator-facing prerequisite contract:

- The VM must already satisfy the host application's requirements from the official Looking Glass setup guide.
- The shared-memory device or file named by `shm_file` must already exist before the relay is started.
- Any optional SPICE-side integration for clipboard, audio, or fallback display remains operator-managed outside the relay installer.

## Data model / Persistence

Persistent data:

- `/etc/relayinner-display/config.toml`

Runtime data:

- `/run/relayinner-display/daemon.state.json`

Runtime state example:

```json
{
  "console_backend": "looking-glass",
  "active_console_backend": "looking-glass",
  "looking_glass_shm_file": "/dev/kvmfr0",
  "last_error": null
}
```

Persistence rules:

- No Looking Glass config file is written by the relay in this spec.
- The daemon re-runs preflight checks on every start and before each fresh launch after a prolonged degraded state is cleared.
- Runtime state records the selected shared-memory path for troubleshooting only.

## Security model (Permission/Isolation/Audit)

Permissions:

- The daemon remains responsible for prerequisite checking.
- The session launches `looking-glass-client` as the unprivileged relay user.
- Host-side permissions on `shm_file` must be granted explicitly; the relay must not widen them automatically.

Isolation:

- The relay only supports a curated subset of Looking Glass client options.
- The session must not accept arbitrary extra CLI flags from IPC or config.
- The backend must not trigger root-level module loading or device creation as a side effect of normal runtime startup.

Audit:

- Log the selected shared-memory path and renderer at startup.
- Log a concise degraded reason when the binary or shared-memory device is missing or unreadable.
- Log client launch and exit using `backend=looking-glass`.

## Acceptance criteria (Testable, Verifiable)

- With `console_backend = "looking-glass"` and all visible host-side prerequisites satisfied, the relay launches `looking-glass-client` fullscreen inside Cage.
- If `looking-glass-client` exits unexpectedly while the VM remains running, the relay re-enters reconnect flow and attempts to relaunch according to the shared backoff policy.
- If `shm_file` is missing, unreadable, or inaccessible to the session user, the relay enters `degraded` with a clear reason.
- If the VM is stopped, the relay stays on the ordinary waiting/display-sleep path instead of launching the client.
- The backend does not attempt to change host kernel-module, VM hardware, or guest-application state automatically.

## Test plan

Config validation:

- accept valid `renderer`
- reject invalid `renderer`
- reject relative `shm_file`
- reject empty `binary`

Preflight behavior:

- binary missing
- shared-memory path missing
- shared-memory path present but unreadable to the session user
- VM stopped
- VM running with preflight satisfied

Session behavior:

- verify command construction with default flags
- verify `spice_enabled = false` appends `-s`
- verify unexpected exit emits backend-tagged `console_exited`

Manual checks:

- run on a host with a working `/dev/kvmfr0` device and confirm fullscreen launch
- remove access to the shared-memory device and confirm controlled degradation

## Rollout / Backward compatibility

This backend is additive and opt-in:

- default installs remain on SPICE
- operators switch to Looking Glass only by editing config

Documentation rules:

- README and setup docs must describe this backend as preflight-only support.
- The docs must state explicitly that GPU passthrough, shared-memory device creation, and guest host-app installation are outside relay automation.
- The docs must link operators to the upstream Looking Glass setup guidance for those prerequisites.

## Open questions

- Whether the session user will typically need a dedicated host group membership for `/dev/kvmfr0` on Proxmox hosts.
- Whether a future spec should add support for a relay-managed `looking-glass-client.ini` file instead of CLI-only launch.
- Whether audio and clipboard options should remain implicit via upstream setup or become explicit relay config in a later series.

## Spec Dependencies

- Spec 20. Configurable Console Backend Contract
