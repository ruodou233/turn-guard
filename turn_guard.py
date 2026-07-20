#!/usr/bin/env python3
"""turn-guard — Claude Code 回合级流程守护（2026-07-04 流程守护方案 §2.4/§4，L1+L2 入口）。

用法（注册在 ~/.claude/settings.json hooks）：
  turn_guard.py stop    Stop hook：
    L1 确定性检测（同步、毫秒级）：
      (a) 有未决后台工作且未布唤醒 → 提示"仅预计等待超 1h 才需续命拍"（v5，订阅主会话 1h TTL）
      (b) [v5 已撤销] 场景B提醒——订阅主会话 1h TTL，等用户回复无需保温
    L2 语义分类（可选、异步、fire-and-forget）：默认关闭；仅当
      TURN_GUARD_L2_MODE=claude 时拉起 turn_classifier.py 调 Claude 判断"是否方案该送审/
      是否宣称完成执行该走实施后审查"，结果写 state，下一回合 inject 注入。
  turn_guard.py inject  UserPromptSubmit hook：注入 L2 pending 提醒（一次性，注入即删）；
    另检测本次提交的 prompt 是否含会审执行请求，命中即打印 advisory 提醒（2026-07-21）。

安全约定：全程 fail-open（任何异常 exit 0 不阻塞回合）；stop_hook_active=true 时不再注入
（防循环）；同 session 同 topic 只提醒一次（去重文件，适用于 L1 保温与 L2 分类器提醒；
会审提醒不去重，命中即提示，跨回合重复提及会重复提醒，这是有意行为而非漏改）。

作者环境耦合说明（轻量发布，未做通用化改写）：本文件里的提醒文案硬编码了作者本机的
具体约定，照抄不会在你的环境里直接生效，按需改成你自己的等价物——
  - "cache-keepalive v6"、"skills/cross-review/SKILL.md" 等提醒文案：指向作者自己安装的
    另外两个 skill，你没装这些 skill 时这些文案没有意义，删掉或替换成你自己的流程提醒。
  - AGENTOPS 默认路径：指向作者本机的仓库位置，设置 AGENTOPS 环境变量即可覆盖，不是写死的。
  - "agent-memory 双审规则"（见 turn_classifier.py）：指向作者的私有长期指令文档，你没有
    同名文件时这句提醒同样没有意义。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

AGENTOPS = Path(os.environ.get("AGENTOPS", str(Path.home() / "Library/Application Support/AgentOpsRuntime/repo")))
STATE_DIR = AGENTOPS / "automations" / "state" / "turn-guard"
AUDIT_LOG = AGENTOPS / "automations" / "state" / "turn-audit.jsonl"
CLASSIFIER = AGENTOPS / "automations" / "turn-guard" / "turn_classifier.py"
CTX_THRESHOLD = 30_000  # [v5 弃用] 原场景 B 门槛；保留常量供 5m TTL 环境复用

HUISHEN_TERMS = ("会审", "跨审", "内审", "双审", "cross-review", "cross review")
_TERM_PATTERN = "|".join(re.escape(t) for t in HUISHEN_TERMS)
_CLAUSE_SPLIT_RE = re.compile(r"[。！？\n；;，,、：:]")
_LOCAL_SPLIT_RE = re.compile(r"(?:然后|而是|改为)")
_HUISHEN_ACTION_RE = re.compile(
    rf"(走|发起|启动|送审|来一轮|做|安排)(?:个|一次)?(?!过|了|完).{{0,6}}({_TERM_PATTERN})")
_HUISHEN_NEGATION_RE = re.compile(r"(?:不用|不要|别(?!忘)|无需|不需要|不再)")
_HUISHEN_PREFIX_QUERY_RE = re.compile(r"(有没有|是否)")
_HUISHEN_QUERY_WORD_RE = re.compile(r"(完成|办完|做完|走完|成功|结束|进展|状态|到哪)")
_HUISHEN_QUESTION_MARK_RE = re.compile(r"(吗|没|如何|怎样|哪|什么)")
_HUISHEN_QUERY_END_RE = re.compile(r"(了吗|了没|过吗|没有)$")


def read_stdin_json() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def last_assistant_usage_tokens(transcript_path: str) -> int:
    """transcript JSONL 里最后一条带 usage 的 assistant 消息的前缀规模估算。"""
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return 0
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        msg = obj.get("message") or {}
        usage = msg.get("usage") or {}
        if msg.get("role") == "assistant" and usage:
            return int(usage.get("input_tokens", 0) or 0) \
                 + int(usage.get("cache_read_input_tokens", 0) or 0) \
                 + int(usage.get("cache_creation_input_tokens", 0) or 0)
    return 0


_SAFE_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")


def sanitize_session_id(raw: str) -> str:
    """session_id 会被直接拼进状态文件名（remind-{sid}-{topic}、pending-remind-{sid}.txt）。
    只保留字母数字与 -/_，其余字符（含 / 和 ..）一律替换为 _，防止路径穿越或写到
    STATE_DIR 之外；截断到 64 字符不变。"""
    cleaned = _SAFE_SESSION_ID_RE.sub("_", str(raw))[:64]
    return cleaned or "unknown"


def dedup_hit(session_id: str, topic: str) -> bool:
    """同 session 同 topic 只提醒一次。返回 True=已提醒过。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    marker = STATE_DIR / f"remind-{session_id}-{topic}"
    if marker.exists():
        return True
    marker.write_text("", encoding="utf-8")
    return False


