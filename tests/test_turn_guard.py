#!/usr/bin/env python3
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

# 定位包含 turn_guard.py 的目录：本仓库布局是 tests/ 与 turn_guard.py 同级的父目录，
# 用测试文件自身位置推导，不依赖固定层级数字（原 automations 仓库层级不同，parents[N] 会断）。
TURN_GUARD_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TURN_GUARD_DIR))

import turn_guard  # noqa: E402


class TestHuishenActionRequested(unittest.TestCase):
    """纯函数：只传字符串，不触碰任何文件/环境。"""

    CASES = [
        ("请发起跨审", True),
        ("走一轮跨审", True),
        ("好的，你写一份新的方案，然后去走会审。", True),
        ("我知道如何发起会审现在发起会审", True),
        ("举例发起会审", True),
        ("怎么启动会审？", True),
        ("不要内审，发起跨审", True),
        ("不要跨审，走内审", True),
        ("不要内审、发起跨审", True),
        ("不要内审：发起跨审", True),
        ("能给这个方案做个会审吗", True),
        ("给我做个会审流程说明", True),  # 已知接受：不排除教程/说明语境
        ("有没有发起会审", False),
        ("是否已经发起跨审", False),
        ("请确认是否启动会审", False),
        ("确认后发起会审", True),
        ("给我讲讲会审是什么意思", False),
        ("给我解释一下会审", False),
        ("给我看会审结果", False),
        ("拿会审记录给我看", False),
        ("把会审结果给我", False),
        ("这个方案有走过双审吗", False),
        ("他已经走完会审了吗", False),
        ("走会审了吗？", False),
        ("安排一次会审了吗？", False),
        ("不用会审", False),
        ("不用发起会审", False),
        ("不要针对这份方案安排一次跨审", False),
        ("发起会审确保项目成功", True),
        (str({"instruction": "发起会审"}), True),  # 预先转换后的字符串
        ("", False),
        ("别忘了发起会审", True),  # "别忘了"不是否定
        ("不要内审然后发起跨审", True),  # 无标点模式切换，局部片段判断
        ("是否需要修改然后发起会审", True),
        ("别走跨审改为安排一次内审", True),
        ("请安排会审", True),  # 裸"安排"，无量词
        ("帮我做会审", True),  # 裸"做"，无量词
        ("不要再发起会审", False),
        ("不再走跨审", False),
        ("不要直接发起会审", False),
        ("别先安排一次内审", False),
        ("别给这个方案做个会审", False),
        ("不要针对这份方案做个跨审", False),
        ("走会审进展如何？", False),
        ("发起跨审到哪一步了？", False),
    ]

    def test_cases(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(turn_guard.huishen_action_requested(text), expected)


class TestCmdInjectIsolated(unittest.TestCase):
    """集成测试：隔离真实状态目录和环境变量；依赖可写的系统临时目录。"""

    def _run_inject(self, prompt, session_id="sess-1"):
        payload = {"session_id": session_id, "prompt": prompt}
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = turn_guard.cmd_inject()
        return rc, buf.getvalue()

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmpdir.name)
        self._state_patch = patch.object(turn_guard, "STATE_DIR", self.state_dir)
        self._state_patch.start()
        self._env_patch = patch.dict("os.environ", {}, clear=True)
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._state_patch.stop()
        self._tmpdir.cleanup()

    def test_only_huishen_hit(self):
        _, out = self._run_inject("请发起跨审")
        self.assertIn("会审提醒", out)
        self.assertNotIn("L2 分类器提醒", out)

    def test_only_l2_pending(self):
        pending = self.state_dir / "pending-remind-sess-1.txt"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        pending.write_text("L2 测试内容", encoding="utf-8")
        _, out = self._run_inject("普通问题，不涉及会审")
        self.assertNotIn("会审提醒", out)
        self.assertIn("L2 分类器提醒", out)
        self.assertFalse(pending.exists())

    def test_neither(self):
        _, out = self._run_inject("普通问题，不涉及会审")
        self.assertEqual(out, "")

    def test_both_present(self):
        pending = self.state_dir / "pending-remind-sess-1.txt"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        pending.write_text("L2 测试内容", encoding="utf-8")
        _, out = self._run_inject("请发起跨审")
        self.assertIn("会审提醒", out)
        self.assertIn("L2 分类器提醒", out)
        self.assertFalse(pending.exists())

    def test_turn_guard_skip(self):
        pending = self.state_dir / "pending-remind-sess-1.txt"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        pending.write_text("L2 测试内容", encoding="utf-8")
        with patch.dict("os.environ", {"TURN_GUARD_SKIP": "1"}):
            _, out = self._run_inject("请发起跨审")
        self.assertEqual(out, "")
        self.assertTrue(pending.exists())

    def test_disabled_file(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "DISABLED").write_text("", encoding="utf-8")
        pending = self.state_dir / "pending-remind-sess-1.txt"
        pending.write_text("L2 测试内容", encoding="utf-8")
        _, out = self._run_inject("请发起跨审")
        self.assertEqual(out, "")
        self.assertTrue(pending.exists())

    def test_container_prompt_triggers(self):
        _, out = self._run_inject({"instruction": "发起会审"})
        self.assertIn("会审提醒", out)

    def test_none_prompt_no_trigger(self):
        _, out = self._run_inject(None)
        self.assertNotIn("会审提醒", out)

    def test_empty_list_prompt_no_trigger(self):
        _, out = self._run_inject([])
        self.assertNotIn("会审提醒", out)

    def test_empty_dict_prompt_no_trigger(self):
        _, out = self._run_inject({})
        self.assertNotIn("会审提醒", out)

    def test_pending_oserror_does_not_block_huishen_reminder(self):
        pending = self.state_dir / "pending-remind-sess-1.txt"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        pending.write_text("L2 测试内容", encoding="utf-8")
        real_read_text = Path.read_text

        def _fail_only_pending(self_path, *a, **kw):
            if self_path == pending:
                raise OSError("boom")
            return real_read_text(self_path, *a, **kw)

        with patch.object(Path, "read_text", _fail_only_pending):
            rc, out = self._run_inject("请发起跨审")
        self.assertEqual(rc, 0)
        self.assertIn("会审提醒", out)
        self.assertNotIn("L2 分类器提醒", out)
        self.assertTrue(pending.exists())  # 读取失败未被消费

    def test_huishen_exception_does_not_block_pending(self):
        pending = self.state_dir / "pending-remind-sess-1.txt"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        pending.write_text("L2 测试内容", encoding="utf-8")
        with patch.object(turn_guard, "huishen_action_requested", side_effect=RuntimeError("boom")):
            rc, out = self._run_inject("普通问题，不涉及会审")
        self.assertEqual(rc, 0)
        self.assertNotIn("会审提醒", out)
        self.assertIn("L2 分类器提醒", out)
        self.assertFalse(pending.exists())


if __name__ == "__main__":
    unittest.main()
