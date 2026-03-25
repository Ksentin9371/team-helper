"""
库存管理服务模块
使用 SQLite 存储激活码库存，Redis 管理库存余量
"""
import sqlite3
import secrets
import string
import os
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from utils.redis_client import redis_client
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

def init_stock_database():
    """初始化库存表"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # 库存表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                status INTEGER DEFAULT 0,
                sold_order_id TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        ''')
    log_info("Database", "库存表初始化完成")

def get_beijing_time():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

def add_stock_codes(codes_list):
    """批量添加库存激活码
    
    Args:
        codes_list: 激活码列表 ['CODE1', 'CODE2', ...]
        
    Returns:
        dict: {"success": int, "failed": int, "errors": []}
    """
    if not codes_list:
        return {"success": 0, "failed": 0, "errors": ["没有提供激活码"]}
    
    created_at = get_beijing_time()
    success_count = 0
    failed_count = 0
    errors = []
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        for code in codes_list:
            code = code.strip().upper()
            if not code:
                continue
            try:
                cursor.execute(
                    'INSERT INTO stock_codes (code, status, created_at) VALUES (?, ?, ?)',
                    (code, 0, created_at)
                )
                success_count += 1
                # 同步更新 Redis 库存计数
                redis_client.incr(Config.STOCK_COUNT_KEY)
            except sqlite3.IntegrityError:
                failed_count += 1
                errors.append(f"激活码 {code} 已存在")
            except Exception as e:
                failed_count += 1
                errors.append(f"添加 {code} 失败: {str(e)}")
    
    log_info("Stock", f"添加库存激活码", success=success_count, failed=failed_count)
    return {"success": success_count, "failed": failed_count, "errors": errors}

def get_stock_count():
    """获取库存余量（从Redis读取）"""
    try:
        count = redis_client.get(Config.STOCK_COUNT_KEY)
        return int(count) if count else 0
    except Exception as e:
        log_error("Stock", "获取Redis库存失败", str(e))
        # 降级到数据库查询
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM stock_codes WHERE status = 0')
            return cursor.fetchone()[0]

def sync_stock_count():
    """同步数据库库存到Redis（修复不一致）"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM stock_codes WHERE status = 0')
        db_count = cursor.fetchone()[0]
        redis_client.set(Config.STOCK_COUNT_KEY, db_count)
        log_info("Stock", f"同步库存计数到Redis", count=db_count)
        return db_count

def acquire_stock_code(order_id):
    """原子地分配一个库存激活码（下单时预扣减）
    
    Args:
        order_id: 订单号
        
    Returns:
        str or None: 成功返回激活码，失败返回None
    """
    # 1. 尝试从 Redis 扣减库存
    lua_script = """
    local count = redis.call('GET', KEYS[1])
    if count and tonumber(count) > 0 then
        redis.call('DECR', KEYS[1])
        return 1
    else
        return 0
    end
    """
    try:
        result = redis_client.eval(lua_script, 1, Config.STOCK_COUNT_KEY)
        if result == 0:
            log_warn("Stock", "Redis库存不足", order=order_id)
            return None
    except Exception as e:
        log_error("Stock", "Redis扣减失败", str(e))
        return None
    
    # 2. 从数据库随机获取一个未售激活码并标记
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT code FROM stock_codes WHERE status = 0 ORDER BY RANDOM() LIMIT 1'
            )
            row = cursor.fetchone()
            if not row:
                # 数据库没有库存了，回滚Redis
                redis_client.incr(Config.STOCK_COUNT_KEY)
                log_error("Stock", "数据库库存不足但Redis有余量，已回滚", order=order_id)
                return None
            
            code = row['code']
            cursor.execute(
                'UPDATE stock_codes SET status = 1, sold_order_id = ? WHERE code = ? AND status = 0',
                (order_id, code)
            )
            
            if cursor.rowcount == 0:
                # 更新失败（可能被其他进程占用），回滚Redis
                redis_client.incr(Config.STOCK_COUNT_KEY)
                log_error("Stock", "激活码被占用，已回滚", code=code, order=order_id)
                return None
            
            log_info("Stock", "分配激活码成功", code=code[:8], order=order_id)
            return code
    except Exception as e:
        # 出错时回滚 Redis
        redis_client.incr(Config.STOCK_COUNT_KEY)
        log_error("Stock", "分配激活码异常，已回滚", str(e))
        return None

def get_code_by_order(order_id):
    """根据订单号查询已分配的激活码"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT code FROM stock_codes WHERE sold_order_id = ?', (order_id,))
        row = cursor.fetchone()
        return row['code'] if row else None

def get_all_stock_codes(limit=500):
    """获取所有库存激活码列表"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM stock_codes ORDER BY id DESC LIMIT ?',
            (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]

