"""
用户路由模块 - 简化版
"""
from flask import Blueprint, render_template, session, request, jsonify, redirect, url_for
from config import Config
from utils.redis_client import get_semaphore_status, check_rate_limit, acquire_semaphore, release_semaphore, redis_client, touch_active_user
from core.openai_service import refresh_stats
from core.invite_service import process_invite, get_user_invite_status, process_free_invite
from core.activation_code_service import validate_activation_code
from models.exceptions import TeamBannedException
from utils.logger import log_info, log_error, log_warn

user_bp = Blueprint('user', __name__)

def mask_email(email):
    if not email or '@' not in email:
        return email or ''
    local, domain = email.split('@', 1)
    prefix = local[:2] if len(local) > 2 else local[:1]
    return f"{prefix}***@{domain}"

def get_user_target_account(user_email, activation_code):
    from core.activation_code_service import get_invite_status_by_code, get_invite_status_by_email
    from core.team_config_service import get_active_account_id, get_team_config_by_account_id
    active_account_id = get_active_account_id()
    local_data = None
    if activation_code:
        local_data = get_invite_status_by_code(activation_code)
    if not local_data and user_email:
        local_data = get_invite_status_by_email(user_email)
    bound_account_id = local_data.get('bound_account_id') if local_data else None
    target_account_id = bound_account_id or active_account_id
    token = None
    if target_account_id and target_account_id != active_account_id:
        config = get_team_config_by_account_id(target_account_id)
        if config:
            token = config.get('authorization_token')
    return target_account_id, token


@user_bp.route("/")
def index():
    """首页 - 激活码登录"""
    # 标记用户活跃（用于智能刷新）
    touch_active_user(request.remote_addr or "anonymous")
    
    if "user" in session:
        return redirect(url_for("user.invite_page"))
    return render_template("index.html")


@user_bp.route("/invite")
def invite_page():
    """邀请页面 - 显示车位信息和邀请操作"""
    if "user" not in session:
        return redirect(url_for("user.index"))
    
    user = session["user"]
    activation_code = user.get("activation_code", "")
    user_email = user.get("email", "").lower()

    # 验证是否是限免用户，限免用户禁止进入查询页
    from core.activation_code_service import get_invite_status_by_email
    local_data = get_invite_status_by_email(user_email)
    if local_data and local_data.get('user_type') == 'free_invite':
        return render_template("error.html", message="无效查询", title="错误"), 403
    
    # 验证激活码是否仍然有效（未被删除、停用或解绑）
    if activation_code:
        is_valid, msg, code_data = validate_activation_code(activation_code)
        
        # 场景1: 激活码不存在或已停用
        if not is_valid and ("无效" in msg or "停用" in msg):
            log_warn("Auth", "激活码已失效，强制登出", code=activation_code[:8], reason=msg)
            session.pop("user", None)
            session.pop("invite_status", None)
            return redirect(url_for("user.index"))
        
        # 场景2: 激活码已被解绑（状态变回 UNUSED，或者绑定的邮箱不是当前用户）
        if code_data:
            bound_email = (code_data.get("used_by") or "").lower()
            code_status = code_data.get("status", 0)
            
            # 如果激活码是未使用状态，或者绑定的邮箱不是当前用户
            if code_status == Config.CODE_STATUS_UNUSED or (bound_email and bound_email != user_email):
                log_warn("Auth", "激活码已解绑或换绑，强制登出", 
                        code=activation_code[:8], 
                        user=user_email,
                        bound=bound_email)
                session.pop("user", None)
                session.pop("invite_status", None)
                return redirect(url_for("user.index"))
    
    # 标记用户活跃（用于智能刷新）
    touch_active_user(user_email or request.remote_addr)
    
    # 获取邀请状态
    invite_status = get_user_invite_status(user_email, code=activation_code)
    
    # 获取统计数据 (智能识别归属母号，实时获取)
    try:
        from core.activation_code_service import get_invite_status_by_code, get_invite_status_by_email
        from core.team_config_service import get_active_account_id
        from core.openai_service import refresh_stats_for_account
        
        active_account_id = get_active_account_id()
        local_data = None
        if activation_code:
            local_data = get_invite_status_by_code(activation_code)
        if not local_data:
            local_data = get_invite_status_by_email(user_email)
        bound_account_id = local_data.get('bound_account_id') if local_data else None
        
        if not bound_account_id or bound_account_id == active_account_id:
            stats_data, updated_at = refresh_stats()
        else:
            # 实时获取历史母号的统计数据
            stats_data, updated_at = refresh_stats_for_account(bound_account_id)
    except Exception as e:
        log_error("Invite", f"首屏加载统计失败: {str(e)}")
        stats_data, updated_at = None, None
    
    # 计算绑定母号的前缀
    bound_team_prefix = "未绑定"
    if local_data:
        b_id = local_data.get('bound_account_id')
        if b_id:
            bound_team_prefix = b_id[:6] + "..."
    elif invite_status != 'new':
        # 如果还没查到 local_data 但状态不是 new，尝试取当前的
        active_id = get_active_account_id()
        if active_id:
            bound_team_prefix = active_id[:6] + "..."

    return render_template(
        "invite.html",
        user=user,
        user_email=user_email,
        activation_code=activation_code,
        invite_status=invite_status,
        stats=stats_data,
        updated_at=updated_at,
        bound_team_prefix=bound_team_prefix
    )


