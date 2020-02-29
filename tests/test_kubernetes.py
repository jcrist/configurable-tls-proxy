from copy import deepcopy

import pytest

kubernetes_asyncio = pytest.importorskip("kubernetes_asyncio")

from dask_gateway_server.utils import FrozenAttrDict
from dask_gateway_server.backends.kubernetes.backend import (
    KubeClusterConfig,
    KubeBackend,
)
from dask_gateway_server.backends.kubernetes.controller import KubeController
from dask_gateway_server.backends.kubernetes.utils import merge_json_objects


@pytest.mark.parametrize(
    "a,b,sol",
    [
        (
            {"a": {"b": {"c": 1}}},
            {"a": {"b": {"d": 2}}},
            {"a": {"b": {"c": 1, "d": 2}}},
        ),
        ({"a": {"b": {"c": 1}}}, {"a": {"b": 2}}, {"a": {"b": 2}}),
        ({"a": [1, 2]}, {"a": [3, 4]}, {"a": [1, 2, 3, 4]}),
        ({"a": {"b": 1}}, {"a2": 3}, {"a": {"b": 1}, "a2": 3}),
        ({"a": 1}, {}, {"a": 1}),
    ],
)
def test_merge_json_objects(a, b, sol):
    a_orig = deepcopy(a)
    b_orig = deepcopy(b)
    res = merge_json_objects(a, b)
    assert res == sol
    assert a == a_orig
    assert b == b_orig


def test_make_cluster_object():
    backend = KubeBackend(gateway_instance="instance-1234")

    config = KubeClusterConfig()
    options = {"somekey": "someval"}

    obj = backend.make_cluster_object("alice", options, config)

    name = obj["metadata"]["name"]
    labels = obj["metadata"]["labels"]
    assert labels["gateway.dask.org/instance"] == "instance-1234"
    assert labels["gateway.dask.org/user"] == "alice"
    assert labels["gateway.dask.org/cluster"] == name

    spec = obj["spec"]
    sol = {"options": options, "config": config.to_dict()}
    assert spec == sol


def example_config():
    sched_tol = {
        "key": "foo",
        "operator": "Equal",
        "value": "bar",
        "effect": "NoSchedule",
    }
    worker_tol = {
        "key": "foo",
        "operator": "Equal",
        "value": "baz",
        "effect": "NoSchedule",
    }
    kwargs = {
        "namespace": "mynamespace",
        "worker_extra_pod_config": {"tolerations": [worker_tol]},
        "worker_extra_container_config": {"workingDir": "/worker"},
        "worker_memory": "4G",
        "worker_memory_limit": "6G",
        "worker_cores": 2,
        "worker_cores_limit": 3,
        "scheduler_extra_pod_config": {"tolerations": [sched_tol]},
        "scheduler_extra_container_config": {"workingDir": "/scheduler"},
        "scheduler_memory": "2G",
        "scheduler_memory_limit": "3G",
        "scheduler_cores": 1,
        "scheduler_cores_limit": 2,
    }
    return FrozenAttrDict(KubeClusterConfig(**kwargs).to_dict())


@pytest.mark.parametrize("is_worker", [False, True])
def test_make_pod(is_worker):
    controller = KubeController(
        gateway_instance="instance-1234", api_url="http://example.com/api"
    )

    config = example_config()
    namespace = config.namespace
    cluster_name = "c1234"
    username = "alice"

    pod = controller.make_pod(
        namespace, cluster_name, username, config, is_worker=is_worker
    )

    if is_worker:
        component = "dask-worker"
        tolerations = config.worker_extra_pod_config["tolerations"]
        workdir = "/worker"
        resources = {
            "limits": {"cpu": "3.0", "memory": str(6 * 2 ** 30)},
            "requests": {"cpu": "2.0", "memory": str(4 * 2 ** 30)},
        }
    else:
        component = "dask-scheduler"
        tolerations = config.scheduler_extra_pod_config["tolerations"]
        workdir = "/scheduler"
        resources = {
            "limits": {"cpu": "2.0", "memory": str(3 * 2 ** 30)},
            "requests": {"cpu": "1.0", "memory": str(2 * 2 ** 30)},
        }

    labels = pod["metadata"]["labels"]
    assert labels["gateway.dask.org/instance"] == "instance-1234"
    assert labels["gateway.dask.org/cluster"] == cluster_name
    assert labels["gateway.dask.org/user"] == username
    assert labels["app.kubernetes.io/component"] == component

    assert pod["spec"]["tolerations"] == tolerations
    container = pod["spec"]["containers"][0]
    assert container["workingDir"] == workdir

    assert container["resources"] == resources


