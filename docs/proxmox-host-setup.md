# Proxmox Host Setup

This MVP supports one deployment path: direct installation onto a Proxmox VE host that has a monitor attached locally.

## Prerequisites

- Proxmox VE host with `systemd`
- root shell access
- A target VM that exposes either a SPICE display, an operator-prepared loopback-only VNC endpoint, a fully operator-prepared Looking Glass guest, or a Sunshine host reachable from the Proxmox host for Moonlight
- Package sources that can install:
  - `python3`
  - `python3-evdev`
  - `seatd`
  - `virt-viewer`
  - `wlr-randr`
  - `cage` when the resolved kiosk compositor is `cage`
  - `sway` when the resolved kiosk compositor is `sway`

When `console_backend = "moonlight"`, operators also need Linux `moonlight-qt` version `6.0.0` or newer. The installer does not add that package for you.

## Install

1. Clone or copy this repository onto the Proxmox host.
2. Run `sudo ./install.sh`.
3. Edit `/etc/relayinner-display/config.toml` and set at least:
   - `[target].vmid`
   - `[target].node_name`
   - `[target].console_backend` if you are switching from the default SPICE path
   - `[kiosk].compositor` if you need to override the default `auto` behavior; `auto` resolves to `cage` for `spice`, `vnc`, and `looking-glass`, and to `sway` for `moonlight`
   - `[console.vnc].display_number` plus matching VM `args: -vnc 127.0.0.1:<display_number>` when using `console_backend = "vnc"`
   - `[console.looking_glass].shm_file` plus any renderer or SPICE overrides when using `console_backend = "looking-glass"`
   - `[console.moonlight].host` plus any non-default `app`, `base_port`, `resolution`, or `state_dir` when using `console_backend = "moonlight"`
   - `[display].output_name` if you want to pin a specific connector name; on the managed sway path this also pins workspace `1` to that connector when it is currently connected
   - `[input].power_button_event` if the default evdev path does not match the host
4. Start the services after editing the config:

```sh
systemctl restart relayinner-display-seatd.service relayinner-displayd.service relayinner-display-kiosk.service
```

5. Reboot once to confirm that `tty1` returns directly into the kiosk session.

Installer flags:

- `./install.sh --skip-package-install` skips the `apt-get` step when the required packages are already present.
- `./install.sh --replace-config` backs up the existing `/etc/relayinner-display/config.toml` and replaces it with the sample config.

Successful installer runs also rewrite `/var/lib/relayinner-display/install-state.json`. Re-running without `--replace-config` preserves the current config and records `config_state.action=preserved`; re-running with `--replace-config` records `config_state.action=replaced` plus the backup path created during that run.

The installer now selects compositor packages from the current config. If you later switch the resolved kiosk compositor from `cage` to `sway`, rerun `sudo ./install.sh` or install `sway` yourself before restarting the services.

## VNC Backend

Use `console_backend = "vnc"` only after the target VM has been prepared with a loopback-only VNC bind in Proxmox VM config. The supported shape is:

```text
args: -vnc 127.0.0.1:77
```

Then configure the relay to match:

```toml
[target]
console_backend = "vnc"

[console.vnc]
bind_host = "127.0.0.1"
display_number = 77
viewer = "remote-viewer"
```

The relay derives TCP port `5900 + display_number`, launches `remote-viewer --full-screen vnc://<bind_host>:<port>`, and refuses to run if `qm config <vmid>` exposes VNC on any non-loopback address or on a different display number. The installer does not rewrite VM config for you.

## Looking Glass Backend

Use `console_backend = "looking-glass"` only after the guest already satisfies the upstream Looking Glass requirements. This relay backend is intentionally preflight-only support:

- the installer does not create `/dev/kvmfr*`
- the installer does not configure GPU passthrough or VFIO
- the installer does not install the guest-side Looking Glass host application

Configure the relay like this:

```toml
[target]
console_backend = "looking-glass"

[console.looking_glass]
shm_file = "/dev/kvmfr0"
binary = "looking-glass-client"
renderer = "auto"
fullscreen = true
disable_host_screensaver = true
spice_enabled = true
```

At runtime the daemon verifies that the configured client binary exists, that `shm_file` already exists, and that the `relayinner-display` session user can read the file or device before it launches `looking-glass-client` in the kiosk session. If any of those visible host-side prerequisites fail, the appliance moves into controlled `degraded` state instead of showing a blank screen.

For the required host and guest preparation, follow the upstream documentation:

