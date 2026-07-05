#!/usr/bin/env python3
"""
AnyRouter 自动签到脚本
支持多账号、多平台签到，兼容 NewAPI/OneAPI 平台
使用 Playwright + Stealth 绕过 WAF 获取余额
支持 GitHub OAuth 登录
支持 Server酱 / 飞书 推送通知
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from typing import Optional, Tuple

# 持久化浏览器 profile 目录(用于 GitHub OAuth 类登录,避免每次依赖 .env cookies)
PROFILE_BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".browser_profile")


def _profile_dir_for(account_name: str, provider: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", account_name or "default")
    return os.path.join(PROFILE_BASE_DIR, f"{provider}_{safe}")


def _profile_has_session(profile_dir: str) -> bool:
    """简单判断 profile 是否已经登录过(看 Cookies 文件存在且非空)"""
    for rel in ("Default/Network/Cookies", "Default/Cookies"):
        path = os.path.join(profile_dir, rel)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return True
    return False

import httpx

# 本地测试时加载 .env 文件
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 配置
PROVIDERS = {
    "anyrouter": {
        "domain": "https://anyrouter.top",
        "login_path": "/api/user/login",
        "sign_in_path": "/api/user/sign_in",
        "user_info_path": "/api/user/self",
        "supports_sign_in": True,
    },
    "agentrouter": {
        "domain": "https://agentrouter.org",
        "login_path": "/api/user/login",
        "sign_in_path": "/api/user/sign_in",
        "user_info_path": "/api/user/self",
        # AgentRouter's daily check-in is triggered by a fresh password login,
        # so the Playwright flow logs out first and then fetches the balance.
        "supports_sign_in": False,
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
    try:
        print(f"[{timestamp}] [{level}] {message}")
    except UnicodeEncodeError:
        # Windows GBK 编码问题，使用 ASCII 替代
        safe_message = message.encode('ascii', errors='replace').decode('ascii')
        print(f"[{timestamp}] [{level}] {safe_message}")


def _as_bool(value, default: bool = False) -> bool:
    """兼容 JSON boolean 和环境变量风格的字符串 boolean。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "y", "on"):
            return True
        if lowered in ("0", "false", "no", "n", "off"):
            return False
    return default


def _is_github_actions() -> bool:
    return _as_bool(os.environ.get("GITHUB_ACTIONS"), False)


def _notifications_disabled() -> bool:
    return _as_bool(os.environ.get("DISABLE_NOTIFY"), False) or _as_bool(os.environ.get("NO_NOTIFY"), False)


def _should_soft_fail(account: dict, provider_name: str) -> bool:
    """返回失败是否只作为警告处理。"""
    if "fail_soft" in account:
        return _as_bool(account.get("fail_soft"), False)
    return provider_name == "agentrouter" and _is_github_actions()


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


def random_delay(min_seconds: float = 1.0, max_seconds: float = 3.0):
    """随机延迟，模拟人类操作"""
    delay = random.uniform(min_seconds, max_seconds)
    return asyncio.sleep(delay)


async def apply_stealth(page):
    """应用 stealth 反检测脚本，兼容 v1.x 和 v2.x"""
    try:
        from playwright_stealth import Stealth
        await Stealth().apply_stealth_async(page)
        log("已应用 Stealth 反检测 (v2 API)")
        return
    except ImportError:
        pass
    except Exception as e:
        log(f"应用 Stealth v2 失败: {e}", "WARN")
    try:
        from playwright_stealth import stealth_async  # type: ignore
        await stealth_async(page)
        log("已应用 Stealth 反检测 (v1 API)")
    except ImportError:
        log("playwright-stealth 未安装或 API 不兼容，跳过反检测", "WARN")
    except Exception as e:
        log(f"应用 Stealth 失败: {e}", "WARN")


