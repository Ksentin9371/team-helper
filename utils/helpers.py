import requests
from flask import request
from config import Config
from utils.logger import log_error

def get_client_ip_address():
    if "CF-Connecting-IP" in request.headers:
        return request.headers["CF-Connecting-IP"]
    if "X-Forwarded-For" in request.headers:
        return request.headers["X-Forwarded-For"].split(",")[0].strip()
    return request.remote_addr or "unknown"

def validate_turnstile(turnstile_response):
    if not turnstile_response:
        return False
    data = {
        "secret": Config.CF_TURNSTILE_SECRET_KEY,
        "response": turnstile_response,
        "remoteip": get_client_ip_address(),
    }
    try:
        response = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=data,
            timeout=10,
        )
        result = response.json()
        return result.get("success", False)
    except Exception:
        return False

def parse_emails(raw_emails):
    if not raw_emails:
        return [], []
    parts = raw_emails.replace("\n", ",").split(",")
    emails = [p.strip() for p in parts if p.strip()]
    valid = [e for e in emails if e.count("@") == 1]
    return emails, valid
