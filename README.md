# Claude Code 回合守护配方｜Claude Code Turn Guard Recipes

Copy-paste recipes for Claude Code's built-in hooks and `/goal`: remind the agent to review before claiming done, wrap up background tasks, notify on API failures, and keep going until a goal is met.

Agent 一说"完成了"，你还得翻回去看：后台任务有人接着管吗，方案该不该送审，改完有没有再查一遍？Agent 老忘流程，你就得一遍遍催。

turn-guard 管的就是这件事：把回合里容易漏的步骤带回对话。现在它是一组配方，每个都是一段能直接粘进 `settings.json` 的配置，加一两句说明，用的全是 Claude Code 自带的能力。

> **v1 的做法已被平台取代。** v1 是一套自己写的 Python hook（钩子）：`turn_guard.py` 在 Stop 时猜后台任务字段，`turn_classifier.py` 异步拉起 `claude -p` 判断该不该送审，结果写状态文件、下一回合再注入。这三件事 Claude Code 现在都内置了，见文末「从 v1 迁过来」。旧代码在 git 历史的 `v1-legacy` 标签里：`git checkout v1-legacy`。

## 先看你要防哪种漏

| 场景 | 配方 | 平台能力 | 需要的版本 |
|---|---|---|---|
| 宣称做完了，却没交代复查 | [1](#配方-1宣称完成却没交代复查就别收尾) | Stop 上的 `type: "prompt"` hook | 2.1.47+ |
| 后台任务还在跑，Agent 就收尾了 | [2](#配方-2后台任务还在跑先交代清楚再停) | Stop 输入里的 `background_tasks` | 2.1.163+ |
| 回合被限流、502 打断，你不知道 | [3](#配方-3回合被-api-错误打断时弹系统通知) | `StopFailure` 事件 | 2.1.78+ |
| 活没干完就停下来等你 | [4](#配方-4没做完就接着做goal) | `/goal` 命令 | 2.1.139+ |
| 想用 markdown 写提醒规则 | [5](#配方-5想用-markdown-写提醒规则hookify) | 官方 hookify 插件 | — |
| 用户一提送审，就先把流程读进来 | [6](#配方-6用户提到送审先把流程读进来) | UserPromptSubmit 的纯文本输出 | — |

版本号取自 [anthropics/claude-code 的 CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)，字段名按[官方 hooks 文档](https://code.claude.com/docs/en/hooks)核对过。配方 1、2、3 在 Claude Code 2.1.325 上实际跑过，过程写在各节「实测」里。

## 配方 1：宣称完成却没交代复查，就别收尾

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "prompt",
            "prompt": "你在检查 Claude 这一轮能不能就此收尾。输入 JSON：$ARGUMENTS\n\n按顺序判断：\n1. stop_hook_active 为 true：已经提醒过一次，返回 {\"ok\": true}。\n2. last_assistant_message 宣称方案已定稿，或宣称代码、文档、配置改动已经完成，却没交代送审、复查、测试结果或自查结论：返回 {\"ok\": false, \"reason\": \"宣称完成但没交代复查。先复查一遍（重读改动、跑测试，重要方案送审），把结论补进回复再结束。\"}。\n3. 其他情况（闲聊、回答问题、进度汇报、已经交代了复查）：返回 {\"ok\": true}。"
          }
        ]
      }
    ]
  }
}
```

- 每个回合结束时，Claude Code 把这段 prompt 连同本轮的 Stop 输入（含 `last_assistant_message`、`stop_hook_active`）和对话记录交给后台小模型判断。返回 `ok: false` 时，`reason` 会作为下一条指令交回 Claude，它接着把复查做了。
- Stop 上不用写 `continueOnBlock`：按官方文档，这个字段只在 PreToolUse、PostToolUse、TeammateIdle 上起作用；Stop 上 `ok: false` 本来就会接着干。
- 第 1 条规则让同一轮只提醒一次。平台另有兜底：连续拦 8 次就强制结束（`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` 可调）。
- 默认用后台小模型、30 秒超时，`model`、`timeout` 字段可改。判断走的是你当前会话的同一个 provider（模型服务商），每回合多一次小模型调用。
- 拦下来时界面会显示一行 `Stop hook error`，这是拦截生效的正常样子，不是配置坏了。
- 换成你自己的流程：改第 2 条的判断和 `reason` 里的文字。要让它真去读文件、看测试结果，可以把 `type` 换成实验性的 `"agent"`，它能用 Read、Grep 自己查，默认超时 60 秒。

**实测**（2.1.325）：让 Claude 只回一句"已完成全部代码改动，可以合并了"。hook 判 `ok: false` 并把理由交回；Claude 补了一段更正，第二次判断 `ok: true`，回合结束。评估走的是后台小模型 haiku。

## 配方 2：后台任务还在跑，先交代清楚再停

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "jq -c 'if (.stop_hook_active | not) and ((.background_tasks // []) | length > 0) then {hookSpecificOutput: {hookEventName: \"Stop\", additionalContext: (\"还有后台任务没收尾：\" + ([.background_tasks[] | \"\\(.type) \\(.description)\"] | join(\"；\")) + \"。告诉用户它们在跑什么、结果出来后怎么接着处理；用不上了就停掉。\")}} else empty end'"
          }
        ]
      }
    ]
  }
}
```

- Stop 输入里的 `background_tasks` 列出还在跑的后台 shell、子代理、monitor、workflow 等，每项带 `type` 和 `description`。这个 hook 用 `jq` 读它：有任务就返回 `additionalContext`，Claude 会接着说清楚任务在跑什么、结果出来后怎么接；没有任务就什么都不输出。
- 纯本地判断，不调模型，需要装 `jq`。同样靠 `stop_hook_active` 只提醒一次。
- 已经用 `/loop` 或定时唤醒安排了回来查看的，可以在条件里加 `and ((.session_crons // []) | length == 0)`，有定时就不提醒。

**实测**（2.1.325）：让 Claude 后台跑 `sleep 25` 后直接收尾。hook 注入提醒，Claude 补了一句交代；25 秒后任务结束，平台自动把结果作为新一轮送回 Claude。v1 当年要提醒"续命拍"，就是因为没有这一步。

## 配方 3：回合被 API 错误打断时弹系统通知

```json
{
  "hooks": {
    "StopFailure": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title \"Claude Code 回合中断\" sound name \"Basso\"' -e 'end run' \"$(jq -r '.error + \"：\" + (.last_assistant_message // \"\")')\""
          }
        ]
      }
    ]
  }
}
```

- 回合因 API 错误结束时（限流、服务端 5xx、鉴权失败等），Claude Code 不触发 Stop，改为触发 `StopFailure`。它不能拦截，只用来通知和记日志。
- 输入里 `error` 是错误类型（`rate_limit`、`server_error`、`authentication_failed`……认不出的是 `unknown`），`last_assistant_message` 是错误原文。只想管某几类，加 `"matcher": "rate_limit|server_error"`（这个事件的 matcher 只认 `|` 分隔）。
- 上面用的是 macOS 的 `osascript`。Linux 换成 `notify-send`；在终端里用的，也可以让 hook 返回 `terminalSequence` 字段，交给 Claude Code 发终端通知（2.1.141+，只在交互式会话生效，写法见[官方文档](https://code.claude.com/docs/en/hooks#emit-terminal-notifications)）。

**实测**（2.1.325）：经网关请求一个上游不支持的模型，拿到 400。`StopFailure` 触发、Stop 没触发，这时 `error` 是 `unknown`，原文在 `last_assistant_message`。通知命令另外用一条样例输入跑过，执行成功。

## 配方 4：没做完就接着做：`/goal`

不用改配置，在会话里输入：

```text
/goal test/auth 下的测试全部通过、lint 干净，并且没改其他测试文件；跑满 20 轮还没好就停
```

- `/goal` 就是一个只在当前会话生效的 prompt 型 Stop hook：每轮结束，小模型对照条件判断，没达成就自动开下一轮；达成、或判定做不到，就自动清掉。`/goal` 不带参数看进度，`/goal clear` 取消。
- 适合有明确终点、能在对话里证明的活：迁移到每个调用点都编译通过，按验收标准把设计稿实现完，把一批 issue 清空。
- 不适合"每回合都该检查"的常驻规则，比如配方 1 的复查提醒。那种写进 settings，对所有会话生效。
- 评估模型不跑命令也不读文件，只看对话，所以条件要写成 Claude 的输出能证明的："`npm test` 退出码 0"、"`git status` 干净"。想限时就加一句"跑满 N 轮就停"。
- 后台任务还在跑时不评估，等它结束再判；等满 30 分钟会自动提醒 Claude 去看一眼。想无人值守，配合 auto mode（自动批准工具调用）用；`claude -p "/goal …"` 也能跑。

## 配方 5：想用 markdown 写提醒规则：hookify

```text
/plugin install hookify@claude-plugins-official
```

官方 [hookify 插件](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/hookify)：规则写成项目里的 `.claude/hookify.<名字>.local.md`，YAML 头写事件和匹配条件，正文就是提醒文字；改完立即生效，不用重启。`/hookify 描述` 让 Claude 帮你写一条，`/hookify` 不带参数会从最近的对话里找你纠正过的行为。

```markdown
---
name: review-before-stop
enabled: true
event: stop
action: block
conditions:
  - field: transcript
    operator: regex_match
    pattern: 已完成|改完了|可以合并
  - field: transcript
    operator: not_contains
    pattern: 复查
---
这一轮宣称做完了，但整段对话里还没出现过复查。先重读改动、跑一遍测试，把结论写进回复再结束。
```

- 规则按正则匹配，不调模型，需要 `python3`。只读当前项目的 `.claude/`，不读 `~/.claude/`。
- `event: stop` 匹配的 `transcript` 是整个会话文件，不只看这一轮：会话里出现过一次"复查"，之后就不再拦。要"读懂这一轮在干什么"，用配方 1。
- 已知问题（2026-11 仍未修）：`event: prompt` 的规则读的是 `user_prompt` 字段，Claude Code 实际发的是 `prompt`，所以按用户输入匹配的规则不会触发（[#1789](https://github.com/anthropics/claude-plugins-official/issues/1789)、[#2190](https://github.com/anthropics/claude-plugins-official/issues/2190)）。修好之前用配方 6。

上面这条规则用 hookify 自带的 `stop.py` 和两份假对话记录跑过：没出现"复查"时拦下，出现了就放行。

## 配方 6：用户提到送审，先把流程读进来

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "jq -r 'if (.prompt // \"\" | test(\"会审|送审|cross-review\"; \"i\")) then \"用户提到了送审：先读 cross-review 的 SKILL.md，按里面的步骤发起，别凭记忆走。\" else empty end'"
          }
        ]
      }
    ]
  }
}
```

- UserPromptSubmit hook 的纯文本输出会作为系统提醒交给 Claude。这里用 `jq` 匹配关键词，命中就提醒去读对应的流程文档。不调模型。
- 把关键词和提醒文字换成你自己的流程就行。

## 一次装全

[`settings.example.json`](settings.example.json) 是配方 1、2、3、6 合在一起的版本。

- 先试：`claude --settings settings.example.json`，它会和你已有的 hooks 一起生效。
- 长期用：把里面的条目合并进 `~/.claude/settings.json` 的 `hooks`；已有 Stop 等数组时往里追加，别整段覆盖。改完在会话里用 `/hooks` 看有没有加载上。

## 从 v1 迁过来

| v1 | 现在 |
|---|---|
| L1：Stop 时猜 `background_tasks` / `backgroundTasks` 字段，提醒补"续命拍" | 配方 2。字段已经正式提供；后台任务结束时平台会自己把结果送回 Claude，不用再布续命拍 |
| L2：每次 Stop 异步拉起 `claude -p` 分类，写状态文件，下一回合注入 | 配方 1。平台在 Stop 时直接调小模型判断，当场把理由交回 Claude，不落状态文件 |
| 用户提到"会审"时提醒读 cross-review | 配方 6 |
| （v1 没有）回合被 API 错误打断 | 配方 3 |

## License

MIT，见 `LICENSE`。
