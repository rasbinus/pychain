"""A minimal blockchain: proof of work, UTXO transactions, merkle roots, difficulty retargeting."""
import hashlib
import json
import math
import time

import transactions as tx_rules
from merkle import merkle_proof, merkle_root as compute_merkle_root
from transactions import BLOCK_REWARD  # noqa: F401  (re-exported for callers)
from wallet import Wallet

DIFFICULTY = 16          # starting difficulty, in leading zero bits (16 bits = "0000" hex)
MIN_DIFFICULTY = 1
TARGET_BLOCK_TIME = 10   # seconds
ADJUST_INTERVAL = 10     # retarget every this many blocks
MAX_STEP = 2             # max bits to change per retarget
MAX_FUTURE_DRIFT = 120   # seconds a block's timestamp may be ahead of our clock


class Block:
    def __init__(self, index, transactions, prev_hash, nonce=0, timestamp=None, merkle_root=None,
                 difficulty=0):
        self.index = index
        self.difficulty = difficulty  # required leading zero bits in the hash
        self.timestamp = time.time() if timestamp is None else timestamp
        self.transactions = transactions
        self.prev_hash = prev_hash
        self.nonce = nonce
        # A received block carries its claimed root; is_valid checks it against the txs.
        self.merkle_root = compute_merkle_root(transactions) if merkle_root is None else merkle_root

    def header(self):
        """What gets hashed and mined. Transactions are covered via the merkle root."""
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "merkle_root": self.merkle_root,
            "prev_hash": self.prev_hash,
            "nonce": self.nonce,
            "difficulty": self.difficulty,
        }

    def meets_difficulty(self):
        return int(self.hash(), 16) < 1 << (256 - self.difficulty)

    def work(self):
        """Expected number of hashes it took to mine this block."""
        return 1 << self.difficulty

    def to_dict(self):
        return {**self.header(), "transactions": self.transactions}

    @classmethod
    def from_dict(cls, d):
        return cls(d["index"], d["transactions"], d["prev_hash"], d["nonce"], d["timestamp"],
                   d["merkle_root"], d["difficulty"])

    def hash(self):
        data = json.dumps(self.header(), sort_keys=True).encode()
        return hashlib.sha256(data).hexdigest()

    def has_valid_merkle_root(self):
        return self.merkle_root == compute_merkle_root(self.transactions)

    def proof_for(self, tid):
        """Merkle proof that the transaction with this txid is in the block."""
        for i, tx in enumerate(self.transactions):
            if tx_rules.txid(tx) == tid:
                return tx, merkle_proof(self.transactions, i)
        return None, None


