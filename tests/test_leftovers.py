"""The processes a VM leaves behind when podman loses its container.

podman owns a VM's container, but the processes inside it are ordinary
host processes. When podman loses the container's record -- a storage
reset, a wiped database, a container removed while its processes were
still running -- those processes carry on: the QEMU holds the disk open,
the port forwarder holds the host ports, and vmhub calls the VM
"stopped" while all of it is still true.

These tests pin down the three things that make that state survivable:
the processes are found again, the VM is reported as the running thing
it is, and stopping it actually stops it.
"""

from __future__ import annotations

import pytest

from vmhub import lifecycle, paths, podman, ports, procs, registry, spec

import vmhub.lifecycle.lifecycle as lifecycle_mod

from vmhub.lifecycle.status import status as vm_status


# ---------------------------------------------------------------- the scanner


def test_the_uuid_is_read_from_the_qemu_command_line() -> None:
    """The UUID on QEMU's command line is the VM's own UUID."""
    argv = ["qemu-system-x86_64", "-uuid", "a71fe736-d29c", "-m", "16G"]
    assert procs._flag_value(argv, "-uuid") == "a71fe736-d29c"


def test_a_joined_flag_is_read_too() -> None:
    """QEMU and conmon both accept the joined spelling."""
    assert procs._flag_value(["qemu-system-x86_64", "-uuid=abc"], "-uuid") == "abc"


def test_the_host_ports_are_read_from_the_forwarders_arguments() -> None:
    """pasta is told them as -t 127.0.0.1/8006-8006:8006-8006."""
    argv = [
        "/usr/bin/pasta",
        "--config-net",
        "-t",
        "127.0.0.1/2222-2222:22-22",
        "-t",
        "127.0.0.1/8006-8006:8006-8006",
    ]
    assert procs._forwarded_ports(argv) == (2222, 8006)


def test_a_forwarder_is_matched_by_the_namespace_it_shares_with_the_qemu() -> None:
    """The namespace is the one link that cannot be mistaken.

    The forwarder and the QEMU inside the container are in the same
    network namespace, and the number of that namespace is the same on
    both sides. The name of it is not: podman deletes the path when the
    container goes away.
    """
    snapshot = procs.Snapshot(
        qemus={"uuid-1": procs.Qemu(pid=100, uuid="uuid-1", netns=4026533354)},
        forwarders=[
            procs.Forwarder(pid=200, host_ports=(8006,), netns=4026533354),
            procs.Forwarder(pid=201, host_ports=(8007,), netns=4026533928),
        ],
    )
    found = procs.leftovers(snapshot, uuid="uuid-1", container="vmhub-demo")
    assert found.qemu is not None and found.qemu.pid == 100
    assert found.forwarder is not None and found.forwarder.pid == 200


def test_a_forwarder_is_matched_by_its_ports_when_the_qemu_is_gone() -> None:
    """The QEMU can die and leave the forwarder holding the ports."""
    snapshot = procs.Snapshot(
        forwarders=[procs.Forwarder(pid=200, host_ports=(8006,), netns=0)],
    )
    found = procs.leftovers(
        snapshot, uuid="uuid-1", container="vmhub-demo", host_ports=[8006]
    )
    assert found.forwarder is not None and found.forwarder.pid == 200


def test_a_forwarder_holding_one_of_our_ports_is_not_ours() -> None:
    """One number in common is not an identity: another VM used that
    number before we did."""
    snapshot = procs.Snapshot(
        forwarders=[procs.Forwarder(pid=200, host_ports=(8006, 9001), netns=0)],
    )
    found = procs.leftovers(
        snapshot, uuid="uuid-1", container="vmhub-demo", host_ports=[8006]
    )
    assert found.forwarder is None


def test_another_vms_forwarder_is_not_mistaken_for_this_one() -> None:
    """Two VMs can publish the same base port number at different times."""
    snapshot = procs.Snapshot(
        forwarders=[procs.Forwarder(pid=200, host_ports=(8007,), netns=0)],
    )
    found = procs.leftovers(
        snapshot, uuid="uuid-1", container="vmhub-demo", host_ports=[8006]
    )
    assert found.forwarder is None


