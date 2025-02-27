"""Tests for minting with Plutus using `transaction build-raw`."""
import datetime
import logging
import shutil
from pathlib import Path
from typing import List
from typing import Tuple

import allure
import pytest
from cardano_clusterlib import clusterlib

from cardano_node_tests.tests import common
from cardano_node_tests.tests import plutus_common
from cardano_node_tests.utils import cluster_management
from cardano_node_tests.utils import clusterlib_utils
from cardano_node_tests.utils import configuration
from cardano_node_tests.utils import dbsync_utils
from cardano_node_tests.utils import helpers
from cardano_node_tests.utils import tx_view
from cardano_node_tests.utils.versions import VERSIONS

LOGGER = logging.getLogger(__name__)

# skip all tests if Tx era < alonzo
pytestmark = [
    pytest.mark.skipif(
        VERSIONS.transaction_era < VERSIONS.ALONZO,
        reason="runs only with Alonzo+ TX",
    ),
    pytest.mark.smoke,
]


param_plutus_version = pytest.mark.parametrize(
    "plutus_version",
    (
        "v1",
        pytest.param(
            "v2",
            marks=pytest.mark.skipif(
                VERSIONS.transaction_era < VERSIONS.BABBAGE or configuration.SKIP_PLUTUSV2,
                reason="runs only with Babbage+ TX; needs PlutusV2 cost model",
            ),
        ),
    ),
    ids=("plutus_v1", "plutus_v2"),
)

# approx. fee for Tx size
FEE_MINT_TXSIZE = 400_000


@pytest.fixture
def payment_addrs(
    cluster_manager: cluster_management.ClusterManager,
    cluster: clusterlib.ClusterLib,
) -> List[clusterlib.AddressRecord]:
    """Create new payment address."""
    test_id = common.get_test_id(cluster)
    addrs = clusterlib_utils.create_payment_addr_records(
        *[f"{test_id}_payment_addr_{i}" for i in range(2)],
        cluster_obj=cluster,
    )

    # fund source address
    clusterlib_utils.fund_from_faucet(
        addrs[0],
        cluster_obj=cluster,
        faucet_data=cluster_manager.cache.addrs_data["user1"],
        amount=3_000_000_000,
    )

    return addrs


def _check_pretty_utxo(
    cluster_obj: clusterlib.ClusterLib, tx_raw_output: clusterlib.TxRawOutput
) -> str:
    """Check that pretty printed `query utxo` output looks as expected."""
    err = ""
    txid = cluster_obj.get_txid(tx_body_file=tx_raw_output.out_file)

    utxo_out = (
        cluster_obj.cli(
            [
                "query",
                "utxo",
                "--tx-in",
                f"{txid}#0",
                *cluster_obj.magic_args,
            ]
        )
        .stdout.decode("utf-8")
        .split()
    )

    expected_out = [
        "TxHash",
        "TxIx",
        "Amount",
        "--------------------------------------------------------------------------------------",
        txid,
        "0",
        str(tx_raw_output.txouts[0].amount),
        tx_raw_output.txouts[0].coin,
        "+",
        str(tx_raw_output.txouts[1].amount),
        tx_raw_output.txouts[1].coin,
        "+",
        str(tx_raw_output.txouts[2].amount),
        tx_raw_output.txouts[2].coin,
        "+",
        "TxOutDatumNone",
    ]

    if utxo_out != expected_out:
        err = f"Pretty UTxO output doesn't match expected output:\n{utxo_out}\nvs\n{expected_out}"

    return err


def _fund_issuer(
    cluster_obj: clusterlib.ClusterLib,
    temp_template: str,
    payment_addr: clusterlib.AddressRecord,
    issuer_addr: clusterlib.AddressRecord,
    minting_cost: plutus_common.ScriptCost,
    amount: int,
    fee_txsize: int = FEE_MINT_TXSIZE,
    collateral_utxo_num: int = 1,
) -> Tuple[List[clusterlib.UTXOData], List[clusterlib.UTXOData], clusterlib.TxRawOutput]:
    """Fund the token issuer."""
    single_collateral_amount = minting_cost.collateral // collateral_utxo_num
    collateral_amounts = [single_collateral_amount for c in range(collateral_utxo_num - 1)]
    collateral_subtotal = sum(collateral_amounts)
    collateral_amounts.append(minting_cost.collateral - collateral_subtotal)

    issuer_init_balance = cluster_obj.get_address_balance(issuer_addr.address)

    tx_files = clusterlib.TxFiles(
        signing_key_files=[payment_addr.skey_file],
    )
    txouts = [
        clusterlib.TxOut(
            address=issuer_addr.address,
            amount=amount + minting_cost.fee + fee_txsize,
        ),
        *[clusterlib.TxOut(address=issuer_addr.address, amount=a) for a in collateral_amounts],
    ]

    tx_raw_output = cluster_obj.send_tx(
        src_address=payment_addr.address,
        tx_name=f"{temp_template}_step1",
        txouts=txouts,
        tx_files=tx_files,
        # TODO: workaround for https://github.com/input-output-hk/cardano-node/issues/1892
        witness_count_add=2,
        # don't join 'change' and 'collateral' txouts, we need separate UTxOs
        join_txouts=False,
    )

    issuer_balance = cluster_obj.get_address_balance(issuer_addr.address)
    assert (
        issuer_balance
        == issuer_init_balance + amount + minting_cost.fee + fee_txsize + minting_cost.collateral
    ), f"Incorrect balance for token issuer address `{issuer_addr.address}`"

    txid = cluster_obj.get_txid(tx_body_file=tx_raw_output.out_file)
    mint_utxos = cluster_obj.get_utxo(txin=f"{txid}#0")
    collateral_utxos = [
        clusterlib.UTXOData(utxo_hash=txid, utxo_ix=idx, amount=a, address=issuer_addr.address)
        for idx, a in enumerate(collateral_amounts, start=1)
    ]

    return mint_utxos, collateral_utxos, tx_raw_output


