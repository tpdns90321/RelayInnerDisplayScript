# Spec 81. LXC Power Button Passthrough Spike

## Context / Problem

The host-direct appliance reads the physical power button through evdev and maps it to target guest start or graceful shutdown. In the LXC direct-seat profile, the runtime remains inside an unprivileged container, but the user has chosen to keep power-button handling inside that container rather than moving it into the host proxy.

Input devices are sensitive. Passing all host input into a container would expose keyboards or other controls unnecessarily. This spike verifies whether the existing evdev power-button reader can operate with only one stable, read-only power-button event node exposed to the unprivileged LXC.

## Goals / Non-goals

Goals:

- Discover a stable host power-button device path using `/dev/input/by-path` or `/dev/input/by-id`.
- Verify that the selected device reports `KEY_POWER`.
- Bind only the selected event node into the LXC.
- Grant read-only device access where supported by LXC cgroup policy.
- Confirm that existing relay input code can observe power-button presses from inside the container.
- Configure host logind so it does not race the relay for power-key handling.

Non-goals:

- Passing all input devices into the container.
- Supporting keyboards, mice, gamepads, or lid switches.
- Implementing the guest shutdown/start policy itself.
- Replacing the existing evdev code.
- Using privileged LXC fallback.

## User stories

- As an operator, I want the physical power button to retain the relay appliance behavior when the runtime runs inside LXC.
- As a maintainer, I want the LXC input surface limited to one audited power-button event node.
- As a reviewer, I want host logind conflicts prevented before the relay takes over the button.

## Public API / Interfaces

Host profile dry-run output must show the selected stable symlink, resolved event node, and cgroup rule:

```text
power_button_symlink=/dev/input/by-path/<stable-name>
power_button_resolved=/dev/input/eventN
lxc.cgroup2.devices.allow = c <major>:<minor> r
lxc.mount.entry = <resolved-node> dev/input/<relay-power-node> none bind,ro,create=file 0 0
```

The container config remains the existing relay input config surface, but the generated sample for LXC mode should prefer the container-local stable relay path:

```toml
[input]
power_button_event = "/dev/input/relay-power-button"
```

Host logind must be configured with the existing relay-managed override pattern so host shutdown does not win the race:

```ini
[Login]
HandlePowerKey=ignore
```

## Data model / Persistence

Install-state additions for applied host changes:

- selected stable symlink path
- resolved event node path
- device major/minor at apply time
- generated mount target inside the container
- logind override path and previous conflicting-unit state
- LXC config backup path and managed block content

Runtime state should not store raw button events. Existing relay runtime diagnostics may continue to expose the current power-button action state.

## Security model (Permission/Isolation/Audit)

Permissions:

- The event node is mounted read-only.
- The device cgroup rule grants read access only to the concrete event node.
- No `c 13:*` input-class rule is allowed for this MVP path.

Isolation:

- `/dev/input` as a directory must not be broadly mounted.
- Only one power-button event node is visible to the container.
- The host logind override is installed only under the LXC direct-seat host profile and tracked for uninstall.

Audit:

- The preflight must print the `KEY_POWER` verification result.
- The preflight must print rejected candidate devices when discovery is ambiguous.
- The implementation review must include whether a real press was observed by the container and whether the host attempted shutdown.

## Acceptance criteria (Testable, Verifiable)

- Discovery finds a stable by-path or by-id event device that supports `KEY_POWER`, or stops with actionable diagnostics.
- Apply mode refuses to expose `/dev/input/eventN` directly when a stable symlink exists but resolves inconsistently.
- The generated cgroup rule uses the selected event node's concrete major/minor and read-only access.
- The container sees exactly the configured relay power-button device path, not the whole input tree.
- Host logind is configured to ignore the power key while the profile is active.
- A physical power-button press is observed by the relay input reader inside the LXC.
- The host does not power off or suspend from the same press during the spike.
- Uninstall restores the previous logind state and removes the managed input passthrough lines.

## Test plan

Manual hardware validation:

1. Run host discovery in dry-run mode.
2. Confirm the selected candidate is the expected power switch.
3. Apply the LXC input passthrough while the CT is stopped.
4. Start the CT and run the existing input reader or daemon preflight.
5. Press the host power button once.
6. Confirm the container logs the event.
7. Confirm the host does not shut down.

Automated tests should cover:

- candidate selection from fake `/dev/input/by-path` and `/dev/input/by-id` trees
- `KEY_POWER` capability parsing
- ambiguous candidate refusal
- concrete major/minor rendering
- logind override install-state recording
- uninstall restoration behavior

## Rollout / Backward compatibility

This spike does not change the host-direct input path. Existing `[input].power_button_event` behavior remains valid.

LXC mode must remain disabled unless the LXC direct-seat profile is explicitly selected.

## Open questions

- Do Proxmox LXC bind mounts reliably preserve read-only semantics for evdev nodes across supported versions?
- Should long-press power events be filtered in the container or delegated to host logind in future profiles?
- What diagnostics should be shown when firmware exposes multiple `KEY_POWER` devices?

## Spec Dependencies

- Spec 13: Host Power Button to Guest Power Control
- Spec 17: Safe Uninstall Flow and README Removal Guide
- Spec 80: LXC Direct-Seat Feasibility Spike
