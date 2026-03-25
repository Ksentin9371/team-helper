"""
支付相关路由模块
处理用户购买、订单查询、支付回调
"""
from flask import Blueprint, render_template, request, jsonify, redirect, url_for
import requests
from config import Config
from core.payment_service import (
    create_order,
    get_order_by_id,
    mark_order_paid,
    build_payment_url,
    verify_epay_sign,
    get_order_remaining_time,
    cancel_order,
    get_pending_order_by_ip
)
from core.stock_service import (
    get_stock_count,
    acquire_stock_code
)
from utils.logger import log_info, log_error, log_warn

payment_bp = Blueprint('payment', __name__)


@payment_bp.route("/buy")
def buy_page():
    """购买页面"""
    return render_template("buy.html")


@payment_bp.route("/api/buy/create", methods=["POST"])
def create_buy_order():
    """创建购买订单"""
    # 检查易支付配置
    if not Config.EPAY_MERCHANT_ID or not Config.EPAY_API_KEY:
        return jsonify({
            "success": False,
            "message": "支付功能未配置，请联系管理员"
        }), 400
    
    # 1. 先检查母号是否存活
    from core.team_config_service import get_active_team_config, get_all_team_configs
    from core.openai_service import fetch_stats_from_api
    from models.exceptions import TeamBannedException
    
    # 检查是否有任何可用的母号（包括当前激活的和备用的）
    has_available_account = False
    active_config = get_active_team_config()
    
    # 先检查当前激活的母号
    if active_config and active_config.get('status') == 1:
        try:
            stats = fetch_stats_from_api()
            if stats:
                has_available_account = True
        except TeamBannedException:
            log_warn("Buy", "当前激活母号已封禁，检查备用母号")
        except Exception as e:
            log_warn("Buy", "当前激活母号连接失败，检查备用母号", str(e))
    
    # 如果当前激活母号不可用，检查是否有其他可用的母号
    if not has_available_account:
        all_configs = get_all_team_configs()
        for config in all_configs:
            # 跳过当前激活的（已经检查过了）
            if active_config and config.get('id') == active_config.get('id'):
                continue
            
            # 只检查状态正常的母号
            if config.get('status') != 1:
                continue
            
            try:
                # 使用该母号的凭证检查
                token = config.get('authorization_token')
                account_id = config.get('account_id')
                stats = fetch_stats_from_api(custom_token=token, custom_account_id=account_id)
                if stats:
                    has_available_account = True
                    log_info("Buy", "找到可用的备用母号", name=config.get('name'))
                    break
            except:
                continue
    
    # 如果没有任何可用的母号，拒绝售卖
    if not has_available_account:
        log_error("Buy", "所有母号均不可用，拒绝售卖")
        return jsonify({
            "success": False,
            "message": "所有母号均已翻车或无法连接，暂时无法售卖"
        }), 400
    
    # 获取用户IP
    ip_address = request.remote_addr
    
    # 2. 检查是否有未支付订单
    payment_method = request.form.get("method", "alipay")
    
    pending_order = get_pending_order_by_ip(ip_address)
    
    if pending_order:
        # 如果有未支付订单，返回该订单信息，让用户继续支付
        order_id = pending_order['order_id']
        amount = pending_order['amount']
        
        # 重新生成支付链接（因为可能选择了不同的支付方式）
        payment_url = build_payment_url(order_id, amount, payment_method)
        
        return jsonify({
            "success": False,
            "code": "HAS_PENDING_ORDER",
            "message": "您有未支付的订单，请继续支付或等待过期",
            "order_id": order_id,
            "amount": amount,
            "payment_url": payment_url
        }), 200
    
    # 确定基础价格
    base_price = round(Config.EPAY_PRODUCT_PRICE, 2)
        
    # 创建订单（使用基础价格，手续费仅展示）
    fee_amount = round(base_price * Config.PAYMENT_FEE_RATE, 2)
    create_order_amount = base_price
    
    order_id = create_order(create_order_amount, ip_address)
    
    if not order_id:
        return jsonify({
            "success": False,
            "message": "创建订单失败，请重试"
        }), 500
    
    # 3. 检查库存（分配激活码）
    code = acquire_stock_code(order_id)
        
    if not code:
        from core.payment_service import delete_order
        delete_order(order_id)
            
        return jsonify({
            "success": False,
            "message": "库存不足，订单创建失败"
        }), 400
    
    # 生成支付链接
    payment_method = request.form.get("method", "alipay")
    payment_url = build_payment_url(order_id, create_order_amount, payment_method)
    
    if not payment_url:
        return jsonify({
            "success": False,
            "message": "生成支付链接失败"
        }), 500
    
    log_info("Buy", "创建购买订单", order=order_id, amount=create_order_amount, fee_display=fee_amount)
    
    return jsonify({
        "success": True,
        "order_id": order_id,
        "amount": create_order_amount,
        "payment_url": payment_url
    })


@payment_bp.route("/api/buy/query", methods=["POST"])
def query_order():
    """查询订单状态"""
    order_id = request.form.get("order_id", "").strip()
    
    if not order_id:
        return jsonify({
            "success": False,
            "message": "请输入订单号"
        }), 400
    
    order = get_order_by_id(order_id)
    
    if not order:
        log_warn("OrderQuery", "订单查询失败", order=order_id, ip=request.remote_addr)
        return jsonify({
            "success": False,
            "message": "订单不存在"
        }), 404
    
    # 获取剩余时间（仅对pending订单）
    remaining_time = -1
    if order["status"] == "pending":
        remaining_time = get_order_remaining_time(order_id)
    
    log_info("OrderQuery", "用户查询订单", order=order_id, status=order["status"], ip=request.remote_addr)
    return jsonify({
        "success": True,
        "order": {
            "order_id": order["order_id"],
            "amount": order["amount"],
            "status": order["status"],
            "code": order["code"] if order["status"] == "success" else "",
            "created_at": order["created_at"],
            "pay_time": order["pay_time"],
            "remaining_time": remaining_time,
            "type": order.get("type", "team")
        }
    })


