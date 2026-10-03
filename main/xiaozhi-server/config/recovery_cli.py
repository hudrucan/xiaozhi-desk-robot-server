"""Safe terminal interaction shared by provisioning and fresh-node restore."""

import argparse
import getpass
import warnings

from config.cloud_recovery import RecoveryError
from config.node_identity import hostname_node_id


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise RecoveryError()


def recovery_passphrase(*, confirm=False, prompt=None):
    prompt = prompt or getpass.getpass
    try:
        with warnings.catch_warnings():
            # getpass's non-TTY fallback echoes input; abort before that read.
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = prompt("Recovery passphrase: ")
            if not value or (confirm and value != prompt("Confirm recovery passphrase: ")):
                raise RecoveryError()
        return value
    except Exception:
        raise RecoveryError() from None


def choose(items, title, *, input_fn=input):
    if not items:
        raise RecoveryError()
    if len(items) == 1:
        return items[0]
    print(title)
    for index, label in enumerate(items, 1):
        print(f"{index}. {label}")
    try:
        selection = int(input_fn("Select number: "))
        if not 1 <= selection <= len(items):
            raise RecoveryError()
        return items[selection - 1]
    except Exception:
        raise RecoveryError() from None


def choose_node(nodes, *, input_fn=input):
    if not nodes:
        raise RecoveryError()
    if len(nodes) == 1:
        return next(iter(nodes))
    hostname = hostname_node_id()
    print("Available nodes:")
    for node in sorted(nodes):
        print(f"  {node}")
    prompt = f"Node ID [{hostname}]: " if hostname in nodes else f"Node ID (hostname: {hostname}): "
    try:
        node = input_fn(prompt).strip() or hostname
        if node not in nodes:
            raise RecoveryError()
        return node
    except Exception:
        raise RecoveryError() from None
