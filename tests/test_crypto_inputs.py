"""Invalid custodian shares must never silently produce a private key."""
import pytest
from nacl.public import PrivateKey
from pol2_agent_ballot.crypto import KeyShare, _P, recover_private_key, split_private_key


@pytest.mark.parametrize("k", [0, -1, True, 1.5])
def test_recovery_requires_positive_integer_threshold(k):
    with pytest.raises(ValueError):
        recover_private_key([], k)


def test_duplicate_shares_do_not_satisfy_threshold():
    shares = split_private_key(PrivateKey.generate(), 2, 3)
    with pytest.raises(ValueError, match="duplicate"):
        recover_private_key([shares[0], shares[0]], 2)


@pytest.mark.parametrize("share", [KeyShare(0, 1, 1), KeyShare(_P, 1, 1),
    KeyShare(-1, 1, 1), KeyShare(True, 1, 1), KeyShare(1, -1, 1),
    KeyShare(1, 1, _P), KeyShare(1, 1.5, 1), {}])
def test_recovery_rejects_out_of_field_shares(share):
    with pytest.raises(ValueError):
        recover_private_key([share], 1)


@pytest.mark.parametrize("k,n", [(True, 3), (2.0, 3), (2, 3.0), (0, 3), (3, 2)])
def test_split_requires_integer_thresholds(k, n):
    with pytest.raises(ValueError):
        split_private_key(PrivateKey.generate(), k, n)


def test_valid_unordered_custodians_still_recover():
    key = PrivateKey.generate()
    shares = split_private_key(key, 2, 4)
    assert bytes(recover_private_key([shares[3], shares[1]], 2)) == bytes(key)