- <https://looking-glass.io/docs/stable/requirements/>
- <https://looking-glass.io/docs/stable/install_client/>

## Moonlight Backend

Use `console_backend = "moonlight"` only after the guest already runs Sunshine and the Proxmox host already has Linux `moonlight-qt` version `6.0.0` or newer. Specs 30 through 32, Specs 40 through 41, and Specs 50 through 51 cover the backend contract, persistent workspace, PIN-assist pairing, Desktop fast-path behavior, optional client resolution override, direct `moonlight stream` launch, reconnect behavior, compositor selection, and the managed sway runtime:

- the installer does not configure Sunshine inside the guest
- the relay does not store Sunshine usernames or passwords
- this path targets Linux `moonlight-qt`, not mobile clients or browser flows

Configure the relay like this:

```toml
[target]
console_backend = "moonlight"

[kiosk]
compositor = "auto"

[console.moonlight]
binary = "moonlight"
host = "192.168.50.20"
base_port = 47989
app = "Desktop"
resolution = "1920x1080"  # optional
state_dir = "/var/lib/relayinner-display/moonlight"
quit_app_after_session = false
```

With `compositor = "auto"`, the relay resolves Moonlight to the managed `sway` kiosk path. The other backends still resolve to `cage` unless you override that contract explicitly with a supported combination.

On that managed sway path, the kiosk launcher writes `/run/relayinner-display/sway.config` on each kiosk start, launches `sway --config /run/relayinner-display/sway.config`, and does not load operator-owned or system sway config files. If `[display].output_name` is set, the launcher adds `workspace 1 output <name>` only when that connector is currently visible under `/sys/class/drm`; otherwise it logs a warning and lets sway continue with ordinary output selection.

If you are enabling Moonlight on an existing relay install that previously resolved to `cage`, rerun `sudo ./install.sh` first so the host package set includes `sway` for the new resolved compositor.

`app` is matched case-insensitively against the live Sunshine app list from `moonlight list --csv` when you target a non-`Desktop` Sunshine entry. `quit_app_after_session = true` is valid only for non-`Desktop` apps; the `Desktop` entry is treated as a stream surface, not a relay-managed launchable program. When `resolution` is set, it must use `<width>x<height>` dimensions; the relay trims surrounding whitespace, accepts either `x` or `X` in config input, and stores the canonical lowercase form.

At runtime the daemon verifies that the configured Moonlight binary exists and reports version `6.0.0` or newer, prepares `state_dir` plus `portable.dat`, probes TCP reachability to the configured Sunshine host, and reads the managed workspace to determine whether the host is already paired. For `app = "Desktop"`, that paired workspace state is sufficient and the relay skips daemon-side catalog validation before launch. For non-`Desktop` apps, the daemon also runs `moonlight list <host-authority> --csv` from that managed workspace to verify that the configured app still exists before launch. Those daemon-side Moonlight helper calls run with a headless Qt platform so they do not fall back to EGLFS/DRM outside the kiosk session, and they still honor `[policy].command_timeout_s` so a hung CLI check fails with a clear degraded reason instead of wedging the appliance in `requesting_console`. If `base_port` differs from `47989`, the relay renders `host:port` for hostnames and IPv4 or `[ipv6]:port` for IPv6 literals before invoking Moonlight. If `resolution` is set, the relay appends `--resolution <width>x<height>` before `--display-mode fullscreen` on every `moonlight stream` launch and mirrors that configured requested value into `moonlight_resolution` in the runtime state file.

If the host is reachable but not paired, the daemon generates a 4-digit PIN, launches `moonlight pair <host-authority> --pin <pin>` inside the kiosk session, and moves the appliance into `waiting_for_pairing`. Moonlight's own pairing UI shows that PIN fullscreen; approve it in the Sunshine web UI `PIN` page on the guest-side host. The same PIN is also mirrored in `/run/relayinner-display/daemon.state.json` while approval is pending. Once the managed workspace shows the paired-host record, the relay clears the PIN and continues to `moonlight stream <host-authority> <app> [--resolution <width>x<height>] --display-mode fullscreen` automatically. The relay appends `--quit-after` only when `quit_app_after_session = true`. The paired-host state remains in `state_dir` across daemon and host restarts.

If the configured app name does not exist in the live app list, the appliance enters controlled `degraded` state with a backend-tagged reason instead of launching an empty fullscreen Moonlight session. If Moonlight exits unexpectedly while the VM stays running and the Sunshine host remains reachable, the relay re-enters reconnect flow and repeats the same preflight checks before relaunching.