@user_bp.route("/api/invite", methods=["POST"])
def api_invite():
    """发送邀请API"""
    if "user" not in session:
        return jsonify({"success": False, "message": "请先登录"}), 401
    
    user = session["user"]
    user_email = user.get("email", "")
    activation_code = user.get("activation_code", "")
    
    if not user_email:
        return jsonify({"success": False, "message": "邮箱信息丢失，请重新登录"}), 400
    
    status = get_user_invite_status(user_email, code=activation_code)
    if status == "expired":
        session["invite_status"] = "expired"
        return jsonify({"success": False, "message": "该激活码绑定的车位无法连接或已翻车，请重新购买"}), 400
    
    # 检查频率限制
    is_allowed, remaining, reset_time = check_rate_limit(user_email)
    if not is_allowed:
        return jsonify({"success": False, "message": f"请求过于频繁，请在 {reset_time} 秒后重试"}), 429
    
    # 获取信号量
    semaphore_token = acquire_semaphore()
    if not semaphore_token:
        return jsonify({"success": False, "message": "当前排队人数较多，请稍后再试"}), 429
    
    try:
        from utils.helpers import get_client_ip_address
        user["ip"] = get_client_ip_address() # 获取实时 IP 覆盖旧的
        result = process_invite(user_email, user)
        
        # 更新session中的状态
        if result.get("status") == "invited":
            session["invite_status"] = "pending"
        elif result.get("status") == "in_space":
            session["invite_status"] = "in_space"
        elif result.get("status") == "expired":
            session["invite_status"] = "expired"
        
        return jsonify(result)
    except TeamBannedException:
        session["invite_status"] = "expired"
        return jsonify({
            "success": False, 
            "status": "expired",
            "message": "该激活码绑定的车队已翻车或母号Token已过期，无法发送邀请，请联系客服进行确认。"
        }), 400
    except Exception as e:
        log_error("API", "邀请异常", str(e))
        return jsonify({"success": False, "message": f"系统异常: {str(e)}"}), 500
    finally:
        release_semaphore(semaphore_token)


@user_bp.route("/api/invite/free", methods=["POST"])
def api_free_invite():
    """限免邀请API"""
    data = request.json
    if not data or "email" not in data:
        return jsonify({"success": False, "message": "缺少邮箱参数"}), 400
        
    email = data["email"].strip().lower()
    if not email or "@" not in email:
        return jsonify({"success": False, "message": "邮箱格式不正确"}), 400

    # 检查频率限制
    is_allowed, remaining, reset_time = check_rate_limit(f"free_invite:{email}")
    if not is_allowed:
        return jsonify({"success": False, "message": f"请求过于频繁，请在 {reset_time} 秒后重试"}), 429

    try:
        result = process_free_invite(email)
        return jsonify(result)
    except Exception as e:
        log_error("API", "限免邀请异常", str(e))
        return jsonify({"success": False, "message": f"系统异常: {str(e)}"}), 500


