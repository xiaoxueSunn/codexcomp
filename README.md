# codex-516-guard

> Local Responses proxy for OpenAI Codex CLI: detects the gpt-5.5 "516" reasoning-truncation
> fingerprint (`reasoning_tokens == 518*n - 2`), auto-continues the model's thinking, and folds
> all rounds into one response — **without changing `model_provider`**, so session grouping,
> remote compaction and remote-control stay intact. WebSocket-first: no
> "Falling back from WebSockets" retry noise.

自研本地 Responses 代理，缓解 Codex gpt-5.5 的「516 降智」：思考在
`reasoning_tokens == 518*n - 2`（516、1034、1552…）处被截断，答案质量骤降
（上游 issue：[openai/codex#30364](https://github.com/openai/codex/issues/30364)，无官方修复）。
本代理检测该指纹后自动让模型继续思考，并把多轮续写**折叠为单个下游响应**。

机制思路来自 [neteroster/CodexCont](https://github.com/neteroster/CodexCont)（MIT），
实现为全新代码。与其关键差异：

| | codex-516-guard | CodexCont |
| --- | --- | --- |
| Codex 侧接线 | 顶层 `openai_base_url`（**不新建 provider**） | 新建 `[model_providers]`（会话按 provider 分组被隐藏、remote-control 不可用、丢远程压缩） |
| 下游传输 | **WebSocket 第一传输**（完整实现 `responses_websockets` 协议）+ SSE 兜底 | 仅 SSE（codex 先试 ws → 405 → 每会话约 5 次重连告警后回退） |
| zstd 请求压缩（0.142.x 内置 provider 默认开） | 原生解压，无需改 codex 配置 | 需 `[features] enable_request_compression = false` |
| `GET /v1/models` 模型目录刷新 | `/v1/*` 透传 | 未代理（静默失败，靠本地缓存） |
| 续写方法 | commentary 法（`phase:"commentary"` 消息 + encrypted reasoning 重放） | commentary + tool_pair legacy + 跨轮 repair 等更多可配置项 |

## 原理

1. 上游每轮结束时读取 `usage.output_tokens_details.reasoning_tokens`，命中 `518n-2`（n∈[1,6]，最多续写 3 轮）即判定思考被截断；
2. 丢弃该轮的**暂定输出**（message / tool calls——它们基于被截断的思考），把该轮 reasoning items（含 `encrypted_content`）+ 一条 `Continue thinking...` 的 `phase:"commentary"` 助手消息追加进 input 重放，开下一轮；
3. 思考流实时透传给 agent，只有干净收尾那一轮的最终输出被放行；terminal 事件重建为单响应口径的 usage（input 取第 1 轮防止「假爆上下文」，reasoning 求和），真实累计成本记在 `metadata.proxy_billed_usage`。

## 安装

要求：Python ≥ 3.12、[uv](https://docs.astral.sh/uv/)、Codex CLI（ChatGPT OAuth 登录，0.142.x 实测）。

```bash
git clone https://github.com/dzshzx/codex-516-guard
cd codex-516-guard
uv sync
uv run python test_fold.py        # 状态机自测，应输出 ALL PASS
uv run python run.py              # 前台跑起（127.0.0.1:8787）
```

> 建议把克隆仓库与运行目录分开，运行目录只放运行必需件：`guard/`、`run.py`、
> `pyproject.toml`、`uv.lock`（在其中执行 `uv sync` 生成 `.venv`）。例如：
> `rsync -a guard run.py pyproject.toml uv.lock ~/.local/share/codex-516-guard/`。

Codex 侧接线——`~/.codex/config.toml` 顶层（必须在第一个 `[table]` 之前）加一行：

```toml
openai_base_url = "http://127.0.0.1:8787/v1"
```

这是覆盖内置 openai provider base_url 的**官方 config key**
（[#16719](https://github.com/openai/codex/issues/16719)；同名 `[model_providers.openai]`
覆盖被维护者拒绝，`OPENAI_BASE_URL` 环境变量已移除）。provider id 保持 `openai`，
因此会话历史分组、远程压缩、remote-control 均不受影响。

常驻运行（Linux/WSL）：见 `systemd/codex-516-guard.service.example`。

**关闭**：注释掉 `openai_base_url` 行 + 停掉代理进程。代理停止而 key 在位时，Codex 会因上游不可达报错。

## 验证

```bash
curl -sS http://127.0.0.1:8787/healthz            # {"ok":true,...}
journalctl --user -u codex-516-guard -f | grep -E 'round|done'
```

命中折叠时的日志（实测样例，连环双 516 被击破、答案正确）：

```
round 1: in=21550 out=664 reason=516 total=22214 | n=1 buffered=['function_call'] -> continue
round 2: in=22078 out=652 reason=516 total=22730 | n=1 buffered=['function_call'] -> continue
round 3: in=22606 out=566 reason=291 total=23172 | n=None buffered=[...] -> clean
done: 3 round(s) | ... | status=completed stop=natural
```

## 结构

- `guard/fold.py` — 指纹检测 + 折叠状态机（传输无关；`test_fold.py` 覆盖丢弃/放行、重编号、双口径 usage）
- `guard/server.py` — starlette 传输层：ws / SSE 下游、SSE 上游、zstd/gzip 请求解压、`/v1/*` 透传
- `run.py` — uvicorn 入口（仅监听 127.0.0.1；auth passthrough，不存储任何凭据）

## 安全与免责

- 代理只做 auth **passthrough**：转发 Codex 发来的 Authorization 头，不读取、不落盘任何凭据。
- 仅监听回环地址；不要暴露到非回环接口。
- 非官方项目，依赖上游未公开的行为（截断指纹、ws 帧格式），OpenAI 侧变更可能使其失效；使用风险自负。
- 续写会产生额外的真实 token 消耗（见 `metadata.proxy_billed_usage`），guard 以 n 窗口 + 3 轮上限约束。

## License

MIT（见 LICENSE；机制思路 credit neteroster/CodexCont）。