class TestMinting:
    """Tests for minting using Plutus smart contracts."""

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.dbsync
    @pytest.mark.testnets
    @param_plutus_version
    def test_minting_two_tokens(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
        plutus_version: str,
    ):
        """Test minting two tokens with a single Plutus script.

        * fund the token issuer and create a UTxO for collateral
        * check that the expected amount was transferred to token issuer's address
        * mint the tokens using a Plutus script
        * check that the tokens were minted and collateral UTxO was not spent
        * (optional) check transactions in db-sync
        """
        # pylint: disable=too-many-locals
        temp_template = f"{common.get_test_id(cluster)}_{plutus_version}"

        payment_addr = payment_addrs[0]
        issuer_addr = payment_addrs[1]

        lovelace_amount = 2_000_000
        token_amount = 5
        fee_txsize = 600_000

        plutus_v_record = plutus_common.MINTING_PLUTUS[plutus_version]

        minting_cost = plutus_common.compute_cost(
            execution_cost=plutus_v_record.execution_cost,
            protocol_params=cluster.get_protocol_params(),
        )

        # Step 1: fund the token issuer

        mint_utxos, collateral_utxos, tx_raw_output_step1 = _fund_issuer(
            cluster_obj=cluster,
            temp_template=temp_template,
            payment_addr=payment_addr,
            issuer_addr=issuer_addr,
            minting_cost=minting_cost,
            amount=lovelace_amount,
            fee_txsize=fee_txsize,
            collateral_utxo_num=2,
        )

        issuer_fund_balance = cluster.get_address_balance(issuer_addr.address)

        # Step 2: mint the "qacoin"

        policyid = cluster.get_policyid(plutus_v_record.script_file)
        asset_name_a = f"qacoina{clusterlib.get_rand_str(4)}".encode("utf-8").hex()
        token_a = f"{policyid}.{asset_name_a}"
        asset_name_b = f"qacoinb{clusterlib.get_rand_str(4)}".encode("utf-8").hex()
        token_b = f"{policyid}.{asset_name_b}"
        mint_txouts = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token_a),
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token_b),
        ]

        plutus_mint_data = [
            clusterlib.Mint(
                txouts=mint_txouts,
                script_file=plutus_v_record.script_file,
                collaterals=collateral_utxos,
                execution_units=(
                    plutus_v_record.execution_cost.per_time,
                    plutus_v_record.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
            )
        ]

        tx_files_step2 = clusterlib.TxFiles(
            signing_key_files=[issuer_addr.skey_file],
        )
        txouts_step2 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount),
            *mint_txouts,
        ]
        tx_raw_output_step2 = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data,
            tx_files=tx_files_step2,
            fee=minting_cost.fee + fee_txsize,
            # ttl is optional in this test
            invalid_hereafter=cluster.get_slot_no() + 200,
        )
        tx_signed_step2 = cluster.sign_tx(
            tx_body_file=tx_raw_output_step2.out_file,
            signing_key_files=tx_files_step2.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )
        cluster.submit_tx(tx_file=tx_signed_step2, txins=mint_utxos)

        assert (
            cluster.get_address_balance(issuer_addr.address)
            == issuer_fund_balance - tx_raw_output_step2.fee
        ), f"Incorrect balance for token issuer address `{issuer_addr.address}`"

        token_utxo_a = cluster.get_utxo(address=issuer_addr.address, coins=[token_a])
        assert (
            token_utxo_a and token_utxo_a[0].amount == token_amount
        ), "The 'token a' was not minted"

        token_utxo_b = cluster.get_utxo(address=issuer_addr.address, coins=[token_b])
        assert (
            token_utxo_b and token_utxo_b[0].amount == token_amount
        ), "The 'token b' was not minted"

        # check tx view
        tx_view.check_tx_view(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)

        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step1)
        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)

        utxo_err = _check_pretty_utxo(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)
        if utxo_err:
            pytest.fail(utxo_err)

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.dbsync
    @pytest.mark.testnets
    @pytest.mark.parametrize(
        "key",
        (
            "normal",
            "extended",
        ),
    )
    def test_witness_redeemer(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
        key: str,
    ):
        """Test minting a token with a Plutus script.

        * fund the token issuer and create a UTxO for collateral
        * check that the expected amount was transferred to token issuer's address
        * mint the token using a Plutus script with required signer
        * check that the token was minted and collateral UTxO was not spent
        * (optional) check transactions in db-sync
        """
        # pylint: disable=too-many-locals
        temp_template = f"{common.get_test_id(cluster)}_{key}"

        payment_addr = payment_addrs[0]
        issuer_addr = payment_addrs[1]

        lovelace_amount = 2_000_000
        token_amount = 5

        minting_cost = plutus_common.compute_cost(
            execution_cost=plutus_common.MINTING_WITNESS_REDEEMER_COST,
            protocol_params=cluster.get_protocol_params(),
        )

        if key == "normal":
            redeemer_file = plutus_common.DATUM_WITNESS_GOLDEN_NORMAL
            signing_key_golden = plutus_common.SIGNING_KEY_GOLDEN
        else:
            redeemer_file = plutus_common.DATUM_WITNESS_GOLDEN_EXTENDED
            signing_key_golden = plutus_common.SIGNING_KEY_GOLDEN_EXTENDED

        # Step 1: fund the token issuer

        mint_utxos, collateral_utxos, tx_raw_output_step1 = _fund_issuer(
            cluster_obj=cluster,
            temp_template=temp_template,
            payment_addr=payment_addr,
            issuer_addr=issuer_addr,
            minting_cost=minting_cost,
            amount=lovelace_amount,
        )

        issuer_fund_balance = cluster.get_address_balance(issuer_addr.address)

        # Step 2: mint the "qacoin"

        policyid = cluster.get_policyid(plutus_common.MINTING_WITNESS_REDEEMER_PLUTUS_V1)
        asset_name = f"qacoin{clusterlib.get_rand_str(4)}".encode("utf-8").hex()
        token = f"{policyid}.{asset_name}"
        mint_txouts = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token)
        ]

        plutus_mint_data = [
            clusterlib.Mint(
                txouts=mint_txouts,
                script_file=plutus_common.MINTING_WITNESS_REDEEMER_PLUTUS_V1,
                collaterals=collateral_utxos,
                execution_units=(
                    plutus_common.MINTING_WITNESS_REDEEMER_COST.per_time,
                    plutus_common.MINTING_WITNESS_REDEEMER_COST.per_space,
                ),
                redeemer_file=redeemer_file,
            )
        ]

        tx_files_step2 = clusterlib.TxFiles(
            signing_key_files=[issuer_addr.skey_file, signing_key_golden],
        )
        txouts_step2 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount),
            *mint_txouts,
        ]
        tx_raw_output_step2 = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data,
            tx_files=tx_files_step2,
            fee=minting_cost.fee + FEE_MINT_TXSIZE,
            required_signers=[signing_key_golden],
        )
        # sign incrementally (just to check that it works)
        tx_signed_step2 = cluster.sign_tx(
            tx_body_file=tx_raw_output_step2.out_file,
            signing_key_files=[issuer_addr.skey_file],
            tx_name=f"{temp_template}_step2_sign0",
        )
        tx_signed_step2_inc = cluster.sign_tx(
            tx_file=tx_signed_step2,
            signing_key_files=[signing_key_golden],
            tx_name=f"{temp_template}_step2_sign1",
        )
        cluster.submit_tx(tx_file=tx_signed_step2_inc, txins=mint_utxos)

        assert (
            cluster.get_address_balance(issuer_addr.address)
            == issuer_fund_balance - tx_raw_output_step2.fee
        ), f"Incorrect balance for token issuer address `{issuer_addr.address}`"

        token_utxo = cluster.get_utxo(address=issuer_addr.address, coins=[token])
        assert token_utxo and token_utxo[0].amount == token_amount, "The token was not minted"

        # check tx_view
        tx_view.check_tx_view(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)

        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step1)
        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.dbsync
    @pytest.mark.testnets
    def test_time_range_minting(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
    ):
        """Test minting a token with a time constraints Plutus script.

        * fund the token issuer and create a UTxO for collateral
        * check that the expected amount was transferred to token issuer's address
        * mint the token using a Plutus script
        * check that the token was minted and collateral UTxO was not spent
        * (optional) check transactions in db-sync
        """
        # pylint: disable=too-many-locals
        temp_template = common.get_test_id(cluster)
        payment_addr = payment_addrs[0]
        issuer_addr = payment_addrs[1]

        lovelace_amount = 2_000_000
        token_amount = 5

        minting_cost = plutus_common.compute_cost(
            execution_cost=plutus_common.MINTING_TIME_RANGE_COST,
            protocol_params=cluster.get_protocol_params(),
        )

        # Step 1: fund the token issuer

        mint_utxos, collateral_utxos, tx_raw_output_step1 = _fund_issuer(
            cluster_obj=cluster,
            temp_template=temp_template,
            payment_addr=payment_addr,
            issuer_addr=issuer_addr,
            minting_cost=minting_cost,
            amount=lovelace_amount,
        )

        issuer_fund_balance = cluster.get_address_balance(issuer_addr.address)

        # Step 2: mint the "qacoin"

        slot_step2 = cluster.get_slot_no()
        slots_offset = 200
        timestamp_offset_ms = int(slots_offset * cluster.slot_length + 5) * 1_000

        protocol_version = cluster.get_protocol_params()["protocolVersion"]["major"]
        if protocol_version > 5:
            # POSIX timestamp + offset
            redeemer_value = int(datetime.datetime.now().timestamp() * 1_000) + timestamp_offset_ms
        else:
            # BUG: https://github.com/input-output-hk/cardano-node/issues/3090
            redeemer_value = 1_000_000_000_000

        policyid = cluster.get_policyid(plutus_common.MINTING_TIME_RANGE_PLUTUS_V1)
        asset_name = f"qacoin{clusterlib.get_rand_str(4)}".encode("utf-8").hex()
        token = f"{policyid}.{asset_name}"
        mint_txouts = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token)
        ]

        plutus_mint_data = [
            clusterlib.Mint(
                txouts=mint_txouts,
                script_file=plutus_common.MINTING_TIME_RANGE_PLUTUS_V1,
                collaterals=collateral_utxos,
                execution_units=(
                    plutus_common.MINTING_TIME_RANGE_COST.per_time,
                    plutus_common.MINTING_TIME_RANGE_COST.per_space,
                ),
                redeemer_value=str(redeemer_value),
            )
        ]

        tx_files_step2 = clusterlib.TxFiles(
            signing_key_files=[issuer_addr.skey_file],
        )
        txouts_step2 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount),
            *mint_txouts,
        ]
        tx_raw_output_step2 = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data,
            tx_files=tx_files_step2,
            fee=minting_cost.fee + FEE_MINT_TXSIZE,
            invalid_before=slot_step2 - slots_offset,
            invalid_hereafter=slot_step2 + slots_offset,
        )
        tx_signed_step2 = cluster.sign_tx(
            tx_body_file=tx_raw_output_step2.out_file,
            signing_key_files=tx_files_step2.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )
        cluster.submit_tx(tx_file=tx_signed_step2, txins=mint_utxos)

        assert (
            cluster.get_address_balance(issuer_addr.address)
            == issuer_fund_balance - tx_raw_output_step2.fee
        ), f"Incorrect balance for token issuer address `{issuer_addr.address}`"

        token_utxo = cluster.get_utxo(address=issuer_addr.address, coins=[token])
        assert token_utxo and token_utxo[0].amount == token_amount, "The token was not minted"

        # check tx_view
        tx_view.check_tx_view(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)

        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step1)
        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.dbsync
    @pytest.mark.testnets
    @pytest.mark.parametrize(
        "plutus_version",
        (
            "plutus_v1",
            pytest.param(
                "mix_v2_v1",
                marks=pytest.mark.skipif(
                    VERSIONS.transaction_era < VERSIONS.BABBAGE or configuration.SKIP_PLUTUSV2,
                    reason="runs only with Babbage+ TX; needs PlutusV2 cost model",
                ),
            ),
        ),
    )
    def test_two_scripts_minting(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
        plutus_version: str,
    ):
        """Test minting two tokens with two different Plutus scripts.

        * fund the token issuer and create a UTxO for collaterals
        * check that the expected amount was transferred to token issuer's address
        * mint the tokens using two different Plutus scripts
        * check that the tokens were minted and collateral UTxOs were not spent
        * check transaction view output
        * (optional) check transactions in db-sync
        """
        # pylint: disable=too-many-locals,too-many-statements
        temp_template = f"{common.get_test_id(cluster)}_{plutus_version}"

        payment_addr = payment_addrs[0]
        issuer_addr = payment_addrs[1]

        lovelace_amount = 2_000_000
        token_amount = 5

        script_file1_v1 = plutus_common.MINTING_PLUTUS_V1
        script_file1_v2 = plutus_common.MINTING_PLUTUS_V2

        # this is higher than `plutus_common.MINTING*_COST`, because the script context has changed
        # to include more stuff
        if configuration.ALONZO_COST_MODEL or VERSIONS.cluster_era == VERSIONS.ALONZO:
            minting_cost1_v1 = plutus_common.ExecutionCost(
                per_time=408_545_501, per_space=1_126_016, fixed_cost=94_428
            )
            minting_cost2_v1 = plutus_common.ExecutionCost(
                per_time=427_707_230, per_space=1_188_952, fixed_cost=99_441
            )
        else:
            minting_cost1_v1 = plutus_common.ExecutionCost(
                per_time=297_744_405, per_space=1_126_016, fixed_cost=86_439
            )
            minting_cost2_v1 = plutus_common.ExecutionCost(
                per_time=312_830_204, per_space=1_188_952, fixed_cost=91_158
            )

        minting_cost1_v2 = plutus_common.ExecutionCost(
            per_time=185_595_199, per_space=595_446, fixed_cost=47_739
        )

        if plutus_version == "plutus_v1":
            script_file1 = script_file1_v1
            execution_cost1 = minting_cost1_v1
        elif plutus_version == "mix_v2_v1":
            script_file1 = script_file1_v2
            execution_cost1 = minting_cost1_v2
        else:
            raise AssertionError("Unknown test variant.")

        script_file2 = plutus_common.MINTING_TIME_RANGE_PLUTUS_V1

        protocol_params = cluster.get_protocol_params()
        minting_cost1 = plutus_common.compute_cost(
            execution_cost=execution_cost1, protocol_params=protocol_params
        )
        minting_cost2 = plutus_common.compute_cost(
            execution_cost=minting_cost2_v1, protocol_params=protocol_params
        )

        fee_step2_total = minting_cost1.fee + minting_cost2.fee + FEE_MINT_TXSIZE

        issuer_init_balance = cluster.get_address_balance(issuer_addr.address)

        # Step 1: fund the token issuer

        tx_files_step1 = clusterlib.TxFiles(
            signing_key_files=[payment_addr.skey_file],
        )
        txouts_step1 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount + fee_step2_total),
            # for collaterals
            clusterlib.TxOut(address=issuer_addr.address, amount=minting_cost1.collateral),
            clusterlib.TxOut(address=issuer_addr.address, amount=minting_cost2.collateral),
        ]

        tx_raw_output_step1 = cluster.send_tx(
            src_address=payment_addr.address,
            tx_name=f"{temp_template}_step1",
            txouts=txouts_step1,
            tx_files=tx_files_step1,
            # TODO: workaround for https://github.com/input-output-hk/cardano-node/issues/1892
            witness_count_add=2,
            # don't join 'change' and 'collateral' txouts, we need separate UTxOs
            join_txouts=False,
        )

        issuer_step1_balance = cluster.get_address_balance(issuer_addr.address)
        assert (
            issuer_step1_balance
            == issuer_init_balance
            + lovelace_amount
            + fee_step2_total
            + minting_cost1.collateral
            + minting_cost2.collateral
        ), f"Incorrect balance for token issuer address `{issuer_addr.address}`"

        # Step 2: mint the "qacoins"

        txid_step1 = cluster.get_txid(tx_body_file=tx_raw_output_step1.out_file)
        mint_utxos = cluster.get_utxo(txin=f"{txid_step1}#0")
        collateral_utxo1 = cluster.get_utxo(txin=f"{txid_step1}#1")
        collateral_utxo2 = cluster.get_utxo(txin=f"{txid_step1}#2")

        slot_step2 = cluster.get_slot_no()

        # "anyone can mint" qacoin
        policyid1 = cluster.get_policyid(script_file1)
        asset_name1 = f"qacoina{clusterlib.get_rand_str(4)}".encode("utf-8").hex()
        token1 = f"{policyid1}.{asset_name1}"
        mint_txouts1 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token1)
        ]

        # "timerange" qacoin
        slots_offset = 200
        timestamp_offset_ms = int(slots_offset * cluster.slot_length + 5) * 1_000

        protocol_version = cluster.get_protocol_params()["protocolVersion"]["major"]
        if protocol_version > 5:
            # POSIX timestamp + offset
            redeemer_value_timerange = (
                int(datetime.datetime.now().timestamp() * 1_000) + timestamp_offset_ms
            )
        else:
            # BUG: https://github.com/input-output-hk/cardano-node/issues/3090
            redeemer_value_timerange = 1_000_000_000_000

        policyid2 = cluster.get_policyid(script_file2)
        asset_name2 = f"qacoint{clusterlib.get_rand_str(4)}".encode("utf-8").hex()
        token2 = f"{policyid2}.{asset_name2}"
        mint_txouts2 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token2)
        ]

        # mint the tokens
        plutus_mint_data = [
            clusterlib.Mint(
                txouts=mint_txouts1,
                script_file=script_file1,
                collaterals=collateral_utxo1,
                execution_units=(
                    execution_cost1.per_time,
                    execution_cost1.per_space,
                ),
                redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
            ),
            clusterlib.Mint(
                txouts=mint_txouts2,
                script_file=script_file2,
                collaterals=collateral_utxo2,
                execution_units=(
                    minting_cost2_v1.per_time,
                    minting_cost2_v1.per_space,
                ),
                redeemer_value=str(redeemer_value_timerange),
            ),
        ]

        tx_files_step2 = clusterlib.TxFiles(
            signing_key_files=[issuer_addr.skey_file],
        )
        txouts_step2 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount),
            *mint_txouts1,
            *mint_txouts2,
        ]
        tx_raw_output_step2 = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data,
            tx_files=tx_files_step2,
            fee=fee_step2_total,
            invalid_before=slot_step2 - slots_offset,
            invalid_hereafter=slot_step2 + slots_offset,
        )
        tx_signed_step2 = cluster.sign_tx(
            tx_body_file=tx_raw_output_step2.out_file,
            signing_key_files=tx_files_step2.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )
        cluster.submit_tx(tx_file=tx_signed_step2, txins=mint_utxos)

        assert (
            cluster.get_address_balance(issuer_addr.address)
            == issuer_init_balance
            + minting_cost1.collateral
            + minting_cost2.collateral
            + lovelace_amount
        ), f"Incorrect balance for token issuer address `{issuer_addr.address}`"

        token_utxo1 = cluster.get_utxo(address=issuer_addr.address, coins=[token1])
        assert (
            token_utxo1 and token_utxo1[0].amount == token_amount
        ), "The 'anyone' token was not minted"

        token_utxo2 = cluster.get_utxo(address=issuer_addr.address, coins=[token2])
        assert (
            token_utxo2 and token_utxo2[0].amount == token_amount
        ), "The 'timerange' token was not minted"

        # check tx_view
        tx_view.check_tx_view(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)

        # check transactions in db-sync
        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step1)
        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.dbsync
    @pytest.mark.testnets
    def test_minting_policy_executed_once1(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
    ):
        """Test that minting policy is executed only once even when the same policy is used twice.

        Test by minting two tokens while using the same Plutus script twice
        with two different redeemers.

        The Plutus script used in this test takes the expected token name as
        redeemer. Even though the redeemer used for minting the first token
        doesn't match the token name, the token get's minted anyway. That's
        because only the last redeemer is used and all the other scripts with
        identical minting policy (and corresponding redeemers) are ignored. So
        it only matters that the last redeemer matches the last token name.

        * fund the token issuer and create a UTxO for collateral - funds for fees and collateral
          are sufficient for just single minting script
        * check that the expected amount was transferred to token issuer's address
        * mint the tokens using two identical Plutus scripts and two redeemers, where the first
          redeemer value is invalid
        * check that the tokens were minted and collateral UTxOs were not spent, i.e. the first
          script and its redeemer were ignored
        * check transaction view output
        * (optional) check transactions in db-sync
        """
        # pylint: disable=too-many-locals,too-many-statements
        temp_template = common.get_test_id(cluster)
        payment_addr = payment_addrs[0]
        issuer_addr = payment_addrs[1]

        lovelace_amount = 2_000_000
        token_amount = 5

        minting_cost = plutus_common.compute_cost(
            execution_cost=plutus_common.MINTING_TOKENNAME_COST,
            protocol_params=cluster.get_protocol_params(),
        )

        # Step 1: fund the token issuer

        mint_utxos, collateral_utxos, tx_raw_output_step1 = _fund_issuer(
            cluster_obj=cluster,
            temp_template=temp_template,
            payment_addr=payment_addr,
            issuer_addr=issuer_addr,
            minting_cost=minting_cost,
            amount=lovelace_amount,
        )

        issuer_init_balance = cluster.get_address_balance(issuer_addr.address)

        # Step 2: mint the "qacoins"

        policyid_tokenname = cluster.get_policyid(plutus_common.MINTING_TOKENNAME_PLUTUS_V1)

        # qacoinA
        asset_name_a_dec = f"qacoinA{clusterlib.get_rand_str(4)}"
        asset_name_a = asset_name_a_dec.encode("utf-8").hex()
        token_a = f"{policyid_tokenname}.{asset_name_a}"
        mint_txouts_a = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token_a)
        ]

        # qacoinB
        asset_name_b_dec = f"qacoinB{clusterlib.get_rand_str(4)}"
        asset_name_b = asset_name_b_dec.encode("utf-8").hex()
        token_b = f"{policyid_tokenname}.{asset_name_b}"
        mint_txouts_b = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token_b)
        ]

        # mint the tokens
        plutus_mint_data = [
            # First redeemer and first script are ignored when there are
            # multiple scripts for the same minting policy. Even though we
            # specified execution units for the script, these will not be used.
            # That's why we were able to use the costs for just single script,
            # even when we passed it twice.
            clusterlib.Mint(
                txouts=mint_txouts_a,
                script_file=plutus_common.MINTING_TOKENNAME_PLUTUS_V1,
                # execution units are too low, but it doesn't matter as they get ignored anyway
                execution_units=(1, 1),
                redeemer_value='"ignored_value"',
            ),
            clusterlib.Mint(
                txouts=mint_txouts_b,
                script_file=plutus_common.MINTING_TOKENNAME_PLUTUS_V1,
                collaterals=collateral_utxos,
                execution_units=(
                    plutus_common.MINTING_TOKENNAME_COST.per_time,
                    plutus_common.MINTING_TOKENNAME_COST.per_space,
                ),
                redeemer_value=f'"{asset_name_b_dec}"',
            ),
        ]

        tx_files_step2 = clusterlib.TxFiles(
            signing_key_files=[issuer_addr.skey_file],
        )
        txouts_step2 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount),
            *mint_txouts_a,
            *mint_txouts_b,
        ]
        tx_raw_output_step2 = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data,
            tx_files=tx_files_step2,
            fee=minting_cost.fee + FEE_MINT_TXSIZE,
        )
        tx_signed_step2 = cluster.sign_tx(
            tx_body_file=tx_raw_output_step2.out_file,
            signing_key_files=tx_files_step2.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )
        cluster.submit_tx(tx_file=tx_signed_step2, txins=mint_utxos)

        assert (
            cluster.get_address_balance(issuer_addr.address)
            == issuer_init_balance - tx_raw_output_step2.fee
        ), f"Incorrect balance for token issuer address `{issuer_addr.address}`"

        token_utxo_a = cluster.get_utxo(address=issuer_addr.address, coins=[token_a])
        assert (
            token_utxo_a and token_utxo_a[0].amount == token_amount
        ), f"The '{asset_name_a_dec}' token was not minted"

        token_utxo_b = cluster.get_utxo(address=issuer_addr.address, coins=[token_b])
        assert (
            token_utxo_b and token_utxo_b[0].amount == token_amount
        ), f"The '{asset_name_b_dec}' token was not minted"

        # check tx_view
        tx_view.check_tx_view(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)

        # check transactions in db-sync
        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step1)
        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.dbsync
    @pytest.mark.testnets
    def test_minting_policy_executed_once2(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
    ):
        """Test that minting policy is executed only once even when the same policy is used twice.

        Test minting two tokens while using one Plutus script and one redeemer.

        The Plutus script used in this test takes the expected token name as
        redeemer. Even though the redeemer doesn't match name of the first
        token, the token get's minted anyway. That's because it is only checked
        that the last token name matches the redeemer, and redeemer for the
        first token is not needed.

        * fund the token issuer and create a UTxO for collateral
        * check that the expected amount was transferred to token issuer's address
        * mint the tokens using a redeemer value that doesn't match the name of the first token
        * check that the tokens were minted and collateral UTxOs were not spent, i.e. redeemer for
          the first token was not needed
        * check transaction view output
        * (optional) check transactions in db-sync
        """
        # pylint: disable=too-many-locals
        temp_template = common.get_test_id(cluster)
        payment_addr = payment_addrs[0]
        issuer_addr = payment_addrs[1]

        lovelace_amount = 2_000_000
        token_amount = 5

        minting_cost = plutus_common.compute_cost(
            execution_cost=plutus_common.MINTING_TOKENNAME_COST,
            protocol_params=cluster.get_protocol_params(),
        )

        # Step 1: fund the token issuer

        mint_utxos, collateral_utxos, tx_raw_output_step1 = _fund_issuer(
            cluster_obj=cluster,
            temp_template=temp_template,
            payment_addr=payment_addr,
            issuer_addr=issuer_addr,
            minting_cost=minting_cost,
            amount=lovelace_amount,
            collateral_utxo_num=2,
        )

        issuer_fund_balance = cluster.get_address_balance(issuer_addr.address)

        # Step 2: mint the "qacoin"

        policyid = cluster.get_policyid(plutus_common.MINTING_TOKENNAME_PLUTUS_V1)

        # qacoinA
        asset_name_a_dec = f"qacoinA{clusterlib.get_rand_str(4)}"
        asset_name_a = asset_name_a_dec.encode("utf-8").hex()
        token_a = f"{policyid}.{asset_name_a}"

        # qacoinB
        asset_name_b_dec = f"qacoinB{clusterlib.get_rand_str(4)}"
        asset_name_b = asset_name_b_dec.encode("utf-8").hex()
        token_b = f"{policyid}.{asset_name_b}"

        mint_txouts = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token_a),
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token_b),
        ]

        plutus_mint_data = [
            clusterlib.Mint(
                txouts=mint_txouts,
                script_file=plutus_common.MINTING_TOKENNAME_PLUTUS_V1,
                collaterals=collateral_utxos,
                execution_units=(
                    plutus_common.MINTING_TOKENNAME_COST.per_time,
                    plutus_common.MINTING_TOKENNAME_COST.per_space,
                ),
                # both tokens will be minted even though the redeemer value
                # matches the name of only the second one
                redeemer_value=f'"{asset_name_b_dec}"',
            )
        ]

        tx_files_step2 = clusterlib.TxFiles(
            signing_key_files=[issuer_addr.skey_file],
        )
        txouts_step2 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount),
            *mint_txouts,
        ]
        tx_raw_output_step2 = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data,
            tx_files=tx_files_step2,
            fee=minting_cost.fee + FEE_MINT_TXSIZE,
        )
        tx_signed_step2 = cluster.sign_tx(
            tx_body_file=tx_raw_output_step2.out_file,
            signing_key_files=tx_files_step2.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )
        cluster.submit_tx(tx_file=tx_signed_step2, txins=mint_utxos)

        assert (
            cluster.get_address_balance(issuer_addr.address)
            == issuer_fund_balance - tx_raw_output_step2.fee
        ), f"Incorrect balance for token issuer address `{issuer_addr.address}`"

        token_utxo_a = cluster.get_utxo(address=issuer_addr.address, coins=[token_a])
        assert (
            token_utxo_a and token_utxo_a[0].amount == token_amount
        ), f"The '{asset_name_a_dec}' was not minted"

        token_utxo_b = cluster.get_utxo(address=issuer_addr.address, coins=[token_b])
        assert (
            token_utxo_b and token_utxo_b[0].amount == token_amount
        ), f"The '{asset_name_b_dec}' was not minted"

        # check tx view
        tx_view.check_tx_view(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)

        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step1)
        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.skipif(
        not shutil.which("create-script-context"),
        reason="cannot find `create-script-context` on the PATH",
    )
    @pytest.mark.dbsync
    @pytest.mark.testnets
    def test_minting_context_equivalance(
        self, cluster: clusterlib.ClusterLib, payment_addrs: List[clusterlib.AddressRecord]
    ):
        """Test context equivalence while minting a token.

        * fund the token issuer and create a UTxO for collateral
        * check that the expected amount was transferred to token issuer's address
        * generate a dummy redeemer and a dummy Tx
        * derive the correct redeemer from the dummy Tx
        * mint the token using the derived redeemer
        * check that the token was minted and collateral UTxO was not spent
        * (optional) check transactions in db-sync
        """
        # pylint: disable=too-many-locals,too-many-statements
        temp_template = common.get_test_id(cluster)
        payment_addr = payment_addrs[0]
        issuer_addr = payment_addrs[1]

        lovelace_amount = 2_000_000
        token_amount = 5

        minting_cost = plutus_common.compute_cost(
            execution_cost=plutus_common.MINTING_CONTEXT_EQUIVALENCE_COST,
            protocol_params=cluster.get_protocol_params(),
        )

        # Step 1: fund the token issuer

        mint_utxos, collateral_utxos, tx_raw_output_step1 = _fund_issuer(
            cluster_obj=cluster,
            temp_template=temp_template,
            payment_addr=payment_addr,
            issuer_addr=issuer_addr,
            minting_cost=minting_cost,
            amount=lovelace_amount,
        )

        issuer_fund_balance = cluster.get_address_balance(issuer_addr.address)

        # Step 2: mint the "qacoin"

        invalid_hereafter = cluster.get_slot_no() + 1_000

        policyid = cluster.get_policyid(plutus_common.MINTING_CONTEXT_EQUIVALENCE_PLUTUS_V1)
        asset_name = f"qacoin{clusterlib.get_rand_str(4)}".encode("utf-8").hex()
        token = f"{policyid}.{asset_name}"
        mint_txouts = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token)
        ]

        tx_files_step2 = clusterlib.TxFiles(
            signing_key_files=[issuer_addr.skey_file, plutus_common.SIGNING_KEY_GOLDEN],
        )
        txouts_step2 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount),
            *mint_txouts,
        ]

        # generate a dummy redeemer in order to create a txbody from which
        # we can generate a tx and then derive the correct redeemer
        redeemer_file_dummy = Path(f"{temp_template}_dummy_script_context.redeemer")
        clusterlib_utils.create_script_context(
            cluster_obj=cluster, redeemer_file=redeemer_file_dummy
        )

        plutus_mint_data_dummy = [
            clusterlib.Mint(
                txouts=mint_txouts,
                script_file=plutus_common.MINTING_CONTEXT_EQUIVALENCE_PLUTUS_V1,
                collaterals=collateral_utxos,
                execution_units=(
                    plutus_common.MINTING_CONTEXT_EQUIVALENCE_COST.per_time,
                    plutus_common.MINTING_CONTEXT_EQUIVALENCE_COST.per_space,
                ),
                redeemer_file=redeemer_file_dummy,
            )
        ]

        tx_output_dummy = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_dummy_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data_dummy,
            tx_files=tx_files_step2,
            fee=minting_cost.fee + FEE_MINT_TXSIZE,
            required_signers=[plutus_common.SIGNING_KEY_GOLDEN],
            invalid_before=1,
            invalid_hereafter=invalid_hereafter,
            script_valid=False,
        )
        assert tx_output_dummy

        tx_file_dummy = cluster.sign_tx(
            tx_body_file=tx_output_dummy.out_file,
            signing_key_files=tx_files_step2.signing_key_files,
            tx_name=f"{temp_template}_dummy",
        )

        # generate the "real" redeemer
        redeemer_file = Path(f"{temp_template}_script_context.redeemer")

        try:
            clusterlib_utils.create_script_context(
                cluster_obj=cluster, redeemer_file=redeemer_file, tx_file=tx_file_dummy
            )
        except AssertionError as err:
            err_msg = str(err)
            if "DeserialiseFailure" in err_msg:
                pytest.xfail("DeserialiseFailure: see issue #944")
            if "TextEnvelopeTypeError" in err_msg and cluster.use_cddl:  # noqa: SIM106
                pytest.xfail(
                    "TextEnvelopeTypeError: `create-script-context` doesn't work with CDDL format"
                )
            else:
                raise

        plutus_mint_data = [plutus_mint_data_dummy[0]._replace(redeemer_file=redeemer_file)]

        tx_raw_output_step2 = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data,
            tx_files=tx_files_step2,
            fee=minting_cost.fee + FEE_MINT_TXSIZE,
            required_signers=[plutus_common.SIGNING_KEY_GOLDEN],
            invalid_before=1,
            invalid_hereafter=invalid_hereafter,
        )

        tx_signed_step2 = cluster.sign_tx(
            tx_body_file=tx_raw_output_step2.out_file,
            signing_key_files=tx_files_step2.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )
        cluster.submit_tx(tx_file=tx_signed_step2, txins=mint_utxos)

        assert (
            cluster.get_address_balance(issuer_addr.address)
            == issuer_fund_balance - tx_raw_output_step2.fee
        ), f"Incorrect balance for token issuer address `{issuer_addr.address}`"

        token_utxo = cluster.get_utxo(address=issuer_addr.address, coins=[token])
        assert token_utxo and token_utxo[0].amount == token_amount, "The token was not minted"

        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step1)
        dbsync_utils.check_tx(cluster_obj=cluster, tx_raw_output=tx_raw_output_step2)


