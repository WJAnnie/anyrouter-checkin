#!/usr/bin/env python3
"""
AnyRouter 自动签到脚本
支持多账号、多平台签到，兼容 NewAPI/OneAPI 平台
使用 Playwright 绕过 WAF 获取余额
支持 Server酱 推送通知
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Optional

import httpx

# 配置
PROVIDERS = {
    "anyrouter": {
        "domain": "https://anyrouter.top",
        "sign_in_path": "/api/user/sign_in",
        "user_info_path": "/api/user/self",
    },
    "agentrouter": {
        "domain": "https://agentrouter.top",
        "sign_in_path": "/api/user/sign_in",
        "user_info_path": "/api/user/self",
    },
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}


def log(message: str, level: str = "INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")


def get_accounts() -> list:
    accounts_json = os.environ.get("ANYROUTER_ACCOUNTS", "")
    if not accounts_json:
        log("未找到 ANYROUTER_ACCOUNTS 环境变量", "ERROR")
        return []
    try:
        accounts = json.loads(accounts_json)
        if not isinstance(accounts, list):
            accounts = [accounts]
        return accounts
    except json.JSONDecodeError as e:
        log(f"解析账号配置失败: {e}", "ERROR")
        return []


async def get_waf_cookies(domain: str) -> dict:
    """使用 Playwright 获取 WAF cookies"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log("Playwright 未安装，跳过 WAF cookie 获取", "WARN")
        return {}

    waf_cookies = {}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=HEADERS["User-Agent"],
            )
            page = await context.new_page()

            log(f"正在通过浏览器访问 {domain} 获取 WAF cookies...")
            await page.goto(domain, wait_until="networkidle", timeout=30000)
            # 等待 WAF JS 执行完毕
            await page.wait_for_timeout(3000)

            cookies = await context.cookies()
            for cookie in cookies:
                waf_cookies[cookie["name"]] = cookie["value"]

            await browser.close()

            if waf_cookies:
                log(f"获取到 WAF cookies: {list(waf_cookies.keys())}")
            else:
                log("未获取到 WAF cookies", "WARN")
    except Exception as e:
        log(f"Playwright 获取 WAF cookies 失败: {e}", "ERROR")

    return waf_cookies


async def get_user_info(client: httpx.AsyncClient, domain: str, user_info_path: str) -> Optional[dict]:
    """获取用户信息"""
    try:
        url = f"{domain}{user_info_path}"
        response = await client.get(url, headers=HEADERS)

        if response.status_code == 401:
            log("认证失败，session 可能已过期", "ERROR")
            return None

        if response.status_code != 200:
            log(f"获取用户信息: HTTP {response.status_code}", "WARN")
            return None

        text = response.text
        if not text or text.startswith("<"):
            log("获取用户信息: 被 WAF 拦截", "WARN")
            return None

        data = response.json()
        if data.get("success") and data.get("data"):
            return data["data"]
        elif data.get("data"):
            return data["data"]
        return data
    except json.JSONDecodeError:
        log("获取用户信息: 响应不是有效的 JSON", "WARN")
        return None
    except Exception as e:
        log(f"获取用户信息失败: {e}", "ERROR")
        return None


def format_balance(quota: int, used_quota: int) -> float:
    """转换余额为美元"""
    return (quota - used_quota) / 500000


async def do_sign_in(client: httpx.AsyncClient, domain: str, sign_in_path: str) -> dict:
    """执行签到"""
    result = {"success": False, "message": ""}
    try:
        url = f"{domain}{sign_in_path}"
        response = await client.post(url, headers=HEADERS, json={})

        if response.status_code == 401:
            result["message"] = "认证失败，请更新 session cookie"
            return result

        try:
            data = response.json()
            if data.get("success") is True:
                result["success"] = True
                result["message"] = data.get("message", "签到成功")
            elif data.get("ret") == 1:
                result["success"] = True
                result["message"] = data.get("msg", "签到成功")
            elif data.get("code") == 0:
                result["success"] = True
                result["message"] = data.get("message", "签到成功")
            elif "已经签到" in str(data) or "already" in str(data).lower():
                result["success"] = True
                result["message"] = "今日已签到"
            else:
                result["message"] = data.get("message", data.get("msg", str(data)))
        except json.JSONDecodeError:
            text = response.text
            if "success" in text.lower() or response.status_code == 200:
                result["success"] = True
                result["message"] = "签到成功"
            else:
                result["message"] = f"响应解析失败: {text[:100]}"
    except httpx.HTTPStatusError as e:
        result["message"] = f"HTTP 错误: {e.response.status_code}"
    except Exception as e:
        result["message"] = f"签到异常: {str(e)}"
    return result


