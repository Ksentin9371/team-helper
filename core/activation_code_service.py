"""
激活码服务模块 - SQLite 状态管理增强版
状态：0-未使用, 1-已绑定邮箱, 2-已停用
"""
import sqlite3
import secrets
import string
import os
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from config import Config
from utils.logger import log_info, log_error, log_warn

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'database.db')

def get_db_path():
    db_dir = os.path.dirname(DB_PATH)
    if not os.path.exists(db_dir):
        os.makedirs(db_dir)
    return DB_PATH

@contextmanager
def get_db_connection():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def init_database():
    """初始化数据库表，支持状态字段"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS activation_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT DEFAULT 'admin',
                note TEXT DEFAULT '',
                status INTEGER DEFAULT 0,  -- 0:未使用, 1:已绑定, 2:已停用
                used_at TEXT,
                used_by TEXT,
                used_by_name TEXT,
                invite_status TEXT DEFAULT 'new',  -- new/pending/in_space
                bound_account_id TEXT DEFAULT '',   -- 绑定到哪个母号的 account_id
                user_type TEXT DEFAULT 'paid'      -- paid:付费, free_invite:限免
            )
        ''')
        # 检查是否需要添加新字段
        try:
            cursor.execute('ALTER TABLE activation_codes ADD COLUMN status INTEGER DEFAULT 0')
        except:
            pass
        try:
            cursor.execute('ALTER TABLE activation_codes ADD COLUMN invite_status TEXT DEFAULT "new"')
        except:
            pass
        try:
            cursor.execute('ALTER TABLE activation_codes ADD COLUMN bound_account_id TEXT DEFAULT ""')
        except:
            pass
        try:
            cursor.execute('ALTER TABLE activation_codes ADD COLUMN user_type TEXT DEFAULT "paid"')
        except:
            pass
            
    log_info("Database", "激活码数据库初始化完成")

def get_beijing_time():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

def generate_activation_code(length=16, prefix="CCODE"):
    chars = string.ascii_uppercase + string.digits
    chars = chars.replace('O', '').replace('0', '').replace('I', '').replace('1', '').replace('L', '')
    code_body = ''.join(secrets.choice(chars) for _ in range(length))
    return f"{prefix}-" + "-".join([code_body[i:i+4] for i in range(0, len(code_body), 4)])

def create_activation_codes(count=1, created_by="admin", note=""):
    created_at = get_beijing_time()
    codes = []
    with get_db_connection() as conn:
        cursor = conn.cursor()
        for _ in range(count):
            for _ in range(10):
                code = generate_activation_code()
                try:
                    cursor.execute('INSERT INTO activation_codes (code, created_at, created_by, note, status) VALUES (?, ?, ?, ?, ?)', 
                                 (code, created_at, created_by, note, Config.CODE_STATUS_UNUSED))
                    codes.append({"code": code, "status": 0})
                    break
                except sqlite3.IntegrityError:
                    continue
    return codes

def validate_activation_code(code):
    """验证激活码"""
    if not code: return False, "请输入激活码", None
    code = code.strip().upper()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM activation_codes WHERE code = ?', (code,))
            row = cursor.fetchone()
            if not row: return False, "激活码无效", None
            
            data = dict(row)
            if data['status'] == Config.CODE_STATUS_DISABLED:
                return False, "该激活码已停用", data
            if data['status'] == Config.CODE_STATUS_ACTIVE:
                return False, f"该激活码已于 {data.get('used_at')} 绑定邮箱", data
            
            return True, "验证通过", data
    except Exception as e:
        log_error("ActivationCode", "验证异常", str(e))
        return False, "系统错误", None

def use_activation_code(code, user_info):
    """使用并绑定激活码"""
    code = code.strip().upper()
    try:
        is_valid, msg, _ = validate_activation_code(code)
        if not is_valid: return False, msg
        
        used_at = get_beijing_time()
        used_by = user_info.get("email", "unknown")
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE activation_codes 
                SET status = ?, used_at = ?, used_by = ?
                WHERE code = ? AND status = ?
            ''', (Config.CODE_STATUS_ACTIVE, used_at, used_by, code, Config.CODE_STATUS_UNUSED))
            if cursor.rowcount == 0: return False, "绑定失败，可能已被占用"
            
        return True, "绑定成功"
    except Exception as e:
        log_error("ActivationCode", "绑定异常", str(e))
        return False, "系统错误"


def bind_activation_code_with_seat_check(code, user_info, account_id, seats_entitled, api_in_use=0, api_pending=0):
    """原子化绑定激活码并校验剩余名额

    说明：
    - 使用 SQLite 的 BEGIN IMMEDIATE 获取写锁，作为 Redis 全局锁的兜底。
    - 在同一事务内完成“计算本地待上车人数 + 绑定激活码 + 标记 invite_status/bound_account_id”。
    - 这样即使两个请求几乎同时进入，第二个请求也会在事务里看到第一个请求刚占下的本地名额。
    """
    if not code:
        return False, "请输入激活码", None

    code = code.strip().upper()
    used_by = (user_info.get("email") or "unknown").strip().lower()
    used_at = get_beijing_time()

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            cursor.execute('''
                SELECT status, used_at, used_by
                FROM activation_codes
                WHERE code = ?
            ''', (code,))
            row = cursor.fetchone()

            if not row:
                return False, "激活码无效", None

            status = row["status"]
            bound_email = (row["used_by"] or "").strip().lower()

            if status == Config.CODE_STATUS_DISABLED:
                return False, "该激活码已停用", None

            if status == Config.CODE_STATUS_ACTIVE:
                if bound_email == used_by:
                    return True, "绑定成功", {
                        "available_before": max(0, int(seats_entitled or 0)),
                        "available_after": max(0, int(seats_entitled or 0))
                    }
                return False, f"该激活码已于 {row['used_at']} 绑定邮箱", None

            cursor.execute('''
                SELECT COUNT(*) AS count
                FROM activation_codes
                WHERE bound_account_id = ?
                  AND status = ?
                  AND COALESCE(invite_status, 'new') = 'new'
            ''', (account_id, Config.CODE_STATUS_ACTIVE))
            local_new_count = (cursor.fetchone()["count"] or 0)

            total_occupied = int(api_in_use or 0) + int(api_pending or 0) + int(local_new_count or 0)
            seats_total = int(seats_entitled or 0)
            available_before = seats_total - total_occupied

            if seats_total > 0 and available_before <= 0:
                return False, f"当前车位已满（已上车/已发邀请/已绑定激活码共计 {total_occupied} 人）", {
                    "reason": "full",
                    "total_occupied": total_occupied,
                    "seats_entitled": seats_total,
                    "local_new_count": local_new_count,
                    "api_in_use": int(api_in_use or 0),
                    "api_pending": int(api_pending or 0)
                }

            cursor.execute('''
                UPDATE activation_codes
                SET status = ?,
                    used_at = ?,
                    used_by = ?,
                    invite_status = ?,
                    bound_account_id = ?
                WHERE code = ? AND status = ?
            ''', (
                Config.CODE_STATUS_ACTIVE,
                used_at,
                used_by,
                'new',
                account_id,
                code,
                Config.CODE_STATUS_UNUSED
            ))

            if cursor.rowcount == 0:
                return False, "绑定失败，可能已被占用", None

            return True, "绑定成功", {
                "available_before": max(0, available_before),
                "available_after": max(0, available_before - 1),
                "total_occupied_before": total_occupied
            }
    except Exception as e:
        log_error("ActivationCode", "原子绑定异常", str(e))
        return False, "系统错误", None

def set_code_status(code, status):
    """手动设置激活码状态"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 智能恢复：如果目标是"未使用"状态，但存在绑定信息，则自动恢复为"已绑定"
        if status == Config.CODE_STATUS_UNUSED:
            cursor.execute('SELECT used_by FROM activation_codes WHERE code = ?', (code,))
            row = cursor.fetchone()
            if row and row['used_by']:
                status = Config.CODE_STATUS_ACTIVE
        
        cursor.execute('UPDATE activation_codes SET status = ? WHERE code = ?', (status, code))
        return cursor.rowcount > 0

