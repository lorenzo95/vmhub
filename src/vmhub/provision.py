"""Provisioning a VM from a cloud image + cloud-init seed.

The one flow behind `vmctl provision` and the GUI's Create button when
the chosen blueprint carries an `image` key. A cloud blueprint cannot be
created the install way: `lifecycle.create` alone leaves an empty disk
with no image and no seed, and the VM has nothing to boot.

The order matters and is the same everywhere: fetch the image *before*
creating anything (a failed 400 MB download must not leave a half-made
VM), convert it into the VM's own disk (not boot media, so snapshots and
clones operate on the right drive), attach the CIDATA seed as the second
CD-ROM, start, and wait for cloud-init to finish.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import blueprints, cloudinit, images, iso as isomod, lifecycle, registry, seed

Progress = Callable[[str], None]


def provision(
    name: str,
    *,
    blueprint: str = "debian13-cloud",
    image: str = "",
    user_data: str,
    user: str | None = None,
    cpus: int | None = None,
    ram: str | None = None,
    disk: str | None = None,
    disk_type: str | None = None,
    sha512: str = "",
    wait: bool = True,
    timeout: float = cloudinit.DEFAULT_TIMEOUT,
    progress: Progress | None = None,
) -> dict:
    """Create, seed, start and (by default) wait for a cloud-image VM.

    `user` records the guest account the seed creates, so exec/ssh log in
    as the right person; None leaves the blueprint's ssh_user alone (a
    custom seed may create any account). Raises VmhubError on failure;
    if the VM was already created it is left in place for inspection.
    Returns the same summary payload `vmctl provision --json` prints.
    """
    say = progress or (lambda _m: None)
    if not image:
        image = blueprints.load(blueprint).image or "debian13"

    # Fetch before creating anything, so a download that fails does not
    # leave a half-made VM behind.
    source, digest = images.fetch(image, expect_sha512=sha512, progress=say)

    lifecycle.create(
        name,
        blueprint=blueprint,
        cpus=cpus,
        ram=ram,
        disk_size=disk,
        disk_type=disk_type,
        boot_iso="",
        progress=say,
    )
    if user is not None:
        # The blueprint's ssh_user is only the default; a seed that creates
        # a different account must win, or exec/ssh log in as the wrong one.
        created = registry.load_spec(name)
        created.ssh_user = user
        registry.save_spec(created)

    record = images.install(name, source, progress=say, key=image, digest=digest)
    iso_path = seed.build(name, user_data=user_data, progress=say)
    lifecycle.attach_iso(name, isomod.SEED_SLOT, iso_path, progress=say)
    lifecycle.start(name, progress=say)
    if wait:
        cloudinit.wait(name, timeout=timeout, progress=say)

    vm_spec = registry.load_spec(name)
    port = vm_spec.network.host_for("ssh")
    host = vm_spec.network.bind or "127.0.0.1"
    ssh_user = lifecycle.spec_user_hint(name)
    return {
        "name": name,
        "state": "running",
        "image": record.get("image", Path(source).name),
        "sha512": digest,
        "console": lifecycle.console_url(name),
        "ssh": f"{ssh_user}@{host}",
        "port": port,
        "cloud_init": "done" if wait else "not waited for",
        "seed": str(iso_path),
    }
