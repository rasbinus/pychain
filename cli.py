"""Command-line wallet for pychain.

    python cli.py new alice              create a wallet (key saved under ~/.pychain/wallets)
    python cli.py list                   your wallets and their addresses
    python cli.py address alice          print a wallet's address
    python cli.py balance alice          confirmed and available balance
    python cli.py coins alice            the unspent outputs it owns
    python cli.py mine alice             mine a block, reward goes to alice
    python cli.py send alice bob 20      pay 20 to a wallet name or a raw address
    python cli.py send alice bob 20 --fee 1

Options: --node URL (default http://127.0.0.1:5000), --dir PATH (where key files live).
Your private keys never leave this machine: the node only sees signed transactions.
"""
import argparse
import os
import sys
from pathlib import Path

import requests

from wallet import Wallet

DEFAULT_NODE = os.environ.get("PYCHAIN_NODE", "http://127.0.0.1:5000")
DEFAULT_DIR = Path(os.environ.get("PYCHAIN_WALLETS", Path.home() / ".pychain" / "wallets"))
TIMEOUT = 60  # mining can take a while at high difficulty


class CliError(Exception):
    pass


def key_path(directory, name):
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise CliError(f"bad wallet name {name!r}: use letters, numbers, - and _")
    return Path(directory) / f"{name}.pem"


def load(directory, name):
    path = key_path(directory, name)
    if not path.exists():
        raise CliError(f"no wallet named {name!r} (create it with: python cli.py new {name})")
    return Wallet.load(path)


def resolve_recipient(directory, who):
    """A recipient can be one of your wallet names or a raw 128-hex-char address."""
    if key_path_exists(directory, who):
        return load(directory, who).address
    try:
        if len(who) == 128 and bytes.fromhex(who):
            return who
    except ValueError:
        pass
    raise CliError(f"{who!r} is not one of your wallets or a valid address")


def key_path_exists(directory, name):
    try:
        return key_path(directory, name).exists()
    except CliError:
        return False


def call(method, node, path, **kwargs):
    try:
        r = requests.request(method, f"{node}{path}", timeout=TIMEOUT, **kwargs)
    except requests.RequestException:
        raise CliError(f"can't reach node at {node} (is it running?)")
    try:
        body = r.json()
    except ValueError:
        raise CliError(f"node returned HTTP {r.status_code} with no JSON")
    if r.status_code >= 400:
        raise CliError(f"node rejected it: {body.get('error', r.status_code)}")
    return body


# --- commands ---

def cmd_new(args):
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = key_path(directory, args.name)
    try:
        w = Wallet()
        w.save(path)
    except FileExistsError:
        raise CliError(f"wallet {args.name!r} already exists at {path}")
    print(f"created {args.name}\naddress: {w.address}\nkey:     {path}")
    print("keep the key file safe: anyone with it can spend this wallet's coins")


def cmd_list(args):
    paths = sorted(Path(args.dir).glob("*.pem")) if Path(args.dir).exists() else []
    if not paths:
        print("no wallets yet (create one with: python cli.py new <name>)")
    for p in paths:
        print(f"{p.stem:<16} {Wallet.load(p).address[:24]}...")


def cmd_address(args):
    print(load(args.dir, args.name).address)


def cmd_balance(args):
    w = load(args.dir, args.name)
    b = call("GET", args.node, f"/balance/{w.address}")
    print(f"{args.name}: {b['balance']} confirmed, {b['available']} available (incl. pending)")


def cmd_coins(args):
    w = load(args.dir, args.name)
    coins = call("GET", args.node, f"/utxos/{w.address}")["utxos"]
    if not coins:
        print(f"{args.name} has no coins")
    for c in coins:
        print(f"{c['amount']:>8}  {c['txid'][:16]}...:{c['index']}")


def cmd_mine(args):
    w = load(args.dir, args.name)
    print("mining...", flush=True)
    block = call("POST", args.node, "/mine", json={"address": w.address})["block"]
    reward = block["transactions"][0]["outputs"][0]["amount"]
    print(f"mined block #{block['index']}, {reward} coins to {args.name}")


def cmd_send(args):
    if args.amount <= 0 or args.fee < 0:
        raise CliError("amount must be positive and fee can't be negative")
    sender = load(args.dir, args.sender)
    to = resolve_recipient(args.dir, args.recipient)
    coins = call("GET", args.node, f"/utxos/{sender.address}")["utxos"]
    try:
        tx = sender.create_transaction(coins, to, args.amount, args.fee)  # signed locally
    except ValueError as e:
        raise CliError(str(e))
    result = call("POST", args.node, "/transactions", json=tx)
    change = sum(o["amount"] for o in tx["outputs"] if o["address"] == sender.address)
    print(f"sent {args.amount} to {args.recipient} (fee {args.fee}, change {change})")
    print(f"txid: {result['txid']}")
    print("it's pending until someone mines a block")


def build_parser():
    p = argparse.ArgumentParser(prog="cli.py", description="pychain wallet",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--node", default=DEFAULT_NODE, help=f"node URL (default {DEFAULT_NODE})")
    p.add_argument("--dir", default=str(DEFAULT_DIR), help="folder holding wallet key files")
    sub = p.add_subparsers(dest="command", required=True)

    for name, fn, helptext in [("new", cmd_new, "create a wallet"),
                               ("address", cmd_address, "show a wallet's address"),
                               ("balance", cmd_balance, "show a wallet's balance"),
                               ("coins", cmd_coins, "list a wallet's unspent outputs"),
                               ("mine", cmd_mine, "mine a block, reward to this wallet")]:
        s = sub.add_parser(name, help=helptext)
        s.add_argument("name")
        s.set_defaults(func=fn)

    sub.add_parser("list", help="list your wallets").set_defaults(func=cmd_list)

    s = sub.add_parser("send", help="send coins")
    s.add_argument("sender", help="your wallet name")
    s.add_argument("recipient", help="wallet name or address")
    s.add_argument("amount", type=int)
    s.add_argument("--fee", type=int, default=0)
    s.set_defaults(func=cmd_send)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
