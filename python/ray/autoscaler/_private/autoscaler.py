from collections import defaultdict, namedtuple
from typing import Any, Optional, Dict, List
from urllib3.exceptions import MaxRetryError
import copy
import logging
import math
import os
import subprocess
import threading
import time
import yaml
import collections

from ray.experimental.internal_kv import _internal_kv_put, \
    _internal_kv_initialized
from ray.autoscaler.tags import (TAG_RAY_LAUNCH_CONFIG, TAG_RAY_RUNTIME_CONFIG,
                                 TAG_RAY_FILE_MOUNTS_CONTENTS,
                                 TAG_RAY_NODE_STATUS, TAG_RAY_NODE_KIND,
                                 TAG_RAY_USER_NODE_TYPE, STATUS_UP_TO_DATE,
                                 NODE_KIND_WORKER, NODE_KIND_UNMANAGED)
from ray.autoscaler._private.providers import _get_node_provider
from ray.autoscaler._private.updater import NodeUpdaterThread
from ray.autoscaler._private.node_launcher import NodeLauncher
from ray.autoscaler._private.resource_demand_scheduler import \
    get_bin_pack_residual, ResourceDemandScheduler, NodeType, NodeID
from ray.autoscaler._private.util import ConcurrentCounter, validate_config, \
    with_head_node_ip, hash_launch_conf, hash_runtime_conf, \
    DEBUG_AUTOSCALING_STATUS, DEBUG_AUTOSCALING_ERROR
from ray.autoscaler._private.constants import \
    AUTOSCALER_MAX_NUM_FAILURES, AUTOSCALER_MAX_LAUNCH_BATCH, \
    AUTOSCALER_MAX_CONCURRENT_LAUNCHES, AUTOSCALER_UPDATE_INTERVAL_S, \
    AUTOSCALER_HEARTBEAT_TIMEOUT_S
from six.moves import queue

logger = logging.getLogger(__name__)

# Tuple of modified fields for the given node_id returned by should_update
# that will be passed into a NodeUpdaterThread.
UpdateInstructions = namedtuple(
    "UpdateInstructions",
    ["node_id", "init_commands", "start_ray_commands", "docker_config"])


