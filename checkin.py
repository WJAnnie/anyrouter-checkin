#!/usr/bin/env python3
"""
AnyRouter 自动签到脚本
支持多账号、多平台签到，兼容 NewAPI/OneAPI 平台
"""

import asyncio
import hashlib
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

# 通用请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}


def log(message: str, level: str = "INFO"):
    """打印日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")


def get_accounts() -> list:
    """从环境变量获取账号配置"""
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


async def get_user_info(client: httpx.AsyncClient, domain: str, user_info_path: str) -> Optional[dict]:
    """获取用户信息"""
    try:
        url = f"{domain}{user_info_path}"
        response = await client.get(url, headers=HEADERS)

        if response.status_code == 401:
            log("认证失败，session 可能已过期", "ERROR")
            return None

        response.raise_for_status()
        data = response.json()

        if data.get("success") or data.get("data"):
            return data.get("data", data)
        return None
    except Exception as e:
        log(f"获取用户信息失败: {e}", "ERROR")
        return None


async def do_sign_in(client: httpx.AsyncClient, domain: str, sign_in_path: str) -> dict:
    """执行签到"""
    result = {"success": False, "message": ""}

    try:
        url = f"{domain}{sign_in_path}"
        response = await client.post(url, headers=HEADERS, json={})

        if response.status_code == 401:
            result["message"] = "认证失败，请更新 session cookie"
            return result

        # 尝试解析 JSON 响应
        try:
            data = response.json()

            # 检查各种成功标志
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
            # JSON 解析失败，检查文本响应
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


async def process_account(account: dict) -> dict:
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
        "balance_before": None,
        "balance_after": None,
    }

    log(f"正在处理账号: {name} ({provider_name})")

    # 构建 cookies
    cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 设置 cookies
        client.headers["Cookie"] = cookie_str
        if api_user:
            client.headers["New-Api-User"] = str(api_user)

        # 获取签到前余额
        user_info = await get_user_info(client, domain, user_info_path)
        if user_info:
            quota = user_info.get("quota", 0)
            used = user_info.get("used_quota", 0)
            result["balance_before"] = (quota - used) / 500000  # 转换为美元
            log(f"签到前余额: ${result['balance_before']:.2f}")

        # 执行签到
        sign_result = await do_sign_in(client, domain, sign_in_path)
        result["success"] = sign_result["success"]
        result["message"] = sign_result["message"]

        if result["success"]:
            log(f"签到结果: {result['message']}", "INFO")

            # 等待一下再获取余额
            await asyncio.sleep(1)

            # 获取签到后余额
            user_info = await get_user_info(client, domain, user_info_path)
            if user_info:
                quota = user_info.get("quota", 0)
                used = user_info.get("used_quota", 0)
                result["balance_after"] = (quota - used) / 500000
                log(f"签到后余额: ${result['balance_after']:.2f}")

                if result["balance_before"] is not None:
                    diff = result["balance_after"] - result["balance_before"]
                    if diff > 0:
                        log(f"获得奖励: +${diff:.2f}")
        else:
            log(f"签到失败: {result['message']}", "ERROR")

    return result


async def main():
    """主函数"""
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

    # 统计结果
    success_count = sum(1 for r in results if r["success"])
    fail_count = len(results) - success_count

    log("=" * 50)
    log("签到完成统计")
    log(f"成功: {success_count}, 失败: {fail_count}")
    log("=" * 50)

    # 打印详细结果
    for r in results:
        status = "✓" if r["success"] else "✗"
        balance_info = ""
        if r["balance_after"] is not None:
            balance_info = f" | 余额: ${r['balance_after']:.2f}"
        log(f"{status} {r['name']}: {r['message']}{balance_info}")

    # 如果有失败的，返回非零退出码
    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
