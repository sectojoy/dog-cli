# dog-cli 中文说明

`dog` 是 Claude Code、OpenAI Codex 和 Opencode 的稳定包装层。

它运行在原始 CLI 前面，监听常见的可恢复错误，并自动发送合适的后续输入，尽量让会话继续执行。

[Back to English README](./README.md)

## 安装

### 1. 克隆仓库

```bash
git clone <your-repo-url>
cd dog-cli
```

### 2. 本地安装

```bash
./install.sh
```

这会：

- 创建 `.venv`
- 以 editable 模式安装 `dog`
- 输出本地可执行文件路径

手动安装方式：

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/dog --version
```

### 3. 全局可用

方式一：把本地 venv 加到 PATH

```bash
echo 'export PATH="$(pwd)/.venv/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

方式二：创建全局软链接

```bash
sudo ln -sf "$(pwd)/.venv/bin/dog" /usr/local/bin/dog
```

验证：

```bash
dog --version
dog --help
```

## 快速开始

先用最短命令启动：

```bash
dog claude
dog codex
dog opencode
```

如果原始 CLI 支持无参数进入交互模式，`dog` 会直接接管该会话，并在后台处理自动恢复。

## 常见用法

确认基础流程没问题后，再像平时一样透传原始 CLI 参数。

### Claude Code

```bash
dog claude --model claude-opus-4-5 --prompt "fix the flaky tests"
dog claude -r 20 -t 60 --dangerously-skip-permissions
```

### OpenAI Codex

```bash
dog codex --full-auto "write unit tests for utils.py"
dog codex -r 5 -t 60 --model o4-mini "refactor auth module"
```

### Opencode

```bash
dog opencode run "write unit tests for utils.py"
dog opencode run --continue --model openai/gpt-5 "fix flaky tests"
```

### 包装任意命令

```bash
dog run npx claude-code --model opus
dog run uv run my-agent --profile prod
```

## `dog` 会做什么

- 支持 `claude`、`codex`、`opencode`，也支持任意命令行工具
- 识别常见可恢复错误，例如 SSL、网络错误、超时、rate limit
- 自动发送 `retry`、`continue`、`y` 或回车
- 使用同一套恢复主流程，但会按不同 CLI 做输入框 ready 判定，而不是假设所有终端界面都同样重绘
- 遇到 fatal 条件时立即停止，避免无意义循环
- 除非 `dog` 自己中止，否则保留子进程原始退出码

## 高级用法

### 常用选项

| 选项 | 默认值 | 说明 |
|---|---:|---|
| `-r, --max-retries` | `360` | 最大自动重试次数，超出后以退出码 `3` 结束 |
| `-t, --timeout` | `30.0` | 传给 `pexpect` 的启动超时 |
| `--no-echo` | off | 隐藏子进程输出，但仍继续匹配规则 |
| `--retry-on PATTERN` | none | 额外添加一个或多个正则触发条件 |
| `--retry-cmd TEXT` | `continue` | 自定义规则命中时发送的命令 |
| `--no-auto-permission` | off | 关闭自动权限确认 |

### 自定义重试规则

```bash
dog codex --retry-on "stream disconnected" --retry-cmd continue
dog run --retry-on "Gateway Timeout" --retry-cmd $'\n' -- my-ai-tool --interactive
```

### 透传规则

- `dog claude ...` 会把后续参数透传给 `claude`
- `dog codex ...` 会把后续参数透传给 `codex`
- `dog opencode ...` 会把后续参数透传给 `opencode`
- `dog run ...` 可以包装任意命令

## 内置行为

### 重试处理

内置规则覆盖了这类场景：

- 证书和 SSL 失败
- API 连接失败
- 通用网络错误
- 请求超时和网关超时
- rate limit 和 quota 响应
- Codex `APIConnectionError` / `RateLimitError`
- Codex 断流和响应体解码错误
- Claude 的 `(y to retry)`、`Press Enter to continue` 等提示

多数网络类恢复会先等待 `30s`，再发送 `retry` 或 `continue`。
交互确认类提示一般只等待 `0.3s` 到 `1.0s`。

三类 CLI 共享同一个恢复框架，但终端状态判断并不完全相同：

- `claude` 更偏向普通 prompt 交互，很多场景只需要识别明确提示词
- `codex` 和 `opencode` 更接近 TUI，`dog` 会等输入区或高亮动作真正稳定后，再发送 `retry` 或 `continue`
- `dog run ...` 默认走通用路径；如果命令本身能识别成内置 profile，则会复用对应的判定逻辑

规则定义在 [`dog/patterns.py`](/Users/striver/workspace/sectojoy/dog-cli/dog/patterns.py)。

### 完成态处理

`dog` 会识别常见“任务已完成”输出，并在完成后暂停自动恢复。
只有当你真正提交下一条新输入后，自动恢复才会重新开启。

### Fatal 处理

以下情况会立即停止，不会重试：

- `Invalid API key`
- `AuthenticationError`
- `Permission denied`
- billing hard limit failures
- disabled account errors
- maximum context length exceeded

## 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 包装的进程正常退出 |
| `1` | 启动子进程失败 |
| `2` | 命中 `dog` 的 fatal 规则 |
| `3` | 自动重试次数耗尽 |
| 子进程退出码 | 直接透传原始 CLI 的退出码 |

## 开发

运行测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```
