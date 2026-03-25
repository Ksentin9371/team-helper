"""
支付订单服务模块
管理易支付订单的创建、查询、支付回调
"""
import sqlite3
import os
import time
import hashlib
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from urllib.parse import urlencode
from config import Config
from utils.logger import log_info, log_error, log_warn

def get_beijing_time_obj():
    """获取北京时间对象"""
    return datetime.now(timezone(timedelta(hours=8)))

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

def init_payment_database():
    """初始化订单表"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pay_orders (
                order_id TEXT PRIMARY KEY,
                amount REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                code TEXT DEFAULT '',
                trade_no TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                pay_time TEXT DEFAULT '',
                ip_address TEXT DEFAULT ''
            )
        ''')
        
        # 尝试添加 ip_address 列（如果表已存在但没有该列）
        try:
            cursor.execute('ALTER TABLE pay_orders ADD COLUMN ip_address TEXT DEFAULT ""')
        except sqlite3.OperationalError:
            # 如果列已存在，会抛出错误，忽略即可
            pass
            
    log_info("Database", "订单表初始化完成")

def get_beijing_time():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

def generate_order_id():
    """生成15位订单号：时间戳(10位) + 随机5位数字"""
    timestamp = str(int(time.time()))  # 10位
    import random
    random_part = ''.join([str(random.randint(0, 9)) for _ in range(5)])
    return timestamp + random_part

def generate_epay_sign(params, key):
    """生成易支付签名
    
    Args:
        params: 参数字典
        key: 商户密钥
        
    Returns:
        签名字符串
    """
    # 过滤空值和sign字段
    filtered = {k: str(v) for k, v in params.items() 
                if k not in ['sign', 'sign_type'] and str(v).strip() != ''}
    
    # 按键名排序
    sorted_keys = sorted(filtered.keys())
    
    # 拼接字符串: key1=value1&key2=value2&...&key
    sign_str = '&'.join([f"{k}={filtered[k]}" for k in sorted_keys]) + key
    
    # MD5签名
    return hashlib.md5(sign_str.encode('utf-8')).hexdigest()

def verify_epay_sign(params, key):
    """验证易支付签名"""
    expected_sign = params.get('sign', '')
    if not expected_sign:
        return False
    
    calculated_sign = generate_epay_sign(params, key)
    return calculated_sign.lower() == expected_sign.lower()

def create_order(amount, ip_address=''):
    """创建订单
    
    Args:
        amount: 订单金额
        ip_address: 下单IP地址
        
    Returns:
        订单号或None
    """
    order_id = generate_order_id()
    created_at = get_beijing_time()
    
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO pay_orders (order_id, amount, status, created_at, ip_address) VALUES (?, ?, ?, ?, ?)',
                (order_id, amount, 'pending', created_at, ip_address)
            )
        
        # 在Redis中设置订单超时标记，使用订单号作为key，设置过期时间
        from utils.redis_client import redis_client
        redis_key = f"{Config.ORDER_TIMEOUT_KEY_PREFIX}{order_id}"
        redis_client.setex(redis_key, Config.ORDER_TIMEOUT, "1")
        
        log_info("Payment", "创建订单", order=order_id, amount=amount, ip=ip_address, timeout=Config.ORDER_TIMEOUT)
        return order_id
    except Exception as e:
        log_error("Payment", "创建订单失败", str(e))
        return None

def get_pending_order_by_ip(ip_address):
    """根据IP查询待支付订单
    
    Args:
        ip_address: IP地址
        
    Returns:
        dict: 订单信息或None
    """
    if not ip_address:
        return None
        
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT order_id, amount, created_at, status 
                FROM pay_orders 
                WHERE ip_address = ? AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
            ''', (ip_address,))
            row = cursor.fetchone()
            
            if row:
                order_id = row['order_id']
                # 检查是否超时
                remaining = get_order_remaining_time(order_id)
                if remaining > 0:
                    return dict(row)
                else:
                    # 如果已超时，取消该订单
                    log_info("Payment", "发现过期未支付订单，自动取消", order=order_id, ip=ip_address)
                    cancel_order(order_id, reason="timeout_auto_check")
                    return None
            return None
    except Exception as e:
        log_error("Payment", "查询IP待支付订单失败", str(e))
        return None

def get_order_by_id(order_id):
    """根据订单号查询订单（关联查询激活码）"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # 使用LEFT JOIN从stock_codes表关联查询激活码
            # 使用NULLIF将空字符串转为NULL，然后COALESCE才能正确工作
            cursor.execute('''
                SELECT 
                    p.order_id,
                    p.amount,
                    p.status,
                    COALESCE(NULLIF(p.code, ''), s.code, '') as code,
                    p.trade_no,
                    p.created_at,
                    p.pay_time
                FROM pay_orders p
                LEFT JOIN stock_codes s ON p.order_id = s.sold_order_id
                WHERE p.order_id = ?
            ''', (order_id,))
            row = cursor.fetchone()
            if row:
                res = dict(row)
                res['type'] = 'team' # 标记类型
                return res
            return None
    except Exception as e:
        log_error("Payment", "查询订单失败", str(e))
        return None

