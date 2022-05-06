"""Tests for KES period."""
# pylint: disable=abstract-class-instantiated
import datetime
import json
import logging
import shutil
import time
from pathlib import Path

import allure
import pytest
from _pytest.tmpdir import TempdirFactory
from cardano_clusterlib import clusterlib

from cardano_node_tests.tests import common
from cardano_node_tests.tests import kes
from cardano_node_tests.utils import cluster_management
from cardano_node_tests.utils import cluster_nodes
from cardano_node_tests.utils import clusterlib_utils
from cardano_node_tests.utils import helpers
from cardano_node_tests.utils import locking
from cardano_node_tests.utils import logfiles
from cardano_node_tests.utils import temptools
from cardano_node_tests.utils.versions import VERSIONS

LOGGER = logging.getLogger(__name__)

# number of epochs traversed during local cluster startup
# NOTE: must be kept up-to-date
NUM_OF_EPOCHS = 6


@pytest.fixture
def cluster_lock_pool2(cluster_manager: cluster_management.ClusterManager) -> clusterlib.ClusterLib:
    return cluster_manager.get(lock_resources=[cluster_management.Resources.POOL2])


@pytest.fixture(scope="module")
def short_kes_start_cluster(tmp_path_factory: TempdirFactory) -> Path:
    """Update *slotsPerKESPeriod* and *maxKESEvolutions*."""
    shared_tmp = temptools.get_pytest_shared_tmp(tmp_path_factory)
    max_kes_evolutions = 10

    # need to lock because this same fixture can run on several workers in parallel
    with locking.FileLockIfXdist(f"{shared_tmp}/startup_files_short_kes.lock"):
        destdir = shared_tmp / "startup_files_short_kes"
        destdir.mkdir(exist_ok=True)

        # return existing script if it is already generated by other worker
        destdir_ls = list(destdir.glob("start-cluster*"))
        if destdir_ls:
            return destdir_ls[0]

        startup_files = cluster_nodes.get_cluster_type().cluster_scripts.copy_scripts_files(
            destdir=destdir
        )
        with open(startup_files.genesis_spec, encoding="utf-8") as fp_in:
            genesis_spec = json.load(fp_in)

        # KES needs to be valid at least until the local cluster is fully started.
        # We need to calculate how many slots there is from the start of Shelley epoch
        # until the cluster is fully started.
        # Assume k=10, i.e. k * 10 = 100 slots in Byron era.
        # Subtract one Byron epoch and current (last) epoch when calculating slots in
        # Shelley epochs.
        epoch_length = genesis_spec["epochLength"]
        cluster_start_time_slots = int((NUM_OF_EPOCHS - 2) * epoch_length + 100)
        exact_kes_period_slots = int(cluster_start_time_slots / max_kes_evolutions)

        genesis_spec["slotsPerKESPeriod"] = int(exact_kes_period_slots * 1.2)  # add buffer
        genesis_spec["maxKESEvolutions"] = max_kes_evolutions

        with open(startup_files.genesis_spec, "w", encoding="utf-8") as fp_out:
            json.dump(genesis_spec, fp_out)

        return startup_files.start_script


@pytest.fixture
def cluster_kes(
    cluster_manager: cluster_management.ClusterManager, short_kes_start_cluster: Path
) -> clusterlib.ClusterLib:
    return cluster_manager.get(
        lock_resources=[cluster_management.Resources.CLUSTER],
        cleanup=True,
        start_cmd=str(short_kes_start_cluster),
    )


def _wait_epoch_chores(cluster_obj: clusterlib.ClusterLib, temp_template: str, this_epoch: int):
    if cluster_obj.get_epoch() == this_epoch:
        LOGGER.info(f"{datetime.datetime.now()}: Waiting for next epoch.")
        cluster_obj.wait_for_new_epoch()

    LOGGER.info(f"{datetime.datetime.now()}: Waiting for the end of current epoch.")
    clusterlib_utils.wait_for_epoch_interval(cluster_obj=cluster_obj, start=-19, stop=-15)

    # save ledger state
    clusterlib_utils.save_ledger_state(
        cluster_obj=cluster_obj,
        state_name=f"{temp_template}_{cluster_obj.get_epoch()}",
    )