def test_make_secret():
    controller = KubeController(
        gateway_instance="instance-1234", api_url="http://example.com/api"
    )

    cluster_name = "c1234"
    username = "alice"

    secret = controller.make_secret(cluster_name, username)

    labels = secret["metadata"]["labels"]
    assert labels["gateway.dask.org/instance"] == "instance-1234"
    assert labels["gateway.dask.org/cluster"] == cluster_name
    assert labels["gateway.dask.org/user"] == username
    assert labels["app.kubernetes.io/component"] == "credentials"

    assert set(secret["data"].keys()) == {"dask.crt", "dask.pem", "api-token"}


def test_make_service():
    controller = KubeController(
        gateway_instance="instance-1234", api_url="http://example.com/api"
    )

    cluster_name = "c1234"
    username = "alice"

    service = controller.make_service(cluster_name, username)

    labels = service["metadata"]["labels"]
    assert labels["gateway.dask.org/instance"] == "instance-1234"
    assert labels["gateway.dask.org/cluster"] == cluster_name
    assert labels["gateway.dask.org/user"] == username
    assert labels["app.kubernetes.io/component"] == "dask-scheduler"

    selector = service["spec"]["selector"]
    assert selector["gateway.dask.org/cluster"] == cluster_name
    assert selector["gateway.dask.org/instance"] == "instance-1234"
    assert selector["app.kubernetes.io/component"] == "dask-scheduler"


def test_make_ingressroute():
    middlewares = [{"name": "my-middleware"}]

    controller = KubeController(
        gateway_instance="instance-1234",
        api_url="http://example.com/api",
        proxy_prefix="/foo/bar",
        proxy_web_middlewares=middlewares,
    )

    cluster_name = "c1234"
    username = "alice"
    namespace = "mynamespace"

    ingress = controller.make_ingressroute(cluster_name, username, namespace)

    labels = ingress["metadata"]["labels"]
    assert labels["gateway.dask.org/instance"] == "instance-1234"
    assert labels["gateway.dask.org/cluster"] == cluster_name
    assert labels["gateway.dask.org/user"] == username
    assert labels["app.kubernetes.io/component"] == "dask-scheduler"

    route = ingress["spec"]["routes"][0]
    assert route["middlewares"] == middlewares
    assert (
        route["match"] == f"PathPrefix(`/foo/bar/clusters/{namespace}.{cluster_name}/`)"
    )


def test_make_ingressroutetcp():
    controller = KubeController(
        gateway_instance="instance-1234", api_url="http://example.com/api"
    )

    cluster_name = "c1234"
    username = "alice"
    namespace = "mynamespace"

    ingress = controller.make_ingressroutetcp(cluster_name, username, namespace)

    labels = ingress["metadata"]["labels"]
    assert labels["gateway.dask.org/instance"] == "instance-1234"
    assert labels["gateway.dask.org/cluster"] == cluster_name
    assert labels["gateway.dask.org/user"] == username
    assert labels["app.kubernetes.io/component"] == "dask-scheduler"

    route = ingress["spec"]["routes"][0]
    assert route["match"] == f"HostSNI(`daskgateway-{namespace}.{cluster_name}`)"
    assert ingress["spec"]["tls"]["passthrough"]