def get_all_activation_codes(include_used=True, limit=500):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        query = 'SELECT * FROM activation_codes'
        if not include_used:
            query += ' WHERE status = 0'
        query += ' ORDER BY id DESC LIMIT ?'
        cursor.execute(query, (limit,))
        return [dict(row) for row in cursor.fetchall()]

def get_activation_codes_page(include_used=True, page=1, per_page=25):
    if page < 1:
        page = 1
    offset = (page - 1) * per_page
    with get_db_connection() as conn:
        cursor = conn.cursor()
        base_query = 'FROM activation_codes'
        params = []
        if not include_used:
            base_query += ' WHERE status = 0'
        cursor.execute(f'SELECT COUNT(1) as total {base_query}', params)
        total = cursor.fetchone()['total'] or 0
        cursor.execute(
            f'SELECT * {base_query} ORDER BY id DESC LIMIT ? OFFSET ?',
            (*params, per_page, offset)
        )
        return [dict(row) for row in cursor.fetchall()], total

def get_activation_code_stats():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT status, COUNT(*) as count FROM activation_codes GROUP BY status')
        rows = cursor.fetchall()
        stats = {0: 0, 1: 0, 2: 0}
        for row in rows:
            stats[row['status']] = row['count']
        return {
            "total": sum(stats.values()),
            "unused": stats[0],
            "active": stats[1],
            "disabled": stats[2]
        }

