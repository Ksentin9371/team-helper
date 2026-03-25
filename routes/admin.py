"""
管理后台路由模块
包含激活码管理、邀请记录查看、系统状态等功能
"""
import os
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
from config import Config, get_settings, update_settings
from core.invite_service import get_invite_records, get_invite_stats
from core.openai_service import refresh_stats, fetch_pending_invites_from_api, fetch_space_members_from_api, set_cached_pending_invites, cancel_pending_invite, remove_space_member
from core.activation_code_service import (
    create_activation_codes, 
    get_activation_codes_page,
    get_activation_code_stats,
    delete_activation_code,
    unbind_activation_code,
    update_activation_code_binding,
    set_code_status,
    get_all_bound_emails
)
from core.team_config_service import (
    get_all_team_configs,
    get_team_configs_page,
    get_team_config,
    create_team_config,
    update_team_config,
    delete_team_config,
    set_active_config,
    get_active_team_config,
    get_active_account_id
)
from core.stock_service import (
    add_stock_codes,
    get_all_stock_codes,
    get_stock_codes_page,
    get_stock_stats,
    delete_stock_code,
    sync_stock_count,
    batch_delete_stock
)
from core.payment_service import (
    get_orders_page,
    get_order_stats,
    cancel_order,
    delete_order,
    manual_complete_order
)
from models.exceptions import TeamBannedException
from utils.logger import log_info, log_error

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


