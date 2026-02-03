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
from typing import Optional, Tuple

import httpx

# 配置
PROVIDERS = {
    "anyrouter": {
        "domain": "https://anyrouter.top",
        "sign_in_path": "/api/user/sign_in",
        "user_info_path": "/api/user/self",
    },
    "agentrouter": {
        "domain": "https://agentrouter.org",
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


async def playwright_session(domain: str, cookies: dict, api_user: str = "", username: str = "", password: str = "") -> Tuple[Optional[dict], Optional[dict], Optional[str]]:
    """
    使用 Playwright 在浏览器中执行所有操作（登录、签到、获取余额）
    返回: (sign_result, user_info, new_session)
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log("Playwright 未安装", "ERROR")
        return None, None, None

    sign_result = None
    user_info = None
    new_session = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=HEADERS["User-Agent"])

            # 添加用户的 session cookie
            if cookies.get("session"):
                await context.add_cookies([{
                    "name": "session",
                    "value": cookies["session"],
                    "domain": domain.replace("https://", "").replace("http://", ""),
                    "path": "/"
                }])

            page = await context.new_page()

            # 用于捕获 API 响应
            captured_data = {"user_info": None, "sign_in": None}

            async def handle_response(response):
                try:
                    url = response.url
                    if response.status == 200:
                        if "/api/user/self" in url:
                            data = await response.json()
                            if data.get("success") and data.get("data"):
                                captured_data["user_info"] = data["data"]
                            elif data.get("data"):
                                captured_data["user_info"] = data["data"]
                        elif "/api/user/sign_in" in url:
                            data = await response.json()
                            captured_data["sign_in"] = data
                except Exception:
                    pass

            page.on("response", handle_response)

            # 访问首页
            log(f"正在通过浏览器访问 {domain}...")
            await page.goto(domain, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(5000)

            # 导航到 console
            log("导航到 console 页面...")
            await page.goto(f"{domain}/console", wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(5000)

            # 检查 session 是否有效
            current_url = page.url
            session_valid = "login" not in current_url and captured_data.get("user_info") is not None

            if not session_valid and username and password:
                log("Session 无效，尝试浏览器内登录...", "WARN")
                login_result = await page.evaluate(f"""
                    async () => {{
                        try {{
                            const resp = await fetch('{domain}/api/user/login', {{
                                method: 'POST',
                                headers: {{'Accept': 'application/json', 'Content-Type': 'application/json'}},
                                body: JSON.stringify({{username: '{username}', password: '{password}'}}),
                                credentials: 'include'
                            }});
                            return await resp.json();
                        }} catch (e) {{
                            return {{success: false, message: e.toString()}};
                        }}
                    }}
                """)

                if login_result and login_result.get("success"):
                    log("浏览器内登录成功")
                    browser_cookies = await context.cookies()
                    for c in browser_cookies:
                        if c["name"] == "session":
                            new_session = c["value"]
                            break
                    # 重新加载 console 页面获取用户信息
                    await page.reload(wait_until="networkidle", timeout=60000)
                    await page.wait_for_timeout(3000)
                else:
                    msg = login_result.get('message', '未知错误') if login_result else '无响应'
                    log(f"浏览器内登录失败: {msg}", "ERROR")

            # 执行签到 - 通过页面内 JavaScript
            log("执行签到...")
            api_user_header = f'headers["New-Api-User"] = "{api_user}";' if api_user else ''
            sign_result = await page.evaluate(f"""
                async () => {{
                    try {{
                        const headers = {{'Accept': 'application/json', 'Content-Type': 'application/json'}};
                        {api_user_header}
                        const resp = await fetch('{domain}/api/user/sign_in', {{
                            method: 'POST',
                            headers: headers,
                            body: '{{}}',
                            credentials: 'include'
                        }});
                        const text = await resp.text();
                        if (text.startsWith('<')) return {{success: false, message: '被 WAF 拦截'}};
                        return JSON.parse(text);
                    }} catch (e) {{
                        return {{success: false, message: e.toString()}};
                    }}
                }}
            """)

            # 等待并获取余额
            await page.wait_for_timeout(2000)
            log("获取用户余额...")

            # 刷新页面让 SPA 重新获取用户信息
            await page.reload(wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(3000)

            # 使用捕获的数据
            if captured_data.get("user_info"):
                user_info = captured_data["user_info"]

            # 如果还没有，手动 fetch 一次
            if not user_info:
                fetched = await page.evaluate(f"""
                    async () => {{
                        try {{
                            const headers = {{'Accept': 'application/json', 'Content-Type': 'application/json'}};
                            {api_user_header}
                            const resp = await fetch('{domain}/api/user/self', {{
                                headers: headers,
                                credentials: 'include'
                            }});
                            const text = await resp.text();
                            if (text.startsWith('<')) return null;
                            const data = JSON.parse(text);
                            if (data.success && data.data) return data.data;
                            if (data.data) return data.data;
                            return data;
                        }} catch (e) {{
                            return null;
                        }}
                    }}
                """)
                if fetched and fetched.get("quota") is not None:
                    user_info = fetched

            await browser.close()

    except Exception as e:
        log(f"Playwright 操作失败: {e}", "ERROR")

    return sign_result, user_info, new_session


async def process_account(account: dict) -> dict:
    """处理单个账号"""
    name = account.get("name", "未命名账号")
    cookies = account.get("cookies", {})
    api_user = account.get("api_user", "")
    username = account.get("username", "")
    password = account.get("password", "")
    provider_name = account.get("provider", "anyrouter")

    provider = PROVIDERS.get(provider_name, PROVIDERS["anyrouter"])
    domain = account.get("domain", provider["domain"])

    result = {
        "name": name,
        "provider": provider_name,
        "success": False,
        "message": "",
        "quota": None,
        "used": None,
        "balance": None,
    }

    log(f"正在处理账号: {name} ({provider_name})")

    # 使用 Playwright 执行操作
    sign_result, user_info, new_session = await playwright_session(
        domain, cookies, api_user, username, password
    )

    # 如果 Playwright 签到失败（被 WAF 拦截），尝试用 httpx 配合 Playwright 获取的 cookies
    if sign_result and sign_result.get("message") == "被 WAF 拦截":
        log("尝试备用方案: 使用 httpx 签到...", "WARN")
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(user_agent=HEADERS["User-Agent"])

                # 添加用户 session
                if cookies.get("session"):
                    await context.add_cookies([{
                        "name": "session",
                        "value": cookies["session"],
                        "domain": domain.replace("https://", "").replace("http://", ""),
                        "path": "/"
                    }])

                page = await context.new_page()
                await page.goto(domain, wait_until="networkidle", timeout=60000)
                await page.wait_for_timeout(5000)

                # 获取所有 cookies
                all_cookies = await context.cookies()
                cookie_dict = {c["name"]: c["value"] for c in all_cookies}
                await browser.close()

            # 用 httpx 发请求
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])
            headers = {**HEADERS, "Cookie": cookie_str}
            if api_user:
                headers["New-Api-User"] = str(api_user)

            async with httpx.AsyncClient(timeout=30.0) as client:
                # 签到
                resp = await client.post(f"{domain}/api/user/sign_in", headers=headers, json={})
                text = resp.text
                if not text.startswith("<"):
                    sign_result = json.loads(text)
                    log(f"备用方案签到结果: {sign_result}")

                # 获取余额
                await asyncio.sleep(1)
                resp = await client.get(f"{domain}/api/user/self", headers=headers)
                text = resp.text
                if not text.startswith("<"):
                    data = json.loads(text)
                    if data.get("success") and data.get("data"):
                        user_info = data["data"]
                    elif data.get("data"):
                        user_info = data["data"]
        except Exception as e:
            log(f"备用方案失败: {e}", "ERROR")

    # 处理签到结果
    if sign_result:
        if sign_result.get("success") is True:
            result["success"] = True
            result["message"] = sign_result.get("message", "签到成功")
        elif "已经签到" in str(sign_result) or "already" in str(sign_result).lower():
            result["success"] = True
            result["message"] = "今日已签到"
        else:
            result["message"] = sign_result.get("message", str(sign_result))
    else:
        result["message"] = "签到请求失败"

    if result["success"]:
        log(f"签到结果: {result['message']}")
    else:
        log(f"签到失败: {result['message']}", "ERROR")

    # 处理余额信息
    if user_info:
        quota = user_info.get("quota", 0)
        used = user_info.get("used_quota", 0)

        result["balance"] = quota / 500000
        result["used"] = used / 500000
        result["quota"] = (quota + used) / 500000

        log(f"当前余额: ${result['balance']:.2f}, 历史消耗: ${result['used']:.2f}, 总获得: ${result['quota']:.2f}")
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

    results = []
    for account in accounts:
        result = await process_account(account)
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
            line += f"\n   - 📊 历史消耗: ${r['used']:.2f}"
        else:
            line += f"\n   - 💰 余额: 获取失败"

        log_line = f"{'✓' if r['success'] else '✗'} {r['name']}: {r['message']}"
        if r["balance"] is not None:
            log_line += f" | 余额: ${r['balance']:.2f}, 消耗: ${r['used']:.2f}"
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