class TestMintingNegative:
    """Tests for minting with Plutus using `transaction build-raw` that are expected to fail."""

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.testnets
    def test_witness_redeemer_missing_signer(
        self, cluster: clusterlib.ClusterLib, payment_addrs: List[clusterlib.AddressRecord]
    ):
        """Test minting a token with a Plutus script with invalid signers.

        Expect failure.

        * fund the token issuer and create a UTxO for collateral
        * check that the expected amount was transferred to token issuer's address
        * try to mint the token using a Plutus script and a TX with signing key missing for
          the required signer
        * check that the minting failed because the required signers were not provided
        """
        # pylint: disable=too-many-locals
        temp_template = common.get_test_id(cluster)
        payment_addr = payment_addrs[0]
        issuer_addr = payment_addrs[1]

        lovelace_amount = 2_000_000
        token_amount = 5

        minting_cost = plutus_common.compute_cost(
            execution_cost=plutus_common.MINTING_WITNESS_REDEEMER_COST,
            protocol_params=cluster.get_protocol_params(),
        )

        # Step 1: fund the token issuer

        mint_utxos, collateral_utxos, __ = _fund_issuer(
            cluster_obj=cluster,
            temp_template=temp_template,
            payment_addr=payment_addr,
            issuer_addr=issuer_addr,
            minting_cost=minting_cost,
            amount=lovelace_amount,
        )

        # Step 2: mint the "qacoin"

        policyid = cluster.get_policyid(plutus_common.MINTING_WITNESS_REDEEMER_PLUTUS_V1)
        asset_name = f"qacoin{clusterlib.get_rand_str(4)}".encode("utf-8").hex()
        token = f"{policyid}.{asset_name}"
        mint_txouts = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token)
        ]

        plutus_mint_data = [
            clusterlib.Mint(
                txouts=mint_txouts,
                script_file=plutus_common.MINTING_WITNESS_REDEEMER_PLUTUS_V1,
                collaterals=collateral_utxos,
                execution_units=(
                    plutus_common.MINTING_WITNESS_REDEEMER_COST.per_time,
                    plutus_common.MINTING_WITNESS_REDEEMER_COST.per_space,
                ),
                redeemer_file=plutus_common.DATUM_WITNESS_GOLDEN_NORMAL,
            )
        ]

        tx_files_step2 = clusterlib.TxFiles(
            signing_key_files=[issuer_addr.skey_file],
        )
        txouts_step2 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount),
            *mint_txouts,
        ]
        tx_raw_output_step2 = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data,
            tx_files=tx_files_step2,
            fee=minting_cost.fee + FEE_MINT_TXSIZE,
            required_signers=[plutus_common.SIGNING_KEY_GOLDEN],
        )
        tx_signed_step2 = cluster.sign_tx(
            tx_body_file=tx_raw_output_step2.out_file,
            signing_key_files=tx_files_step2.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )
        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.submit_tx(tx_file=tx_signed_step2, txins=mint_utxos)
        assert "MissingRequiredSigners" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.testnets
    @param_plutus_version
    def test_low_budget(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
        plutus_version: str,
    ):
        """Test minting a token when budget is too low.

        Expect failure.

        * fund the token issuer and create a UTxO for collateral
        * check that the expected amount was transferred to token issuer's address
        * try to mint the token using a Plutus script when execution units are set to half
          of the expected values
        * check that the minting failed because the budget was overspent
        """
        # pylint: disable=too-many-locals
        temp_template = f"{common.get_test_id(cluster)}_{plutus_version}"

        payment_addr = payment_addrs[0]
        issuer_addr = payment_addrs[1]

        lovelace_amount = 2_000_000
        token_amount = 5

        plutus_v_record = plutus_common.MINTING_PLUTUS[plutus_version]

        minting_cost = plutus_common.compute_cost(
            execution_cost=plutus_v_record.execution_cost,
            protocol_params=cluster.get_protocol_params(),
        )

        # Step 1: fund the token issuer

        mint_utxos, collateral_utxos, __ = _fund_issuer(
            cluster_obj=cluster,
            temp_template=temp_template,
            payment_addr=payment_addr,
            issuer_addr=issuer_addr,
            minting_cost=minting_cost,
            amount=lovelace_amount,
        )

        # Step 2: try to mint the "qacoin"

        policyid = cluster.get_policyid(plutus_v_record.script_file)
        asset_name = f"qacoin{clusterlib.get_rand_str(4)}".encode("utf-8").hex()
        token = f"{policyid}.{asset_name}"
        mint_txouts = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token)
        ]

        plutus_mint_data = [
            clusterlib.Mint(
                txouts=mint_txouts,
                script_file=plutus_v_record.script_file,
                collaterals=collateral_utxos,
                # set execution units too low - to half of the expected values
                execution_units=(
                    plutus_v_record.execution_cost.per_time // 2,
                    plutus_v_record.execution_cost.per_space // 2,
                ),
                redeemer_file=plutus_common.DATUM_42,
            )
        ]

        tx_files_step2 = clusterlib.TxFiles(
            signing_key_files=[issuer_addr.skey_file],
        )
        txouts_step2 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount),
            *mint_txouts,
        ]
        tx_raw_output_step2 = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data,
            tx_files=tx_files_step2,
            fee=minting_cost.fee + FEE_MINT_TXSIZE,
        )
        tx_signed_step2 = cluster.sign_tx(
            tx_body_file=tx_raw_output_step2.out_file,
            signing_key_files=tx_files_step2.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.submit_tx(tx_file=tx_signed_step2, txins=mint_utxos)
        err_str = str(excinfo.value)
        assert (
            "The budget was overspent" in err_str or "due to overspending the budget" in err_str
        ), err_str

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.testnets
    @param_plutus_version
    def test_low_fee(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
        plutus_version: str,
    ):
        """Test minting a token when fee is set too low.

        Expect failure.

        * fund the token issuer and create a UTxO for collateral
        * check that the expected amount was transferred to token issuer's address
        * try to mint a token using a Plutus script when fee is set lower than is the computed fee
        * check that minting failed because the fee amount was too low
        """
        temp_template = f"{common.get_test_id(cluster)}_{plutus_version}"

        payment_addr = payment_addrs[0]
        issuer_addr = payment_addrs[1]

        lovelace_amount = 2_000_000
        token_amount = 5

        plutus_v_record = plutus_common.MINTING_PLUTUS[plutus_version]

        minting_cost = plutus_common.compute_cost(
            execution_cost=plutus_v_record.execution_cost,
            protocol_params=cluster.get_protocol_params(),
        )

        # Step 1: fund the token issuer

        mint_utxos, collateral_utxos, __ = _fund_issuer(
            cluster_obj=cluster,
            temp_template=temp_template,
            payment_addr=payment_addr,
            issuer_addr=issuer_addr,
            minting_cost=minting_cost,
            amount=lovelace_amount,
        )

        # Step 2: try to mint the "qacoin"

        policyid = cluster.get_policyid(plutus_v_record.script_file)
        asset_name = f"qacoin{clusterlib.get_rand_str(4)}".encode("utf-8").hex()
        token = f"{policyid}.{asset_name}"
        mint_txouts = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token)
        ]

        plutus_mint_data = [
            clusterlib.Mint(
                txouts=mint_txouts,
                script_file=plutus_v_record.script_file,
                collaterals=collateral_utxos,
                execution_units=(
                    plutus_v_record.execution_cost.per_time,
                    plutus_v_record.execution_cost.per_space,
                ),
                redeemer_file=plutus_common.DATUM_42,
            )
        ]

        tx_files_step2 = clusterlib.TxFiles(
            signing_key_files=[issuer_addr.skey_file],
        )

        fee_subtract = 300_000
        txouts_step2 = [
            # add subtracted fee to the transferred Lovelace amount so the Tx remains balanced
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount + fee_subtract),
            *mint_txouts,
        ]
        tx_raw_output_step2 = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data,
            tx_files=tx_files_step2,
            fee=FEE_MINT_TXSIZE + minting_cost.fee - fee_subtract,
        )
        tx_signed_step2 = cluster.sign_tx(
            tx_body_file=tx_raw_output_step2.out_file,
            signing_key_files=tx_files_step2.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.submit_tx(tx_file=tx_signed_step2, txins=mint_utxos)
        assert "FeeTooSmallUTxO" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.skipif(
        VERSIONS.transaction_era < VERSIONS.BABBAGE,
        reason="runs only with Babbage+ TX",
    )
    @pytest.mark.parametrize(
        "ttl",
        (3_000, 10_000, 100_000, 1000_000, -1),
    )
    @param_plutus_version
    def test_past_horizon(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
        ttl: int,
        plutus_version: str,
    ):
        """Test minting a token with ttl too far in the future.

        Expect failure.

        * fund the token issuer and create a UTxO for collateral
        * check that the expected amount was transferred to token issuer's address
        * try to mint a token using a Plutus script when ttl is set too far in the future
        * check that minting failed because of 'PastHorizon' failure
        """
        temp_template = f"{common.get_test_id(cluster)}_{plutus_version}_{ttl}"

        payment_addr = payment_addrs[0]
        issuer_addr = payment_addrs[1]

        lovelace_amount = 2_000_000
        token_amount = 5
        fee_txsize = 600_000

        plutus_v_record = plutus_common.MINTING_PLUTUS[plutus_version]

        minting_cost = plutus_common.compute_cost(
            execution_cost=plutus_v_record.execution_cost,
            protocol_params=cluster.get_protocol_params(),
        )

        # Step 1: fund the token issuer

        mint_utxos, collateral_utxos, __ = _fund_issuer(
            cluster_obj=cluster,
            temp_template=temp_template,
            payment_addr=payment_addr,
            issuer_addr=issuer_addr,
            minting_cost=minting_cost,
            amount=lovelace_amount,
            fee_txsize=fee_txsize,
        )

        # Step 2: try to mint the "qacoin"

        policyid = cluster.get_policyid(plutus_v_record.script_file)
        asset_name = f"qacoin{clusterlib.get_rand_str(4)}".encode("utf-8").hex()
        token = f"{policyid}.{asset_name}"
        mint_txouts = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token),
        ]

        plutus_mint_data = [
            clusterlib.Mint(
                txouts=mint_txouts,
                script_file=plutus_v_record.script_file,
                collaterals=collateral_utxos,
                execution_units=(
                    plutus_v_record.execution_cost.per_time,
                    plutus_v_record.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
            )
        ]

        tx_files_step2 = clusterlib.TxFiles(
            signing_key_files=[issuer_addr.skey_file],
        )
        txouts_step2 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount),
            *mint_txouts,
        ]

        # ttl == -1 means we'll use 3k/f + 100 slots for ttl
        if ttl == -1:
            ttl = (
                round(3 * cluster.genesis["securityParam"] / cluster.genesis["activeSlotsCoeff"])
                + 100
            )

        cluster.wait_for_new_block()
        tx_raw_output_step2 = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data,
            tx_files=tx_files_step2,
            fee=minting_cost.fee + fee_txsize,
            invalid_hereafter=cluster.get_slot_no() + ttl,
        )
        tx_signed_step2 = cluster.sign_tx(
            tx_body_file=tx_raw_output_step2.out_file,
            signing_key_files=tx_files_step2.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        err = ""
        try:
            cluster.submit_tx(tx_file=tx_signed_step2, txins=mint_utxos)
        except clusterlib.CLIError as exc:
            err = str(exc)
        else:
            pytest.xfail("ttl > 3k/f was accepted")

        assert "TimeTranslationPastHorizon" in err, err


class TestNegativeCollateral:
    """Tests for collaterals that are expected to fail."""

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.testnets
    @param_plutus_version
    def test_minting_with_invalid_collaterals(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
        plutus_version: str,
    ):
        """Test minting a token with a Plutus script with invalid collaterals.

        Expect failure.

        * fund the token issuer and create an UTxO for collateral with insufficient funds
        * check that the expected amount was transferred to token issuer's address
        * mint the token using a Plutus script
        * check that the minting failed because no valid collateral was provided
        """
        # pylint: disable=too-many-locals
        temp_template = f"{common.get_test_id(cluster)}_{plutus_version}"

        payment_addr = payment_addrs[0]
        issuer_addr = payment_addrs[1]

        lovelace_amount = 2_000_000
        token_amount = 5

        plutus_v_record = plutus_common.MINTING_PLUTUS[plutus_version]

        minting_cost = plutus_common.compute_cost(
            execution_cost=plutus_v_record.execution_cost,
            protocol_params=cluster.get_protocol_params(),
        )

        # Step 1: fund the token issuer

        mint_utxos, *__ = _fund_issuer(
            cluster_obj=cluster,
            temp_template=temp_template,
            payment_addr=payment_addr,
            issuer_addr=issuer_addr,
            minting_cost=minting_cost,
            amount=lovelace_amount,
        )

        # Step 2: mint the "qacoin"

        invalid_collateral_utxo = clusterlib.UTXOData(
            utxo_hash=mint_utxos[0].utxo_hash,
            utxo_ix=10,
            amount=minting_cost.collateral,
            address=issuer_addr.address,
        )

        policyid = cluster.get_policyid(plutus_v_record.script_file)
        asset_name = f"qacoin{clusterlib.get_rand_str(4)}".encode("utf-8").hex()
        token = f"{policyid}.{asset_name}"
        mint_txouts = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token)
        ]

        plutus_mint_data = [
            clusterlib.Mint(
                txouts=mint_txouts,
                script_file=plutus_v_record.script_file,
                collaterals=[invalid_collateral_utxo],
                execution_units=(
                    plutus_v_record.execution_cost.per_time,
                    plutus_v_record.execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
            )
        ]

        tx_files_step2 = clusterlib.TxFiles(
            signing_key_files=[issuer_addr.skey_file],
        )
        txouts_step2 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount),
            *mint_txouts,
        ]
        tx_raw_output_step2 = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data,
            tx_files=tx_files_step2,
            fee=minting_cost.fee + FEE_MINT_TXSIZE,
        )
        tx_signed_step2 = cluster.sign_tx(
            tx_body_file=tx_raw_output_step2.out_file,
            signing_key_files=tx_files_step2.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        # it should NOT be possible to mint with an invalid collateral
        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.submit_tx(tx_file=tx_signed_step2, txins=mint_utxos)
        assert "NoCollateralInputs" in str(excinfo.value)

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.testnets
    @param_plutus_version
    def test_minting_with_insufficient_collateral(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addrs: List[clusterlib.AddressRecord],
        plutus_version: str,
    ):
        """Test minting a token with a Plutus script with insufficient collateral.

        Expect failure.

        * fund the token issuer and create a UTxO for collateral with insufficient funds
        * check that the expected amount was transferred to token issuer's address
        * mint the token using a Plutus script
        * check that the minting failed because a collateral with insufficient funds was provided
        """
        # pylint: disable=too-many-locals
        temp_template = f"{common.get_test_id(cluster)}_{plutus_version}"

        payment_addr = payment_addrs[0]
        issuer_addr = payment_addrs[1]

        lovelace_amount = 2_000_000
        collateral_amount = 2_000_000
        token_amount = 5

        plutus_v_record = plutus_common.MINTING_PLUTUS[plutus_version]

        # increase fixed cost so the required collateral is higher than minimum collateral of 2 ADA
        execution_cost = plutus_v_record.execution_cost._replace(fixed_cost=2_000_000)

        minting_cost = plutus_common.compute_cost(
            execution_cost=execution_cost, protocol_params=cluster.get_protocol_params()
        )

        # Step 1: fund the token issuer

        mint_utxos, *__ = _fund_issuer(
            cluster_obj=cluster,
            temp_template=temp_template,
            payment_addr=payment_addr,
            issuer_addr=issuer_addr,
            minting_cost=minting_cost,
            amount=lovelace_amount,
        )

        # Step 2: mint the "qacoin"

        invalid_collateral_utxo = clusterlib.UTXOData(
            utxo_hash=mint_utxos[0].utxo_hash,
            utxo_ix=1,
            amount=collateral_amount,
            address=issuer_addr.address,
        )

        policyid = cluster.get_policyid(plutus_v_record.script_file)
        asset_name = f"qacoin{clusterlib.get_rand_str(4)}".encode("utf-8").hex()
        token = f"{policyid}.{asset_name}"
        mint_txouts = [
            clusterlib.TxOut(address=issuer_addr.address, amount=token_amount, coin=token)
        ]

        plutus_mint_data = [
            clusterlib.Mint(
                txouts=mint_txouts,
                script_file=plutus_v_record.script_file,
                collaterals=[invalid_collateral_utxo],
                execution_units=(
                    execution_cost.per_time,
                    execution_cost.per_space,
                ),
                redeemer_cbor_file=plutus_common.REDEEMER_42_CBOR,
            )
        ]

        tx_files_step2 = clusterlib.TxFiles(
            signing_key_files=[issuer_addr.skey_file],
        )
        txouts_step2 = [
            clusterlib.TxOut(address=issuer_addr.address, amount=lovelace_amount),
            *mint_txouts,
        ]
        tx_raw_output_step2 = cluster.build_raw_tx_bare(
            out_file=f"{temp_template}_step2_tx.body",
            txins=mint_utxos,
            txouts=txouts_step2,
            mint=plutus_mint_data,
            tx_files=tx_files_step2,
            fee=minting_cost.fee + FEE_MINT_TXSIZE,
        )
        tx_signed_step2 = cluster.sign_tx(
            tx_body_file=tx_raw_output_step2.out_file,
            signing_key_files=tx_files_step2.signing_key_files,
            tx_name=f"{temp_template}_step2",
        )

        # it should NOT be possible to mint with a collateral with insufficient funds
        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.submit_tx(tx_file=tx_signed_step2, txins=mint_utxos)
        assert "InsufficientCollateral" in str(excinfo.value)