def get_stock_codes_page(page=1, per_page=25, include_sold=True):
    if page < 1:
        page = 1
    offset = (page - 1) * per_page
    with get_db_connection() as conn:
        cursor = conn.cursor()
        base_query = 'FROM stock_codes'
        params = []
        if not include_sold:
            base_query += ' WHERE status = 0'
        cursor.execute(f'SELECT COUNT(1) as total {base_query}', params)
        total = cursor.fetchone()['total'] or 0
        cursor.execute(
            f'SELECT * {base_query} ORDER BY id DESC LIMIT ? OFFSET ?',
            (*params, per_page, offset)
        )
        return [dict(row) for row in cursor.fetchall()], total

def get_stock_stats():
    """获取库存统计"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT status, COUNT(*) as count FROM stock_codes GROUP BY status')
        rows = cursor.fetchall()
        stats = {0: 0, 1: 0}
        for row in rows:
            stats[row['status']] = row['count']
        
        redis_count = get_stock_count()
        
        return {
            "total": sum(stats.values()),
            "unsold": stats[0],
            "sold": stats[1],
            "redis_count": redis_count
        }

def release_stock_code(code):
    """释放已分配的激活码（用于订单超时）
    
    Args:
        code: 激活码
        
    Returns:
        bool: 是否释放成功
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT status FROM stock_codes WHERE code = ?', (code,))
        row = cursor.fetchone()
        if not row:
            return False
        
        if row['status'] == 1:
            # 释放激活码
            cursor.execute(
                'UPDATE stock_codes SET status = 0, sold_order_id = ? WHERE code = ?',
                ('', code)
            )
            if cursor.rowcount > 0:
                # 回补Redis库存
                redis_client.incr(Config.STOCK_COUNT_KEY)
                log_info("Stock", f"释放激活码 {code[:8]}")
                return True
        return False


def delete_stock_code(code, force=False):
    """删除库存激活码
    
    Args:
        code: 激活码
        force: 是否强制删除（删除已售激活码及关联订单）
        
    Returns:
        tuple: (是否成功, 消息, 是否已售)
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT status, sold_order_id FROM stock_codes WHERE code = ?', (code,))
        row = cursor.fetchone()
        if not row:
            return False, "激活码不存在", False
        
        is_sold = row['status'] == 1
        order_id = row['sold_order_id']
        
        # 如果已售出且没有强制删除标志，返回警告
        if is_sold and not force:
            return False, f"该激活码已售出（关联订单: {order_id}），确定要删除吗？删除后将同时删除关联订单。", True
        
        # 如果已售出，先删除关联订单
        if is_sold and order_id:
            try:
                # 导入 payment_service 以避免循环导入
                import sqlite3
                # 使用相同的连接删除订单
                cursor.execute('DELETE FROM pay_orders WHERE order_id = ?', (order_id,))
                log_info("Stock", f"级联删除订单 {order_id}", code=code[:8])
            except Exception as e:
                log_error("Stock", f"删除关联订单失败", str(e))
                return False, f"删除关联订单失败: {str(e)}", True
        
        # 删除激活码
        cursor.execute('DELETE FROM stock_codes WHERE code = ?', (code,))
        if cursor.rowcount > 0:
            # 只有未售出的才需要减少 Redis 库存
            if not is_sold:
                redis_client.decr(Config.STOCK_COUNT_KEY)
                log_info("Stock", f"删除库存激活码 {code}")
            else:
                log_info("Stock", f"删除已售激活码 {code}", order=order_id)
            
            return True, "删除成功" if not is_sold else f"已删除激活码及关联订单 {order_id}", is_sold
        return False, "删除失败", is_sold

# 初始化数据库
init_stock_database()


def batch_delete_stock(ids):
    """批量删除库存激活码
    
    Args:
        ids: ID 列表
        
    Returns:
        tuple: (成功数量, 总数量)
    """
    success_count = 0
    with get_db_connection() as conn:
        cursor = conn.cursor()
        for stock_id in ids:
            # 获取激活码信息
            cursor.execute('SELECT code, status, sold_order_id FROM stock_codes WHERE id = ?', (stock_id,))
            row = cursor.fetchone()
            if not row:
                continue
                
            code = row['code']
            is_sold = row['status'] == 1
            order_id = row['sold_order_id']
            
            # 级联删除订单
            if is_sold and order_id:
                try:
                    cursor.execute('DELETE FROM pay_orders WHERE order_id = ?', (order_id,))
                except Exception as e:
                    log_error("Stock", f"批量删除关联订单失败 {order_id}", str(e))
            
            # 删除库存
            cursor.execute('DELETE FROM stock_codes WHERE id = ?', (stock_id,))
            if cursor.rowcount > 0:
                success_count += 1
                if not is_sold:
                    # 更新 Redis
                    redis_client.decr(Config.STOCK_COUNT_KEY)
                    
        conn.commit()
    return success_count, len(ids)
