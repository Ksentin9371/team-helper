"""
邀请服务模块 - 简化版
仅包含发送邀请和检查状态功能
"""
import json
import time
from flask import session
from datetime import datetime, timezone, timedelta
from config import Config
from utils.logger import log_info, log_error, log_warn
from utils.redis_client import acquire_invite_lock, release_invite_lock
from core.openai_service import (
    refresh_stats, 
    fetch_pending_invites_from_api, 
    get_cached_pending_invites, 
    fetch_space_members_from_api, 
    send_chatgpt_invite,
    check_invite_pending
)
from core.invite_record_service import (
    add_invite_record as sqlite_add_record,
    get_invite_records as sqlite_get_records,
    get_invite_stats as sqlite_get_stats,
    get_global_stats
)
from models.exceptions import TeamBannedException


def check_seats_available(force_refresh=False, exclude_email=None, exclude_code=None, custom_account_id=None):
    """检查是否有可用名额（精准校验）
    
    Args:
        force_refresh: 是否强制刷新 API 统计
        exclude_email: 排除在统计之外的邮箱（通常是当前正在操作的用户）
        exclude_code: 排除在统计之外的激活码（优先用于精准识别当前用户）
        custom_account_id: 指定检查的母号 ID，不传则检查当前激活母号
    """
    try:
        from core.openai_service import refresh_stats, refresh_stats_for_account
        from core.activation_code_service import get_pending_boarding_count
        from core.team_config_service import get_active_account_id
        
        target_account_id = custom_account_id or get_active_account_id()
        
        # 1. 获取 API 统计
        if not custom_account_id or custom_account_id == get_active_account_id():
            data, _ = refresh_stats(force=force_refresh)
        else:
            data, _ = refresh_stats_for_account(target_account_id, force=force_refresh)
            
        if not data:
            return False, None
            
        seats_in_use = data.get("seats_in_use", 0)
        seats_entitled = data.get("seats_entitled", 0)
        
        # 2. 获取本地统计：仅取"已绑定未发送"的人数
        local_boarding = get_pending_boarding_count(target_account_id)
        local_new_count = local_boarding.get('waiting_invite', 0) 
        
        # 3. 三方加和精准计算：
        # 已占用 = (API 已入位) + (API 已发邮件待接收) + (本地已绑定激活码未发邮件)
        api_in_use = data.get("seats_in_use", 0)
        api_pending = data.get("pending_invites", 0)
        
        # 总占用
        total_occupied = api_in_use + api_pending + local_new_count
        
        # 除我之外的人占了多少坑？
        occupied_by_others = total_occupied
        is_whitelisted = False
        user_local = None
        if exclude_code or exclude_email:
            # 检查当前操作的用户是否已经包含在 total_occupied 的某一部分中
            from core.activation_code_service import get_invite_status_by_code, get_invite_status_by_email

            if exclude_code:
                user_local = get_invite_status_by_code(exclude_code)
            if not user_local and exclude_email:
                user_local = get_invite_status_by_email(exclude_email)

            if user_local:
                user_status = user_local.get('invite_status') or 'new'
                user_account_id = user_local.get('bound_account_id') or ''
                is_same_account = not user_account_id or user_account_id == target_account_id
                if is_same_account and user_status in ['new', 'pending', 'in_space']:
                    # 他已经占了一个坑了，所以他自己不算"别人"
                    occupied_by_others = max(0, occupied_by_others - 1)
                    is_whitelisted = True
        
        available = seats_entitled - occupied_by_others
        
        # 只要剩余名额还能容纳至少一个新邀请，或者是白名单占位用户，就返回 True
        return is_whitelisted or available > 0, data
    except TeamBannedException:
        raise
    except Exception as e:
        log_error("Seats", "名额检查异常", str(e))
        return False, None


def check_user_in_space(email, custom_token=None, custom_account_id=None):
    """检查用户是否已在空间中（支持自定义母号）"""
    try:
        items, _ = fetch_space_members_from_api(6, custom_token, custom_account_id)
        for item in items:
            member_email = item.get("email", "").lower()
            if member_email == email.lower():
                return True
    except TeamBannedException:
        raise
    except:
        pass
    return False


