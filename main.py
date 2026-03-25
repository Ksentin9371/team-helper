import atexit
from flask import Flask
from flask_session import Session
from werkzeug.middleware.proxy_fix import ProxyFix
from apscheduler.schedulers.background import BackgroundScheduler

from config import Config, is_using_bootstrap_admin_password, is_using_bootstrap_secret_key
from utils.logger import setup_logger, log_info
from utils.redis_client import session_redis, has_active_users
from routes.auth import auth_bp
from routes.user import user_bp
from routes.admin import admin_bp
from routes.payment import payment_bp
from core.openai_service import background_refresh_stats
from core.team_config_service import migrate_from_env, get_active_team_config, get_active_account_id
from core.order_cleanup_service import cleanup_expired_orders

# 全局调度器
scheduler = None

def smart_refresh_job():
    """智能刷新任务：只有在有活跃用户时才刷新"""
    if has_active_users():
        background_refresh_stats()



def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    
    # 初始化日志
    setup_logger()
    
    # 启用代理修复中间件，支持 X-Forwarded-Proto, X-Forwarded-Host 等
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1
    )
    
    # 尝试从环境变量迁移配置到数据库（首次运行时）
    if migrate_from_env():
        log_info("Startup", "已从 .env 迁移母号配置到数据库")
    
    # Session 配置
    app.config['SESSION_REDIS'] = session_redis
    Session(app)
    
    # 注册蓝图
    app.register_blueprint(auth_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(payment_bp)
    
    # 添加模板上下文处理器，动态提供配置变量
    @app.context_processor
    def inject_config():
        """注入全局配置到所有模板"""
        active_account_id = get_active_account_id()
        return {
            'config': {
                'GROUP_URL': Config.GROUP_URL,
                'CUSTOMER_SERVICE_URL': Config.CUSTOMER_SERVICE_URL,
                'ACTIVE_TEAM_PREFIX': (active_account_id[:6] + "...") if active_account_id else "未配置"
            }
        }
    
    return app

def init_scheduler():
    """初始化后台定时任务"""
    global scheduler
    scheduler = BackgroundScheduler(daemon=True)
    
    # 智能刷新：每3分钟检查一次，有用户在线才刷新
    scheduler.add_job(
        func=smart_refresh_job,
        trigger='interval',
        seconds=180,
        id='smart_refresh_stats',
        replace_existing=True,
        max_instances=1
    )
    
    # 订单超时清理：可配置间隔清理超时订单
    scheduler.add_job(
        func=cleanup_expired_orders,
        trigger='interval',
        seconds=Config.ORDER_CLEANUP_INTERVAL,
        id='cleanup_expired_orders',
        replace_existing=True,
        max_instances=1
    )
    
    scheduler.start()
    log_info("Scheduler", "后台智能刷新任务已启动", interval="180s (仅有用户在线时)")
    log_info("Scheduler", "订单超时清理任务已启动", interval=f"{Config.ORDER_CLEANUP_INTERVAL}s")
    
    # 程序退出时关闭调度器
    atexit.register(lambda: scheduler.shutdown(wait=False))

app = create_app()
init_scheduler()

if __name__ == "__main__":
    import os
    import socket
    
    port = int(os.getenv("PORT", 39001))
    
    def get_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    local_ip = get_ip()
    
    print("\n" + "🚀 " + "="*46 + " 🚀")
    print("ChatGPT Team 邀请助手启动成功！")
    print("="*50)
    if is_using_bootstrap_secret_key():
        print("⚠️  未配置 SECRET_KEY，当前正在使用进程级临时密钥。重启后登录态会失效，请尽快在 .env 中设置。")
    if Config.ADMIN_PASSWORD == "admin123":
        print("⚠️  检测到后台仍在使用默认密码 admin123，请在公开前立即修改。")
    elif is_using_bootstrap_admin_password():
        print(f"🔐 首次启动临时后台密码: {Config.ADMIN_PASSWORD}")
        print("   请登录后台后立即修改，或在 .env 中显式设置 ADMIN_PASSWORD。")
    print(f"🏠 用户主页:    http://localhost:{port}")
    print(f"⚙️ 管理后台:    http://localhost:{port}/admin/")
    print("-" * 50)

    # 检查连通性
    from core.openai_service import verify_connectivity
    
    # 打印当前代理状态
    if Config.SOCKS5_PROXY:
        # 显示时隐藏部分敏感信息，或者直接提示已开启 SOCKS5
        proxy_display = Config.SOCKS5_PROXY.split('@')[-1] if '@' in Config.SOCKS5_PROXY else Config.SOCKS5_PROXY
        print(f"🌐 代理状态: 已开启 SOCKS5 代理 ({proxy_display})")
    elif Config.HTTP_PROXY or Config.HTTPS_PROXY:
        print("🌐 代理状态: 已开启 HTTP 代理")
    else:
        print("🌐 代理状态: 直连模式 (未配置代理)")

    is_ok, conn_msg = verify_connectivity()
    if is_ok:
        print(f"✅ 网络检查: {conn_msg}")
    else:
        print(f"❌ 网络检查: {conn_msg}")
        if not Config.SOCKS5_PROXY and not Config.HTTP_PROXY and not Config.HTTPS_PROXY:
            print("   ⚠️  请确认您的网络环境是否可以直接访问 ChatGPT")

    # 检查母号配置状态
    from core.team_config_service import get_active_team_config
    active_config = get_active_team_config()
    if not active_config:
        print("\n⚠️  警告: 尚未配置母号，请前往管理后台添加！")
    
    print("="*50 + "\n")

    app.run(host="0.0.0.0", port=port, debug=False)
