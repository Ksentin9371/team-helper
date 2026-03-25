import logging
import json
import os

# 自定义日志格式
log_formatter = logging.Formatter(
    '[%(asctime)s] %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# 清除 werkzeug 的 404 日志
class No404Filter(logging.Filter):
    def filter(self, record):
        return not (getattr(record, "status_code", None) == 404)

def setup_logger(name="team_invite", level=logging.INFO):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    if not logger.handlers:
        # 控制台输出
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(log_formatter)
        logger.addHandler(stream_handler)
        
        # 文件输出
        log_dir = "data"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        file_handler = logging.FileHandler(os.path.join(log_dir, "logs.log"), encoding="utf-8")
        file_handler.setFormatter(log_formatter)
        logger.addHandler(file_handler)
    
    # 也应用到 werkzeug
    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.setLevel(logging.ERROR)
    werkzeug_logger.addFilter(No404Filter())
    
    return logger

logger = setup_logger()

def log_info(module: str, action: str, message: str = "", **kwargs):
    """统一 INFO 日志格式"""
    extra = " | ".join(f"{k}={v}" for k, v in kwargs.items()) if kwargs else ""
    log_msg = f"[{module}] {action}"
    if message:
        log_msg += f" - {message}"
    if extra:
        log_msg += f" | {extra}"
    logger.info(log_msg)

def log_error(module: str, action: str, message: str = "", **kwargs):
    """统一 ERROR 日志格式"""
    extra = " | ".join(f"{k}={v}" for k, v in kwargs.items()) if kwargs else ""
    log_msg = f"[{module}] {action}"
    if message:
        log_msg += f" - {message}"
    if extra:
        log_msg += f" | {extra}"
    logger.error(log_msg)

def log_warn(module: str, action: str, message: str = "", **kwargs):
    """统一 WARNING 日志格式"""
    extra = " | ".join(f"{k}={v}" for k, v in kwargs.items()) if kwargs else ""
    log_msg = f"[{module}] {action}"
    if message:
        log_msg += f" - {message}"
    if extra:
        log_msg += f" | {extra}"
    logger.warning(log_msg)
