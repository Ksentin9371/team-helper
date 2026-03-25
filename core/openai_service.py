import requests
import json
import time
from datetime import datetime, timezone, timedelta
from config import Config
from utils.logger import log_info, log_error, log_warn
from utils.redis_client import redis_client
from models.exceptions import TeamBannedException
from core.team_config_service import get_active_authorization_token, get_active_account_id, get_active_team_config, update_team_config_stats, get_team_config_by_account_id
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

if not Config.VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 初始化全局 Session
session = requests.Session()
session.verify = Config.VERIFY_SSL

# 配置代理
proxies = {}
if Config.SOCKS5_PROXY:
    # 强制使用 socks5h 以确保远程 DNS 解析，解决许多连接问题
    proxy_url = Config.SOCKS5_PROXY
    if proxy_url.startswith("socks5://"):
        proxy_url = proxy_url.replace("socks5://", "socks5h://")
    
    proxies = {
        "http": proxy_url,
        "https": proxy_url
    }
elif Config.HTTP_PROXY or Config.HTTPS_PROXY:
    proxies = {
        "http": Config.HTTP_PROXY,
        "https": Config.HTTPS_PROXY
    }

if proxies:
    session.proxies.update(proxies)

# 配置重试策略
retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)


def get_proxies():
    """获取当前的代理配置"""
    return session.proxies

def verify_connectivity():
    """检查是否可以连接到 ChatGPT 官方服务器"""
    try:
        url = "https://chatgpt.com"
        headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        # 使用 GET 请求并限制读取，某些 CDN 会拦截无 Header 的 HEAD 请求
        response = session.get(url, headers=headers, timeout=10, stream=True)
        status = response.status_code
        
        if status == 200 or (300 <= status < 400):
            return True, f"成功连接至 {url} (HTTP {status})"
        elif status == 403:
            return False, f"连接被拒绝 (HTTP 403 Forbidden)。这通常意味着该代理 IP 被 Cloudflare 或 OpenAI 封锁，建议更换代理。"
        elif status == 429:
            return False, f"请求过于频繁 (HTTP 429 Too Many Requests)，请稍后再试。"
        else:
            return False, f"连接 {url} 异常 (HTTP {status})"
    except Exception as e:
        return False, f"无法连接至 {url}: {str(e)}"

def get_current_account_id():
    """获取当前激活的 Account ID"""
    return get_active_account_id()