def cancel_order(order_id, reason="manual"):
    """取消订单并释放库存（通用函数）
    
    Args:
        order_id: 订单号
        reason: 取消原因 (manual=手动取消, timeout=超时)
        
    Returns:
        tuple: (是否成功, 释放的激活码, 错误消息)
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # 查询订单
            cursor.execute('SELECT status, code FROM pay_orders WHERE order_id = ?', (order_id,))
            row = cursor.fetchone()
            
            if not row:
                return False, None, "订单不存在"
            
            if row['status'] != 'pending':
                return False, None, f"订单已{row['status']}，无法取消"
            
            # 先从订单表的code字段获取
            code = row['code'].strip() if row['code'] else ''
            
            # 如果订单表中没有code，从stock_codes表查找
            if not code:
                cursor.execute('SELECT code FROM stock_codes WHERE sold_order_id = ?', (order_id,))
                code_row = cursor.fetchone()
                if code_row:
                    code = code_row['code']
            
            # 标记订单为失败
            cursor.execute(
                'UPDATE pay_orders SET status = ? WHERE order_id = ? AND status = ?',
                ('fail', order_id, 'pending')
            )
            
            if cursor.rowcount > 0:
                # 删除Redis超时标记
                from utils.redis_client import redis_client
                redis_key = f"{Config.ORDER_TIMEOUT_KEY_PREFIX}{order_id}"
                redis_client.delete(redis_key)
                
                # 释放激活码
                if code:
                    cursor.execute(
                        'UPDATE stock_codes SET status = 0, sold_order_id = ? WHERE code = ?',
                        ('', code)
                    )
                    # 回补Redis库存
                    redis_client.incr(Config.STOCK_COUNT_KEY)
                    
                    log_info("Payment", f"订单已取消({reason})，激活码已释放", order=order_id, code=code[:8])
                    return True, code, "取消成功"
                else:
                    log_warn("Payment", f"订单已取消({reason})，但未找到关联激活码", order=order_id)
                    return True, None, "订单已取消，但未找到关联激活码"
            
            return False, None, "取消失败"
    except Exception as e:
        log_error("Payment", "取消订单失败", str(e))
        return False, None, f"取消失败: {str(e)}"




def cancel_expired_order(order_id):
    """取消超时订单并释放库存（兼容旧接口）
    
    Args:
        order_id: 订单号
        
    Returns:
        tuple: (是否成功, 释放的激活码)
    """
    success, code, _ = cancel_order(order_id, reason="timeout")
    return success, code


def get_expired_orders_from_redis():
    """从Redis获取所有已超时的订单
    
    通过扫描已过期的Redis key来找到超时订单
    
    Returns:
        list: 超时订单ID列表
    """
    try:
        from utils.redis_client import redis_client
        
        # 查询所有待处理订单
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # 查询普通订单
            cursor.execute('SELECT order_id FROM pay_orders WHERE status = ?', ('pending',))
            pending_orders = [row['order_id'] for row in cursor.fetchall()]
        
        if not pending_orders:
            return []
        
        expired_orders = []
        
        # 检查每个订单的Redis key是否还存在
        for order_id in pending_orders:
            redis_key = f"{Config.ORDER_TIMEOUT_KEY_PREFIX}{order_id}"
            # 如果key不存在（已过期），说明订单超时
            if not redis_client.exists(redis_key):
                expired_orders.append(order_id)
        
        return expired_orders
    except Exception as e:
        log_error("Payment", "从Redis查询超时订单失败", str(e))
        return []


def check_order_timeout(order_id):
    """检查指定订单是否超时
    
    Args:
        order_id: 订单号
        
    Returns:
        bool: True=已超时, False=未超时
    """
    try:
        from utils.redis_client import redis_client
        redis_key = f"{Config.ORDER_TIMEOUT_KEY_PREFIX}{order_id}"
        return not redis_client.exists(redis_key)
    except Exception as e:
        log_error("Payment", "检查订单超时失败", str(e))
        return False


def get_order_remaining_time(order_id):
    """获取订单剩余时间（秒）
    
    Args:
        order_id: 订单号
        
    Returns:
        int: 剩余秒数，-1表示已过期或不存在
    """
    try:
        from utils.redis_client import redis_client
        redis_key = f"{Config.ORDER_TIMEOUT_KEY_PREFIX}{order_id}"
        ttl = redis_client.ttl(redis_key)
        # ttl返回值: >0=剩余秒数, -1=key存在但无过期时间, -2=key不存在
        return ttl if ttl > 0 else -1
    except Exception as e:
        log_error("Payment", "获取订单剩余时间失败", str(e))
        return -1


def manual_complete_order(order_id):
    """手动补单（管理员标记订单为已支付）
    
    Args:
        order_id: 订单号
        
    Returns:
        tuple: (是否成功, 消息)
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # 查询订单
            cursor.execute('SELECT status, code FROM pay_orders WHERE order_id = ?', (order_id,))
            row = cursor.fetchone()
            
            if not row:
                return False, "订单不存在"
            
            if row['status'] != 'pending':
                return False, f"订单状态为 {row['status']}，只能补单待支付订单"
            
            # 获取激活码
            code = row['code'].strip() if row['code'] else ''
            
            # 如果订单没有code，从stock_codes表查找
            if not code:
                cursor.execute('SELECT code FROM stock_codes WHERE sold_order_id = ?', (order_id,))
                code_row = cursor.fetchone()
                if code_row:
                    code = code_row['code']
            
            if not code:
                return False, "订单无关联激活码，无法补单"
            
            pay_time = get_beijing_time()
            
            # 标记订单为已支付
            cursor.execute(
                'UPDATE pay_orders SET status = ?, pay_time = ?, trade_no = ? WHERE order_id = ? AND status = ?',
                ('success', pay_time, 'MANUAL', order_id, 'pending')
            )
            
            if cursor.rowcount > 0:
                # 删除Redis超时标记
                from utils.redis_client import redis_client
                redis_key = f"{Config.ORDER_TIMEOUT_KEY_PREFIX}{order_id}"
                redis_client.delete(redis_key)
                
                log_info("Payment", "手动补单成功", order=order_id, code=code[:8])
                return True, f"补单成功，订单已标记为已支付，激活码: {code}"
            
            return False, "补单失败"
    except Exception as e:
        log_error("Payment", "手动补单失败", str(e))
        return False, f"补单失败: {str(e)}"


