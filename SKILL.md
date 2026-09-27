---
name: vmhub
description: >
  Operate vmhub, a local manager for QEMU VMs running in rootless podman
  containers on this machine. Use when creating, starting, stopping, cloning,
  snapshotting, exporting or debugging VMs here, or when asked why a VM did not
  boot, why a port is unreachable, or why an ISO was downloaded unexpectedly.
version: 1
---

# vmhub

Local VM manager. QEMU runs inside **rootless podman** containers — one
container per VM. No root, no libvirt, no systemd on this host.

Project root: `/home/Gero/vms`

```bash
cd ~/vms
./vmhub                 # GTK4 desktop app
./vmctl <command>       # CLI
./vmctl doctor          # verify the environment
./selftest              # 69 backend checks
xvfb-run -a ./guicheck   # 40 GUI checks (needs a display)
```

## How VMs are discovered

The filesystem is the database. A directory with a `meta.json` is a VM, and the
**directory name is the VM's name** — neither `meta.json` nor `vm.toml` stores
one, so renaming or moving a directory is safe and everything follows it.

There is no index and no cache. `vmctl ls --json` is the machine-readable
inventory and is computed live, so it cannot go stale.

Copying a directory duplicates the uuid and mac. Use `./vmctl clone` for a real
second machine.

**Two things drift and are repaired on contact:**

- *Port conflicts* — `start()` reassigns the starting VM's ports to free ones
  and reports the change. `vmctl repair-ports` does it in bulk.
- *Orphaned containers* — renaming a directory orphans the container, because it
  is named after the old directory. It keeps running and nothing can stop it.
  `podman.orphans()` finds them via the immutable `vmhub.name` label;
  `vmctl prune` removes and verifies. `doctor` reports both.

A corrupt `meta.json` yields a meta with **empty uuid/mac** and `broken=True`
(`registry.broken_metas()`, reported by `doctor`). Never let that become a
*defaulted* identity: `VmMeta`'s uuid/mac have random factories, so a
synthesized one written back would silently replace the real one.

The SysVinit autostart script substitutes **@OWNER@** at install time and runs
`su - "$OWNER"` with `XDG_RUNTIME_DIR`. It must not use `$(id -un)` at runtime —
at boot that is root, which owns no rootless containers.

QGA is bridged to `storage/qga.sock`. `qga.py` speaks the protocol and must send
`guest-sync-delimited` first — the agent may have buffered output from before we
connected, and a 0xFF byte marks the boundary. Query it on demand, never in the
status poll: a guest without the agent would cost a timeout per refresh.
`stop --mode guest` is the only clean stop for Windows.

`libvirt` imports convert the domain's disk into a new qcow2 and default to
`disk_type=ide` (a Windows guest installed against IDE will not boot on
virtio-scsi) and `boot.mode=auto` (the container detects firmware from the disk).
`boot.mode="auto"` means "omit BOOT_MODE" — the container then probes the disk.

Remmina profiles must carry the port **inside `server`** (`server=host:port`), not
only in the separate `port=` key. Remmina writes both in its own profiles, and a
profile with only `port=` set silently connects to 3389. Remmina also writes both
the legacy and current spellings of `restrictedadmin`/`restricted-admin` and
`gateway_host`/`gateway_server`; match that.

`rm --keep-disk` removes only the container: the VM stays registered and
startable. Do not "simplify" it by deleting `meta.json` — discovery requires
that file, so the disk would become invisible to vmhub with no way back.

`export` resolves any backing chain via `qemu-img convert` while it copies, so
bundles are always standalone. It stages beside the destination, never in /tmp.

Note `podman.rm()` takes a **VM** name and adds the `vmhub-` prefix itself;
passing a container name silently double-prefixes and removes nothing.

## Mental model

Read this before changing anything. Several facts are counter-intuitive and
each one caused a real bug.

