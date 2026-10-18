#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用 ConsoleSession 的单元测试。

做什么：用 Juniper-like 和 Cisco-like 两个 profile 验证登录、模式切换、
命令交换、启动探测和文本分析。
为什么：ConsoleSession 必须厂商无关，才能被 Juniper 和未来 Cisco 复用。
"""

import asyncio
import re
import unittest
from collections import deque

from console_session import (
    ConsoleProfile,
    ConsoleSession,
    ModeStep,
    PromptName,
    PromptRule,
    _split_terminal_lines,
)


JUNOS_PROMPTS = (
    PromptRule("config", re.compile(r"(?m)^root@router#[ \t]*$")),
    PromptRule("shell", re.compile(r"(?m)^root@router:~[ \t]*#[ \t]*$")),
    PromptRule("exec", re.compile(r"(?m)^root@router>[ \t]*$")),
    PromptRule("login", re.compile(r"(?im)^(?!Last[ \t]+login:)[^\r\n]*login:[ \t]*$")),
    PromptRule("password", re.compile(r"(?im)^[^\r\n]*password:[ \t]*$")),
)
JUNOS_PROFILE = ConsoleProfile(
    name="juniper-like",
    prompts=JUNOS_PROMPTS,
    login_actions={"login": "root", "password": "admin123"},
    mode_steps=(
        ModeStep("shell", "exec", "cli"),
        ModeStep("exec", "config", "configure"),
    ),
    command_prompt="config",
    line_ending=b"\n",
    boot_prompts=("login",),
    error_patterns=(re.compile(r"(?i)(?:error:|syntax error\b)"),),
    background_patterns=(re.compile(r"(?i)Auto Image Upgrade"),),
    noise_patterns=(re.compile(r"(?m)^\[edit[^\]]*\]\s*$"),),
)

CISCO_PROMPTS = (
    PromptRule("config", re.compile(r"(?m)^RP/\S+\(config[^)]*\)#[ \t]*$")),
    PromptRule("exec", re.compile(r"(?m)^RP/\S+#[ \t]*$")),
    PromptRule("root_username", re.compile(r"(?im)^Enter root-system username:[ \t]*$")),
    PromptRule("secret", re.compile(r"(?im)^Enter secret(?: again)?:[ \t]*$")),
    PromptRule("username", re.compile(r"(?im)^Username:[ \t]*$")),
    PromptRule("password", re.compile(r"(?im)^Password:[ \t]*$")),
    PromptRule("confirm", re.compile(r"(?im)^Do you wish to proceed.*\?[ \t]*$")),
)
CISCO_PROFILE = ConsoleProfile(
    name="cisco-like",
    prompts=CISCO_PROMPTS,
    login_actions={
        "root_username": "root",
        "secret": "secretpw",
        "username": "root",
        "password": "secretpw",
    },
    mode_steps=(ModeStep("exec", "config", "config"),),
    command_prompt="config",
    line_ending=b"\r\n",
    error_patterns=(
        re.compile(r"% (?:Invalid input|Incomplete command|Ambiguous command)"),
        re.compile(r"Failed to commit"),
    ),
)


class ScriptedTelnet:
    """最小脚本化 Telnet 客户端。

    做什么：按写入命令返回预置输出，并记录所有写入。
    为什么：测试不需要真实网络，也能精确控制拆包和时序。
    """

    def __init__(self, chunks=None, on_write=None):
        self.chunks = deque(chunks or [])
        self.writes = []
        self.line_endings = []
        self.connected = True
        self.on_write = on_write

    def enqueue(self, text, size=2):
        if not text:
            return
        for index in range(0, len(text), size):
            self.chunks.append(text[index:index + size])

    async def read_available(self):
        await asyncio.sleep(0)
        if not self.connected:
            raise ConnectionError("scripted console disconnected")
        return self.chunks.popleft() if self.chunks else ""

    async def write_line(self, content, line_ending=None):
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        self.writes.append(content)
        self.line_endings.append(line_ending or b"\n")
        if self.on_write is not None:
            self.on_write(self, content.strip())

    async def telnet_write(self, content, sleep_time=0):
        await self.write_line(content, b"\n")

    async def telnet_line_break(self):
        self.writes.append("\n")

    async def reconnect_async(self):
        self.connected = True
        self.chunks.clear()
        self.enqueue("router login: ")

    @property
    def is_connected(self):
        return self.connected


def make_session(fake, profile):
    """创建关闭等待节拍的 ConsoleSession。"""
    session = ConsoleSession(fake, profile)
    session.READ_INTERVAL = 0.001
    session.PROMPT_SETTLE = 0.005
    session.DRAIN_QUIET = 0.003
    session.DRAIN_MAX = 0.02
    session.BOOT_READY_SETTLE = 0.005
    session.BOOT_NUDGE_INTERVAL = 0.05
    return session


def boot_device(fake, content):
    """模拟登录提示符对空行的响应。"""
    if content == "":
        fake.enqueue("router login: ")


def junos_device(fake, command):
    """模拟 Juniper 登录和命令响应。"""
    if command == "root":
        fake.enqueue("Password: ")
    elif command == "admin123":
        fake.enqueue("Last login: from 10.0.0.1\r\nroot@router:~ # ")
    elif command == "cli":
        fake.enqueue("root@router> ")
    elif command == "configure":
        fake.enqueue("[edit]\r\nroot@router# ")
    elif command == "show login":
        fake.enqueue("show login\r\nrouter login: ")
    elif command == "commit":
        fake.enqueue("commit\r\ncommit comp")
        fake.enqueue("lete\r\nroot@router# ")
    elif command == "warning":
        fake.enqueue("warning\r\nwarning: harmless\r\nroot@router# ")
    else:
        fake.enqueue(command + "\r\n")
        fake.enqueue("root@router# ")


class ConsoleSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_boot_probes_split_login(self):
        """验证拆包登录提示和空行探测。"""
        fake = ScriptedTelnet(on_write=boot_device)
        fake.enqueue("router login: ", size=2)
        session = make_session(fake, JUNOS_PROFILE)

        output = await session.wait_for_boot(timeout=2)

        self.assertIn("router login:", output)
        self.assertEqual(fake.writes, [""])

    async def test_wait_for_boot_requires_probe_response(self):
        """验证只有提示符、没有探测响应时不会假就绪。"""
        fake = ScriptedTelnet()
        fake.enqueue("router login: ")
        session = make_session(fake, JUNOS_PROFILE)

        with self.assertRaises(TimeoutError):
            await session.wait_for_boot(timeout=0.05)

        self.assertIn("", fake.writes)

    async def test_wait_for_boot_ignores_non_boot_prompt(self):
        """验证非 login 提示符不能结束启动等待。"""
        fake = ScriptedTelnet()
        fake.enqueue("root@router> ")
        session = make_session(fake, JUNOS_PROFILE)

        with self.assertRaises(TimeoutError):
            await session.wait_for_boot(timeout=0.05)

    async def test_enter_command_prompt_from_login(self):
        """验证 login→password→shell→exec→config 状态迁移。"""
        fake = ScriptedTelnet(on_write=junos_device)
        session = make_session(fake, JUNOS_PROFILE)

        await session.enter_command_prompt("router login: ", timeout=2)

        self.assertEqual(fake.writes, ["root", "admin123", "cli", "configure"])
        self.assertEqual(set(fake.line_endings), {b"\n"})

    async def test_exchange_returns_prompt_without_extra_write(self):
        """验证命令交换只写一次且能识别提示符。"""
        fake = ScriptedTelnet(on_write=junos_device)
        session = make_session(fake, JUNOS_PROFILE)

        result = await session.exchange("top", timeout=1)

        self.assertTrue(result.sent)
        self.assertTrue(result.prompt_returned)
        self.assertEqual(result.prompt_name, "config")
        self.assertEqual(fake.writes, ["top"])

    async def test_exchange_detects_login_prompt(self):
        """验证命令执行中回到登录提示时要求恢复会话。"""
        fake = ScriptedTelnet(on_write=junos_device)
        session = make_session(fake, JUNOS_PROFILE)

        result = await session.exchange("show login", timeout=1)

        self.assertTrue(result.restore_required)
        self.assertEqual(result.prompt_name, "login")
        self.assertFalse(result.prompt_returned)

    async def test_exchange_success_marker_split_across_packets(self):
        """验证成功标记跨包时仍能被识别。"""
        fake = ScriptedTelnet(on_write=junos_device)
        session = make_session(fake, JUNOS_PROFILE)

        result = await session.exchange(
            "commit",
            timeout=1,
            success_pattern=re.compile(r"(?i)^commit complete$", re.MULTILINE),
        )

        self.assertTrue(result.prompt_returned)
        self.assertTrue(result.success_seen)

    async def test_ensure_ready_restores_from_shell(self):
        """验证残留 shell 提示会触发模式恢复。"""
        fake = ScriptedTelnet(["root@router:~ # "], on_write=junos_device)
        session = make_session(fake, JUNOS_PROFILE)

        await session.ensure_ready()

        self.assertEqual(fake.writes, ["cli", "configure"])

    async def test_restore_reconnects_when_disconnected(self):
        """验证断线后恢复会先重连再登录。"""
        fake = ScriptedTelnet(on_write=junos_device)
        fake.connected = False
        session = make_session(fake, JUNOS_PROFILE)

        await session.restore()

        self.assertTrue(fake.connected)
        self.assertEqual(fake.writes, ["root", "admin123", "cli", "configure"])

    async def test_analysis_and_error_filtering(self):
        """验证 echo、提示符、后台日志和 warning 过滤。"""
        fake = ScriptedTelnet(on_write=junos_device)
        session = make_session(fake, JUNOS_PROFILE)
        output = (
            "top\r\nsome output\r\n"
            "Auto Image Upgrade: error text\r\n"
            "root@router# "
        )

        analysis = session.analysis_text(output, "top")

        self.assertEqual(analysis, "some output")
        self.assertEqual(
            session.find_explicit_error("warning: ignore\r\nerror: real failure"),
            "error: real failure",
        )
        self.assertIsNone(
            session.find_explicit_error(
                "error: configuration database locked",
                ignore_pattern=re.compile(r"configuration database locked"),
            )
        )

    async def test_analysis_text_handles_cr_only_output(self):
        """验证只有 CR 的输出也能正常分析和删除 echo。"""
        fake = ScriptedTelnet()
        session = make_session(fake, JUNOS_PROFILE)
        output = "top\rsome output\rroot@router# "

        analysis = session.analysis_text(output, "top")

        self.assertEqual(analysis, "some output")

    async def test_prompt_name_is_stable_string_enum(self):
        """验证提示符枚举仍与旧字符串完全兼容。"""
        self.assertEqual(PromptName.CONFIG, "config")
        self.assertEqual(str(PromptName.CONFIG), "config")
        self.assertEqual(str(PromptName.CONFIRM), "confirm")

    async def test_find_explicit_error_accepts_stage_patterns(self):
        """验证调用方可以只使用当前阶段的错误模式。"""
        session = make_session(ScriptedTelnet(), JUNOS_PROFILE)
        custom = (re.compile(r"(?i)stage failure"),)

        self.assertEqual(
            session.find_explicit_error("prefix\nstage failure\n", patterns=custom),
            "stage failure",
        )
        self.assertIsNone(
            session.find_explicit_error("stage failure", patterns=())
        )

    async def test_find_explicit_error_handles_cr_only_text(self):
        """验证只有 CR 的文本仍能识别明确错误。"""
        fake = ScriptedTelnet()
        session = make_session(fake, JUNOS_PROFILE)

        analysis = session.analysis_text(
            "warning: ignore\rerror: real failure", "show test"
        )
        error = session.find_explicit_error(analysis)

        self.assertEqual(error, "error: real failure")

    async def test_analysis_text_only_removes_complete_edit_context(self):
        """验证只过滤完整的 [edit ...] 层级行。"""
        fake = ScriptedTelnet()
        session = make_session(fake, JUNOS_PROFILE)
        output = (
            "top\r\n"
            "[edit]\r\n"
            "some output\r\n"
            "[edit interfaces]\r\n"
            "[edit] ordinary note\r\n"
            "[legacy] note\r\n"
            "root@router# "
        )

        analysis = session.analysis_text(output, "top")

        self.assertEqual(
            analysis,
            "some output\n[edit] ordinary note\n[legacy] note",
        )

    async def test_analysis_text_removes_prompt_command_echo_only(self):
        """验证提示符和命令同一行时只删除 echo。"""
        fake = ScriptedTelnet()
        session = make_session(fake, JUNOS_PROFILE)
        output = "root@router# top\r\nsome output\r\nroot@router# "

        analysis = session.analysis_text(output, "top")

        self.assertEqual(analysis, "some output")

    async def test_analysis_text_preserves_later_command_text(self):
        """验证只删除第一条 echo，后续相同文本保留。"""
        fake = ScriptedTelnet()
        session = make_session(fake, JUNOS_PROFILE)
        output = "top\r\noutput A\r\ntop\r\noutput B\r\nroot@router# "

        analysis = session.analysis_text(output, "top")

        self.assertEqual(analysis, "output A\ntop\noutput B")

    async def test_analysis_text_preserves_output_when_command_not_found(self):
        """验证命令不在回显中时不做整段截断。"""
        fake = ScriptedTelnet()
        session = make_session(fake, JUNOS_PROFILE)
        output = "some output\r\nroot@router# "

        analysis = session.analysis_text(output, "top")

        self.assertEqual(analysis, "some output")

    async def test_last_meaningful_line_ignores_large_auto_image_flood(self):
        """验证 login 后大量 Auto Image 不会把提示符挤出窗口。"""
        fake = ScriptedTelnet()
        session = make_session(fake, JUNOS_PROFILE)
        flood = "Auto Image Upgrade: checking image, continuing\r\n" * 200
        text = "router login: \r\n" + flood

        last_line = session._last_meaningful_line(text)

        self.assertEqual(last_line, "router login:")

    async def test_prompt_kind_ignores_large_auto_image_flood(self):
        """验证命令提示符后大量 Auto Image 仍能识别 prompt。"""
        fake = ScriptedTelnet()
        session = make_session(fake, JUNOS_PROFILE)
        flood = "Auto Image Upgrade: checking image, continuing\r\n" * 200
        text = "root@router# \r\n" + flood

        prompt = session._prompt_kind(text)

        self.assertEqual(prompt, "config")

    async def test_split_terminal_lines_handles_mixed_endings(self):
        """验证 CRLF、LF、CR 都按行拆分。"""
        lines, pending = _split_terminal_lines("a\r\nb\nc\rd")

        self.assertEqual(lines, ["a", "b", "c"])
        self.assertEqual(pending, "d")

    async def test_split_terminal_lines_waits_for_split_crlf(self):
        """验证拆开的 CR/LF 不会被当成两个换行。"""
        lines, pending = _split_terminal_lines("a\r")

        self.assertEqual(lines, [])
        self.assertEqual(pending, "a\r")

        lines, pending = _split_terminal_lines(pending + "\nb")

        self.assertEqual(lines, ["a"])
        self.assertEqual(pending, "b")

    async def test_split_terminal_lines_keeps_incomplete_line(self):
        """验证没有换行的尾部作为 pending 保留。"""
        lines, pending = _split_terminal_lines("router log")

        self.assertEqual(lines, [])
        self.assertEqual(pending, "router log")

    async def test_wait_for_boot_handles_cr_only_login(self):
        """验证只有 CR 的 login 也能完成启动探测。"""
        fake = ScriptedTelnet(on_write=boot_device)
        fake.enqueue("router login:\r")
        session = make_session(fake, JUNOS_PROFILE)

        output = await session.wait_for_boot(timeout=2)

        self.assertIn("router login:", output)

    async def test_wait_for_boot_handles_crlf_login(self):
        """验证 CRLF login 也能完成启动探测。"""
        fake = ScriptedTelnet(on_write=boot_device)
        fake.enqueue("router login:\r\n")
        session = make_session(fake, JUNOS_PROFILE)

        output = await session.wait_for_boot(timeout=2)

        self.assertIn("router login:", output)

    async def test_wait_for_boot_with_auto_image_flood(self):
        """验证持续 Auto Image 输出下仍能完成启动探测。"""
        fake = ScriptedTelnet(on_write=boot_device)
        fake.enqueue("router login: ")
        flood = "Auto Image Upgrade: checking image, continuing\r\n" * 100
        fake.enqueue(flood, size=200)
        session = make_session(fake, JUNOS_PROFILE)

        output = await session.wait_for_boot(timeout=2)

        self.assertIn("router login:", output)

    async def test_analysis_text_does_not_strip_normal_line_ending_with_command(self):
        """验证普通输出行末尾出现命令文本时不被误删。"""
        fake = ScriptedTelnet()
        session = make_session(fake, JUNOS_PROFILE)
        output = "prefix top\r\nroot@router# "

        analysis = session.analysis_text(output, "top")

        self.assertEqual(analysis, "prefix top")

    async def test_cisco_like_profile_login_mode_and_confirm(self):
        """验证 Cisco-like profile 可复用登录、模式切换和确认提示。"""
        state = {"secret_count": 0}

        def cisco_device(fake, command):
            if command == "root":
                fake.enqueue("Enter secret: ")
            elif command == "secretpw":
                state["secret_count"] += 1
                if state["secret_count"] == 1:
                    fake.enqueue("Enter secret again: ")
                else:
                    fake.enqueue("RP/0/RP0/CPU0:router# ")
            elif command == "config":
                fake.enqueue("RP/0/RP0/CPU0:router(config)# ")
            elif command == "commit replace":
                fake.enqueue("Do you wish to proceed with this commit anyway? ")
            elif command == "yes":
                fake.enqueue("RP/0/RP0/CPU0:router(config)# ")
            else:
                fake.enqueue("RP/0/RP0/CPU0:router(config)# ")

        fake = ScriptedTelnet(on_write=cisco_device)
        session = make_session(fake, CISCO_PROFILE)

        await session.enter_command_prompt("Enter root-system username: ", timeout=2)
        self.assertIn("config", fake.writes)
        self.assertIn(b"\r\n", fake.line_endings)

        confirm = await session.exchange(
            "commit replace", timeout=1, prompt_names={"confirm"}
        )
        self.assertTrue(confirm.prompt_returned)
        self.assertEqual(confirm.prompt_name, "confirm")

        done = await session.exchange("yes", timeout=1)
        self.assertTrue(done.prompt_returned)
        self.assertEqual(done.prompt_name, "config")
        self.assertIsNotNone(session.find_explicit_error("% Invalid input"))


if __name__ == "__main__":
    unittest.main()
