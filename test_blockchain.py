import pytest

from blockchain import BLOCK_REWARD, Block, Blockchain
from merkle import merkle_root
from transactions import coinbase, txid
from wallet import Wallet

ALICE, BOB, CAROL = Wallet(), Wallet(), Wallet()


def pay(bc, sender, recipient, amount, fee=0):
    tx = sender.create_transaction(bc.utxos_for(sender.address), recipient.address, amount, fee)
    bc.add_transaction(tx)
    return tx


def make_chain():
    bc = Blockchain(difficulty=8)  # low difficulty keeps tests fast
    bc.mine(ALICE.address)
    pay(bc, ALICE, BOB, 20)
    bc.mine(ALICE.address)
    return bc


def test_new_chain_has_genesis_block():
    bc = Blockchain()
    assert len(bc.chain) == 1
    assert bc.chain[0].prev_hash == "0" * 64
    assert bc.utxos == {}


def test_mined_blocks_link_and_meet_difficulty():
    bc = make_chain()
    assert len(bc.chain) == 3
    for prev, curr in zip(bc.chain, bc.chain[1:]):
        assert curr.prev_hash == prev.hash()
        assert curr.hash().startswith("00")
    assert bc.is_valid()


def test_mining_clears_pending():
    assert make_chain().pending == []


def test_balances():
    bc = make_chain()
    assert bc.balance(ALICE.address) == 80  # 2 rewards of 50, minus 20 sent
    assert bc.balance(BOB.address) == 20


def test_tampered_transaction_invalidates_chain():
    bc = make_chain()
    bc.chain[1].transactions[0]["outputs"][0]["amount"] = 1_000_000
    assert not bc.is_valid()


def test_rehashing_tampered_block_still_breaks_next_link():
    bc = make_chain()
    tampered = bc.chain[1]
    tampered.transactions[0]["outputs"][0]["amount"] = 1_000_000
    tampered.merkle_root = merkle_root(tampered.transactions)  # attacker fixes up the header...
    while not tampered.hash().startswith("00"):  # ...and re-mines block 1
        tampered.nonce += 1
    assert not bc.is_valid()  # block 2 still points at the old hash


# --- UTXO mechanics ---

def test_payment_creates_change_output():
    bc = Blockchain(difficulty=8)
    bc.mine(ALICE.address)
    tx = pay(bc, ALICE, BOB, 20)
    assert len(tx["inputs"]) == 1  # the 50-coin reward
    assert tx["outputs"] == [{"address": BOB.address, "amount": 20},
                             {"address": ALICE.address, "amount": 30}]  # change


def test_exact_amount_has_no_change():
    bc = Blockchain(difficulty=8)
    bc.mine(ALICE.address)
    assert len(pay(bc, ALICE, BOB, 50)["outputs"]) == 1


def test_combines_multiple_coins():
    bc = Blockchain(difficulty=8)
    for _ in range(3):
        bc.mine(ALICE.address)  # three 50-coin outputs
    tx = pay(bc, ALICE, BOB, 120)
    assert len(tx["inputs"]) == 3
    bc.mine(CAROL.address)
    assert bc.balance(BOB.address) == 120
    assert bc.balance(ALICE.address) == 30


def test_fee_goes_to_miner():
    bc = Blockchain(difficulty=8)
    bc.mine(ALICE.address)
    pay(bc, ALICE, BOB, 20, fee=3)
    block = bc.mine(CAROL.address)
    assert block.transactions[0]["outputs"][0]["amount"] == BLOCK_REWARD + 3
    assert bc.balance(ALICE.address) == 27
    assert bc.balance(CAROL.address) == 53


def test_spent_output_leaves_utxo_set():
    bc = Blockchain(difficulty=8)
    bc.mine(ALICE.address)
    coin = bc.utxos_for(ALICE.address)[0]
    pay(bc, ALICE, BOB, 10)
    bc.mine(CAROL.address)
    assert (coin["txid"], coin["index"]) not in bc.utxos


def test_can_spend_unconfirmed_change():
    bc = Blockchain(difficulty=8)
    bc.mine(ALICE.address)
    pay(bc, ALICE, BOB, 10)
    pay(bc, ALICE, CAROL, 15)  # spends the pending change output
    bc.mine(ALICE.address)
    assert bc.balance(BOB.address) == 10
    assert bc.balance(CAROL.address) == 15
    assert bc.is_valid()


def test_coinbase_txids_are_unique_per_height():
    assert txid(coinbase(ALICE.address, 1)) != txid(coinbase(ALICE.address, 2))


# --- rejections ---

def test_rejects_double_spend_in_mempool():
    bc = Blockchain(difficulty=8)
    bc.mine(ALICE.address)
    coins = bc.utxos_for(ALICE.address)
    bc.add_transaction(ALICE.create_transaction(coins, BOB.address, 10))
    with pytest.raises(ValueError, match="unspent"):
        bc.add_transaction(ALICE.create_transaction(coins, CAROL.address, 10))


def test_rejects_spending_already_mined_coin():
    bc = Blockchain(difficulty=8)
    bc.mine(ALICE.address)
    coins = bc.utxos_for(ALICE.address)
    bc.add_transaction(ALICE.create_transaction(coins, BOB.address, 10))
    bc.mine(ALICE.address)
    with pytest.raises(ValueError, match="unspent"):
        bc.add_transaction(ALICE.create_transaction(coins, CAROL.address, 10))


def test_rejects_replayed_transaction():
    bc = make_chain()
    tx = pay(bc, ALICE, BOB, 5)
    bc.mine(ALICE.address)
    with pytest.raises(ValueError, match="unspent"):
        bc.add_transaction(tx)


