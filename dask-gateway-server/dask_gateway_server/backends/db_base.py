import asyncio
import base64
import json
import os
import time
import uuid
from collections import defaultdict

import sqlalchemy as sa
from traitlets import Unicode, Bool, List, Integer, Float, validate, default
from cryptography.fernet import MultiFernet, Fernet

from .base import Backend
from .. import models
from ..tls import new_keypair
from ..utils import FrozenAttrDict


def timestamp():
    """An integer timestamp represented as milliseconds since the epoch UTC"""
    return int(time.time() * 1000)


def _normalize_encrypt_key(key):
    if isinstance(key, str):
        key = key.encode("ascii")

    if len(key) == 44:
        try:
            key = base64.urlsafe_b64decode(key)
        except ValueError:
            pass

    if len(key) == 32:
        return base64.urlsafe_b64encode(key)

    raise ValueError(
        "All keys in `db_encrypt_keys`/`DASK_GATEWAY_ENCRYPT_KEYS` must be 32 "
        "bytes, base64-encoded"
    )


def _is_in_memory_db(url):
    return url in ("sqlite://", "sqlite:///:memory:")


class _IntEnum(sa.TypeDecorator):
    impl = sa.Integer

    def __init__(self, enumclass, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._enumclass = enumclass

    def process_bind_param(self, value, dialect):
        return value.value

    def process_result_value(self, value, dialect):
        return self._enumclass(value)


class _JSON(sa.TypeDecorator):
    "Represents an immutable structure as a json-encoded string."

    impl = sa.LargeBinary

    def process_bind_param(self, value, dialect):
        if value is not None:
            value = json.dumps(value).encode("utf-8")
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            value = json.loads(value)
        return value


class JobStatus(models.IntEnum):
    CREATED = 1
    SUBMITTED = 2
    RUNNING = 3
    STOPPED = 4
    FAILED = 5


class Cluster(object):
    def __init__(
        self,
        id=None,
        name=None,
        username=None,
        token=None,
        options=None,
        config=None,
        status=None,
        target=None,
        state=None,
        scheduler_address="",
        dashboard_address="",
        api_address="",
        tls_cert=b"",
        tls_key=b"",
        start_time=None,
        stop_time=None,
    ):
        self.id = id
        self.name = name
        self.username = username
        self.token = token
        self.options = options
        self.config = config
        self.status = status
        self.target = target
        self.state = state
        self.scheduler_address = scheduler_address
        self.dashboard_address = dashboard_address
        self.api_address = api_address
        self.tls_cert = tls_cert
        self.tls_key = tls_key
        self.start_time = start_time
        self.stop_time = stop_time
        self.workers = {}

    _status_map = {
        (JobStatus.CREATED, JobStatus.RUNNING): models.ClusterStatus.PENDING,
        (JobStatus.CREATED, JobStatus.STOPPED): models.ClusterStatus.STOPPING,
        (JobStatus.CREATED, JobStatus.FAILED): models.ClusterStatus.STOPPING,
        (JobStatus.SUBMITTED, JobStatus.RUNNING): models.ClusterStatus.PENDING,
        (JobStatus.SUBMITTED, JobStatus.STOPPED): models.ClusterStatus.STOPPING,
        (JobStatus.SUBMITTED, JobStatus.FAILED): models.ClusterStatus.STOPPING,
        (JobStatus.RUNNING, JobStatus.RUNNING): models.ClusterStatus.RUNNING,
        (JobStatus.RUNNING, JobStatus.STOPPED): models.ClusterStatus.STOPPING,
        (JobStatus.RUNNING, JobStatus.FAILED): models.ClusterStatus.STOPPING,
        (JobStatus.STOPPED, JobStatus.STOPPED): models.ClusterStatus.STOPPED,
        (JobStatus.FAILED, JobStatus.FAILED): models.ClusterStatus.FAILED,
    }

    def active_workers(self):
        return [w for w in self.workers.values() if w.is_active()]

    def is_active(self):
        return self.target < JobStatus.STOPPED

    @property
    def model_status(self):
        return self._status_map[self.status, self.target]

    def to_model(self):
        return models.Cluster(
            name=self.name,
            username=self.username,
            options=self.options,
            status=self.model_status,
            scheduler_address=self.scheduler_address,
            dashboard_address=self.dashboard_address,
            api_address=self.api_address,
            tls_cert=self.tls_cert,
            tls_key=self.tls_key,
            start_time=self.start_time,
            stop_time=self.stop_time,
        )


class Worker(object):
    def __init__(
        self,
        id=None,
        name=None,
        cluster=None,
        status=None,
        target=None,
        state=None,
        start_time=None,
        stop_time=None,
    ):
        self.id = id
        self.name = name
        self.cluster = cluster
        self.status = status
        self.target = target
        self.state = state
        self.start_time = start_time
        self.stop_time = stop_time

    def is_active(self):
        return self.target < JobStatus.STOPPED


metadata = sa.MetaData()

clusters = sa.Table(
    "clusters",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Unicode(255), nullable=False, unique=True),
    sa.Column("username", sa.Unicode(255), nullable=False),
    sa.Column("status", _IntEnum(JobStatus), nullable=False),
    sa.Column("target", _IntEnum(JobStatus), nullable=False),
    sa.Column("options", _JSON, nullable=False),
    sa.Column("config", _JSON, nullable=False),
    sa.Column("state", _JSON, nullable=False),
    sa.Column("token", sa.BINARY(140), nullable=False, unique=True),
    sa.Column("scheduler_address", sa.Unicode(255), nullable=False),
    sa.Column("dashboard_address", sa.Unicode(255), nullable=False),
    sa.Column("api_address", sa.Unicode(255), nullable=False),
    sa.Column("tls_credentials", sa.LargeBinary, nullable=False),
    sa.Column("start_time", sa.Integer, nullable=False),
    sa.Column("stop_time", sa.Integer, nullable=True),
)

