import os
import secrets
import sqlite3
from threading import Lock
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "database.db")
_settings_lock = Lock()
_settings_cache = {}
_env_secret_key = os.getenv("SECRET_KEY", "").strip()
_env_admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
BOOTSTRAP_SECRET_KEY = _env_secret_key or secrets.token_urlsafe(32)
BOOTSTRAP_ADMIN_PASSWORD = _env_admin_password or secrets.token_urlsafe(18)
USING_GENERATED_SECRET_KEY = not bool(_env_secret_key)
USING_GENERATED_ADMIN_PASSWORD = not bool(_env_admin_password)

SETTINGS_SCHEMA = {
    "ADMIN_PASSWORD": {"default": BOOTSTRAP_ADMIN_PASSWORD, "type": "str"},
    "GROUP_URL": {"default": os.getenv("GROUP_URL", ""), "type": "str"},
    "CUSTOMER_SERVICE_URL": {"default": os.getenv("CUSTOMER_SERVICE_URL", ""), "type": "str"},
    "EPAY_MERCHANT_ID": {"default": os.getenv("EPAY_MERCHANT_ID", ""), "type": "str"},
    "EPAY_API_KEY": {"default": os.getenv("EPAY_API_KEY", ""), "type": "str"},
    "EPAY_NOTIFY_URL": {"default": os.getenv("EPAY_NOTIFY_URL", ""), "type": "str"},
    "EPAY_RETURN_URL": {"default": os.getenv("EPAY_RETURN_URL", ""), "type": "str"},
    "EPAY_GATEWAY_URL": {"default": os.getenv("EPAY_GATEWAY_URL", ""), "type": "str"},
    "EPAY_PRODUCT_PRICE": {"default": os.getenv("EPAY_PRODUCT_PRICE", "1.00"), "type": "float"},
    "PAYMENT_FEE_RATE": {"default": os.getenv("PAYMENT_FEE_RATE", "0.05"), "type": "float"},
    "ORDER_TIMEOUT": {"default": os.getenv("ORDER_TIMEOUT", "1800"), "type": "int"},
    "ORDER_CLEANUP_INTERVAL": {"default": os.getenv("ORDER_CLEANUP_INTERVAL", "300"), "type": "int"},
    "AUTO_SWITCH_ENABLED": {"default": os.getenv("AUTO_SWITCH_ENABLED", "1"), "type": "bool"},
    "FREE_INVITE_ENABLED": {"default": "0", "type": "bool"},
    "FREE_INVITE_END_TIME": {"default": "0", "type": "int"}
}

def _get_settings_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _normalize_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

def _parse_setting_value(value, value_type, default):
    if value is None:
        return default
    try:
        if value_type == "int":
            return int(value)
        if value_type == "float":
            return float(value)
        if value_type == "bool":
            return _normalize_bool(value)
        return str(value)
    except Exception:
        return default

def _stringify_setting_value(value, value_type):
    if value_type == "bool":
        return "1" if _normalize_bool(value) else "0"
    return str(value)

def init_settings_db():
    with _settings_lock:
        conn = _get_settings_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        finally:
            conn.close()

def _default_setting_value(meta):
    return _parse_setting_value(meta["default"], meta["type"], meta["default"])

def seed_settings_if_missing():
    init_settings_db()
    with _settings_lock:
        conn = _get_settings_connection()
        try:
            cursor = conn.cursor()
            for key, meta in SETTINGS_SCHEMA.items():
                cursor.execute("SELECT 1 FROM app_settings WHERE key = ?", (key,))
                if not cursor.fetchone():
                    cursor.execute(
                        "INSERT INTO app_settings (key, value) VALUES (?, ?)",
                        (key, _stringify_setting_value(meta["default"], meta["type"]))
                    )
            conn.commit()
        finally:
            conn.close()

def load_settings():
    seed_settings_if_missing()
    with _settings_lock:
        conn = _get_settings_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM app_settings")
            rows = cursor.fetchall()
        finally:
            conn.close()
    db_map = {row["key"]: row["value"] for row in rows}
    settings = {}
    for key, meta in SETTINGS_SCHEMA.items():
        default_value = _default_setting_value(meta)
        settings[key] = _parse_setting_value(db_map.get(key), meta["type"], default_value)
    return settings

def apply_settings_to_config(settings):
    for key, value in settings.items():
        setattr(Config, key, value)

def refresh_settings_cache():
    global _settings_cache
    settings = load_settings()
    _settings_cache = settings
    apply_settings_to_config(settings)
    return settings

def is_using_bootstrap_secret_key():
    return USING_GENERATED_SECRET_KEY and Config.SECRET_KEY == BOOTSTRAP_SECRET_KEY

def is_using_bootstrap_admin_password():
    if not _settings_cache:
        refresh_settings_cache()
    return USING_GENERATED_ADMIN_PASSWORD and Config.ADMIN_PASSWORD == BOOTSTRAP_ADMIN_PASSWORD

def get_settings(include_sensitive=False):
    if not _settings_cache:
        refresh_settings_cache()
    if include_sensitive:
        return dict(_settings_cache)
    return {k: v for k, v in _settings_cache.items() if k not in {"EPAY_API_KEY", "ADMIN_PASSWORD"}}

