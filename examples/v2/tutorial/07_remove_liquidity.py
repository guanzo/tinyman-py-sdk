# This sample is provided for demonstration purposes only.
# It is not intended for production use.
# This example does not constitute trading advice.
from pprint import pprint
from urllib.parse import quote_plus

from examples.v2.tutorial.common import get_account, get_algod, get_assets
from tinyman.v2.client import TinymanV2TestnetClient


account = get_account()
algod = get_algod()
client = TinymanV2TestnetClient(algod_client=algod, user_address=account["address"])

ASSET_A_ID, ASSET_B_ID = get_assets()["ids"]
ASSET_A = client.fetch_asset(ASSET_A_ID)
ASSET_B = client.fetch_asset(ASSET_B_ID)
pool = client.fetch_pool(ASSET_A_ID, ASSET_B_ID)

assert pool.exists, "Pool has not been bootstrapped yet!"
assert pool.issued_pool_tokens, "Pool has no liquidity"

position = pool.fetch_pool_position()
pool_token_asset_in = position[pool.pool_token_asset].amount // 4

quote = pool.fetch_remove_liquidity_quote(
    pool_token_asset_in=pool_token_asset_in,
    slippage=0,  # TODO: 0.05
)

print("\nRemove Liquidity Quote:")
print(quote)

txn_group = pool.prepare_remove_liquidity_transactions_from_quote(quote=quote)

# Sign
txn_group.sign_with_private_key(account["address"], account["private_key"])

# Submit
txinfo = txn_group.submit(algod, wait=True)
print("Transaction Info")
pprint(txinfo)

print(
    f"Check the transaction group on Algoexplorer: https://testnet.algoexplorer.io/tx/group/{quote_plus(txn_group.id)}"
)

pool.refresh()

pool_position = pool.fetch_pool_position()
share = pool_position["share"] * 100
print(f"Pool Tokens: {pool_position[pool.liquidity_asset]}")
print(f"Assets: {pool_position[ASSET_A]}, {pool_position[ASSET_B]}")
print(f"Share of pool: {share:.3f}%")
