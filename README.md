# AnyRouter 自动签到

自动签到脚本,支持 [AnyRouter](https://anyrouter.top)、[AgentRouter](https://agentrouter.org) 等基于 NewAPI / OneAPI 的平台。多账号、多平台、多种登录方式。

## ✨ 特性

- 🍪 **持久化浏览器 profile** — GitHub OAuth 类登录一次,自动维护 cookies,GitHub session 由 GitHub 自动续期,**无需定期手动导出**
- 🥷 **WAF 绕过** — Playwright + stealth 反检测 + 真实浏览器指纹,稳定通过 anyrouter / agentrouter 的 WAF
- 🔐 **多种登录方式** — 平台 session cookie / 用户名密码 / GitHub OAuth(完整 cookies)
- 🔄 **自动 fallback** — session 过期自动用 username/password 或 github_session 重新登录
- 🔁 **AgentRouter 重新登录签到** — AgentRouter 每次先退出旧会话,再用账号密码重新登录并获取余额
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
| `FEISHU_WEBHOOK` | ❌ | 飞书/Lark 自定义机器人 Webhook URL |
| `FEISHU_SECRET` | ❌ | 飞书/Lark 自定义机器人签名密钥,未开启签名可不填 |
| `FEISHU_APP_ID` | ❌ | 飞书/Lark 自建应用 App ID |
| `FEISHU_APP_SECRET` | ❌ | 飞书/Lark 自建应用 App Secret |
| `FEISHU_RECEIVE_ID_TYPE` | ❌ | App 推送接收 ID 类型,默认 `chat_id` |
| `FEISHU_RECEIVE_ID` | ❌ | App 推送接收目标,如群聊 `oc_xxx` |
| `GITHUB_SESSION` | ❌ | 全局 GitHub session,当账号未指定 github_session 时使用 |

也可以用 GitHub CLI 更新 production 环境的 Secret:

```bash
gh secret set ANYROUTER_ACCOUNTS --env production --body '[{"name":"AnyRouter账号1","provider":"anyrouter","username":"YOUR_ANYROUTER_USERNAME","password":"YOUR_ANYROUTER_PASSWORD"}]'
gh secret set FEISHU_WEBHOOK --env production --body 'https://open.feishu.cn/open-apis/bot/v2/hook/xxxx'
gh secret set FEISHU_SECRET --env production --body 'YOUR_FEISHU_BOT_SECRET'
gh secret set FEISHU_APP_ID --env production --body 'cli_xxx'
gh secret set FEISHU_APP_SECRET --env production --body 'YOUR_FEISHU_APP_SECRET'
gh secret set FEISHU_RECEIVE_ID_TYPE --env production --body 'chat_id'
gh secret set FEISHU_RECEIVE_ID --env production --body 'oc_xxx'
```

`FEISHU_SECRET` 只有在飞书机器人开启“签名校验”时需要配置。没有开启签名时,只配置 `FEISHU_WEBHOOK` 即可。

3. Actions 标签 → 启用工作流。脚本默认每天北京时间 8:30 自动运行。

> ⚠️ **注意**: GitHub Actions 是每次运行后销毁实例,**无法持久化 profile**。云端跑必须在 `ANYROUTER_ACCOUNTS` 里提供 `username+password` 让脚本每次自登录。本地运行才能享受持久化 profile 的优势。
>
> AgentRouter 的 WAF 对 GitHub-hosted runner 更严格,建议不要放进 GitHub Actions 的 `ANYROUTER_ACCOUNTS`。AgentRouter 使用本地定时任务 `scripts/run_agentrouter_local.ps1` 签到;如果误放进 GitHub Actions,脚本会直接跳过,不再尝试自动登录。

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
    "api_user": "123",                   // 可选,自动检测,无需手动填
    "fail_soft": true                    // 可选,失败只记警告,不让任务退出失败
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

### 方式 B:用户名 + 密码(推荐用于 anyrouter / agentrouter)

```json
[{
  "name": "我的账号",
  "provider": "agentrouter",
  "username": "your_username",
  "password": "your_password"
}]
```

anyrouter 会在 session 失效时自动用账号密码重新登录。AgentRouter 的每日签到以“重新登录”为准,因此脚本每次运行都会先退出旧会话并清理当前浏览器状态,再用账号密码登录,登录响应里直接包含 `user_id`,自动设置 `New-Api-User` header。`provider` 可填写 `anyrouter` 或 `agentrouter`。

### 方式 C:GitHub OAuth(agentrouter 备用)

如果 agentrouter 账号不想配置平台账号密码,仍可使用 GitHub OAuth。配置一次,后续 profile 自动维护。

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

**AgentRouter username/password 模式**: 仍使用 `.browser_profile/agentrouter_<name>/` 维持浏览器指纹和 WAF 状态,但每次运行都会先退出 AgentRouter 旧会话,再重新账号密码登录。日志中看到 `AgentRouter 已退出并重新账号密码登录成功，已获取余额` 才表示这次重新登录式签到完成。

本地 AgentRouter 默认优先使用系统 Chrome + headful 模式,以便通过登录页风控。可以用环境变量覆盖:

```env
AGENTROUTER_HEADLESS=1
AGENTROUTER_BROWSER_CHANNEL=chrome
```

`AGENTROUTER_BROWSER_CHANNEL` 留空时会使用 Playwright bundled Chromium。

**首次运行 OAuth 模式**: 脚本读取 `github_session` → OAuth → AgentRouter session 写入 `.browser_profile/agentrouter_<name>/`

**后续运行 OAuth 模式**: 直接复用 profile session,**完全跳过 GitHub OAuth**。GitHub 会在每次访问时自动 rotate `_gh_sess`,只要不在其他浏览器登出 / 改密码,session 可长期有效(实测 6 个月以上)。

**何时需要更新**:
- 在另一浏览器主动登出 GitHub
- 改了 GitHub 密码 / 启用 2FA
- AgentRouter 主动作废 session(罕见)

**手动续期**:

```bash
python checkin.py --relogin AgentRouter主账号
```

会弹出有头 Chromium → 你手动完成 GitHub 登录(含 2FA / 邮件验证) → 按 Enter → profile 自动保存,**无需再修改 .env**。

## 🔔 通知推送

脚本支持多个通知通道并行推送,配置哪个就推哪个。

### Server 酱

本地 `.env` 或 GitHub Actions Secret 中配置:

```env
SERVERCHAN_KEY=你的SendKey
```

### 飞书 / Lark 自定义机器人 Webhook

1. 飞书群聊 → 群设置 → 群机器人 → 添加机器人 → 自定义机器人
2. 复制 Webhook URL
3. 本地 `.env` 配置:

```env
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
FEISHU_SECRET=如果开启签名校验则填写
```

GitHub Actions 则在 `production` 环境 Secrets 里配置同名变量:

```bash
gh secret set FEISHU_WEBHOOK --env production --body 'https://open.feishu.cn/open-apis/bot/v2/hook/xxxx'
gh secret set FEISHU_SECRET --env production --body 'YOUR_FEISHU_BOT_SECRET'
```

`FEISHU_SECRET` 可留空。脚本也兼容 `LARK_WEBHOOK` / `LARK_SECRET` 变量名。

### 飞书 / Lark 自建应用

如果你已有飞书开放平台应用的 App ID / App Secret,也可以走 App API 推送。应用需要具备发送消息相关权限,并且机器人需要能访问目标会话。

本地 `.env` 配置:

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=你的AppSecret
FEISHU_RECEIVE_ID_TYPE=chat_id
FEISHU_RECEIVE_ID=oc_xxx
```

GitHub Actions 则在 `production` 环境 Secrets 里配置同名变量:

```bash
gh secret set FEISHU_APP_ID --env production --body 'cli_xxx'
gh secret set FEISHU_APP_SECRET --env production --body 'YOUR_FEISHU_APP_SECRET'
gh secret set FEISHU_RECEIVE_ID_TYPE --env production --body 'chat_id'
gh secret set FEISHU_RECEIVE_ID --env production --body 'oc_xxx'
```

常见 `FEISHU_RECEIVE_ID_TYPE`: `chat_id`、`open_id`、`user_id`、`email`。群聊推荐 `chat_id`,值形如 `oc_xxx`。脚本也兼容 `LARK_APP_ID` / `LARK_APP_SECRET` / `LARK_RECEIVE_ID_TYPE` / `LARK_RECEIVE_ID`。

## 🔧 配置参数详表

| 参数 | 类型 | 说明 |
|------|------|------|
| `name` | string | 账号名,用于日志和 profile 目录命名 |
| `provider` | string | `anyrouter` / `agentrouter`,决定流程分发 |
| `domain` | string | 自定义域名,覆盖 provider 默认值 |
| `login_path` | string | 登录接口路径,默认 `/api/user/login` |
| `sign_in_path` | string | 签到接口路径,默认 `/api/user/sign_in` |
| `supports_sign_in` | boolean | 是否调用签到接口,默认 `true`;如果平台只支持登录和查余额可设为 `false` |
| `fail_soft` | boolean | 失败是否只作为警告处理;AgentRouter 在 GitHub Actions 默认开启,可显式设为 `false` 改回严格失败 |
| `cookies.session` | string | 平台 session cookie |
| `username` / `password` | string | 平台账号密码 |
| `github_session` | string \| array | GitHub session,推荐用完整 cookies 数组 |
| `api_user` | string | New-Api-User header 值,通常自动检测无需手填 |

## 🛠 故障排除

### Q: AnyRouter 报 "session 已过期"

正常现象,脚本会自动用 username/password 重新登录。如果 username/password 也没配置,需要手动更新 cookies 或加上账号密码。

### Q: AgentRouter 账号密码登录失败怎么办

1. 确认 `provider` 是 `agentrouter`,并且 `username` / `password` 是平台账号密码,不是 GitHub 账号密码
2. GitHub-hosted runner 容易被 AgentRouter WAF 拦截,因此 GitHub Actions 会直接跳过 AgentRouter,不再自动登录
3. AgentRouter 推荐使用本地定时任务 `scripts/run_agentrouter_local.ps1`
4. 如果必须在云端获取 AgentRouter 余额,建议改用可持久化浏览器 profile 的自托管 runner

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
- anyrouter 等支持 `/api/user/sign_in` 的标准 NewAPI 系平台 → httpx + WAF cookie 复用 + username/password 自登录
- agentrouter → Playwright 浏览器流程;username/password 模式每次先退出再重新登录并获取余额,GitHub OAuth 模式仅登录/查余额

## 📜 License

MIT
