"""
母号配置服务模块
使用 SQLite 存储 ChatGPT Team 配置，支持运行时修改
"""
import sqlite3
import os
from datetime import datetime
from threading import Lock
from utils.logger import log_info, log_error

# 数据库路径 (统一使用同一个数据库文件)
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "database.db")

# 全局锁，用于线程安全
_db_lock = Lock()

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_team_config_db():
    """初始化母号配置表"""
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS team_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL DEFAULT '默认母号',
                    authorization_token TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    owner_email TEXT DEFAULT '',
                    is_active INTEGER DEFAULT 1,
                    note TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    seats_in_use INTEGER DEFAULT 0,
                    seats_entitled INTEGER DEFAULT 0,
                    pending_invites INTEGER DEFAULT 0,
                    active_start TEXT DEFAULT '',
                    active_until TEXT DEFAULT '',
                    stats_updated_at TIMESTAMP DEFAULT NULL,
                    status INTEGER DEFAULT 1 -- 1: 正常, 0: 封禁/翻车, -1: 软删除
                )
            """)
            conn.commit()
            
            # 检查是否需要添加新列（兼容旧数据库）
            cursor.execute("PRAGMA table_info(team_configs)")
            columns = [col[1] for col in cursor.fetchall()]
            
            new_columns = [
                ("owner_email", "TEXT DEFAULT ''"),
                ("seats_in_use", "INTEGER DEFAULT 0"),
                ("seats_entitled", "INTEGER DEFAULT 0"),
                ("pending_invites", "INTEGER DEFAULT 0"),
                ("active_start", "TEXT DEFAULT ''"),
                ("active_until", "TEXT DEFAULT ''"),
                ("stats_updated_at", "TIMESTAMP DEFAULT NULL"),
                ("status", "INTEGER DEFAULT 1"),
                ("allow_overload", "INTEGER DEFAULT 0"),
                ("max_overload", "INTEGER DEFAULT 0")
            ]
            
            for col_name, col_def in new_columns:
                if col_name not in columns:
                    cursor.execute(f"ALTER TABLE team_configs ADD COLUMN {col_name} {col_def}")
                    log_info("TeamConfig", f"添加新列 {col_name}")
            
            conn.commit()
            log_info("TeamConfig", "母号配置表初始化完成")
        except Exception as e:
            log_error("TeamConfig", "初始化母号配置表失败", str(e))
            raise
        finally:
            conn.close()

def migrate_from_env():
    """从环境变量迁移配置到数据库（首次运行时）"""
    # 检查是否已有配置
    configs = get_all_team_configs()
    if configs:
        return False  # 已有配置，不迁移
    
    # 从环境变量读取
    token = os.getenv("AUTHORIZATION_TOKEN", "")
    account_id = os.getenv("ACCOUNT_ID", "")
    
    if token and account_id:
        create_team_config(
            name="从.env迁移的母号",
            authorization_token=token,
            account_id=account_id,
            note="自动从.env文件迁移"
        )
        log_info("TeamConfig", "已从.env迁移母号配置")
        return True
    return False

def create_team_config(name, authorization_token, account_id, owner_email="", note="", is_active=1, allow_overload=0, max_overload=0):
    """创建新的母号配置"""
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            
            # 如果新配置设为激活，则先将其他配置设为非激活
            if is_active:
                cursor.execute("UPDATE team_configs SET is_active = 0")
            
            cursor.execute("""
                INSERT INTO team_configs (name, authorization_token, account_id, owner_email, note, is_active, status, allow_overload, max_overload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """, (name, authorization_token, account_id, owner_email, note, is_active, allow_overload, max_overload, datetime.now(), datetime.now()))
            
            conn.commit()
            config_id = cursor.lastrowid
            log_info("TeamConfig", f"创建母号配置成功", id=config_id, name=name)
            return config_id
        except Exception as e:
            log_error("TeamConfig", "创建母号配置失败", str(e))
            raise
        finally:
            conn.close()

def update_team_config(config_id, **kwargs):
    """更新母号配置"""
    allowed_fields = ['name', 'authorization_token', 'account_id', 'owner_email', 'note', 'is_active', 'status', 'allow_overload', 'max_overload']
    updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
    
    if not updates:
        return False
    
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            
            # 如果要激活此配置，先将其他配置设为非激活
            if updates.get('is_active') == 1:
                cursor.execute("UPDATE team_configs SET is_active = 0 WHERE id != ?", (config_id,))
            
            updates['updated_at'] = datetime.now()
            set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
            values = list(updates.values()) + [config_id]
            
            cursor.execute(f"UPDATE team_configs SET {set_clause} WHERE id = ?", values)
            conn.commit()
            
            if cursor.rowcount > 0:
                log_info("TeamConfig", f"更新母号配置成功", id=config_id)
                return True
            return False
        except Exception as e:
            log_error("TeamConfig", "更新母号配置失败", str(e))
            raise
        finally:
            conn.close()

def update_team_config_stats(config_id, stats_data):
    """更新母号的统计数据"""
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE team_configs SET 
                    seats_in_use = ?,
                    seats_entitled = ?,
                    pending_invites = ?,
                    active_start = ?,
                    active_until = ?,
                    stats_updated_at = ?
                WHERE id = ?
            """, (
                stats_data.get('seats_in_use', 0),
                stats_data.get('seats_entitled', 0),
                stats_data.get('pending_invites', 0),
                stats_data.get('active_start', ''),
                stats_data.get('active_until', ''),
                datetime.now(),
                config_id
            ))
            conn.commit()
            
            if cursor.rowcount > 0:
                log_info("TeamConfig", f"更新母号统计数据", id=config_id, seats=stats_data.get('seats_in_use'))
                return True
            return False
        except Exception as e:
            log_error("TeamConfig", "更新母号统计数据失败", str(e))
            return False
        finally:
            conn.close()