def check_user_already_invited(email, custom_token=None, custom_account_id=None):
    """检查用户是否已有待处理邀请（支持自定义母号）"""
    # 只有当前激活母号才使用 Redis 缓存
    if custom_account_id is None:
        cached = get_cached_pending_invites()
        if cached:
            items = cached.get("items", [])
            for item in items:
                if item.get("email_address", "").lower() == email.lower():
                    return True
    # 实时查询
    return check_invite_pending(email, custom_token, custom_account_id)


def get_user_invite_status(email, code=None):
    """获取用户的邀请状态
    
    优先逻辑：
    1. 先检查本地数据库中的状态（如果提供了 code，按 code 查，否则按 email 查）
    2. 如果本地状态绑定了旧母号，使用该母号配置请求一次信息（临时验证且不参与自动刷新）
    3. 如果验证失败（由于母号过期/封禁等），状态标记为 expired
    4. 如果没有绑定或已过期，则检查当前活跃母号
    
    Returns:
        str: 'in_space' - 已在空间中
             'pending' - 有待处理邀请
             'expired' - 已过期（车队不可用）
             'new' - 新用户，或需重新邀请
    """
    from core.activation_code_service import get_invite_status_by_email, get_invite_status_by_code, update_invite_status
    from core.team_config_service import get_active_account_id, get_all_team_configs, get_active_team_config
    
    active_account_id = get_active_account_id()
    
    # 1. 先检查本地数据库中保存的状态
    if code:
        local_data = get_invite_status_by_code(code)
    else:
        local_data = get_invite_status_by_email(email)
        
    if local_data:
        saved_status = local_data.get('invite_status', 'new')
        bound_id = local_data.get('bound_account_id')
        if code and not bound_id:
            update_invite_status(email, 'expired', code=code)
            return 'expired'
        
        # 场景 A: 用户绑定的是当前激活母号
        if bound_id == active_account_id:
            # 校验当前母号是否正常
            active_team = get_active_team_config()
            if active_team and active_team.get('status', 1) <= 0:
                return 'expired'
                
            # 仅直接返回 in_space，pending 状态需要去 API 复核（因为用户可能已经接受邀请）
            if saved_status == 'in_space':
                return saved_status
                
        # 场景 B: 用户绑定的是历史母号 -> 临时请求验证
        elif bound_id and bound_id != active_account_id:
            # 查找历史母号的配置
            teams = get_all_team_configs()
            old_team = next((t for t in teams if t['account_id'] == bound_id), None)
            
            if old_team:
                # 首先检查该历史母号是否已经标记为 翻车(0) 或 软删除(-1)
                if old_team.get('status', 1) <= 0:
                    update_invite_status(email, 'expired', bound_id, code=code)
                    return 'expired'
                
                try:
                    token = old_team['authorization_token']
                    # 临时校验状态
                    if check_user_in_space(email, token, bound_id):
                        update_invite_status(email, 'in_space', bound_id, code=code)
                        return 'in_space'
                    if check_user_already_invited(email, token, bound_id):
                        update_invite_status(email, 'pending', bound_id, code=code)
                        return 'pending'
                    
                    # 如果 API 没查到，说明可能被手动踢出，重置为 new 或 expired
                    return 'new'
                except TeamBannedException:
                    # 车队封禁，状态标记为已过期
                    update_invite_status(email, 'expired', bound_id, code=code)
                    return 'expired'
                except Exception as e:
                    log_error("Invite", f"校验历史车队失败: {str(e)}")
                    # 网络或其他错误，暂时返回 current_status 或 expired
                    return 'expired'
    
    # 3. 检查当前活跃母号
    try:
        if check_user_in_space(email):
            update_invite_status(email, 'in_space', active_account_id, code=code)
            return 'in_space'
            
        if check_user_already_invited(email):
            update_invite_status(email, 'pending', active_account_id, code=code)
            return 'pending'
    except TeamBannedException:
        update_invite_status(email, 'expired', active_account_id, code=code)
        return 'expired'
    
    return 'new'


