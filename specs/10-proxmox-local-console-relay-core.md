# Spec 10. Proxmox Local Console Relay Core

## Context / Problem

The product needs a minimal and reliable way to show one Proxmox-managed KVM guest directly on a monitor attached to the same Proxmox host. The relay path must not depend on a general desktop session or a remote operator clicking through the Proxmox GUI after each boot, guest restart, or viewer crash.

For this MVP, the display relay runs directly on the Proxmox host instead of in a container. That choice removes one layer of packaging complexity, but it also changes the control-plane assumptions:

- guest state can be read locally through `qm`
- SPICE connection material can be requested locally through `pvesh`
- guest start and shutdown operations can be performed without remote API credentials

The core problem is not merely launching `remote-viewer` once. The appliance must converge on the correct state over time:

- show the guest when it is available
- remain on an intentional waiting screen when the guest is not running
- reconnect after viewer exits or a transient SPICE failure
- avoid stale console content after the guest stops

## Goals / Non-goals

Goals:

- Define one supported MVP path for one local Proxmox VM using SPICE.
- Establish the control daemon, session supervisor, and local IPC split reused by later specs.
- Define one stable configuration model for the whole MVP.
- Keep all Proxmox control interactions local to the host through `qm` and `pvesh`.
- Support automatic reconnect while the VM remains in a runnable state.

Non-goals:

- Multiple target VMs
- Cluster migration tracking
- Audio, clipboard, file sharing, or USB redirection policy
- noVNC or browser-based viewing
- Guest provisioning or VM creation workflows

## User stories

- As an operator, I can configure one target VMID and have the host always relay that VM.
- As an operator, I can reboot the Proxmox host and the relay returns automatically without logging into a desktop.
- As an operator, I can restart the guest and the display reconnects without manual action.
- As a user standing at the monitor, I see either the target guest or an explicit waiting state, never a stale Linux desktop.

## Public API / Interfaces

Primary configuration file:

- Path: `/etc/relayinner-display/config.toml`
- Format: TOML

Required keys:

```toml
[target]
vmid = 101
node_name = "auto"
guest_os = "windows"
console_backend = "spice"

[runtime]
run_dir = "/run/relayinner-display"
control_socket = "/run/relayinner-display/session.sock"
spice_vv_path = "/run/relayinner-display/current.vv"
log_namespace = "relayinner-display"

[policy]
poll_interval_ms = 2000
reconnect_initial_ms = 1000
reconnect_max_ms = 15000
command_timeout_s = 10
```

Node resolution rules:

- `node_name = "auto"` means derive the local Proxmox node name from the host.
- MVP supports only VMs currently resident on the local node.
- If the VM is configured elsewhere or migrates away, the daemon enters `degraded`.

Processes:

- `relayinner-displayd`: root-owned control daemon
- `relayinner-display-session`: session supervisor running inside Cage
- `remote-viewer`: child process spawned by the session supervisor

Local IPC:

- Unix stream socket at `/run/relayinner-display/session.sock`
- Message format: newline-delimited JSON

Daemon-to-session messages:

- `{"type":"show_waiting","reason":"vm_stopped"}`
- `{"type":"connect_spice","vv_path":"/run/relayinner-display/current.vv"}`
- `{"type":"disconnect_console","reason":"vm_not_running"}`
- `{"type":"health_ping"}`

Session-to-daemon messages:

- `{"type":"session_ready"}`
- `{"type":"console_started","pid":1234}`
- `{"type":"console_exited","code":1,"signal":0}`
- `{"type":"session_error","reason":"viewer_launch_failed"}`

Local Proxmox command contract:

- Read VM state through `qm status <vmid>`.
- Request SPICE connection data through `pvesh create /nodes/<node>/qemu/<vmid>/spiceproxy`.
- Generate a `.vv` file from the returned SPICE metadata for `remote-viewer`.

State machine:

- `booting`
- `waiting_for_session`
- `waiting_for_vm`
- `requesting_console`
- `connecting_console`
- `showing_console`
- `reconnecting_console`
- `degraded`

## Data model / Persistence

Persistent data:

- `/etc/relayinner-display/config.toml`

Runtime data:

- `/run/relayinner-display/current.vv`
- `/run/relayinner-display/session.sock`
- `/run/relayinner-display/daemon.state.json`

Runtime state shape:

```json
{
  "vmid": 101,
  "node_name": "pve-01",
  "vm_power_state": "running",
  "session_state": "showing_console",
  "last_connect_attempt_at": "2026-04-03T12:00:00Z",
  "last_error": null
}
```

Persistence rules:

- No database is used in MVP.
- Runtime state is recreated on each boot.
- The daemon replaces stale runtime files on startup.
- The `.vv` file is regenerated on each fresh connect attempt to avoid reusing expired SPICE material.

## Security model (Permission/Isolation/Audit)

Permissions:

- `relayinner-displayd` runs as root because later specs require access to power-button devices and host-level power policy.
- `relayinner-display-session` runs as a dedicated non-login user, `relayinner-display`.
- `remote-viewer` is launched only by the session supervisor, never directly by root.

Isolation:

- No remote Proxmox API token is stored in MVP because control stays local to the Proxmox host.
- The session socket is owned by `relayinner-display:relayinner-display` and uses mode `0600`.
- Only one configured VMID may be controlled or displayed.

Audit:

- Log every Proxmox command invocation result at the operation level, not full raw output.
- Log state transitions, SPICE request failures, viewer restarts, and degraded-mode entry.
- Do not log full SPICE tickets or any generated `.vv` file content.

## Acceptance criteria (Testable, Verifiable)

- With a valid target VM configured and running locally on the host, the daemon generates a valid `.vv` file and the session launches `remote-viewer` fullscreen.
- If `remote-viewer` exits unexpectedly while the VM remains active, the daemon re-enters reconnect flow and restores the console without manual intervention.
- If the VM is not running, the session stays on the waiting screen and the daemon does not loop on failed SPICE requests.
- If local `qm` or `pvesh` calls fail, the daemon enters a controlled degraded or waiting path instead of leaving stale content on screen.
- Rebooting the Proxmox host restores the same target behavior from configuration alone.

## Test plan

Static validation:

- Validate TOML parsing with missing keys and unsupported `console_backend` values.
- Validate JSON IPC parsing and unknown message handling.

Functional scenarios:

- Start with VM already running.
- Start with VM stopped, then boot the VM.
- Kill `remote-viewer` while the VM is running.
- Restart the daemon while Cage remains active.
- Force `pvesh` SPICE request failure and verify degraded behavior.

Manual checks:

- Confirm only one `remote-viewer` instance exists for the configured VM.
- Confirm no stale guest image remains visible after a clean VM shutdown.

## Rollout / Backward compatibility

This is the first functional spec in the repository, so there is no existing compatibility burden. Later specs must preserve:

- config path `/etc/relayinner-display/config.toml`
- runtime directory `/run/relayinner-display`
- daemon plus session split

Implementation should avoid adding a second control path for remote API mode during MVP. If remote control is ever added later, it must be additive and not replace the local Proxmox CLI contract defined here.

## Open questions

- Whether local node-name discovery should use hostname, `pvesh get /nodes`, or another Proxmox-native source.
- Whether some Proxmox versions require extra transformation of `spiceproxy` response fields before generating a `.vv` file.
- Whether the guest display adapter assumptions for Windows should be fixed in documentation as part of MVP or left to operator setup notes.

## Spec Dependencies

- None