class Blockchain:
    def __init__(self, difficulty=DIFFICULTY, target_block_time=TARGET_BLOCK_TIME,
                 adjust_interval=ADJUST_INTERVAL, clock=time.time):
        self.target_block_time = target_block_time
        self.adjust_interval = adjust_interval
        self.clock = clock  # injectable so tests can fake fast or slow mining
        self.chain = [Block(0, [], "0" * 64, timestamp=0, difficulty=difficulty)]  # genesis
        self.pending = []
        self.utxos = {}  # (txid, output index) -> {"address", "amount"}

    @property
    def last(self):
        return self.chain[-1]

    @property
    def difficulty(self):
        """Difficulty the next block must meet."""
        return self.next_difficulty(self.chain)

    def next_difficulty(self, chain):
        """Retarget every `adjust_interval` blocks so blocks arrive every `target_block_time`.

        Every node derives this from the chain alone, so they all agree on it.
        """
        prev = chain[-1]
        height = len(chain)
        n = self.adjust_interval
        if height % n or height <= n:  # not a retarget height, or window would include genesis
            return prev.difficulty
        actual = max(chain[-1].timestamp - chain[-n].timestamp, 1e-9)
        expected = self.target_block_time * (n - 1)  # n blocks span n-1 gaps
        # each bit doubles the work, so shift by log2(expected/actual), at most 2 bits per retarget
        step = max(-MAX_STEP, min(MAX_STEP, round(math.log2(expected / actual))))
        return max(MIN_DIFFICULTY, prev.difficulty + step)

    def total_work(self, chain=None):
        return sum(b.work() for b in (self.chain if chain is None else chain)[1:])

    # --- transactions and the UTXO set ---

    def _mempool_view(self):
        """UTXO set as it will look once every pending tx is mined.

        Lets a pending tx spend another pending tx's outputs, and makes a second
        spend of the same coin fail as "not an unspent output".
        """
        view = dict(self.utxos)
        for tx in self.pending:
            tx_rules.apply(tx, view)
        return view

    def add_transaction(self, tx):
        """Accept a signed transaction into the mempool after checking it. Returns its txid."""
        tx_rules.validate(tx, self._mempool_view())
        self.pending.append(tx)
        return tx_rules.txid(tx)

    def mine(self, miner_address):
        """Bundle pending transactions into a new block and find a valid nonce."""
        view, fees = dict(self.utxos), 0
        for tx in self.pending:
            fees += tx_rules.validate(tx, view)
            tx_rules.apply(tx, view)
        txs = [tx_rules.coinbase(miner_address, len(self.chain), fees)] + self.pending
        timestamp = max(self.clock(), self.last.timestamp)  # never earlier than the parent
        block = Block(len(self.chain), txs, self.last.hash(), timestamp=timestamp,
                      difficulty=self.difficulty)
        while not block.meets_difficulty():
            block.nonce += 1
        self.chain.append(block)
        self.utxos = view
        tx_rules.apply(txs[0], self.utxos)
        self.pending = []
        return block

    def is_valid(self, chain=None):
        """Fully re-check a chain, e.g. one received from another node."""
        return self._replay(self.chain if chain is None else chain) is not None

    def _replay(self, chain):
        """Validate every block from genesis and return the resulting UTXO set, or None."""
        if not chain or chain[0].hash() != self.chain[0].hash():
            return None  # different genesis = different network
        utxos = {}
        for i, (prev, curr) in enumerate(zip(chain, chain[1:]), start=1):
            if curr.index != prev.index + 1 or curr.prev_hash != prev.hash():
                return None
            if curr.difficulty != self.next_difficulty(chain[:i]):
                return None  # miner picked an easier difficulty than the rules allow
            if not curr.meets_difficulty() or not curr.transactions:
                return None
            # timestamps drive difficulty, so stop miners from faking them
            if curr.timestamp < prev.timestamp or curr.timestamp > self.clock() + MAX_FUTURE_DRIFT:
                return None
            if not curr.has_valid_merkle_root():
                return None  # transactions don't match what the header committed to
            try:
                self._apply_block(curr, utxos)
            except (ValueError, KeyError, TypeError):
                return None
        return utxos

    @staticmethod
    def _apply_block(block, utxos):
        """Check a block's transactions against `utxos` and apply them. Raises if invalid."""
        cb, *rest = block.transactions
        tx_rules.check_shape(cb)
        if cb["inputs"] or cb.get("height") != block.index:
            raise ValueError("first transaction must be this block's coinbase")
        fees = 0
        for tx in rest:
            fees += tx_rules.validate(tx, utxos)  # also rejects a second coinbase
            tx_rules.apply(tx, utxos)
        if sum(o["amount"] for o in cb["outputs"]) > tx_rules.BLOCK_REWARD + fees:
            raise ValueError("coinbase pays more than reward + fees")
        tx_rules.apply(cb, utxos)

    def utxos_for(self, address, include_pending=True):
        """Unspent outputs an address can spend, in the shape Wallet.create_transaction wants."""
        source = self._mempool_view() if include_pending else self.utxos
        return [{"txid": t, "index": n, "amount": o["amount"]}
                for (t, n), o in source.items() if o["address"] == address]

    # --- networking helpers (stage 4) ---

    def to_list(self):
        # "hash" and "txids" are for humans; from_dict ignores them and receivers recompute
        return [{**b.to_dict(), "hash": b.hash(), "txids": [tx_rules.txid(t) for t in b.transactions]}
                for b in self.chain]

    def replace_chain(self, chain_dicts):
        """Adopt a chain with more total work, if it is fully valid."""
        try:
            candidate = [Block.from_dict(d) for d in chain_dicts]
        except (KeyError, TypeError):
            return False
        # Most-work rule: length alone could be faked with many easy blocks.
        if self.total_work(candidate) <= self.total_work():
            return False
        utxos = self._replay(candidate)
        if utxos is None:
            return False
        self.chain, self.utxos = candidate, utxos
        self._recheck_pending()
        return True

    def add_block(self, block_dict):
        """Append a block a peer just mined, if it extends our tip validly."""
        try:
            block = Block.from_dict(block_dict)
        except (KeyError, TypeError):
            return False
        # cheap header checks first, then only this block's txs against our UTXO set
        prev = self.last
        if block.index != prev.index + 1 or block.prev_hash != prev.hash():
            return False
        if block.difficulty != self.difficulty or not block.meets_difficulty():
            return False
        if block.timestamp < prev.timestamp or block.timestamp > self.clock() + MAX_FUTURE_DRIFT:
            return False
        if not block.transactions or not block.has_valid_merkle_root():
            return False
        utxos = dict(self.utxos)
        try:
            self._apply_block(block, utxos)
        except (ValueError, KeyError, TypeError):
            return False
        self.chain.append(block)
        self.utxos = utxos
        self._recheck_pending()
        return True

    def _recheck_pending(self):
        """After the chain changes, keep only pending txs that are still valid.

        Ones that got mined now fail as "already spent", which is what drops them.
        """
        old, self.pending = self.pending, []
        for tx in old:
            try:
                self.add_transaction(tx)
            except ValueError:
                pass

    def balance(self, address):
        """Sum of confirmed unspent outputs owned by the address."""
        return sum(o["amount"] for o in self.utxos.values() if o["address"] == address)

    def available(self, address):
        """Balance including pending transactions (outgoing spent, incoming change counted)."""
        return sum(o["amount"] for o in self._mempool_view().values() if o["address"] == address)


