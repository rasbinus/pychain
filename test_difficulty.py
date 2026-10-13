"""Stage 5: difficulty adjustment and most-work consensus."""
from blockchain import MAX_STEP, MIN_DIFFICULTY, Block, Blockchain
from transactions import coinbase
from wallet import Wallet

MINER = Wallet().address


class FakeClock:
    """Each call advances time by `step` seconds, simulating how fast blocks are found."""

    def __init__(self, step):
        self.now, self.step = 1_000_000.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def chain(seconds_per_block, difficulty=6, target=10, interval=5):
    return Blockchain(difficulty, target_block_time=target, adjust_interval=interval,
                      clock=FakeClock(seconds_per_block))


def mine_n(bc, n):
    for _ in range(n):
        bc.mine(MINER)


def test_difficulty_stays_put_between_retargets():
    bc = chain(seconds_per_block=1)
    mine_n(bc, 9)  # retarget happens at height 10 (first window can't include genesis)
    assert {b.difficulty for b in bc.chain} == {6}


def test_fast_blocks_raise_difficulty():
    bc = chain(seconds_per_block=1)  # 10x faster than target
    mine_n(bc, 10)
    assert bc.chain[-1].difficulty == 6 + MAX_STEP  # log2(10) ~ 3.3, capped at 2


def test_slow_blocks_lower_difficulty():
    bc = chain(seconds_per_block=40)  # 4x slower than target
    mine_n(bc, 10)
    assert bc.chain[-1].difficulty == 6 - 2


def test_on_target_blocks_keep_difficulty():
    bc = chain(seconds_per_block=10)
    mine_n(bc, 15)
    assert {b.difficulty for b in bc.chain} == {6}


def test_difficulty_never_drops_below_minimum():
    bc = chain(seconds_per_block=10_000, difficulty=2)
    mine_n(bc, 20)
    assert bc.chain[-1].difficulty == MIN_DIFFICULTY


def test_adjusted_chain_is_valid_and_blocks_meet_their_target():
    bc = chain(seconds_per_block=1)
    mine_n(bc, 15)
    assert bc.is_valid()
    assert all(int(b.hash(), 16) < 1 << (256 - b.difficulty) for b in bc.chain[1:])


def test_block_claiming_easier_difficulty_rejected():
    bc = chain(seconds_per_block=1)
    mine_n(bc, 9)
    cheat = Block(10, [coinbase(MINER, len(bc.chain))], bc.last.hash(),
                  timestamp=bc.clock(), difficulty=1)  # rules say 8
    while not cheat.meets_difficulty():
        cheat.nonce += 1
    assert not bc.add_block(cheat.to_dict())


def test_control_honest_hand_built_block_accepted():
    """Same construction as the rejection tests below, minus the flaw, so they fail for the right reason."""
    bc = chain(seconds_per_block=10)
    mine_n(bc, 2)
    block = Block(3, [coinbase(MINER, len(bc.chain))], bc.last.hash(),
                  timestamp=bc.clock(), difficulty=bc.difficulty)
    while not block.meets_difficulty():
        block.nonce += 1
    assert bc.add_block(block.to_dict())


def test_timestamp_before_parent_rejected():
    bc = chain(seconds_per_block=10)
    mine_n(bc, 2)
    block = Block(3, [coinbase(MINER, len(bc.chain))], bc.last.hash(),
                  timestamp=bc.last.timestamp - 1, difficulty=bc.difficulty)
    while not block.meets_difficulty():
        block.nonce += 1
    assert not bc.add_block(block.to_dict())


def test_timestamp_far_in_future_rejected():
    bc = chain(seconds_per_block=10)
    mine_n(bc, 2)
    block = Block(3, [coinbase(MINER, len(bc.chain))], bc.last.hash(),
                  timestamp=bc.clock() + 3600, difficulty=bc.difficulty)
    while not block.meets_difficulty():
        block.nonce += 1
    assert not bc.add_block(block.to_dict())


def test_more_work_beats_more_blocks():
    """Many easy blocks must not beat fewer, harder ones."""
    honest = Blockchain(difficulty=1, adjust_interval=3, clock=FakeClock(0.001))
    mine_n(honest, 12)  # blocks come fast, so difficulty climbs: fewer blocks, more work
    attacker = Blockchain(difficulty=1, adjust_interval=3, clock=FakeClock(10_000))
    mine_n(attacker, 15)  # slow blocks keep difficulty at the minimum

    assert len(attacker.chain) > len(honest.chain)
    assert attacker.total_work() < honest.total_work()
    assert not honest.replace_chain(attacker.to_list())
    assert attacker.replace_chain(honest.to_list())
