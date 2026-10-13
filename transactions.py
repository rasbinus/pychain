"""UTXO transactions (stage 5).

There are no account balances. Every transaction consumes earlier *unspent outputs*
(UTXOs) and creates new ones:

    {
      "inputs":  [{"txid": "...", "index": 0, "signature": "..."}],   # coins being spent
      "outputs": [{"address": "<bob>", "amount": 30},                  # payment
                  {"address": "<alice>", "amount": 19}]                # change back to sender
    }

Inputs minus outputs is the fee, which the miner collects. An address's balance is
just the sum of the unspent outputs it owns.

A coinbase transaction has no inputs. It is the first tx in each block and pays the
miner the block reward plus fees. It carries the block height so every coinbase has
a unique txid (Bitcoin's BIP 34).
"""
import hashlib
import json

from wallet import verify

BLOCK_REWARD = 50


def txid(tx):
    """Hash of the transaction *without* signatures.

    Signatures sign the txid, so they can't be inside it. Leaving them out also means
    nobody can change a txid by tweaking a signature (transaction malleability).
    """
    body = {
        "inputs": [{"txid": i["txid"], "index": i["index"]} for i in tx["inputs"]],
        "outputs": tx["outputs"],
    }
    if "height" in tx:
        body["height"] = tx["height"]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def coinbase(address, height, fees=0):
    return {"inputs": [], "outputs": [{"address": address, "amount": BLOCK_REWARD + fees}],
            "height": height}


def _is_amount(x):
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def check_shape(tx):
    """Structural checks that don't need the UTXO set."""
    if not isinstance(tx, dict):
        raise ValueError("transaction must be an object")
    inputs, outputs = tx.get("inputs"), tx.get("outputs")
    if not isinstance(inputs, list) or not isinstance(outputs, list) or not outputs:
        raise ValueError("transaction needs inputs and at least one output")
    for o in outputs:
        if not isinstance(o, dict) or not isinstance(o.get("address"), str):
            raise ValueError("bad output")
        if not _is_amount(o.get("amount")):
            raise ValueError("amount must be a positive whole number")
    for i in inputs:
        if not isinstance(i, dict) or not isinstance(i.get("txid"), str) \
                or not isinstance(i.get("index"), int) or not isinstance(i.get("signature"), str):
            raise ValueError("bad input")
    refs = [(i["txid"], i["index"]) for i in inputs]
    if len(set(refs)) != len(refs):
        raise ValueError("double spend within transaction")


def validate(tx, utxos):
    """Check a regular transaction against a UTXO set and return its fee.

    `utxos` maps (txid, index) -> output. Raises ValueError on anything wrong.
    """
    check_shape(tx)
    if not tx["inputs"]:
        raise ValueError("only a coinbase may have no inputs")
    message = bytes.fromhex(txid(tx))
    total_in = 0
    for i in tx["inputs"]:
        spent = utxos.get((i["txid"], i["index"]))
        if spent is None:
            raise ValueError("input is not an unspent output (double spend or unknown coin)")
        if not verify(spent["address"], i["signature"], message):
            raise ValueError("invalid signature")
        total_in += spent["amount"]
    fee = total_in - sum(o["amount"] for o in tx["outputs"])
    if fee < 0:
        raise ValueError("insufficient funds: outputs exceed inputs")
    return fee


def apply(tx, utxos):
    """Spend a transaction's inputs and add its outputs to `utxos` in place."""
    for i in tx["inputs"]:
        del utxos[(i["txid"], i["index"])]
    tid = txid(tx)
    for n, o in enumerate(tx["outputs"]):
        utxos[(tid, n)] = dict(o)  # copy, so editing a block afterwards can't alter the set
