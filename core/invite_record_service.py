"""
邀请记录服务模块
使用 SQLite 存储邀请历史记录，与母号绑定
"""
import sqlite3
import os
from datetime import datetime, timezone, timedelta
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


def init_invite_records_db():
    """初始化邀请记录表"""
    # 确保 data 目录存在
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS invite_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    user_id TEXT,
                    username TEXT,
                    display_name TEXT,
                    activation_code TEXT,
                    email TEXT NOT NULL,
                    success INTEGER DEFAULT 1,
                    message TEXT DEFAULT '',
                    ip TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 创建索引以提高查询效率
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_invite_records_account_id 
                ON invite_records(account_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_invite_records_created_at 
                ON invite_records(created_at DESC)
            """)
            
            conn.commit()
            log_info("InviteRecords", "邀请记录数据库初始化完成")
        except Exception as e:
            log_error("InviteRecords", "初始化邀请记录数据库失败", str(e))
            raise
        finally:
            conn.close()


def add_invite_record(account_id, user_info, email, success, message=""):
    """添加邀请记录到 SQLite
    
    Args:
        account_id: 母号的 Account ID
        user_info: 用户信息字典
        email: 用户邮箱
        success: 是否成功
        message: 备注消息
        
    Returns:
        dict: 记录字典，失败返回 None
    """
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            created_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
            
            cursor.execute("""
                INSERT INTO invite_records 
                (account_id, user_id, username, display_name, activation_code, email, success, message, ip, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                account_id,
                user_info.get("id", ""),
                user_info.get("username", ""),
                user_info.get("name") or user_info.get("display_name", ""),
                user_info.get("activation_code", ""),
                email,
                1 if success else 0,
                message,
                user_info.get("ip", "unknown"),
                created_at
            ))
            
            conn.commit()
            record_id = cursor.lastrowid
            
            return {
                "id": record_id,
                "account_id": account_id,
                "user_id": user_info.get("id", ""),
                "username": user_info.get("username", ""),
                "display_name": user_info.get("name") or user_info.get("display_name", ""),
                "activation_code": user_info.get("activation_code", ""),
                "email": email,
                "success": success,
                "message": message,
                "ip": user_info.get("ip", "unknown"),
                "created_at": created_at
            }
        except Exception as e:
            log_error("InviteRecords", "保存邀请记录失败", str(e))
            return None
        finally:
            conn.close()


def get_invite_records(account_id=None, limit=100):
    """获取邀请记录
    
    Args:
        account_id: 母号 Account ID，为 None 时返回所有记录
        limit: 返回记录数量限制
        
    Returns:
        list: 记录列表
    """
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            
            if account_id:
                cursor.execute("""
                    SELECT * FROM invite_records 
                    WHERE account_id = ?
                    ORDER BY created_at DESC 
                    LIMIT ?
                """, (account_id, limit))
            else:
                cursor.execute("""
                    SELECT * FROM invite_records 
                    ORDER BY created_at DESC 
                    LIMIT ?
                """, (limit,))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            log_error("InviteRecords", "获取邀请记录失败", str(e))
            return []
        finally:
            conn.close()


def get_invite_stats(account_id=None):
    """获取邀请统计
    
    Args:
        account_id: 母号 Account ID，为 None 时返回全站统计
        
    Returns:
        dict: 统计数据
    """
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            
            if account_id:
                # 单个母号的统计
                cursor.execute("""
                    SELECT 
                        COUNT(*) as total,
                        SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count,
                        SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failed_count
                    FROM invite_records 
                    WHERE account_id = ?
                """, (account_id,))
            else:
                # 全站统计
                cursor.execute("""
                    SELECT 
                        COUNT(*) as total,
                        SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count,
                        SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failed_count
                    FROM invite_records
                """)
            
            row = cursor.fetchone()
            if row:
                return {
                    "total_invited": row["total"] or 0,
                    "success_count": row["success_count"] or 0,
                    "failed_count": row["failed_count"] or 0
                }
            return {"total_invited": 0, "success_count": 0, "failed_count": 0}
        except Exception as e:
            log_error("InviteRecords", "获取邀请统计失败", str(e))
            return {"total_invited": 0, "success_count": 0, "failed_count": 0}
        finally:
            conn.close()


def get_global_stats():
    """获取全站统计（管理员使用）"""
    return get_invite_stats(account_id=None)


def delete_records_by_account(account_id):
    """删除指定母号的所有邀请记录（删除母号时调用）
    
    Args:
        account_id: 母号的 Account ID
        
    Returns:
        int: 删除的记录数量
    """
    with _db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM invite_records WHERE account_id = ?", (account_id,))
            conn.commit()
            deleted_count = cursor.rowcount
            if deleted_count > 0:
                log_info("InviteRecords", f"删除母号关联记录", account_id=account_id, count=deleted_count)
            return deleted_count
        except Exception as e:
            log_error("InviteRecords", "删除母号关联记录失败", str(e))
            return 0
        finally:
            conn.close()


# 初始化数据库
init_invite_records_db()
