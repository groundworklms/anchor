"""Unit tests for uplink_routes() in src/api/network_status.py.

The banner has to be driven by a real interface check, not a hardcoded string -- a judge
should be able to plug a cable in and watch it change, then pull it and watch it change
back. uplink_routes() is the part of that which is pure: it takes the parsed output of
`ip -json route show default` and decides which of those routes count as an uplink.

The rest of the module shells out to `ip` or opens a TCP connection, so it is out of
scope here by construction.
"""
import pytest

import network_status


def route(dev, gateway="192.168.1.1", dst="default"):
    """A row as `ip -json route show default` emits it."""
    return {"dst": dst, "gateway": gateway, "dev": dev, "flags": []}


def test_usb_device_link_route_is_not_an_uplink():
    # The bug this function was added for. The Jetson's device-mode bridge installs a
    # default route via l4tbr0, so the board reported "LINK UP - NO INTERNET" whenever the
    # USB cable to the workstation was attached -- which is exactly how it is
    # administered. It could never reach AIR-GAPPED, so the one moment in the demo that
    # proves the offline claim would not have fired.
    assert network_status.uplink_routes([route("l4tbr0")]) == []


@pytest.mark.parametrize("dev", sorted(network_status.DEV_LINK))
def test_every_dev_link_interface_is_excluded(dev):
    # DEV_LINK covers the several names the gadget driver uses across L4T releases. A
    # rename on a kernel bump would silently reopen the same bug, so all of them are
    # pinned rather than just the one observed.
    assert network_status.uplink_routes([route(dev)]) == []


def test_real_ethernet_route_is_kept():
    # The honest inconvenient case: a cable is genuinely in. This must survive, or the
    # banner claims AIR-GAPPED while a wire is plugged in, which is the dishonest
    # direction and the one that would destroy the demo's credibility if noticed.
    r = route("eth0")
    assert network_status.uplink_routes([r]) == [r]


def test_wifi_route_is_kept():
    r = route("wlan0", gateway="10.0.0.1")
    assert network_status.uplink_routes([r]) == [r]


def test_mixed_routes_keep_only_the_real_uplink():
    # The everyday development state: USB cable attached AND ethernet up.
    dev_link = route("l4tbr0", gateway="192.168.55.100")
    real = route("eth0")
    assert network_status.uplink_routes([dev_link, real]) == [real]


def test_order_of_surviving_routes_is_preserved():
    # status() reports these by name in the payload the UI renders; reordering them
    # would make the banner detail disagree with `ip route` for no reason.
    rows = [route("eth0"), route("l4tbr0"), route("wlan0"), route("usb0")]
    assert [r["dev"] for r in network_status.uplink_routes(rows)] == ["eth0", "wlan0"]


def test_no_routes_at_all():
    # The genuinely air-gapped board. Empty in, empty out -- no default route is not an
    # error condition.
    assert network_status.uplink_routes([]) == []


def test_route_without_a_dev_key_is_kept():
    # `ip -json` is not schema-guaranteed across versions. A row with no dev must not
    # raise a KeyError inside the banner check; the failure mode of an unknown route is
    # to report it, because reachability is probed separately and decides anyway.
    r = {"dst": "default", "gateway": "192.168.1.1"}
    assert network_status.uplink_routes([r]) == [r]


def test_loopback_and_dev_link_sets_are_disjoint():
    # lo is filtered in interfaces() and the gadget links in uplink_routes(). If the two
    # sets ever overlapped, one of the two filters would be dead code and the bug it
    # guards against would come back silently.
    assert network_status.LOOPBACK & network_status.DEV_LINK == set()
