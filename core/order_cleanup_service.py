"""
订单清理服务
自动取消超时未支付的订单并释放库存

使用Redis作为超时判断依据：
1. 创建订单时在Redis设置key，过期时间=ORDER_TIMEOUT
2. 清理任务检查pending订单的Redis key是否存在
3. 如果key不存在（已过期），说明订单超时，执行清理
"""
from config import Config
from core.payment_service import get_expired_orders_from_redis, cancel_expired_order
from utils.logger import log_info, log_warn

def cleanup_expired_orders():
    """清理所有超时订单（基于Redis过期机制）"""
    try:
        # 清理未支付的超时订单
        # 从Redis获取已超时的订单ID列表
        expired_order_ids = get_expired_orders_from_redis()
        
        if expired_order_ids:
            log_info("OrderCleanup", f"发现 {len(expired_order_ids)} 个超时订单，开始处理...")
            
            success_count = 0
            failed_count = 0
            
            for order_id in expired_order_ids:
                success, code = cancel_expired_order(order_id)
                
                if success:
                    success_count += 1
                    if code:
                        log_info("OrderCleanup", f"订单 {order_id} 已取消，激活码 {code[:8]} 已释放")
                else:
                    failed_count += 1
                    log_warn("OrderCleanup", f"订单 {order_id} 取消失败")
            
            log_info("OrderCleanup", f"超时订单清理完成：成功 {success_count}，失败 {failed_count}")
        
    except Exception as e:
        log_warn("OrderCleanup", f"清理订单时出错: {str(e)}")