def delete_activation_code(code, force=False):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if force:
            cursor.execute('DELETE FROM activation_codes WHERE code = ?', (code,))
        else:
            cursor.execute('DELETE FROM activation_codes WHERE code = ? AND status = 0', (code,))
        
        if cursor.rowcount > 0:
            return True, "删除成功"
        else:
            return False, "删除失败，可能激活码不存在或已使用（需使用强制删除）"

def unbind_activation_code(code):
    """解绑激活码：重置为未使用状态（彻底清除邮箱、母号绑定和状态记录）"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE activation_codes 
            SET status = 0, used_at = NULL, used_by = NULL, used_by_name = NULL, 
                invite_status = 'new', bound_account_id = ''
            WHERE code = ?
        ''', (code,))
        return cursor.rowcount > 0, "解绑成功" if cursor.rowcount > 0 else "激活码不存在"

def update_activation_code_binding(code, new_email):
    """换绑激活码：更新使用者"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        used_at = get_beijing_time()
        cursor.execute('''
            UPDATE activation_codes 
            SET used_by = ?, used_at = ?
            WHERE code = ?
        ''', (new_email, used_at, code))
        return cursor.rowcount > 0, "换绑成功" if cursor.rowcount > 0 else "激活码不存在"

def get_activation_code_by_email(email):
    """根据邮箱查找激活码绑定信息"""
    if not email:
        return None
    email = email.strip().lower()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT code, status, used_at FROM activation_codes 
                WHERE LOWER(used_by) = ? AND status = ?
            ''', (email, Config.CODE_STATUS_ACTIVE))
            row = cursor.fetchone()
            if row:
                return {
                    "code": row["code"],
                    "status": row["status"],
                    "used_at": row["used_at"]
                }
            return None
    except Exception as e:
        log_error("ActivationCode", "查询邮箱绑定异常", str(e))
        return None

def get_activation_code_by_code(code):
    """根据激活码获取详细信息"""
    if not code:
        return None
    code = code.strip().upper()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM activation_codes WHERE code = ?', (code,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
    except Exception as e:
        log_error("ActivationCode", "获取激活码详情失败", str(e))
        return None

def get_all_bound_emails():
    """获取所有已绑定邮箱及其激活码/母号信息的映射"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT LOWER(used_by) as email, code, bound_account_id FROM activation_codes 
                WHERE used_by IS NOT NULL AND status = ?
            ''', (Config.CODE_STATUS_ACTIVE,))
            return {
                row["email"]: {
                    "code": row["code"],
                    "bound_account_id": row["bound_account_id"] or ""
                }
                for row in cursor.fetchall()
            }
    except Exception as e:
        log_error("ActivationCode", "获取绑定映射异常", str(e))
        return {}

def update_invite_status(email, invite_status, account_id=None, code=None):
    """更新用户的邀请状态
    
    Args:
        email: 用户邮箱
        invite_status: 邀请状态 ('new'/'pending'/'in_space')
        account_id: 绑定的母号 account_id（可选）
        code: 具体的激活码（可选，如果提供则仅更新该激活码）
    """
    if not email:
        return False
    email = email.strip().lower()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # 如果提供了 code，则精准更新
            if code:
                code = code.strip().upper()  # 确保激活码大写
                if account_id:
                    cursor.execute('''
                        UPDATE activation_codes 
                        SET invite_status = ?, bound_account_id = ?
                        WHERE code = ? AND status = ?
                    ''', (invite_status, account_id, code, Config.CODE_STATUS_ACTIVE))
                else:
                    cursor.execute('''
                        UPDATE activation_codes 
                        SET invite_status = ?
                        WHERE code = ? AND status = ?
                    ''', (invite_status, code, Config.CODE_STATUS_ACTIVE))
            else:
                # 否则更新该邮箱下的所有活跃激活码（旧逻辑，用于兼容）
                if account_id:
                    cursor.execute('''
                        UPDATE activation_codes 
                        SET invite_status = ?, bound_account_id = ?
                        WHERE LOWER(used_by) = ? AND status = ?
                    ''', (invite_status, account_id, email, Config.CODE_STATUS_ACTIVE))
                else:
                    cursor.execute('''
                        UPDATE activation_codes 
                        SET invite_status = ?
                        WHERE LOWER(used_by) = ? AND status = ?
                    ''', (invite_status, email, Config.CODE_STATUS_ACTIVE))
            
            if cursor.rowcount > 0:
                log_info("ActivationCode", f"更新邀请状态", email=email, status=invite_status, code=code)
                return True
            return False
    except Exception as e:
        log_error("ActivationCode", "更新邀请状态失败", str(e))
        return False

