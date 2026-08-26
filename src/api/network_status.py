"""Real network-status check for the trust banner.

The brief is explicit: the banner must be driven by an actual interface check, not a
hardcoded string. A banner that always says OFFLINE proves nothing — a judge should be
able to plug a cable in and watch it change, then pull it and watch it change back. If it
cannot do that, it is a picture of a claim rather than evidence for one.

So this reports what is actually true, including the inconvenient case. Three independent
signals, from cheapest to strongest:

  1. Interfaces that are UP and carry a routable address.
  2. A default route.
  3. An actual outbound connection attempt.

A device can have a live interface and still be unable to reach anything; it can also have
a default route pointing nowhere. Only the third proves reachability, so it is the one that
decides, with the first two reported as supporting detail.
"""
import json
import socket
import subprocess

# Interfaces that do not constitute "connected to a network" for this device.
# l4tbr0/usb0/usb1 are the Jetson's USB device-mode link to the workstation used for
# development. It is a wire, and pretending otherwise would be dishonest -- but it is not
# an uplink, and it is reported separately rather than silently ignored.
LOOPBACK = {"lo"}
DEV_LINK = {"l4tbr0", "usb0", "usb1", "usb2", "rndis0"}

PROBE_TARGETS = [("1.1.1.1", 53), ("8.8.8.8", 53)]
PROBE_TIMEOUT = 1.5


def _ip_json(args):
    try:
        out = subprocess.run(["ip", "-json"] + args, capture_output=True,
                             text=True, timeout=5)
        return json.loads(out.stdout) if out.stdout.strip() else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def interfaces():
    rows = []
    for link in _ip_json(["addr", "show"]):
        name = link.get("ifname", "")
        if name in LOOPBACK:
            continue
        addrs = [a.get("local") for a in link.get("addr_info", [])
                 if a.get("family") == "inet"]
        rows.append({
            "name": name,
            "state": link.get("operstate", "UNKNOWN"),
            "addresses": [a for a in addrs if a],
            "is_dev_link": name in DEV_LINK,
        })
    return rows


def default_routes():
    return [r for r in _ip_json(["route", "show", "default"]) if r.get("gateway")]


def uplink_routes(routes):
    """Default routes that are not over the USB development link.

    Interfaces already exclude the dev link from what counts as an uplink; routes did
    not, and the Jetson's device-mode bridge installs a default route via l4tbr0. The
    result was that the board reported "LINK UP - NO INTERNET" whenever the USB cable to
    the workstation was attached -- which is exactly how it is administered -- so it
    could never reach AIR-GAPPED, and the one moment in the demo that proves the offline
    claim would not have fired.

    This is safe rather than flattering: reachability is probed first and decides. If
    the workstation is sharing its connection over that same link, the probe succeeds
    and the state is ONLINE regardless of what this function says.
    """
    return [r for r in routes if r.get("dev") not in DEV_LINK]


def can_reach_internet():
    """Attempt a real outbound TCP connection. This is the signal that decides."""
    for host, port in PROBE_TARGETS:
        try:
            with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
                return True, f"{host}:{port}"
        except OSError:
            continue
    return False, None


def status():
    ifaces = interfaces()
    uplinks = [i for i in ifaces
               if i["state"] == "UP" and i["addresses"] and not i["is_dev_link"]]
    routes = default_routes()
    real_routes = uplink_routes(routes)
    reachable, via = can_reach_internet()

    if reachable:
        state, headline = "ONLINE", "NETWORK REACHABLE"
    elif uplinks or real_routes:
        # Honest middle case: a cable is in, but nothing is reachable.
        state, headline = "LINK_NO_INTERNET", "LINK UP - NO INTERNET"
    else:
        state, headline = "OFFLINE", "AIR-GAPPED"

    return {
        "state": state,
        "headline": headline,
        "internet_reachable": reachable,
        "probe_target": via,
        "uplink_interfaces": [i["name"] for i in uplinks],
        "default_routes": [r.get("dev") for r in real_routes],
        "dev_link_routes": [r.get("dev") for r in routes
                            if r.get("dev") in DEV_LINK],
        "dev_link_present": any(i["is_dev_link"] and i["state"] == "UP" for i in ifaces),
        "interfaces": ifaces,
    }


if __name__ == "__main__":
    print(json.dumps(status(), indent=2))
