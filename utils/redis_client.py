import redis
import secrets
import time
import json
from config import Config
from utils.logger import log_error
from utils.helpers import get_client_ip_address

# Redis 连接参数 (统一配置)
redis_kwargs = {
    'host': Config.REDIS_HOST,
    'port': Config.REDIS_PORT,
    'password': Config.REDIS_PASSWORD or None,
    'db': Config.REDIS_DB,
    'socket_connect_timeout': 5,
    'socket_timeout': 5,
    'retry_on_timeout': True,
    'health_check_interval': 30,  # 每30秒检查一次连接健康状态
    'socket_keepalive': True,     # 开启 TCP Keepalive
}

# Redis 连接池 (用于业务逻辑, decode_responses=True)
redis_pool = redis.ConnectionPool(
    decode_responses=True,
    max_connections=50,
    **redis_kwargs
)
redis_client = redis.Redis(connection_pool=redis_pool)

# Session 使用 Redis 存储 (用于 Flask-Session, 不使用 decode_responses)
# 同样使用连接池以提高稳定性
session_pool = redis.ConnectionPool(
    decode_responses=False,
    max_connections=50,
    **redis_kwargs
)
session_redis = redis.Redis(connection_pool=session_pool)

def acquire_invite_lock(user_id, timeout=30):
    """获取用户邀请分布式锁"""
    lock_key = f"{Config.INVITE_LOCK_KEY}:{user_id}"
    lock_token = secrets.token_hex(8)
    acquired = redis_client.set(lock_key, lock_token, nx=True, ex=timeout)
    return lock_token if acquired else None

def release_invite_lock(user_id, lock_token):
    """释放用户邀请锁"""
    lock_key = f"{Config.INVITE_LOCK_KEY}:{user_id}"
    lua_script = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("del", KEYS[1])
    else
        return 0
    end
    """
    try:
        redis_client.eval(lua_script, 1, lock_key, lock_token)
    except Exception:
        pass

def acquire_global_invite_lock(timeout=30, max_wait=5, retry_interval=0.3):
    """获取全局邀请锁（带重试机制）
    
    Args:
        timeout: 锁的过期时间（秒）
        max_wait: 最大等待时间（秒），在此期间会重试获取锁
        retry_interval: 重试间隔（秒）
        
    Returns:
        lock_token 如果成功，None 如果失败
    """
    import time as _time
    lock_token = secrets.token_hex(8)
    start_time = _time.time()
    
    while True:
        acquired = redis_client.set(Config.INVITE_LOCK_KEY, lock_token, nx=True, ex=timeout)
        if acquired:
            return lock_token
        
        # 检查是否超过最大等待时间
        elapsed = _time.time() - start_time
        if elapsed >= max_wait:
            return None  # 超时，返回失败
        
        # 等待一小段时间后重试
        _time.sleep(retry_interval)

def release_global_invite_lock(lock_token):
    """释放全局邀请锁"""
    lua_script = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("del", KEYS[1])
    else
        return 0
    end
    """
    try:
        redis_client.eval(lua_script, 1, Config.INVITE_LOCK_KEY, lock_token)
    except Exception:
        pass

def acquire_semaphore(timeout=Config.SEMAPHORE_TIMEOUT):
    """获取并发控制信号量"""
    token = secrets.token_hex(8)
    now = time.time()
    expire_at = now + timeout
    try:
        lua_script = """
        local key = KEYS[1]
        local max_concurrent = tonumber(ARGV[1])
        local now = tonumber(ARGV[2])
        local expire_at = tonumber(ARGV[3])
        local token = ARGV[4]
        redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
        local current = redis.call('ZCARD', key)
        if current < max_concurrent then
            redis.call('ZADD', key, expire_at, token)
            return 1
        else
            return 0
        end
        """
        result = redis_client.eval(
            lua_script, 1, Config.SEMAPHORE_KEY,
            Config.SEMAPHORE_MAX_CONCURRENT, now, expire_at, token
        )
        return token if result == 1 else None
    except Exception as e:
        log_error("Redis", "获取信号量失败", str(e))
        return None

def release_semaphore(token):
    """释放信号量"""
    if token:
        try:
            redis_client.zrem(Config.SEMAPHORE_KEY, token)
        except Exception as e:
            log_error("Redis", "释放信号量失败", str(e))

def get_semaphore_status():
    """获取当前信号量状态"""
    try:
        now = time.time()
        redis_client.zremrangebyscore(Config.SEMAPHORE_KEY, '-inf', now)
        current = redis_client.zcard(Config.SEMAPHORE_KEY)
        return {
            "current": current,
            "max": Config.SEMAPHORE_MAX_CONCURRENT,
            "available": max(0, Config.SEMAPHORE_MAX_CONCURRENT - current)
        }
    except Exception as e:
        log_error("Redis", "获取信号量状态失败", str(e))
        return {"current": 0, "max": Config.SEMAPHORE_MAX_CONCURRENT, "available": 0}

def check_rate_limit(identifier=None):
    """检查请求限流"""
    if identifier is None:
        identifier = get_client_ip_address()

    key = f"{Config.RATE_LIMIT_KEY}:{identifier}"

    try:
        pipe = redis_client.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        results = pipe.execute()

        current_count = results[0]
        ttl = results[1]

        if ttl == -1:
            redis_client.expire(key, Config.RATE_LIMIT_WINDOW)
            ttl = Config.RATE_LIMIT_WINDOW

        remaining = max(0, Config.RATE_LIMIT_MAX_REQUESTS - current_count)
        is_allowed = current_count <= Config.RATE_LIMIT_MAX_REQUESTS

        return is_allowed, remaining, ttl
    except Exception as e:
        log_error("RateLimit", "限流检查失败", str(e))
        return True, Config.RATE_LIMIT_MAX_REQUESTS, Config.RATE_LIMIT_WINDOW


# ============ 活跃用户追踪 ============
ACTIVE_USERS_KEY = "team_invite:active_users"
ACTIVE_USER_TTL = 120  # 2分钟内访问过的用户视为在线

def touch_active_user(user_id):
    """标记用户为活跃状态"""
    try:
        now = time.time()
        redis_client.zadd(ACTIVE_USERS_KEY, {user_id: now})
        # 清理过期用户
        redis_client.zremrangebyscore(ACTIVE_USERS_KEY, '-inf', now - ACTIVE_USER_TTL)
    except Exception as e:
        log_error("ActiveUser", "标记活跃用户失败", str(e))

def get_active_user_count():
    """获取当前活跃用户数"""
    try:
        now = time.time()
        # 清理过期用户
        redis_client.zremrangebyscore(ACTIVE_USERS_KEY, '-inf', now - ACTIVE_USER_TTL)
        return redis_client.zcard(ACTIVE_USERS_KEY)
    except Exception as e:
        log_error("ActiveUser", "获取活跃用户数失败", str(e))
        return 0

def has_active_users():
    """检查是否有活跃用户"""
    return get_active_user_count() > 0
