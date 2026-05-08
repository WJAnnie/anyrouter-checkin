# AnyRouter 自动签到

自动签到脚本,支持 [AnyRouter](https://anyrouter.top)、[AgentRouter](https://agentrouter.org) 等基于 NewAPI / OneAPI 的平台。多账号、多平台、多种登录方式。

## ✨ 特性

- 🍪 **持久化浏览器 profile** — AgentRouter 类登录一次,自动维护 cookies,GitHub session 由 GitHub 自动续期,**无需定期手动导出**
- 🥷 **WAF 绕过** — Playwright + stealth 反检测 + 真实浏览器指纹,稳定通过 anyrouter / agentrouter 的 WAF
- 🔐 **多种登录方式** — 平台 session cookie / 用户名密码 / GitHub OAuth(完整 cookies)
- 🔄 **自动 fallback** — session 过期自动用 username/password 或 github_session 重新登录
- 🆔 **自动获取 user_id** — 从登录响应或 localStorage 自动提取 `New-Api-User`,无需手动配 `api_user`
- 📢 **Server 酱推送** — 签到结果自动推送到微信
- 🔁 **手动续期入口** — `--relogin` 命令在 cookies 失效时打开有头浏览器手动登录

## 📦 快速开始

### 1. 本地运行

```bash
git clone https://github.com/WJAnnie/anyrouter-checkin.git
cd anyrouter-checkin

# 安装依赖
pip install -r requirements.txt
python -m playwright install chromium

# 配置环境变量
cp .env.example .env
# 编辑 .env,填入 ANYROUTER_ACCOUNTS

# 运行
python checkin.py
```

### 2. GitHub Actions 定时运行

1. **Fork 本仓库** → Settings → Environments → New environment → 命名为 `production`
2. 添加 Secrets:

| Secret | 必填 | 说明 |
|--------|------|------|
| `ANYROUTER_ACCOUNTS` | ✅ | 账号配置 JSON(同 .env.example) |
| `SERVERCHAN_KEY` | ❌ | Server 酱推送 key |
| `GITHUB_SESSION` | ❌ | 全局 GitHub session,当账号未指定 github_session 时使用 |

3. Actions 标签 → 启用工作流。脚本默认每天北京时间 8:30 自动运行。

> ⚠️ **注意**: GitHub Actions 是每次运行后销毁实例,**无法持久化 profile**。云端跑必须在 `ANYROUTER_ACCOUNTS` 里提供 `github_session` 或 `username+password` 让脚本每次自登录。本地运行才能享受持久化 profile 的优势。

## 🎯 账号配置详解

`ANYROUTER_ACCOUNTS` 是一个 JSON 数组,每个对象代表一个账号:

```json
[
  {
    "name": "账号显示名",
    "provider": "anyrouter | agentrouter",
    "domain": "https://...",            // 可选,覆盖 provider 默认域名
    "cookies": {"session": "xxx"},      // 方式 A: 直接给平台 session
    "username": "...",                   // 方式 B: 用户名 + 密码
    "password": "...",
    "github_session": [...],             // 方式 C: 完整 GitHub cookies 列表
    "api_user": "123"                    // 可选,自动检测,无需手动填
  }
]
```

### 方式 A:平台 session cookie(最简单)

适合所有 NewAPI 平台。登录后 F12 → Application → Cookies → 复制 `session`。

```json
[{
  "name": "我的账号",
  "provider": "anyrouter",
  "cookies": {"session": "MTc3O..."}
}]
```

### 方式 B:用户名 + 密码(推荐用于 anyrouter)

```json
[{
  "name": "我的账号",
  "provider": "anyrouter",
  "username": "your_username",
  "password": "your_password"
}]
```

session 失效时,脚本会自动用账号密码重新登录,登录响应里直接包含 `user_id`,自动设置 `New-Api-User` header。

### 方式 C:GitHub OAuth(推荐用于 agentrouter)

agentrouter 仅支持 GitHub 登录。配置一次,后续 profile 自动维护。

**首次配置(引导):**

1. 浏览器登录 [github.com](https://github.com),F12 → Application → Cookies → github.com
2. 复制以下 5 个 cookie 的值: `user_session`、`__Host-user_session_same_site`、`_gh_sess`、`logged_in`、`dotcom_user`(推荐用 [Cookie-Editor](https://cookie-editor.com) 一键导出全部 cookies)
3. 填入 .env(参考 `.env.example` 中的 agentrouter 示例)

```json
[{
  "name": "AgentRouter主账号",
  "provider": "agentrouter",
  "github_session": [
    {"name": "user_session", "value": "...", "domain": "github.com"},
    {"name": "__Host-user_session_same_site", "value": "...", "domain": "github.com"},
    {"name": "_gh_sess", "value": "...", "domain": "github.com"},
    {"name": "logged_in", "value": "yes", "domain": ".github.com"},
    {"name": "dotcom_user", "value": "your_github_username", "domain": ".github.com"}
  ]
}]
```

## 🔄 持久化 profile 机制(本地运行)

**首次运行**: 脚本读取 `github_session` → OAuth → AgentRouter session 写入 `.browser_profile/agentrouter_<name>/`

**后续运行**: 直接复用 profile session,**完全跳过 GitHub OAuth**。GitHub 会在每次访问时自动 rotate `_gh_sess`,只要不在其他浏览器登出 / 改密码,session 可长期有效(实测 6 个月以上)。

**何时需要更新**:
- 在另一浏览器主动登出 GitHub
- 改了 GitHub 密码 / 启用 2FA
- AgentRouter 主动作废 session(罕见)

**手动续期**:

```bash
python checkin.py --relogin AgentRouter主账号
```

会弹出有头 Chromium → 你手动完成 GitHub 登录(含 2FA / 邮件验证) → 按 Enter → profile 自动保存,**无需再修改 .env**。

## 🔧 配置参数详表

| 参数 | 类型 | 说明 |
|------|------|------|
| `name` | string | 账号名,用于日志和 profile 目录命名 |
| `provider` | string | `anyrouter` / `agentrouter`,决定流程分发 |
| `domain` | string | 自定义域名,覆盖 provider 默认值 |
| `cookies.session` | string | 平台 session cookie |
| `username` / `password` | string | 平台账号密码 |
| `github_session` | string \| array | GitHub session,推荐用完整 cookies 数组 |
| `api_user` | string | New-Api-User header 值,通常自动检测无需手填 |

## 🛠 故障排除

### Q: AnyRouter 报 "session 已过期"

正常现象,脚本会自动用 username/password 重新登录。如果 username/password 也没配置,需要手动更新 cookies 或加上账号密码。

### Q: AgentRouter 提示 "GitHub OAuth 失败"

1. 确认 `.env` 中 `github_session` 是 **5 个完整 cookie 的 JSON 数组**(不是单个 `_gh_sess` 字符串)
2. 在浏览器中访问 https://github.com,确认右上角能看到自己的头像(说明 cookies 真的有效)
3. 如果反复失败,可能是在另一浏览器登录导致 session 作废,重新导出即可

### Q: 想完全摆脱 .env 中的 cookies

本地跑一次 `python checkin.py --relogin <账号名>`,profile 接管之后可以删掉 `github_session`,只保留 `name` + `provider`。**注意:GitHub Actions 云端跑没有持久化目录,无法用此方式**。

### Q: localStorage 中没有 user.id 怎么办

通常意味着平台 SPA 没成功初始化。脚本会自动 fallback 到 OAuth 期间捕获的 `/api/user/self` 响应,无需手动干预。

## 📋 命令行用法

```bash
python checkin.py                        # 正常签到所有账号
python checkin.py --relogin <账号名>      # 有头浏览器手动登录,持久化 cookies
python checkin.py --help                 # 显示帮助
```

## 🏗 技术实现

| 组件 | 用途 |
|------|------|
| Playwright + Chromium | 浏览器自动化,支持持久化 profile |
| playwright-stealth | 反 bot 检测,绕过 Cloudflare/aliyun WAF |
| httpx | 普通 NewAPI 平台的纯 HTTP 流程(无需浏览器,更快) |
| python-dotenv | 本地 .env 加载 |

**流程分发**:
- `provider == "agentrouter"` 或 `supports_sign_in == False` → Playwright + 持久化 profile
- 其他(标准 NewAPI 系)→ httpx + WAF cookie 复用 + username/password 自登录

## 📜 License

MIT