workers = sa.Table(
    "workers",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Unicode(255), nullable=False),
    sa.Column(
        "cluster_id", sa.ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("status", _IntEnum(JobStatus), nullable=False),
    sa.Column("target", _IntEnum(JobStatus), nullable=False),
    sa.Column("state", _JSON, nullable=False),
    sa.Column("start_time", sa.Integer, nullable=False),
    sa.Column("stop_time", sa.Integer, nullable=True),
)


class DataManager(object):
    """Holds the internal state for a single Dask Gateway.

    Keeps the memory representation in-sync with the database.
    """

    def __init__(self, url="sqlite:///:memory:", encrypt_keys=(), **kwargs):
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}

        if _is_in_memory_db(url):
            kwargs["poolclass"] = sa.pool.StaticPool
            self.fernet = None
        else:
            self.fernet = MultiFernet([Fernet(key) for key in encrypt_keys])

        engine = sa.create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            # Register PRAGMA foreigh_keys=on for sqlite
            @sa.event.listens_for(engine, "connect")
            def connect(dbapi_con, con_record):
                cursor = dbapi_con.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        metadata.create_all(engine)

        self.db = engine

        self.username_to_clusters = defaultdict(dict)
        self.token_to_cluster = {}
        self.name_to_cluster = {}
        self.id_to_cluster = {}

        # Load all existing clusters into memory
        for c in self.db.execute(clusters.select()):
            tls_cert, tls_key = self.decode_tls_credentials(c.tls_credentials)
            token = self.decode_token(c.token)
            cluster = Cluster(
                id=c.id,
                name=c.name,
                username=c.username,
                token=token,
                options=c.options,
                config=FrozenAttrDict(c.config),
                status=c.status,
                target=c.target,
                state=c.state,
                scheduler_address=c.scheduler_address,
                dashboard_address=c.dashboard_address,
                api_address=c.api_address,
                tls_cert=tls_cert,
                tls_key=tls_key,
                start_time=c.start_time,
                stop_time=c.stop_time,
            )
            self.username_to_clusters[cluster.username][cluster.name] = cluster
            self.id_to_cluster[cluster.id] = cluster
            self.token_to_cluster[cluster.token] = cluster
            self.name_to_cluster[cluster.name] = cluster

        # Next load all existing workers into memory
        for w in self.db.execute(workers.select()):
            cluster = self.id_to_cluster[w.cluster_id]
            worker = Worker(
                id=w.id,
                name=w.name,
                status=w.status,
                target=w.target,
                cluster=cluster,
                state=w.state,
                start_time=w.start_time,
                stop_time=w.stop_time,
            )
            cluster.workers[worker.name] = worker

    def cleanup_expired(self, max_age_in_seconds):
        cutoff = timestamp() - max_age_in_seconds * 1000
        with self.db.begin() as conn:
            to_delete = conn.execute(
                sa.select([clusters.c.id]).where(clusters.c.stop_time < cutoff)
            ).fetchall()

            if to_delete:
                to_delete = [i for i, in to_delete]

                conn.execute(
                    clusters.delete().where(clusters.c.id == sa.bindparam("id")),
                    [{"id": i} for i in to_delete],
                )

                for i in to_delete:
                    cluster = self.id_to_cluster.pop(i)
                    del self.token_to_cluster[cluster.token]
                    del self.name_to_cluster[cluster.name]
                    del cluster.user.clusters[cluster.name]

        return len(to_delete)

    def encrypt(self, b):
        """Encrypt bytes ``b``. If encryption is disabled this is a no-op"""
        return b if self.fernet is None else self.fernet.encrypt(b)

    def decrypt(self, b):
        """Decrypt bytes ``b``. If encryption is disabled this is a no-op"""
        return b if self.fernet is None else self.fernet.decrypt(b)

    def encode_tls_credentials(self, tls_cert, tls_key):
        return self.encrypt(b";".join((tls_cert, tls_key)))

    def decode_tls_credentials(self, data):
        return self.decrypt(data).split(b";")

    def encode_token(self, token):
        return self.encrypt(token.encode("utf8"))

    def decode_token(self, data):
        return self.decrypt(data).decode()

    def get_cluster(self, cluster_name):
        return self.name_to_cluster.get(cluster_name)

    def list_clusters(self, username=None, statuses=None):
        if statuses is None:
            select = lambda x: x.is_active()
        else:
            statuses = set(statuses)
            select = lambda x: x.model_status in statuses
        if username is None:
            return [
                cluster for cluster in self.name_to_cluster.values() if select(cluster)
            ]
        else:
            clusters = self.username_to_clusters.get(username)
            if clusters is None:
                return []
            return [cluster for cluster in clusters.values() if select(cluster)]

    def cluster_from_token(self, token):
        """Lookup a cluster from a token"""
        return self.token_to_cluster.get(token)

    def cluster_from_name(self, name):
        """Lookup a cluster by name"""
        return self.name_to_cluster.get(name)

    def active_clusters(self):
        for user in self.username_to_user.values():
            for cluster in user.clusters.values():
                if cluster.is_active():
                    yield cluster

    def create_cluster(self, username, options, config):
        """Create a new cluster for a user"""
        cluster_name = uuid.uuid4().hex
        token = uuid.uuid4().hex
        tls_cert, tls_key = new_keypair(cluster_name)
        # Encode the tls credentials for storing in the database
        tls_credentials = self.encode_tls_credentials(tls_cert, tls_key)
        enc_token = self.encode_token(token)

        common = {
            "name": cluster_name,
            "username": username,
            "options": options,
            "status": JobStatus.CREATED,
            "target": JobStatus.RUNNING,
            "state": {},
            "scheduler_address": "",
            "dashboard_address": "",
            "api_address": "",
            "start_time": timestamp(),
        }

        with self.db.begin() as conn:
            res = conn.execute(
                clusters.insert().values(
                    tls_credentials=tls_credentials,
                    token=enc_token,
                    config=config,
                    **common,
                )
            )
            cluster = Cluster(
                id=res.inserted_primary_key[0],
                token=token,
                tls_cert=tls_cert,
                tls_key=tls_key,
                config=FrozenAttrDict(config),
                **common,
            )
            self.id_to_cluster[cluster.id] = cluster
            self.token_to_cluster[token] = cluster
            self.name_to_cluster[cluster_name] = cluster
            self.username_to_clusters[username][cluster_name] = cluster

        return cluster

    def create_worker(self, cluster):
        """Create a new worker for a cluster"""
        worker_name = uuid.uuid4().hex

        common = {
            "name": worker_name,
            "status": JobStatus.CREATED,
            "target": JobStatus.RUNNING,
            "state": {},
            "start_time": timestamp(),
        }

        with self.db.begin() as conn:
            res = conn.execute(workers.insert().values(cluster_id=cluster.id, **common))
            worker = Worker(id=res.inserted_primary_key[0], cluster=cluster, **common)
            cluster.workers[worker.name] = worker

        return worker

    def update_cluster(self, cluster, **kwargs):
        """Update a cluster's state"""
        with self.db.begin() as conn:
            conn.execute(
                clusters.update().where(clusters.c.id == cluster.id).values(**kwargs)
            )
            for k, v in kwargs.items():
                setattr(cluster, k, v)

    def update_worker(self, worker, **kwargs):
        """Update a worker's state"""
        with self.db.begin() as conn:
            conn.execute(
                workers.update().where(workers.c.id == worker.id).values(**kwargs)
            )
            for k, v in kwargs.items():
                setattr(worker, k, v)