class TestKES:
    """Basic tests for KES period."""

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.order(5)
    @pytest.mark.long
    @pytest.mark.skipif(
        not (VERSIONS.cluster_era == VERSIONS.transaction_era == VERSIONS.LAST_KNOWN_ERA),
        reason="meant to run only with the latest cluster era and the latest transaction era",
    )
    def test_expired_kes(
        self,
        cluster_kes: clusterlib.ClusterLib,
        cluster_manager: cluster_management.ClusterManager,
        worker_id: str,
    ):
        """Test expired KES.

        * start local cluster instance configured with short KES period and low number of key
          evolutions, so KES expires soon on all pools
        * refresh opcert on 2 of the 3 pools, so KES doesn't expire on those 2 pools and
          the pools keep minting blocks
        * wait for KES expiration on the selected pool
        * check that the pool with expired KES didn't mint blocks in an epoch that followed after
          KES expiration
        * check KES period info command with an operational certificate with an expired KES
        * check KES period info command with operational certificates with a valid KES
        """
        cluster = cluster_kes
        temp_template = common.get_test_id(cluster)

        expire_timeout = 200
        expire_node_name = "pool1"
        expire_pool_name = f"node-{expire_node_name}"
        expire_pool_rec = cluster_manager.cache.addrs_data[expire_pool_name]
        expire_pool_id = cluster.get_stake_pool_id(expire_pool_rec["cold_key_pair"].vkey_file)
        expire_pool_id_dec = helpers.decode_bech32(expire_pool_id)

        # refresh opcert on 2 of the 3 pools, so KES doesn't expire on those 2 pools and
        # the pools keep minting blocks
        refreshed_nodes = ["pool2", "pool3"]

        def _refresh_opcerts():
            for n in refreshed_nodes:
                refreshed_pool_rec = cluster_manager.cache.addrs_data[f"node-{n}"]
                refreshed_opcert_file = cluster.gen_node_operational_cert(
                    node_name=f"{n}_refreshed_opcert",
                    kes_vkey_file=refreshed_pool_rec["kes_key_pair"].vkey_file,
                    cold_skey_file=refreshed_pool_rec["cold_key_pair"].skey_file,
                    cold_counter_file=refreshed_pool_rec["cold_key_pair"].counter_file,
                    kes_period=cluster.get_kes_period(),
                )
                shutil.copy(refreshed_opcert_file, refreshed_pool_rec["pool_operational_cert"])
            cluster_nodes.restart_nodes(refreshed_nodes)

        _refresh_opcerts()

        expected_err_regexes = ["KESKeyAlreadyPoisoned", "KESCouldNotEvolve"]
        # ignore expected errors in bft1 node log file, as bft1 opcert will not get refreshed
        for err in expected_err_regexes:
            logfiles.add_ignore_rule(
                files_glob="bft1.stdout",
                regex=err,
                ignore_file_id=worker_id,
            )
        # search for expected errors only in log file corresponding to pool with expired KES
        expected_errors = [(f"{expire_node_name}.stdout", err) for err in expected_err_regexes]

        this_epoch = -1
        with logfiles.expect_errors(expected_errors, ignore_file_id=worker_id):
            LOGGER.info(
                f"{datetime.datetime.now()}: Waiting for {expire_timeout} sec for KES expiration."
            )
            time.sleep(expire_timeout)

            _wait_epoch_chores(
                cluster_obj=cluster, temp_template=temp_template, this_epoch=this_epoch
            )
            this_epoch = cluster.get_epoch()

            # check that the pool is not producing any blocks
            blocks_made = clusterlib_utils.get_ledger_state(cluster_obj=cluster)["blocksCurrent"]
            if blocks_made:
                assert (
                    expire_pool_id_dec not in blocks_made
                ), f"The pool '{expire_pool_name}' has minted blocks in epoch {this_epoch}"

            # refresh opcerts one more time
            _refresh_opcerts()

            LOGGER.info(
                f"{datetime.datetime.now()}: Waiting 120 secs to make sure the expected errors "
                "make it to log files."
            )
            time.sleep(120)

        # check kes-period-info with an operational certificate with KES expired
        kes_info_expired = cluster.get_kes_period_info(
            opcert_file=expire_pool_rec["pool_operational_cert"]
        )
        kes.check_kes_period_info_result(
            kes_output=kes_info_expired, expected_scenario=kes.KesScenarios.INVALID_KES_PERIOD
        )

        # check kes-period-info with valid operational certificates
        for n in refreshed_nodes:
            refreshed_pool_rec = cluster_manager.cache.addrs_data[f"node-{n}"]
            kes_info_valid = cluster.get_kes_period_info(
                opcert_file=refreshed_pool_rec["pool_operational_cert"]
            )
            kes.check_kes_period_info_result(
                kes_output=kes_info_valid, expected_scenario=kes.KesScenarios.ALL_VALID
            )

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.order(6)
    @pytest.mark.long
    def test_opcert_future_kes_period(
        self,
        cluster_lock_pool2: clusterlib.ClusterLib,
        cluster_manager: cluster_management.ClusterManager,
    ):
        """Start a stake pool with an operational certificate created with invalid `--kes-period`.

        * generate new operational certificate with `--kes-period` in the future
        * restart the node with the new operational certificate
        * check that the pool is not producing any blocks
        * generate new operational certificate with valid `--kes-period` and restart the node
        * check that the pool is producing blocks again
        """
        pool_name = cluster_management.Resources.POOL2
        node_name = "pool2"
        cluster = cluster_lock_pool2

        temp_template = common.get_test_id(cluster)
        pool_rec = cluster_manager.cache.addrs_data[pool_name]

        node_cold = pool_rec["cold_key_pair"]
        stake_pool_id = cluster.get_stake_pool_id(node_cold.vkey_file)
        stake_pool_id_dec = helpers.decode_bech32(stake_pool_id)

        opcert_file: Path = pool_rec["pool_operational_cert"]

        with cluster_manager.restart_on_failure():
            # generate new operational certificate with `--kes-period` in the future
            invalid_opcert_file = cluster.gen_node_operational_cert(
                node_name=f"{node_name}_invalid_opcert_file",
                kes_vkey_file=pool_rec["kes_key_pair"].vkey_file,
                cold_skey_file=pool_rec["cold_key_pair"].skey_file,
                cold_counter_file=pool_rec["cold_key_pair"].counter_file,
                kes_period=cluster.get_kes_period() + 100,
            )

            expected_errors = [
                (f"{node_name}.stdout", "TPraosCannotForgeKeyNotUsableYet"),
            ]
            with logfiles.expect_errors(expected_errors, ignore_file_id=cluster_manager.worker_id):
                # restart the node with the new operational certificate
                logfiles.add_ignore_rule(
                    files_glob="*.stdout",
                    regex="MuxBearerClosed",
                    ignore_file_id=cluster_manager.worker_id,
                )
                shutil.copy(invalid_opcert_file, opcert_file)
                cluster_nodes.restart_nodes([node_name])
                cluster.wait_for_new_epoch()

                LOGGER.info("Checking blocks production for 5 epochs.")
                this_epoch = -1
                for __ in range(5):
                    _wait_epoch_chores(
                        cluster_obj=cluster, temp_template=temp_template, this_epoch=this_epoch
                    )
                    this_epoch = cluster.get_epoch()

                    # check that the pool is not producing any blocks
                    blocks_made = clusterlib_utils.get_ledger_state(cluster_obj=cluster)[
                        "blocksCurrent"
                    ]
                    if blocks_made:
                        assert (
                            stake_pool_id_dec not in blocks_made
                        ), f"The pool '{pool_name}' has produced blocks in epoch {this_epoch}"

            # check kes-period-info with operational certificate with invalid `--kes-period`
            kes_period_info = cluster.get_kes_period_info(invalid_opcert_file)
            kes.check_kes_period_info_result(
                kes_output=kes_period_info, expected_scenario=kes.KesScenarios.INVALID_KES_PERIOD
            )

            # generate new operational certificate with valid `--kes-period`
            valid_opcert_file = cluster.gen_node_operational_cert(
                node_name=f"{node_name}_valid_opcert_file",
                kes_vkey_file=pool_rec["kes_key_pair"].vkey_file,
                cold_skey_file=pool_rec["cold_key_pair"].skey_file,
                cold_counter_file=pool_rec["cold_key_pair"].counter_file,
                kes_period=cluster.get_kes_period(),
            )
            # copy the new certificate and restart the node
            shutil.copy(valid_opcert_file, opcert_file)
            cluster_nodes.restart_nodes([node_name])
            this_epoch = cluster.wait_for_new_epoch()

            LOGGER.info("Checking blocks production for another 5 epochs.")
            blocks_made_db = []
            active_again_epoch = this_epoch
            for __ in range(5):
                _wait_epoch_chores(
                    cluster_obj=cluster, temp_template=temp_template, this_epoch=this_epoch
                )
                this_epoch = cluster.get_epoch()

                # check that the pool is producing blocks
                blocks_made = clusterlib_utils.get_ledger_state(cluster_obj=cluster)[
                    "blocksCurrent"
                ]
                blocks_made_db.append(stake_pool_id_dec in blocks_made)

            assert any(blocks_made_db), (
                f"The pool '{pool_name}' has not produced any blocks "
                f"since epoch {active_again_epoch}"
            )

            # check kes-period-info with valid operational certificate
            kes_period_info = cluster.get_kes_period_info(valid_opcert_file)
            kes.check_kes_period_info_result(
                kes_output=kes_period_info, expected_scenario=kes.KesScenarios.ALL_VALID
            )

            # check kes-period-info with invalid operational certificate, wrong counter and period
            kes_period_info = cluster.get_kes_period_info(invalid_opcert_file)
            kes.check_kes_period_info_result(
                kes_output=kes_period_info, expected_scenario=kes.KesScenarios.ALL_INVALID
            )

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.order(7)
    @pytest.mark.long
    def test_update_valid_opcert(
        self,
        cluster_lock_pool2: clusterlib.ClusterLib,
        cluster_manager: cluster_management.ClusterManager,
    ):
        """Update a valid operational certificate with another valid operational certificate.

        * generate new operational certificate with valid `--kes-period`
        * copy new operational certificate to the node
        * stop the node so the corresponding pool is not minting new blocks
        * check `kes-period-info` while the pool is not minting blocks
        * start the node with the new operational certificate
        * check that the pool is minting blocks again
        * check that metrics reported by `kes-period-info` got updated once the pool started
          minting blocks again
        * check `kes-period-info` with the old (replaced) operational certificate
        """
        pool_name = cluster_management.Resources.POOL2
        node_name = "pool2"
        cluster = cluster_lock_pool2

        temp_template = common.get_test_id(cluster)
        pool_rec = cluster_manager.cache.addrs_data[pool_name]

        node_cold = pool_rec["cold_key_pair"]
        stake_pool_id = cluster.get_stake_pool_id(node_cold.vkey_file)
        stake_pool_id_dec = helpers.decode_bech32(stake_pool_id)

        opcert_file = pool_rec["pool_operational_cert"]
        opcert_file_old = shutil.copy(opcert_file, f"{opcert_file}_old")

        with cluster_manager.restart_on_failure():
            # generate new operational certificate with valid `--kes-period`
            new_opcert_file = cluster.gen_node_operational_cert(
                node_name=f"{node_name}_new_opcert_file",
                kes_vkey_file=pool_rec["kes_key_pair"].vkey_file,
                cold_skey_file=pool_rec["cold_key_pair"].skey_file,
                cold_counter_file=pool_rec["cold_key_pair"].counter_file,
                kes_period=cluster.get_kes_period(),
            )

            # copy new operational certificate to the node
            logfiles.add_ignore_rule(
                files_glob="*.stdout",
                regex="MuxBearerClosed",
                ignore_file_id=cluster_manager.worker_id,
            )
            shutil.copy(new_opcert_file, opcert_file)

            # stop the node so the corresponding pool is not minting new blocks
            cluster_nodes.stop_nodes([node_name])

            time.sleep(10)

            # check kes-period-info while the pool is not minting blocks
            kes_period_info_new = cluster.get_kes_period_info(opcert_file)
            kes.check_kes_period_info_result(
                kes_output=kes_period_info_new, expected_scenario=kes.KesScenarios.ALL_VALID
            )
            kes_period_info_old = cluster.get_kes_period_info(opcert_file_old)
            kes.check_kes_period_info_result(
                kes_output=kes_period_info_old, expected_scenario=kes.KesScenarios.ALL_VALID
            )
            assert (
                kes_period_info_new["metrics"]["qKesNodeStateOperationalCertificateNumber"]
                == kes_period_info_old["metrics"]["qKesNodeStateOperationalCertificateNumber"]
            )

            # start the node with the new operational certificate
            cluster_nodes.start_nodes([node_name])

            # make sure we are not at the very end of an epoch so we still have time for
            # the first block production check
            clusterlib_utils.wait_for_epoch_interval(cluster_obj=cluster, start=5, stop=-18)

            LOGGER.info("Checking blocks production for 5 epochs.")
            blocks_made_db = []
            this_epoch = -1
            updated_epoch = cluster.get_epoch()
            for __ in range(5):
                # wait for next epoch
                if cluster.get_epoch() == this_epoch:
                    cluster.wait_for_new_epoch()

                # wait for the end of the epoch
                clusterlib_utils.wait_for_epoch_interval(
                    cluster_obj=cluster, start=-19, stop=-15, force_epoch=True
                )
                this_epoch = cluster.get_epoch()

                ledger_state = clusterlib_utils.get_ledger_state(cluster_obj=cluster)

                # save ledger state
                clusterlib_utils.save_ledger_state(
                    cluster_obj=cluster,
                    state_name=f"{temp_template}_{this_epoch}",
                    ledger_state=ledger_state,
                )

                # check that the pool is minting blocks
                blocks_made = ledger_state["blocksCurrent"]
                blocks_made_db.append(stake_pool_id_dec in blocks_made)

            assert any(
                blocks_made_db
            ), f"The pool '{pool_name}' has not minted any blocks since epoch {updated_epoch}"

            # check that metrics reported by kes-period-info got updated once the pool started
            # minting blocks again
            kes_period_info_updated = cluster.get_kes_period_info(opcert_file)
            kes.check_kes_period_info_result(
                kes_output=kes_period_info_updated, expected_scenario=kes.KesScenarios.ALL_VALID
            )
            assert (
                kes_period_info_updated["metrics"]["qKesNodeStateOperationalCertificateNumber"]
                != kes_period_info_old["metrics"]["qKesNodeStateOperationalCertificateNumber"]
            )

            # check kes-period-info with operational certificate with a wrong counter
            kes_period_info_invalid = cluster.get_kes_period_info(opcert_file_old)
            kes.check_kes_period_info_result(
                kes_output=kes_period_info_invalid,
                expected_scenario=kes.KesScenarios.INVALID_COUNTERS,
            )

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.skipif(
        VERSIONS.transaction_era != VERSIONS.DEFAULT_TX_ERA,
        reason="different TX eras doesn't affect this test, pointless to run",
    )
    def test_no_kes_period_arg(
        self,
        cluster: clusterlib.ClusterLib,
        cluster_manager: cluster_management.ClusterManager,
    ):
        """Try to generate new operational certificate without specifying the `--kes-period`.

        Expect failure.
        """
        pool_name = cluster_management.Resources.POOL2
        pool_rec = cluster_manager.cache.addrs_data[pool_name]

        temp_template = common.get_test_id(cluster)
        out_file = Path(f"{temp_template}_shouldnt_exist.opcert")

        # try to generate new operational certificate without specifying the `--kes-period`
        with pytest.raises(clusterlib.CLIError) as excinfo:
            cluster.cli(
                [
                    "node",
                    "issue-op-cert",
                    "--kes-verification-key-file",
                    str(pool_rec["kes_key_pair"].vkey_file),
                    "--cold-signing-key-file",
                    str(pool_rec["cold_key_pair"].skey_file),
                    "--operational-certificate-issue-counter",
                    str(pool_rec["cold_key_pair"].counter_file),
                    "--out-file",
                    str(out_file),
                ]
            )
        assert "Missing: --kes-period NATURAL" in str(excinfo.value)
        assert not out_file.exists(), "New operational certificate was generated"