**vmhub owns the disk; the container does not.** The disk lives at
`<vm>/disk/data.qcow2` and is bind-mounted into the container at `/data.qcow2`.
The same file is *also* mounted at `/storage/data.qcow2` so the container's own
`getDisk()` recognises it and stops fetching a default image. Never let the
container create a disk — `start()` fails loudly if a stray one appears.

**The container writes ISOs and NVRAM into `storage/<name-of-distro>/`**, where
the subdirectory depends on the `BOOT` value. That path is unpredictable, so
vmhub never relies on it. Do not read or write disk images from there.

**Snapshots live inside the qcow2**, not in app metadata. `qemu-img snapshot -l`
reads them, so they survive deleting `meta.json`; `vmctl registry` rebuilds the
index.

**Disks are always qcow2.** The container's default is `raw`, which cannot hold
internal snapshots. Never change `DISK_FMT`.

**`vm.toml` holds only relative paths** so the whole tree can be relocated. The
one exception is `media.install` / `media.drivers`, which are absolute host paths
to ISOs and are validated at start.

## Core operations

```bash
./vmctl new <vm> -b debian13              # create (blueprints: alpine, debian13, win11, winserver2022)
./vmctl ls / info <vm> / show <vm>
./vmctl start <vm> [<vm>...]              # accepts several
./vmctl stop <vm> [--mode graceful|power|force]
./vmctl snapshot <vm> <name>              # instantaneous, works while running
./vmctl revert <vm> <name> -y             # offline; discards later snapshots
./vmctl template mark <vm>                # then clone from it
./vmctl clone <vm> <template> [--mode linked|full]
./vmctl flatten <vm>                      # linked clone -> standalone
./vmctl rebase <template>                 # repoint all clones at a new template disk
./vmctl export <vm> / import <bundle>     # portable bundle
./vmctl iso ls|attach|detach|probe <vm>
./vmctl set <vm> resources.cpus=8 ram=16G # edits vm.toml
./vmctl rdp <vm>                          # launches Remmina
```

### Clone modes

| | `--mode linked` (default) | `--mode full` |
|---|---|---|
| Disk | copy-on-write overlay on the template | standalone copy |
| Cost | instant, ~200 KB | copies used data |
| Runs alongside its template | **no** — lock conflict | yes |
| Template can be deleted first | no | yes |

Both directions are guarded: starting a template while a linked clone runs, and
starting a linked clone while its template runs, both fail immediately with an
explanatory message. Do not "fix" a lock error by reordering starts — the
dependency is real.

### Stopping

`graceful` (default) sends ACPI and waits `shutdown.timeout` seconds; a live
installer ISO never answers, so it always burns the full timeout. `power` cuts
power via QMP. `force` SIGKILLs — no guest shutdown, no cleanup.

Windows guests also do not answer the container's ACPI signal, so a graceful
stop always escalates to SIGKILL (container exit 137). Prefer `power` for
Windows, or lower `shutdown.timeout`.

## Non-obvious container behaviour

Do not re-derive these; they were established by reading upstream
`qemus/qemu` and verified here.

- **`BOOT=none` is not a valid input.** It only exists as an internal value and
  `exit 64`s if supplied. An empty `BOOT` makes the container fall back to
  downloading Alpine. The only supported way to avoid a download is a disk that
  already contains data.
- **An empty disk triggers a one-time 60 MB Alpine fetch.** It is attached at
  the lowest boot priority (`bootindex 9`), never boots over an install ISO
  (`1`) or the disk (`3`), is cached, and stops once the guest is installed.
  Do not try to "fix" this by pre-partitioning the disk or by making the user's
  ISO writable — both are worse than the problem.
- **Attach an install ISO at `/start.iso`, never `/custom.iso`.** `/custom.iso`
  is recognised as boot media but hybrid ISOs (every Linux distro ISO) are then
  attached as `usb-storage` *without* `readonly=on`, which fails on a read-only
  mount. `/start.iso` is a proper read-only CD-ROM that installers see.
