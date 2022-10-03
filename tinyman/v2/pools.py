from typing import Optional

from algosdk.future.transaction import LogicSigAccount
from algosdk.v2client.algod import AlgodClient

from tinyman.assets import Asset, AssetAmount
from tinyman.optin import prepare_asset_optin_transactions
from tinyman.utils import get_state_int, get_state_bytes, bytes_to_int, TransactionGroup
from .add_liquidity import (
    prepare_initial_add_liquidity_transactions,
    prepare_single_asset_add_liquidity_transactions,
    prepare_flexible_add_liquidity_transactions,
)
from .bootstrap import prepare_bootstrap_transactions
from .client import TinymanV2Client
from .constants import MIN_POOL_BALANCE_ASA_ALGO_PAIR, MIN_POOL_BALANCE_ASA_ASA_PAIR
from .contracts import get_pool_logicsig
from .exceptions import (
    AlreadyBootstrapped,
    BootstrapIsRequired,
    PoolHasNoLiquidity,
    PoolAlreadyHasLiquidity,
)
from .formulas import (
    calculate_subsequent_add_liquidity,
    calculate_initial_add_liquidity,
    calculate_fixed_input_swap,
    calculate_remove_liquidity_output_amounts,
    calculate_fixed_output_swap,
)
from .quotes import (
    FlexibleAddLiquidityQuote,
    SingleAssetAddLiquidityQuote,
    InitialAddLiquidityQuote,
    RemoveLiquidityQuote,
    InternalSwapQuote,
    SingleAssetRemoveLiquidityQuote,
    SwapQuote,
)
from .remove_liquidity import (
    prepare_remove_liquidity_transactions,
    prepare_single_asset_remove_liquidity_transactions,
)
from .swap import prepare_swap_transactions


def get_pool_info(
    client: AlgodClient, validator_app_id: int, asset_1_id: int, asset_2_id: int
) -> dict:
    pool_logicsig = get_pool_logicsig(validator_app_id, asset_1_id, asset_2_id)
    pool_address = pool_logicsig.address()
    account_info = client.account_info(pool_address)
    return get_pool_info_from_account_info(account_info)


def get_pool_info_from_account_info(account_info: dict) -> dict:
    try:
        validator_app_id = account_info["apps-local-state"][0]["id"]
    except IndexError:
        return {}
    validator_app_state = {
        x["key"]: x["value"] for x in account_info["apps-local-state"][0]["key-value"]
    }

    asset_1_id = get_state_int(validator_app_state, "asset_1_id")
    asset_2_id = get_state_int(validator_app_state, "asset_2_id")

    pool_logicsig = get_pool_logicsig(validator_app_id, asset_1_id, asset_2_id)
    pool_address = pool_logicsig.address()

    assert account_info["address"] == pool_address

    pool_token_asset_id = get_state_int(validator_app_state, "pool_token_asset_id")
    issued_pool_tokens = get_state_int(validator_app_state, "issued_pool_tokens")

    # reserves
    asset_1_reserves = get_state_int(validator_app_state, "asset_1_reserves")
    asset_2_reserves = get_state_int(validator_app_state, "asset_2_reserves")

    # fees
    asset_1_protocol_fees = get_state_int(validator_app_state, "asset_1_protocol_fees")
    asset_2_protocol_fees = get_state_int(validator_app_state, "asset_2_protocol_fees")

    # fee rates
    total_fee_share = get_state_int(validator_app_state, "total_fee_share")
    protocol_fee_ratio = get_state_int(validator_app_state, "protocol_fee_ratio")

    # oracle
    asset_1_cumulative_price = bytes_to_int(
        get_state_bytes(validator_app_state, "asset_1_cumulative_price")
    )
    asset_2_cumulative_price = bytes_to_int(
        get_state_bytes(validator_app_state, "asset_2_cumulative_price")
    )
    cumulative_price_update_timestamp = get_state_int(
        validator_app_state, "cumulative_price_update_timestamp"
    )

    pool = {
        "address": pool_address,
        "asset_1_id": asset_1_id,
        "asset_2_id": asset_2_id,
        "pool_token_asset_id": pool_token_asset_id,
        "asset_1_reserves": asset_1_reserves,
        "asset_2_reserves": asset_2_reserves,
        "issued_pool_tokens": issued_pool_tokens,
        "asset_1_protocol_fees": asset_1_protocol_fees,
        "asset_2_protocol_fees": asset_2_protocol_fees,
        "asset_1_cumulative_price": asset_1_cumulative_price,
        "asset_2_cumulative_price": asset_2_cumulative_price,
        "cumulative_price_update_timestamp": cumulative_price_update_timestamp,
        "total_fee_share": total_fee_share,
        "protocol_fee_ratio": protocol_fee_ratio,
        "validator_app_id": validator_app_id,
        "algo_balance": account_info["amount"],
        "round": account_info["round"],
    }
    return pool