Sunshine setup inside the guest still remains operator-managed, including app catalog definitions such as `Desktop`.

## Uninstall

Run uninstall from the repository checkout on the Proxmox host:

```sh
sudo ./uninstall.sh
```

Default uninstall performs these steps conservatively:

- stops `relayinner-display-seatd.service`, `relayinner-display-kiosk.service`, and `relayinner-displayd.service`
- disables the relay services so they do not come back on the next boot
- removes `/usr/local/lib/relayinner-display/`, `/usr/local/share/relayinner-display/`, the relay unit files, and `/etc/systemd/logind.conf.d/relayinner-display.conf`
- runs `systemctl daemon-reload`
- restores `getty@tty1.service`
- preserves `/etc/relayinner-display/config.toml`
- deletes `/var/lib/relayinner-display/install-state.json` only after the restore and cleanup steps succeed

If the installer-created service user is still recorded as `service_user.created_by_installer=true`, uninstall also removes the `relayinner-display` user and `/var/lib/relayinner-display/`. If the installer did not create that user, uninstall leaves the user and home directory intact.

Use purge mode only when you want the relay config removed too:

```sh
sudo ./uninstall.sh --purge-config
```

`--purge-config` performs the default uninstall and also removes:

- `/etc/relayinner-display/config.toml`
- `/etc/relayinner-display/config.toml.bak.*`
- `/etc/relayinner-display/` if it becomes empty after the purge

`/var/lib/relayinner-display/install-state.json` is the restore authority for uninstall. When it is present, uninstall uses it to decide:

- whether the relay service user can be removed safely
- whether `display-manager.service` existed and was changed by the installer
- which canonical relay-managed unit and asset paths should be removed

`display-manager.service` is restored only when install-state proves both `existed=true` and `changed_by_installer=true`. If the unit was masked before install, uninstall leaves it masked. If it was enabled before install, uninstall enables it again. If it was active before install, uninstall starts it again.

If `install-state.json` is missing, uninstall prints one best-effort warning, removes the canonical relay assets by known path, restores `getty@tty1.service`, and preserves the relay service user plus home directory because installer ownership can no longer be proven safely. In that mode it does not attempt to restore `display-manager.service`.

After uninstall, verify that the relay stack is gone and the host login path is back:

```sh
systemctl status getty@tty1.service
test -e /etc/systemd/system/relayinner-displayd.service && echo "relay unit still present" || echo "relay units removed"
test -e /etc/systemd/logind.conf.d/relayinner-display.conf && echo "logind override still present" || echo "logind override removed"
test -e /var/lib/relayinner-display/install-state.json && echo "install-state still present" || echo "install-state removed"
```

## Managed Paths

- `/usr/local/lib/relayinner-display/`
- `/usr/local/share/relayinner-display/`
- `/etc/relayinner-display/config.toml`
- `/etc/systemd/system/relayinner-display-seatd.service`
- `/etc/systemd/system/relayinner-display-kiosk.service`
- `/etc/systemd/system/relayinner-displayd.service`
- `/etc/systemd/logind.conf.d/relayinner-display.conf`
- `/var/lib/relayinner-display/install-state.json`
- `/run/relayinner-display/`
- `/run/relayinner-display/sway.config` while the resolved kiosk compositor is `sway`

## Managed Services

- `relayinner-display-seatd.service`
- `relayinner-display-kiosk.service`
- `relayinner-displayd.service`

The installer also masks `getty@tty1.service` so the kiosk stack owns `/dev/tty1`. If `display-manager.service` exists, the installer disables and masks it as well.

For Spec 15, the rendered units also enforce `StartLimitIntervalSec=120` and `StartLimitBurst=5` so repeated crash loops stop being retried indefinitely.

`/var/lib/relayinner-display/install-state.json` is the authoritative record of installer-managed host state for later uninstall or restore work. It records the managed paths, config preservation or replacement result, whether the service user was created during that run, and the pre-install state of `getty@tty1.service` plus `display-manager.service`.

## Verification

Use these checks after installation:

```sh
systemctl status relayinner-display-seatd.service relayinner-displayd.service relayinner-display-kiosk.service
journalctl -u relayinner-displayd.service -u relayinner-display-kiosk.service -b
sudo cat /var/lib/relayinner-display/install-state.json
stat -c '%a %U %G %n' /var/lib/relayinner-display/install-state.json
cat /run/relayinner-display/daemon.state.json
```

To focus on subsystem-scoped entries from the MVP logging contract, filter the journal output for logger names such as:

```sh
journalctl -u relayinner-displayd.service -u relayinner-display-kiosk.service -b | grep 'relayinner-display\.'
```

The runtime state file now records the public appliance state and key fault markers:

- `appliance_state`
- `kiosk_compositor`
- `session_ready`
- `vm_power_state`
- `display_power_applied`
- `degraded_reason`
- `last_console_exit`

When VNC, Looking Glass, or Moonlight is selected, the same state file also includes backend-specific troubleshooting metadata:

- `vnc_endpoint`
- `looking_glass_shm_file`
- `moonlight_host`
- `moonlight_base_port`
- `moonlight_app`
- `moonlight_resolution`
- `moonlight_pair_state`
- `moonlight_pair_pin` while pairing approval is pending

If power-button forwarding is enabled, confirm the logind override is active:

```sh
grep -R HandlePowerKey /etc/systemd/logind.conf /etc/systemd/logind.conf.d /run/systemd/logind.conf.d /usr/lib/systemd/logind.conf.d 2>/dev/null
```

## Troubleshooting

If you need to confirm exactly what the installer changed on the host, inspect `/var/lib/relayinner-display/install-state.json` first. The `config_state` section shows whether the current config was created, preserved, or replaced, and `conflicting_units` captures whether `getty@tty1.service` or `display-manager.service` existed and whether this installer run changed them.

When the appliance is not showing the guest, inspect the state file first:

- `appliance_state=display_sleeping` means the VM has remained off past `dpms_off_delay_ms`.
- `appliance_state=waiting_for_vm` means the session is healthy but the VM is not in a runnable state.
- `appliance_state=waiting_for_pairing` means the Moonlight host is reachable but still waiting for Sunshine PIN approval, typically while the kiosk is showing Moonlight's pair UI.
- `appliance_state=degraded` means a local runtime dependency, power-button validation, or repeated Proxmox command failure tripped the Spec 15 failure policy.

Then inspect journald by subsystem:

- `relayinner-display.proxmox` for `qm`/`pvesh` failures and retry exhaustion
- `relayinner-display.console` for `remote-viewer`, `looking-glass-client`, or `moonlight` launch, exit, and preflight problems
- `relayinner-display.display` for display power helper failures
- `relayinner-display.input` for host power-button validation or evdev read failures
- `relayinner-display.session` for session connection and state-transition events

If the kiosk journal shows the `libseat` sequence `Could not connect to socket /run/seatd.sock: Permission denied`, `Could not open target tty: Permission denied`, `Timeout waiting session to become active`, or `Unable to create the wlroots backend`, confirm the installed kiosk unit launches `/usr/local/lib/relayinner-display/relayinner-display-kiosk` and that `relayinner-display-seatd.service` is up with the relay group owning `/run/seatd.sock`. Older installs that still launch `seatd-launch -- cage -- ...` or embed `cage -- /usr/local/lib/relayinner-display/session-entrypoint` directly can fail immediately or drift from the current compositor-selection contract; rerun `sudo ./install.sh` to refresh the units before debugging further. If `relayinner-display-seatd.service` instead fails with `Failed at step EXEC spawning /usr/bin/seatd`, rerun `sudo ./install.sh` so the installer refreshes the seatd unit with the actual host binary path, such as `/usr/sbin/seatd`, then restart `relayinner-display-seatd.service`, `relayinner-displayd.service`, and `relayinner-display-kiosk.service`.

If the kiosk journal shows `failed to open /dev/dri/renderD128: Permission denied`, `failed to open /dev/dri/card0: Permission denied`, or `Unable to create the wlroots renderer`, the kiosk service lacks the GPU groups required to open the DRM nodes. The installer now detects the owning groups for `/dev/dri/card*` and `/dev/dri/renderD*` and renders them into `SupplementaryGroups=` for `relayinner-display-kiosk.service`; rerun `sudo ./install.sh` and verify the unit contains the expected groups, typically `video render`.

The managed kiosk unit also exports `LIBSEAT_BACKEND=seatd` so the resolved kiosk compositor uses the same backend as the successful transient `systemd-run` checks instead of depending on libseat backend auto-selection.

If `systemctl status relayinner-display-kiosk.service` briefly shows the child process as `/usr/bin/python3 /usr/local/lib/relayinner-display/session-entrypoint` before `cage` exits with `status=1`, the installed runtime is likely older than the absolute-path launcher hotfix. Refresh `/usr/local/lib/relayinner-display/relayinner_display/kiosk.py` with `sudo ./install.sh`; older copies tried to exec `relayinner-display-session` by bare name, which fails when `cage` does not preserve the kiosk unit `PATH`.

