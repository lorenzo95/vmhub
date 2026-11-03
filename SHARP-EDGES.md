# Sharp edges for unattended operation

Every item below was read out of the source in this checkout (not inferred from
`SKILL.md`), and each one is a place where an **unattended, script-driven, or
LLM-driven** job does something other than what it appears to do.

The organising question is: *what does this command look like from the outside,
and what does it actually change?* Most of the sharp edges are cases where the
two differ, and where the difference is invisible to a script that only checks
the exit code.

## Status

The first seven in the order of work below — **C1, C2, C3, C4, H1, H3 and H4** —
have been fixed. Each one is marked **Fixed** in its entry, with what changed and
where. The regression tests live in `tests/test_unattended.py`.

One more turned up while the fixes were being made, and is fixed too: a CLI
command that reads a flag straight off the namespace (`args.preserve_run_state`)
raises `AttributeError` for any caller that built the namespace by hand. The
commands are called directly like that — `selftest` does it — so an optional
flag now has to default to "off" for the callers that predate it.

Everything else is still open.

Severity:

- **Critical** — data loss, or a silent no-op that reports success.
- **High** — wrong state, hidden downtime, or a failure that surfaces as the
  wrong symptom.
- **Medium** — drift, blind spots, or reports an operator can't act on.
- **Low** — cosmetic, but confusing.

---

## Critical

### C1. `vmctl stop` silently cancels the boot restore

`lifecycle.py:522-530` (`_mark_stopped`) writes `last_run_state = "stopped"`
unless `preserve_run_state=True`. `restore_run_state` only restores VMs whose
`last_run_state == "running"` (`lifecycle.py:788`).

So a maintenance job that stops a VM for a patch **un-registers it from
autostart, permanently** — the next boot simply does not bring it back, and
nothing says so. The docstring at `lifecycle.py:555-561` states the rule but
the default is the dangerous side of it: `preserve_run_state` defaults to
`False` (`lifecycle.py:538`).

The systemd unit gets this right (`autostart.py:113`,
`stop-all --preserve-run-state`). Every other caller has to remember.

**Fixed.** `vmctl stop` and `vmctl stop-all` now take `--preserve-run-state`,
and a stop that clears the boot restore registration says so on the console
instead of doing it silently (`cli/vms.py`, `_was_restored_at_boot`). The
default is unchanged: a stop you typed still cancels the restore, because
that is what a typed stop means. The flag is what a scripted stop wants.

**Fix:** make `preserve_run_state` the default for non-interactive use, and
require an explicit `--disable-autostart` to opt *out* of restoring.

### C2. A failed backup leaves the VM down *and* un-restorable

`cli/media.py:138-154` implements `--include-running` as stop → export →
restart. The stop marks the VM stopped (C1). If the export raises, or the
restart raises (`cli/media.py:152-154` reports "restart failed"), `last_run_state`
is left at `"stopped"` — so the VM is not restored at the next boot either.

Net effect: one failed nightly backup takes a VM out of service until someone
notices and starts it by hand.

**Fixed.** `export-all --include-running` stops with `preserve_run_state=True`,
so the record of what was running survives the stop (`cli/media.py`). If the
export or the restart fails, the VM is still in the boot restore set and comes
back at the next boot.

**Fix:** `export-all` should preserve the run state across the whole cycle and
restore it explicitly in the failure path.

### C3. `vmctl export-all` silently skips running VMs and exits 0

`cli/media.py:135-137` skips any VM that is powered on unless
`--include-running` is given. Skipped VMs are reported in the summary line
(`cli/media.py:156-161`) but **do not affect the exit code**
(`cli/media.py:164` → `return 1 if failed else 0`).

A nightly backup of a fleet where everything is running therefore produces
*zero* bundles and exits `0`. From cron's point of view that is a successful
backup of nothing.

**Fixed.** A skipped VM is a VM that was not backed up, and it now counts as a
failure: `export-all` exits non-zero when anything was skipped or failed, and
the summary says "skipped VMs were NOT backed up" (`cli/media.py`).

