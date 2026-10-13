"""ECDSA wallets that build and sign UTXO transactions."""
from ecdsa import BadSignatureError, SECP256k1, SigningKey, VerifyingKey


class Wallet:
    def __init__(self, signing_key=None):
        self.signing_key = signing_key or SigningKey.generate(curve=SECP256k1)

    def save(self, path):
        """Write the private key to a PEM file. Refuses to overwrite an existing wallet."""
        with open(path, "xb") as f:  # "x" = fail if the file exists
            f.write(self.signing_key.to_pem())

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            return cls(SigningKey.from_pem(f.read()))

    @property
    def address(self):
        """The hex-encoded public key doubles as the wallet address."""
        return self.signing_key.get_verifying_key().to_string().hex()

    def sign_inputs(self, tx):
        """Sign every input with this wallet's key. Each signature covers the whole txid."""
        from transactions import txid  # avoid a circular import

        for i in tx["inputs"]:
            i["signature"] = ""
        message = bytes.fromhex(txid(tx))
        for i in tx["inputs"]:
            i["signature"] = self.signing_key.sign(message).hex()
        return tx

    def create_transaction(self, utxos, recipient, amount, fee=0):
        """Pay `amount` to `recipient` from our unspent outputs.

        `utxos` is a list of {"txid", "index", "amount"} we own (see Blockchain.utxos_for
        or GET /utxos/<address>). Picks coins until they cover amount + fee and sends
        whatever is left back to ourselves as change.
        """
        chosen, total = [], 0
        for u in sorted(utxos, key=lambda u: u["amount"], reverse=True):  # largest first
            if total >= amount + fee:
                break
            chosen.append(u)
            total += u["amount"]
        if total < amount + fee:
            raise ValueError(f"insufficient funds: have {total}, need {amount + fee}")
        outputs = [{"address": recipient, "amount": amount}]
        if total - amount - fee:
            outputs.append({"address": self.address, "amount": total - amount - fee})
        tx = {"inputs": [{"txid": u["txid"], "index": u["index"]} for u in chosen],
              "outputs": outputs}
        return self.sign_inputs(tx)


def verify(address, signature_hex, message):
    try:
        key = VerifyingKey.from_string(bytes.fromhex(address), curve=SECP256k1)
        return key.verify(bytes.fromhex(signature_hex), message)
    except (BadSignatureError, ValueError, AssertionError):
        return False
