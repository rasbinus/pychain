"""Merkle trees (stage 5): commit to all of a block's transactions with one hash.

Leaves and inner nodes get different prefixes (as in RFC 6962) so a leaf can never
be passed off as an inner node. An odd node out is promoted to the next level as-is,
rather than duplicated like Bitcoin does, which avoids Bitcoin's CVE-2012-2459
where two different transaction lists share a root.
"""
import hashlib
import json

EMPTY_ROOT = hashlib.sha256(b"").hexdigest()


def leaf_hash(tx):
    return hashlib.sha256(b"\x00" + json.dumps(tx, sort_keys=True).encode()).hexdigest()


def _node_hash(left, right):
    return hashlib.sha256(b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def _levels(transactions):
    """All tree levels, leaves first, root last."""
    level = [leaf_hash(tx) for tx in transactions]
    levels = [level]
    while len(level) > 1:
        nxt = [_node_hash(level[i], level[i + 1]) for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            nxt.append(level[-1])  # promote the odd one out
        level = nxt
        levels.append(level)
    return levels


def merkle_root(transactions):
    return _levels(transactions)[-1][0] if transactions else EMPTY_ROOT


def merkle_proof(transactions, index):
    """Sibling hashes needed to rebuild the root from one transaction.

    Each step is (sibling_hash, side) where side says where the sibling sits.
    """
    if not 0 <= index < len(transactions):
        raise IndexError("transaction index out of range")
    proof = []
    for level in _levels(transactions)[:-1]:
        sibling = index ^ 1
        if sibling < len(level):
            proof.append((level[sibling], "left" if sibling < index else "right"))
        index //= 2
    return proof


def verify_proof(tx, proof, root):
    h = leaf_hash(tx)
    for sibling, side in proof:
        h = _node_hash(sibling, h) if side == "left" else _node_hash(h, sibling)
    return h == root