**Fix:** exit non-zero when anything was skipped, and give unattended use an
explicit profile where `--include-running` is the default.

### C4. A truncated backup looks like a valid backup

`export.py:187-189`: `_build_tar` writes straight to the final
`<vm>-<stamp>.vmhub.tar.gz` path. There is no temp-file-and-rename, and no
verification of the archive after it is written. If the filesystem fills during
the tar — the ordinary cause — you are left with a partial archive that
`vmctl bundles` (`export.py:330-346`) will list like any other, because the
manifest is written *inside* the tar before the disk member.

Compare the two places that get this right: `registry.save_meta`
(`registry.py:92-97`) and `import_bundle` (`export.py:284-286`) both stage and
`os.replace`.

**Fixed.** The archive is written to a `.partial` name and renamed into place
only once it is whole, so a truncated bundle never appears under the name a
backup is looked up by (`export.py`). A bundle that is finished gets a `.sha256`
sidecar beside it, holding the checksum of the compressed archive; `vmctl
bundles --verify` re-hashes and reports a damaged bundle, and exits non-zero.
There is also a free-space check before the copy starts, so a disk that is
nearly full says so instead of leaving a truncated file behind.

**Fix:** write to `.tmp` and rename; then reopen the archive and read the
manifest back before declaring success.

### C5. Deleting a VM whose container podman lost leaves QEMU running on a deleted disk

`lifecycle.remove` (`lifecycle.py:635-685`) guards on `podman.is_running(vm)`
(`lifecycle.py:639`) and raises `VmRunning`. But if podman has lost the
container, `is_running` is False even though the QEMU is very much alive — that
is exactly the situation `stop_leftovers` exists for (`lifecycle.py:450-519`),
and `remove()` never calls it.

So the path runs `shutil.rmtree(paths.vm_dir(vm))` (`lifecycle.py:683`) while
the leftover QEMU still has the disk open and running.

**Fix:** call `stop_leftovers(vm)` in `remove()` before touching the directory.

### C6. The checkout, the VM registry and the backup directory are one directory

`paths.vm_dir()` is `project_root()/<name>` (`paths.py:85`) and
`paths.backup_dir()` is `project_root()/backups` (`paths.py:143`).

So `git clean -fdx`, `rm -rf ~/vmhub/*`, or a "tidy up the workspace" step in an
agent's routine **destroys every VM and every backup on the box**. Nothing in
the layout separates project from data.

**Fix:** move the VM root and the backup directory out of the checkout, the way
the image cache was already moved out (`paths.py:121-128`).

### C7. Backups live inside the checkout, so they bloat every tree-level operation

The same root cause as C6, but cheap and independent: `backups/` holds
multi-GB bundles inside a git working tree. `paths.py:121-128` says exactly
why the ~400 MB cloud-image cache was moved to `~/.cache/vmhub/images` — "several
of them next to the VMs would be copied into every `git status` and every backup
of the project tree" — and the backup directory was never moved with it.

**Fix:** same as C6, or at minimum point `backup_dir()` at
`~/.local/share/vmhub/backups`.

---

## High

### H1. systemd's default 90 s `TimeoutStartSec` will kill `vmctl restore` part-way

The generated unit (`autostart.py:99-117`) is `Type=oneshot` and sets **no
timeout**. systemd's default `TimeoutStartSec` is 90 s, and it applies to
oneshot units.

`restore` starts VMs **serially** (`lifecycle.py:787-800`), and each one waits
up to `QEMU_READY_TIMEOUT = 90` s for QEMU to become ready
(`lifecycle.py:319`, `lifecycle.py:334-351`). Three slow VMs blow past 90 s:
systemd kills the unit, some VMs never come back, and the boot looks failed.
The same applies to `ExecStop=vmctl stop-all` against the 90 s
`TimeoutStopSec`, where each VM can take `shutdown.timeout + 20` s
(`spec.py:326`).

**Fixed.** The generated unit sets `TimeoutStartSec=0` and `TimeoutStopSec=0`
(`autostart.py`). A restore of several VMs is no longer killed part-way
through by systemd's 90 second default.