@user_bp.route("/api/check-status", methods=["POST"])
def check_status():
    """检查当前邀请状态"""
    if "user" not in session:
        return jsonify({"success": False, "message": "请先登录"}), 401
    
    user_email = session["user"].get("email", "")
    activation_code = session["user"].get("activation_code", "")
    if not user_email:
        return jsonify({"success": False, "message": "邮箱信息丢失"}), 400
    
    status = get_user_invite_status(user_email, code=activation_code)
    session["invite_status"] = status
    
    return jsonify({
        "success": True,
        "status": status,
        "message": {
            "in_space": "您已在Team空间中",
            "pending": "您有待处理的邀请，请查看邮箱",
            "new": "点击下方按钮发送邀请"
        }.get(status, "未知状态")
    })


@user_bp.route("/stats")
def stats():
    """获取统计信息 - 智能识别归属母号，实时获取"""
    force_refresh = request.args.get("refresh") == "1"
    try:
        from core.activation_code_service import get_invite_status_by_code, get_invite_status_by_email, get_pending_boarding_count
        from core.team_config_service import get_active_account_id
        from core.openai_service import refresh_stats_for_account
        
        user_email = session.get("user", {}).get("email")
        activation_code = session.get("user", {}).get("activation_code")
        active_account_id = get_active_account_id()
        bound_account_id = None
        
        # 1. 检查用户是否归属于某个母号
        if activation_code:
            local_data = get_invite_status_by_code(activation_code)
        elif user_email:
            local_data = get_invite_status_by_email(user_email)
        else:
            local_data = None
        if local_data:
            bound_account_id = local_data.get('bound_account_id')
        
        # 2. 确定使用哪个母号的数据
        target_account_id = bound_account_id or active_account_id
        
        # 3. 获取本地统计
        local_boarding = get_pending_boarding_count(target_account_id)
        
        # 3.5 检查当前用户是否已占位
        user_has_seat = False
        if local_data:
            invite_status = local_data.get('invite_status', 'new')
            # 如果用户状态是 new, pending, in_space 中的任何一个，说明已经占位
            if invite_status in ['new', 'pending', 'in_space']:
                user_has_seat = True
        
        # 4. 如果用户未绑定，或者绑定的就是当前激活母号 -> 使用常规 refresh_stats
        if not bound_account_id or bound_account_id == active_account_id:
            data, updated_at = refresh_stats(force=force_refresh)
            semaphore = get_semaphore_status()
            
            # 获取限免配置
            from config import get_settings
            settings = get_settings()
            config_data = {
                "FREE_INVITE_ENABLED": settings.get("FREE_INVITE_ENABLED", False),
                "FREE_INVITE_END_TIME": settings.get("FREE_INVITE_END_TIME", 0)
            }
            
            return jsonify({
                "success": True,
                "data": data,
                "updated_at": updated_at,
                "semaphore": semaphore,
                "cached": not force_refresh,
                "local_boarding": local_boarding,
                "user_has_seat": user_has_seat,
                "config": config_data
            })
        
        # 5. 如果用户绑定的是历史母号 -> 实时获取该母号的统计数据
        else:
            data, updated_at = refresh_stats_for_account(bound_account_id, force=force_refresh)
            if data:
                return jsonify({
                    "success": True,
                    "data": data,
                    "updated_at": updated_at,
                    "semaphore": {"current": 0, "max": 1, "available": 1},  # 历史车队不参与排队
                    "is_historical": True,  # 标记为历史母号数据
                    "local_boarding": local_boarding,  # 本地待上车统计
                    "user_has_seat": user_has_seat  # 用户是否已占位
                })
            else:
                return jsonify({"success": False, "banned": True, "message": "车队已翻车"}), 503

    except TeamBannedException:
        return jsonify({"success": False, "banned": True, "message": "车已翻 - Team 账号状态异常"}), 503
    except Exception as e:
        log_error("Stats", f"获取统计失败: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500


@user_bp.route("/api/overloads")
def user_overloads():
    if "user" not in session:
        return jsonify({"success": False, "message": "请先登录"}), 401
    user_email = session.get("user", {}).get("email", "").lower()
    activation_code = session.get("user", {}).get("activation_code", "")
    try:
        from core.openai_service import fetch_space_members_from_api
        from core.activation_code_service import get_all_bound_emails
        from core.team_config_service import get_team_config_by_account_id
        target_account_id, token = get_user_target_account(user_email, activation_code)
        if not target_account_id:
            return jsonify({"success": False, "message": "未找到所属车队"}), 400
        items, total = fetch_space_members_from_api(1000, token, target_account_id)
        bound_emails = get_all_bound_emails()
        team_config = get_team_config_by_account_id(target_account_id)
        owner_email = (team_config.get("owner_email") or "").lower() if team_config else ""
        overloads = []
        for item in items:
            member_email = (item.get("email") or "").lower()
            if item.get("role") == "account-owner" or (owner_email and member_email == owner_email):
                continue
            # 过滤掉母号邮箱，防止被列入超载名单（即使用户手动把 role 设错了，只要邮箱对得上也豁免）
            if owner_email and member_email == owner_email:
                continue
            bound_info = bound_emails.get(member_email)
            activation = bound_info.get("code") if bound_info else None
            bound_account_id = bound_info.get("bound_account_id") if bound_info else ""
            is_overload = not (activation and bound_account_id == target_account_id)
            if is_overload:
                overloads.append({
                    "id": item.get("id"),
                    "email_masked": mask_email(member_email)
                })
        return jsonify({
            "success": True,
            "items": overloads,
            "total": total,
            "overload_count": len(overloads)
        })
    except TeamBannedException:
        return jsonify({"success": False, "banned": True, "message": "车队已翻车"}), 503
    except Exception as e:
        log_error("Overload", f"获取超载列表失败: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500


@user_bp.route("/api/overloads/remove", methods=["POST"])
def user_remove_overload():
    if "user" not in session:
        return jsonify({"success": False, "message": "请先登录"}), 401
    user_id = request.form.get("user_id", "").strip()
    if not user_id:
        return jsonify({"success": False, "message": "请提供用户ID"}), 400
    user_email = session.get("user", {}).get("email", "").lower()
    activation_code = session.get("user", {}).get("activation_code", "")
    try:
        from core.openai_service import refresh_stats_for_account, remove_space_member
        from core.activation_code_service import get_pending_boarding_count
        from core.team_config_service import get_active_account_id
        target_account_id, token = get_user_target_account(user_email, activation_code)
        if not target_account_id:
            return jsonify({"success": False, "message": "未找到所属车队"}), 400
        active_account_id = get_active_account_id()
        if target_account_id == active_account_id:
            data, _ = refresh_stats(force=True)
        else:
            data, _ = refresh_stats_for_account(target_account_id, force=True)
        if not data:
            return jsonify({"success": False, "message": "获取车队状态失败"}), 500
        local_boarding = get_pending_boarding_count(target_account_id)
        seats_in_use = data.get("seats_in_use", 0)
        seats_entitled = data.get("seats_entitled", 0)
        pending_invites = data.get("pending_invites", 0)
        local_waiting = local_boarding.get("waiting_invite", 0)
        total_occupied = seats_in_use + pending_invites + local_waiting
        if seats_entitled > 0 and total_occupied <= seats_entitled:
            return jsonify({"success": False, "message": "当前未超员"}), 403
        success, message = remove_space_member(user_id, token, target_account_id)
        if success:
            log_info("UserOverload", f"用户踢人成功", operator=user_email, target=user_id)
        return jsonify({"success": success, "message": message})
    except TeamBannedException:
        return jsonify({"success": False, "banned": True, "message": "车队已翻车"}), 503
    except Exception as e:
        log_error("Overload", f"踢人失败: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500


@user_bp.route("/health")
def health_check():
    """健康检查"""
    status = {"status": "healthy", "redis": False}
    try:
        redis_client.ping()
        status["redis"] = True
    except:
        status["status"] = "degraded"
    return jsonify(status)