If the kiosk journal warns that a requested output pin is unavailable at sway startup, confirm that `[display].output_name` matches a currently connected DRM connector name such as `HDMI-A-1` or `DP-1`. The managed sway launcher writes the final runtime config to `/run/relayinner-display/sway.config`; when that warning appears it intentionally omits `workspace 1 output ...` so sway falls back to ordinary output selection instead of failing startup.

If `runuser -u relayinner-display -- /usr/local/lib/relayinner-display/session-entrypoint` or the kiosk journal shows `PermissionError: [Errno 13] Permission denied: '/etc/relayinner-display/config.toml'`, the session user cannot read the preserved host config. Refresh the install with `sudo ./install.sh`; the current installer normalizes `/etc/relayinner-display/` to service-group-readable permissions so the unprivileged kiosk session can load the same config as the root daemon.

If the display helper logs `Wayland server does not support wlr-output-power-management-v1`, the host is still configured for `wlopm`. Cage supports output management through `wlr-randr` more broadly than it supports the `wlopm` protocol, so rerun `sudo ./install.sh` and keep `power_helper = "wlr-randr"` unless you have confirmed `wlopm` support on that compositor build.

If the daemon logs `Prepared console launch for VM ... with backend=spice` and `Console started: backend=spice pid=...` but the viewer immediately closes with a dialog like `Unable to connect graphic server /run/relayinner-display/console/spice-current.vv`, inspect the generated `.vv` file. Proxmox returns the `ca` certificate with escaped newlines, and the relay writer must keep that value escaped on one `ca=...` line; literal embedded newlines break the INI format and can cause `remote-viewer` to fail before the SPICE session opens.

If the state file or journal shows `console_backend=vnc` and the appliance moves into `degraded`, verify that `qm config <vmid>` contains a loopback-only `args: -vnc 127.0.0.1:<display_number>` entry and that `[console.vnc].display_number` matches it exactly. Non-loopback binds such as `0.0.0.0` are refused intentionally.

If the VNC backend stays in the reconnecting path while the VM is running, inspect `vnc_endpoint` in `/run/relayinner-display/daemon.state.json` and confirm the derived host-local port is actually listening before `remote-viewer` launches. The relay probes the loopback endpoint before each launch and waits in reconnect flow until the socket accepts TCP connections.

If the state file or journal shows `console_backend=looking-glass` and the appliance moves into `degraded`, verify that `looking_glass_shm_file` matches the expected KVMFR or IVSHMEM device, that the file already exists before the daemon starts, and that the `relayinner-display` session user can read it. Missing or unreadable shared memory is treated as a hard preflight failure by design.

If Looking Glass reconnects repeatedly while the guest stays up, inspect the console log lines for `backend=looking-glass` and confirm the guest-side host application plus upstream passthrough setup are healthy. The relay only validates host-visible prerequisites; it does not diagnose guest-side capture or passthrough failures beyond that boundary.

If the state file shows `appliance_state=waiting_for_pairing`, open the Sunshine web UI on the guest-side host, go to the `PIN` page, and enter the current `moonlight_pair_pin` shown by Moonlight's pairing UI or in `/run/relayinner-display/daemon.state.json`. The relay rotates that PIN after 300 seconds if approval has not completed yet.

If the state file or journal shows `console_backend=moonlight` and the appliance moves into `degraded`, verify that Linux `moonlight-qt` `6.0.0` or newer is installed, that `[console.moonlight].host`, `base_port`, `app`, and optional `resolution` point at the expected Sunshine host and app, and that the configured `state_dir` plus `portable.dat` are present and writable by the `relayinner-display` session user. If `app = "Desktop"`, confirm that `/run/relayinner-display/daemon.state.json` still reports `moonlight_pair_state = "paired"` and focus on stream/session failures. If `app` names another Sunshine entry, check `moonlight_app` in the state file against `moonlight list <host-authority> --csv`; the relay matches app names case-insensitively but requires an exact app-name match, and `quit_app_after_session = true` is only valid for non-`Desktop` apps. `moonlight_resolution` in the same state file shows the configured requested override rather than a measured negotiated stream mode.

If Moonlight reconnects repeatedly while the VM stays running, inspect `relayinner-display.console` log lines for `backend=moonlight` and confirm that the Sunshine host is still reachable and that the configured app still exists in the live Sunshine app list. The relay re-validates both before each relaunch.