**Verify on the target host:** `systemd-analyze cat-config systemd/system.conf | grep DefaultTimeout`.

**Fix:** `TimeoutStartSec=0` and `TimeoutStopSec=0` in the unit, and/or
parallelise the restore.

### H2. The "make sure everything is up" job fails where it should repair

`lifecycle.start` refuses to start a VM whose host port is taken
(`lifecycle.py:252-270`) and only `--repair-ports` (`cli/parser.py:97-104`)
reassigns. A nightly health loop that just runs `vmctl start` therefore fails
for every VM whose port is held — including by the VM's *own* leftover
(`ports.py:278-287`).

**Fix:** a documented batch/health mode that repairs explicitly and reports
what changed, rather than making the caller know the flag.

### H3. `vmctl autostart install` exits 0 even when it failed

`cli/env.py:247-250` prints the lines returned by `autostart.install()` and
returns `0` unconditionally. `_enable_linger` (`autostart.py:328-355`) can
report *"could not enable lingering for <user> ... the unit only runs while you
are logged in, so VMs will NOT come back at boot"* — and the command still
exits `0`.

A provisioning script that trusts the exit code ships a box whose VMs never
come back.

**Fixed.** `autostart.install()` returns an `InstallResult` with the messages
*and* the problems, and `vmctl autostart install` exits non-zero when there are
any (`autostart.py`, `cli/env.py`). An install that cannot work at boot is no
longer a success.

**Fix:** exit non-zero unless the unit is enabled *and* lingering is on.

### H4. The owner is inferred from the environment; a cron context resolves it to root

`_vm_owner()` (`autostart.py:68-74`) uses `SUDO_USER` or `USER`, falling back to
`getpass.getuser()`. Under a **system cron job** or a non-login SSH session
those are unset or `root` — so the unit is written into `/root/.config/systemd/user/`
and enabled in root's manager, while rootless podman's store lives under the
real owner's home. Nothing starts, and every message says it worked.

This is precisely the failure the code comment at `autostart.py:46-51` was
written to prevent — but the prevention only covers the `sudo` case, not the
cron case. There is no `--owner` flag.

**Fixed.** `vmctl autostart install` takes `--owner`, and the install refuses
an owner that is root or that is not a real account on the host
(`autostart.py`, `resolve_owner`). A cron job has neither `SUDO_USER` nor
`USER`, so it now has to say who the VMs belong to rather than landing on root.

**Fix:** an explicit `--owner`, required when the install is not interactive.

### H5. `stop --mode graceful` on a Windows guest escalates to SIGKILL

Windows ignores the container's ACPI signal, so a graceful stop always ends in
SIGKILL — container exit 137 — with no guest filesystem flush
(`SKILL.md:206-210`, `lifecycle.py:543-553`).

The backup path stops VMs with `--mode graceful` by default
(`cli/parser.py:233-235` → `cli/media.py:141`), so **a nightly backup of a
Windows guest risks filesystem corruption every single night**.

**Fix:** a per-VM stop mode, defaulting to `guest` when the agent is present,
and never `graceful` for Windows.

### H6. `stop --mode guest` fails outright when the guest has no qemu-guest-agent

`lifecycle.py:592-600` raises `LifecycleError` if the guest agent does not
answer. Fixing H5 by switching to `guest` mode just relocates the failure
unless the agent first probes for it (`vmctl guest <vm>`) and falls back.

### H7. Backups silently strip the install media

`export.py:128-160` rebuilds the portable spec field by field, and `media` is
not among the copied fields — `media.install`, `media.drivers` and `media.seed`
are dropped, and `boot.iso` is forced to `""` (`export.py:132`).

So a backup taken while a VM is still being provisioned restores as a guest
with **no installer attached and no boot media** — unbootable.

**Fix:** refuse to export a VM whose disk holds no installed system, or carry
the media across explicitly.

### H8. Import drops `snapshot_power` and template linkage

`export.py:302-310` calls `registry.create_meta()` with only blueprint,
is_template, notes and (optionally) uuid/mac. `snapshot_power` and
`dependents` are lost — even though the manifest *does* carry
`meta.snapshot_power` (`export.py:179`). The data exists and is discarded.