def mark_order_paid(order_id, code, trade_no=''):
    """标记订单已支付
    
    Args:
        order_id: 订单号
        code: 分配的激活码
        trade_no: 支付平台交易号
        
    Returns:
        bool: 是否更新成功（防止重复回调）
    """
    pay_time = get_beijing_time()
    
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # 只有pending状态的订单才能标记为成功
            cursor.execute(
                'UPDATE pay_orders SET status = ?, code = ?, trade_no = ?, pay_time = ? WHERE order_id = ? AND status = ?',
                ('success', code, trade_no, pay_time, order_id, 'pending')
            )
            
            if cursor.rowcount > 0:
                # 删除Redis中的超时标记（订单已完成）
                from utils.redis_client import redis_client
                redis_key = f"{Config.ORDER_TIMEOUT_KEY_PREFIX}{order_id}"
                redis_client.delete(redis_key)
                
                log_info("Payment", "订单支付成功", order=order_id, code=code[:8])
                return True
            else:
                log_warn("Payment", "订单已处理或不存在", order=order_id)
                return False
    except Exception as e:
        log_error("Payment", "标记订单失败", str(e))
        return False

def build_payment_url(order_id, amount, method='alipay'):
    """构建易支付支付链接
    
    Args:
        order_id: 订单号
        amount: 金额
        method: 支付方式 (alipay/wxpay/qqpay等)
        
    Returns:
        支付URL
    """
    if not Config.EPAY_MERCHANT_ID or not Config.EPAY_API_KEY:
        log_error("Payment", "易支付配置不完整")
        return None
    
    # 在 return_url 中添加订单号参数
    return_url = Config.EPAY_RETURN_URL
    if '?' in return_url:
        return_url = f"{return_url}&order_id={order_id}"
    else:
        return_url = f"{return_url}?order_id={order_id}"
    
    params = {
        'pid': Config.EPAY_MERCHANT_ID,
        'type': method,
        'out_trade_no': order_id,
        'notify_url': Config.EPAY_NOTIFY_URL,
        'return_url': return_url,
        'name': f'激活码购买-{order_id[:10]}',
        'money': f'{amount:.2f}',
        'sign_type': 'MD5'
    }
    
    # 生成签名
    sign = generate_epay_sign(params, Config.EPAY_API_KEY)
    params['sign'] = sign
    
    # 构建URL
    gateway = Config.EPAY_GATEWAY_URL.rstrip('/')
    query_string = urlencode(params)
    payment_url = f"{gateway}/submit.php?{query_string}"
    
    log_info("Payment", "生成支付链接", order=order_id)
    return payment_url