def process_invite(user_email, user_info):
    """处理邀请请求
    
    Args:
        user_email: 用户邮箱（用户自己提供的）
        user_info: 用户信息字典
        
    Returns:
        dict: 处理结果
    """
    from utils.redis_client import acquire_global_invite_lock, release_global_invite_lock
    from core.team_config_service import get_active_account_id, get_team_config_by_account_id
    from core.activation_code_service import get_invite_status_by_code, get_invite_status_by_email
    
    user_id = user_info.get("id", user_email)
    
    # 获取用户锁，防止同一用户重复提交
    lock_token = acquire_invite_lock(user_id)
    if not lock_token:
        return {"success": False, "message": "已有任务在处理中，请不要重复提交"}
    
    try:
        # 确定目标母号
        target_account_id = get_active_account_id()
        target_token = None
        
        # 尝试获取用户绑定信息
        code = user_info.get("activation_code")
        local_data = None
        if code:
            local_data = get_invite_status_by_code(code)
        if not local_data:
            local_data = get_invite_status_by_email(user_email)
            
        if local_data:
            bound_id = local_data.get('bound_account_id')
            # 如果绑定了母号，且不是当前激活的母号
            if bound_id and bound_id != target_account_id:
                # 检查该母号状态
                team_config = get_team_config_by_account_id(bound_id)
                # status=1 表示正常，0表示翻车
                if team_config and team_config.get('status', 1) > 0:
                    target_account_id = bound_id
                    target_token = team_config.get('authorization_token')
                    log_info("Invite", f"检测到用户绑定历史母号，定向邀请", user=user_email, team=bound_id[:8])
                else:
                    log_warn("Invite", f"用户绑定的母号 {bound_id[:8]} 已失效/翻车，拒绝邀请", user=user_email)
                    return {
                        "success": False, 
                        "status": "expired",
                        "message": "该激活码绑定的车队已翻车或母号Token已过期，无法发送邀请，请联系客服进行确认。"
                    }

        # 检查是否已在空间（使用目标母号检查）
        if check_user_in_space(user_email, custom_token=target_token, custom_account_id=target_account_id):
            return {
                "success": True, 
                "status": "in_space",
                "message": "您已经在 Team 空间中了"
            }
        
        # 检查是否已有待处理邀请（使用目标母号检查）
        if check_user_already_invited(user_email, custom_token=target_token, custom_account_id=target_account_id):
            return {
                "success": True, 
                "status": "pending",
                "message": "您已有待处理的邀请，请查看邮箱"
            }
        
        # ⚠️ 关键路径：获取全局锁，保证"检查名额+发送邀请"的原子性
        # 防止多人同时通过名额检查导致超载
        # 会自动等待最多5秒，期间会重试获取锁
        global_lock = acquire_global_invite_lock(timeout=30, max_wait=5)
        if not global_lock:
            return {
                "success": False, 
                "message": "当前排队人数较多，请稍后再试"
            }
        
        try:
            # 在全局锁内检查名额（传入当前邮箱以排除自身统计，防止自锁）
            available, stats = check_seats_available(
                force_refresh=True,
                exclude_email=user_email,
                exclude_code=code,
                custom_account_id=target_account_id
            )
            if not available:
                return {
                    "success": False, 
                    "status": "full",
                    "message": "当前名额已满，请联系管理员"
                }
            
            # 在全局锁内发送邀请
            invite_success, invite_msg = send_chatgpt_invite(user_email, custom_token=target_token, custom_account_id=target_account_id)
            if invite_success:
                # 记录邀请
                add_invite_record(user_info, user_email, True, "邀请成功", account_id=target_account_id)
                
                # 更新本地邀请状态
                from core.activation_code_service import update_invite_status
                update_invite_status(user_email, 'pending', target_account_id, code=user_info.get("activation_code"))
                
                # 🚀 关键操作：发送任务成功后，立即触发一次 OpenAI 统计刷新，确保数据实时
                from core.openai_service import refresh_stats, refresh_stats_for_account
                try:
                    if target_account_id == get_active_account_id():
                        refresh_stats(force=True)
                    else:
                        refresh_stats_for_account(target_account_id, force=True)
                except Exception as e:
                    log_warn("Invite", f"即时刷新统计失败 (不影响邀请结果): {str(e)}")
                
                return {
                    "success": True, 
                    "status": "invited",
                    "message": "邀请发送成功！请查看您的邮箱"
                }
            else:
                add_invite_record(user_info, user_email, False, f"发送失败: {invite_msg}", account_id=target_account_id)
                return {
                    "success": False, 
                    "status": "error",
                    "message": f"邀请发送失败: {invite_msg}"
                }
        finally:
            release_global_invite_lock(global_lock)
    finally:
        release_invite_lock(user_id, lock_token)