Consequences: after a restore, `revert()` falls back to `was_running`
(`lifecycle/snapshot.py:140-142`), and deleting the restored template no
longer warns about its clones.

**Fix:** carry `snapshot_power` and `dependents` across on import.

### H9. Import reallocates host ports, so a restored service comes back elsewhere

`export.py:293-298` calls `ports.reallocate()`. Every firewall rule, reverse
proxy target, DNS record, bookmark and monitoring check pointing at the old
port breaks silently after a restore.

**Fix:** a `--keep-ports` option, and record the original ports in the manifest
so a restore script can reconcile them.

### H10. Leftover detection deliberately under-matches, so a restore can fail with the wrong symptom

`procs.py:280-285`: when the QEMU is already gone, the leftover forwarder is
matched only on the **exact set** of host ports, because a forwarder holding
one matching number "is more likely to belong to a different VM".

A leftover holding a *subset* of the VM's ports therefore does not match. The
port is then reported as "bound by another process" (`ports.py:284-287`),
`doctor` does not list it as a leftover, and `vmctl restore` fails for that VM
pointing at the wrong cause.

**Fix:** match on "holds any of this VM's ports" plus an ownership marker
recorded at publish time.

### H11. No locking anywhere — concurrent automation races

- `ports.port_is_free` (`ports.py:14-21`) is a check-then-bind TOCTOU: two
  concurrent `vmctl new` calls can pick the same port.
- `registry.save_meta` (`registry.py:84-101`) is atomic per file but takes no
  cross-process lock, so two saves of the same VM interleave.
- `vm.toml` and `meta.json` are separate files updated as a pair, with no
  transaction around them.

Overlapping cron jobs — backup while patching, say — can lose a port
assignment or half-apply a spec change.

**Fix:** an `flock` around every mutating entry point.

### H12. Changing a VM spec silently destroys and recreates its container

`_needs_recreate` (`lifecycle.py:165-174`) hashes the spec; any change means
`podman.rm(vm)` and a fresh container (`lifecycle.py:291-305`). The VM
**restarts**, and if the image is not local, `podman.pull` runs — at 3 a.m.
(`lifecycle.py:296-297`).

"Set the CPU count" therefore means "restart the VM", and it depends on the
network being up.

**Fix:** separate "apply now" from "apply at next start" in the CLI, and make
resource-only changes hot-reloadable.

### H13. A meta.json missing its `uuid` silently loses the VM's identity

`VmMeta.from_dict` (`registry.py:46-49`) passes only the keys present in the
file, so a missing `uuid` falls through to the dataclass default factory
(`registry.py:20`) — a **freshly random UUID**. `save_meta` then persists it.

A hand-edited or truncated `meta.json` therefore changes the VM's UUID with no
error. That changes the container's `vmhub.uuid` label and the `-uuid` QEMU is
given, so the guest sees a new SMBIOS identity, and `_needs_recreate` tears the
container down and rebuilds it.

`iter_metas` gets this right for unparseable files (`registry.py:129-135`,
empty strings, never a synthesized identity); `load_meta` (`registry.py:71-81`)
does not, for parseable-but-incomplete ones.

**Fix:** make missing `uuid`/`mac` a hard error in `load_meta`, exactly as
`iter_metas` treats a corrupt file.

---

## Medium

### M1. `doctor` has no machine-readable output and conflates severities

`cli/env.py:160-239` prints a human table and returns 0/1. A nightly check that
alerts on exit 1 fires identically for a missing `/dev/kvm` (`:168`) and for a
benign "port clash" (`:220-225`). There is no JSON mode — contrast
`vmctl ls --json` — and no severity levels.

**Fix:** `vmctl doctor --json` with a stable schema and a per-check severity.

### M2. `restore` reports per-VM failures only as prose

`lifecycle.py:794-800` catches the exception and passes it to `progress(...)`.
`cmd_restore` (`cli/vms.py:255-270`) returns 1, but no machine-readable list
of which VMs failed is produced. The systemd unit's stdout reaches the journal,
but a cron job has to scrape English prose.

