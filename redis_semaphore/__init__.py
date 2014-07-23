#-*- coding:utf-8 -*-
from redis import StrictRedis
import time


__version_info__ = ('0', '1', '0')


class Semaphore(object):

    exists_val = 'ok'

    def __init__(self, client, count, namespace=None, stale_client_timeout=None):
        self.client = client or StrictRedis()
        if count < 1:
            raise ValueError("Parameter 'count' must be larger than 1")
        self.count = count
        self.namespace = namespace if namespace else 'SEMAPHORE'
        self.stale_client_timeout = stale_client_timeout
        self.is_use_local_time = False
        self._init()
        self._local_tokens = list()

    def _exists_or_init(self):
        old_key = self.client.getset(self.check_exists_key, self.exists_val)
        if old_key:
            return False
        return self._init()

    def _init(self):
        self.client.expire(self.check_exists_key, 10)
        with self.client.pipeline() as pipe:
            pipe.multi()
            pipe.delete(self.grabbed_key, self.available_key)
            pipe.rpush(self.available_key, *range(self.count))
            pipe.execute()

    @property
    def available_count(self):
        return self.client.llen(self.available_key)

    def acquire(self, timeout=0, target=None):
        if self.stale_client_timeout is not None:
            self.release_stale_locks()

        pair = self.client.blpop(self.available_key, timeout)
        if pair is None:
            return None
        token = pair[1]
        self._local_tokens.append(token)
        self.client.hset(self.grabbed_key, token, self.current_time)
        if target is not None:
            try:
                target(token)
            finally:
                self.signal(token)
        return token

    def release_stale_locks(self, expires=10):
        token = self.client.getset(self.check_release_locks_key, self.exists_val)
        if token:
            return False
        self.client.expire(self.check_release_locks_key, expires)
        try:
            for token, looked_at in self.client.hgetall(self.grabbed_key).iteritems():
                timed_out_at = float(looked_at) + self.stale_client_timeout
                if timed_out_at < self.current_time:
                    self.signal(token)
        finally:
            self.client.delete(self.check_release_locks_key)

    def _is_locked(self, token):
        return self.client.hexists(self.grabbed_key, token)

    def has_lock(self):
        for t in self._local_tokens:
            if self._is_locked(t):
                return True
        return False

    def release(self):
        if not self.has_lock():
            return False
        return self.signal(self._local_tokens.pop())

    def signal(self, token):
        if token is None:
            return None
        with self.client.pipeline() as pipe:
            pipe.multi()
            pipe.hdel(self.grabbed_key, token)
            pipe.lpush(self.available_key, token)
            pipe.execute()
            return token

    def get_namespaced_key(self, suffix):
        return '{0}:{1}'.format(self.namespace, suffix)

    @property
    def check_exists_key(self):
        return self._get_and_set_key('_exists_key', 'EXISTS')

    @property
    def available_key(self):
        return self._get_and_set_key('_available_key', 'AVAILABLE')

    @property
    def grabbed_key(self):
        return self._get_and_set_key('_grabbed_key', 'GRABBED')

    @property
    def check_release_locks_key(self):
        return self._get_and_set_key('_release_locks_ley', 'RELEASE_LOCKS')

    def _get_and_set_key(self, key_name, namespace_suffix):
        if not hasattr(self, key_name):
            setattr(self, key_name, self.get_namespaced_key(namespace_suffix))
        return getattr(self, key_name)

    @property
    def current_time(self):
        if self.is_use_local_time:
            return time.time()
        return float('.'.join(map(str, self.client.time())))

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return True if exc_type is None else False