def get_all_orders(limit=200):
    """获取所有订单列表（关联查询激活码）"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 
                p.order_id,
                p.amount,
                p.status,
                COALESCE(NULLIF(p.code, ''), s.code, '') as code,
                p.trade_no,
                p.created_at,
                p.pay_time
            FROM pay_orders p
            LEFT JOIN stock_codes s ON p.order_id = s.sold_order_id
            ORDER BY p.created_at DESC 
            LIMIT ?
        ''', (limit,))
        return [dict(row) for row in cursor.fetchall()]

def get_orders_page(page=1, per_page=25, search=None, only_completed=False):
    if page < 1:
        page = 1
    offset = (page - 1) * per_page
    with get_db_connection() as conn:
        cursor = conn.cursor()
        base_query = '''
            FROM pay_orders p
            LEFT JOIN stock_codes s ON p.order_id = s.sold_order_id
        '''
        params = []
        conditions = []
        if search:
            conditions.append('p.order_id LIKE ?')
            params.append(f'%{search}%')
        if only_completed:
            conditions.append("p.status = 'success'")
        if conditions:
            base_query += ' WHERE ' + ' AND '.join(conditions)
        cursor.execute(f'SELECT COUNT(1) as total {base_query}', params)
        total = cursor.fetchone()['total'] or 0
        cursor.execute(f'''
            SELECT 
                p.order_id,
                p.amount,
                p.status,
                COALESCE(NULLIF(p.code, ''), s.code, '') as code,
                p.trade_no,
                p.created_at,
                p.pay_time
            {base_query}
            ORDER BY p.created_at DESC 
            LIMIT ? OFFSET ?
        ''', (*params, per_page, offset))
        return [dict(row) for row in cursor.fetchall()], total

def search_orders_by_id(order_id):
    """根据订单号搜索订单（关联查询激活码）"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # 使用LEFT JOIN从stock_codes表关联查询激活码
        # 使用NULLIF将空字符串转为NULL，然后COALESCE才能正确工作
        cursor.execute('''
            SELECT 
                p.order_id,
                p.amount,
                p.status,
                COALESCE(NULLIF(p.code, ''), s.code, '') as code,
                p.trade_no,
                p.created_at,
                p.pay_time
            FROM pay_orders p
            LEFT JOIN stock_codes s ON p.order_id = s.sold_order_id
            WHERE p.order_id LIKE ? 
            ORDER BY p.created_at DESC
        ''', (f'%{order_id}%',))
        return [dict(row) for row in cursor.fetchall()]