**Fix:** a `--json` summary listing restored / failed / skipped.

### M3. Harness VMs are filtered from `restore` but not from backup or inventory

`HARNESS_PREFIXES` (`lifecycle.py:723`) is consulted in `restore_run_state`
(`lifecycle.py:790`) **and nowhere else**. `cmd_export_all`
(`cli/media.py:119`) uses `lifecycle.list_vms()` with no filter.

In this working tree there are already three such VMs — `web-a01`, `web-a02`,
`web-a05`, each with a real `meta.json` — and their names do not match the
harness prefixes. `doctor` counts them, `export-all` will try to back them up,
and they are all candidates for boot restore.

**Fix:** filter harness VMs everywhere, and delete these three.

### M4. Port probing and port publishing use different addresses

`ports.PROBE_HOST` is `127.0.0.1` (`ports.py:11`), while a VM with
`network.bind = "0.0.0.0"` publishes on every interface
(`podman.py:287-288`). A service bound to a *specific* external address is
invisible to the probe, so vmhub believes the port is free and podman fails to
publish it at start.

**Fix:** probe the address that will actually be published —
`spec.effective_bind` (`spec.py:78-88`) is already the single source of truth
for that.

### M5. A backup of a linked clone silently loses the clone's internal snapshots

`export.py:91-101`: a backing chain is resolved with `qemu-img convert`, which
flattens the image and drops its internal snapshots. The manifest reads the
snapshot list off the *exported* disk (`export.py:118`) so it honestly reports
the loss — but the backup no longer contains them.

**Fix:** re-apply the snapshots after conversion, or exclude them explicitly
and say so in the manifest.

### M6. Backing-chain resolution makes a clone's backup as large as its template

`export.py:91-101` copies the chain's entire used data. Three clones of one
template means three full copies of the template. There is no space accounting
and no preflight check before the copy starts.

**Fix:** report the chain's real size before starting, and check free space.

### M7. `backups/` has no retention policy and grows without bound

`paths.backup_dir()` (`paths.py:143`) is a plain directory; nothing prunes it.
Retention has to be implemented by the automation — and must not prune the
bundle currently being written, or the newest N.

### M8. `vmctl backup` is a single-VM alias for `export`, not a fleet backup

`cli/parser.py:243`. An agent reaching for "backup" gets one VM; the fleet
operation is `export-all`.

### M9. `podman.rm()` with a container name is a silent no-op that still succeeds

`podman.py:334-341`: `rm(vm)` adds the `vmhub-` prefix itself and returns
early if the container does not exist. Passing `"vmhub-foo"` makes it look for
`vmhub-vmhub-foo`, remove nothing, and exit 0.

`prune_orphans` guards this with a post-check (`podman.py:442`), but a direct
call from a script does not.

### M10. Cross-VM traffic needs a second host address, and the default bind hides it

`SKILL.md:236-257`: both guests report the host's own address (`192.168.1.3`),
and loopback is never shared between guests. To reach one guest from another
you must dial a *different* host address, and the target must have
`network.bind = 0.0.0.0` — the default is loopback (`spec.py:72-75`).

For a box whose entire purpose is running several services that talk to each
other, this reads as a network failure rather than a configuration default.

### M11. Boot restore is serial and can exceed the hook's retry budget

`lifecycle.py:787-800` starts VMs one at a time, each up to 90 s. The sysvinit
hook's budget is 5 attempts (`autostart.py:243-257`), but one attempt already
covers every VM — so a single slow VM consumes the entire budget and the hook
reports "gave up after 5 attempts" even though the user's own VMs are fine.

### M12. `media.install` / `media.drivers` / `media.seed` are absolute host paths

Per `SKILL.md:114-117`, a spec copied to another machine fails `validate()` at
start. Export happens to drop them (H7), which hides the problem in bundles but
not in a hand-copied tree.

### M13. `.gitignore` hides every new top-level file

The root `/*` rule (`.gitignore:5`) swallows anything not explicitly
un-ignored — confirmed with `git check-ignore`: both `SHARP-EDGES.md` and
`docs/` are ignored. The file itself documents the trap for `screenshot.png`.