if __name__ == "__main__":
    alice, bob = Wallet(), Wallet()
    bc = Blockchain()
    print(f"alice: {alice.address[:16]}...\nbob:   {bob.address[:16]}...")

    print("Mining block 1...")
    start = time.time()
    b = bc.mine(alice.address)
    print(f"  nonce={b.nonce} hash={b.hash()[:16]}... ({time.time() - start:.2f}s)")

    print(f"alice's coins: {bc.utxos_for(alice.address)}")
    tx = alice.create_transaction(bc.utxos_for(alice.address), bob.address, 20, fee=1)
    print("alice pays bob 20 with a fee of 1:")
    print(f"  spends 1 coin of 50 -> outputs {[o['amount'] for o in tx['outputs']]} (bob, change)")
    bc.add_transaction(tx)
    print("Mining block 2 (miner keeps the fee)...")
    b = bc.mine(alice.address)
    print(f"  nonce={b.nonce} hash={b.hash()[:16]}...")

    print(f"Balances: alice={bc.balance(alice.address)} bob={bc.balance(bob.address)}")
    print(f"Chain valid: {bc.is_valid()}")

    print("Bob tries to spend alice's coins with his own key...")
    forged = {"inputs": [{"txid": c["txid"], "index": c["index"]} for c in bc.utxos_for(alice.address)],
              "outputs": [{"address": bob.address, "amount": 10}]}
    try:
        bc.add_transaction(bob.sign_inputs(forged))
    except ValueError as e:
        print(f"  rejected: {e}")

    print("alice tries to spend the same coin twice...")
    coins = bc.utxos_for(alice.address)
    bc.add_transaction(alice.create_transaction(coins, bob.address, 5))
    try:
        bc.add_transaction(alice.create_transaction(coins, bob.address, 5))
    except ValueError as e:
        print(f"  rejected: {e}")

    print("Tampering with an amount in block 2...")
    bc.chain[2].transactions[1]["outputs"][0]["amount"] = 1_000_000
    print(f"Chain valid: {bc.is_valid()}")