def save_active_config_stats():
    """保存当前激活配置的统计数据到数据库（从 Redis 缓存读取）"""
    from utils.redis_client import redis_client
    from config import Config
    import json
    
    active_config = get_active_team_config()
    if not active_config:
        return False
    
    try:
        # 从 Redis 缓存读取统计数据
        cached = redis_client.get(Config.STATS_CACHE_KEY)
        if cached:
            cache_obj = json.loads(cached)
            stats_data = cache_obj.get('data', {})
            if stats_data:
                return update_team_config_stats(active_config['id'], stats_data)
    except Exception as e:
        log_error("TeamConfig", "保存激活配置统计数据失败", str(e))
    
    return False

def delete_team_config(config_id):
    """【软删除】母号配置
    
    不再彻底物理删除记录，而是将 status 置为 -1 (软删除)。
    关联的邀请记录也将保留，以便查询历史。
    """
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            
            # 检查是否存在
            cursor.execute("SELECT id, is_active, account_id FROM team_configs WHERE id = ?", (config_id,))
            row = cursor.fetchone()
            if not row:
                return False, "配置不存在"
            
            # 如果是激活中的配置，不允许删除
            if row['is_active']:
                return False, "不能删除当前激活的母号配置（请先切换到其他母号）"
            
            # 执行软删除
            cursor.execute("UPDATE team_configs SET status = -1, is_active = 0, updated_at = ? WHERE id = ?", 
                          (datetime.now(), config_id))
            conn.commit()
            
            if cursor.rowcount > 0:
                log_info("TeamConfig", f"母号配置已标记为软删除", id=config_id)
                return True, "已成功软删除"
            return False, "删除操作未生效"
        except Exception as e:
            log_error("TeamConfig", "软删除母号配置失败", str(e))
            return False, str(e)
        finally:
            conn.close()

def set_team_config_failed(config_id):
    """将母号设置为‘翻车/封禁’状态 (status=0)"""
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT account_id FROM team_configs WHERE id = ?", (config_id,))
            row = cursor.fetchone()
            account_id = row["account_id"] if row else None
            cursor.execute("UPDATE team_configs SET status = 0, is_active = 0, updated_at = ? WHERE id = ?", 
                          (datetime.now(), config_id))
            conn.commit()
            updated = cursor.rowcount > 0
            if updated and account_id:
                from core.activation_code_service import mark_activation_codes_expired_by_account_id
                mark_activation_codes_expired_by_account_id(account_id)
            return updated
        except Exception as e:
            log_error("TeamConfig", "标记车队翻车失败", str(e))
            return False
        finally:
            conn.close()

def _apply_overload_logic(config):
    """应用超载逻辑，修改 seats_entitled 为最终计算结果"""
    if not config:
        return config
    
    allow_overload = config.get('allow_overload', 0)
    max_overload = config.get('max_overload', 0)
    
    # 只有开启允许超载且设置了最大超载人数，才进行计算
    if allow_overload and max_overload > 0:
        original_seats = config.get('seats_entitled', 0)
        # 如果原始车位为0（未同步），可能不应该加？或者应该加？
        # 用户说：设置了2，官方给5，一共7。
        # 如果官方还没给（0），那可能就是 0+2=2？或者保持0？
        # 通常 seats_entitled=0 表示未获取到数据。
        # 假设获取到了数据才生效。但为了保险，只要 > 0 就加。
        if original_seats > 0:
            config['seats_entitled'] = original_seats + max_overload
            
    return config

def get_team_config(config_id):
    """获取单个母号配置"""
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM team_configs WHERE id = ?", (config_id,))
            row = cursor.fetchone()
            if row:
                return _apply_overload_logic(dict(row))
            return None
        finally:
            conn.close()

def get_active_team_config():
    """获取当前激活的母号配置"""
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM team_configs WHERE is_active = 1 AND status = 1 LIMIT 1")
            row = cursor.fetchone()
            if row:
                return _apply_overload_logic(dict(row))
            return None
        finally:
            conn.close()

