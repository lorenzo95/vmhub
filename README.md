# vmhub

A local VM manager for [qemus/qemu](https://github.com/qemus/qemu) running in
**rootless podman** containers, with a native GTK4 desktop app and a full CLI.

One container per VM. No root, no systemd, no libvirt. Snapshots, instant
clones, portable export/import — all working on a stock Debian 13 host.

```
~/vms/vmhub     # the GUI
~/vms/vmctl     # the CLI
~/vms/selftest  # 154-check end-to-end test of the backend
~/vms/guicheck  # 47-check headless test of every GUI handler
```

## What it does

| | |
|---|---|
| **Start / stop** | Each VM is a podman container. State survives GUI restarts. |
| **Snapshots** | Instantaneous copy-on-write via QMP, even while the VM is running. |
| **Revert** | Restores the disk to a snapshot and discards everything after it. |
| **Templates** | Mark any VM as a template. Clones are instant and cost almost no disk. |
| **Clone** | `qemu-img` backing-file overlay. 196 KB instead of a full copy. |
| **Rebase** | Push every clone onto an updated template without copying data. |
| **Flatten** | Resolve a clone's backing chain so it stands alone. |
| **Export / import** | Self-contained bundle with manifest and checksums, portable to another machine. |
| **Backup / restore** | `export` and `import`, so a backup is just a bundle. |
| **Resize** | Grow a disk in place; never shrinks. |
| **Console** | The container's own web viewer, plus one-click SSH and RDP. |
| **Install media** | Attach a real ISO from your disk: read-only, boots first, and available as a CD-ROM. |

## Requirements

Debian 13 (trixie), x86-64. Everything comes from the normal Debian `main`
repository — no third-party apt sources are needed.

### Install

```bash
sudo apt-get update
sudo apt-get install -y \
    podman \
    crun \
    uidmap \
    passt \
    qemu-utils \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gtk-4.0
```

What each one is for, so you can tell what is optional:

| Package | Why |
|---|---|
| `podman` | the container runtime (5.4.2 in trixie) |
| `crun` | OCI runtime. podman's default on Debian; the better-tested one for **rootless**, and it is what makes `--group-add keep-groups` work |
| `uidmap` | provides the setuid `newuidmap`/`newgidmap` helpers that rootless podman needs to map subuids |
| `passt` | user-mode networking for rootless containers. podman 5.x uses `pasta` from this package. `slirp4netns` also works |
| `qemu-utils` | `qemu-img` — every disk operation: create, resize, snapshot, convert, and the backing-file clones |
| `python3-gi`, `python3-gi-cairo` | PyGObject, for the GUI |
| `gir1.2-gtk-4.0` | GTK 4, which the GUI requires |

The GUI needs all four Python/GTK packages. The CLI works without them.

**Optional, per feature:**

| Feature | Needs |
|---|---|
| `vmctl rdp` | `remmina` (preferred) or `freerdp2-x11` |
| `vmctl libvirt` | `libvirt-clients` for `virsh`, and read access to `qemu:///system` |
| `guicheck` headless | `xvfb` (run it as `xvfb-run -a ./guicheck`) |
| `--mode guest` | `qemu-guest-agent` **inside the guest**, not on the host |

### Also required

- `/dev/kvm` readable by your user — either group membership
  (`sudo usermod -aG kvm "$USER"`, then log out and back in) **or** an ACL
  (`sudo setfacl -m u:"$USER":rw /dev/kvm`). Check with `vmctl doctor`.
- Hardware virtualisation enabled in BIOS/UEFI (Intel VT-x / AMD-V).
- `subuid`/`subgid` ranges for your user, which `uidmap`'s packaging normally
  sets up. Verify: `grep "$USER" /etc/subuid /etc/subgid`.

Nothing else is needed — the QEMU container ships its own QEMU 11, OVMF
firmware, SeaBIOS and noVNC.

### Verify

```bash
./vmctl doctor                 # environment: podman, rootless, crun, kvm, init
./selftest                     # 154-check backend lifecycle (creates throwaway VMs)
xvfb-run -a ./guicheck         # 47-check GUI sweep, touches no existing VMs
```

`doctor` is the one to run first; it checks every item above and names anything
missing. Note `guicheck` and `selftest` create and delete their own throwaway
VMs — they never touch VMs you have created.


## Quick start

```bash
cd ~/vms

./vmhub                              # launch the GUI

./vmctl new debian-a -b debian13      # or from the CLI
./vmctl ls
```

Open the console URL it prints, install Debian, then:

```bash
./vmctl finish-install debian-a       # stop booting the ISO
./vmctl stop debian-a
./vmctl template mark debian-a        # now it's clonable

./vmctl clone debian-b debian-a        # instant
./vmctl clone debian-c debian-a        # and again
./vmctl start debian-b debian-c        # all three on distinct ports
```

Cloning is the point: `debian-b` and `debian-c` share `debian-a`'s disk
read-only until you flatten or delete them. See *Templates and clones* below.

## Blueprints

A blueprint is a declarative starting point in `~/vms/blueprints/*.toml`.

| Blueprint | Notes |
|---|---|
| `alpine` | 60 MB. Fastest way to try everything. |
| `debian13` | 4 vCPU / 8 GB / 64 GB. Good template base. |
| `win11` | Installs on IDE, then switches to virtio-scsi. |
| `winserver2022` | Same flow, steadier under virtualization. |

```bash
./vmctl blueprints                    # list them
./vmctl new myvm -b debian13 --cpus 8 --ram 16G
```

### Attaching an ISO

Point vmhub at an installer image on your disk. It is bind-mounted **read-only**
as an IDE CD-ROM and boots before the disk, so you can install straight into it.
Nothing is copied, so a 5 GB Windows ISO costs no extra disk.

```bash
./vmctl iso attach myvm install ~/Documents/iso/windows11.iso
./vmctl iso attach myvm drivers ~/Documents/iso/virtio-win-0.1.285.iso
./vmctl iso ls myvm
./vmctl start myvm
```

Two slots exist:

| Slot | Mounted as | Purpose |
|---|---|---|
| `install` | `/start.iso` | The installer. Boots first, so remove it when done. |
| `drivers` | `/drivers.iso` | A second CD-ROM for virtio and other drivers. |

`install` boots before the disk every time, so detach it once the OS is
installed or the VM will boot the ISO again:

```bash
./vmctl iso detach myvm install
```

Attaching an install ISO **clears the blueprint's `boot.iso`**, so the container
never downloads a distro image behind your back. This matters: without it the
container downloads its own ISO *in addition to* yours, which is confusing and
wastes bandwidth.

vmhub validates the file first — it must contain an ISO 9660 or UDF volume
descriptor, and it reports the size, volume label and whether an El Torito boot
record was found. If the ISO disappears before the next start, startup fails
with a clear message rather than booting something unexpected. In the GUI this
is the **Install media** section of the Console tab, and **vmctl iso probe
<file>** checks an image without attaching it.

### Windows

`qemus/qemu` has no unattended install, so you supply the ISO and click
through setup. Put it at `~/vms/<vm>/storage/boot.iso` and point the spec at
it:

```bash
./vmctl set myvm boot.iso=https://example.com/windows11.iso
```

The `win11` blueprint boots on **IDE** so Windows Setup sees the disk without
any drivers. Afterwards:

1. Drop `virtio-win.iso` into `~/vms/<vm>/storage/drivers.iso`. The container
   auto-attaches it as a second CD-ROM.
2. Run `virtio-win-guest-install.bat` in the guest, reboot.
3. `./vmctl set myvm resources.disk_type=scsi` for the finished template.
4. **Before** marking it a template, sysprep with *generalize*. Otherwise every
   clone inherits the same MachineGuid and SID, which breaks domain joins and
   collides on activation.

Use RDP for day-to-day work — the web console has no clipboard or drag-and-drop.
The Console tab has a **Connect with Remmina** button; vmhub prefers Remmina
and falls back to FreeRDP.

```bash
./vmctl rdp myvm              # launch Remmina
./vmctl rdp myvm --print      # show the URI and the FreeRDP command
```

Credentials come from the blueprint (`rdp_user`) and default to a blank
password. Override them per VM if you set a real one during installation:

```bash
./vmctl set myvm media.rdp_user=myuser media.rdp_password=secret
```

## Snapshots

```bash
./vmctl snapshot myvm before-upgrade   # instant, costs ~nothing
./vmctl snapshots myvm
./vmctl revert myvm before-upgrade -y  # stops, restores, discards later snapshots
```

Snapshots are genuine qcow2 internal snapshots taken over QMP, so you can take
one while the VM is running. They live **inside the disk file**, not in a
database — `qemu-img snapshot -l` reads them even if you delete the metadata.

Reverting is necessarily an **offline** operation: QEMU has no
"load internal snapshot" command. vmhub stops the VM, applies the snapshot with
`qemu-img`, discards every snapshot taken after it, and restarts. That
discarding is deliberate and matches how you would expect a revert to behave.

## Stopping a VM

Stopping is three-tiered, because a live installer ISO will never answer ACPI
and a graceful stop will otherwise sit there for the full timeout.

| Mode | What happens |
|---|---|
| `graceful` *(default)* | ACPI shutdown, waiting up to `shutdown.timeout` seconds for the guest to respond. Safe for a running OS. |
| `power` | QMP `quit` — cuts power immediately. The container still shuts down cleanly and is removed, but the guest gets no chance to flush. |
| `force` | SIGKILL the container. No guest shutdown, no cleanup, no waiting. |

```bash
./vmctl stop myvm                    # graceful
./vmctl stop myvm --mode power       # cut power
./vmctl stop myvm --force            # SIGKILL, for when a stop is hanging
./vmctl stop                         # all running VMs
```

**Windows guests do not answer the ACPI shutdown.** A graceful stop will always
burn the full `shutdown.timeout` and then escalate to SIGKILL anyway (the
container exits 137). For Windows, use `--mode power`, or drop the timeout so
the escalation is fast:

```bash
./vmctl set mywin shutdown.timeout=10
```

The wait is tunable per VM, in the Hardware tab or directly:

```bash
./vmctl set myvm shutdown.timeout=10
```

The GUI shows a **Force stop** button whenever a VM is running, with a warning
that it skips guest shutdown and cleanup.

## Templates and clones

A clone is a qcow2 overlay whose backing file is the template's disk:

```bash
./vmctl template mark debian-a
./vmctl clone debian-b debian-a
```

### Linked vs full clones

The clone dialog offers both, and they differ in more than cost:

| | `--mode linked` (default) | `--mode full` |
|---|---|---|
| Disk | copy-on-write overlay on the template | standalone copy |
| Cost | instant, ~200 KB | copies the used data |
| Runs **alongside** its template | no — lock conflict | **yes** |
| Template can be deleted first | no | yes |
| Needs flattening before export | yes | no |

Use **linked** for many throwaway VMs off one golden image. Use **full** when
the clone must live independently — a machine you will keep, patch and re-clone,
or one you intend to move to another host.

```bash
./vmctl clone web-01 golden --mode linked    # instant
./vmctl clone build-01 golden --mode full    # independent, copies data
```

Both directions are guarded: starting a template while a linked clone runs, and
starting a linked clone while its template runs, fail immediately with an
explanation instead of a QEMU lock error.

Because the overlay is a *reference*, the template's absolute path is recorded
in the clone's header and bind-mounted read-only into the clone's container at
that same path. Consequences:

- A template **cannot run while its clones are running** — the clone holds a
  read lock and QEMU cannot open the template read-write. vmhub refuses with an
  explanatory error rather than letting QEMU fail obscurely.
- A template **cannot be deleted** while clones depend on it.
- A clone costs only the blocks it actually writes.

To break the dependency:

```bash
./vmctl flatten debian-b        # resolve the chain, copy used blocks, stand alone
./vmctl template mark debian-b  # now debian-b can be a template in its own right
```

To update a template and keep its clones cheap:

```bash
./vmctl stop debian-b debian-c
# ... update debian-a ...
./vmctl rebase debian-a          # repoint all clones at the new disk
```

## Portability

Every VM's disk can be made self-contained and moved to another machine.

```bash
./vmctl export debian-a                     # -> backups/debian-a-<date>.vmhub.tar.gz
scp backups/debian-a-*.tar.gz otherhost:~/vms/backups/

# on the other host
./vmctl import ~/vms/backups/debian-a-*.tar.gz
./vmctl start debian-a
```

A bundle contains a **standalone** disk, the spec, and a manifest pinning the
container image tag, the qemu-img version, and a SHA-256 of the disk. Import
verifies the checksum and assigns a **fresh UUID and MAC** by default so a moved
VM does not collide with the original on your network; pass `--keep-identity`
for genuine failover.

`export` requires the VM to be stopped. It **resolves a linked clone's backing
chain as it copies**, so the bundle is always self-contained — no separate
`flatten` step, and no way to produce an archive that cannot be imported. That
cost is real: exporting a clone of an 8 GB template copies all 8 GB, not the
clone's own few hundred MB, and vmhub says so before it starts.

Staging happens beside the destination rather than in `/tmp`, because a bundle
can be tens of gigabytes and `/tmp` is often a small tmpfs. Copying the whole
`~/vms` tree also works, since paths are stored relative to the project root.

Windows will very likely need reactivation on a different machine, since
OEM/DSP licensing is hardware-bound.

## Networking, and getting one VM to talk to another

Every guest sees itself as `192.168.1.3` — your host's real LAN address. That is
not a conflict and not a bug: `passt` is a userspace TCP/IP proxy, so the address
is a fiction and traffic is re-originated by the host. You will see the same
address at three layers (host, container, guest) and different MACs, because the
MAC is the only genuinely real identity — it is the QEMU NIC's, and the guest's
gateway MAC is derived from it so the router does not appear to change.

The consequence is that **guests cannot reach each other on that address** —
inside a guest, `192.168.1.3` *is itself*.

They *can* reach each other through the host, using one of the host's other
addresses. A container and its guest both hold a copy of the host's
default-route address, so that one is shadowed and unusable; any other global
host address is a valid rendezvous point. vmhub detects this and tells you what
to dial:

```bash
./vmctl ls            # shows "peer 192.168.122.1:2225" for reachable VMs
./vmctl info myvm     # "peer dial" row
```

For a VM to be reachable this way it must publish to more than loopback:

```bash
./vmctl set myvm network.bind=0.0.0.0
./vmctl start myvm
# another guest can now reach it at 192.168.122.1:8006
```

`127.0.0.1` is the default because it is the safe choice — `0.0.0.0` exposes
that VM's services to your entire LAN, not just to other VMs. Only widen it when
you specifically want that.

Guests are never visible on your LAN. Rootless podman cannot create the bridge
or the `/dev/tapN` node that would be required, and those failures are
deliberately silenced by upstream, so you get no warning — only the published
ports.

## Guest agent

The container wires a QEMU guest agent socket into each VM, so if the guest has
`qemu-guest-agent` installed you can ask it questions and shut it down cleanly:

```bash
./vmctl guest myvm                 # ip, hostname, OS
./vmctl stop myvm --mode guest     # ask the guest to shut down
```

`--mode guest` is **the only clean way to stop Windows**. Windows ignores the
container's ACPI signal, so a graceful stop always waits out the timeout and
then SIGKILLs (exit 137) — see the stop table above. Install the agent in the
guest (`qemu-guest-agent`, usually one package) and this becomes a real shutdown.

There is a **Query guest agent** button in the Hardware tab. It is on demand
rather than part of the status poll, because a guest without the agent would
otherwise cost a socket timeout on every refresh.

The agent is bridged to `storage/qga.sock`, visible on the host, so nothing
needs to run inside the container.

## Importing from libvirt

If you already run VMs under libvirt, vmhub can take them over:

```bash
./vmctl libvirt ls                     # list domains and their disks
./vmctl libvirt import win10           # convert the disk into a vmhub VM
./vmctl libvirt import win10 --new-name win10-copy --disk-index 1
```

The disk is **converted into a fresh qcow2**, so the libvirt domain keeps
working and vmhub gets an image it owns. `--move` deletes the original after a
successful convert.

Two deliberate defaults, both about booting first time:

- `--disk-type ide`. A guest installed against IDE drivers (typically Windows)
  will not boot on virtio-scsi. Switch it to `scsi` once the guest has virtio
  drivers.
- `boot.mode = auto`, which leaves `BOOT_MODE` unset so the container inspects
  the imported disk and picks firmware itself.

The domain must be shut off, so its disk is consistent.

## Backups

```bash
./vmctl export-all                     # every stopped VM -> backups/
./vmctl export-all web-01 db-01        # just these
./vmctl export-all --include-running   # stop, export, restart
```

Running VMs are skipped by default, since exporting needs the disk quiesced.
`--include-running` stops them, exports, then restarts them (using `--mode` for
how to stop). Each bundle is self-contained, so `backups/` is everything you
need — which makes it a natural pre-command for the backup tool you already run:

```bash
~/vms/vmctl export-all --include-running --mode guest
```

Exit status is non-zero if any VM failed, so a backup job can detect it.

## Autostart

vmhub can restart whatever was running when you last shut down. It works on
both systemd and SysVinit hosts and detects which you have.

```bash
./vmctl autostart status
```

**systemd host** — writes a user unit:

```bash
./vmctl autostart install            # systemctl --user enable vmhub.service
sudo loginctl enable-linger $USER   # so it also runs before you log in
```

**SysVinit host** (like this machine, PID 1 is `init`):

```bash
sudo ~/vms/vmctl autostart install   # writes /etc/init.d/vmhub
```

The generated script runs the restore as **your** user, not root, and with
`XDG_RUNTIME_DIR` set — rootless podman keeps its containers under your home, so
running as root at boot would start nothing. Installed with `sudo` it uses
`$SUDO_USER` to work out who that is.

No root? Install a session autostart entry instead, which starts the GUI with
your desktop session:

```bash
./vmctl autostart install --target session
```

To be explicit, pass `--target systemd|sysvinit|session`. Uninstall with
`./vmctl autostart uninstall`.

## The GUI

```bash
~/vms/vmhub
```

Left: every VM with a live state dot, resources and badges. Right: five tabs.

- **Console** — the container's web viewer, copyable `ssh` and `xfreerdp`
  commands, a **Connect with Remmina** button, and install-media attach/detach.
- Right-click a VM in the sidebar for console, start/stop, clone, export and
  delete. `Ctrl+N` creates a new VM.
- **Hardware** — edit vCPUs, RAM, disk size and disk bus; see UUID, MAC,
  backing file and port mapping.
- **Snapshots** — take, revert, delete.
- **Actions** — mark template, clone, rebase, export, flatten, import, delete.
- **Logs** — streaming container output.

Long operations run on worker threads and report progress in the status bar, so
the UI never blocks. The bar shows the newest message with the last few kept in
its tooltip.

## CLI reference

```
vmctl ls [--json]                 list VMs
vmctl info <vm>                   full detail for one VM
vmctl new <vm> [-b BP] [options]  create (--cpus --ram --disk --disk-type --iso
                                  --image --no-start --mark-template)
vmctl start <vm> [--console]      start, optionally open the viewer
vmctl stop <vm>... [--mode M]     stop (guest|graceful|power|force; --force = force)
vmctl stop-all                    stop everything
vmctl restore                     restart what was running at last shutdown
vmctl rm <vm> [-y] [--keep-disk]  delete a VM (--keep-disk: keep it, drop the container)

vmctl snapshot <vm> <name>        take a snapshot
vmctl snapshots <vm>              list them
vmctl revert <vm> <name> [-y]     revert, discarding later snapshots
vmctl snapshot-rm <vm> <name>     delete a snapshot

vmctl template ls|mark|unmark     manage templates
vmctl clone <vm> <template>       instant copy-on-write clone
vmctl flatten <vm>                resolve the backing chain
vmctl rebase <template>           repoint all clones

vmctl export|backup <vm>          write a portable bundle
vmctl import <bundle> [--name N]  import one
vmctl bundles                     list bundles
vmctl resize <vm> <size>          grow a disk

vmctl guest <vm>                  query the guest agent (ip/hostname/os)
vmctl libvirt ls|import <name>    list or import libvirt domains
vmctl export-all [names] [--include-running]
vmctl iso ls|attach|detach <vm>   manage install media
vmctl iso probe <file.iso>        check an ISO without attaching it
vmctl set <vm> KEY=VALUE ...      edit the spec, e.g. resources.cpus=8
                                  an empty value clears it, e.g. boot.iso=
vmctl show <vm>                   print vm.toml
vmctl logs <vm> [-f]              container logs
vmctl console|ssh <vm>           open console / ssh
vmctl rdp <vm> [--print]          connect over RDP via Remmina or FreeRDP
vmctl finish-install <vm>         clear the boot ISO after installing

vmctl blueprints                  list blueprints
vmctl doctor                      check the environment
vmctl autostart status|install|uninstall
vmctl registry                    rebuild registry.json
```

## How it finds your VMs

**The filesystem is the database.** There is no index, no cache and no
central state — a VM exists if and only if its directory has a `meta.json`:

```
~/vms/<name>/          a directory containing meta.json  →  a VM
~/vms/<name>/meta.json uuid, mac, template flag, created time
~/vms/<name>/vm.toml   resources, ports, network, media
~/vms/<name>/disk/     the disk, owned by vmhub
```

**The directory name is the VM's name.** Neither `meta.json` nor `vm.toml`
stores a name, so a directory can be renamed or moved and everything still
resolves — discovery, spec loading, container creation and teardown all follow
the directory. For a machine-readable inventory use `vmctl ls --json`, which is
computed live and therefore cannot go stale.

Note that **copying a directory duplicates the VM's uuid and mac**. If the copy
is meant to be a separate machine, clone it properly instead:

```bash
./vmctl clone new-name old-name        # fresh uuid, mac and ports
```

### Reconciliation

vmhub keeps no state of its own, so there is nothing to sync — but the *outside*
can drift from the filesystem, and vmhub repairs that when it matters:

**Port conflicts.** Two VMs claiming the same host port is resolved
automatically at start: the VM that is starting reassigns itself to a free port
and tells you what changed.

```bash
./vmctl repair-ports          # reassign every conflict now
./vmctl repair-ports --dry-run
```

**Orphaned containers.** Renaming a VM directory leaves its container behind,
still running, unreachable by anything — because the container is named after
the old directory. This is the one real consequence of the filesystem being the
only source of truth, so it is detected explicitly:

```bash
./vmctl prune --dry-run         # show them
./vmctl prune                   # remove them
./vmctl prune --keep-running    # skip the ones still up, e.g. holding 16 GB
```

`vmctl doctor` reports both.

## Layout

```
~/vms/
├── vmctl, vmhub, selftest        entry points
├── blueprints/*.toml             declarative VM templates
├── backups/                      exported bundles
├── systemd/user/                 unit template
├── sysvinit/                     LSB script template
└── <vm>/
    ├── vm.toml                   declarative spec, relative paths only
    ├── meta.json                 uuid, mac, template flags (rebuildable)
    ├── disk/data.qcow2           the disk, owned by vmhub
    └── storage/                  bind-mounted to /storage
        ├── qmp.sock, monitor.sock
        ├── start.iso             cached boot image, written by the container
        └── <os>/                 NVRAM, written by the container
```

`disk/data.qcow2` is bind-mounted into the container at `/data.qcow2`, which
means vmhub owns the disk outright and its location never depends on the boot
image. The same file is also mounted at `/storage/data.qcow2` so the container
recognises it as an existing disk and stops defaulting to its own Alpine image
once the guest has been installed. Snapshots live inside that file, so they
survive losing `meta.json` — `vmctl registry` rebuilds the index.

## Known constraints

These are inherent to running QEMU in rootless podman, not bugs:

- **Guests are not on your LAN.** Rootless podman cannot do NAT or macvtap, so
  the container falls back to user-mode networking. Upstream is explicit:
  *"macvtap is not supported when using Podman, only when using Docker."*
  Reach guests through the published `127.0.0.1` ports.
- **Revert is offline** (see above).
- **Export and flatten need the VM stopped**, and copy real data.
- **`restart: always` does nothing** without systemd, so vmhub owns VM
  lifecycle. Nothing resurrects a VM you killed by hand, which is the point.
- **Windows reactivation** on a moved VM.
- **The web console has no clipboard or drag-and-drop** — use RDP for Windows.
- **A brand-new, still-empty disk triggers the container's default Alpine fetch
  once (60 MB).** It is attached at the *lowest* boot priority and never boots
  ahead of your ISO (bootindex 1), the disk (3), or anything else. It is cached,
  never re-downloaded, and disappears entirely once the guest is installed.
  `vmctl ls` and the GUI both say so explicitly. The container has no supported
  way to disable this — its only "no download" sentinel requires the disk to
  already contain data, and every other route needs the ISO opened read-write.
- **An install ISO boots before the disk on every start** until you detach it.

## Troubleshooting

**"failed to stay running"** — vmhub prints the relevant container log lines.
The usual cause is a template being started while a clone holds its disk.

**A port is already in use** — `vmctl ls` shows which VM owns it. Move it with
`vmctl set <vm> network.host_ssh=2222`.

**The disk seems to have vanished** — check `vmctl info <vm>` for the `backing
file`. vmhub fails loudly rather than silently creating a blank disk.

**A VM shows as `unreadable` / `doctor` reports "bad meta"** — its `meta.json`
is corrupt. vmhub deliberately will not guess an identity for it; the uuid and
mac stay empty rather than being silently replaced with new ones. `meta.json`
holds only uuid, mac, template flags and the last run state, so if you can
rebuild it the disk and `vm.toml` are untouched.

**Nothing is visible in the web console** — the guest must be booted; the
console renders the guest's framebuffer, it is not a management view.
