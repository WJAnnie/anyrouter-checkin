# AnyRouter 自动签到

自动签到脚本，支持 AnyRouter、AgentRouter 等基于 NewAPI/OneAPI 的平台。

## 使用方法

### 1. Fork 本仓库

### 2. 获取登录凭证

1. 登录 https://anyrouter.top/console
2. F12 打开开发者工具
3. Application → Cookies → 复制 `session` 的值
4. Network → 任意请求 → Headers → 复制 `New-Api-User` 的值

### 3. 配置 GitHub Secrets

1. Settings → Environments → New environment → 命名为 `production`
2. 添加 Secret: `ANYROUTER_ACCOUNTS`

```json
[
  {
    "name": "我的账号",
    "cookies": {"session": "你的session值"},
    "api_user": "你的API用户ID"
  }
]
```

### 4. 启用 Actions

进入 Actions 标签，启用工作流。

## 运行频率

- 每 6 小时自动执行
- 支持手动触发

## 签到奖励

每日签到可获得 $25 额度。