def get_team_config_by_account_id(account_id):
    """根据 Account ID 获取母号配置"""
    if not account_id:
        return None
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM team_configs WHERE account_id = ?", (account_id,))
            row = cursor.fetchone()
            if row:
                return _apply_overload_logic(dict(row))
            return None
        finally:
            conn.close()

def get_all_team_configs():
    """获取所有母号配置"""
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            # 包含软删除的记录，但正常记录排在前面
            cursor.execute("SELECT * FROM team_configs WHERE status != -1 ORDER BY is_active DESC, status DESC, created_at DESC")
            rows = cursor.fetchall()
            return [_apply_overload_logic(dict(row)) for row in rows]
        finally:
            conn.close()

def get_team_configs_page(page=1, per_page=25, include_banned=True):
    if page < 1:
        page = 1
    offset = (page - 1) * per_page
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            base_query = "FROM team_configs WHERE status != -1"
            params = []
            if not include_banned:
                base_query += " AND status != 0"
            cursor.execute(f"SELECT COUNT(1) as total {base_query}", params)
            total = cursor.fetchone()['total'] or 0
            cursor.execute(
                f"SELECT * {base_query} ORDER BY is_active DESC, status DESC, created_at DESC LIMIT ? OFFSET ?",
                (*params, per_page, offset)
            )
            rows = cursor.fetchall()
            return [_apply_overload_logic(dict(row)) for row in rows], total
        finally:
            conn.close()

def set_active_config(config_id):
    """设置激活的母号配置（切换前会保存当前激活配置的统计数据，切换后清理缓存）"""
    # 先保存当前激活配置的统计数据
    save_active_config_stats()
    
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            
            # 先将所有配置设为非激活
            cursor.execute("UPDATE team_configs SET is_active = 0")
            
            # 激活指定配置
            cursor.execute("UPDATE team_configs SET is_active = 1, updated_at = ? WHERE id = ?", 
                          (datetime.now(), config_id))
            conn.commit()
            
            if cursor.rowcount > 0:
                log_info("TeamConfig", f"切换激活母号配置", id=config_id)
                # 清理旧母号的缓存数据
                _clear_team_related_cache(new_config_id=config_id)
                return True
            return False
        except Exception as e:
            log_error("TeamConfig", "切换激活配置失败", str(e))
            return False
        finally:
            conn.close()

def get_earliest_available_team_config():
    """获取最早创建且未满员的母号配置"""
    from core.activation_code_service import get_pending_boarding_count
    
    configs = get_all_team_configs()
    # 按创建时间正序排列（最早的在前）
    configs_sorted = sorted(configs, key=lambda x: x.get('created_at', ''))
    
    for config in configs_sorted:
        # 只考虑正常状态且未激活的（或者包含当前激活的也行，但逻辑上是找下一个）
        if config.get('status') != 1:
            continue
            
        seats_in_use = config.get('seats_in_use', 0)
        seats_entitled = config.get('seats_entitled', 0)
        pending_invites = config.get('pending_invites', 0)
        
        # 获取本地待邀请数量
        acc_id = config.get('account_id')
        boarding = get_pending_boarding_count(acc_id)
        local_waiting = boarding.get('waiting_invite', 0)
        
        total_occupancy = seats_in_use + pending_invites + local_waiting
        
        # 如果还没同步过数据 (seats_entitled == 0)，或者没满员，则视为可用
        if seats_entitled == 0 or total_occupancy < seats_entitled:
            return config
            
    return None


def _clear_team_related_cache(new_config_id=None):
    """清理与母号相关的 Redis 缓存（切换母号时调用）"""
    from config import Config
    try:
        from utils.redis_client import redis_client
        # 清理统计缓存
        redis_client.delete(Config.STATS_CACHE_KEY)
        # 清理待处理邀请缓存
        redis_client.delete(Config.PENDING_INVITES_CACHE_KEY)
        
        # 清理新激活母号的满员倒计时 key，确保计时重置
        if new_config_id:
            redis_client.delete(f"team_full_countdown_{new_config_id}")

        log_info("TeamConfig", "已清理旧母号缓存数据")
    except Exception as e:
        log_error("TeamConfig", "清理缓存失败（不影响功能）", str(e))

def get_active_authorization_token():
    """获取当前激活的 Authorization Token"""
    config = get_active_team_config()
    if config:
        return config.get('authorization_token', '')
    # 回退到环境变量
    return os.getenv("AUTHORIZATION_TOKEN", "")

def get_active_account_id():
    """获取当前激活的 Account ID"""
    config = get_active_team_config()
    if config:
        return config.get('account_id', '')
    # 回退到环境变量
    return os.getenv("ACCOUNT_ID", "")

# 初始化数据库
init_team_config_db()
