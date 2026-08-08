"""RPC holder snapshots — shape parity with the paid API, and the safety rules."""

import pytest

from src.ingest.rpc_holders import get_token_holders_rpc, get_token_holders_resilient


class FakeSession:
    """Serves canned RPC results keyed by method."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json["method"])
        payload = self.results.get(json["method"])
        session = self

        class Ctx:
            async def __aenter__(self):
                class R:
                    async def json(_self):
                        return {"result": payload} if payload is not None else {}
                return R()

            async def __aexit__(self, *a):
                return False
        return Ctx()


LARGEST = {"value": [{"address": "tokenacct1", "uiAmount": 100.0},
                     {"address": "tokenacct2", "uiAmount": 50.0}]}
OWNERS = {"value": [{"data": {"parsed": {"info": {"owner": "walletA"}}}},
                    {"data": {"parsed": {"info": {"owner": "walletB"}}}}]}


@pytest.mark.asyncio
async def test_resolves_token_accounts_to_owner_wallets():
    """The critical distinction: a token ACCOUNT is not a wallet. Team detection
    reasons about wallets, so an unresolved account must never leak through."""
    s = FakeSession({"getTokenLargestAccounts": LARGEST, "getMultipleAccounts": OWNERS})
    rows = await get_token_holders_rpc(s, "mint", ["http://x"])
    assert rows == [{"address": "walletA", "uiAmount": 100.0},
                    {"address": "walletB", "uiAmount": 50.0}]


@pytest.mark.asyncio
async def test_merges_multiple_token_accounts_of_one_wallet():
    """One wallet can hold several token accounts for the same mint — balances must
    be summed, or the same entity appears twice with split supply."""
    owners = {"value": [{"data": {"parsed": {"info": {"owner": "walletA"}}}},
                        {"data": {"parsed": {"info": {"owner": "walletA"}}}}]}
    s = FakeSession({"getTokenLargestAccounts": LARGEST, "getMultipleAccounts": owners})
    rows = await get_token_holders_rpc(s, "mint", ["http://x"])
    assert rows == [{"address": "walletA", "uiAmount": 150.0}]


@pytest.mark.asyncio
async def test_unresolvable_owner_is_dropped_not_guessed():
    owners = {"value": [{"data": {"parsed": {"info": {"owner": "walletA"}}}}, None]}
    s = FakeSession({"getTokenLargestAccounts": LARGEST, "getMultipleAccounts": owners})
    rows = await get_token_holders_rpc(s, "mint", ["http://x"])
    assert rows == [{"address": "walletA", "uiAmount": 100.0}]


@pytest.mark.asyncio
async def test_returns_empty_when_rpc_unavailable():
    s = FakeSession({})
    assert await get_token_holders_rpc(s, "mint", ["http://x"]) == []


@pytest.mark.asyncio
async def test_falls_back_to_paid_api_only_when_rpc_empty():
    class ST:
        def __init__(self): self.called = False
        async def get_token_holders(self, mint):
            self.called = True
            return [{"address": "fromST", "uiAmount": 1.0}]

    st = ST()
    rows, src = await get_token_holders_resilient(FakeSession({}), "mint", st)
    assert src == "solana_tracker" and st.called and rows[0]["address"] == "fromST"

    st2 = ST()
    good = FakeSession({"getTokenLargestAccounts": LARGEST, "getMultipleAccounts": OWNERS})
    rows, src = await get_token_holders_resilient(good, "mint", st2)
    assert src == "rpc" and not st2.called      # paid API untouched when RPC works