def detect_background_and_wakeup(hook_input: dict) -> tuple[bool, bool]:
    """(有非空拍后台任务, 已布唤醒/空拍)。字段防御式解析，取不到按 (False, False)。"""
    has_bg, has_wakeup = False, False
    tasks = hook_input.get("background_tasks") or hook_input.get("backgroundTasks") or []
    if isinstance(tasks, list):
        for t in tasks:
            cmd = ""
            if isinstance(t, dict):
                cmd = str(t.get("command") or t.get("description") or "")
            elif isinstance(t, str):
                cmd = t
            if "sleep" in cmd.lower():
                has_wakeup = True
            else:
                has_bg = True
    crons = hook_input.get("session_crons") or hook_input.get("sessionCrons") or []
    if isinstance(crons, list) and crons:
        has_wakeup = True
    return has_bg, has_wakeup


def append_audit(record: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def cmd_stop() -> int:
    # 防递归护栏（2026-07-04 实测必要）：本 hook 全局生效，分类器启用时会调 claude -p、
    # 审计/综合的 headless claude 会话结束时同样触发 Stop——不拦会形成分类器自繁殖链。
    # 这些自动化入口都带 TURN_GUARD_SKIP=1；DISABLED 文件是手动熔断开关。
    if os.environ.get("TURN_GUARD_SKIP") == "1" or (STATE_DIR / "DISABLED").exists():
        return 0
    data = read_stdin_json()
    # A/B 期间观测：落盘原始 Stop input（发现清单 #3——验证 background_tasks 等字段名是否真实存在）
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "last-stop-input.json").write_text(
            json.dumps({k: data[k] for k in data if k != "transcript"}, ensure_ascii=False)[:8000],
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if data.get("stop_hook_active"):
        return 0  # 已在 hook 续行中，防循环
    session_id = sanitize_session_id(data.get("session_id", "unknown"))
    transcript = str(data.get("transcript_path", ""))

    # ---- L2：异步语义分类（可选；默认关闭，避免静默消耗 Claude 额度） ----
    try:
        l2_mode = os.environ.get("TURN_GUARD_L2_MODE", "off").strip().lower() or "off"
        if l2_mode == "claude" and CLASSIFIER.exists() and transcript:
            subprocess.Popen(
                [sys.executable, str(CLASSIFIER), transcript, session_id],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        elif l2_mode != "claude" and transcript and not dedup_hit(session_id, f"l2-disabled-{l2_mode}"):
            append_audit({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "session": session_id,
                "source": "turn_guard.stop",
                "mode": "disabled",
                "verdict": None,
                "reason": f"TURN_GUARD_L2_MODE={l2_mode}",
            })
    except Exception:  # noqa: BLE001
        pass

    # ---- L1：确定性保温检测（同步） ----
    reminders = []
    has_bg, has_wakeup = detect_background_and_wakeup(data)
    # v5（2026-07-04）：订阅版 Claude Code 主会话为 1h TTL（官方文档核实）——
    # 场景 B 提醒撤销（等用户回复无需保温）；场景 A 仅"预计等待超 1 小时"才值得续命拍。
    if has_bg and not has_wakeup:
        if not dedup_hit(session_id, "keepalive-A"):
            reminders.append(
                "cache-keepalive v6 提示：后台等待超 1 小时才需续命拍（50min 间隔，上限 1-2 拍）；"
                "若任务未闭环且预期用户离场较久，可布 50 分钟节拍改进循环（每拍优先派便宜子代理"
                "小活，上限 4 拍，每拍先校验上一拍缓存命中）；1 小时内的普通等待无需保温。")
    if reminders:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": "[turn-guard·L1] " + " ".join(reminders),
            }
        }, ensure_ascii=False))
    return 0