async def process_account(account: dict, waf_cookies_cache: dict) -> dict:
    """处理单个账号"""
    name = account.get("name", "未命名账号")
    cookies = account.get("cookies", {})
    api_user = account.get("api_user", "")
    provider_name = account.get("provider", "anyrouter")

    provider = PROVIDERS.get(provider_name, PROVIDERS["anyrouter"])
    domain = account.get("domain", provider["domain"])
    sign_in_path = account.get("sign_in_path", provider["sign_in_path"])
    user_info_path = account.get("user_info_path", provider["user_info_path"])

    result = {
        "name": name,
        "provider": provider_name,
        "success": False,
        "message": "",
        "balance": None,
    }

    log(f"正在处理账号: {name} ({provider_name})")

    # 获取该域名的 WAF cookies（有缓存则复用）
    if domain not in waf_cookies_cache:
        waf_cookies_cache[domain] = await get_waf_cookies(domain)
    waf_cookies = waf_cookies_cache[domain]

    # 合并 WAF cookies 和用户 session cookies
    all_cookies = {**waf_cookies, **cookies}
    cookie_str = "; ".join([f"{k}={v}" for k, v in all_cookies.items()])

    async with httpx.AsyncClient(timeout=30.0) as client:
        client.headers["Cookie"] = cookie_str
        if api_user:
            client.headers["New-Api-User"] = str(api_user)

        # 执行签到
        sign_result = await do_sign_in(client, domain, sign_in_path)
        result["success"] = sign_result["success"]
        result["message"] = sign_result["message"]

        if result["success"]:
            log(f"签到结果: {result['message']}")
        else:
            log(f"签到失败: {result['message']}", "ERROR")

        # 获取余额（签到后）
        await asyncio.sleep(1)
        user_info = await get_user_info(client, domain, user_info_path)
        if user_info:
            quota = user_info.get("quota", 0)
            used = user_info.get("used_quota", 0)
            result["balance"] = format_balance(quota, used)
            log(f"当前余额: ${result['balance']:.2f}")
        else:
            log("未能获取余额信息", "WARN")

    return result


async def send_serverchan(title: str, content: str):
    """通过 Server酱 推送通知"""
    key = os.environ.get("SERVERCHAN_KEY", "")
    if not key:
        return

    url = f"https://sctapi.ftqq.com/{key}.send"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, data={"title": title, "desp": content})
            data = response.json()
            if data.get("code") == 0:
                log("Server酱 推送成功")
            else:
                log(f"Server酱 推送失败: {data.get('message', '')}", "ERROR")
    except Exception as e:
        log(f"Server酱 推送异常: {e}", "ERROR")


async def main():
    log("=" * 50)
    log("AnyRouter 自动签到开始")
    log("=" * 50)

    accounts = get_accounts()
    if not accounts:
        log("没有找到有效的账号配置", "ERROR")
        sys.exit(1)

    log(f"共找到 {len(accounts)} 个账号")

    waf_cookies_cache = {}
    results = []
    for account in accounts:
        result = await process_account(account, waf_cookies_cache)
        results.append(result)
        log("-" * 30)

    # 统计
    success_count = sum(1 for r in results if r["success"])
    fail_count = len(results) - success_count

    log("=" * 50)
    log("签到完成统计")
    log(f"成功: {success_count}, 失败: {fail_count}")
    log("=" * 50)

    # 构建推送内容
    notify_lines = []
    for r in results:
        status = "✅" if r["success"] else "❌"
        line = f"{status} **{r['name']}**: {r['message']}"
        if r["balance"] is not None:
            line += f"\n   - 💰 当前余额: **${r['balance']:.2f}**"
        else:
            line += f"\n   - 💰 余额: 获取失败"

        log_line = f"{'✓' if r['success'] else '✗'} {r['name']}: {r['message']}"
        if r["balance"] is not None:
            log_line += f" | 余额: ${r['balance']:.2f}"
        log(log_line)
        notify_lines.append(line)

    # Server酱 推送
    title = f"AnyRouter 签到 - 成功{success_count} 失败{fail_count}"
    content = f"## 📋 签到结果\n\n"
    content += f"- ⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    content += f"- ✅ 成功: {success_count}\n"
    content += f"- ❌ 失败: {fail_count}\n\n"
    content += "---\n\n"
    content += "## 📊 账号详情\n\n"
    for line in notify_lines:
        content += f"{line}\n\n"
    await send_serverchan(title, content)

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
