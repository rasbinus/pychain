# pychain

A cryptocurrency built from scratch for learning.

## Run

```bash
pip install -r requirements.txt
python blockchain.py
pytest
```

## Run a network

Start nodes in separate terminals:

```bash
python node.py --port 5000
python node.py --port 5001 --peers http://127.0.0.1:5000
python node.py --port 5002 --peers http://127.0.0.1:5000
```

| Endpoint | What it does |
| --- | --- |
| `GET /` | Block explorer web page |
| `GET /status` | Height, difficulty, total work, peers |
| `GET /chain` | Full chain |
| `GET /pending` | Transactions waiting to be mined |
| `GET /balance/<address>` | Confirmed and available balance |
| `GET /utxos/<address>` | Unspent coins an address can spend |
| `GET /proof/<txid>` | Merkle proof that a transaction is in a block |
| `GET /miner` | This node's reward address |
| `POST /transactions` | Submit a signed transaction (gossiped to peers), returns its txid |
| `POST /mine` | Mine a block (broadcast to peers); optional `{"address": ...}` for the reward |
| `POST /blocks` | Receive a block from a peer |
| `POST /nodes/register` | `{"nodes": [urls]}` add peers |
| `GET /nodes/resolve` | Adopt the valid chain with the most work among peers |

## Wallet CLI

With a node running on port 5000:

```bash
python cli.py new alice              # key saved to ~/.pychain/wallets/alice.pem
python cli.py new bob
python cli.py mine alice             # alice earns 50
python cli.py send alice bob 20 --fee 1
python cli.py mine bob               # confirms the payment, bob keeps the fee
python cli.py balance bob
python cli.py coins alice
python cli.py list
```

`send` accepts a wallet name or a raw address. Use `--node URL` to talk to another
node and `--dir PATH` to keep keys elsewhere (or set `PYCHAIN_NODE` / `PYCHAIN_WALLETS`).
Keys never leave your machine; the node only ever sees signed transactions.

## Sending a payment from Python

```python
import requests
from wallet import Wallet

node = "http://127.0.0.1:5000"
alice, bob = Wallet(), Wallet()
requests.post(f"{node}/mine", json={"address": alice.address})       # alice earns 50
coins = requests.get(f"{node}/utxos/{alice.address}").json()["utxos"]
tx = alice.create_transaction(coins, bob.address, 20, fee=1)          # 20 to bob, 29 change
requests.post(f"{node}/transactions", json=tx)
```

## Roadmap

- [x] Stage 1: blocks, SHA-256 hashing, genesis block
- [x] Stage 2: proof of work mining, chain validation, balances
- [x] Stage 3: ECDSA wallets, signed transactions, overspend checks
- [x] Stage 4: P2P nodes (Flask), longest-chain consensus
- [x] Stage 5
  - [x] Merkle trees and inclusion proofs
  - [x] Difficulty adjustment (retarget every 10 blocks toward 10s/block) and most-work consensus
  - [x] Block explorer (open any node's URL in a browser)
  - [x] UTXO model with change outputs and transaction fees
