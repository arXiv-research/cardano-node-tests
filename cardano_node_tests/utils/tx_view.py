"""Checks for `transaction view` CLI command."""
import itertools
import logging
import re
from typing import Any
from typing import Dict
from typing import List
from typing import Set
from typing import Tuple
from typing import Union

import yaml
from cardano_clusterlib import clusterlib

from cardano_node_tests.utils import helpers
from cardano_node_tests.utils.versions import VERSIONS

LOGGER = logging.getLogger(__name__)

CERTIFICATES_INFORMATION = {
    "genesis key delegation": {"VRF key hash", "delegate key hash", "genesis key hash"},
    "MIR": {"pot", "target stake addresses", "send to treasury", "send to reserves"},
    "stake address deregistration": {
        "stake credential key hash",
        "stake credential script hash",
    },
    "stake address registration": {"stake credential key hash", "stake credential script hash"},
    "stake address delegation": {
        "pool",
        "stake credential key hash",
        "stake credential script hash",
    },
    "stake pool retirement": {"epoch", "pool"},
    "stake pool registration": {
        "VRF key hash",
        "cost",
        "margin",
        "metadata",
        "owners (stake key hashes)",
        "pledge",
        "pool",
        "relays",
        "reward account",
    },
}


def load_tx_view(tx_view: str) -> dict:
    """Load tx view output as YAML."""
    tx_loaded: dict = yaml.safe_load(tx_view)
    return tx_loaded


def _load_assets(assets: Dict[str, Dict[str, int]]) -> List[Tuple[int, str]]:
    loaded_data = []

    for policy_key, policy_rec in assets.items():
        if policy_key == clusterlib.DEFAULT_COIN:
            continue
        if "policy " in policy_key:
            policy_key = policy_key.replace("policy ", "")
        for asset_name, amount in policy_rec.items():
            if "asset " in asset_name:
                asset_name = re.search(r"asset ([0-9a-f]*)", asset_name).group(1)  # type: ignore
            elif asset_name == "default asset":
                asset_name = ""
            token = f"{policy_key}.{asset_name}" if asset_name else policy_key
            loaded_data.append((amount, token))

    return loaded_data


def _load_coins_data(coins_data: Union[dict, str]) -> List[Tuple[int, str]]:
    # `coins_data` for Mary+ Tx era has Lovelace amount and policies info,
    # for older Tx eras it's just Lovelace amount
    try:
        amount_lovelace = coins_data.get(clusterlib.DEFAULT_COIN)  # type: ignore
        policies_data: dict = coins_data  # type: ignore
    except AttributeError:
        amount_lovelace = int(coins_data.split()[0] or 0)  # type: ignore
        policies_data = {}

    loaded_data = []

    if amount_lovelace:
        loaded_data.append((amount_lovelace, clusterlib.DEFAULT_COIN))

    assets_data = _load_assets(assets=policies_data)

    return [*loaded_data, *assets_data]


def _check_collateral_inputs(
    tx_raw_output: clusterlib.TxRawOutput, expected_collateral: List[str]
) -> bool:
    """Check collateral inputs of tx_view."""
    all_collateral_locations: List[Any] = [
        *(tx_raw_output.mint or []),
        *(tx_raw_output.script_txins or []),
        *(tx_raw_output.script_withdrawals or []),
        *(tx_raw_output.complex_certs or []),
    ]

    _collateral_ins_nested = [
        r.collaterals for r in all_collateral_locations if hasattr(r, "collaterals")
    ]

    collateral_ins = list(itertools.chain.from_iterable(_collateral_ins_nested))

    collateral_strings = {f"{c.utxo_hash}#{c.utxo_ix}" for c in collateral_ins}

    return collateral_strings == set(expected_collateral)


def _check_reference_inputs(
    tx_raw_output: clusterlib.TxRawOutput, expected_reference_inputs: List[str]
) -> bool:
    """Check reference inputs of tx_view."""
    reference_scripts = [
        s.reference_txin for s in tx_raw_output.script_txins if getattr(s, "reference_txin", None)
    ]

    all_reference_inputs: List[Any] = [
        *(tx_raw_output.readonly_reference_txins or []),
        *(reference_scripts or []),
    ]

    reference_strings = {f"{r.utxo_hash}#{r.utxo_ix}" for r in all_reference_inputs}

    return reference_strings == set(expected_reference_inputs)


