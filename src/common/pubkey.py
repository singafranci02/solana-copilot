"""On-curve test for Solana addresses — the wallet-vs-PDA discriminator.

Why this exists: team supply exceeded 100% because the PumpSwap pool was being
counted as a holder. The first fix excluded owners whose account is owned by a
program rather than the System Program, which catches initialized PDAs — but a
pool authority PDA is frequently NEVER initialized, so getMultipleAccounts
returns null for it and it looks exactly like an unfunded wallet.

Curve membership settles it without a network call and without a vendor's pool
list: a keypair's public key is a point ON the ed25519 curve by construction,
while a program-derived address is defined as a hash that is deliberately OFF
the curve (that is precisely what makes it unsignable). So an off-curve address
can never be a wallet, funded or not.

Pure Python, no dependency — the project has neither solders nor base58.
"""

from __future__ import annotations

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(_B58)}

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        v = _B58_IDX.get(ch)
        if v is None:
            raise ValueError(f"invalid base58 char {ch!r}")
        n = n * 58 + v
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def is_on_curve(address: str) -> bool:
    """True if `address` is a valid ed25519 point (i.e. can be a wallet).

    Returns True on anything unparseable: this gates EXCLUSION, so an address we
    cannot decode must not be silently dropped from the holder set.
    """
    try:
        raw = b58decode(address)
    except ValueError:
        return True
    if len(raw) != 32:
        return True

    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    u = (y2 - 1) % _P                      # numerator   of x^2 = (y^2-1)/(d*y^2+1)
    v = (_D * y2 + 1) % _P                 # denominator
    if v == 0:
        return False
    xx = u * pow(v, _P - 2, _P) % _P
    if xx == 0:
        return True
    return pow(xx, (_P - 1) // 2, _P) == 1  # Euler's criterion: is x^2 a residue?
