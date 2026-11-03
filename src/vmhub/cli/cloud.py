"""Provisioning from cloud images, and the image cache they use."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .. import (
    blueprints,
    cloudinit,
    images,
    iso as isomod,
    lifecycle,
    registry,
    seed,
    spec,
)
from ..errors import VmhubError
from .output import Out
from .vms import error_text


def cmd_images(args: argparse.Namespace, out: Out) -> int:
    rows = images.inventory()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    out.say(out.bold(f"{'KEY':22s} {'CACHED':7s} {'SIZE':>8s}  FILE"))
    for row in rows:
        cached = out.good("yes") if row["cached"] else out.dim("no")
        size = spec.format_size(row["size"]) if row["size"] else "-"
        out.say(f"{row['key']:22s} {cached:7s} {size:>8s}  {out.dim(row['filename'])}")
    if not any(row["cached"] for row in rows):
        out.say()
        out.say(out.dim("  nothing cached yet; `vmctl provision` fetches on first use"))
    return 0


def cmd_provision(args: argparse.Namespace, out: Out) -> int:
    name = args.name
    blueprint = args.blueprint or "debian13-cloud"
    try:
        blueprints.load(blueprint)
    except VmhubError as exc:
        print(out.bad(str(exc)))
        return 2

    if registry.exists(name):
        print(out.bad(f"VM already exists: {name}"))
        out.say(f"  delete it first: vmctl rm -y {name}")
        return 1

    if args.seed:
        try:
            user_data = seed.read_user_data(Path(args.seed))
        except VmhubError as exc:
            print(out.bad(str(exc)))
            return 2
    else:
        keys: list[str] = []
        for key in args.ssh_key:
            key_path = Path(key).expanduser()
            if not key_path.is_file():
                print(out.bad(f"no such SSH public key: {key_path}"))
                return 2
            try:
                keys.append(key_path.read_text().strip())
            except OSError as exc:
                print(out.bad(f"cannot read {key_path}: {exc}"))
                return 2
        if not keys:
            keys = seed.discover_ssh_keys()
        try:
            user_data = seed.default_user_data(
                hostname=name, user=args.user, ssh_keys=keys
            )
        except VmhubError as exc:
            print(out.bad(str(exc)))
            return 2

    # Fetch before creating anything, so a 400 MB download that fails does
    # not leave a half-made VM behind.
    try:
        source, digest = images.fetch(
            args.image, expect_sha512=args.sha512, progress=out.step
        )
    except VmhubError as exc:
        print(out.bad(str(exc)))
        return 1

    try:
        lifecycle.create(
            name,
            blueprint=blueprint,
            cpus=args.cpus,
            ram=args.ram,
            disk_size=args.disk,
            disk_type=args.disk_type,
            boot_iso="",
            progress=out.step,
        )
        created = registry.load_spec(name)
        if not args.seed:
            # `--user` changes the guest account, so record it: the
            # blueprint's ssh_user is only the default and exec/ssh would
            # otherwise log in as the wrong person.
            created.ssh_user = args.user
        registry.save_spec(created)
    except VmhubError as exc:
        print(error_text(exc))
        return 1

    try:
        record = images.install(
            name, source, progress=out.step, key=args.image, digest=digest
        )
        iso_path = seed.build(name, user_data=user_data, progress=out.step)
        lifecycle.attach_iso(name, isomod.SEED_SLOT, iso_path, progress=out.step)
    except VmhubError as exc:
        print(out.bad(f"provisioning {name} failed: {exc}"))
        out.say(
            f"  the VM exists but is not fully provisioned; "
            f"remove it with: vmctl rm -y {name}"
        )
        return 1

    try:
        lifecycle.start(name, progress=out.step)
    except VmhubError as exc:
        print(error_text(exc))
        return 1

    if not args.no_wait:
        try:
            cloudinit.wait(name, timeout=args.timeout, progress=out.step)
        except VmhubError as exc:
            print(out.bad(str(exc)))
            return 1

    vm_spec = registry.load_spec(name)
    port = vm_spec.network.host_for("ssh")
    host = vm_spec.network.bind or "127.0.0.1"
    user = lifecycle.spec_user_hint(name)
    payload = {
        "name": name,
        "state": "running",
        "image": record.get("image", Path(source).name),
        "sha512": digest,
        "console": lifecycle.console_url(name),
        "ssh": f"{user}@{host}",
        "port": port,
        "cloud_init": "done" if not args.no_wait else "not waited for",
        "seed": str(iso_path),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    out.say()
    out.say(out.good(f"{name} is up"))
    if port:
        out.say(f"  ssh   {out.info(f'ssh -p {port} {user}@{host}')}")
        out.say(f"  exec  {out.info(f'vmctl exec {name} -- <command>')}")
    if payload["console"]:
        out.say(f"  view  {out.info(payload['console'])}")
    return 0