def check_tx_view(  # noqa: C901
    cluster_obj: clusterlib.ClusterLib, tx_raw_output: clusterlib.TxRawOutput
) -> dict:
    """Check output of the `transaction view` command."""
    # pylint: disable=too-many-branches,too-many-locals,too-many-statements

    tx_view_raw = cluster_obj.view_tx(tx_body_file=tx_raw_output.out_file)
    tx_loaded: dict = load_tx_view(tx_view=tx_view_raw)

    # check inputs
    loaded_txins = set(tx_loaded.get("inputs") or [])
    _tx_raw_script_txins = list(
        itertools.chain.from_iterable(r.txins for r in tx_raw_output.script_txins)
    )
    tx_raw_script_txins = {f"{r.utxo_hash}#{r.utxo_ix}" for r in _tx_raw_script_txins}
    tx_raw_simple_txins = {f"{r.utxo_hash}#{r.utxo_ix}" for r in tx_raw_output.txins}
    tx_raw_txins = tx_raw_simple_txins.union(tx_raw_script_txins)

    if tx_raw_txins != loaded_txins:
        raise AssertionError(f"txins: {tx_raw_txins} != {loaded_txins}")

    # check outputs
    tx_loaded_outputs = tx_loaded.get("outputs") or []
    loaded_txouts: Set[Tuple[str, int, str]] = set()
    for txout in tx_loaded_outputs:
        address = txout["address"]
        for amount in _load_coins_data(txout["amount"]):
            loaded_txouts.add((address, amount[0], amount[1]))

    tx_raw_txouts = {(r.address, r.amount, r.coin) for r in tx_raw_output.txouts}

    if not tx_raw_txouts.issubset(loaded_txouts):
        raise AssertionError(f"txouts: {tx_raw_txouts} not in {loaded_txouts}")

    # check fee
    fee = int(tx_loaded.get("fee", "").split()[0] or 0)
    # pylint: disable=consider-using-in
    if (
        tx_raw_output.fee != -1 and tx_raw_output.fee != fee
    ):  # for `transaction build` the `tx_raw_output.fee` can be -1
        raise AssertionError(f"fee: {tx_raw_output.fee} != {fee}")

    # check validity intervals
    validity_range = tx_loaded.get("validity range") or {}

    loaded_invalid_before = validity_range.get("lower bound")
    if tx_raw_output.invalid_before != loaded_invalid_before:
        raise AssertionError(
            f"invalid before: {tx_raw_output.invalid_before} != {loaded_invalid_before}"
        )

    loaded_invalid_hereafter = validity_range.get("upper bound") or validity_range.get(
        "time to live"
    )
    if tx_raw_output.invalid_hereafter != loaded_invalid_hereafter:
        raise AssertionError(
            f"invalid hereafter: {tx_raw_output.invalid_hereafter} != {loaded_invalid_hereafter}"
        )

    # check minting and burning
    loaded_mint = set(_load_assets(assets=tx_loaded.get("mint") or {}))
    mint_txouts = list(itertools.chain.from_iterable(m.txouts for m in tx_raw_output.mint))
    tx_raw_mint = {(r.amount, r.coin) for r in mint_txouts}

    if tx_raw_mint != loaded_mint:
        raise AssertionError(f"mint: {tx_raw_mint} != {loaded_mint}")

    # check withdrawals
    tx_loaded_withdrawals = tx_loaded.get("withdrawals")
    loaded_withdrawals = set()
    if tx_loaded_withdrawals:
        for withdrawal in tx_loaded_withdrawals:
            withdrawal_key = (
                withdrawal.get("stake credential key hash")
                or withdrawal.get("stake credential script hash")
                # TODO: legacy for backwards compatibility with 1.34.1
                or withdrawal.get("credential", {}).get("key hash")
                or withdrawal.get("credential", {}).get("script hash")
            )
            withdrawal_amount = int(withdrawal["amount"].split()[0] or 0)
            loaded_withdrawals.add((withdrawal_key, withdrawal_amount))

    tx_raw_withdrawals = {
        (helpers.decode_bech32(r.address)[2:], r.amount) for r in tx_raw_output.withdrawals
    }

    if tx_raw_withdrawals != loaded_withdrawals:
        raise AssertionError(f"withdrawals: {tx_raw_withdrawals} != {loaded_withdrawals}")

    # check certificates
    tx_raw_len_certs = len(tx_raw_output.tx_files.certificate_files) + len(
        tx_raw_output.complex_certs
    )
    loaded_len_certs = len(tx_loaded.get("certificates") or [])

    if tx_raw_len_certs != loaded_len_certs:
        raise AssertionError(f"certificates: {tx_raw_len_certs} != {loaded_len_certs}")

    for certificate in tx_loaded.get("certificates") or []:
        certificate_name = list(certificate.keys())[0]
        certificate_fields = set(list(certificate.values())[0].keys())

        if CERTIFICATES_INFORMATION.get(certificate_name) and not certificate_fields.issubset(
            CERTIFICATES_INFORMATION[certificate_name]
        ):
            raise AssertionError(
                f"The output of the certificate '{certificate_name}' doesn't have "
                "the expected fields"
            )

    # load and check transaction era
    loaded_tx_era: str = tx_loaded["era"]
    loaded_tx_version = getattr(VERSIONS, loaded_tx_era.upper())

    output_tx_version = (
        getattr(VERSIONS, tx_raw_output.era.upper())
        if tx_raw_output.era
        else VERSIONS.DEFAULT_TX_ERA
    )

    if loaded_tx_version != output_tx_version:
        raise AssertionError(
            f"transaction era is not the expected: {loaded_tx_version} != {output_tx_version}"
        )

    # check collateral inputs, this is only available on Alonzo+ TX
    if loaded_tx_version >= VERSIONS.ALONZO and not _check_collateral_inputs(
        tx_raw_output=tx_raw_output, expected_collateral=tx_loaded["collateral inputs"]
    ):
        raise AssertionError("collateral inputs are not the expected")

    # check reference inputs, this is only available on Babbage+ TX
    if loaded_tx_version >= VERSIONS.BABBAGE and not _check_reference_inputs(
        tx_raw_output=tx_raw_output,
        expected_reference_inputs=tx_loaded.get("reference inputs") or [],
    ):
        raise AssertionError("reference inputs are not the expected")

    return tx_loaded