def test_the_container_supervisor_is_matched_by_its_container_name() -> None:
    """conmon is told which container it is supervising."""
    snapshot = procs.Snapshot(
        conmons={"vmhub-demo": procs.Conmon(pid=300, container="vmhub-demo")}
    )
    found = procs.leftovers(snapshot, uuid="uuid-1", container="vmhub-demo")
    assert found.conmon is not None and found.conmon.pid == 300


def test_a_process_is_only_stopped_if_it_is_still_the_one_it_was_found_as(
    monkeypatch,
) -> None:
    """A pid is reused. Between finding a leftover and stopping it, the
    number may belong to something else entirely."""
    monkeypatch.setattr(procs, "_cmdline", lambda pid: ["qemu-system-x86_64", "-uuid", "other"])
    assert not procs.still_matches(100, flag="-uuid", value="mine")


# ------------------------------------------------------------ the port story


def _vm_with_leftover(make_vm, minimal_spec_data, monkeypatch, name="demo"):
    """A VM whose container podman lost, with its forwarder still holding
    the host ports."""
    spec_data = dict(minimal_spec_data)
    spec_data["network"] = dict(spec_data["network"])
    spec_data["network"]["ports"] = [
        {"guest": 8006, "host": 8006, "role": "web"},
        {"guest": 22, "host": 2222, "role": "ssh"},
    ]
    make_vm(name, meta={"uuid": "uuid-1"}, spec_data=spec_data)
    snapshot = procs.Snapshot(
        qemus={"uuid-1": procs.Qemu(pid=100, uuid="uuid-1", netns=4026533354)},
        forwarders=[
            procs.Forwarder(pid=200, host_ports=(2222, 8006), netns=4026533354)
        ],
        conmons={"vmhub-demo": procs.Conmon(pid=300, container="vmhub-demo")},
    )
    monkeypatch.setattr(procs, "scan", lambda: snapshot)
    monkeypatch.setattr(ports, "port_is_free", lambda *_a, **_k: False)
    # The processes are still the ones they were found as.
    monkeypatch.setattr(procs, "still_matches", lambda pid, **_k: True)
    return name


