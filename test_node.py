"""Stage 4: consensus and a real multi-node network on localhost."""
import threading

import pytest
import requests
from werkzeug.serving import make_server

from blockchain import Block, Blockchain
from merkle import verify_proof
from node import create_app
from transactions import coinbase
from wallet import Wallet


def utxos(url, wallet):
    return requests.get(f"{url}/utxos/{wallet.address}").json()["utxos"]

ALICE, BOB = Wallet(), Wallet()


# --- consensus at the Blockchain level ---

def test_replace_chain_adopts_longer_valid_chain():
    a, b = Blockchain(difficulty=8), Blockchain(difficulty=8)
    a.mine(ALICE.address)
    for _ in range(3):
        b.mine(BOB.address)
    assert a.replace_chain(b.to_list())
    assert len(a.chain) == 4
    assert a.balance(BOB.address) == 150


def test_replace_chain_ignores_shorter_chain():
    a, b = Blockchain(difficulty=8), Blockchain(difficulty=8)
    a.mine(ALICE.address)
    a.mine(ALICE.address)
    b.mine(BOB.address)
    assert not a.replace_chain(b.to_list())


def test_replace_chain_rejects_longer_chain_with_overspend():
    honest, attacker = Blockchain(difficulty=8), Blockchain(difficulty=8)
    honest.mine(ALICE.address)
    attacker.mine(BOB.address)
    attacker.mine(BOB.address)
    # hand-build a block with a validly signed tx that spends more than its inputs
    coin = attacker.utxos_for(BOB.address)[0]
    cheat = BOB.sign_inputs({"inputs": [{"txid": coin["txid"], "index": coin["index"]}],
                             "outputs": [{"address": ALICE.address, "amount": 500}]})
    block = Block(3, [coinbase(BOB.address, 3), cheat], attacker.last.hash(),
                  timestamp=attacker.clock(), difficulty=attacker.difficulty)
    while not block.meets_difficulty():
        block.nonce += 1
    attacker.chain.append(block)
    assert not honest.replace_chain(attacker.to_list())


def test_replace_chain_rejects_garbage():
    assert not Blockchain(difficulty=8).replace_chain([{"nope": 1}, {"nope": 2}])


def test_add_block_rejects_block_not_on_tip():
    a, b = Blockchain(difficulty=8), Blockchain(difficulty=8)
    a.mine(ALICE.address)
    b.mine(BOB.address)
    stale = b.mine(BOB.address).to_dict()  # index 2, but a's tip is a different block 1
    assert not a.add_block(stale)


# --- real HTTP nodes ---

@pytest.fixture
def network():
    servers, urls = [], []
    for _ in range(3):
        app = create_app(difficulty=8)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        url = f"http://127.0.0.1:{server.server_port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        urls.append(url)
    for i, url in enumerate(urls):
        others = [u for j, u in enumerate(urls) if j != i]
        requests.post(f"{url}/nodes/register", json={"nodes": others})
    yield urls
    for s in servers:
        s.shutdown()


def chain_len(url):
    return requests.get(f"{url}/chain").json()["length"]


def test_mined_block_propagates_to_all_nodes(network):
    n0, n1, n2 = network
    requests.post(f"{n0}/mine")
    assert [chain_len(u) for u in network] == [2, 2, 2]


def test_all_nodes_agree_on_rewards(network):
    n0, n1, n2 = network
    miner = requests.get(f"{n0}/miner").json()["address"]
    requests.post(f"{n0}/mine")
    requests.post(f"{n1}/mine")
    assert requests.get(f"{n2}/balance/{miner}").json()["balance"] == 50


def test_spending_nonexistent_coin_rejected(network):
    fake = ALICE.create_transaction([{"txid": "ab" * 32, "index": 0, "amount": 5}], BOB.address, 1)
    r = requests.post(f"{network[0]}/transactions", json=fake)
    assert r.status_code == 400 and "unspent" in r.json()["error"]


def test_malformed_transaction_rejected(network):
    r = requests.post(f"{network[0]}/transactions", data="not json")
    assert r.status_code == 400


def test_signed_payment_end_to_end(network):
    n0, n1, n2 = network
    # a fourth node whose miner is ALICE, so she can earn coins to spend
    app = create_app(difficulty=8, miner=ALICE)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    alice_node = f"http://127.0.0.1:{server.server_port}"
    try:
        requests.post(f"{alice_node}/nodes/register", json={"nodes": [n0]})
        requests.get(f"{alice_node}/nodes/resolve")
        requests.post(f"{alice_node}/mine")  # ALICE earns 50, block reaches n0

        # a wallet asks any node for its coins, builds the tx locally, and submits it
        tx = ALICE.create_transaction(utxos(n0, ALICE), BOB.address, 30, fee=1)
        r = requests.post(f"{n0}/transactions", json=tx)
        assert r.status_code == 201
        tid = r.json()["txid"]
        assert requests.get(f"{n2}/pending").json()["txids"] == [tid]  # gossiped

        # the same coin can't be spent again on another node
        spent = [{"txid": i["txid"], "index": i["index"], "amount": 50} for i in tx["inputs"]]
        again = ALICE.create_transaction(spent, BOB.address, 5)
        assert requests.post(f"{n1}/transactions", json=again).status_code == 400

        requests.post(f"{n2}/mine")
        for url in network:
            assert requests.get(f"{url}/balance/{BOB.address}").json()["balance"] == 30
            assert requests.get(f"{url}/balance/{ALICE.address}").json()["balance"] == 19

        # Bob checks his payment with a merkle proof instead of the full block
        p = requests.get(f"{n1}/proof/{tid}").json()
        assert verify_proof(p["transaction"], p["proof"], p["header"]["merkle_root"])
        assert requests.get(f"{n1}/proof/deadbeef").status_code == 404
    finally:
        server.shutdown()


def test_lagging_node_catches_up_with_resolve(network):
    n0, n1, n2 = network
    lone = create_app(difficulty=8)
    with lone.test_client() as c:
        lone.peers.add(n0)
        for _ in range(3):
            requests.post(f"{n0}/mine")
        assert c.get("/chain").get_json()["length"] == 1
        assert c.get("/nodes/resolve").get_json() == {"replaced": True, "length": 4}


def test_rejects_forged_transaction_over_http(network):
    n0 = network[0]
    miner = requests.get(f"{n0}/miner").json()["address"]
    requests.post(f"{n0}/mine")
    coin = requests.get(f"{n0}/utxos/{miner}").json()["utxos"][0]
    forged = BOB.sign_inputs({"inputs": [{"txid": coin["txid"], "index": coin["index"]}],
                              "outputs": [{"address": BOB.address, "amount": 50}]})
    r = requests.post(f"{n0}/transactions", json=forged)
    assert r.status_code == 400 and "signature" in r.json()["error"]