class StandardAutoscaler:
    """The autoscaling control loop for a Ray cluster.

    There are two ways to start an autoscaling cluster: manually by running
    `ray start --head --autoscaling-config=/path/to/config.yaml` on a
    instance that has permission to launch other instances, or you can also use
    `ray create_or_update /path/to/config.yaml` from your laptop, which will
    configure the right AWS/Cloud roles automatically.

    StandardAutoscaler's `update` method is periodically called by `monitor.py`
    to add and remove nodes as necessary. Currently, load-based autoscaling is
    not implemented, so all this class does is try to maintain a constant
    cluster size.

    StandardAutoscaler is also used to bootstrap clusters (by adding workers
    until the cluster size that can handle the resource demand is met).
    """

    def __init__(self,
                 config_path,
                 load_metrics,
                 max_launch_batch=AUTOSCALER_MAX_LAUNCH_BATCH,
                 max_concurrent_launches=AUTOSCALER_MAX_CONCURRENT_LAUNCHES,
                 max_failures=AUTOSCALER_MAX_NUM_FAILURES,
                 process_runner=subprocess,
                 update_interval_s=AUTOSCALER_UPDATE_INTERVAL_S):
        self.config_path = config_path
        # Keep this before self.reset (self.provider needs to be created
        # exactly once).
        self.provider = None
        self.resource_demand_scheduler = None
        self.reset(errors_fatal=True)
        self.head_node_ip = load_metrics.local_ip
        self.load_metrics = load_metrics

        self.max_failures = max_failures
        self.max_launch_batch = max_launch_batch
        self.max_concurrent_launches = max_concurrent_launches
        self.process_runner = process_runner

        # Map from node_id to NodeUpdater processes
        self.updaters = {}
        self.num_failed_updates = defaultdict(int)
        self.num_successful_updates = defaultdict(int)
        self.num_failures = 0
        self.last_update_time = 0.0
        self.update_interval_s = update_interval_s

        # Node launchers
        self.launch_queue = queue.Queue()
        self.pending_launches = ConcurrentCounter()
        max_batches = math.ceil(
            max_concurrent_launches / float(max_launch_batch))
        for i in range(int(max_batches)):
            node_launcher = NodeLauncher(
                provider=self.provider,
                queue=self.launch_queue,
                index=i,
                pending=self.pending_launches,
                node_types=self.available_node_types,
            )
            node_launcher.daemon = True
            node_launcher.start()

        # Expand local file_mounts to allow ~ in the paths. This can't be done
        # earlier when the config is written since we might be on different
        # platform and the expansion would result in wrong path.
        self.config["file_mounts"] = {
            remote: os.path.expanduser(local)
            for remote, local in self.config["file_mounts"].items()
        }

        for local_path in self.config["file_mounts"].values():
            assert os.path.exists(local_path)

        # List of resource bundles the user is requesting of the cluster.
        self.resource_demand_vector = []

        logger.info("StandardAutoscaler: {}".format(self.config))

    def update(self):
        try:
            self.reset(errors_fatal=False)
            self._update()
        except Exception as e:
            logger.exception("StandardAutoscaler: "
                             "Error during autoscaling.")
            if _internal_kv_initialized():
                _internal_kv_put(
                    DEBUG_AUTOSCALING_ERROR, str(e), overwrite=True)
            # Don't abort the autoscaler if the K8s API server is down.
            # https://github.com/ray-project/ray/issues/12255
            is_k8s_connection_error = (
                self.config["provider"]["type"] == "kubernetes"
                and isinstance(e, MaxRetryError))
            if not is_k8s_connection_error:
                self.num_failures += 1
            if self.num_failures > self.max_failures:
                logger.critical("StandardAutoscaler: "
                                "Too many errors, abort.")
                raise e

    def _update(self):
        now = time.time()

        # Throttle autoscaling updates to this interval to avoid exceeding
        # rate limits on API calls.
        if now - self.last_update_time < self.update_interval_s:
            return

        self.last_update_time = now
        nodes = self.workers()

        self.load_metrics.prune_active_ips([
            self.provider.internal_ip(node_id)
            for node_id in self.all_workers()
        ])
        self.log_info_string(nodes)

        # Terminate any idle or out of date nodes
        last_used = self.load_metrics.last_used_time_by_ip
        horizon = now - (60 * self.config["idle_timeout_minutes"])

        nodes_to_terminate = []
        node_type_counts = collections.defaultdict(int)
        # Sort based on last used to make sure to keep min_workers that
        # were most recently used. Otherwise, _keep_min_workers_of_node_type
        # might keep a node that should be terminated.
        sorted_node_ids = self._sort_based_on_last_used(nodes, last_used)
        # Account for request_resources().
        nodes_allowed_to_terminate = {}
        if self.resource_demand_vector:
            nodes_allowed_to_terminate = self._get_nodes_allowed_to_terminate(
                sorted_node_ids)

        for node_id in sorted_node_ids:
            # Make sure to not kill idle node types if the number of workers
            # of that type is lower/equal to the min_workers of that type
            # or it is needed for request_resources().
            if (self._keep_min_worker_of_node_type(node_id, node_type_counts)
                    or not nodes_allowed_to_terminate.get(
                        node_id, True)) and self.launch_config_ok(node_id):
                continue

            node_ip = self.provider.internal_ip(node_id)
            if node_ip in last_used and last_used[node_ip] < horizon:
                logger.info("StandardAutoscaler: "
                            "{}: Terminating idle node".format(node_id))
                nodes_to_terminate.append(node_id)
            elif not self.launch_config_ok(node_id):
                logger.info("StandardAutoscaler: "
                            "{}: Terminating outdated node".format(node_id))
                nodes_to_terminate.append(node_id)

        if nodes_to_terminate:
            self.provider.terminate_nodes(nodes_to_terminate)
            nodes = self.workers()
            self.log_info_string(nodes)

        # Terminate nodes if there are too many
        nodes_to_terminate = []
        while (len(nodes) -
               len(nodes_to_terminate)) > self.config["max_workers"] and nodes:
            to_terminate = nodes.pop()
            logger.info("StandardAutoscaler: "
                        "{}: Terminating unneeded node".format(to_terminate))
            nodes_to_terminate.append(to_terminate)

        if nodes_to_terminate:
            self.provider.terminate_nodes(nodes_to_terminate)
            nodes = self.workers()

            self.log_info_string(nodes)

        to_launch = self.resource_demand_scheduler.get_nodes_to_launch(
            self.provider.non_terminated_nodes(tag_filters={}),
            self.pending_launches.breakdown(),
            self.load_metrics.get_resource_demand_vector(),
            self.load_metrics.get_resource_utilization(),
            self.load_metrics.get_pending_placement_groups(),
            self.load_metrics.get_static_node_resources_by_ip(),
            ensure_min_cluster_size=self.resource_demand_vector)
        for node_type, count in to_launch.items():
            self.launch_new_node(count, node_type=node_type)

        nodes = self.workers()

        # Process any completed updates
        completed = []
        for node_id, updater in self.updaters.items():
            if not updater.is_alive():
                completed.append(node_id)
        if completed:
            for node_id in completed:
                if self.updaters[node_id].exitcode == 0:
                    self.num_successful_updates[node_id] += 1
                else:
                    self.num_failed_updates[node_id] += 1
                del self.updaters[node_id]
            # Mark the node as active to prevent the node recovery logic
            # immediately trying to restart Ray on the new node.
            self.load_metrics.mark_active(self.provider.internal_ip(node_id))
            nodes = self.workers()
            self.log_info_string(nodes)

        # Update nodes with out-of-date files.
        # TODO(edoakes): Spawning these threads directly seems to cause
        # problems. They should at a minimum be spawned as daemon threads.
        # See https://github.com/ray-project/ray/pull/5903 for more info.
        T = []
        for node_id, commands, ray_start, docker_config in (
                self.should_update(node_id) for node_id in nodes):
            if node_id is not None:
                resources = self._node_resources(node_id)
                logger.debug(f"{node_id}: Starting new thread runner.")
                T.append(
                    threading.Thread(
                        target=self.spawn_updater,
                        args=(node_id, commands, ray_start, resources,
                              docker_config)))
        for t in T:
            t.start()
        for t in T:
            t.join()

        # Attempt to recover unhealthy nodes
        for node_id in nodes:
            self.recover_if_needed(node_id, now)

    def _sort_based_on_last_used(self, nodes: List[NodeID],
                                 last_used: Dict[str, float]) -> List[NodeID]:
        """Sort the nodes based on the last time they were used.

        The first item in the return list is the most recently used.
        """
        updated_last_used = copy.deepcopy(last_used)
        # Add the unconnected nodes as the least recently used (the end of
        # list). This prioritizes connected nodes.
        least_recently_used = min(last_used.values())
        for node_id in nodes:
            node_ip = self.provider.internal_ip(node_id)
            if node_ip not in updated_last_used:
                updated_last_used[node_ip] = least_recently_used - 1

        def last_time_used(node_id: NodeID):
            node_ip = self.provider.internal_ip(node_id)
            return updated_last_used[node_ip]

        return sorted(nodes, key=last_time_used, reverse=True)

    def _get_nodes_allowed_to_terminate(
            self, sorted_node_ids: List[NodeID]) -> Dict[NodeID, bool]:
        """Returns the nodes allowed to terminate for request_resources().

        Args:
            sorted_node_ids: the node ids sorted based on last used (LRU last).

        Returns:
            nodes_allowed_to_terminate: whether the node id is allowed to
                terminate or not.
        """
        nodes_allowed_to_terminate = {}
        max_node_resources = []
        resource_demand_vector_node_ids = []
        # Get max resources on all the non terminated nodes.
        for node_id in sorted_node_ids:
            tags = self.provider.node_tags(node_id)
            if TAG_RAY_USER_NODE_TYPE in tags:
                node_type = tags[TAG_RAY_USER_NODE_TYPE]
                max_node_resources.append(
                    copy.deepcopy(
                        self.available_node_types[node_type]["resources"]))
                resource_demand_vector_node_ids.append(node_id)
        # Since it is sorted based on last used, we "keep" nodes that are
        # most recently used when we binpack.
        unfulfilled_resource_requests, used_resource_requests = \
            get_bin_pack_residual(
                max_node_resources, self.resource_demand_vector)
        for i, node_id in enumerate(resource_demand_vector_node_ids):
            if unfulfilled_resource_requests:
                nodes_allowed_to_terminate[node_id] = False
            elif used_resource_requests[i] != max_node_resources[i]:
                nodes_allowed_to_terminate[node_id] = False
            else:
                nodes_allowed_to_terminate[node_id] = True
        return nodes_allowed_to_terminate

    def _keep_min_worker_of_node_type(
            self, node_id: NodeID,
            node_type_counts: Dict[NodeType, int]) -> bool:
        """Returns if workers of node_type can be terminated.
        The worker cannot be terminated to respect min_workers constraint.

        Receives the counters of running nodes so far and determines if idle
        node_id should be terminated or not. It also updates the counters
        (node_type_counts), which is returned by reference.

        Args:
            node_type_counts(Dict[NodeType, int]): The non_terminated node
                types counted so far.
        Returns:
            bool: if workers of node_types can be terminated or not.
        """
        tags = self.provider.node_tags(node_id)
        if TAG_RAY_USER_NODE_TYPE in tags:
            node_type = tags[TAG_RAY_USER_NODE_TYPE]
            node_type_counts[node_type] += 1
            min_workers = self.available_node_types[node_type].get(
                "min_workers", 0)
            max_workers = self.available_node_types[node_type].get(
                "max_workers", 0)
            if node_type_counts[node_type] <= min(min_workers, max_workers):
                return True

        return False

    def _node_resources(self, node_id):
        node_type = self.provider.node_tags(node_id).get(
            TAG_RAY_USER_NODE_TYPE)
        if self.available_node_types:
            return self.available_node_types.get(node_type, {}).get(
                "resources", {})
        else:
            return {}

    def reset(self, errors_fatal=False):
        sync_continuously = False
        if hasattr(self, "config"):
            sync_continuously = self.config.get(
                "file_mounts_sync_continuously", False)
        try:
            with open(self.config_path) as f:
                new_config = yaml.safe_load(f.read())
            if new_config != getattr(self, "config", None):
                try:
                    validate_config(new_config)
                except Exception as e:
                    logger.debug(
                        "Cluster config validation failed. The version of "
                        "the ray CLI you launched this cluster with may "
                        "be higher than the version of ray being run on "
                        "the cluster. Some new features may not be "
                        "available until you upgrade ray on your cluster.",
                        exc_info=e)
            (new_runtime_hash,
             new_file_mounts_contents_hash) = hash_runtime_conf(
                 new_config["file_mounts"],
                 new_config["cluster_synced_files"],
                 [
                     new_config["worker_setup_commands"],
                     new_config["worker_start_ray_commands"],
                 ],
                 generate_file_mounts_contents_hash=sync_continuously,
             )
            self.config = new_config
            self.runtime_hash = new_runtime_hash
            self.file_mounts_contents_hash = new_file_mounts_contents_hash
            if not self.provider:
                self.provider = _get_node_provider(self.config["provider"],
                                                   self.config["cluster_name"])

            self.available_node_types = self.config["available_node_types"]
            upscaling_speed = self.config.get("upscaling_speed")
            aggressive = self.config.get("autoscaling_mode") == "aggressive"
            target_utilization_fraction = self.config.get(
                "target_utilization_fraction")
            if upscaling_speed:
                upscaling_speed = float(upscaling_speed)
            # TODO(ameer): consider adding (if users ask) an option of
            # initial_upscaling_num_workers.
            elif aggressive:
                upscaling_speed = 99999
                logger.warning(
                    "Legacy aggressive autoscaling mode "
                    "detected. Replacing it by setting upscaling_speed to "
                    "99999.")
            elif target_utilization_fraction:
                upscaling_speed = (
                    1 / max(target_utilization_fraction, 0.001) - 1)
                logger.warning(
                    "Legacy target_utilization_fraction config "
                    "detected. Replacing it by setting upscaling_speed to " +
                    "1 / target_utilization_fraction - 1.")
            else:
                upscaling_speed = 1.0
            if self.resource_demand_scheduler:
                # The node types are autofilled internally for legacy yamls,
                # overwriting the class will remove the inferred node resources
                # for legacy yamls.
                self.resource_demand_scheduler.reset_config(
                    self.provider, self.available_node_types,
                    self.config["max_workers"], upscaling_speed)
            else:
                self.resource_demand_scheduler = ResourceDemandScheduler(
                    self.provider, self.available_node_types,
                    self.config["max_workers"], upscaling_speed)

        except Exception as e:
            if errors_fatal:
                raise e
            else:
                logger.exception("StandardAutoscaler: "
                                 "Error parsing config.")

    def launch_config_ok(self, node_id):
        node_tags = self.provider.node_tags(node_id)
        tag_launch_conf = node_tags.get(TAG_RAY_LAUNCH_CONFIG)
        node_type = node_tags.get(TAG_RAY_USER_NODE_TYPE)

        launch_config = copy.deepcopy(self.config["worker_nodes"])
        if node_type:
            launch_config.update(
                self.config["available_node_types"][node_type]["node_config"])
        calculated_launch_hash = hash_launch_conf(launch_config,
                                                  self.config["auth"])

        if calculated_launch_hash != tag_launch_conf:
            return False
        return True

    def files_up_to_date(self, node_id):
        node_tags = self.provider.node_tags(node_id)
        applied_config_hash = node_tags.get(TAG_RAY_RUNTIME_CONFIG)
        applied_file_mounts_contents_hash = node_tags.get(
            TAG_RAY_FILE_MOUNTS_CONTENTS)
        if (applied_config_hash != self.runtime_hash
                or (self.file_mounts_contents_hash is not None
                    and self.file_mounts_contents_hash !=
                    applied_file_mounts_contents_hash)):
            logger.info("StandardAutoscaler: "
                        "{}: Runtime state is ({},{}), want ({},{})".format(
                            node_id, applied_config_hash,
                            applied_file_mounts_contents_hash,
                            self.runtime_hash, self.file_mounts_contents_hash))
            return False
        return True

    def recover_if_needed(self, node_id, now):
        if not self.can_update(node_id):
            return
        key = self.provider.internal_ip(node_id)
        if key not in self.load_metrics.last_heartbeat_time_by_ip:
            self.load_metrics.last_heartbeat_time_by_ip[key] = now
        last_heartbeat_time = self.load_metrics.last_heartbeat_time_by_ip[key]
        delta = now - last_heartbeat_time
        if delta < AUTOSCALER_HEARTBEAT_TIMEOUT_S:
            return
        logger.warning("StandardAutoscaler: "
                       "{}: No heartbeat in {}s, "
                       "restarting Ray to recover...".format(node_id, delta))
        updater = NodeUpdaterThread(
            node_id=node_id,
            provider_config=self.config["provider"],
            provider=self.provider,
            auth_config=self.config["auth"],
            cluster_name=self.config["cluster_name"],
            file_mounts={},
            initialization_commands=[],
            setup_commands=[],
            ray_start_commands=with_head_node_ip(
                self.config["worker_start_ray_commands"], self.head_node_ip),
            runtime_hash=self.runtime_hash,
            file_mounts_contents_hash=self.file_mounts_contents_hash,
            process_runner=self.process_runner,
            use_internal_ip=True,
            is_head_node=False,
            docker_config=self.config.get("docker"),
            node_resources=self._node_resources(node_id))
        updater.start()
        self.updaters[node_id] = updater

    def _get_node_type_specific_fields(self, node_id: str,
                                       fields_key: str) -> Any:
        fields = self.config[fields_key]
        node_tags = self.provider.node_tags(node_id)
        if TAG_RAY_USER_NODE_TYPE in node_tags:
            node_type = node_tags[TAG_RAY_USER_NODE_TYPE]
            if node_type not in self.available_node_types:
                raise ValueError(f"Unknown node type tag: {node_type}.")
            node_specific_config = self.available_node_types[node_type]
            if fields_key in node_specific_config:
                fields = node_specific_config[fields_key]
        return fields

    def _get_node_specific_docker_config(self, node_id):
        if "docker" not in self.config:
            return {}
        docker_config = copy.deepcopy(self.config.get("docker", {}))
        node_specific_docker = self._get_node_type_specific_fields(
            node_id, "docker")
        docker_config.update(node_specific_docker)
        return docker_config

    def should_update(self, node_id):
        if not self.can_update(node_id):
            return UpdateInstructions(None, None, None, None)  # no update

        status = self.provider.node_tags(node_id).get(TAG_RAY_NODE_STATUS)
        if status == STATUS_UP_TO_DATE and self.files_up_to_date(node_id):
            return UpdateInstructions(None, None, None, None)  # no update

        successful_updated = self.num_successful_updates.get(node_id, 0) > 0
        if successful_updated and self.config.get("restart_only", False):
            init_commands = []
            ray_commands = self.config["worker_start_ray_commands"]
        elif successful_updated and self.config.get("no_restart", False):
            init_commands = self._get_node_type_specific_fields(
                node_id, "worker_setup_commands")
            ray_commands = []
        else:
            init_commands = self._get_node_type_specific_fields(
                node_id, "worker_setup_commands")
            ray_commands = self.config["worker_start_ray_commands"]

        docker_config = self._get_node_specific_docker_config(node_id)
        return UpdateInstructions(
            node_id=node_id,
            init_commands=init_commands,
            start_ray_commands=ray_commands,
            docker_config=docker_config)

    def spawn_updater(self, node_id, init_commands, ray_start_commands,
                      node_resources, docker_config):
        logger.info(f"Creating new (spawn_updater) updater thread for node"
                    f" {node_id}.")
        updater = NodeUpdaterThread(
            node_id=node_id,
            provider_config=self.config["provider"],
            provider=self.provider,
            auth_config=self.config["auth"],
            cluster_name=self.config["cluster_name"],
            file_mounts=self.config["file_mounts"],
            initialization_commands=with_head_node_ip(
                self._get_node_type_specific_fields(
                    node_id, "initialization_commands"), self.head_node_ip),
            setup_commands=with_head_node_ip(init_commands, self.head_node_ip),
            ray_start_commands=with_head_node_ip(ray_start_commands,
                                                 self.head_node_ip),
            runtime_hash=self.runtime_hash,
            file_mounts_contents_hash=self.file_mounts_contents_hash,
            is_head_node=False,
            cluster_synced_files=self.config["cluster_synced_files"],
            rsync_options={
                "rsync_exclude": self.config.get("rsync_exclude"),
                "rsync_filter": self.config.get("rsync_filter")
            },
            process_runner=self.process_runner,
            use_internal_ip=True,
            docker_config=docker_config,
            node_resources=node_resources)
        updater.start()
        self.updaters[node_id] = updater

    def can_update(self, node_id):
        if node_id in self.updaters:
            return False
        if not self.launch_config_ok(node_id):
            return False
        if self.num_failed_updates.get(node_id, 0) > 0:  # TODO(ekl) retry?
            return False
        logger.debug(f"{node_id} is not being updated and "
                     "passes config check (can_update=True).")
        return True

    def launch_new_node(self, count: int, node_type: Optional[str]) -> None:
        logger.info(
            "StandardAutoscaler: Queue {} new nodes for launch".format(count))
        self.pending_launches.inc(node_type, count)
        config = copy.deepcopy(self.config)
        # Split into individual launch requests of the max batch size.
        while count > 0:
            self.launch_queue.put((config, min(count, self.max_launch_batch),
                                   node_type))
            count -= self.max_launch_batch

    def all_workers(self):
        return self.workers() + self.unmanaged_workers()

    def workers(self):
        return self.provider.non_terminated_nodes(
            tag_filters={TAG_RAY_NODE_KIND: NODE_KIND_WORKER})

    def unmanaged_workers(self):
        return self.provider.non_terminated_nodes(
            tag_filters={TAG_RAY_NODE_KIND: NODE_KIND_UNMANAGED})

    def log_info_string(self, nodes):
        tmp = "Cluster status: "
        tmp += self.info_string(nodes)
        tmp += "\n"
        tmp += self.load_metrics.info_string()
        tmp += "\n"
        tmp += self.resource_demand_scheduler.debug_string(
            nodes, self.pending_launches.breakdown(),
            self.load_metrics.get_resource_utilization())
        if _internal_kv_initialized():
            _internal_kv_put(DEBUG_AUTOSCALING_STATUS, tmp, overwrite=True)
        logger.debug(tmp)

    def info_string(self, nodes):
        suffix = ""
        if self.updaters:
            suffix += " ({} updating)".format(len(self.updaters))
        if self.num_failed_updates:
            suffix += " ({} failed to update)".format(
                len(self.num_failed_updates))

        return "{} nodes{}".format(len(nodes), suffix)

    def request_resources(self, resources: List[dict]):
        """Called by monitor to request resources.

        Args:
            resources: A list of resource bundles.
        """
        if resources:
            logger.info(
                "StandardAutoscaler: resource_requests={}".format(resources))
        assert isinstance(resources, list), resources
        self.resource_demand_vector = resources

    def kill_workers(self):
        logger.error("StandardAutoscaler: kill_workers triggered")
        nodes = self.workers()
        if nodes:
            self.provider.terminate_nodes(nodes)
        logger.error("StandardAutoscaler: terminated {} node(s)".format(
            len(nodes)))