def delete_order(order_id):
    """删除订单（解绑激活码，不删除激活码）
    
    Args:
        order_id: 订单号
        
    Returns:
        tuple: (是否成功, 消息)
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # 查询订单
            cursor.execute('SELECT status, code FROM pay_orders WHERE order_id = ?', (order_id,))
            row = cursor.fetchone()
            
            if not row:
                return False, "订单不存在"
            
            code = row['code'].strip() if row['code'] else ''
            
            # 如果订单没有code，从stock_codes表查找
            if not code:
                cursor.execute('SELECT code FROM stock_codes WHERE sold_order_id = ?', (order_id,))
                code_row = cursor.fetchone()
                if code_row:
                    code = code_row['code']
            
            # 删除订单
            cursor.execute('DELETE FROM pay_orders WHERE order_id = ?', (order_id,))
            
            if cursor.rowcount > 0:
                # 如果有关联激活码，解绑激活码（恢复为未售状态）
                if code:
                    cursor.execute(
                        'UPDATE stock_codes SET status = 0, sold_order_id = ? WHERE code = ?',
                        ('', code)
                    )
                    
                    # 如果订单状态不是成功，需要回补Redis库存
                    if row['status'] != 'success':
                        from utils.redis_client import redis_client
                        redis_client.incr(Config.STOCK_COUNT_KEY)
                        log_info("Payment", f"删除订单并释放激活码", order=order_id, code=code[:8])
                    else:
                        # 已支付订单的激活码被释放，也要回补库存
                        from utils.redis_client import redis_client
                        redis_client.incr(Config.STOCK_COUNT_KEY)
                        log_info("Payment", f"删除已支付订单并释放激活码", order=order_id, code=code[:8])
                    
                    return True, f"订单已删除，激活码 {code} 已解绑并重新可用"
                else:
                    log_info("Payment", f"删除订单（无关联激活码）", order=order_id)
                    return True, "订单已删除"
            
            return False, "删除失败"
    except Exception as e:
        log_error("Payment", "删除订单失败", str(e))
        return False, f"删除失败: {str(e)}"


def get_order_stats():
    """获取订单统计"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT status, COUNT(*) as count, SUM(amount) as total FROM pay_orders GROUP BY status')
        rows = cursor.fetchall()
        
        stats = {
            "pending": {"count": 0, "amount": 0},
            "success": {"count": 0, "amount": 0},
            "fail": {"count": 0, "amount": 0}
        }
        
        for row in rows:
            status = row['status']
            if status in stats:
                stats[status]["count"] = row['count']
                stats[status]["amount"] = row['total'] or 0
        
        return stats

# 初始化数据库
init_payment_database()