def _huishen_segment_requested(segment: str) -> bool:
    """按模式切换词缩小否定作用域后的局部片段判断；教程/举例/疑问语境允许
    误报。已知不覆盖：切分词表和否定习语豁免均为有限枚举，不再扩充（会重蹈
    "越补越漏"的覆辙，见 review_id 2026-07-21-huishen-post-impl 处置表）；
    动作词固定要求出现在术语之前、间隔不超过6字。"""
    if _HUISHEN_NEGATION_RE.search(segment):
        return False
    for m in _HUISHEN_ACTION_RE.finditer(segment):
        prefix, suffix = segment[:m.start()], segment[m.end():]
        if _HUISHEN_PREFIX_QUERY_RE.search(prefix):
            continue
        if _HUISHEN_QUERY_WORD_RE.search(suffix) and _HUISHEN_QUESTION_MARK_RE.search(suffix):
            continue
        if _HUISHEN_QUERY_END_RE.search(suffix.strip()):
            continue
        return True
    return False


def huishen_action_requested(prompt_text: str) -> bool:
    for clause in _CLAUSE_SPLIT_RE.split(prompt_text or ""):
        for segment in _LOCAL_SPLIT_RE.split(clause):
            if _huishen_segment_requested(segment):
                return True
    return False


def prune_stale_state(max_age_sec: int = 48 * 3600) -> None:
    """清理 48h 以上的孤儿 pending/去重标记（headless 会话产生的 pending 永远不会被注入）。"""
    import time
    now = time.time()
    try:
        for p in STATE_DIR.glob("*"):
            if p.name == "DISABLED":
                continue
            try:
                if now - p.stat().st_mtime > max_age_sec:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        pass


def cmd_inject() -> int:
    if os.environ.get("TURN_GUARD_SKIP") == "1" or (STATE_DIR / "DISABLED").exists():
        return 0  # 熔断开关全停（审查第4轮：DISABLED 须同时覆盖 inject 才是"touch 即全停"）
    prune_stale_state()
    data = read_stdin_json()
    session_id = sanitize_session_id(data.get("session_id", "unknown"))

    # 先运行会审提醒；异常 fail-open，不影响下方 pending 注入。
    try:
        prompt_text = str(data.get("prompt", "") or "")
        if huishen_action_requested(prompt_text):
            print("[turn-guard·会审提醒] 检测到可能涉及会审的表述——如需正式发起，请按 "
                  "skills/cross-review/SKILL.md 选择入口，不要用临时子代理冒充。")
    except Exception:  # noqa: BLE001
        pass

    pending = STATE_DIR / f"pending-remind-{session_id}.txt"
    if pending.exists():
        try:
            text = pending.read_text(encoding="utf-8").strip()
            pending.unlink(missing_ok=True)
            if text:
                print(f"[turn-guard·L2 分类器提醒] {text}")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    try:
        mode = sys.argv[1] if len(sys.argv) > 1 else "stop"
        sys.exit(cmd_stop() if mode == "stop" else cmd_inject())
    except Exception:  # noqa: BLE001 — fail-open：守护脚本绝不阻塞回合
        sys.exit(0)
