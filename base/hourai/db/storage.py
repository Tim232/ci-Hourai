import asyncio
import aioredis
import collections
import enum
import coders
import logging
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import create_engine, orm, pool
from hourai import config
from . import models, caches, proto, bans

log = logging.getLogger(__name__)


CacheConfig = collections.namedtuple(
    'CacheConfig', ('attr', 'prefix', 'subprefix', 'subcoder',
                    'value_coder', 'timeout', 'proto_type'),
    defaults=(None,) * 7)


def protobuf(msg_type):
    return lambda: coders.ProtobufCoder(msg_type)


class StoragePrefix(enum.Enum):
    """ Top level prefixes in the root keyspace of Redis. """
    # Persistent Guild Level Data
    #   Generally stored in Redis as a hash with all submodels underneath it.
    GUILD_CONFIGS = 1
    MUSIC_STATES = 3
    # Ephemeral data that have expirations assigned to them.
    BANS = 2


class GuildPrefix(enum.Enum):
    """ Guild config prefixes. Used as prefixes or full keys in the hash
    underneath the guild key. All use the StoragePrefix.GUILD_CONFIGS as a
    prefix to the top level key.
    """
    # 1:1s. Hash key is just the prefix. Size: 1 byte.
    AUTO_CONFIG = CacheConfig(subprefix=0, proto_type=proto.AutoConfig)
    MODERATION_CONFIG = CacheConfig(subprefix=1,
                                    proto_type=proto.ModerationConfig)
    LOGGING_CONFIG = CacheConfig(subprefix=2, proto_type=proto.LoggingConfig)
    VALIDATION_CONFIG = CacheConfig(subprefix=3,
                                    proto_type=proto.ValidationConfig)
    MUSIC_CONFIG = CacheConfig(subprefix=4, proto_type=proto.MusicConfig)
    ANNOUNCE_CONFIG = CacheConfig(subprefix=5,
                                  proto_type=proto.AnnouncementConfig)
    ROLE_CONFIG = CacheConfig(subprefix=6, proto_type=proto.RoleConfig)


def _prefixize(val):
    if val is not None and isinstance(val, int):
        return bytes([val])
    return val


class Storage:
    """A generic interface for managing the remote storage services connected to
    the bot.
    """

    def __init__(self, config_module=config):
        self.config = config_module
        self.session_class = None
        self.redis = None
        self.executor = ThreadPoolExecutor()
        for conf in Storage._get_cache_configs():
            setattr(self, conf.attr, None)

    async def init(self):
        await asyncio.gather(
            self._init_sql_database(),
            self._init_redis()
        )

    async def _init_sql_database(self):
        try:
            log.info('Initializing connection to SQL database...')
            engine = self._create_sql_engine()
            self.session_class = orm.sessionmaker(bind=engine)
            self.ensure_created()
            log.info('SQL database connection established.')
        except Exception:
            log.exception('Error when initializing SQL database:')
            raise

    async def _init_redis(self):
        try:
            log.info('Initializing connection to Redis...')
            redis_conf = config.get_config_value(self.config, 'redis',
                                                 type=str)
            await self._connect_to_redis(redis_conf)
            self.__setup_caches()
            log.info('Redis connection established.')
        except Exception:
            log.exception('Error when initializing Redis:')
            raise

    async def _connect_to_redis(self, redis_conf):
        wait_time = 1.0
        max_wait_time = 60
        while True:
            try:
                self.redis = await aioredis.create_redis_pool(
                    redis_conf,
                    loop=asyncio.get_event_loop())
                break
            except aioredis.ReplyError:
                if wait_time >= max_wait_time:
                    raise
                log.exception(f'Failed to connect to Redis, backing off for '
                              f'{wait_time} seconds...')
                await asyncio.sleep(wait_time)
                wait_time *= 2

    def __setup_caches(self):
        self.bans = bans.BanStorage(self, StoragePrefix.BANS.value)

        self.music_states = caches.Cache(
                caches.RedisStore(self.redis, timeout=None),
                key_coder=_prefixize(GuildPrefix.MUSIC_STATES.value),
                value_coder=protobuf(proto.MusicBotState).compressed(),
                local_cache_size=0)

        for conf in Storage._get_cache_configs():
            # Initialize Parameters
            prefix = _prefixize(conf.prefix.value)
            key_coder = coders.IntCoder().prefixed(prefix)
            value_coder = (conf.value_coder or protobuf(conf.proto_type))()
            value_coder = value_coder.compressed()

            timeout = conf.timeout or 0
            if conf.subprefix is None:
                store = caches.RedisStore(self.redis, timeout=timeout)
            else:
                subprefix = _prefixize(conf.subprefix)
                subcoder = coders.ConstCoder(subprefix)
                if conf.subcoder is not None:
                    subcoder = conf.subcoder.prefixed(subprefix)

                key_coder = coders.TupleCoder([key_coder, subcoder])
                store = caches.RedisHashStore(self.redis, timeout=timeout)

            cache = caches.Cache(store,
                                 key_coder=key_coder,
                                 value_coder=value_coder)
            setattr(self, conf.attr, cache)

        # TODO(james7132): Uncomment the above once AggregateProtoHashCache
        # supports client side caching
        mapping = []
        for conf in Storage._get_cache_configs():
            if conf.prefix != StoragePrefix.GUILD_CONFIGS:
                continue

            attr = conf.attr
            if '_configs' in attr:
                attr = attr.replace('_configs', '')
            mapping.append((attr, getattr(self, conf.attr)))
        self.guild_configs = caches.AggregateProtoCache(proto.GuildConfig,
                                                        mapping)

    @staticmethod
    def _get_cache_configs():
        configs = list(GuildPrefix)
        configs = [conf.value._replace(attr=conf.name.lower() + 's',
                                       prefix=StoragePrefix.GUILD_CONFIGS)
                   for conf in configs]
        return configs

    def create_session(self):
        return StorageSession(self)

    async def close(self):
        self.redis.close()

    def ensure_created(self, engine=None):
        engine = engine or self._create_sql_engine()
        models.Base.metadata.create_all(engine)

    def _create_sql_engine(self, connection_str=None):
        connection_str = connection_str or \
            config.get_config_value(self.config, 'database', type=str)

        connect_args = dict()
        if 'sqlite' in connection_str:
            connect_args['check_same_thread'] = False

        return create_engine(connection_str,
                             poolclass=pool.SingletonThreadPool,
                             client_encoding='utf8',
                             connect_args=connect_args)


class StorageSession:
    __slots__ = ['storage', 'db_session', 'redis', 'subitems']

    def __init__(self, storage):
        self.storage = storage
        self.db_session = storage.session_class()
        self.redis = storage.redis

        self.subitems = (self.db_session, self.storage)

    @property
    def executor(self):
        return self.storage.executor

    def __enter__(self):
        return self

    def __exit__(self, exc, exc_type, tb):
        if exc is None:
            self.db_session.commit()
        else:
            self.db_session.rollback()
        self.db_session.close()

    async def execute_query(self, callback, *args):
        return await asyncio.run_in_executor(self.executor, callback, *args)

    def __getattr__(self, attr):
        for subitem in self.subitems:
            try:
                return getattr(subitem, attr)
            except AttributeError:
                pass
        raise AttributeError