async def create_browser_context(playwright, domain: str, profile_dir: Optional[str] = None, headless: bool = True, channel: Optional[str] = None):
    """创建优化的浏览器上下文。
    profile_dir 给定时使用 launch_persistent_context — cookies 会持久化到该目录,
    GitHub session 在每次访问时由 GitHub 自动续期(_gh_sess rotation),除非用户主动登出。
    返回 (browser_or_context, context):持久化模式下两个值是同一个 context,close 任一都生效。
    """
    common_args = [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-accelerated-2d-canvas',
        '--no-first-run',
        '--no-zygote',
        '--disable-gpu',
        '--disable-blink-features=AutomationControlled',
    ]
    common_kwargs = dict(
        viewport={'width': 1920, 'height': 1080},
        user_agent=HEADERS["User-Agent"],
        locale='zh-CN',
        timezone_id='Asia/Shanghai',
        color_scheme='light',
    )
    init_script = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
        window.chrome = {runtime: {}};
    """

    if profile_dir:
        os.makedirs(profile_dir, exist_ok=True)
        launch_kwargs = dict(
            user_data_dir=profile_dir,
            headless=headless,
            args=common_args,
            **common_kwargs,
        )
        if channel:
            launch_kwargs["channel"] = channel
        try:
            context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            if not channel:
                raise
            log(f"启动 {channel} 失败,回退到 bundled Chromium: {e}", "WARN")
            launch_kwargs.pop("channel", None)
            context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
        await context.add_init_script(init_script)
        return context, context

    launch_kwargs = {"headless": headless, "args": common_args}
    if channel:
        launch_kwargs["channel"] = channel
    try:
        browser = await playwright.chromium.launch(**launch_kwargs)
    except Exception as e:
        if not channel:
            raise
        log(f"启动 {channel} 失败,回退到 bundled Chromium: {e}", "WARN")
        launch_kwargs.pop("channel", None)
        browser = await playwright.chromium.launch(**launch_kwargs)
    context = await browser.new_context(**common_kwargs)
    await context.add_init_script(init_script)
    return browser, context


async def verify_logged_in(page, domain: str) -> bool:
    """通过 /api/user/self 验证 session 是否有效。从 localStorage 读 user.id 并塞 New-Api-User header。"""
    try:
        result = await page.evaluate(f"""
            async () => {{
                try {{
                    const headers = {{'Accept': 'application/json'}};
                    try {{
                        const _u = JSON.parse(localStorage.getItem('user') || '{{}}');
                        if (_u && (_u.id !== undefined && _u.id !== null)) headers['New-Api-User'] = String(_u.id);
                    }} catch(_) {{}}
                    const r = await fetch('{domain}/api/user/self', {{credentials: 'include', headers: headers}});
                    return {{status: r.status, text: await r.text()}};
                }} catch(e) {{ return {{status: 0, text: e.toString()}}; }}
            }}
        """)
        if not result:
            return False
        text = (result.get("text") or "")
        if not text or text.startswith("<"):
            return False
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return False
        return bool(data.get("success")) and bool(data.get("data"))
    except Exception:
        return False


async def force_agentrouter_password_relogin(page, context, domain: str) -> None:
    """AgentRouter needs a fresh password login to count daily check-in."""
    log("AgentRouter 要求先退出登录,再使用账号密码重新登录")
    try:
        logout_result = await page.evaluate(
            """
            async (domain) => {
                const attempts = [
                    {method: 'POST', path: '/api/user/logout', body: '{}'},
                    {method: 'GET', path: '/api/user/logout'},
                    {method: 'POST', path: '/api/user/logout'}
                ];
                const results = [];
                for (const attempt of attempts) {
                    try {
                        const options = {
                            method: attempt.method,
                            credentials: 'include',
                            headers: {
                                'Accept': 'application/json',
                                'Content-Type': 'application/json'
                            }
                        };
                        if (attempt.body !== undefined) {
                            options.body = attempt.body;
                        }
                        const resp = await fetch(domain + attempt.path, options);
                        results.push({method: attempt.method, path: attempt.path, status: resp.status});
                        if (resp.status >= 200 && resp.status < 500) {
                            break;
                        }
                    } catch (e) {
                        results.push({method: attempt.method, path: attempt.path, error: e.toString()});
                    }
                }
                try {
                    const host = new URL(domain).hostname;
                    const cookieDomains = ['', `; domain=${host}`, `; domain=.${host.replace(/^www\\./, '')}`];
                    for (const cookieDomain of cookieDomains) {
                        document.cookie = `session=; Max-Age=0; path=/${cookieDomain}`;
                    }
                    localStorage.clear();
                    sessionStorage.clear();
                } catch (_) {}
                return results;
            }
            """,
            domain,
        )
        if logout_result:
            summary = ", ".join(
                f"{item.get('method')} {item.get('path')}={item.get('status', item.get('error', 'unknown'))}"
                for item in logout_result
            )
            log(f"AgentRouter 退出接口尝试结果: {summary}")
    except Exception as e:
        log(f"AgentRouter 退出接口调用异常,继续清理本地会话: {e}", "WARN")

    try:
        await context.clear_cookies(name="session")
        log("已清理 AgentRouter session cookie")
    except Exception as e:
        log(f"清理 AgentRouter session cookie 失败: {e}", "WARN")

    try:
        await page.evaluate(
            """
            () => {
                try {
                    localStorage.clear();
                    sessionStorage.clear();
                } catch (_) {}
            }
            """
        )
        log("已清理 AgentRouter 本地登录状态")
    except Exception as e:
        log(f"清理 AgentRouter 本地登录状态失败: {e}", "WARN")

    try:
        await page.goto(domain, wait_until="domcontentloaded", timeout=60000)
        await random_delay(4, 7)
        await page.goto(f"{domain}/login", wait_until="domcontentloaded", timeout=60000)
        await random_delay(4, 7)
    except Exception as e:
        log(f"进入 AgentRouter 登录页异常,继续尝试 API 登录: {e}", "WARN")


async def github_oauth_login(page, context, domain: str, github_session: str = None) -> bool:
    """
    执行 GitHub OAuth 登录流程
    使用 GitHub session cookie 来完成 OAuth 授权
    返回: 是否登录成功
    """
    try:
        log("开始 GitHub OAuth 登录流程...")
        
        # 支持三种凭据格式:
        #   (1) github_session 是 list → 完整 cookie 列表 (推荐)
        #   (2) github_session 是 JSON 数组字符串 → 解析后同 (1)
        #   (3) github_session 是字符串 → 仅 _gh_sess (通常不足以让 GitHub 认为已登录)
        cookies_full = None
        if isinstance(github_session, list):
            cookies_full = github_session
        elif isinstance(github_session, str) and github_session.lstrip().startswith("["):
            try:
                cookies_full = json.loads(github_session)
            except json.JSONDecodeError:
                cookies_full = None

        if cookies_full and isinstance(cookies_full, list):
            cookies_to_add = []
            for c in cookies_full:
                if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
                    continue
                ck = {
                    "name": c["name"],
                    "value": str(c["value"]),
                    "domain": c.get("domain", ".github.com"),
                    "path": c.get("path", "/"),
                    "httpOnly": c.get("httpOnly", True),
                    "secure": c.get("secure", True),
                }
                # 关键: 必须传 expires,否则 Chromium 视为 session cookie,持久化 profile 关闭后丢失
                exp = c.get("expirationDate") or c.get("expires")
                if exp:
                    ck["expires"] = int(exp)
                # __Host- 前缀 cookie 不能有 domain (RFC 6265bis),Playwright 要求改用 url
                if c["name"].startswith("__Host-"):
                    ck.pop("domain", None)
                    ck.pop("path", None)
                    ck["url"] = "https://github.com/"
                if c.get("sameSite"):
                    sn = str(c["sameSite"]).lower()
                    ck["sameSite"] = {"lax": "Lax", "strict": "Strict", "no_restriction": "None", "none": "None"}.get(sn, "Lax")
                cookies_to_add.append(ck)
            log(f"添加 {len(cookies_to_add)} 个 GitHub cookies (完整列表模式)")
            if cookies_to_add:
                await context.add_cookies(cookies_to_add)
        elif github_session:
            log("仅有 _gh_sess (单 cookie 模式) - 通常不足以让 GitHub 认为已登录,建议改为完整 cookies JSON 列表", "WARN")
            await context.add_cookies([{
                "name": "_gh_sess",
                "value": github_session,
                "domain": ".github.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
            }])
        
        # 访问登录页面
        login_url = f"{domain}/login"
        log(f"访问登录页面: {login_url}")
        try:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
            await random_delay(2, 4)
        except Exception as e:
            log(f"访问登录页面超时，尝试继续: {e}", "WARN")
            await random_delay(2, 4)
        
        # 先尝试点击注册按钮（某些平台如 AgentRouter 的 GitHub 登录入口在注册页面）
        register_selectors = [
            'button:has-text("注册")',
            'a:has-text("注册")',
            'button:has-text("Register")',
            'a:has-text("Register")',
        ]
        
        for selector in register_selectors:
            try:
                element = page.locator(selector).first
                if await element.is_visible(timeout=2000):
                    log(f"找到注册按钮: {selector}，点击进入注册页面")
                    await element.click()
                    await random_delay(2, 4)
                    break
            except Exception:
                continue
        
        # AgentRouter SPA 的 React onClick handler 在 headless 中被反 bot 检测拦截不会触发，
        # 改为直接调用后端 API 拿 OAuth state + client_id，然后 page.goto 到 GitHub OAuth URL。
        clicked = False
        try:
            log("通过 API 获取 OAuth state 和 github_client_id...")
            api_data = await page.evaluate(f"""
                async () => {{
                    try {{
                        const headers = {{'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}};
                        const sResp = await fetch('{domain}/api/oauth/state', {{headers: headers, credentials: 'include'}});
                        const sData = await sResp.json();
                        const stResp = await fetch('{domain}/api/status', {{headers: headers, credentials: 'include'}});
                        const stData = await stResp.json();
                        return {{state: sData, status: stData}};
                    }} catch(e) {{ return {{error: e.toString()}}; }}
                }}
            """)
            if not api_data or api_data.get("error"):
                log(f"API 调用失败: {api_data}", "WARN")
            else:
                state_val = (api_data.get("state") or {}).get("data")
                status_data = (api_data.get("status") or {}).get("data") or {}
                client_id = status_data.get("github_client_id")
                if state_val and client_id:
                    oauth_url = (
                        f"https://github.com/login/oauth/authorize"
                        f"?client_id={client_id}&state={state_val}&scope=user:email"
                    )
                    log(f"跳转到 GitHub OAuth: client_id={client_id} state={state_val[:8]}...")
                    # GitHub 在国内访问可能很慢；先用 commit 模式只等 navigation start
                    try:
                        await page.goto(oauth_url, wait_until="commit", timeout=90000)
                    except Exception as e:
                        log(f"GitHub OAuth 初次 navigation 超时/失败: {e}", "WARN")
                    # 不论是否 timeout 都继续观察 URL 变化和等待页面稳定
                    for _ in range(8):
                        await random_delay(2, 3)
                        cur = page.url
                        # 1. 已经被 GitHub redirect 回站点 (callback 完成)
                        if domain in cur and "/login" not in cur and "/register" not in cur:
                            clicked = True
                            log(f"GitHub OAuth 已 callback 回站点: {cur}")
                            break
                        # 2. 在 GitHub 授权页(等待自动授权)
                        if "github.com" in cur and "/login" not in cur and "/oauth/authorize" in cur:
                            log(f"在 GitHub 授权页: {cur}")
                            # 继续等待自动 redirect
                            continue
                        # 3. GitHub 要求登录 → cookie 过期
                        if "github.com" in cur and "/login" in cur:
                            log(f"GitHub 要求登录 (cookies 已过期): {cur}", "ERROR")
                            return False
                    if not clicked:
                        log(f"GitHub OAuth 等待超时,当前 URL: {page.url}", "WARN")
                else:
                    log(f"未能从 API 取到 state/client_id (state={state_val}, client_id={bool(client_id)})", "WARN")
        except Exception as e:
            log(f"API 模式 OAuth 异常: {e}", "WARN")

        if not clicked:
            log(f"无法触发 GitHub OAuth 跳转 (当前 URL: {page.url})", "ERROR")
            return False

        # 如果已经回到目标站点 (callback 完成),直接进入验证;否则继续 GitHub 阶段
        if domain in page.url and "/login" not in page.url and "/register" not in page.url:
            log(f"OAuth callback 已完成,跳过 GitHub 阶段处理")
            await random_delay(1, 2)
            # 主动 navigate 到 /console 让 SPA 加载并填充 localStorage
            try:
                await page.goto(f"{domain}/console", wait_until="domcontentloaded", timeout=30000)
                await random_delay(2, 4)
            except Exception as e:
                log(f"导航到 /console 时出错: {e}", "WARN")

            if await verify_logged_in(page, domain):
                log("GitHub OAuth 登录成功 (已通过 /api/user/self 验证)")
                return True
            log("/api/user/self 仍未返回有效用户数据,登录失败", "ERROR")
            return False


        
        # 处理 GitHub 阶段
        await random_delay(1, 2)
        current_url = page.url
        log(f"当前 URL: {current_url}")

        if "github.com" not in current_url:
            log(f"OAuth 跳转失败，当前未到达 GitHub: {current_url}", "ERROR")
            return False

        if "/login" in current_url:
            log("GitHub 要求重新登录，_gh_sess 可能已过期", "ERROR")
            return False

        if "oauth/authorize" in current_url or "authorize" in current_url:
            authorize_selectors = [
                'button[name="authorize"][value="1"]',
                'button[name="authorize"]',
                '#js-oauth-authorize-btn',
                'input[type="submit"][value*="Authorize"]',
                'button:has-text("Authorize")',
                'button.btn-primary[type="submit"]',
            ]
            for selector in authorize_selectors:
                try:
                    element = page.locator(selector).first
                    if await element.is_visible(timeout=3000):
                        log(f"点击授权按钮: {selector}")
                        try:
                            async with page.expect_navigation(
                                url=lambda u: domain in u,
                                timeout=20000,
                                wait_until="domcontentloaded",
                            ):
                                await element.click()
                        except Exception:
                            await element.click()
                            await random_delay(2, 4)
                        break
                except Exception:
                    continue

        try:
            await page.wait_for_url(lambda u: domain in u, timeout=20000)
        except Exception:
            pass

        if domain not in page.url:
            log(f"OAuth 完成后未跳回目标站点 (当前: {page.url})", "ERROR")
            return False
        
        # 通过 /api/user/self 真实验证 session 而非看 URL
        await random_delay(1, 2)
        verify_js = (
            "async () => {"
            "  try {"
            "    const headers = {'Accept': 'application/json'};"
            "    try {"
            "      const u = JSON.parse(localStorage.getItem('user') || '{}');"
            "      if (u && (u.id !== undefined && u.id !== null)) headers['New-Api-User'] = String(u.id);"
            "    } catch(_) {}"
            "    const r = await fetch('" + domain + "/api/user/self', {credentials: 'include', headers: headers});"
            "    return {status: r.status, text: await r.text()};"
            "  } catch(e) { return {status:0, text: e.toString()}; }"
            "}"
        )
        try:
            vr = await page.evaluate(verify_js)
            if vr and vr.get("text") and not vr["text"].startswith("<"):
                try:
                    vdata = json.loads(vr["text"])
                    if vdata.get("success") and vdata.get("data"):
                        log("GitHub OAuth 登录成功 (已通过 /api/user/self 验证)")
                        return True
                except json.JSONDecodeError:
                    pass
            log(f"OAuth 验证失败: status={vr.get('status') if vr else 'N/A'} url={page.url}", "ERROR")
            return False
        except Exception as e:
            log(f"验证登录状态异常: {e}", "ERROR")
            return False
            
    except Exception as e:
        log(f"GitHub OAuth 登录异常: {e}", "ERROR")
        return False


async def playwright_session(domain: str, cookies: dict, api_user: str = "", username: str = "", password: str = "", github_session: str = "", supports_sign_in: bool = True, account_name: str = "", provider_key: str = "") -> Tuple[Optional[dict], Optional[dict], Optional[str]]:
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

    # GitHub OAuth 类平台启用持久化 profile
    profile_dir = None
    if provider_key == "agentrouter" and account_name:
        profile_dir = _profile_dir_for(account_name, provider_key)
    force_password_relogin = provider_key == "agentrouter" and bool(username and password)

    try:
        async with async_playwright() as p:
            headless = True
            browser_channel = None
            if provider_key == "agentrouter" and username and password and not _is_github_actions():
                headless = _as_bool(os.environ.get("AGENTROUTER_HEADLESS"), False)
                browser_channel = os.environ.get("AGENTROUTER_BROWSER_CHANNEL", "chrome").strip() or None
                log(f"AgentRouter 本地浏览器模式: {'headless' if headless else 'headful'}; channel={browser_channel or 'bundled'}")
            browser, context = await create_browser_context(
                p, domain, profile_dir=profile_dir, headless=headless, channel=browser_channel
            )
            if profile_dir:
                exists = _profile_has_session(profile_dir)
                log(f"使用持久化 profile: {profile_dir} ({'已有 session' if exists else '空目录,首次运行'})")

            # 添加用户的 session cookie
            if cookies.get("session"):
                await context.add_cookies([{
                    "name": "session",
                    "value": cookies["session"],
                    "domain": domain.replace("https://", "").replace("http://", ""),
                    "path": "/"
                }])
            
            # 如果有 GitHub session cookie，稍后会添加到上下文
            if github_session:
                log("GitHub session cookie 已配置")

            await apply_stealth(context)
            page = await context.new_page()

            # 用于捕获 API 响应
            captured_data = {"user_info": None, "sign_in": None, "login": None}

            async def handle_response(response):
                try:
                    url = response.url
                    if response.status == 200:
                        text = await response.text()
                        if text.startswith("<") or not text.strip():
                            return
                        if "/api/user/self" in url:
                            data = json.loads(text)
                            if data.get("success") and data.get("data"):
                                captured_data["user_info"] = data["data"]
                            elif data.get("data"):
                                captured_data["user_info"] = data["data"]
                        elif "/api/user/login" in url:
                            data = json.loads(text)
                            captured_data["login"] = data
                            if data.get("success") and data.get("data"):
                                captured_data["user_info"] = data["data"]
                        elif "/api/user/sign_in" in url:
                            data = json.loads(text)
                            captured_data["sign_in"] = data
                except Exception:
                    pass

            page.on("response", handle_response)

            # 访问首页，建立正常浏览痕迹
            log(f"正在通过浏览器访问 {domain}...")
            try:
                await page.goto(domain, wait_until="domcontentloaded", timeout=60000)
                await random_delay(3, 8)
            except Exception as e:
                log(f"访问首页超时，尝试继续: {e}", "WARN")
                await random_delay(2, 4)

            # 导航到 console
            log("导航到 console 页面...")
            try:
                await page.goto(f"{domain}/console", wait_until="domcontentloaded", timeout=60000)
                await random_delay(3, 5)
            except Exception as e:
                log(f"导航到 console 超时，尝试继续: {e}", "WARN")
                await random_delay(2, 4)

            if force_password_relogin:
                if not api_user and captured_data.get("user_info"):
                    old_user_id = captured_data["user_info"].get("id")
                    if old_user_id:
                        api_user = str(old_user_id)
                        log(f"已记录 AgentRouter user_id 用于重新登录后验证: {api_user}")
                await force_agentrouter_password_relogin(page, context, domain)
                captured_data["user_info"] = None
                captured_data["sign_in"] = None

            # 检查 session 是否有效
            current_url = page.url
            session_valid = (
                not force_password_relogin
                and "login" not in current_url
                and captured_data.get("user_info") is not None
            )

            if not session_valid:
                if force_password_relogin:
                    log("已退出旧会话,开始使用账号密码重新登录")
                else:
                    log("Session 无效，需要重新登录", "WARN")
                
                # 尝试 GitHub OAuth 登录
                if github_session or not (username and password):
                    login_success = await github_oauth_login(page, context, domain, github_session)
                    if login_success:
                        # 重新导航到 console
                        await page.goto(f"{domain}/console", wait_until="networkidle", timeout=60000)
                        await random_delay(2, 4)
                
                # 如果 GitHub OAuth 失败，尝试用户名密码登录
                if not captured_data.get("user_info") and username and password:
                    login_result = None
                    for login_attempt in range(1, 4):
                        log("尝试用户名密码登录..." if login_attempt == 1 else f"重试用户名密码登录 ({login_attempt}/3)...")
                        try:
                            login_result = await page.evaluate(
                                """
                                async ({domain, username, password}) => {
                                    try {
                                        let turnstile = '';
                                        try {
                                            const status = JSON.parse(localStorage.getItem('status') || '{}');
                                            if (!status.turnstile_check) turnstile = '';
                                        } catch (_) {}
                                        const resp = await fetch(`${domain}/api/user/login?turnstile=${encodeURIComponent(turnstile)}`, {
                                            method: 'POST',
                                            headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
                                            body: JSON.stringify({username, password}),
                                            credentials: 'include'
                                        });
                                        const text = await resp.text();
                                        if (!text || !text.trim()) {
                                            return {success: false, status: resp.status, message: `empty response (status=${resp.status})`};
                                        }
                                        try {
                                            const data = JSON.parse(text);
                                            data.status = resp.status;
                                            return data;
                                        } catch (e) {
                                            return {success: false, status: resp.status, message: e.toString(), raw: text.substring(0, 200)};
                                        }
                                    } catch (e) {
                                        return {success: false, message: e.toString()};
                                    }
                                }
                                """,
                                {"domain": domain, "username": username, "password": password},
                            )
                        except Exception as e:
                            login_result = {"success": False, "message": f"页面上下文不可用: {e}"}
                            log(f"用户名密码 API 登录异常: {e}", "WARN")
                        if login_result and login_result.get("success"):
                            break
                        if login_result and login_result.get("status") == 429 and force_password_relogin:
                            log("AgentRouter 登录接口返回 429,停止自动重试以避免加重限流", "WARN")
                            break
                        if login_result and login_result.get("status") == 429 and login_attempt < 3:
                            wait_seconds = 25 * login_attempt
                            log(f"AgentRouter 登录被限流(429),等待约 {wait_seconds} 秒后重试", "WARN")
                            await random_delay(wait_seconds, wait_seconds + 8)
                            try:
                                await page.goto(domain, wait_until="domcontentloaded", timeout=60000)
                                await random_delay(4, 7)
                                await page.goto(f"{domain}/login", wait_until="domcontentloaded", timeout=60000)
                                await random_delay(4, 7)
                            except Exception as e:
                                log(f"重建 AgentRouter 登录页面失败: {e}", "WARN")
                            continue
                        break

                    if login_result and login_result.get("success"):
                        if force_password_relogin:
                            log("AgentRouter 账号密码重新登录成功")
                        else:
                            log("用户名密码登录成功")
                        login_user_data = login_result.get("data")
                        if isinstance(login_user_data, dict):
                            captured_data["user_info"] = login_user_data
                            if login_user_data.get("id") and not api_user:
                                api_user = str(login_user_data["id"])
                                log(f"从登录响应获取 api_user: {api_user}")
                            try:
                                await page.evaluate(
                                    """(user) => {
                                        try { localStorage.setItem('user', JSON.stringify(user)); } catch (_) {}
                                    }""",
                                    login_user_data,
                                )
                            except Exception:
                                pass
                    elif username and password:
                        log(f"用户名密码 API 登录失败: {login_result}", "WARN")
                        if force_password_relogin and api_user:
                            try:
                                verified = await page.evaluate(f"""
                                    async () => {{
                                        try {{
                                            const headers = {{'Accept': 'application/json', 'New-Api-User': '{api_user}'}};
                                            const resp = await fetch('{domain}/api/user/self', {{
                                                headers: headers,
                                                credentials: 'include'
                                            }});
                                            return {{status: resp.status, text: await resp.text()}};
                                        }} catch (e) {{
                                            return {{status: 0, text: e.toString()}};
                                        }}
                                    }}
                                """)
                                if verified and verified.get("text") and not verified["text"].startswith("<"):
                                    data = json.loads(verified["text"])
                                    if data.get("success") and data.get("data"):
                                        captured_data["user_info"] = data["data"]
                                        log("登录响应为空,但 /api/user/self 验证成功")
                            except Exception as e:
                                log(f"登录后验证 /api/user/self 失败: {e}", "WARN")
                        if not captured_data.get("user_info"):
                            log("尝试页面表单登录...")
                            try:
                                form_ready = False
                                for attempt in range(1, 4):
                                    await page.goto(f"{domain}/login", wait_until="domcontentloaded", timeout=60000)
                                    await random_delay(4, 7)
                                    for selector in (
                                        'button:has-text("使用 邮箱或用户名 登录")',
                                        'button:has-text("邮箱或用户名")',
                                        'text="使用 邮箱或用户名 登录"',
                                    ):
                                        try:
                                            password_entry = page.locator(selector).first
                                            if await password_entry.is_visible(timeout=2000):
                                                await password_entry.click()
                                                await random_delay(2, 4)
                                                break
                                        except Exception:
                                            pass
                                    username_input = page.locator(
                                        'input[name="username"], #username, '
                                        'input[placeholder*="用户名"], input[placeholder*="邮箱"]'
                                    ).first
                                    if await username_input.is_visible(timeout=5000):
                                        form_ready = True
                                        break
                                    log(f"登录表单未出现,等待页面/Turnstile 后重试 ({attempt}/3)", "WARN")
                                if not form_ready:
                                    raise RuntimeError("登录页未出现用户名输入框,可能仍被 WAF 拦截")
                                turnstile_enabled = False
                                try:
                                    turnstile_enabled = await page.evaluate(
                                        """() => {
                                            try {
                                                const status = JSON.parse(localStorage.getItem('status') || '{}');
                                                return Boolean(status.turnstile_check);
                                            } catch (_) { return false; }
                                        }"""
                                    )
                                except Exception:
                                    pass
                                if turnstile_enabled:
                                    log("等待 Turnstile 环境校验完成...")
                                    await random_delay(10, 16)
                                await page.locator(
                                    'input[name="username"], #username, input[placeholder*="用户名"], input[placeholder*="邮箱"]'
                                ).first.fill(username)
                                await page.locator(
                                    'input[name="password"], #password, input[placeholder*="密码"]'
                                ).first.fill(password)
                                await page.locator(
                                    'button[type="submit"], button:has-text("继续"), button:has-text("登 录"), button:has-text("登录")'
                                ).first.click()
                                await random_delay(6, 10)
                                if captured_data.get("user_info"):
                                    log("页面表单登录成功")
                                else:
                                    log("页面表单登录后未捕获用户信息", "WARN")
                            except Exception as e:
                                log(f"页面表单登录失败: {e}", "WARN")

                    if captured_data.get("user_info"):
                        await page.reload(wait_until="networkidle", timeout=60000)
                        await random_delay(2, 4)

            # 获取新的 session cookie
            browser_cookies = await context.cookies()
            for c in browser_cookies:
                if c["name"] == "session":
                    new_session = c["value"]
                    log("获取到新的 session cookie")
                    break

            # 如果还没有 api_user，尝试从响应中获取
            if not api_user and captured_data.get("user_info"):
                user_id = captured_data["user_info"].get("id")
                if user_id:
                    api_user = str(user_id)
                    log(f"从用户信息获取 api_user: {api_user}")

            # 准备 API 请求头（所有分支共用）
            # 优先用配置的 api_user;否则从前端 localStorage 读取(OAuth 登录后会有)
            api_user_header = (
            'try { const _u = JSON.parse(localStorage.getItem("user") || "{}");'
            ' if (_u && (_u.id !== undefined && _u.id !== null)) headers["New-Api-User"] = String(_u.id); } catch(_) {}'
        )
        if api_user:
            api_user_header = (
                'headers["New-Api-User"] = "' + str(api_user) + '"; '
            ) + api_user_header

            # 对于不支持签到的平台（如 AgentRouter），直接通过 API 获取余额
            if not supports_sign_in:
                log("该平台不支持签到 API,直接通过 /api/user/self 获取余额...")
                if force_password_relogin:
                    balance_success_message = "AgentRouter 已退出并重新账号密码登录成功，已获取余额"
                    balance_failure_message = "AgentRouter 已退出并重新账号密码登录成功，但获取余额失败 (session 可能无效)"
                else:
                    balance_success_message = "AgentRouter 登录成功，已获取余额" if provider_key == "agentrouter" else "该平台不支持签到功能，已获取余额"
                    balance_failure_message = "AgentRouter 登录成功，但获取余额失败 (session 可能无效)" if provider_key == "agentrouter" else "该平台不支持签到功能，且获取余额失败 (session 可能无效)"
                if captured_data.get("user_info"):
                    user_info = captured_data["user_info"]
                    log("使用登录响应中的用户信息")
                    sign_result = {"success": True, "message": balance_success_message}
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    return sign_result, user_info, new_session

                # 确保在 console 页(SPA 已加载,localStorage 有 user data)
                try:
                    if "/console" not in page.url:
                        await page.goto(f"{domain}/console", wait_until="domcontentloaded", timeout=30000)
                        await random_delay(2, 3)
                except Exception as e:
                    log(f"导航 /console 出错: {e}", "WARN")

                # 调用 /api/user/self,从 localStorage 读 user.id 塞 New-Api-User
                fetched = None
                try:
                    fetched = await page.evaluate(f"""
                        async () => {{
                            try {{
                                const headers = {{'Accept': 'application/json'}};
                                try {{
                                    const _u = JSON.parse(localStorage.getItem('user') || '{{}}');
                                    if (_u && (_u.id !== undefined && _u.id !== null)) headers['New-Api-User'] = String(_u.id);
                                }} catch(_) {{}}
                                const r = await fetch('{domain}/api/user/self', {{
                                    headers: headers, credentials: 'include'
                                }});
                                return {{status: r.status, text: await r.text()}};
                            }} catch(e) {{ return {{status: 0, text: e.toString()}}; }}
                        }}
                    """)
                except Exception as e:
                    log(f"调用 /api/user/self 异常: {e}", "WARN")

                if fetched and fetched.get("text") and not fetched["text"].startswith("<"):
                    try:
                        data = json.loads(fetched["text"])
                        if data.get("success") and data.get("data"):
                            user_info = data["data"]
                            log("成功通过 API 获取用户信息")
                        else:
                            log(f"/api/user/self 响应: {fetched['text'][:200]}", "WARN")
                    except json.JSONDecodeError as e:
                        log(f"JSON 解析失败: {e}", "WARN")

                if not user_info and captured_data.get("user_info"):
                    user_info = captured_data["user_info"]
                    log("使用 OAuth 期间捕获的用户信息")

                # 该平台不支持签到 API: 只有真正拿到 user_info 才能算成功
                if user_info:
                    sign_result = {"success": True, "message": balance_success_message}
                else:
                    sign_result = {"success": False, "message": balance_failure_message}
                try:
                    await browser.close()
                except Exception:
                    pass
                return sign_result, user_info, new_session

            else:
                # 支持签到的平台：准备 API 请求头并执行签到
                api_user_header = (
                    'try { const _u = JSON.parse(localStorage.getItem("user") || "{}");'
                    ' if (_u && (_u.id !== undefined && _u.id !== null)) headers["New-Api-User"] = String(_u.id); } catch(_) {}'
                )
                if api_user:
                    api_user_header = (
                        'headers["New-Api-User"] = "' + str(api_user) + '"; '
                    ) + api_user_header

                log("执行签到...")
                
                # 先等待一下，模拟用户操作
                await random_delay(1, 3)
                
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
                        if (text.startsWith('<') || text.includes('cf-ray') || text.includes('cloudflare')) {{
                            return {{success: false, message: '被 WAF 拦截', raw: text.substring(0, 200)}};
                        }}
                        return JSON.parse(text);
                    }} catch (e) {{
                        return {{success: false, message: e.toString()}};
                    }}
                }}
                """)

                # 如果签到被 WAF 拦截，尝试备用方案
                if sign_result and sign_result.get("message") == "被 WAF 拦截":
                    log("API 签到被 WAF 拦截，尝试备用方案...", "WARN")
                    sign_result = await backup_sign_in(page, domain, api_user)

            # 等待并获取余额
            await random_delay(2, 4)
            log("获取用户余额...")

            # 先确保在 console 页面
            current_url = page.url
            if "console" not in current_url:
                log("导航到 console 页面获取余额...")
                await page.goto(f"{domain}/console", wait_until="networkidle", timeout=60000)
                await random_delay(3, 5)

            # 手动 fetch 用户信息
            log("请求用户信息 API...")
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
                        return {{text: text, status: resp.status}};
                    }} catch (e) {{
                        return {{error: e.toString()}};
                    }}
                }}
            """)
            
            if fetched:
                if fetched.get("error"):
                    log(f"API 请求错误: {fetched['error']}", "ERROR")
                elif fetched.get("text"):
                    text = fetched["text"]
                    if text.startswith('<'):
                        log("API 返回 HTML，可能被重定向", "WARN")
                    else:
                        try:
                            data = json.loads(text)
                            log(f"API 响应: {json.dumps(data, ensure_ascii=False)[:200]}")
                            if data.get("success") and data.get("data"):
                                user_info = data["data"]
                                log("成功获取用户信息")
                            elif data.get("data"):
                                user_info = data["data"]
                                log("成功获取用户信息")
                            else:
                                log(f"API 返回数据无 data 字段: {list(data.keys())}", "WARN")
                        except json.JSONDecodeError as e:
                            log(f"JSON 解析失败: {e}", "ERROR")
            
            if not user_info and captured_data.get("user_info"):
                user_info = captured_data["user_info"]
                log("使用捕获的用户信息")

            await browser.close()

    except Exception as e:
        log(f"Playwright 操作失败: {e}", "ERROR")

    return sign_result, user_info, new_session


async def backup_sign_in(page, domain: str, api_user: str = "") -> Optional[dict]:
    """
    备用签到方案：尝试通过导航到特定页面触发签到
    """
    try:
        log("尝试备用签到方案...")
        
        # 一些网站有专门的签到页面
        sign_in_urls = [
            f"{domain}/console/signin",
            f"{domain}/console/checkin",
            f"{domain}/signin",
            f"{domain}/checkin",
        ]
        
        for url in sign_in_urls:
            try:
                log(f"尝试访问: {url}")
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await random_delay(2, 4)
                
                # 检查是否有签到按钮
                sign_buttons = [
                    'button:has-text("签到")',
                    'button:has-text("Sign")',
                    'button:has-text("Check")',
                    '[class*="sign-in"]',
                    '[class*="checkin"]',
                ]
                
                for selector in sign_buttons:
                    try:
                        btn = page.locator(selector).first
                        if await btn.is_visible(timeout=2000):
                            log(f"找到签到按钮: {selector}")
                            await btn.click()
                            await random_delay(2, 4)
                            log("点击签到按钮成功")
                            return {"success": True, "message": "通过点击按钮签到成功"}
                    except Exception:
                        continue
            except Exception:
                continue
        
        # 最后尝试通过 XHR 重新请求
        log("尝试通过 XHR 重新请求签到 API...")
        api_user_header = (
            'try { const _u = JSON.parse(localStorage.getItem("user") || "{}");'
            ' if (_u && (_u.id !== undefined && _u.id !== null)) headers["New-Api-User"] = String(_u.id); } catch(_) {}'
        )
        if api_user:
            api_user_header = (
                'headers["New-Api-User"] = "' + str(api_user) + '"; '
            ) + api_user_header
        
        result = await page.evaluate(f"""
            async () => {{
                try {{
                    // 添加随机延迟模拟人类行为
                    await new Promise(r => setTimeout(r, Math.random() * 2000 + 1000));
                    
                    const headers = {{
                        'Accept': '*/*',
                        'Content-Type': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest'
                    }};
                    {api_user_header}
                    
                    const resp = await fetch('{domain}/api/user/sign_in', {{
                        method: 'POST',
                        headers: headers,
                        body: '{{}}',
                        credentials: 'include',
                        mode: 'cors'
                    }});
                    
                    const text = await resp.text();
                    if (text.startsWith('<')) {{
                        return {{success: false, message: '仍被 WAF 拦截'}};
                    }}
                    return JSON.parse(text);
                }} catch (e) {{
                    return {{success: false, message: e.toString()}};
                }}
            }}
        """)
        
        return result
        
    except Exception as e:
        log(f"备用签到方案失败: {e}", "ERROR")
        return None


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


async def httpx_auto_login(domain: str, username: str, password: str, waf_cookies: dict, login_path: str = "/api/user/login") -> Tuple[Optional[str], Optional[dict]]:
    """使用用户名密码自动登录,返回 (新 session cookie, login 响应中的 user data)"""
    log(f"正在自动登录: {username}...")
    login_url = f"{domain}{login_path}"
    cookie_str = "; ".join([f"{k}={v}" for k, v in waf_cookies.items()])
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            headers = {**HEADERS, "Cookie": cookie_str}
            response = await client.post(
                login_url, headers=headers,
                json={"username": username, "password": password},
            )
            session_cookie = None
            for header_value in response.headers.get_list("set-cookie"):
                if "session=" in header_value:
                    session_cookie = header_value.split("session=")[1].split(";")[0]
                    break
            user_data = None
            try:
                data = response.json()
                if data.get("success") and data.get("data"):
                    user_data = data["data"]
                elif data.get("data"):
                    user_data = data["data"]
            except json.JSONDecodeError:
                log(f"登录响应解析失败: {response.text[:200]}", "ERROR")
            if session_cookie:
                if user_data and user_data.get("id"):
                    log(f"自动登录成功,session 已获取,user_id={user_data['id']}")
                else:
                    log("自动登录成功,获取到新 session")
            else:
                log(f"登录失败: {response.text[:200]}", "ERROR")
            return session_cookie, user_data
    except Exception as e:
        log(f"自动登录异常: {e}", "ERROR")
    return None, None


async def httpx_get_user_info(client: httpx.AsyncClient, domain: str, user_info_path: str) -> Optional[dict]:
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


async def httpx_do_sign_in(client: httpx.AsyncClient, domain: str, sign_in_path: str) -> dict:
    """执行签到"""
    result = {"success": False, "message": ""}
    try:
        url = f"{domain}{sign_in_path}"
        response = await client.post(url, headers=HEADERS, json={})

        if response.status_code == 401:
            result["message"] = "认证失败，请更新 session cookie"
            return result

        if response.status_code in (404, 405):
            result["unsupported"] = True
            result["message"] = "该平台不支持签到接口"
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
            elif "Invalid URL" in str(data) or "invalid_request_error" in str(data):
                result["unsupported"] = True
                result["message"] = "该平台不支持签到接口"
            else:
                result["message"] = data.get("message", data.get("msg", str(data)))
        except json.JSONDecodeError:
            text = response.text.strip()
            lower_text = text.lower()
            if (
                text.startswith("<")
                or "var arg1=" in lower_text
                or "cf-ray" in lower_text
                or "cloudflare" in lower_text
            ):
                result["message"] = f"被 WAF 拦截: {text[:100]}"
            elif (
                "success" in lower_text
                or "签到成功" in text
                or "已经签到" in text
                or "already" in lower_text
            ):
                result["success"] = True
                result["message"] = "签到成功"
            else:
                result["message"] = f"响应解析失败: {text[:100]}"
    except httpx.HTTPStatusError as e:
        result["message"] = f"HTTP 错误: {e.response.status_code}"
    except Exception as e:
        result["message"] = f"签到异常: {str(e)}"
    return result


# 模块级 WAF cookie 缓存(同一域名的多个账号共享)
_WAF_COOKIES_CACHE: dict = {}


async def process_account_httpx(account: dict) -> dict:
    """旧版 httpx 流程: 适用于 anyrouter 等支持 sign_in API 的 NewAPI 平台"""
    name = account.get("name", "未命名账号")
    cookies = account.get("cookies", {}) or {}
    api_user = account.get("api_user", "")
    username = account.get("username", "")
    password = account.get("password", "")
    provider_name = account.get("provider", "anyrouter")

    provider = PROVIDERS.get(provider_name, PROVIDERS["anyrouter"])
    domain = account.get("domain", provider["domain"])
    login_path = account.get("login_path", provider.get("login_path", "/api/user/login"))
    sign_in_path = account.get("sign_in_path", provider.get("sign_in_path", "/api/user/sign_in"))
    user_info_path = account.get("user_info_path", provider.get("user_info_path", "/api/user/self"))
    supports_sign_in = account.get("supports_sign_in", provider.get("supports_sign_in", True))

    result = {
        "name": name, "provider": provider_name,
        "success": False, "message": "",
        "quota": None, "used": None, "balance": None,
    }

    log(f"[httpx 流程] 正在处理账号: {name} ({provider_name})")

    # 获取该域名的 WAF cookies(缓存)
    if domain not in _WAF_COOKIES_CACHE:
        _WAF_COOKIES_CACHE[domain] = await get_waf_cookies(domain)
    waf_cookies = _WAF_COOKIES_CACHE[domain]

    all_cookies = {**waf_cookies, **cookies}
    cookie_str = "; ".join([f"{k}={v}" for k, v in all_cookies.items()])

    async with httpx.AsyncClient(timeout=30.0) as client:
        client.headers["Cookie"] = cookie_str
        if api_user:
            client.headers["New-Api-User"] = str(api_user)

        user_info = await httpx_get_user_info(client, domain, user_info_path)
        session_valid = user_info is not None

        if not session_valid and username and password:
            log("Session 已过期,尝试自动登录...", "WARN")
            new_session, login_user_data = await httpx_auto_login(domain, username, password, waf_cookies, login_path)
            if new_session:
                all_cookies["session"] = new_session
                cookie_str = "; ".join([f"{k}={v}" for k, v in all_cookies.items()])
                client.headers["Cookie"] = cookie_str
                # 关键: NewAPI 协议要求 New-Api-User header,从 login 响应直接拿 user_id
                if login_user_data and login_user_data.get("id") and not api_user:
                    uid = login_user_data["id"]
                    client.headers["New-Api-User"] = str(uid)
                    api_user = str(uid)
                    log(f"已设置 New-Api-User header: {uid}")
                # login 响应已含 user_info,先缓存
                if login_user_data:
                    user_info = login_user_data
                log("已使用新 session cookie")
            else:
                log("自动登录失败", "ERROR")

        if not user_info:
            user_info = await httpx_get_user_info(client, domain, user_info_path)

        if supports_sign_in:
            sign_result = await httpx_do_sign_in(client, domain, sign_in_path)
            if sign_result.get("unsupported"):
                result["success"] = user_info is not None
                result["message"] = "该平台不支持签到功能，已获取余额" if user_info else "该平台不支持签到功能，且获取余额失败"
            else:
                result["success"] = sign_result["success"]
                result["message"] = sign_result["message"]
        else:
            result["success"] = user_info is not None
            result["message"] = "该平台不支持签到功能，已获取余额" if user_info else "该平台不支持签到功能，且获取余额失败"

        if result["success"]:
            log(f"签到结果: {result['message']}")
        else:
            log(f"签到失败: {result['message']}", "ERROR")

        import asyncio as _aio
        await _aio.sleep(1)
        user_info = await httpx_get_user_info(client, domain, user_info_path)
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


async def process_account(account: dict) -> dict:
    """处理单个账号 - 根据 provider 分流到 Playwright 或 httpx 流程"""
    provider_name = account.get("provider", "anyrouter")
    provider = PROVIDERS.get(provider_name, PROVIDERS["anyrouter"])
    cookies = account.get("cookies", {}) or {}
    username = account.get("username", "")
    password = account.get("password", "")

    # 分流:
    # - 支持 /api/user/sign_in 的 NewAPI 系平台: 走 httpx + WAF cookies + username/password 自动登录
    # - agentrouter 未配置账号密码/session 时: 保留 Playwright + GitHub OAuth 兼容流程
    has_httpx_login = bool(username and password) or bool(cookies.get("session"))
    if provider.get("supports_sign_in", True) and (provider_name != "agentrouter" or has_httpx_login):
        return await process_account_httpx(account)

    name = account.get("name", "未命名账号")
    api_user = account.get("api_user", "")
    github_session = account.get("github_session", "") or os.environ.get("GITHUB_SESSION", "")
    domain = account.get("domain", provider["domain"])

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

    log(f"正在处理账号: {name} ({provider_name})")

    if provider_name == "agentrouter" and _is_github_actions():
        result["success"] = True
        result["soft_failed"] = True
        result["balance_skipped"] = True
        result["message"] = "GitHub Actions 已跳过 AgentRouter; 请使用本地定时任务签到"
        log(result["message"], "WARN")
        return result
    
    # 获取平台是否支持签到
    supports_sign_in = account.get("supports_sign_in", provider.get("supports_sign_in", True))
    log(f"平台签到支持: {'是' if supports_sign_in else '否'}")

    # 使用 Playwright 执行操作
    sign_result, user_info, new_session = await playwright_session(
        domain, cookies, api_user, username, password, github_session, supports_sign_in,
        account_name=name, provider_key=provider_name,
    )

    # 处理签到结果
    if sign_result:
        if sign_result.get("success") is True:
            result["success"] = True
            result["message"] = sign_result.get("message", "签到成功")
        elif "已经签到" in str(sign_result) or "already" in str(sign_result).lower():
            result["success"] = True
            result["message"] = "今日已签到"
        elif "Invalid URL" in str(sign_result) or "invalid_request_error" in str(sign_result):
            # 该平台不支持签到 API
            result["success"] = True
            result["message"] = "该平台不支持签到功能"
        else:
            result["message"] = sign_result.get("message", str(sign_result))
    else:
        result["message"] = "签到请求失败"

    if not result["success"] and _should_soft_fail(account, provider_name):
        original_message = result["message"] or "执行失败"
        result["success"] = True
        result["soft_failed"] = True
        result["balance_skipped"] = True
        if provider_name == "agentrouter" and _is_github_actions():
            result["message"] = "GitHub Actions 云端被 AgentRouter WAF 拦截,已跳过(本地运行可登录查余额)"
        else:
            result["message"] = f"非阻塞失败: {original_message}"
        log(f"非阻塞警告: {original_message}", "WARN")

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
        if result.get("balance_skipped"):
            log("余额获取已跳过: GitHub Actions 云端 AgentRouter WAF 拦截", "WARN")
        else:
            log("未能获取余额信息", "WARN")

    return result


async def send_serverchan(title: str, content: str):
    """通过 Server酱 推送通知"""
    if _notifications_disabled():
        return

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


def _build_feishu_card(title: str, content: str, has_failure: bool = False, has_warning: bool = False) -> dict:
    template = "red" if has_failure else ("orange" if has_warning else "green")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {
                "tag": "plain_text",
                "content": title,
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": content,
            }
        ],
    }


def _feishu_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        b"",
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


async def send_feishu_webhook(title: str, content: str, has_failure: bool = False, has_warning: bool = False):
    """通过飞书/Lark 自定义机器人 Webhook 推送通知"""
    webhook = os.environ.get("FEISHU_WEBHOOK", "") or os.environ.get("LARK_WEBHOOK", "")
    if not webhook:
        return

    secret = os.environ.get("FEISHU_SECRET", "") or os.environ.get("LARK_SECRET", "")
    payload = {
        "msg_type": "interactive",
        "card": _build_feishu_card(title, content, has_failure, has_warning),
    }

    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = _feishu_sign(timestamp, secret)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(webhook, json=payload)
            data = response.json()
            code = data.get("code", data.get("StatusCode"))
            if code == 0:
                log("飞书 Webhook 推送成功")
            else:
                log(f"飞书 Webhook 推送失败: {data.get('msg') or data.get('message') or data}", "ERROR")
    except Exception as e:
        log(f"飞书 Webhook 推送异常: {e}", "ERROR")


async def _get_feishu_tenant_access_token(client: httpx.AsyncClient, app_id: str, app_secret: str) -> Optional[str]:
    response = await client.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
    )
    data = response.json()
    if data.get("code") == 0 and data.get("tenant_access_token"):
        return data["tenant_access_token"]

    log(f"飞书 App 获取 tenant_access_token 失败: {data.get('msg') or data.get('message') or data}", "ERROR")
    return None


async def send_feishu_app(title: str, content: str, has_failure: bool = False, has_warning: bool = False):
    """通过飞书/Lark 自建应用机器人推送通知"""
    app_id = os.environ.get("FEISHU_APP_ID", "") or os.environ.get("LARK_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "") or os.environ.get("LARK_APP_SECRET", "")
    receive_id = os.environ.get("FEISHU_RECEIVE_ID", "") or os.environ.get("LARK_RECEIVE_ID", "")
    receive_id_type = (
        os.environ.get("FEISHU_RECEIVE_ID_TYPE", "")
        or os.environ.get("LARK_RECEIVE_ID_TYPE", "")
        or "chat_id"
    )

    if not app_id and not app_secret and not receive_id:
        return
    if not app_id or not app_secret:
        log("飞书 App 推送跳过: 缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET", "WARN")
        return
    if not receive_id:
        log("飞书 App 推送跳过: 缺少 FEISHU_RECEIVE_ID (如群 chat_id: oc_xxx)", "WARN")
        return

    payload = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(_build_feishu_card(title, content, has_failure, has_warning), ensure_ascii=False),
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token = await _get_feishu_tenant_access_token(client, app_id, app_secret)
            if not token:
                return

            response = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            data = response.json()
            if data.get("code") == 0:
                log("飞书 App 推送成功")
            else:
                log(f"飞书 App 推送失败: {data.get('msg') or data.get('message') or data}", "ERROR")
    except Exception as e:
        log(f"飞书 App 推送异常: {e}", "ERROR")


async def send_feishu(title: str, content: str, has_failure: bool = False, has_warning: bool = False):
    if _notifications_disabled():
        return

    await send_feishu_webhook(title, content, has_failure, has_warning)
    await send_feishu_app(title, content, has_failure, has_warning)


async def relogin_account(name_or_provider: str):
    """打开有头浏览器,让用户手动完成 GitHub 登录,cookies 自动持久化到 profile 目录。
    后续运行无需 .env 中的 github_session,且 GitHub 会自动续期 _gh_sess。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log("Playwright 未安装", "ERROR")
        return

    accounts = get_accounts()
    target = name_or_provider.lower()
    matches = [a for a in accounts if a.get("name", "").lower() == target or a.get("provider", "").lower() == target]
    matches = [a for a in matches if a.get("provider") == "agentrouter"]

    if not matches:
        log(f"未找到匹配的 agentrouter 账号: {name_or_provider}", "ERROR")
        log(f"可用账号: {[a.get('name') for a in accounts if a.get('provider') == 'agentrouter']}")
        return

    for acc in matches:
        name = acc.get("name", "")
        domain = acc.get("domain") or PROVIDERS.get(acc["provider"], {}).get("domain", "https://agentrouter.org")
        profile_dir = _profile_dir_for(name, "agentrouter")
        log("=" * 50)
        log(f"为账号 {name} 打开有头浏览器进行 GitHub 登录")
        log(f"profile 目录: {profile_dir}")
        log("=" * 50)
        async with async_playwright() as p:
            ctx, _ = await create_browser_context(p, domain, profile_dir=profile_dir, headless=False)
            page = await ctx.new_page()
            await page.goto("https://github.com/login", wait_until="domcontentloaded")
            log(">>> 请在浏览器中完成 GitHub 登录 (含 2FA / 邮件验证),登录后返回此终端按 Enter 继续...")
            await asyncio.to_thread(input, "登录完成后按 Enter: ")
            # 顺便访问一次 agentrouter 完成 OAuth,把平台 session 也保存到 profile
            try:
                await page.goto(domain, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)
            except Exception:
                pass
            await ctx.close()
        log(f"profile 已保存: {profile_dir}")
        log("以后直接运行 python checkin.py 即可,无需再依赖 .env 中的 github_session")


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
    warning_count = sum(1 for r in results if r.get("soft_failed"))
    success_count = sum(1 for r in results if r["success"] and not r.get("soft_failed"))
    fail_count = sum(1 for r in results if not r["success"])

    log("=" * 50)
    log("签到完成统计")
    log(f"成功: {success_count}, 警告: {warning_count}, 失败: {fail_count}")
    log("=" * 50)

    # 构建推送内容
    notify_lines = []
    for r in results:
        status = "⚠️" if r.get("soft_failed") else ("✅" if r["success"] else "❌")
        line = f"{status} **{r['name']}**: {r['message']}"
        if r["balance"] is not None:
            line += f"\n   - 💰 当前余额: **${r['balance']:.2f}**"
            line += f"\n   - 📊 历史消耗: ${r['used']:.2f}"
        elif r.get("balance_skipped"):
            line += f"\n   - 💰 余额: 已跳过(云端 WAF 拦截)"
        else:
            line += f"\n   - 💰 余额: 获取失败"

        log_status = "⚠" if r.get("soft_failed") else ("✓" if r["success"] else "✗")
        log_line = f"{log_status} {r['name']}: {r['message']}"
        if r["balance"] is not None:
            log_line += f" | 余额: ${r['balance']:.2f}, 消耗: ${r['used']:.2f}"
        log(log_line)
        notify_lines.append(line)

    # Server酱 推送
    title = f"AnyRouter 签到 - 成功{success_count} 警告{warning_count} 失败{fail_count}"
    content = f"## 📋 签到结果\n\n"
    content += f"- ⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    content += f"- ✅ 成功: {success_count}\n"
    content += f"- ⚠️ 警告: {warning_count}\n"
    content += f"- ❌ 失败: {fail_count}\n\n"
    content += "---\n\n"
    content += "## 📊 账号详情\n\n"
    for line in notify_lines:
        content += f"{line}\n\n"
    await send_serverchan(title, content)
    await send_feishu(title, content, has_failure=fail_count > 0, has_warning=warning_count > 0)

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--relogin":
        asyncio.run(relogin_account(sys.argv[2]))
    elif len(sys.argv) >= 2 and sys.argv[1] in ("-h", "--help"):
        print("用法:")
        print("  python checkin.py                        # 正常签到所有账号")
        print("  python checkin.py --relogin <账号名>     # 有头浏览器手动登录,持久化 cookies")
    else:
        asyncio.run(main())
