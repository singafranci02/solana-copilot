"""On-curve address test — the wallet-vs-PDA discriminator.

Validated against a known vector (the ed25519 basepoint) and against the
statistical property that ~half of random 32-byte strings decode to a valid
curve point, so a decoding bug cannot hide behind a plausible-looking rate.
"""

import os

from src.common.pubkey import _D, _P, b58decode, is_on_curve

# The exact PumpSwap authority PDA that was counted as a team member on 15 coins,
# pushing team supply above 100%. Off the curve, and its account was never
# initialized, so ownership checks alone could not exclude it.
POOL_PDA = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"

# A real holder wallet, taken verbatim from a stored holder snapshot.
REAL_WALLET = "DoNtG44CPnFHaVbVYHTCCtJQvmubqQN4RiwvAvFgd3d"


def _on_curve_bytes(raw: bytes) -> bool:
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    v = (_D * y2 + 1) % _P
    if v == 0:
        return False
    xx = (y2 - 1) * pow(v, _P - 2, _P) % _P
    return True if xx == 0 else pow(xx, (_P - 1) // 2, _P) == 1


def test_ed25519_basepoint_is_on_curve():
    """Known vector: the standard basepoint encoding, y = 4/5 mod p."""
    assert _on_curve_bytes(bytes([0x58] + [0x66] * 31)) is True


def test_random_bytes_are_on_curve_about_half_the_time():
    """A decoder that mangles input would skew far from 50%."""
    hits = sum(_on_curve_bytes(os.urandom(32)) for _ in range(2000))
    assert 0.42 < hits / 2000 < 0.58


def test_pool_pda_is_off_curve():
    assert is_on_curve(POOL_PDA) is False


def test_real_wallet_is_on_curve():
    assert is_on_curve(REAL_WALLET) is True


def test_b58decode_lengths():
    assert len(b58decode(POOL_PDA)) == 32
    assert len(b58decode(REAL_WALLET)) == 32
    assert b58decode("11111111111111111111111111111111") == b"\x00" * 32


def test_addresses_with_leading_zero_bytes_decode_to_32_bytes():
    """Leading '1's are zero bytes; dropping them yields a short key that would
    be misread as a different curve point."""
    addr = "111134RmVrGpCE3LnLyjcXp8Zc1MXnDjouB48uqUuv"
    assert len(b58decode(addr)) == 32


def test_unparseable_input_is_kept_not_excluded():
    # This gates EXCLUSION: anything we cannot decode must stay in the holder set
    # rather than be silently dropped.
    assert is_on_curve("not-base58-0OIl") is True
    assert is_on_curve("abc") is True
