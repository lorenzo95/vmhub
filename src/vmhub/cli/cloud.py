"""Provisioning from cloud images, and the image cache they use."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .. import (
    blueprints,
    images,
    provision,
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

    # The whole flow -- fetch, create, convert, seed, start, wait -- is
    # shared with the GUI's Create button; see vmhub.provision.
    try:
        payload = provision.provision(
            name,
            blueprint=blueprint,
            image=args.image,
            user_data=user_data,
            # A custom seed may create any account; only the default seed
            # knows the user it made, so only then record it.
            user=None if args.seed else args.user,
            cpus=args.cpus,
            ram=args.ram,
            disk=args.disk,
            disk_type=args.disk_type,
            sha512=args.sha512,
            wait=not args.no_wait,
            timeout=args.timeout,
            progress=out.step,
        )
    except VmhubError as exc:
        print(error_text(exc))
        if registry.exists(name):
            out.say(
                f"  the VM exists but is not fully provisioned; "
                f"remove it with: vmctl rm -y {name}"
            )
        return 1

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    port = payload["port"]
    user, _, host = payload["ssh"].partition("@")
    out.say()
    out.say(out.good(f"{name} is up"))
    if port:
        out.say(f"  ssh   {out.info(f'ssh -p {port} {user}@{host}')}")
        out.say(f"  exec  {out.info(f'vmctl exec {name} -- <command>')}")
    if payload["console"]:
        out.say(f"  view  {out.info(payload['console'])}")
    return 0
