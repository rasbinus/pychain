"""A P2P blockchain node over HTTP (stage 4).

Run several on different ports:
    python node.py --port 5000
    python node.py --port 5001 --peers http://127.0.0.1:5000
"""
import argparse
import threading
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_file

from blockchain import DIFFICULTY, Blockchain
from transactions import txid
from wallet import Wallet

TIMEOUT = 3
EXPLORER = Path(__file__).with_name("explorer.html")


def create_app(difficulty=DIFFICULTY, miner=None, peers=(), self_url=None):
    app = Flask(__name__)
    bc = Blockchain(difficulty)
    miner = miner or Wallet()
    peers = set(peers)
    lock = threading.Lock()
    app.blockchain, app.peers, app.miner = bc, peers, miner  # handy for tests

    def broadcast(path, payload):
        for peer in list(peers):
            try:
                requests.post(f"{peer}{path}", json=payload, timeout=TIMEOUT)
            except requests.RequestException:
                pass  # peer offline; it can catch up later via /nodes/resolve

    def resolve():
        """Ask every peer for its chain and adopt the longest valid one."""
        replaced = False
        for peer in list(peers):
            try:
                chain = requests.get(f"{peer}/chain", timeout=TIMEOUT).json()["chain"]
            except (requests.RequestException, ValueError, KeyError):
                continue
            with lock:
                replaced = bc.replace_chain(chain) or replaced
        return replaced

    @app.get("/")
    def explorer():
        return send_file(EXPLORER)

    @app.get("/status")
    def status():
        with lock:
            return jsonify(height=len(bc.chain) - 1, next_difficulty=bc.difficulty,
                           total_work=bc.total_work(), pending=len(bc.pending),
                           peers=sorted(peers), miner=miner.address,
                           target_block_time=bc.target_block_time,
                           adjust_interval=bc.adjust_interval)

    @app.get("/chain")
    def get_chain():
        with lock:
            return jsonify(chain=bc.to_list(), length=len(bc.chain),
                           next_difficulty=bc.difficulty, total_work=bc.total_work())

    @app.get("/pending")
    def get_pending():
        with lock:
            return jsonify(pending=bc.pending, txids=[txid(t) for t in bc.pending])

    @app.get("/balance/<address>")
    def get_balance(address):
        with lock:
            return jsonify(address=address, balance=bc.balance(address),
                           available=bc.available(address))

    @app.get("/utxos/<address>")
    def get_utxos(address):
        """Coins an address can spend; pass these to Wallet.create_transaction."""
        with lock:
            return jsonify(utxos=bc.utxos_for(address))

    @app.get("/proof/<tid>")
    def get_proof(tid):
        """Merkle proof that a transaction is confirmed, plus the block header to check it against."""
        with lock:
            for block in bc.chain:
                tx, proof = block.proof_for(tid)
                if tx:
                    return jsonify(transaction=tx, proof=proof, header=block.header(),
                                   block_hash=block.hash())
        return jsonify(error="transaction not found in chain"), 404

    @app.get("/miner")
    def get_miner():
        return jsonify(address=miner.address)

    @app.post("/transactions")
    def new_transaction():
        tx = request.get_json(silent=True) or {}
        with lock:
            try:
                tid = bc.add_transaction(tx)
            except ValueError as e:
                return jsonify(error=str(e)), 400
        broadcast("/transactions", tx)  # peers reject it as a double spend, so no loop
        return jsonify(message="transaction added", txid=tid), 201

    @app.post("/mine")
    def mine():
        """Mine a block. Optional JSON {"address": ...} sends the reward somewhere else."""
        to = (request.get_json(silent=True) or {}).get("address") or miner.address
        if not isinstance(to, str):
            return jsonify(error="address must be a string"), 400
        with lock:
            block = bc.mine(to).to_dict()
        broadcast("/blocks", block)
        return jsonify(block=block), 201

    @app.post("/blocks")
    def receive_block():
        block = request.get_json(silent=True) or {}
        with lock:
            accepted = bc.add_block(block)
        if accepted:
            broadcast("/blocks", block)  # relay; peers that already have it reject it
        else:
            accepted = resolve()  # maybe we're behind or on a fork
        return jsonify(accepted=accepted), 200 if accepted else 409

    @app.post("/nodes/register")
    def register():
        urls = (request.get_json(silent=True) or {}).get("nodes", [])
        new = {u.rstrip("/") for u in urls} - peers - {self_url}
        peers.update(new)
        if self_url:  # ask new peers to know us back
            for u in new:
                try:
                    requests.post(f"{u}/nodes/register", json={"nodes": [self_url]}, timeout=TIMEOUT)
                except requests.RequestException:
                    pass
        return jsonify(peers=sorted(peers)), 201

    @app.get("/nodes/resolve")
    def consensus():
        replaced = resolve()
        with lock:
            return jsonify(replaced=replaced, length=len(bc.chain))

    return app


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--peers", nargs="*", default=[])
    p.add_argument("--difficulty", type=int, default=DIFFICULTY)
    args = p.parse_args()

    me = f"http://127.0.0.1:{args.port}"
    app = create_app(args.difficulty, self_url=me)
    print(f"Node {me}  miner address {app.miner.address[:16]}...")
    if args.peers:  # introduce ourselves, then sync
        with app.test_client() as c:
            c.post("/nodes/register", json={"nodes": args.peers})
            c.get("/nodes/resolve")
    app.run(port=args.port, threaded=True)