def build_base_headers(custom_token=None, custom_account_id=None):
    return {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "authorization": custom_token or get_active_authorization_token(),
        "chatgpt-account-id": custom_account_id or get_active_account_id(),
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

def build_invite_headers(custom_token=None, custom_account_id=None):
    headers = build_base_headers(custom_token, custom_account_id)
    headers.update({
        "content-type": "application/json",
        "origin": "https://chatgpt.com",
        "referer": "https://chatgpt.com/",
        'sec-ch-ua': '"Chromium";v="120", "Not)A;Brand";v="24", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    })
    return headers

def get_cached_stats(account_id=None):
    try:
        aid = account_id or get_active_account_id()
        if not aid: return None
        key = f"{Config.STATS_CACHE_KEY}_{aid}"
        cached = redis_client.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        log_error("Cache", "读取统计缓存失败", str(e))
    return None

def set_cached_stats(stats_data, account_id=None):
    try:
        aid = account_id or get_active_account_id()
        if not aid: return
        key = f"{Config.STATS_CACHE_KEY}_{aid}"
        cache_obj = {
            "data": stats_data,
            "timestamp": time.time(),
            "updated_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        }
        redis_client.setex(key, Config.STATS_CACHE_TTL, json.dumps(cache_obj))
    except Exception as e:
        log_error("Cache", "写入统计缓存失败", str(e))

def handle_team_banned(active_config):
    """处理母号封禁：标记状态并触发自动切换倒计时"""
    from core.team_config_service import set_team_config_failed
    set_team_config_failed(active_config['id'])
    log_error("AutoSwitch", f"母号 [{active_config['name']}] 已封禁，触发切换程序")
    check_and_auto_switch_team(None, active_config, is_banned=True)

def check_and_auto_switch_team(stats, active_config, is_banned=False):
    """检查当前母号是否满员或封禁，并根据策略自动切换"""
    if not Config.AUTO_SWITCH_ENABLED:
        return
    from core.team_config_service import get_earliest_available_team_config, set_active_config
    from core.activation_code_service import get_pending_boarding_count
    
    aid = active_config.get('account_id')
    if not aid:
        return

    is_full = False
    if stats:
        # 计算总占用：已在空间 + API待处理邀请 + 本地已绑定待邀请
        seats_in_use = stats.get("seats_in_use") or 0
        seats_entitled = stats.get("seats_entitled") or 0
        pending_invites = stats.get("pending_invites") or 0
        
        boarding = get_pending_boarding_count(aid)
        local_waiting = boarding.get('waiting_invite', 0)
        local_pending = boarding.get('pending_accept', 0)
        
        # 修正逻辑：取 API 待处理和本地待处理的最大值，以应对 API 延迟更新的问题
        # 当刚发送邀请但 API 还没更新时，local_pending > pending_invites (0)，此时使用 local_pending
        effective_pending = max(pending_invites, local_pending)
        
        total_occupancy = seats_in_use + effective_pending + local_waiting
        
        # 应用允许超载逻辑
        allow_overload = active_config.get('allow_overload', 0)
        max_overload = active_config.get('max_overload', 0)
        if allow_overload and max_overload > 0 and seats_entitled > 0:
            seats_entitled += max_overload
        
        # 容错处理：如果无法获取总车位，但已用车位>=5，则默认总车位为5
        if seats_entitled <= 0 and seats_in_use >= 5:
            seats_entitled = 5
            
        is_full = seats_entitled > 0 and total_occupancy >= seats_entitled
        
        # Debug Log: 如果已满或接近满员，记录一下判定详情
        if is_full or total_occupancy >= 5:
             log_info("AutoSwitch", f"满员判定: {is_full} ({total_occupancy}/{seats_entitled})", 
                      use=seats_in_use, pending=pending_invites, wait=local_waiting)
    
    # 统一切换逻辑：满员或封禁均触发切换
    should_switch = is_full or is_banned
    switch_countdown_key = f"team_switch_countdown_{active_config['id']}"
    
    if should_switch:
        try:
            start_ts = redis_client.get(switch_countdown_key)
            if not start_ts:
                # 兼容旧版本的满员倒计时 key
                old_key = f"team_full_countdown_{active_config['id']}"
                start_ts = redis_client.get(old_key)
                if start_ts:
                    # 迁移到新 key
                    redis_client.setex(switch_countdown_key, 3600, start_ts)
                    redis_client.delete(old_key)
            
            if not start_ts:
                redis_client.setex(switch_countdown_key, 3600, str(time.time()))
                reason = "封禁" if is_banned else f"满员 ({total_occupancy if stats else '?'}/{seats_entitled if stats else '?'})"
                log_info("AutoSwitch", f"检测到母号{reason}", 
                         name=active_config.get('name'), countdown=60)
                return
            
            elapsed = time.time() - float(start_ts)
            if elapsed >= 60:
                next_config = get_earliest_available_team_config()
                if next_config and next_config['id'] != active_config['id']:
                    reason_msg = "封禁" if is_banned else "满员"
                    log_info("AutoSwitch", f"触发自动切换: {active_config['name']} -> {next_config['name']}", 
                             reason=f"{reason_msg}倒计时结束")
                    set_active_config(next_config['id'])
                    redis_client.delete(switch_countdown_key)
                else:
                    log_warn("AutoSwitch", "母号不可用但未找到可用的备用母号")
        except Exception as e:
            log_error("AutoSwitch", "自动切换逻辑异常", str(e))
    else:
        try:
            redis_client.delete(switch_countdown_key)
            # 同时也尝试清理旧 key
            redis_client.delete(f"team_full_countdown_{active_config['id']}")
        except:
            pass

def fetch_stats_from_api(custom_token=None, custom_account_id=None):
    """获取统计数据（支持自定义母号凭证）
    
    Args:
        custom_token: 自定义 Authorization Token（用于获取非激活母号的数据）
        custom_account_id: 自定义 Account ID
    """
    base_headers = build_base_headers(custom_token, custom_account_id)
    account_id = custom_account_id or get_current_account_id()
    
    subs_url = f"https://chatgpt.com/backend-api/subscriptions?account_id={account_id}"
    invites_url = f"https://chatgpt.com/backend-api/accounts/{account_id}/invites?offset=0&limit=1&query="

    subs_resp = session.get(subs_url, headers=base_headers, timeout=15)
    if subs_resp.status_code in [401, 403]:
        raise TeamBannedException("Team 账号状态异常")
    subs_resp.raise_for_status()
    subs_data = subs_resp.json()

    invites_resp = session.get(invites_url, headers=base_headers, timeout=15)
    if invites_resp.status_code in [401, 403]:
        raise TeamBannedException("Team 账号状态异常")
    invites_resp.raise_for_status()
    invites_data = invites_resp.json()

    return {
        "seats_in_use": subs_data.get("seats_in_use"),
        "seats_entitled": subs_data.get("seats_entitled"),
        "pending_invites": invites_data.get("total"),
        "plan_type": subs_data.get("plan_type"),
        "active_start": subs_data.get("active_start"),
        "active_until": subs_data.get("active_until"),
        "billing_period": subs_data.get("billing_period"),
        "billing_currency": subs_data.get("billing_currency"),
        "is_delinquent": subs_data.get("is_delinquent"),
    }

def refresh_stats(force=False):
    active_config = get_active_team_config()
    if not active_config:
        return None, None
    
    aid = active_config['account_id']

    if not force:
        cached = get_cached_stats(account_id=aid)
        if cached:
            stats = cached["data"]
            # 应用超载逻辑
            if stats and active_config:
                allow_overload = active_config.get('allow_overload', 0)
                max_overload = active_config.get('max_overload', 0)
                if allow_overload and max_overload > 0 and stats.get('seats_entitled', 0) > 0:
                    stats['seats_entitled'] += max_overload
            return stats, cached.get("updated_at")

    try:
        stats = fetch_stats_from_api()
        set_cached_stats(stats, account_id=aid)
        
        # 同步更新 SQLite 中的统计数据
        if active_config:
            update_team_config_stats(active_config['id'], stats)
            # 检查是否需要自动切换母号（满员检测）
            check_and_auto_switch_team(stats, active_config)
    except TeamBannedException:
        # 如果当前车队翻车了，标记其状态为 0 (翻车)
        from core.team_config_service import set_team_config_failed
        set_team_config_failed(active_config['id'])
        log_error("Stats", f"母号 [{active_config['name']}] 已封禁，自动标记为翻车状态")
        
        # 封禁也触发自动切换程序
        check_and_auto_switch_team(None, active_config, is_banned=True)
        raise
    except Exception as e:
        log_error("Stats", "刷新统计失败", str(e))
        return None, None
    
    updated_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    log_info("Stats", "统计数据已刷新", seats_in_use=stats.get("seats_in_use"), pending=stats.get("pending_invites"))
    
    # 应用超载逻辑 (仅影响返回给前端的显示，不影响数据库和缓存)
    if active_config:
        allow_overload = active_config.get('allow_overload', 0)
        max_overload = active_config.get('max_overload', 0)
        if allow_overload and max_overload > 0 and stats.get('seats_entitled', 0) > 0:
            stats['seats_entitled'] += max_overload

    # 🚀 强制刷新时，顺便同步一次用户状态
    if force:
        try:
            sync_individual_statuses(aid)
        except Exception as e:
            log_warn("Stats", f"同步用户状态失败: {str(e)}")
            
    return stats, updated_at

def sync_individual_statuses(account_id, custom_token=None, limit=200):
    """同步个别用户的详细邀请状态 (解决用户接受邀请后状态不刷新的问题)"""
    from core.activation_code_service import bulk_update_invite_status, get_all_users_by_status
    
    # 1. 处理 'pending' -> 'in_space'
    local_pending_users = get_all_users_by_status('pending')
    if local_pending_users:
        # 只同步属于当前母号的用户
        targets = [u for u in local_pending_users if u.get('bound_account_id') == account_id]
        if targets:
            members, _ = fetch_space_members_from_api(limit, custom_token, account_id)
            member_emails = {m.get('email', '').lower() for m in members if m.get('email')}
            
            to_in_space = [u['email'] for u in targets if u['email'].lower() in member_emails]
            if to_in_space:
                bulk_update_invite_status(to_in_space, 'in_space', account_id)
                log_info("Sync", f"自动同步用户状态 -> in_space", count=len(to_in_space), aid=account_id[:8])

    # 2. 处理 'new' -> 'pending' (如果 API 中已有邀请)
    local_new_users = get_all_users_by_status('new')
    if local_new_users:
        targets = [u for u in local_new_users if u.get('bound_account_id') == account_id]
        if targets:
            pending_invites, _ = fetch_pending_invites_from_api(limit, custom_token, account_id)
            pending_emails = {i.get('email_address', '').lower() for i in pending_invites if i.get('email_address')}
            
            to_pending = [u['email'] for u in targets if u['email'].lower() in pending_emails]
            if to_pending:
                bulk_update_invite_status(to_pending, 'pending', account_id)
                log_info("Sync", f"自动同步用户状态 -> pending", count=len(to_pending), aid=account_id[:8])


def refresh_stats_for_account(account_id, force=False):
    """获取指定母号的统计数据（实时获取，带缓存）
    
    当用户绑定的母号与当前激活母号不同时，使用此函数获取实时数据。
    
    Args:
        account_id: 目标母号的 Account ID
        force: 是否强制刷新（忽略缓存）
        
    Returns:
        (stats_data, updated_at) 或 (None, None)
    """
    # 如果就是当前激活母号，直接用常规方法
    if account_id == get_active_account_id():
        return refresh_stats(force=force)
    
    # 获取目标母号的配置
    config = get_team_config_by_account_id(account_id)
    if not config:
        log_error("Stats", f"找不到母号配置", account_id=account_id[:8])
        return None, None
    
    # 尝试从缓存获取（每个母号有独立的缓存 key）
    if not force:
        cached = get_cached_stats(account_id=account_id)
        if cached:
            # 应用超载逻辑
            stats_data = cached["data"]
            if stats_data:
                allow_overload = config.get('allow_overload', 0)
                max_overload = config.get('max_overload', 0)
                if allow_overload and max_overload > 0 and stats_data.get('seats_entitled', 0) > 0:
                    stats_data['seats_entitled'] += max_overload
            return stats_data, cached.get("updated_at")
    
    # 使用该母号的 token 实时获取数据
    try:
        stats = fetch_stats_from_api(
            custom_token=config.get('authorization_token'),
            custom_account_id=account_id
        )
        
        # 更新缓存（每个母号独立缓存）
        set_cached_stats(stats, account_id=account_id)
        
        # 同步更新 SQLite 中该母号的统计数据
        from core.team_config_service import update_team_config_stats
        update_team_config_stats(config['id'], stats)
        
        updated_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        log_info("Stats", f"获取历史母号统计数据", account_id=account_id[:8], seats=stats.get("seats_in_use"))
        
        # 应用超载逻辑 (仅影响返回给前端的显示)
        allow_overload = config.get('allow_overload', 0)
        max_overload = config.get('max_overload', 0)
        if allow_overload and max_overload > 0 and stats.get('seats_entitled', 0) > 0:
            stats['seats_entitled'] += max_overload
            
        return stats, updated_at
        
    except TeamBannedException:
        # 自动标记翻车状态
        from core.team_config_service import set_team_config_failed
        set_team_config_failed(config['id'])
        log_error("Stats", f"指定母号 [{config['name']}] 已封禁，自动标记为翻车状态")
        return None, None
    except Exception as e:
        log_error("Stats", f"获取历史母号统计失败", account_id=account_id[:8], error=str(e))
        # 降级：返回数据库中的快照数据
        snapshot_data = {
            "seats_in_use": config.get("seats_in_use", 0),
            "seats_entitled": config.get("seats_entitled", 0),
            "pending_invites": config.get("pending_invites", 0),
            "active_start": config.get("active_start", ""),
            "active_until": config.get("active_until", ""),
        }
        updated_at = config.get("stats_updated_at")
        if isinstance(updated_at, str):
            pass
        elif updated_at:
            updated_at = updated_at.strftime("%Y-%m-%d %H:%M:%S")
        else:
            updated_at = "从未同步"
        return snapshot_data, updated_at


def fetch_pending_invites_from_api(limit=1000, custom_token=None, custom_account_id=None):
    headers = build_base_headers(custom_token, custom_account_id)
    # proxies = get_proxies()
    account_id = custom_account_id or get_current_account_id()
    all_items = []
    offset = 0
    page_size = 100
    total = 0
    try:
        while True:
            url = f"https://chatgpt.com/backend-api/accounts/{account_id}/invites?offset={offset}&limit={page_size}&query="
            response = session.get(url, headers=headers, timeout=15)
            if response.status_code in [401, 403]:
                raise TeamBannedException("Team 账号状态异常")
            if response.status_code != 200:
                log_error("Invite", "获取待处理邀请失败", status=response.status_code)
                break
            data = response.json()
            items = data.get("items", [])
            total = data.get("total", 0)
            all_items.extend(items)
            if len(items) < page_size or len(all_items) >= total or len(all_items) >= limit:
                break
            offset += page_size
        return all_items[:limit], total
    except TeamBannedException:
        raise
    except Exception as e:
        log_error("Invite", "获取待处理邀请异常", str(e))
        return [], 0

def set_cached_pending_invites(items, total, account_id=None):
    try:
        aid = account_id or get_active_account_id()
        if not aid: return
        key = f"{Config.PENDING_INVITES_CACHE_KEY}_{aid}"
        cache_obj = {"items": items, "total": total, "timestamp": time.time()}
        redis_client.setex(key, Config.STATS_CACHE_TTL, json.dumps(cache_obj))
    except Exception as e:
        log_error("Cache", "写入待处理邀请缓存失败", str(e))

def get_cached_pending_invites(account_id=None):
    try:
        aid = account_id or get_active_account_id()
        if not aid: return None
        key = f"{Config.PENDING_INVITES_CACHE_KEY}_{aid}"
        cached = redis_client.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        log_error("Cache", "读取待处理邀请缓存失败", str(e))
    return None

def get_pending_invites(force=False):
    """获取待处理邀请列表（优先从缓存读取）"""
    aid = get_active_account_id()
    if not aid: return [], 0

    if not force:
        cached = get_cached_pending_invites(account_id=aid)
        if cached:
            return cached["items"], cached["total"]

    items, total = fetch_pending_invites_from_api(100)
    set_cached_pending_invites(items, total, account_id=aid)
    return items, total

def check_invite_pending(email, custom_token=None, custom_account_id=None):
    """检查邮箱是否在待处理邀请列表中（实时查询，支持自定义母号）"""
    try:
        items, _ = fetch_pending_invites_from_api(100, custom_token, custom_account_id)
        for item in items:
            if item.get("email_address", "").lower() == email.lower():
                return True
    except TeamBannedException:
        raise
    except:
        pass
    return False

def fetch_space_members_from_api(limit=1000, custom_token=None, custom_account_id=None):
    headers = build_base_headers(custom_token, custom_account_id)
    # proxies = get_proxies()
    account_id = custom_account_id or get_current_account_id()
    all_items = []
    offset = 0
    page_size = 100
    total = 0
    try:
        while True:
            url = f"https://chatgpt.com/backend-api/accounts/{account_id}/users?offset={offset}&limit={page_size}&query="
            response = session.get(url, headers=headers, timeout=15)
            if response.status_code in [401, 403]:
                raise TeamBannedException("Team 账号状态异常")
            if response.status_code != 200:
                log_error("Members", "获取空间成员失败", status=response.status_code)
                break
            data = response.json()
            items = data.get("items", [])
            total = data.get("total", 0)
            all_items.extend(items)
            if len(items) < page_size or len(all_items) >= total or len(all_items) >= limit:
                break
            offset += page_size
        return all_items[:limit], total
    except TeamBannedException:
        raise
    except Exception as e:
        log_error("Members", "获取空间成员异常", str(e))
        return [], 0

def send_chatgpt_invite(email, custom_token=None, custom_account_id=None):
    account_id = custom_account_id or get_current_account_id()
    url = f"https://chatgpt.com/backend-api/accounts/{account_id}/invites"
    headers = build_invite_headers(custom_token, custom_account_id)
    # proxies = get_proxies()
    payload = {"email_addresses": [email], "role": "standard-user", "resend_emails": True}
    log_info("Invite", "发送邀请", email=email, account_id=account_id[:8])
    try:
        response = session.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code in [401, 403]:
            raise TeamBannedException("Team 账号状态异常")
        if response.status_code == 200:
            return True, "success"
        else:
            log_error("Invite", "邀请失败", response.text[:200], email=email, status=response.status_code)
            return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except TeamBannedException:
        raise
    except Exception as e:
        log_error("Invite", "邀请异常", str(e), email=email)
        return False, str(e)

def cancel_pending_invite(email):
    """取消待处理邀请（通过邮箱地址）"""
    url = f"https://chatgpt.com/backend-api/accounts/{get_current_account_id()}/invites"
    headers = build_invite_headers()
    # proxies = get_proxies()
    payload = {"email_address": email}
    log_info("Invite", "取消邀请", email=email)
    try:
        response = session.delete(url, headers=headers, json=payload, timeout=15)
        if response.status_code == 200:
            log_info("Invite", "取消邀请成功", email=email)
            return True, "邀请已取消"
        elif response.status_code == 404:
            log_warn("Invite", "邀请不存在或已被接受", email=email)
            return False, "邀请不存在或已被接受"
        else:
            error_msg = response.text[:200] if response.text else "未知错误"
            log_error("Invite", "取消邀请失败", error_msg, email=email, status=response.status_code)
            return False, f"HTTP {response.status_code}: {error_msg}"
    except Exception as e:
        log_error("Invite", "取消邀请异常", str(e), email=email)
        return False, str(e)

def remove_space_member(user_id, custom_token=None, custom_account_id=None):
    """踢出空间成员（通过 user_id）"""
    account_id = custom_account_id or get_current_account_id()
    url = f"https://chatgpt.com/backend-api/accounts/{account_id}/users/{user_id}"
    headers = build_invite_headers(custom_token, custom_account_id)
    # proxies = get_proxies()
    log_info("Member", "踢出成员", user_id=user_id)
    try:
        response = session.delete(url, headers=headers, timeout=15)
        if response.status_code == 200:
            log_info("Member", "踢出成员成功", user_id=user_id)
            return True, "成员已移除"
        elif response.status_code == 404:
            log_warn("Member", "成员不存在", user_id=user_id)
            return False, "成员不存在"
        elif response.status_code == 403:
            log_warn("Member", "无法移除该成员（可能是管理员）", user_id=user_id)
            return False, "无权限移除该成员"
        else:
            error_msg = response.text[:200] if response.text else "未知错误"
            log_error("Member", "踢出成员失败", error_msg, user_id=user_id, status=response.status_code)
            return False, f"HTTP {response.status_code}: {error_msg}"
    except Exception as e:
        log_error("Member", "踢出成员异常", str(e), user_id=user_id)
        return False, str(e)

def background_refresh_stats():
    """后台刷新统计数据（由定时任务调用）
    
    除了刷新全局名额统计，还会动态同步当前账户下各用户的邀请状态。
    """
    try:
        from core.team_config_service import get_active_team_config, update_team_config_stats
        
        active_config = get_active_team_config()
        if not active_config:
            return
            
        aid = active_config['account_id']
        
        # 1. 刷新全局统计
        stats = fetch_stats_from_api()
        set_cached_stats(stats, account_id=aid)
        update_team_config_stats(active_config['id'], stats)
        
        # 2. 同步当前活跃母号的用户状态
        sync_individual_statuses(aid)
            
        log_info("Background", "统计数据刷新完成", seats=stats.get("seats_in_use"), pending=stats.get("pending_invites"))
    except TeamBannedException:
        log_error("Background", "统计刷新失败", "账号被封禁")
        # 后台刷新检测到封禁也触发自动切换
        check_and_auto_switch_team(None, active_config, is_banned=True)
    except Exception as e:
        log_error("Background", "统计刷新失败", str(e))