class UniqueQueue(asyncio.Queue):
    """A queue that may only contain each item once."""

    def __init__(self, maxsize=0, *, loop=None):
        super().__init__(maxsize=maxsize, loop=loop)
        self._items = set()

    def _put(self, item):
        if item not in self._items:
            self._items.add(item)
            super()._put(item)

    def _get(self):
        item = super()._get()
        self._items.discard(item)
        return item


class DatabaseBackend(Backend):
    db_url = Unicode(
        "sqlite:///:memory:",
        help="""
        The URL for the database. Default is in-memory only.

        If not in-memory, ``db_encrypt_keys`` must also be set.
        """,
        config=True,
    )

    db_encrypt_keys = List(
        help="""
        A list of keys to use to encrypt private data in the database. Can also
        be set by the environment variable ``DASK_GATEWAY_ENCRYPT_KEYS``, where
        the value is a ``;`` delimited string of encryption keys.

        Each key should be a base64-encoded 32 byte value, and should be
        cryptographically random. Lacking other options, openssl can be used to
        generate a single key via:

        .. code-block:: shell

            $ openssl rand -base64 32

        A single key is valid, multiple keys can be used to support key rotation.
        """,
        config=True,
    )

    @default("db_encrypt_keys")
    def _db_encrypt_keys_default(self):
        keys = os.environb.get(b"DASK_GATEWAY_ENCRYPT_KEYS", b"").strip()
        if not keys:
            return []
        return [_normalize_encrypt_key(k) for k in keys.split(b";") if k.strip()]

    @validate("db_encrypt_keys")
    def _db_encrypt_keys_validate(self, proposal):
        if not proposal.value and not _is_in_memory_db(self.db_url):
            raise ValueError(
                "Must configure `db_encrypt_keys`/`DASK_GATEWAY_ENCRYPT_KEYS` "
                "when not using an in-memory database"
            )
        return [_normalize_encrypt_key(k) for k in proposal.value]

    db_debug = Bool(
        False, help="If True, all database operations will be logged", config=True
    )

    db_cleanup_period = Float(
        600,
        help="""
        Time (in seconds) between database cleanup tasks.

        This sets how frequently old records are removed from the database.
        This shouldn't be too small (to keep the overhead low), but should be
        smaller than ``db_record_max_age`` (probably by an order of magnitude).
        """,
        config=True,
    )

    db_cluster_max_age = Float(
        3600 * 24,
        help="""
        Max time (in seconds) to keep around records of completed clusters.

        Every ``db_cleanup_period``, completed clusters older than
        ``db_cluster_max_age`` are removed from the database.
        """,
        config=True,
    )

    parallelism = Integer(
        20,
        help="""
        Number of handlers to use for starting/stopping clusters.
        """,
        config=True,
    )

    async def startup(self):
        self.db = DataManager(
            url=self.db_url, echo=self.db_debug, encrypt_keys=self.db_encrypt_keys
        )
        self.queues = [UniqueQueue() for _ in range(self.parallelism)]
        self.tasks = [
            asyncio.ensure_future(self.reconciler_loop(q)) for q in self.queues
        ]
        await self.handle_setup()

    async def shutdown(self):
        await self.handle_cleanup()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def list_clusters(self, user=None, statuses=None):
        clusters = self.db.list_clusters(username=user.name, statuses=statuses)
        return [c.to_model() for c in clusters]

    async def start_cluster(self, user, cluster_options):
        options, config = await self.process_cluster_options(user, cluster_options)
        cluster = self.db.create_cluster(user.name, options, config)
        await self.enqueue(cluster)
        return cluster.name

    async def get_cluster(self, cluster_name):
        cluster = self.db.get_cluster(cluster_name)
        return None if cluster is None else cluster.to_model()

    async def stop_cluster(self, cluster_name):
        cluster = self.db.get_cluster(cluster_name)
        if cluster is None:
            return
        if cluster.target <= JobStatus.RUNNING:
            self.db.update_cluster(cluster, target=JobStatus.STOPPED)
        if cluster.status <= JobStatus.RUNNING:
            await self.enqueue(cluster)

    async def enqueue(self, obj):
        ind = hash(obj) % self.parallelism
        await self.queues[ind].put(obj)

    async def reconciler_loop(self, queue):
        while True:
            obj = await queue.get()
            try:
                await self.reconcile(obj)
            except Exception:
                await self.enqueue(obj)

    async def reconcile(self, cluster):
        if cluster.status == JobStatus.CREATED:
            if cluster.target == JobStatus.RUNNING:
                await self._start_cluster(cluster)
            elif cluster.target in (JobStatus.STOPPED, JobStatus.FAILED):
                await self._stop_cluster(cluster, cluster.target)

        elif cluster.status == JobStatus.SUBMITTED:
            if cluster.target == JobStatus.RUNNING:
                if cluster.scheduler_address:
                    cluster.status = JobStatus.RUNNING
            elif cluster.target in (JobStatus.STOPPED, JobStatus.FAILED):
                await self._stop_cluster(cluster, cluster.target)

        elif cluster.status == JobStatus.RUNNING:
            if cluster.target in (JobStatus.STOPPED, JobStatus.FAILED):
                await self._stop_cluster(cluster, cluster.target)
            else:
                # TODO: handle scaling/adaptive requests here
                pass

    async def _start_cluster(self, cluster):
        try:
            self.log.info(
                "Starting cluster %s for user %s...", cluster.name, cluster.username
            )

            # Walk through the startup process, saving state as updates occur
            async for state in self.handle_cluster_start(cluster):
                self.log.debug("State update for cluster %s", cluster.name)
                self.db.update_cluster(cluster, state=state)

            # Move cluster to submitted
            self.db.update_cluster(cluster, status=JobStatus.SUBMITTED)
        except Exception as exc:
            self.log.warning("Failed to submit cluster %s", cluster.name, exc_info=exc)
            self.db.update_cluster(cluster, target=JobStatus.FAILED)
            await self.enqueue(cluster)

    async def _stop_cluster(self, cluster, status):
        self.log.info("Stopping cluster %s...", cluster.name)
        if cluster.status > JobStatus.CREATED:
            try:
                await self.handle_cluster_stop(cluster)
            except Exception as exc:
                self.log.warning(
                    "Failed to stop cluster %s", cluster.name, exc_info=exc
                )
        self.log.info("Cluster %s stopped", cluster.name)
        self.db.update_cluster(cluster, status=status)

    # Subclasses should implement these methods
    async def handle_setup(self):
        pass

    async def handle_cleanup(self):
        pass

    async def handle_cluster_start(self, cluster):
        raise NotImplementedError

    async def handle_cluster_stop(self, cluster):
        raise NotImplementedError

    async def handle_cluster_status(self, cluster):
        raise NotImplementedError

    async def handle_worker_start(self, cluster, worker):
        raise NotImplementedError

    async def handle_worker_stop(self, cluster, worker):
        raise NotImplementedError

    async def handle_worker_status(self, cluster, worker):
        raise NotImplementedError