def add_invite_record(user, email, success, message="", account_id=None):
    """添加邀请记录（使用 SQLite 存储，与母号绑定）"""
    from core.team_config_service import get_active_account_id
    if not account_id:
        account_id = get_active_account_id()
    return sqlite_add_record(account_id, user, email, success, message)


def get_invite_records(limit=100, account_id=None):
    """获取邀请记录
    
    Args:
        limit: 返回记录数量
        account_id: 母号 ID，为 None 时返回当前激活母号的记录
    """
    if account_id is None:
        from core.team_config_service import get_active_account_id
        account_id = get_active_account_id()
    return sqlite_get_records(account_id=account_id, limit=limit)


def process_free_invite(email):
    """处理限免邀请请求
    
    Args:
        email: 用户邮箱
        
    Returns:
        dict: 处理结果
    """
    from config import Config
    if not Config.FREE_INVITE_ENABLED:
        return {"success": False, "message": "限免活动未开启"}
        
    # 检查活动是否过期
    import time
    if Config.FREE_INVITE_END_TIME > 0 and time.time() > Config.FREE_INVITE_END_TIME:
        return {"success": False, "message": "限免活动已结束"}

    from core.activation_code_service import get_invite_status_by_email, get_db_connection, get_beijing_time
    from core.team_config_service import get_active_account_id
    from utils.redis_client import acquire_global_invite_lock, release_global_invite_lock
    
    # 1. 检查是否已邀请过（本地库检查）- 仅记录日志，不拦截
    local_data = get_invite_status_by_email(email)
    if local_data:
        log_info("Invite", "用户重复参加活动（已放行）", email=email)
        # return {"success": False, "message": "该邮箱已参加过活动，请勿重复邀请"}
        
    # 2. 检查是否在 API 中已存在 - 仅记录日志，不拦截
    target_account_id = get_active_account_id()
    if check_user_in_space(email, custom_account_id=target_account_id):
        log_info("Invite", "用户已在车队中（已放行）", email=email)
        # return {"success": False, "message": "该邮箱已在车队中"}
    if check_user_already_invited(email, custom_account_id=target_account_id):
        log_info("Invite", "用户已有待处理邀请（已放行）", email=email)
        # return {"success": False, "message": "该邮箱已有待处理邀请"}

    # 3. 检查名额并发送邀请（带全局锁）
    global_lock = acquire_global_invite_lock(timeout=30, max_wait=5)
    if not global_lock:
        return {"success": False, "message": "系统繁忙，请稍后再试"}
        
    try:
        available, stats = check_seats_available(force_refresh=True, exclude_email=email, custom_account_id=target_account_id)
        if not available:
            return {"success": False, "message": "当前车队名额已满，请稍后再试"}
            
        # 发送邀请
        invite_success, invite_msg = send_chatgpt_invite(email, custom_account_id=target_account_id)
        if invite_success:
            # 特殊处理：记录到 activation_codes 表，标记为 free_invite
            # 不调用 add_invite_record (不记录到邀请日志)
            with get_db_connection() as conn:
                cursor = conn.cursor()
                now = get_beijing_time()
                # 插入一条没有 code 的记录，或者生成一个虚拟 code
                cursor.execute('''
                    INSERT INTO activation_codes 
                    (code, created_at, status, used_at, used_by, invite_status, bound_account_id, user_type, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (f"FREE-{int(time.time())}", now, 1, now, email, 'pending', target_account_id, 'free_invite', '限免活动邀请'))
            
            # 刷新统计
            from core.openai_service import refresh_stats
            try:
                refresh_stats(force=True)
            except:
                pass
                
            log_info("Invite", f"限免邀请发送成功", email=email)
            return {"success": True, "message": f"邀请邮件已发送至 {email}，请查收！"}
        else:
            log_error("Invite", f"限免邀请发送失败", email=email, error=invite_msg)
            return {"success": False, "message": f"邀请发送失败: {invite_msg}"}
    finally:
        release_global_invite_lock(global_lock)

def get_invite_stats(account_id=None):
    """获取邀请统计
    
    Args:
        account_id: 母号 ID，为 None 时返回当前激活母号的统计
    """
    if account_id is None:
        from core.team_config_service import get_active_account_id
        account_id = get_active_account_id()
    return sqlite_get_stats(account_id=account_id)

