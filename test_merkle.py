import pytest

from blockchain import Block, Blockchain
from merkle import EMPTY_ROOT, leaf_hash, merkle_proof, merkle_root, verify_proof
from transactions import txid
from wallet import Wallet

ALICE, BOB = Wallet(), Wallet()
TXS = [{"from": f"a{i}", "to": f"b{i}", "amount": i + 1} for i in range(7)]


def test_empty_and_single():
    assert merkle_root([]) == EMPTY_ROOT
    assert merkle_root(TXS[:1]) == leaf_hash(TXS[0])


def test_root_changes_with_any_edit_or_reorder():
    root = merkle_root(TXS)
    edited = [dict(tx) for tx in TXS]
    edited[4]["amount"] = 999
    assert merkle_root(edited) != root
    assert merkle_root(TXS[::-1]) != root


def test_duplicating_last_tx_changes_root():
    # Bitcoin's duplicate-the-odd-leaf rule makes these collide (CVE-2012-2459); ours must not.
    three = TXS[:3]
    assert merkle_root(three) != merkle_root(three + [three[-1]])


@pytest.mark.parametrize("n", range(1, 10))  # covers odd and even sizes at every level
def test_every_proof_verifies(n):
    txs = [{"n": i} for i in range(n)]
    root = merkle_root(txs)
    for i, tx in enumerate(txs):
        assert verify_proof(tx, merkle_proof(txs, i), root)


def test_proof_is_logarithmic():
    txs = [{"n": i} for i in range(1000)]
    assert len(merkle_proof(txs, 500)) == 10  # not 1000


def test_proof_fails_for_wrong_tx_or_root():
    root = merkle_root(TXS)
    proof = merkle_proof(TXS, 2)
    assert not verify_proof(TXS[3], proof, root)
    assert not verify_proof(TXS[2], proof, merkle_root(TXS[:6]))


def test_proof_index_out_of_range():
    with pytest.raises(IndexError):
        merkle_proof(TXS, 7)


# --- integration with blocks ---

def funded_chain():
    bc = Blockchain(difficulty=8)
    bc.mine(ALICE.address)
    tx = ALICE.create_transaction(bc.utxos_for(ALICE.address), BOB.address, 10)
    bc.add_transaction(tx)
    bc.mine(ALICE.address)
    return bc, tx


def test_block_hash_commits_to_transactions_via_root():
    bc, _ = funded_chain()
    block = bc.chain[2]
    assert block.merkle_root == merkle_root(block.transactions)
    assert "transactions" not in block.header()


def test_swapping_transactions_without_touching_header_is_caught():
    bc, _ = funded_chain()
    # hash and proof of work stay intact, only the body changes
    bc.chain[2].transactions[0]["outputs"][0]["address"] = BOB.address  # steal the reward
    assert bc.chain[2].hash().startswith("00")
    assert not bc.is_valid()


def test_block_with_lying_merkle_root_rejected():
    bc, _ = funded_chain()
    d = bc.chain[2].to_dict()
    d["merkle_root"] = "ab" * 32
    assert not bc.is_valid(bc.chain[:2] + [Block.from_dict(d)])


def test_light_client_verifies_payment_from_proof():
    bc, tx = funded_chain()
    block = bc.chain[2]
    found, proof = block.proof_for(txid(tx))
    assert found == tx
    # a light client only needs the header, not the whole block
    assert verify_proof(tx, proof, block.header()["merkle_root"])