class Pool:
    def __init__(
        self,
        client: TinymanV2Client,
        asset_a: [Asset, int],
        asset_b: [Asset, int],
        info=None,
        fetch=True,
        validator_app_id=None,
    ) -> None:
        self.client = client
        self.validator_app_id = (
            validator_app_id
            if validator_app_id is not None
            else client.validator_app_id
        )

        if isinstance(asset_a, int):
            asset_a = client.fetch_asset(asset_a)
        if isinstance(asset_b, int):
            asset_b = client.fetch_asset(asset_b)

        if asset_a.id > asset_b.id:
            self.asset_1 = asset_a
            self.asset_2 = asset_b
        else:
            self.asset_1 = asset_b
            self.asset_2 = asset_a

        self.exists = None
        self.pool_token_asset: Asset = None
        self.asset_1_reserves = None
        self.asset_2_reserves = None
        self.issued_pool_tokens = None
        self.asset_1_protocol_fees = None
        self.asset_2_protocol_fees = None
        self.total_fee_share = None
        self.protocol_fee_ratio = None
        self.last_refreshed_round = None
        self.algo_balance = None

        if fetch:
            self.refresh()
        elif info is not None:
            self.update_from_info(info)

    def __repr__(self):
        return f"Pool {self.asset_1.unit_name}({self.asset_1.id})-{self.asset_2.unit_name}({self.asset_2.id}) {self.address}"

    @classmethod
    def from_account_info(
        cls, account_info: dict, client: Optional[TinymanV2Client] = None
    ):
        info = get_pool_info_from_account_info(account_info)
        pool = Pool(
            client,
            info["asset_1_id"],
            info["asset_2_id"],
            info,
            validator_app_id=info["validator_app_id"],
        )
        return pool

    def refresh(self, info: Optional[dict] = None) -> None:
        if info is None:
            info = get_pool_info(
                self.client.algod,
                self.validator_app_id,
                self.asset_1.id,
                self.asset_2.id,
            )
            if not info:
                return
        self.update_from_info(info)

    def update_from_info(self, info: dict) -> None:
        if info["pool_token_asset_id"] is not None:
            self.exists = True

        self.pool_token_asset = self.client.fetch_asset(info["pool_token_asset_id"])
        self.asset_1_reserves = info["asset_1_reserves"]
        self.asset_2_reserves = info["asset_2_reserves"]
        self.issued_pool_tokens = info["issued_pool_tokens"]
        self.asset_1_protocol_fees = info["asset_1_protocol_fees"]
        self.asset_2_protocol_fees = info["asset_2_protocol_fees"]
        self.total_fee_share = info["total_fee_share"]
        self.protocol_fee_ratio = info["protocol_fee_ratio"]
        self.last_refreshed_round = info["round"]
        self.algo_balance = info["algo_balance"]

    def get_logicsig(self) -> LogicSigAccount:
        pool_logicsig = get_pool_logicsig(
            self.validator_app_id, self.asset_1.id, self.asset_2.id
        )
        return pool_logicsig

    @property
    def address(self) -> str:
        logicsig = self.get_logicsig()
        pool_address = logicsig.address()
        return pool_address

    @property
    def asset_1_price(self) -> float:
        if not self.issued_pool_tokens:
            raise PoolHasNoLiquidity()

        return self.asset_2_reserves / self.asset_1_reserves

    @property
    def asset_2_price(self) -> float:
        if not self.issued_pool_tokens:
            raise PoolHasNoLiquidity()

        return self.asset_1_reserves / self.asset_2_reserves

    def info(self) -> dict:
        if not self.exists:
            raise BootstrapIsRequired()

        pool = {
            "address": self.address,
            "asset_1_id": self.asset_1.id,
            "asset_2_id": self.asset_2.id,
            "asset_1_unit_name": self.asset_1.unit_name,
            "asset_2_unit_name": self.asset_2.unit_name,
            "pool_token_asset_id": self.pool_token_asset.id,
            "pool_token_asset_name": self.pool_token_asset.name,
            "asset_1_reserves": self.asset_1_reserves,
            "asset_2_reserves": self.asset_2_reserves,
            "issued_pool_tokens": self.issued_pool_tokens,
            "asset_1_protocol_fees": self.asset_1_protocol_fees,
            "asset_2_protocol_fees": self.asset_2_protocol_fees,
            "total_fee_share": self.total_fee_share,
            "protocol_fee_ratio": self.protocol_fee_ratio,
            "last_refreshed_round": self.last_refreshed_round,
        }
        return pool

    def convert(self, amount: AssetAmount) -> AssetAmount:
        if not self.issued_pool_tokens:
            raise PoolHasNoLiquidity()

        if amount.asset == self.asset_1:
            return AssetAmount(self.asset_2, int(amount.amount * self.asset_1_price))
        elif amount.asset == self.asset_2:
            return AssetAmount(self.asset_1, int(amount.amount * self.asset_2_price))

        raise NotImplementedError()

    def prepare_bootstrap_transactions(
        self, pooler_address: Optional[str] = None
    ) -> TransactionGroup:
        self.refresh()

        if self.exists:
            raise AlreadyBootstrapped()

        pooler_address = pooler_address or self.client.user_address
        suggested_params = self.client.algod.suggested_params()

        if self.asset_2.id == 0:
            pool_minimum_balance = MIN_POOL_BALANCE_ASA_ALGO_PAIR
            inner_transaction_count = 5
        else:
            pool_minimum_balance = MIN_POOL_BALANCE_ASA_ASA_PAIR
            inner_transaction_count = 6

        app_call_fee = (inner_transaction_count + 1) * suggested_params.min_fee
        required_algo = pool_minimum_balance + app_call_fee
        required_algo += (
            100_000  # to fund minimum balance increase because of asset creation
        )

        pool_account_info = self.client.algod.account_info(self.address)
        pool_algo_balance = pool_account_info["amount"]
        required_algo = max(required_algo - pool_algo_balance, 0)

        txn_group = prepare_bootstrap_transactions(
            validator_app_id=self.validator_app_id,
            asset_1_id=self.asset_1.id,
            asset_2_id=self.asset_2.id,
            sender=pooler_address,
            suggested_params=suggested_params,
            app_call_fee=app_call_fee,
            required_algo=required_algo,
        )
        return txn_group

    def fetch_flexible_add_liquidity_quote(
        self, amount_a: AssetAmount, amount_b: AssetAmount, slippage: float = 0.05
    ) -> FlexibleAddLiquidityQuote:
        assert {self.asset_1, self.asset_2} == {
            amount_a.asset,
            amount_b.asset,
        }, "Pool assets and given assets don't match."

        amount_1 = amount_a if amount_a.asset == self.asset_1 else amount_b
        amount_2 = amount_a if amount_a.asset == self.asset_2 else amount_b
        self.refresh()

        if not self.exists:
            raise BootstrapIsRequired()

        if not self.issued_pool_tokens:
            raise PoolHasNoLiquidity()

        (
            pool_token_asset_amount,
            swap_from_asset_1_to_asset_2,
            swap_in_amount,
            swap_out_amount,
            swap_total_fee_amount,
            swap_price_impact,
        ) = calculate_subsequent_add_liquidity(
            asset_1_reserves=self.asset_1_reserves,
            asset_2_reserves=self.asset_2_reserves,
            issued_pool_tokens=self.issued_pool_tokens,
            total_fee_share=self.total_fee_share,
            asset_1_amount=amount_1.amount,
            asset_2_amount=amount_2.amount,
        )

        internal_swap_quote = InternalSwapQuote(
            amount_in=AssetAmount(
                self.asset_1 if swap_from_asset_1_to_asset_2 else self.asset_2,
                swap_in_amount,
            ),
            amount_out=AssetAmount(
                self.asset_2 if swap_from_asset_1_to_asset_2 else self.asset_1,
                swap_out_amount,
            ),
            swap_fees=AssetAmount(
                self.asset_1 if swap_from_asset_1_to_asset_2 else self.asset_2,
                int(swap_total_fee_amount),
            ),
            price_impact=swap_price_impact,
        )

        quote = FlexibleAddLiquidityQuote(
            amounts_in={
                self.asset_1: amount_1,
                self.asset_2: amount_2,
            },
            pool_token_asset_amount=AssetAmount(
                self.pool_token_asset, pool_token_asset_amount
            ),
            slippage=slippage,
            internal_swap_quote=internal_swap_quote,
        )
        return quote

    def fetch_single_asset_add_liquidity_quote(
        self, amount_a: AssetAmount, slippage: float = 0.05
    ) -> SingleAssetAddLiquidityQuote:
        self.refresh()
        if not self.exists:
            raise BootstrapIsRequired()

        if not self.issued_pool_tokens:
            raise PoolHasNoLiquidity()

        if amount_a.asset == self.asset_1:
            (
                pool_token_asset_amount,
                swap_from_asset_1_to_asset_2,
                swap_in_amount,
                swap_out_amount,
                swap_total_fee_amount,
                swap_price_impact,
            ) = calculate_subsequent_add_liquidity(
                asset_1_reserves=self.asset_1_reserves,
                asset_2_reserves=self.asset_2_reserves,
                issued_pool_tokens=self.issued_pool_tokens,
                total_fee_share=self.total_fee_share,
                asset_1_amount=amount_a.amount,
                asset_2_amount=0,
            )
        elif amount_a.asset == self.asset_2:
            (
                pool_token_asset_amount,
                swap_from_asset_1_to_asset_2,
                swap_in_amount,
                swap_out_amount,
                swap_total_fee_amount,
                swap_price_impact,
            ) = calculate_subsequent_add_liquidity(
                asset_1_reserves=self.asset_1_reserves,
                asset_2_reserves=self.asset_2_reserves,
                issued_pool_tokens=self.issued_pool_tokens,
                total_fee_share=self.total_fee_share,
                asset_1_amount=0,
                asset_2_amount=amount_a.amount,
            )
        else:
            assert False, "Given asset doesn't belong to the pool assets."

        internal_swap_quote = InternalSwapQuote(
            amount_in=AssetAmount(
                self.asset_1 if swap_from_asset_1_to_asset_2 else self.asset_2,
                swap_in_amount,
            ),
            amount_out=AssetAmount(
                self.asset_2 if swap_from_asset_1_to_asset_2 else self.asset_1,
                swap_out_amount,
            ),
            swap_fees=AssetAmount(
                self.asset_1 if swap_from_asset_1_to_asset_2 else self.asset_2,
                int(swap_total_fee_amount),
            ),
            price_impact=swap_price_impact,
        )
        quote = SingleAssetAddLiquidityQuote(
            amount_in=amount_a,
            pool_token_asset_amount=AssetAmount(
                self.pool_token_asset, pool_token_asset_amount
            ),
            slippage=slippage,
            internal_swap_quote=internal_swap_quote,
        )
        return quote

    def fetch_initial_add_liquidity_quote(
        self,
        amount_a: AssetAmount,
        amount_b: AssetAmount,
    ) -> InitialAddLiquidityQuote:
        assert {self.asset_1, self.asset_2} == {
            amount_a.asset,
            amount_b.asset,
        }, "Pool assets and given assets don't match."

        amount_1 = amount_a if amount_a.asset == self.asset_1 else amount_b
        amount_2 = amount_a if amount_a.asset == self.asset_2 else amount_b
        self.refresh()

        if not self.exists:
            raise BootstrapIsRequired()

        if self.issued_pool_tokens:
            raise PoolAlreadyHasLiquidity()

        pool_token_asset_amount = calculate_initial_add_liquidity(
            asset_1_amount=amount_1.amount, asset_2_amount=amount_2.amount
        )
        quote = InitialAddLiquidityQuote(
            amounts_in={
                self.asset_1: amount_1,
                self.asset_2: amount_2,
            },
            pool_token_asset_amount=AssetAmount(
                self.pool_token_asset, pool_token_asset_amount
            ),
        )
        return quote

    def prepare_flexible_add_liquidity_transactions(
        self,
        amounts_in: dict[Asset, AssetAmount],
        min_pool_token_asset_amount: int,
        pooler_address: Optional[str] = None,
    ) -> TransactionGroup:
        pooler_address = pooler_address or self.client.user_address
        asset_1_amount = amounts_in[self.asset_1]
        asset_2_amount = amounts_in[self.asset_2]
        suggested_params = self.client.algod.suggested_params()

        txn_group = prepare_flexible_add_liquidity_transactions(
            validator_app_id=self.validator_app_id,
            asset_1_id=self.asset_1.id,
            asset_2_id=self.asset_2.id,
            pool_token_asset_id=self.pool_token_asset.id,
            asset_1_amount=asset_1_amount.amount,
            asset_2_amount=asset_2_amount.amount,
            min_pool_token_asset_amount=min_pool_token_asset_amount,
            sender=pooler_address,
            suggested_params=suggested_params,
        )
        return txn_group

    def prepare_single_asset_add_liquidity_transactions(
        self,
        amount_in: AssetAmount,
        min_pool_token_asset_amount: Optional[int],
        pooler_address: Optional[str] = None,
    ) -> TransactionGroup:
        pooler_address = pooler_address or self.client.user_address
        suggested_params = self.client.algod.suggested_params()

        txn_group = prepare_single_asset_add_liquidity_transactions(
            validator_app_id=self.validator_app_id,
            asset_1_id=self.asset_1.id,
            asset_2_id=self.asset_2.id,
            pool_token_asset_id=self.pool_token_asset.id,
            asset_1_amount=amount_in.amount
            if amount_in.asset == self.asset_1
            else None,
            asset_2_amount=amount_in.amount
            if amount_in.asset == self.asset_2
            else None,
            min_pool_token_asset_amount=min_pool_token_asset_amount,
            sender=pooler_address,
            suggested_params=suggested_params,
        )
        return txn_group

    def prepare_initial_add_liquidity_transactions(
        self,
        amounts_in: dict[Asset, AssetAmount],
        pooler_address: Optional[str] = None,
    ) -> TransactionGroup:
        pooler_address = pooler_address or self.client.user_address
        asset_1_amount = amounts_in[self.asset_1]
        asset_2_amount = amounts_in[self.asset_2]
        suggested_params = self.client.algod.suggested_params()

        txn_group = prepare_initial_add_liquidity_transactions(
            validator_app_id=self.validator_app_id,
            asset_1_id=self.asset_1.id,
            asset_2_id=self.asset_2.id,
            pool_token_asset_id=self.pool_token_asset.id,
            asset_1_amount=asset_1_amount.amount,
            asset_2_amount=asset_2_amount.amount,
            sender=pooler_address,
            suggested_params=suggested_params,
        )
        return txn_group

    def prepare_add_liquidity_transactions_from_quote(
        self,
        quote: [
            FlexibleAddLiquidityQuote,
            SingleAssetAddLiquidityQuote,
            InitialAddLiquidityQuote,
        ],
        pooler_address: Optional[str] = None,
    ) -> TransactionGroup:
        if isinstance(quote, FlexibleAddLiquidityQuote):
            return self.prepare_flexible_add_liquidity_transactions(
                amounts_in=quote.amounts_in,
                min_pool_token_asset_amount=quote.min_pool_token_asset_amount_with_slippage,
                pooler_address=pooler_address,
            )
        elif isinstance(quote, SingleAssetAddLiquidityQuote):
            return self.prepare_single_asset_add_liquidity_transactions(
                amount_in=quote.amount_in,
                min_pool_token_asset_amount=quote.min_pool_token_asset_amount_with_slippage,
                pooler_address=pooler_address,
            )
        elif isinstance(quote, InitialAddLiquidityQuote):
            return self.prepare_initial_add_liquidity_transactions(
                amounts_in=quote.amounts_in,
                pooler_address=pooler_address,
            )

        raise Exception(f"Invalid quote type({type(quote)})")

    def fetch_remove_liquidity_quote(
        self, pool_token_asset_in: [AssetAmount, int], slippage: float = 0.05
    ) -> RemoveLiquidityQuote:
        if not self.exists:
            raise BootstrapIsRequired()

        if isinstance(pool_token_asset_in, int):
            pool_token_asset_in = AssetAmount(
                self.pool_token_asset, pool_token_asset_in
            )

        self.refresh()
        (
            asset_1_output_amount,
            asset_2_output_amount,
        ) = calculate_remove_liquidity_output_amounts(
            pool_token_asset_amount=pool_token_asset_in.amount,
            asset_1_reserves=self.asset_1_reserves,
            asset_2_reserves=self.asset_2_reserves,
            issued_pool_tokens=self.issued_pool_tokens,
        )
        quote = RemoveLiquidityQuote(
            amounts_out={
                self.asset_1: AssetAmount(self.asset_1, asset_1_output_amount),
                self.asset_2: AssetAmount(self.asset_2, asset_2_output_amount),
            },
            pool_token_asset_amount=pool_token_asset_in,
            slippage=slippage,
        )
        return quote

    def fetch_single_asset_remove_liquidity_quote(
        self,
        pool_token_asset_in: [AssetAmount, int],
        output_asset: Asset,
        slippage: float = 0.05,
    ) -> SingleAssetRemoveLiquidityQuote:
        if not self.exists:
            raise BootstrapIsRequired()

        if isinstance(pool_token_asset_in, int):
            pool_token_asset_in = AssetAmount(
                self.pool_token_asset, pool_token_asset_in
            )

        self.refresh()
        (
            asset_1_output_amount,
            asset_2_output_amount,
        ) = calculate_remove_liquidity_output_amounts(
            pool_token_asset_amount=pool_token_asset_in.amount,
            asset_1_reserves=self.asset_1_reserves,
            asset_2_reserves=self.asset_2_reserves,
            issued_pool_tokens=self.issued_pool_tokens,
        )

        if output_asset == self.asset_1:
            (
                swap_output_amount,
                total_fee_amount,
                price_impact,
            ) = calculate_fixed_input_swap(
                input_supply=self.asset_2_reserves - asset_2_output_amount,
                output_supply=self.asset_1_reserves - asset_1_output_amount,
                swap_input_amount=asset_2_output_amount,
                total_fee_share=self.total_fee_share,
            )
            internal_swap_quote = InternalSwapQuote(
                amount_in=AssetAmount(self.asset_2, asset_2_output_amount),
                amount_out=AssetAmount(self.asset_1, swap_output_amount),
                swap_fees=AssetAmount(self.asset_2, int(total_fee_amount)),
                price_impact=price_impact,
            )
            quote = SingleAssetRemoveLiquidityQuote(
                amount_out=AssetAmount(
                    self.asset_1, asset_1_output_amount + swap_output_amount
                ),
                pool_token_asset_amount=pool_token_asset_in,
                slippage=slippage,
                internal_swap_quote=internal_swap_quote,
            )
        elif output_asset == self.asset_2:
            (
                swap_output_amount,
                total_fee_amount,
                price_impact,
            ) = calculate_fixed_input_swap(
                input_supply=self.asset_1_reserves - asset_1_output_amount,
                output_supply=self.asset_2_reserves - asset_2_output_amount,
                swap_input_amount=asset_1_output_amount,
                total_fee_share=self.total_fee_share,
            )
            internal_swap_quote = InternalSwapQuote(
                amount_in=AssetAmount(self.asset_1, asset_1_output_amount),
                amount_out=AssetAmount(self.asset_2, swap_output_amount),
                swap_fees=AssetAmount(self.asset_1, int(total_fee_amount)),
                price_impact=price_impact,
            )
            quote = SingleAssetRemoveLiquidityQuote(
                amount_out=AssetAmount(
                    self.asset_2, asset_2_output_amount + swap_output_amount
                ),
                pool_token_asset_amount=pool_token_asset_in,
                slippage=slippage,
                internal_swap_quote=internal_swap_quote,
            )
        else:
            assert False

        return quote

    def prepare_remove_liquidity_transactions(
        self,
        pool_token_asset_amount: [AssetAmount, int],
        amounts_out: dict[Asset, AssetAmount],
        pooler_address: Optional[str] = None,
    ) -> TransactionGroup:
        if isinstance(pool_token_asset_amount, int):
            pool_token_asset_amount = AssetAmount(
                self.pool_token_asset, pool_token_asset_amount
            )

        pooler_address = pooler_address or self.client.user_address
        asset_1_amount = amounts_out[self.asset_1]
        asset_2_amount = amounts_out[self.asset_2]
        suggested_params = self.client.algod.suggested_params()
        txn_group = prepare_remove_liquidity_transactions(
            validator_app_id=self.validator_app_id,
            asset_1_id=self.asset_1.id,
            asset_2_id=self.asset_2.id,
            pool_token_asset_id=self.pool_token_asset.id,
            min_asset_1_amount=asset_1_amount.amount,
            min_asset_2_amount=asset_2_amount.amount,
            pool_token_asset_amount=pool_token_asset_amount.amount,
            sender=pooler_address,
            suggested_params=suggested_params,
        )
        return txn_group

    def prepare_single_asset_remove_liquidity_transactions(
        self,
        pool_token_asset_amount: [AssetAmount, int],
        amount_out: AssetAmount,
        pooler_address: Optional[str] = None,
    ) -> TransactionGroup:
        if isinstance(pool_token_asset_amount, int):
            pool_token_asset_amount = AssetAmount(
                self.pool_token_asset, pool_token_asset_amount
            )

        pooler_address = pooler_address or self.client.user_address
        suggested_params = self.client.algod.suggested_params()
        txn_group = prepare_single_asset_remove_liquidity_transactions(
            validator_app_id=self.validator_app_id,
            asset_1_id=self.asset_1.id,
            asset_2_id=self.asset_2.id,
            pool_token_asset_id=self.pool_token_asset.id,
            output_asset_id=amount_out.asset.id,
            min_output_asset_amount=amount_out.amount,
            pool_token_asset_amount=pool_token_asset_amount.amount,
            sender=pooler_address,
            suggested_params=suggested_params,
        )
        return txn_group

    def prepare_remove_liquidity_transactions_from_quote(
        self,
        quote: [RemoveLiquidityQuote, SingleAssetRemoveLiquidityQuote],
        pooler_address: Optional[str] = None,
    ) -> TransactionGroup:
        pooler_address = pooler_address or self.client.user_address

        if isinstance(quote, SingleAssetRemoveLiquidityQuote):
            return self.prepare_single_asset_remove_liquidity_transactions(
                pool_token_asset_amount=quote.pool_token_asset_amount,
                amount_out=quote.amount_out_with_slippage,
                pooler_address=pooler_address,
            )
        elif isinstance(quote, RemoveLiquidityQuote):
            return self.prepare_remove_liquidity_transactions(
                pool_token_asset_amount=quote.pool_token_asset_amount,
                amounts_out={
                    self.asset_1: quote.amounts_out_with_slippage[self.asset_1],
                    self.asset_2: quote.amounts_out_with_slippage[self.asset_2],
                },
                pooler_address=pooler_address,
            )

        raise NotImplementedError()

    def fetch_fixed_input_swap_quote(
        self, amount_in: AssetAmount, slippage: float = 0.05
    ) -> SwapQuote:
        self.refresh()
        if not self.exists:
            raise BootstrapIsRequired()

        if not self.issued_pool_tokens:
            raise PoolHasNoLiquidity()

        if amount_in.asset == self.asset_1:
            asset_out = self.asset_2
            input_supply = self.asset_1_reserves
            output_supply = self.asset_2_reserves
        elif amount_in.asset == self.asset_1:
            asset_out = self.asset_1
            input_supply = self.asset_2_reserves
            output_supply = self.asset_1_reserves
        else:
            raise False

        swap_output_amount, total_fee_amount, price_impact = calculate_fixed_input_swap(
            input_supply=input_supply,
            output_supply=output_supply,
            swap_input_amount=amount_in.amount,
            total_fee_share=self.total_fee_share,
        )
        amount_out = AssetAmount(asset_out, swap_output_amount)

        quote = SwapQuote(
            swap_type="fixed-input",
            amount_in=amount_in,
            amount_out=amount_out,
            swap_fees=AssetAmount(amount_in.asset, total_fee_amount),
            slippage=slippage,
            price_impact=price_impact,
        )
        return quote

    def fetch_fixed_output_swap_quote(
        self, amount_out: AssetAmount, slippage: float = 0.05
    ) -> SwapQuote:
        self.refresh()
        if not self.exists:
            raise BootstrapIsRequired()

        if not self.issued_pool_tokens:
            raise PoolHasNoLiquidity()

        if amount_out.asset == self.asset_1:
            asset_in = self.asset_2
            input_supply = self.asset_2_reserves
            output_supply = self.asset_1_reserves
        elif amount_out.asset == self.asset_2:
            asset_in = self.asset_1
            input_supply = self.asset_1_reserves
            output_supply = self.asset_2_reserves
        else:
            assert False

        swap_input_amount, total_fee_amount, price_impact = calculate_fixed_output_swap(
            input_supply=input_supply,
            output_supply=output_supply,
            swap_output_amount=amount_out.amount,
            total_fee_share=self.total_fee_share,
        )
        amount_in = AssetAmount(asset_in, swap_input_amount)

        quote = SwapQuote(
            swap_type="fixed-output",
            amount_out=amount_out,
            amount_in=amount_in,
            swap_fees=AssetAmount(amount_in.asset, total_fee_amount),
            slippage=slippage,
            price_impact=price_impact,
        )
        return quote

    def prepare_swap_transactions(
        self,
        amount_in: AssetAmount,
        amount_out: AssetAmount,
        swap_type: [str, bytes],
        swapper_address=None,
    ) -> TransactionGroup:
        swapper_address = swapper_address or self.client.user_address
        suggested_params = self.client.algod.suggested_params()

        txn_group = prepare_swap_transactions(
            validator_app_id=self.validator_app_id,
            asset_1_id=self.asset_1.id,
            asset_2_id=self.asset_2.id,
            asset_in_id=amount_in.asset.id,
            asset_in_amount=amount_in.amount,
            asset_out_amount=amount_out.amount,
            swap_type=swap_type,
            sender=swapper_address,
            suggested_params=suggested_params,
        )
        return txn_group

    def prepare_swap_transactions_from_quote(
        self, quote: SwapQuote, swapper_address=None
    ) -> TransactionGroup:
        return self.prepare_swap_transactions(
            amount_in=quote.amount_in_with_slippage,
            amount_out=quote.amount_out_with_slippage,
            swap_type=quote.swap_type,
            swapper_address=swapper_address,
        )

    def prepare_pool_token_asset_optin_transactions(
        self, user_address: Optional[str] = None
    ) -> TransactionGroup:
        user_address = user_address or self.client.user_address
        suggested_params = self.client.algod.suggested_params()
        txn_group = prepare_asset_optin_transactions(
            asset_id=self.pool_token_asset.id,
            sender=user_address,
            suggested_params=suggested_params,
        )
        return txn_group

    def fetch_pool_position(self, pooler_address: Optional[str] = None) -> dict:
        pooler_address = pooler_address or self.client.user_address
        account_info = self.client.algod.account_info(pooler_address)
        assets = {a["asset-id"]: a for a in account_info["assets"]}
        pool_token_asset_amount = assets.get(self.pool_token_asset.id, {}).get(
            "amount", 0
        )
        quote = self.fetch_remove_liquidity_quote(pool_token_asset_amount)
        return {
            self.asset_1: quote.amounts_out[self.asset_1],
            self.asset_2: quote.amounts_out[self.asset_2],
            self.pool_token_asset: quote.pool_token_asset_amount,
            "share": (pool_token_asset_amount / self.issued_pool_tokens),
        }