- **Attaching an install ISO clears the blueprint's `boot.iso`.** Otherwise the
  container downloads the blueprint's distro *as well*. That behaviour is
  intentional; `vmctl iso attach` does it automatically.

## Networking

Two nested userspace stacks, no veth, no bridge, no L2 path:

```
host LAN ── pasta (holds the published ports) ── container netns ── passt ── guest
```

The container's `eth0` and the guest both report the host's own address
(`192.168.1.3`). That is harmless: passt is a userspace TCP/IP proxy, so the
address is a fiction and traffic is re-originated by the host. There is no
address conflict.

- **Guests cannot reach each other on `192.168.1.3`** — inside a guest that
  address *is itself*. `127.0.0.1` inside a guest is the guest, and loopback is
  never shared with the host.
- **They can reach each other through the host**, by dialling one of the host's
  *other* addresses, e.g. `192.168.122.1:8009`. The target must have
  `network.bind` set to `0.0.0.0`; the default `127.0.0.1` publishes to loopback
  only and is unreachable from a guest. `vmctl ls` and `vmctl info` report the
  dial targets via `lifecycle.peer_dial_targets()`.
- **Guests are never on your LAN.** Rootless podman cannot `mknod` `/dev/tapN`
  or take `CAP_NET_ADMIN` over the host netns, so macvtap and bridge NAT fail.
  Upstream is explicit: *"macvtap is not supported when using Podman."* These
  failures are **deliberately silenced** in rootless mode, so no warning appears.

## RDP

RDP is only meaningful for a Windows guest, so it is hidden by default on
anything without a published port. The Console tab always shows the RDP block
now: either the URI plus a **Connect with Remmina** button, or an explanation
and a one-click **Publish an RDP port anyway** that adds `3389` to
`network.guest_ports` and allocates `host_rdp`.

Credentials come from the blueprint's `rdp_user`, defaulting to a blank password
in `media.rdp_password`. Prefer the web console for installation and RDP for
day-to-day use — the web console has no clipboard or drag-and-drop.

## Debugging

`./vmctl ls` and the GUI surface *notices* for known-benign states. Check those
first — a missing notice is the real problem.

```bash
./vmctl logs <vm> -f            # container stdout
./vmctl info <vm>               # backing file, boot media, ports, notices
```

Reach for QMP directly for anything the CLI does not expose:

```python
import sys; sys.path.insert(0, "/home/Gero/vms/src")
from vmhub import qmp, paths
with qmp.Qmp(paths.qmp_path("<vm>")) as c:
    print(c.query_status())
    for n in c.block_backends():
        print(n["node-name"], n.get("ro"), n.get("file"))
```

QMP runs over a unix socket at `<vm>/storage/qmp.sock`, so the host can reach it
directly — no `podman exec` needed.

Symptoms and their usual cause:

| Symptom | Cause |
|---|---|
| "failed to get shared write lock" | a linked clone and its template both running |
| QEMU exits right after boot | missing ISO, or a disk it cannot open |
| Guest unreachable from the LAN | expected; use a published `127.0.0.1` port |
| Download you did not ask for | empty disk, or an attached ISO plus `boot.iso` |
| Blank web console | the guest has not booted; it is not a management view |

## Invariants to preserve

- Snapshots are pruned on revert; do not restore stale ones.
- Deleting a template with dependents is refused, on purpose.
- Resource changes apply on the next start; only `resources.disk` can be grown,
  never shrunk.
- Never `rm -rf` a VM directory — use `vmctl rm`, which stops the container,
  releases template dependencies, and clears the index.

## Conventions

Python 3.13, GTK4 via PyGObject, **standard library only** — no pip
dependencies. `spec.py` carries the whole schema; `lifecycle.py` orchestrates;
`podman.py` is the only place that shells out to podman. Long operations run on
worker threads and must never raise out of a GUI callback — `VmPane.update()`
and `VmhubWindow._update_pane()` are both defensive for this reason.
