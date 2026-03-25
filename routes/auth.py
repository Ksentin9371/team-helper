"""
认证路由模块 - 激活码 + 自定义邮箱登录
"""
import re
import time
from flask import Blueprint, redirect, url_for, session, request, jsonify
from config import Config
from utils.logger import log_info, log_error, log_warn
from core.invite_service import get_user_invite_status
from core.activation_code_service import validate_activation_code, bind_activation_code_with_seat_check

auth_bp = Blueprint('auth', __name__)


def validate_email(email):
    """验证邮箱格式"""
    if not email:
        return False
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """激活码登录页面和处理"""
    if request.method == "GET":
        # 如果已登录，直接跳转
        if "user" in session:
            return redirect(url_for("user.invite_page"))
        return redirect(url_for("user.index"))
    
    # POST 处理激活码验证
    activation_code = request.form.get("activation_code", "").strip().upper()
    user_email = request.form.get("email", "").strip().lower()
    
    # 验证输入
    if not activation_code:
        return jsonify({"success": False, "message": "请输入激活码"}), 400
    
    if not user_email:
        return jsonify({"success": False, "message": "请输入邮箱"}), 400
    
    if not validate_email(user_email):
        return jsonify({"success": False, "message": "邮箱格式不正确"}), 400
    
    # 验证激活码
    is_valid, message, code_data = validate_activation_code(activation_code)
    is_reentry = False
    
    # 检查是否是已绑定用户重入
    if not is_valid and code_data and code_data.get('status') == Config.CODE_STATUS_ACTIVE:
        if code_data.get('used_by') == user_email:
            # 邮箱匹配，允许重入
            is_valid = True
            is_reentry = True
        else:
            # 邮箱不匹配，报错
            log_warn("Auth", "激活码邮箱不匹配", email=user_email, code=activation_code[:8], bound=code_data.get('used_by'))
            return jsonify({"success": False, "message": "该激活码已被其他邮箱占用"}), 400
            
    if not is_valid:
        log_error("Auth", "激活码验证失败", message, code=activation_code[:8] + "...")
        return jsonify({"success": False, "message": message}), 400
    
    # 如果不是重入，则执行绑定逻辑（需要检查名额）
    if not is_reentry:
        from utils.redis_client import acquire_global_invite_lock, release_global_invite_lock
        from core.openai_service import refresh_stats

        # ⚠️ 关键：获取全局锁保证"检查名额+绑定"的原子性
        # 会自动等待最多5秒，期间会重试获取锁
        global_lock = acquire_global_invite_lock(timeout=30, max_wait=5)
        if not global_lock:
            return jsonify({"success": False, "message": "当前绑定人数较多，请稍后再试"}), 429
        
        try:
            from core.team_config_service import get_active_team_config
            active_config = get_active_team_config()

            if not active_config:
                return jsonify({"success": False, "message": "系统未配置，请联系管理员"}), 500

            active_account_id = active_config['account_id']

            # 获取 OpenAI 官方统计（含私自邀请的人）
            from core.openai_service import refresh_stats
            stats_data, _ = refresh_stats(force=True)
            api_in_use = stats_data.get("seats_in_use", 0) if stats_data else 0
            api_pending = stats_data.get("pending_invites", 0) if stats_data else 0
            seats_entitled = (stats_data.get("seats_entitled", 0) if stats_data else 0) or active_config.get("seats_entitled", 0)

            user_info = {
                "username": user_email.split('@')[0],
                "display_name": user_email.split('@')[0],
                "email": user_email
            }

            success, use_message, bind_meta = bind_activation_code_with_seat_check(
                activation_code,
                user_info,
                active_account_id,
                seats_entitled,
                api_in_use=api_in_use,
                api_pending=api_pending
            )

            if not success:
                if bind_meta and bind_meta.get("reason") == "full":
                    log_warn("Auth", "绑定名额已满（原子事务拦截）",
                             email=user_email,
                             total_occupied=bind_meta.get("total_occupied"),
                             seats_entitled=bind_meta.get("seats_entitled"),
                             api_in_use=bind_meta.get("api_in_use"),
                             api_pending=bind_meta.get("api_pending"),
                             local_new_count=bind_meta.get("local_new_count"))
                return jsonify({"success": False, "message": use_message}), 400

            log_info("Auth", "新用户绑定成功",
                     email=user_email,
                     remaining=(bind_meta or {}).get("available_after", 0),
                     code=activation_code[:12])
        finally:
            release_global_invite_lock(global_lock)
    
    # 创建用户会话
    from utils.helpers import get_client_ip_address
    client_ip = get_client_ip_address()
    
    session["user"] = {
        "id": f"act_{int(time.time())}_{user_email}",
        "username": user_email.split('@')[0],
        "name": user_email.split('@')[0],
        "email": user_email,
        "activation_code": activation_code,  # 保存完整激活码用于显示
        "ip": client_ip
    }
    
    log_info("Auth", "用户通过激活码登录", email=user_email, activation_code=activation_code[:12] + "...")
    session.permanent = True
    
    # 检查用户邀请状态
    invite_status = get_user_invite_status(user_email, code=activation_code)
    session["invite_status"] = invite_status
    
    return jsonify({
        "success": True, 
        "message": "激活成功！正在跳转...",
        "redirect": url_for("user.invite_page")
    })


@auth_bp.route("/logout")
def logout():
    """用户登出"""
    email = session.get("user", {}).get("email", "unknown")
    log_info("Auth", "用户登出", email=email)
    session.pop("user", None)
    session.pop("invite_status", None)
    return redirect(url_for("user.index"))


@auth_bp.route("/check_activation_code", methods=["POST"])
def check_activation_code():
    """检查激活码是否有效（不使用激活码）"""
    activation_code = request.form.get("activation_code", "").strip()
    
    if not activation_code:
        return jsonify({"valid": False, "message": "请输入激活码"})
    
    is_valid, message, _ = validate_activation_code(activation_code)
    return jsonify({"valid": is_valid, "message": message})
