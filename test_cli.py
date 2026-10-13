import threading

import pytest
from werkzeug.serving import make_server

import cli
from node import create_app
from wallet import Wallet


@pytest.fixture
def node():
    server = make_server("127.0.0.1", 0, create_app(difficulty=8), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def run(tmp_path, node, capsys):
    """Run a CLI command against the test node with a temp key folder; return (code, out, err)."""
    def _run(*argv):
        code = cli.main(["--node", node, "--dir", str(tmp_path), *argv])
        out, err = capsys.readouterr()
        return code, out, err
    return _run


def test_new_and_list(run, tmp_path):
    code, out, _ = run("new", "alice")
    assert code == 0 and "address:" in out
    assert (tmp_path / "alice.pem").exists()
    assert "alice" in run("list")[1]


def test_new_refuses_to_overwrite_existing_wallet(run, tmp_path):
    run("new", "alice")
    before = (tmp_path / "alice.pem").read_bytes()
    code, _, err = run("new", "alice")
    assert code == 1 and "already exists" in err
    assert (tmp_path / "alice.pem").read_bytes() == before  # key not lost


def test_key_file_round_trips(run, tmp_path):
    run("new", "alice")
    addr = run("address", "alice")[1].strip()
    assert Wallet.load(tmp_path / "alice.pem").address == addr


def test_rejects_path_like_names(run):
    code, _, err = run("new", "../evil")
    assert code == 1 and "bad wallet name" in err


def test_mine_send_balance_flow(run):
    run("new", "alice")
    run("new", "bob")
    assert run("mine", "alice")[0] == 0
    assert "50 confirmed" in run("balance", "alice")[1]

    code, out, _ = run("send", "alice", "bob", "20", "--fee", "1")
    assert code == 0 and "change 29" in out and "txid:" in out
    assert "0 confirmed, 20 available" in run("balance", "bob")[1]

    run("mine", "bob")  # bob also collects the fee
    assert "71 confirmed" in run("balance", "bob")[1]
    assert "29 confirmed" in run("balance", "alice")[1]
    amounts = sorted(int(line.split()[0]) for line in run("coins", "bob")[1].splitlines())
    assert amounts == [20, 51]  # the payment and the reward+fee are separate coins


def test_send_to_raw_address(run):
    run("new", "alice")
    run("mine", "alice")
    outsider = Wallet().address
    code, out, _ = run("send", "alice", outsider, "5")
    assert code == 0 and "change 45" in out


def test_send_insufficient_funds(run):
    run("new", "alice")
    run("new", "bob")
    code, _, err = run("send", "alice", "bob", "5")
    assert code == 1 and "insufficient" in err


def test_send_to_unknown_recipient(run):
    run("new", "alice")
    code, _, err = run("send", "alice", "nobody", "5")
    assert code == 1 and "not one of your wallets" in err


@pytest.mark.parametrize("args", [["0"], ["-3"], ["5", "--fee", "-1"]])
def test_send_rejects_bad_amounts(run, args):
    run("new", "alice")
    run("new", "bob")
    code, _, err = run("send", "alice", "bob", *args)
    assert code == 1 and "amount must be positive" in err


def test_unknown_wallet(run):
    code, _, err = run("balance", "ghost")
    assert code == 1 and "no wallet named" in err


def test_node_offline(tmp_path, capsys):
    cli.main(["--dir", str(tmp_path), "new", "alice"])
    code = cli.main(["--node", "http://127.0.0.1:9", "--dir", str(tmp_path), "balance", "alice"])
    assert code == 1 and "can't reach node" in capsys.readouterr().err