def test_rejects_same_input_twice_in_one_tx():
    bc = Blockchain(difficulty=8)
    bc.mine(ALICE.address)
    coin = bc.utxos_for(ALICE.address)[0]
    tx = ALICE.sign_inputs({"inputs": [dict(coin), dict(coin)],
                            "outputs": [{"address": BOB.address, "amount": 100}]})
    with pytest.raises(ValueError, match="double spend"):
        bc.add_transaction(tx)


def test_rejects_spending_someone_elses_coin():
    bc = make_chain()
    alice_coin = bc.utxos_for(ALICE.address)[0]
    forged = BOB.sign_inputs({"inputs": [{"txid": alice_coin["txid"], "index": alice_coin["index"]}],
                              "outputs": [{"address": BOB.address, "amount": 1}]})
    with pytest.raises(ValueError, match="signature"):
        bc.add_transaction(forged)


def test_rejects_output_changed_after_signing():
    bc = make_chain()
    tx = ALICE.create_transaction(bc.utxos_for(ALICE.address), BOB.address, 1)
    tx["outputs"][0]["amount"] = 70  # redirect more money; signatures no longer match
    with pytest.raises(ValueError, match="signature"):
        bc.add_transaction(tx)


def test_rejects_outputs_exceeding_inputs():
    bc = Blockchain(difficulty=8)
    bc.mine(ALICE.address)
    coin = bc.utxos_for(ALICE.address)[0]
    tx = ALICE.sign_inputs({"inputs": [{"txid": coin["txid"], "index": coin["index"]}],
                            "outputs": [{"address": BOB.address, "amount": 51}]})
    with pytest.raises(ValueError, match="insufficient"):
        bc.add_transaction(tx)


def test_wallet_refuses_to_build_unaffordable_tx():
    bc = make_chain()
    with pytest.raises(ValueError, match="insufficient"):
        BOB.create_transaction(bc.utxos_for(BOB.address), ALICE.address, 21)


@pytest.mark.parametrize("amount", [0, -5, 1.5, True, "10"])
def test_rejects_bad_amounts(amount):
    bc = make_chain()
    coin = bc.utxos_for(ALICE.address)[0]
    tx = ALICE.sign_inputs({"inputs": [{"txid": coin["txid"], "index": coin["index"]}],
                            "outputs": [{"address": BOB.address, "amount": amount}]})
    with pytest.raises(ValueError):
        bc.add_transaction(tx)


@pytest.mark.parametrize("tx", [
    {}, {"inputs": [], "outputs": []}, {"inputs": "x", "outputs": []},
    {"inputs": [{"txid": "ab"}], "outputs": [{"address": "x", "amount": 1}]},
    coinbase("anyone", 5),  # coinbases only come from miners, never the mempool
])
def test_rejects_malformed_transactions(tx):
    with pytest.raises(ValueError):
        make_chain().add_transaction(tx)


# --- block-level rules ---

def mined(bc, txs):
    block = Block(len(bc.chain), txs, bc.last.hash(), timestamp=bc.clock(), difficulty=bc.difficulty)
    while not block.meets_difficulty():
        block.nonce += 1
    return block.to_dict()


def test_control_honest_hand_built_block_accepted():
    bc = make_chain()
    assert bc.add_block(mined(bc, [coinbase(BOB.address, len(bc.chain))]))


def test_rejects_block_with_inflated_coinbase():
    bc = make_chain()
    cb = coinbase(BOB.address, len(bc.chain))
    cb["outputs"][0]["amount"] = 1_000
    assert not bc.add_block(mined(bc, [cb]))


def test_rejects_block_with_two_coinbases():
    bc = make_chain()
    h = len(bc.chain)
    assert not bc.add_block(mined(bc, [coinbase(BOB.address, h), coinbase(BOB.address, h)]))


def test_rejects_block_without_leading_coinbase():
    bc = make_chain()
    tx = ALICE.create_transaction(bc.utxos_for(ALICE.address), BOB.address, 5)
    assert not bc.add_block(mined(bc, [tx]))


def test_rejects_block_with_wrong_coinbase_height():
    bc = make_chain()
    assert not bc.add_block(mined(bc, [coinbase(BOB.address, 1)]))


def test_rejects_block_containing_double_spend():
    bc = Blockchain(difficulty=8)
    bc.mine(ALICE.address)
    coins = bc.utxos_for(ALICE.address)
    a = ALICE.create_transaction(coins, BOB.address, 10)
    b = ALICE.create_transaction(coins, CAROL.address, 10)
    assert not bc.add_block(mined(bc, [coinbase(BOB.address, 2), a, b]))


def test_accepts_valid_peer_block_and_updates_utxos():
    bc = Blockchain(difficulty=8)
    bc.mine(ALICE.address)
    tx = ALICE.create_transaction(bc.utxos_for(ALICE.address), BOB.address, 10, fee=2)
    assert bc.add_block(mined(bc, [coinbase(CAROL.address, 2, fees=2), tx]))
    assert bc.balance(BOB.address) == 10
    assert bc.balance(CAROL.address) == 52


def test_pending_dropped_once_mined_by_a_peer():
    bc = Blockchain(difficulty=8)
    bc.mine(ALICE.address)
    tx = pay(bc, ALICE, BOB, 10)
    assert bc.add_block(mined(bc, [coinbase(CAROL.address, 2), tx]))
    assert bc.pending == []


def test_utxo_set_matches_full_replay():
    bc = make_chain()
    pay(bc, ALICE, CAROL, 33, fee=1)
    pay(bc, BOB, CAROL, 5)
    bc.mine(BOB.address)
    assert bc.utxos == bc._replay(bc.chain)