def update_settings(values):
    if values is None:
        return False, "无效的设置数据"
    updates = {}
    for key in SETTINGS_SCHEMA.keys():
        if key in values:
            value = values.get(key)
            if value is None:
                continue
            if key == "EPAY_API_KEY" and str(value).strip() == "":
                continue
            updates[key] = value
    if "ADMIN_PASSWORD" in updates and str(updates["ADMIN_PASSWORD"]).strip() == "":
        return False, "管理员密码不能为空"
    parsed_updates = {}
    for key, value in updates.items():
        meta = SETTINGS_SCHEMA[key]
        parsed = _parse_setting_value(value, meta["type"], None)
        if parsed is None:
            return False, f"{key} 参数无效"
        if key in {"ORDER_TIMEOUT", "ORDER_CLEANUP_INTERVAL"} and int(parsed) <= 0:
            return False, f"{key} 需要大于 0"
        parsed_updates[key] = parsed
    seed_settings_if_missing()
    with _settings_lock:
        conn = _get_settings_connection()
        try:
            cursor = conn.cursor()
            for key, value in parsed_updates.items():
                value_str = _stringify_setting_value(value, SETTINGS_SCHEMA[key]["type"])
                cursor.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                    (key, value_str)
                )
            conn.commit()
        finally:
            conn.close()
    refresh_settings_cache()
    return True, "设置已更新"


class Config:
    # Flask & Session
    SECRET_KEY = BOOTSTRAP_SECRET_KEY
    SESSION_TYPE = "redis"
    SESSION_PERMANENT = True
    SESSION_USE_SIGNER = True
    SESSION_KEY_PREFIX = "chatgpt_session:"
    SESSION_COOKIE_NAME = "chatgpt_team_sid"
    SESSION_REFRESH_EACH_REQUEST = True  # 每次请求都刷新 Session 过期时间
    SESSION_COOKIE_SAMESITE = "Lax"      # 防止跨站请求导致 Cookie 丢失
    PERMANENT_SESSION_LIFETIME = 86400 * 7  # 7天
    
    # 代理配置（默认不使用系统代理）
    HTTP_PROXY = os.getenv("HTTP_PROXY", "")
    HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")
    SOCKS5_PROXY = os.getenv("SOCKS5_PROXY", "")
    
    # 激活码状态
    CODE_STATUS_UNUSED = 0    # 未使用
    CODE_STATUS_ACTIVE = 1    # 已绑定邮箱
    CODE_STATUS_DISABLED = 2  # 已停用
    
    # ChatGPT Team API
    AUTHORIZATION_TOKEN = os.getenv("AUTHORIZATION_TOKEN", "")
    ACCOUNT_ID = os.getenv("ACCOUNT_ID", "")
    
    # Cloudflare Turnstile (可选)
    CF_TURNSTILE_SECRET_KEY = os.getenv("CF_TURNSTILE_SECRET_KEY", "")
    CF_TURNSTILE_SITE_KEY = os.getenv("CF_TURNSTILE_SITE_KEY", "")
    
    # Redis配置
    REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
    REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
    REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
    REDIS_DB = int(os.getenv("REDIS_DB", 0))
    
    # Admin
    ADMIN_PASSWORD = BOOTSTRAP_ADMIN_PASSWORD
    
    # 并发控制
    MAX_CONCURRENT_INVITES = int(os.getenv("MAX_CONCURRENT_INVITES", 3))
    SEMAPHORE_TIMEOUT = 30  # 信号量超时时间
    SEMAPHORE_MAX_CONCURRENT = MAX_CONCURRENT_INVITES
    
    # Redis Keys
    INVITE_RECORDS_KEY = "invite:records"
    INVITE_COUNTER_KEY = "invite:counter"
    INVITE_LOCK_KEY = "invite:lock"
    SEMAPHORE_KEY = "invite:semaphore"
    RATE_LIMIT_KEY = "invite:ratelimit"
    STATS_CACHE_KEY = "stats:cache"
    PENDING_INVITES_CACHE_KEY = "stats:pending_invites"
    GLOBAL_INVITE_LOCK_KEY = "invite:global_lock"
    
    # 缓存TTL
    STATS_CACHE_TTL = 180  # 3分钟
    STATS_REFRESH_INTERVAL = 180  # 统计刷新间隔（秒）
    
    # 频率限制配置
    RATE_LIMIT_MAX_REQUESTS = 5  # 每个时间窗口最大请求数
    RATE_LIMIT_WINDOW = 60  # 时间窗口（秒）

    # 外部链接
    GROUP_URL = ""
    CUSTOMER_SERVICE_URL = ""

    # SSL 验证
    VERIFY_SSL = os.getenv("VERIFY_SSL", "True").lower() == "true"

    # 易支付配置
    EPAY_MERCHANT_ID = ""
    EPAY_API_KEY = ""
    EPAY_NOTIFY_URL = ""
    EPAY_RETURN_URL = ""
    EPAY_GATEWAY_URL = ""
    EPAY_PRODUCT_PRICE = 1.00
    
    # 支付手续费配置（0.05 表示 5%）
    PAYMENT_FEE_RATE = 0.05
    
    # 库存管理
    STOCK_COUNT_KEY = "stock:count"
    STOCK_LOCK_KEY = "stock:lock"
    
    # 订单超时配置（秒）
    ORDER_TIMEOUT = 1800  # 默认30分钟
    ORDER_TIMEOUT_KEY_PREFIX = "order:timeout:"  # Redis Key前缀
    ORDER_CLEANUP_INTERVAL = 300  # 清理间隔，默认5分钟
    AUTO_SWITCH_ENABLED = True
    FREE_INVITE_ENABLED = False
    FREE_INVITE_END_TIME = 0

refresh_settings_cache()
