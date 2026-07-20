# turn-guard

Claude Code 回合级流程守护 hook。**L1** 是确定性、低成本的同步提醒（比如后台任务未布"续命拍"就快超时）；**L2** 是可选的、默认关闭的语义分类器（拉起一个模型 CLI 判断"方案是否该送审 / 宣称完成的工作是否该走实施后审查"）。

## 当前状态

- L2 默认关闭（`TURN_GUARD_L2_MODE` 未设置或不为 `claude` 时不会调用任何模型）。
- 作者本机仍在对 L2 做 A/B 实验，判定准确率和误报率还在观察中，**没有跨环境验证过兼容性**——建议先在自己的环境里跑一遍 `tests/`，实际观察一段时间行为再决定是否长期启用 L2。

## 数据外发披露（重要，不是可选项）

当环境变量 `TURN_GUARD_L2_MODE=claude` 时，`turn_guard.py` 会在每个 Stop 事件异步拉起 `turn_classifier.py`，后者会把当前会话最近几轮对话的文本摘录（截取到约 4k tokens）发给你在 `CLAUDE_BIN_CLI`（默认 `~/.local/bin/claude`）配置的模型 CLI 做分类判断。这意味着**你的会话内容会被发送到一个外部模型调用**。如果你不希望发生任何数据外发，保持 `TURN_GUARD_L2_MODE` 不设置（默认关闭）即可，L1 部分不涉及任何模型调用或数据外发。

## 轻量发布声明

这是作者本机可运行的原始实现，没有为通用发布做额外教程化改写。建议你的 Agent 直接阅读源码理解机制，根据你自己的环境和需求判断要保留、简化还是重写。`turn_guard.py` 文件头部的 docstring 里也有一段"作者环境耦合说明"，列出了哪些提醒文案是硬编码在作者本机环境里的（指向作者自己装的其他 skill、私有文档等），这些在你的环境里没有意义，按需删掉或替换成你自己的等价物。

## 文件

- `turn_guard.py` —— 主入口，处理 `stop`（Stop hook）和 `inject`（UserPromptSubmit hook）两个子命令。
- `turn_classifier.py` —— L2 语义分类器，由 `turn_guard.py` 在 `TURN_GUARD_L2_MODE=claude` 时异步拉起，不直接调用。
- `tests/test_turn_guard.py` —— 单元测试，覆盖会审提醒的文本判定逻辑和 `inject` 命令的集成行为。

## 怎么注册

注册进 `~/.claude/settings.json` 的 hooks（Stop 和 UserPromptSubmit 两个事件），具体用法见 `turn_guard.py` 顶部 docstring——里面已经写清楚了两个子命令各自的职责，这里不重复展开。默认状态目录在 `$AGENTOPS/automations/state/turn-guard`（`AGENTOPS` 环境变量可覆盖，默认指向作者本机路径，你需要设置成自己的路径或直接改代码里的默认值）。

## 测试

```bash
cd turn-guard
python3 -m unittest tests.test_turn_guard -v
```

## License

MIT，见 `LICENSE`。