def test_a_port_held_by_the_vms_own_leftover_is_not_a_foreign_process(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    """The port is not in use by anything else: it is held by this VM's
    own previous instance, and saying otherwise sends the user looking
    for a process they do not have."""
    name = _vm_with_leftover(make_vm, minimal_spec_data, monkeypatch)
    problems = ports.conflicts_for(name)
    assert problems, "the port really is busy"
    assert all("leftover instance" in problem for problem in problems), problems
    assert not any("another process" in problem for problem in problems), problems


def test_a_port_held_by_a_foreign_process_is_still_reported_as_one(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    """A leftover of another VM is still somebody else's process."""
    name = _vm_with_leftover(make_vm, minimal_spec_data, monkeypatch)
    monkeypatch.setattr(
        procs,
        "scan",
        lambda: procs.Snapshot(
            forwarders=[
                procs.Forwarder(pid=200, host_ports=(9001, 9002), netns=99999)
            ]
        ),
    )
    problems = ports.conflicts_for(name)
    assert any("another process" in problem for problem in problems), problems


def test_a_running_vm_holds_its_own_ports_and_is_not_a_leftover(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    """The forwarder of a live container is the VM's own, not a leftover.

    Calling it a leftover would tell the user their running VM is in a
    broken state, and would make the ports table shout at them.
    """
    name = _vm_with_leftover(make_vm, minimal_spec_data, monkeypatch)
    monkeypatch.setattr(podman, "is_running", lambda vm: True)
    vm_spec = registry.load_spec(name)
    assert ports.leftover_ports(name, vm_spec) == set()
    assert ports.own_forwarder_ports(name, vm_spec) == {2222, 8006}


# ------------------------------------------------------------- what vmhub says


def test_a_vm_whose_container_podman_lost_is_reported_as_running(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    """It is running. Its QEMU is alive and holding the disk open."""
    name = _vm_with_leftover(make_vm, minimal_spec_data, monkeypatch)
    monkeypatch.setattr(podman, "ps", lambda: {})
    item = vm_status(name)
    assert item.powered is True
    assert item.summary == "running"
    assert item.leftover is True
    assert 100 in item.leftover_pids
    assert sorted(item.leftover_ports) == [2222, 8006]


def test_the_leftover_is_explained_rather_than_left_to_be_guessed(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    name = _vm_with_leftover(make_vm, minimal_spec_data, monkeypatch)
    monkeypatch.setattr(podman, "ps", lambda: {})
    item = vm_status(name)
    assert any("podman lost" in note for note in item.notices), item.notices


def test_a_vm_with_a_container_is_not_reported_as_a_leftover(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    """A VM podman still manages is podman's business, not ours."""
    name = _vm_with_leftover(make_vm, minimal_spec_data, monkeypatch)
    monkeypatch.setattr(
        podman,
        "ps",
        lambda: {
            name: podman.Container(
                name=podman.container_name(name),
                vm=name,
                state="running",
                labels={podman.LABEL_MANAGED: "true"},
            )
        },
    )
    item = vm_status(name)
    assert item.leftover is False


# --------------------------------------------------------------- stopping it


def test_stopping_a_vm_with_no_container_still_stops_the_leftover(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    """podman has nothing to stop, so vmhub stops the processes itself.

    Without this, "stop" reported that there was no container and left
    the QEMU running for ever.
    """
    name = _vm_with_leftover(make_vm, minimal_spec_data, monkeypatch)
    monkeypatch.setattr(podman, "exists", lambda vm: False)
    monkeypatch.setattr(podman, "is_running", lambda vm: False)

    killed: list[int] = []
    monkeypatch.setattr(procs, "terminate", lambda pid, **_k: killed.append(pid) or True)
    monkeypatch.setattr(procs, "children_of", lambda pid: [111])
    monkeypatch.setattr(procs, "process_tree", lambda pid: [])
    monkeypatch.setattr(procs, "signal_pid", lambda pid, sig: True)
    # The guest shuts down when it is asked to, so the QEMU is gone by
    # the time the cleanup looks for it.
    monkeypatch.setattr(procs, "alive", lambda pid: False)

    lifecycle.stop(name)

    assert 200 in killed, "the port forwarder holding the ports was not stopped"
    assert 300 in killed, "the container's supervisor was left running"


def test_the_guest_is_asked_to_shut_down_before_it_is_cut_off(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    """The container's own init gets the same signal podman would send it,
    so the image's shutdown logic -- an ACPI powerdown -- still runs."""
    name = _vm_with_leftover(make_vm, minimal_spec_data, monkeypatch)
    monkeypatch.setattr(podman, "exists", lambda vm: False)
    monkeypatch.setattr(podman, "is_running", lambda vm: False)

    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        procs, "signal_pid", lambda pid, sig: signalled.append((pid, sig)) or True
    )
    monkeypatch.setattr(procs, "children_of", lambda pid: [111])
    monkeypatch.setattr(procs, "process_tree", lambda pid: [])
    monkeypatch.setattr(procs, "terminate", lambda pid, **_k: True)
    monkeypatch.setattr(procs, "alive", lambda pid: False)

    lifecycle.stop(name)

    import signal as signal_mod_

    assert (111, signal_mod_.SIGTERM) in signalled, signalled


def test_stopping_a_vm_with_no_container_and_no_leftover_says_so(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    name = _vm_with_leftover(make_vm, minimal_spec_data, monkeypatch)
    monkeypatch.setattr(procs, "scan", lambda: procs.Snapshot())
    monkeypatch.setattr(podman, "exists", lambda vm: False)
    monkeypatch.setattr(podman, "is_running", lambda vm: False)
    said: list[str] = []
    lifecycle.stop(name, progress=said.append)
    assert any("no container" in line for line in said), said


# --------------------------------------------------------------- starting it


def test_starting_reclaims_the_leftover_before_it_looks_at_the_ports(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    """The leftover holds the very ports the start is about to check, so
    the order is the whole fix."""
    name = _vm_with_leftover(make_vm, minimal_spec_data, monkeypatch)
    monkeypatch.setattr(podman, "is_running", lambda vm: False)
    monkeypatch.setattr(podman, "exists", lambda vm: False)
    monkeypatch.setattr(podman, "image_present", lambda ref: True)

    order: list[str] = []
    monkeypatch.setattr(
        lifecycle_mod, "stop_leftovers", lambda vm, **_k: order.append("reclaim") or []
    )
    monkeypatch.setattr(
        lifecycle_mod.ports, "conflicts_for", lambda vm: order.append("ports") or []
    )
    monkeypatch.setattr(lifecycle_mod.ports, "ensure_for", lambda vm: [])

    class StopHere(Exception):
        pass

    def _stop_here(*_a, **_k):
        raise StopHere

    monkeypatch.setattr(lifecycle_mod.podman, "create", _stop_here)

    with pytest.raises(StopHere):
        lifecycle.start(name)

    assert order[:2] == ["reclaim", "ports"], order


# ------------------------------------------------- how the failure is shown


def test_the_port_conflict_carries_its_advice_apart_from_its_message(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    """The CLI shows a command to run; the GUI shows a button that does
    the same thing. Neither has to parse the other's words, so the
    advice is not part of the message."""
    from vmhub.errors import PortConflict

    name = _vm_with_leftover(make_vm, minimal_spec_data, monkeypatch)
    monkeypatch.setattr(lifecycle_mod.podman, "is_running", lambda vm: False)
    monkeypatch.setattr(lifecycle_mod.podman, "exists", lambda vm: False)
    monkeypatch.setattr(lifecycle_mod.podman, "image_present", lambda ref: True)
    monkeypatch.setattr(lifecycle_mod.ports, "ensure_for", lambda vm: [])
    monkeypatch.setattr(lifecycle_mod.ports, "port_is_free", lambda *_a, **_k: False)

    with pytest.raises(PortConflict) as caught:
        lifecycle.start(name)

    error = caught.value
    assert error.vm == name
    assert "vmctl" not in str(error), str(error)
    assert "vmctl" in error.hint, error.hint


def test_the_cli_prints_the_advice_the_gui_hides() -> None:
    """The command is the CLI's answer; the GUI has a button instead."""
    from vmhub import cli
    from vmhub.errors import PortConflict

    error = PortConflict("demo cannot start:\n  web host port 8006 is taken", hint="  fix: vmctl repair-ports demo")
    assert "vmctl repair-ports demo" in cli.error_text(error)
    assert "vmctl repair-ports demo" not in str(error)


# ------------------------------------------------------- what the Fix does


def test_the_fix_moves_the_port_and_the_vm_can_then_start(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    """What the GUI's Fix button does, and what --repair-ports does.

    Two VMs claim the same host port. The second one is refused, and the
    repair is to give it a port of its own -- the number in vm.toml
    changes, which is why the GUI says so out loud afterwards.
    """
    from vmhub.errors import PortConflict

    spec_data = dict(minimal_spec_data)
    spec_data["network"] = dict(spec_data["network"])
    spec_data["network"]["ports"] = [{"guest": 8006, "host": 8006, "role": "web"}]
    make_vm("first", meta={"uuid": "uuid-1"}, spec_data=spec_data)
    make_vm("second", meta={"uuid": "uuid-2"}, spec_data=spec_data)

    monkeypatch.setattr(podman, "is_running", lambda vm: False)
    monkeypatch.setattr(podman, "exists", lambda vm: False)
    monkeypatch.setattr(ports, "port_is_free", lambda *_a, **_k: True)

    with pytest.raises(PortConflict):
        lifecycle.start("second")

    changes = ports.ensure_for("second")
    assert changes, "the clashing port was left alone"
    assert ports.conflicts_for("second") == []
