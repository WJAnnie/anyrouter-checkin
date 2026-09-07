#!/usr/bin/env python3
"""Robust GitHub Actions entrypoint.

Keep the fast HTTP flow first. If AnyRouter rejects direct API password login
(e.g. login flow / Turnstile / WAF behavior changes), retry through the real
browser login flow using the same username/password credentials.
"""

import asyncio
import os

import checkin


_original_process_account = checkin.process_account


def _build_result(account: dict, sign_result, user_info) -> dict:
    name = account.get("name", "未命名账号")
    provider_name = account.get("provider", "anyrouter")
    result = {
        "name": name,
        "provider": provider_name,
        "success": False,
        "soft_failed": False,
        "balance_skipped": False,
        "message": "",
        "quota": None,
        "used": None,
        "balance": None,
    }

    if sign_result:
        if sign_result.get("success") is True:
            result["success"] = True
            result["message"] = sign_result.get("message", "签到成功")
        elif "已经签到" in str(sign_result) or "already" in str(sign_result).lower():
            result["success"] = True
            result["message"] = "今日已签到"
        elif "Invalid URL" in str(sign_result) or "invalid_request_error" in str(sign_result):
            result["success"] = True
            result["message"] = "该平台不支持签到功能"
        else:
            result["message"] = sign_result.get("message", str(sign_result))
    else:
        result["message"] = "浏览器登录/签到请求失败"

    if user_info:
        quota = user_info.get("quota", 0)
        used = user_info.get("used_quota", 0)
        result["balance"] = quota / 500000
        result["used"] = used / 500000
        result["quota"] = (quota + used) / 500000

    return result


async def process_account_with_browser_fallback(account: dict) -> dict:
    result = await _original_process_account(account)

    provider_name = account.get("provider", "anyrouter")
    username = account.get("username", "")
    password = account.get("password", "")

    if (
        provider_name != "anyrouter"
        or result.get("success")
        or not username
        or not password
    ):
        return result

    message = str(result.get("message") or "")
    if "认证失败" not in message and "登录" not in message and result.get("balance") is not None:
        return result

    checkin.log("HTTP API 账号密码登录失败，切换到 Playwright 真实网页登录兜底", "WARN")

    provider = checkin.PROVIDERS.get(provider_name, checkin.PROVIDERS["anyrouter"])
    cookies = account.get("cookies", {}) or {}
    api_user = account.get("api_user", "")
    github_session = account.get("github_session", "") or os.environ.get("GITHUB_SESSION", "")
    domain = account.get("domain", provider["domain"])
    supports_sign_in = account.get("supports_sign_in", provider.get("supports_sign_in", True))
    name = account.get("name", "未命名账号")

    sign_result, user_info, _new_session = await checkin.playwright_session(
        domain,
        cookies,
        api_user,
        username,
        password,
        github_session,
        supports_sign_in,
        account_name=name,
        provider_key=provider_name,
    )

    fallback = _build_result(account, sign_result, user_info)
    if fallback.get("success"):
        checkin.log("Playwright 网页登录兜底成功")
    else:
        checkin.log(f"Playwright 网页登录兜底仍失败: {fallback.get('message')}", "ERROR")
    return fallback


checkin.process_account = process_account_with_browser_fallback


if __name__ == "__main__":
    asyncio.run(checkin.main())