def mark_activation_codes_expired_by_account_id(account_id):
    if not account_id:
        return 0
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE activation_codes
                SET invite_status = 'expired'
                WHERE bound_account_id = ? AND status = ? AND invite_status != 'expired'
                ''',
                (account_id, Config.CODE_STATUS_ACTIVE),
            )
            updated = cursor.rowcount or 0
            if updated > 0:
                log_info("ActivationCode", "批量标记激活码为 expired", account_id=account_id[:8], count=updated)
            return updated
    except Exception as e:
        log_error("ActivationCode", "批量标记 expired 失败", str(e))
        return 0

def get_invite_status_by_code(code):
    """根据激活码获取本地保存的邀请状态（最推荐的方式）"""
    if not code:
        return None
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT invite_status, bound_account_id, used_by FROM activation_codes 
                WHERE code = ? AND status = ?
            ''', (code, Config.CODE_STATUS_ACTIVE))
            row = cursor.fetchone()
            if row:
                return {
                    "invite_status": row["invite_status"] or "new",
                    "bound_account_id": row["bound_account_id"] or "",
                    "email": row["used_by"] or ""
                }
            return None
    except Exception as e:
        log_error("ActivationCode", "获取激活码状态失败", str(e))
        return None

def get_invite_status_by_email(email):
    """获取用户的本地保存的邀请状态
    
    Returns:
        dict: {'invite_status': 'new'/'pending'/'in_space', 'bound_account_id': 'xxx'} 或 None
    """
    if not email:
        return None
    email = email.strip().lower()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT invite_status, bound_account_id FROM activation_codes 
                WHERE LOWER(used_by) = ? AND status = ?
            ''', (email, Config.CODE_STATUS_ACTIVE))
            row = cursor.fetchone()
            if row:
                return {
                    "invite_status": row["invite_status"] or "new",
                    "bound_account_id": row["bound_account_id"] or ""
                }
            return None
    except Exception as e:
        log_error("ActivationCode", "获取邀请状态失败", str(e))
        return None


def get_pending_boarding_count(account_id):
    """获取指定母号下待上车的用户数
    
    包括：
    - invite_status = 'new'（已绑定激活码但还未发送邀请）
    - invite_status = 'pending'（已发送邀请待接受）
    
    Args:
        account_id: 母号的 Account ID
        
    Returns:
        dict: {'waiting_invite': int, 'pending_accept': int, 'total': int}
    """
    if not account_id:
        return {"waiting_invite": 0, "pending_accept": 0, "total": 0}
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # 统计该母号下各状态的激活码数量
            cursor.execute('''
                SELECT invite_status, COUNT(*) as count FROM activation_codes 
                WHERE bound_account_id = ? AND status = ?
                GROUP BY invite_status
            ''', (account_id, Config.CODE_STATUS_ACTIVE))
            rows = cursor.fetchall()
            
            stats = {"new": 0, "pending": 0, "in_space": 0}
            for row in rows:
                status = row["invite_status"] or "new"
                stats[status] = row["count"]
            
            return {
                "waiting_invite": stats["new"],      # 待发送邀请
                "pending_accept": stats["pending"],  # 已发送待接收
                "total": stats["new"] + stats["pending"]  # 总待上车人数
            }
    except Exception as e:
        log_error("ActivationCode", "获取待上车人数失败", str(e))
        return {"waiting_invite": 0, "pending_accept": 0, "total": 0}

def get_bound_count(account_id):
    """获取某个母号下所有已经占位的（已使用的）激活码数量"""
    if not account_id:
        return 0
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) FROM activation_codes 
                WHERE bound_account_id = ? AND status = ?
            ''', (account_id, Config.CODE_STATUS_ACTIVE))
            return cursor.fetchone()[0] or 0
    except Exception as e:
        log_error("ActivationCode", "获取已绑定数量失败", str(e))
        return 0


def get_all_users_by_status(invite_status):
    """根据邀请状态获取所有用户信息"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT used_by as email, bound_account_id, code 
                FROM activation_codes 
                WHERE invite_status = ? AND status = ?
            ''', (invite_status, Config.CODE_STATUS_ACTIVE))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        log_error("ActivationCode", f"根据状态 {invite_status} 获取用户失败", str(e))
        return []

def bulk_update_invite_status(email_list, invite_status, account_id):
    """批量更新邀请状态（高效更新）"""
    if not email_list:
        return
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # 批量更新为指定状态
            placeholders = ', '.join(['?'] * len(email_list))
            cursor.execute(f'''
                UPDATE activation_codes 
                SET invite_status = ?
                WHERE LOWER(used_by) IN ({placeholders}) 
                AND bound_account_id = ? 
                AND status = ?
            ''', (invite_status, *[e.lower() for e in email_list], account_id, Config.CODE_STATUS_ACTIVE))
            log_info("ActivationCode", f"批量更新状态完成", status=invite_status, count=cursor.rowcount)
    except Exception as e:
        log_error("ActivationCode", "批量更新状态失败", str(e))

init_database()