@payment_bp.route("/api/pay/notify", methods=["GET", "POST"])
def payment_notify():
    """易支付回调接口"""
    # 支持GET和POST两种方式
    if request.method == "POST":
        params = request.form.to_dict()
    else:
        params = request.args.to_dict()
    
    log_info("PayNotify", "收到支付回调", params={k: v for k, v in params.items() if k != 'sign'})
    
    # 验证签名
    if not verify_epay_sign(params, Config.EPAY_API_KEY):
        log_error("PayNotify", "签名验证失败", params=params)
        return "fail", 400
    
    # 检查商户ID
    pid = params.get("pid", "")
    if Config.EPAY_MERCHANT_ID and pid != Config.EPAY_MERCHANT_ID:
        log_error("PayNotify", "商户ID不匹配", expect=Config.EPAY_MERCHANT_ID, got=pid)
        return "fail", 400
    
    # 检查交易状态
    trade_status = params.get("trade_status", "")
    if trade_status != "TRADE_SUCCESS":
        log_warn("PayNotify", "交易状态异常", status=trade_status)
        return "fail", 400
    
    # 获取订单信息
    order_id = params.get("out_trade_no", "")
    trade_no = params.get("trade_no", "")
    money = float(params.get("money", "0"))
    
    if not order_id:
        log_error("PayNotify", "订单号为空")
        return "fail", 400
    
    # 查询订单
    order = get_order_by_id(order_id)
    if not order:
        log_error("PayNotify", "订单不存在", order=order_id)
        return "fail", 404
    
    # 验证金额
    if abs(order["amount"] - money) > 0.01:
        log_error("PayNotify", "金额不匹配", expect=order["amount"], got=money)
        return "fail", 400
    
    # 检查订单是否已处理
    if order["status"] == "success":
        log_info("PayNotify", "订单已处理（重复回调）", order=order_id)
        return "success"
    
    # 标记订单为已支付
    code = order.get("code", "")
    if not code:
        # 如果订单没有关联激活码，尝试重新分配
        from core.stock_service import get_code_by_order
        code = get_code_by_order(order_id)
        
        if not code:
            log_error("PayNotify", "订单无关联激活码", order=order_id)
            return "fail", 500
    
    success = mark_order_paid(order_id, code, trade_no)
    
    if success:
        log_info("PayNotify", "订单支付成功", order=order_id, code=code[:8], trade_no=trade_no)
        return "success"
    else:
        log_error("PayNotify", "更新订单状态失败", order=order_id)
        return "fail", 500


@payment_bp.route("/api/stock/count")
def get_stock_info():
    """获取当前库存数量（公开接口）"""
    from core.team_config_service import get_active_team_config, get_all_team_configs
    from core.openai_service import fetch_stats_from_api
    from models.exceptions import TeamBannedException
    
    # 检查是否有任何可用的母号
    mother_account_available = False
    active_config = get_active_team_config()
    
    # 先检查当前激活的母号
    if active_config and active_config.get('status') == 1:
        try:
            stats = fetch_stats_from_api()
            if stats:
                mother_account_available = True
        except:
            pass
    
    # 如果当前激活母号不可用，检查是否有其他可用的母号
    if not mother_account_available:
        all_configs = get_all_team_configs()
        for config in all_configs:
            # 跳过当前激活的（已经检查过了）
            if active_config and config.get('id') == active_config.get('id'):
                continue
            
            # 只检查状态正常的母号
            if config.get('status') != 1:
                continue
            
            try:
                # 使用该母号的凭证检查
                token = config.get('authorization_token')
                account_id = config.get('account_id')
                stats = fetch_stats_from_api(custom_token=token, custom_account_id=account_id)
                if stats:
                    mother_account_available = True
                    break
            except:
                continue
    
    count = get_stock_count()
    base_price = round(Config.EPAY_PRODUCT_PRICE, 2)
    fee_rate = Config.PAYMENT_FEE_RATE
    fee_amount = round(base_price * fee_rate, 2)
    final_price = round(base_price + fee_amount, 2)
    
    return jsonify({
        "success": True,
        "count": count,
        "price": base_price,
        "fee_rate": fee_rate,
        "fee_amount": fee_amount,
        "final_price": final_price,
        "timeout": Config.ORDER_TIMEOUT,  # 返回超时配置
        "mother_account_available": mother_account_available  # 母号是否可用
    })


@payment_bp.route("/api/order/remaining/<order_id>")
def get_order_remaining(order_id):
    """获取订单剩余时间（公开接口）"""
    remaining = get_order_remaining_time(order_id)
    return jsonify({
        "success": True,
        "order_id": order_id,
        "remaining_time": remaining
    })


@payment_bp.route("/api/order/cancel", methods=["POST"])
def cancel_user_order():
    """用户取消订单（公开接口）"""
    order_id = request.form.get("order_id", "").strip()
    
    if not order_id:
        return jsonify({
            "success": False,
            "message": "请输入订单号"
        }), 400
    
    # 取消订单
    success, code, message = cancel_order(order_id, reason="user_cancel")
    
    if success:
        log_info("UserCancel", "用户取消订单", order=order_id, code=code[:8] if code else "无")
        return jsonify({
            "success": True,
            "message": message,
            "code": code
        })
    else:
        log_warn("UserCancel", "用户取消订单失败", order=order_id, message=message)
        return jsonify({
            "success": False,
            "message": message
        }), 400