def admin_required(f):
    """管理员认证装饰器"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("admin_logged_in"):
            if request.is_json or request.headers.get('Accept') == 'application/json':
                return jsonify({"success": False, "message": "未授权"}), 401
            return redirect(url_for("admin.admin_page"))
        return f(*args, **kwargs)
    return decorated_function


@admin_bp.route("/")
def admin_page():
    if not session.get("admin_logged_in"):
        return render_template("admin_login.html")
    return render_template("admin.html")


@admin_bp.route("/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        return redirect(url_for("admin.admin_page"))
        
    password = request.form.get("password", "")
    if password == Config.ADMIN_PASSWORD:
        session["admin_logged_in"] = True
        session.permanent = True
        log_info("Admin", "后台登录成功")
        return redirect(url_for("admin.admin_page"))
    return render_template("admin_login.html", error="密码错误")


@admin_bp.route("/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin.admin_page"))


# ============ 激活码管理 API ============

@admin_bp.route("/api/activation-codes")
@admin_required
def list_activation_codes():
    """获取激活码列表"""
    include_used = request.args.get("include_used", "true").lower() == "true"
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    per_page = 25
    codes, total = get_activation_codes_page(include_used=include_used, page=page, per_page=per_page)
    stats = get_activation_code_stats()
    
    # 获取母号列表以映射名称
    from core.team_config_service import get_all_team_configs
    teams = get_all_team_configs()
    team_map = {t['account_id']: t['name'] for t in teams}
    
    # 将母号名称添加到激活码数据中
    for code in codes:
        bound_id = code.get('bound_account_id')
        code['bound_team_name'] = team_map.get(bound_id, '未知/已删除') if bound_id else '-'
        
    return jsonify({
        "success": True,
        "codes": codes,
        "stats": stats,
        "total": total
    })


@admin_bp.route("/api/activation-codes/generate", methods=["POST"])
@admin_required
def generate_activation_codes():
    """生成新的激活码"""
    try:
        count = int(request.form.get("count", 1))
        if count < 1 or count > 100:
            return jsonify({"success": False, "message": "生成数量需在 1-100 之间"}), 400
        
        note = request.form.get("note", "").strip()[:200]  # 限制备注长度
        
        codes = create_activation_codes(count=count, created_by="admin", note=note)
        log_info("Admin", f"生成了 {len(codes)} 个激活码", note=note)
        
        return jsonify({
            "success": True,
            "message": f"成功生成 {len(codes)} 个激活码",
            "codes": codes
        })
    except ValueError:
        return jsonify({"success": False, "message": "无效的数量"}), 400
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@admin_bp.route("/api/activation-codes/delete", methods=["POST"])
@admin_required
def delete_code():
    """删除激活码"""
    code = request.form.get("code", "").strip()
    if not code:
        return jsonify({"success": False, "message": "请提供激活码"}), 400
    
    force = request.form.get("force", "false").lower() == "true"
    
    success, message = delete_activation_code(code, force=force)
    return jsonify({"success": success, "message": message})


@admin_bp.route("/api/activation-codes/unbind", methods=["POST"])
@admin_required
def unbind_code():
    """解绑激活码"""
    code = request.form.get("code", "").strip()
    if not code:
        return jsonify({"success": False, "message": "请提供激活码"}), 400
        
    success, message = unbind_activation_code(code)
    if success:
        log_info("Admin", f"解绑激活码 {code}")
    return jsonify({"success": success, "message": message})


@admin_bp.route("/api/activation-codes/rebind", methods=["POST"])
@admin_required
def rebind_code():
    """换绑激活码"""
    code = request.form.get("code", "").strip()
    new_email = request.form.get("email", "").strip()
    
    if not code or not new_email:
        return jsonify({"success": False, "message": "请提供激活码和新邮箱"}), 400
        
    success, message = update_activation_code_binding(code, new_email)
    if success:
        log_info("Admin", f"激活码 {code} 换绑至 {new_email}")
    return jsonify({"success": success, "message": message})


@admin_bp.route("/api/activation-codes/status", methods=["POST"])
@admin_required
def update_code_status_route():
    """修改激活码状态"""
    code = request.form.get("code", "").strip()
    try:
        status = int(request.form.get("status", ""))
    except ValueError:
        return jsonify({"success": False, "message": "无效的状态值"}), 400
        
    if not code:
        return jsonify({"success": False, "message": "请提供激活码"}), 400
        
    success = set_code_status(code, status)
    if success:
        log_info("Admin", f"修改激活码 {code} 状态为 {status}")
        return jsonify({"success": True, "message": "状态更新成功"})
    else:
        return jsonify({"success": False, "message": "激活码不存在"}), 404


@admin_bp.route("/api/activation-codes/refresh-invite", methods=["POST"])
@admin_required
def refresh_invite_status():
    """刷新用户的邀请状态（用于待处理邀请的激活码）"""
    code = request.form.get("code", "").strip()
    email = request.form.get("email", "").strip()
    
    if not code and not email:
        return jsonify({"success": False, "message": "请提供激活码或邮箱"}), 400
    
    try:
        from core.invite_service import get_user_invite_status
        from core.activation_code_service import get_activation_code_by_code
        
        # 如果只提供了激活码，需要先查找对应的邮箱
        if code and not email:
            code_data = get_activation_code_by_code(code)
            if not code_data:
                return jsonify({"success": False, "message": "激活码不存在"}), 404
            email = code_data.get("used_by", "")
            if not email:
                return jsonify({"success": False, "message": "该激活码未绑定邮箱"}), 400
        
        # 获取最新的邀请状态
        status = get_user_invite_status(email, code=code)
        
        status_text = {
            'in_space': '已在空间',
            'pending': '待处理邀请',
            'expired': '已过期',
            'new': '未邀请'
        }.get(status, '未知')
        
        log_info("Admin", "刷新邀请状态", code=code[:8] if code else "N/A", email=email, status=status)
        
        return jsonify({
            "success": True,
            "message": f"状态已更新: {status_text}",
            "status": status,
            "status_text": status_text
        })
    except Exception as e:
        log_error("Admin", "刷新邀请状态失败", str(e))
        return jsonify({"success": False, "message": f"刷新失败: {str(e)}"}), 500


@admin_bp.route("/api/activation-codes/stats")
@admin_required
def activation_code_stats():
    """获取激活码统计"""
    stats = get_activation_code_stats()
    return jsonify({
        "success": True,
        "stats": stats
    })


# ============ 原有 API ============

@admin_bp.route("/api/records")
@admin_required
def admin_records():
    """获取邀请记录"""
    records = get_invite_records(200)
    for i, r in enumerate(reversed(records)):
        r['id'] = len(records) - i
    return jsonify({
        "success": True,
        "records": records,
        "total": len(records)
    })


@admin_bp.route("/api/stats")
@admin_required
def admin_stats():
    """获取邀请统计（包含当前母号统计和全站统计）"""
    from core.invite_record_service import get_global_stats
    from core.activation_code_service import get_pending_boarding_count
    from core.team_config_service import get_active_account_id
    
    stats = get_invite_stats()  # 当前母号的统计
    code_stats = get_activation_code_stats()
    global_stats = get_global_stats()  # 全站统计
    
    # 获取本地待上车人数（已绑定待邀请 + 已发送待接收）
    active_account_id = get_active_account_id()
    local_boarding = get_pending_boarding_count(active_account_id) if active_account_id else {}
    
    return jsonify({
        "success": True,
        "stats": stats,
        "global_stats": global_stats,  # 全站累计统计
        "activation_code_stats": code_stats,
        "local_boarding": local_boarding  # 本地待上车统计
    })


@admin_bp.route("/api/pending-invites")
@admin_required
def admin_pending_invites():
    """获取待处理邀请列表"""
    try:
        items, total = fetch_pending_invites_from_api(1000)
        set_cached_pending_invites(items, total)
        return jsonify({
            "success": True,
            "items": items,
            "total": total
        })
    except TeamBannedException:
        return jsonify({"success": False, "banned": True, "message": "车队已翻车"}), 503


@admin_bp.route("/api/pending-invites/cancel", methods=["POST"])
@admin_required
def cancel_invite():
    """取消待处理邀请"""
    email = request.form.get("email", "").strip()
    if not email:
        return jsonify({"success": False, "message": "请提供邮箱地址"}), 400
    
    success, message = cancel_pending_invite(email)
    if success:
        log_info("Admin", f"取消邀请 {email}")
        try:
            refresh_stats(force=True)
        except:
            pass
    return jsonify({"success": success, "message": message})


@admin_bp.route("/api/members")
@admin_required
def admin_members():
    """获取空间成员列表，关联激活码信息"""
    try:
        items, total = fetch_space_members_from_api(1000)
    except TeamBannedException:
        return jsonify({"success": False, "banned": True, "message": "车队已翻车"}), 503
    
    # 获取所有已绑定的邮箱映射
    bound_emails = get_all_bound_emails()
    active_account_id = get_active_account_id()
    active_config = get_active_team_config()
    owner_email = (active_config.get("owner_email") or "").lower() if active_config else ""
    
    # 为每个成员添加激活码信息
    overload_count = 0
    for item in items:
        member_email = (item.get("email") or "").lower()
        bound_info = bound_emails.get(member_email)
        is_admin = item.get("role") == "account-owner" or (owner_email and member_email == owner_email)
        activation_code = bound_info.get("code") if bound_info else None
        bound_account_id = bound_info.get("bound_account_id") if bound_info else ""

        item["activation_code"] = activation_code

        if is_admin:
            item["is_overload"] = False
            # 如果是母号邮箱，强制视为管理员角色（前端会显示为管理员且不可踢出）
            item["role"] = "account-owner"
        else:
            is_valid_binding = bool(activation_code) and bound_account_id == active_account_id
            item["is_overload"] = not is_valid_binding
            if item["is_overload"]:
                overload_count += 1
    
    return jsonify({
        "success": True,
        "items": items,
        "total": total,
        "overload_count": overload_count
    })


@admin_bp.route("/api/members/remove", methods=["POST"])
@admin_required
def remove_member():
    """踢出空间成员"""
    user_id = request.form.get("user_id", "").strip()
    if not user_id:
        return jsonify({"success": False, "message": "请提供用户ID"}), 400
    
    success, message = remove_space_member(user_id)
    if success:
        log_info("Admin", f"踢出成员 {user_id}")
        try:
            refresh_stats(force=True)
        except:
            pass
    return jsonify({"success": success, "message": message})


# ============ 母号配置管理 API ============

@admin_bp.route("/api/team-configs")
@admin_required
def list_team_configs():
    """获取所有母号配置列表（含本地待上车统计）"""
    from core.activation_code_service import get_pending_boarding_count
    include_banned = request.args.get("include_banned", "true").lower() == "true"
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    per_page = 25
    configs, total = get_team_configs_page(page=page, per_page=per_page, include_banned=include_banned)
    # 隐藏 token 敏感信息，并增加本地统计
    for config in configs:
        token = config.get('authorization_token', '')
        if token and len(token) > 50:
            config['authorization_token_masked'] = token[:30] + '...' + token[-20:]
        else:
            config['authorization_token_masked'] = token[:10] + '...' if token else ''
            
        # 注入本地待上车数据 (new / pending)
        acc_id = config.get('account_id')
        boarding = get_pending_boarding_count(acc_id)
        config['local_waiting_invite'] = boarding.get('waiting_invite', 0)
        config['local_pending_accept'] = boarding.get('pending_accept', 0)
        
    return jsonify({
        "success": True,
        "configs": configs,
        "total": total
    })


@admin_bp.route("/api/team-configs/<int:config_id>")
@admin_required
def get_team_config_detail(config_id):
    """获取单个母号配置详情"""
    config = get_team_config(config_id)
    if not config:
        return jsonify({"success": False, "message": "配置不存在"}), 404
    return jsonify({
        "success": True,
        "config": config
    })


@admin_bp.route("/api/team-configs/create", methods=["POST"])
@admin_required
def create_team_config_route():
    """创建新的母号配置"""
    name = request.form.get("name", "").strip() or "新母号"
    authorization_token = request.form.get("authorization_token", "").strip()
    account_id = request.form.get("account_id", "").strip()
    owner_email = request.form.get("owner_email", "").strip().lower()
    note = request.form.get("note", "").strip()
    is_active = request.form.get("is_active", "0") == "1"
    
    allow_overload = request.form.get("allow_overload", "0") == "1"
    try:
        max_overload = int(request.form.get("max_overload", "0"))
        if max_overload < 0: max_overload = 0
    except ValueError:
        max_overload = 0
    
    if not authorization_token or not account_id:
        return jsonify({"success": False, "message": "Token 和 Account ID 为必填项"}), 400
    
    try:
        config_id = create_team_config(
            name=name,
            authorization_token=authorization_token,
            account_id=account_id,
            owner_email=owner_email,
            note=note,
            is_active=1 if is_active else 0,
            allow_overload=1 if allow_overload else 0,
            max_overload=max_overload
        )
        log_info("Admin", f"创建母号配置 {name}", id=config_id)
        return jsonify({
            "success": True,
            "message": "创建成功",
            "config_id": config_id
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@admin_bp.route("/api/team-configs/batch-create", methods=["POST"])
@admin_required
def batch_create_team_config_route():
    """批量创建母号配置"""
    try:
        data = request.get_json()
        configs = data.get('configs', [])
        if not configs or not isinstance(configs, list):
            return jsonify({"success": False, "message": "无效的数据格式"}), 400
            
        success_count = 0
        fail_count = 0
        errors = []
        
        for i, config in enumerate(configs):
            try:
                name = config.get("name", "").strip() or f"母号{i+1}"
                authorization_token = config.get("authorization_token", "").strip()
                account_id = config.get("account_id", "").strip()
                owner_email = config.get("owner_email", "").strip().lower()
                note = config.get("note", "").strip()
                
                # Handle overload fields
                allow_overload = 1 if config.get("allow_overload", 0) in [1, "1", True] else 0
                
                try:
                    max_overload = int(config.get("max_overload", 0))
                    if max_overload < 0: max_overload = 0
                except (ValueError, TypeError):
                    max_overload = 0
                
                if not authorization_token or not account_id:
                    fail_count += 1
                    errors.append(f"第 {i+1} 条数据缺少 Token 或 Account ID")
                    continue
                    
                create_team_config(
                    name=name,
                    authorization_token=authorization_token,
                    account_id=account_id,
                    owner_email=owner_email,
                    note=note,
                    is_active=0, # Default to inactive for batch import
                    allow_overload=allow_overload,
                    max_overload=max_overload
                )
                success_count += 1
                
            except Exception as e:
                fail_count += 1
                errors.append(f"第 {i+1} 条数据导入失败: {str(e)}")
        
        log_info("Admin", f"批量导入母号配置: 成功 {success_count}, 失败 {fail_count}")
        return jsonify({
            "success": True,
            "success_count": success_count,
            "fail_count": fail_count,
            "errors": errors
        })
        
    except Exception as e:
        return jsonify({"success": False, "message": f"批量导入异常: {str(e)}"}), 500


@admin_bp.route("/api/team-configs/update", methods=["POST"])
@admin_required
def update_team_config_route():
    """更新母号配置"""
    try:
        config_id = int(request.form.get("id", 0))
    except ValueError:
        return jsonify({"success": False, "message": "无效的配置ID"}), 400
    
    if not config_id:
        return jsonify({"success": False, "message": "请提供配置ID"}), 400
    
    # 收集更新字段
    updates = {}
    for field in ['name', 'authorization_token', 'account_id', 'owner_email', 'note']:
        value = request.form.get(field)
        if value is not None:
            updates[field] = value.strip().lower() if field == "owner_email" else value.strip()
            
    # 特殊处理 checkbox 和 number
    if request.form.get("is_active") is not None:
        updates["is_active"] = 1 if request.form.get("is_active") == "1" else 0
        
    if request.form.get("allow_overload") is not None:
        updates["allow_overload"] = 1 if request.form.get("allow_overload") == "1" else 0
        
    if request.form.get("max_overload") is not None:
        try:
            val = int(request.form.get("max_overload"))
            updates["max_overload"] = max(0, val)
        except ValueError:
            updates["max_overload"] = 0
    
    if not updates:
        return jsonify({"success": False, "message": "没有提供要更新的字段"}), 400
    
    try:
        success = update_team_config(config_id, **updates)
        if success:
            log_info("Admin", f"更新母号配置", id=config_id)
            return jsonify({"success": True, "message": "更新成功"})
        else:
            return jsonify({"success": False, "message": "更新失败或配置不存在"}), 404
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@admin_bp.route("/api/team-configs/delete", methods=["POST"])
@admin_required
def delete_team_config_route():
    """删除母号配置"""
    try:
        config_id = int(request.form.get("id", 0))
    except ValueError:
        return jsonify({"success": False, "message": "无效的配置ID"}), 400
    
    if not config_id:
        return jsonify({"success": False, "message": "请提供配置ID"}), 400
    
    success, message = delete_team_config(config_id)
    if success:
        log_info("Admin", f"删除母号配置", id=config_id)
    return jsonify({"success": success, "message": message})

@admin_bp.route("/api/team-configs/activate", methods=["POST"])
@admin_required
def activate_team_config():
    """切换激活的母号配置"""
    try:
        config_id = int(request.form.get("id", 0))
    except ValueError:
        return jsonify({"success": False, "message": "无效的配置ID"}), 400
    
    if not config_id:
        return jsonify({"success": False, "message": "请提供配置ID"}), 400
    
    success = set_active_config(config_id)
    if success:
        log_info("Admin", f"切换激活母号配置", id=config_id)
        return jsonify({"success": True, "message": "已切换为当前激活配置"})
    else:
        return jsonify({"success": False, "message": "切换失败"}), 500


@admin_bp.route("/api/team-configs/check-status", methods=["POST"])
@admin_required
def check_team_config_status():
    """手动检测母号状态并同步更新关联激活码"""
    try:
        config_id = int(request.form.get("id", 0))
    except ValueError:
        return jsonify({"success": False, "message": "无效的配置ID"}), 400
    
    if not config_id:
        return jsonify({"success": False, "message": "请提供配置ID"}), 400
    
    config = get_team_config(config_id)
    if not config:
        return jsonify({"success": False, "message": "配置不存在"}), 404
    
    account_id = config.get("account_id")
    token = config.get("authorization_token")
    
    try:
        from core.openai_service import fetch_stats_from_api
        from core.team_config_service import update_team_config_stats
        # 调用接口尝试获取统计信息，如果能获取成功则说明未封禁
        stats = fetch_stats_from_api(custom_token=token, custom_account_id=account_id)
        
        # 同步更新统计数据和状态
        update_team_config_stats(config_id, stats)
        update_team_config(config_id, status=1)
        
        # 应用超载逻辑 (仅用于显示)
        display_entitled = stats.get('seats_entitled', 0)
        allow_overload = config.get('allow_overload', 0)
        max_overload = config.get('max_overload', 0)
        if allow_overload and max_overload > 0 and display_entitled > 0:
            display_entitled += max_overload

        log_info("Admin", f"手动检测母号状态: 正常", name=config.get('name'))
        return jsonify({
            "success": True, 
            "message": f"检测完成：母号状态正常 (车位: {stats.get('seats_in_use')}/{display_entitled})", 
            "is_banned": False
        })
        
    except TeamBannedException:
        # 捕获到封禁异常
        from core.team_config_service import set_team_config_failed
        set_team_config_failed(config_id)
        log_info("Admin", f"手动检测母号状态: 已封禁", name=config.get('name'))
        return jsonify({"success": True, "message": "检测完成：母号已封禁，关联激活码已标记为过期", "is_banned": True})
        
    except Exception as e:
        log_error("Admin", f"检测母号状态失败: {str(e)}")
        return jsonify({"success": False, "message": f"检测失败: {str(e)}"}), 500


# ============ 库存管理 API ============

@admin_bp.route("/api/stock/codes")
@admin_required
def list_stock_codes():
    """获取库存激活码列表"""
    include_sold = request.args.get("include_sold", "true").lower() == "true"
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    per_page = 25
    codes, total = get_stock_codes_page(page=page, per_page=per_page, include_sold=include_sold)
    stats = get_stock_stats()
    return jsonify({
        "success": True,
        "codes": codes,
        "stats": stats,
        "total": total
    })


@admin_bp.route("/api/stock/add", methods=["POST"])
@admin_required
def add_stock():
    """批量添加库存激活码"""
    codes_text = request.form.get("codes", "").strip()
    if not codes_text:
        return jsonify({"success": False, "message": "请提供激活码"}), 400
    
    # 支持多种分隔符：换行、逗号、空格
    import re
    codes_list = re.split(r'[\n,\s]+', codes_text)
    codes_list = [c.strip() for c in codes_list if c.strip()]
    
    if not codes_list:
        return jsonify({"success": False, "message": "没有有效的激活码"}), 400
    
    result = add_stock_codes(codes_list)
    log_info("Admin", f"批量添加库存", success=result['success'], failed=result['failed'])
    
    return jsonify({
        "success": True,
        "message": f"成功添加 {result['success']} 个，失败 {result['failed']} 个",
        "result": result
    })


@admin_bp.route("/api/stock/delete", methods=["POST"])
@admin_required
def delete_stock():
    """删除库存激活码（支持级联删除已售激活码）"""
    code = request.form.get("code", "").strip()
    force = request.form.get("force", "false").lower() == "true"
    
    if not code:
        return jsonify({"success": False, "message": "请提供激活码"}), 400
    
    success, message, is_sold = delete_stock_code(code, force=force)
    
    # 如果是已售激活码且未强制删除，返回需要确认的状态
    if not success and is_sold:
        return jsonify({
            "success": False, 
            "message": message,
            "need_confirm": True  # 前端需要二次确认
        }), 200  # 使用200状态码，因为这不是错误
    
    return jsonify({"success": success, "message": message})


@admin_bp.route("/api/stock/batch-delete", methods=["POST"])
@admin_required
def batch_delete_stock_route():
    """批量删除库存激活码"""
    data = request.json
    if not data or "ids" not in data:
        return jsonify({"success": False, "message": "缺少参数"}), 400
    
    ids = data["ids"]
    if not isinstance(ids, list):
        return jsonify({"success": False, "message": "参数格式错误"}), 400
        
    success_count, total = batch_delete_stock(ids)
    return jsonify({
        "success": True, 
        "message": f"成功删除 {success_count} 个激活码，失败 {total - success_count} 个"
    })


@admin_bp.route("/api/stock/sync", methods=["POST"])
@admin_required
def sync_stock():
    """同步库存计数到Redis"""
    count = sync_stock_count()
    return jsonify({
        "success": True,
        "message": f"同步成功，当前库存: {count}",
        "count": count
    })


# ============ 订单管理 API ============

@admin_bp.route("/api/orders")
@admin_required
def list_orders():
    """获取订单列表"""
    search = request.args.get("search", "").strip()
    only_completed = request.args.get("only_completed", "false").lower() == "true"
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    per_page = 25
    orders, total = get_orders_page(page=page, per_page=per_page, search=search or None, only_completed=only_completed)
    stats = get_order_stats()
    
    return jsonify({
        "success": True,
        "orders": orders,
        "stats": stats,
        "total": total
    })


@admin_bp.route("/api/epay/config")
@admin_required
def get_epay_config():
    """获取易支付配置（只读展示）"""
    return jsonify({
        "success": True,
        "config": {
            "merchant_id": Config.EPAY_MERCHANT_ID,
            "notify_url": Config.EPAY_NOTIFY_URL,
            "return_url": Config.EPAY_RETURN_URL,
            "gateway_url": Config.EPAY_GATEWAY_URL,
            "product_price": Config.EPAY_PRODUCT_PRICE,
            "enabled": bool(Config.EPAY_MERCHANT_ID and Config.EPAY_API_KEY)
        }
    })


@admin_bp.route("/api/settings")
@admin_required
def get_admin_settings():
    settings = get_settings(include_sensitive=False)
    return jsonify({
        "success": True,
        "settings": settings
    })


@admin_bp.route("/api/settings", methods=["POST"])
@admin_required
def update_admin_settings():
    data = request.get_json(silent=True)
    if data is None:
        data = request.form.to_dict()
    success, message = update_settings(data)
    if not success:
        return jsonify({"success": False, "message": message}), 400
    settings = get_settings(include_sensitive=False)
    return jsonify({
        "success": True,
        "message": message,
        "settings": settings
    })


@admin_bp.route("/api/order/cancel", methods=["POST"])
@admin_required
def cancel_admin_order():
    """管理员取消订单"""
    order_id = request.form.get("order_id", "").strip()
    
    if not order_id:
        return jsonify({"success": False, "message": "请提供订单号"}), 400
    
    success, code, message = cancel_order(order_id, reason="admin_cancel")
    
    if success:
        log_info("Admin", "管理员取消订单", order=order_id, code=code[:8] if code else "无")
        return jsonify({
            "success": True,
            "message": message
        })
    else:
        return jsonify({
            "success": False,
            "message": message
        }), 400


@admin_bp.route("/api/order/delete", methods=["POST"])
@admin_required
def delete_admin_order():
    """管理员删除订单（解绑激活码）"""
    order_id = request.form.get("order_id", "").strip()
    
    if not order_id:
        return jsonify({"success": False, "message": "请提供订单号"}), 400
    
    success, message = delete_order(order_id)
    
    if success:
        log_info("Admin", "管理员删除订单", order=order_id)
        return jsonify({
            "success": True,
            "message": message
        })
    else:
        return jsonify({
            "success": False,
            "message": message
        }), 400


@admin_bp.route("/api/order/complete", methods=["POST"])
@admin_required
def complete_admin_order():
    """管理员手动补单（标记为已支付）"""
    order_id = request.form.get("order_id", "").strip()
    
    if not order_id:
        return jsonify({"success": False, "message": "请提供订单号"}), 400
    
    success, message = manual_complete_order(order_id)
    
    if success:
        log_info("Admin", "管理员手动补单", order=order_id)
        return jsonify({
            "success": True,
            "message": message
        })
    else:
        return jsonify({
            "success": False,
            "message": message
        }), 400


@admin_bp.route("/api/logs")
@admin_required
def admin_logs():
    """获取系统日志，包含自动清理逻辑"""
    log_path = "data/logs.log"
    max_lines = 25000
    display_lines = 2500
    
    if not os.path.exists(log_path):
        return jsonify({"success": True, "logs": "日志文件尚未创建", "total": 0})
    
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        
        total_lines = len(lines)
        
        # 自动清理逻辑：如果超过 25000 行，则只保留最新的 2500 行
        if total_lines > max_lines:
            old_count = total_lines
            new_lines = lines[-display_lines:]
            with open(log_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            lines = new_lines
            total_lines = len(lines)
            log_info("System", "日志自动清理", f"原行数 {old_count}, 已保留最新 {display_lines} 行")
        
        # 返回最新的 2500 行给前端
        logs_to_show = "".join(lines[-display_lines:])
        
        return jsonify({
            "success": True,
            "logs": logs_to_show,
            "total": total_lines
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500



@admin_bp.route("/api/audit/log", methods=["POST"])
@admin_required
def add_audit_log_route():
    """手动记录审计日志（用于前端记录操作）"""
    data = request.json
    if not data:
        return jsonify({"success": False, "message": "Missing data"}), 400
        
    action = data.get("action", "unknown")
    details = data.get("details", "")
    status = data.get("status", "info")
    
    from utils.logger import log_info, log_error, log_warn
    if status == "success":
        log_info("Audit", action, details=details, ip=request.remote_addr)
    elif status == "error":
        log_error("Audit", action, details=details, ip=request.remote_addr)
    else:
        log_warn("Audit", action, details=details, ip=request.remote_addr)
        
    return jsonify({"success": True})