Any automation that drops a script or config at the repo root will find it
silently untracked, and `git clean -fdx` will delete it.

### M14. `vmctl exec` needs a working SSH path into the guest

(`SKILL.md:137`, `SKILL.md:162-179`): the guest needs sshd, and the agent needs
the right key. `provision` seeds it from the user's SSH key
(`SKILL.md:148-149`), so a guest patched over SSH is only reachable if the key
used at provision time is the one in hand.

---

## Low

- **L1.** `_iter_vm_dirs` (`registry.py:52-60`) treats *any* directory containing
  a `meta.json` as a VM, so a stray directory from a half-finished import
  becomes a phantom VM.
- **L2.** `_assert_disk_adopted` (`lifecycle.py:193-207`) adds up to 20 s to
  every start and only warns on timeout — it compounds H1.
- **L3.** The unit's `After=default.target` together with
  `WantedBy=default.target` (`autostart.py:102`, `autostart.py:116`) puts the
  restore late in boot, which is the worst possible moment to hit the 90 s
  default timeout in H1.
- **L4.** `qga.py` must send `guest-sync-delimited` first (`SKILL.md:57-61`); if
  that protocol detail regresses, QGA calls silently return nothing.
- **L5.** `vmctl rm --keep-disk` must not be "simplified" into deleting
  `meta.json` (`SKILL.md:73-77`) — discovery needs that file, so the disk
  becomes invisible with no way back.

---

## Suggested order of work

**Done — the "before running anything unattended" set:**

1. ~~C3 — the backup silently does nothing and reports success.~~ **Fixed.**
2. ~~C4 — the backup can be a corrupt file that looks fine.~~ **Fixed.**
3. ~~C2 — a failed backup takes the VM out of service permanently.~~ **Fixed.**
4. ~~C1 — stopping a VM un-registers it from autostart.~~ **Fixed.**
5. ~~H1 — systemd kills the restore part-way once you have more than a couple
   of VMs.~~ **Fixed.**
6. ~~H3 — provisioning reports success on a box whose VMs never return.~~ **Fixed.**
7. ~~H4 — the cron context installs the unit for the wrong user.~~ **Fixed.**

**Next, for correctness of the fleet:**

8. H5 / H6 — Windows guests are SIGKILLed by the nightly backup.
9. H7 / H8 / H9 — a restore is not a faithful restore.
10. M3 — the stale `web-a01` / `web-a02` / `web-a05` VMs.
11. C5 / H13 — the two data-integrity items.
12. H10 / H11 / H12 — the concurrency and container-recreation items.

**Then, observability:**

13. M1 / M2 — JSON output for `doctor` and `restore`, so the health check can
    alert on severity instead of on a single exit code.

**Still open from the earlier list, not yet scheduled:**

- C6 / C7 — the checkout, the VM registry and the backup directory are one
  directory. This one changes where everybody's VMs live, so it is worth
  doing deliberately rather than as part of a maintenance pass.

---

## Things that are already handled — do not "fix" them

A previous pass already addressed these; they are listed so a later change
does not undo them.

- Boot races on `/run/user/<uid>` and the `/dev/kvm` ACL are waited out in
  `lifecycle.wait_until_podman_can_run` (`lifecycle.py:733-777`), called from
  `cmd_restore` (`cli/vms.py:261-262`), and again in shell in the sysvinit hook
  (`autostart.py:158-204`).
- The owner-vs-root problem at boot is handled for the `sudo` case
  (`autostart.py:45-56`, `autostart.py:358-383`) — but **not** the cron case
  (H4).
- `stop-all --preserve-run-state` in the systemd unit (`autostart.py:113`)
  prevents the shutdown hook from cancelling the next boot's restore.
- PID reuse is guarded by `procs.still_matches` (`procs.py:408-416`).
- `meta.json` writes are atomic (`registry.py:92-97`) — but only per file, and
  not for a missing `uuid` key (H13).
- The archive manifest is written first so an import can short-circuit before
  decompressing a multi-GB disk (`export.py:39-57`).
