#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用 ConsoleSession 的单元测试。

做什么：用 Juniper-like 和 Cisco-like 两个 profile 验证登录、模式切换、
命令交换、静默探测和文本分析。
为什么：ConsoleSession 负责 Juniper 和 Cisco 共用的终端交互。
"""

import asyncio
import re
import unittest
from collections import deque
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from console_session import (
    ConsoleProfile,
    ConsoleSession,
    ModeStep,
    PromptName,
    PromptRule,
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
        ModeStep("shell", "cli"),
        ModeStep("exec", "configure"),
    ),
    command_prompt="config",
    line_ending=b"\n",
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
    mode_steps=(ModeStep("exec", "config"),),
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
    return session


@contextmanager
def exchange_clock(session):
    """用虚拟时间验证探测间隔和总超时，避免测试依赖实际调度。"""
    class Clock:
        now = 0.0

        def time(self):
            return self.now

        async def sleep(self, delay):
            self.now += delay
            await asyncio.sleep(0)

        async def wait_for(self, awaitable, timeout):
            task = asyncio.create_task(awaitable)
            expires = self.now + timeout
            try:
                while not task.done():
                    if self.now >= expires:
                        task.cancel()
                        try:
                            return await task
                        except asyncio.CancelledError:
                            raise TimeoutError from None
                    await asyncio.sleep(0)
                return task.result()
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    clock = Clock()
    session.READ_INTERVAL = 1.0
    clock_api = SimpleNamespace(
        get_running_loop=lambda: clock, sleep=clock.sleep, wait_for=clock.wait_for
    )
    with patch("console_session.asyncio", clock_api):
        yield clock


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
    async def test_expect_returns_boundary_and_caller_consumes_matched_prefix(self):
        prefix = "banner\r\n\x1b[31mroot@router# \x1b[0m\r\n"
        tail = "unconsumed tail\r\n"
        fake = ScriptedTelnet(chunks=[prefix + tail])
        session = make_session(fake, JUNOS_PROFILE)
        match = await session._expect({"config"}, first_prompt=True)
        self.assertEqual(match.name, "config")
        self.assertEqual(await session._expect({"config"}, first_prompt=True), match)
        output = session._consume(match.end)
        self.assertEqual(output, prefix)
        self.assertEqual(await session.drain_residual(), tail)
        self.assertEqual(await session.drain_residual(), "")
        self.assertEqual(output, prefix)

    async def test_generic_expect_only_waits_for_requested_prompt_stability(self):
        fake = ScriptedTelnet(chunks=["root@router# "])
        session = make_session(fake, JUNOS_PROFILE)
        with exchange_clock(session) as clock:
            match = await session._expect({"config"}, settle=2)
        self.assertEqual(clock.time(), 2)
        self.assertEqual(match.since, 0)
        self.assertEqual(session._consume(match.end), "root@router# ")

    async def test_exchange_with_success_marker_finishes_after_normal_settle(self):
        fake = ScriptedTelnet(chunks=["commit\ncommit complete\nroot@router# "])
        session = make_session(fake, JUNOS_PROFILE)
        session.PROMPT_SETTLE = 2
        with exchange_clock(session) as clock:
            result = await session.exchange("commit", timeout=10, success_pattern=re.compile("commit complete"))
        self.assertEqual(clock.time(), 2)
        self.assertTrue(result.success_seen)
        self.assertTrue(result.prompt_returned)

    async def test_exchange_ignores_success_pattern_in_command_echo_and_waits_grace(self):
        fake = ScriptedTelnet(chunks=["commit complete\nroot@router# "])
        session = make_session(fake, JUNOS_PROFILE)
        session.PROMPT_SETTLE = 2
        with exchange_clock(session) as clock:
            result = await session.exchange(
                "commit complete", timeout=10, success_pattern=re.compile("commit complete")
            )
        self.assertEqual(clock.time(), 4)
        self.assertFalse(result.success_seen)
        self.assertTrue(result.prompt_returned)

    async def test_exchange_success_during_grace_does_not_restart_prompt_stability(self):
        fake = ScriptedTelnet(chunks=["commit\nroot@router# \n", "", "", "commit complete\nroot@router# "])
        session = make_session(fake, JUNOS_PROFILE)
        session.PROMPT_SETTLE = 2
        with exchange_clock(session) as clock:
            result = await session.exchange("commit", timeout=10, success_pattern=re.compile("commit complete"))
        self.assertEqual(clock.time(), 3)
        self.assertTrue(result.success_seen)
        self.assertTrue(result.prompt_returned)
        self.assertEqual(fake.writes, ["commit"])

    async def test_exchange_prompt_disappearing_during_grace_restarts_stability(self):
        fake = ScriptedTelnet(chunks=["root@router# \n", "", "", "work in progress\n", "", "root@router# "])
        session = make_session(fake, JUNOS_PROFILE)
        session.PROMPT_SETTLE = 2
        with exchange_clock(session) as clock:
            result = await session.exchange("commit", timeout=12, success_pattern=re.compile("commit complete"))
        self.assertEqual(clock.time(), 9)
        self.assertTrue(result.prompt_returned)
        self.assertFalse(result.success_seen)
        self.assertIn("work in progress", result.output)

    async def test_exchange_grace_uses_original_deadline(self):
        fake = ScriptedTelnet(chunks=["root@router# \n"])
        session = make_session(fake, JUNOS_PROFILE)
        session.PROMPT_SETTLE = 2
        with exchange_clock(session) as clock:
            result = await session.exchange("commit", timeout=3, success_pattern=re.compile("commit complete"))
        self.assertEqual(clock.time(), 3)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.prompt_returned)
        self.assertEqual(fake.writes, ["commit"])

    async def test_exchange_login_during_grace_returns_restore_without_resending(self):
        fake = ScriptedTelnet(chunks=["root@router# \n", "", "", "router login: "])
        session = make_session(fake, JUNOS_PROFILE)
        session.PROMPT_SETTLE = 2
        with exchange_clock(session) as clock:
            result = await session.exchange("commit", timeout=10, success_pattern=re.compile("commit complete"))
        self.assertEqual(clock.time(), 3)
        self.assertTrue(result.restore_required)
        self.assertFalse(result.prompt_returned)
        self.assertEqual(result.prompt_name, "login")
        self.assertEqual(fake.writes, ["commit"])

    async def test_login_consumes_multiple_prompts_from_one_read(self):
        fake = ScriptedTelnet(chunks=["router login:\r\nPassword:\r\nroot@router# "])
        session = make_session(fake, JUNOS_PROFILE)
        await session.enter_command_prompt(timeout=1)
        self.assertEqual(fake.writes, ["root", "admin123"])
        self.assertEqual(await session.drain_residual(), "")

    async def test_login_retains_partial_next_prompt_when_consuming_current_prompt(self):
        def complete_password(fake, content):
            if content == "root":
                fake.chunks.append("word:\r\nroot@router# ")

        fake = ScriptedTelnet(chunks=["router login:\r\nPass"], on_write=complete_password)
        session = make_session(fake, JUNOS_PROFILE)
        await session.enter_command_prompt(timeout=1)
        self.assertEqual(fake.writes, ["root", "admin123"])

    async def test_exchange_keeps_trailing_logs_for_drain(self):
        tail = "Auto Image Upgrade: busy\r\n"
        fake = ScriptedTelnet(chunks=["show\r\nresult\r\nroot@router# \r\n" + tail])
        session = make_session(fake, JUNOS_PROFILE)
        response = await session.exchange("show", timeout=1)
        self.assertTrue(response.prompt_returned)
        self.assertNotIn(tail, response.output)
        self.assertEqual(await session.drain_residual(), tail)
        self.assertIn("result", response.output)

    async def test_consecutive_exchanges_do_not_reuse_success_or_prompt(self):
        fake = ScriptedTelnet(on_write=junos_device)
        session = make_session(fake, JUNOS_PROFILE)
        pattern = re.compile("commit complete")
        first = await session.exchange("commit", timeout=1, success_pattern=pattern)
        first_output = first.output
        second = await session.exchange("top", timeout=1, success_pattern=pattern)
        self.assertTrue(first.success_seen)
        self.assertFalse(second.success_seen)
        self.assertNotIn("commit complete", second.output)
        self.assertEqual(first.output, first_output)

    async def test_timeout_result_consumes_partial_output_once(self):
        fake = ScriptedTelnet(chunks=["partial output"])
        session = make_session(fake, JUNOS_PROFILE)
        with exchange_clock(session):
            result = await session.exchange("show", timeout=2)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.output, "partial output")
        self.assertEqual(await session.drain_residual(), "")

    async def test_cancellation_preserves_unconsumed_output_for_recovery(self):
        fake = ScriptedTelnet()
        waiting = asyncio.Event()

        async def read():
            if not waiting.is_set():
                waiting.set()
                return "partial output"
            await asyncio.Event().wait()

        fake.read_available = AsyncMock(side_effect=read)
        session = make_session(fake, JUNOS_PROFILE)
        task = asyncio.create_task(session.exchange("show"))
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        fake.read_available = AsyncMock(return_value="")
        self.assertEqual(await session.drain_residual(), "partial output")

    async def test_reconnect_discards_unconsumed_partial_prompt(self):
        fake = ScriptedTelnet(chunks=["root@route"], on_write=junos_device)
        session = make_session(fake, JUNOS_PROFILE)
        await session._read()
        fake.connected = False
        await session.restore(timeout=1)
        self.assertEqual(fake.writes, ["root", "admin123", "cli", "configure"])
        self.assertEqual((await session.drain_residual()).strip(), "")

    async def test_login_and_exchange_share_idle_probe_timing(self):
        for operation in ("enter_command_prompt", "exchange"):
            with self.subTest(operation=operation):
                probes = []
                prompt = "root@router# "

                def respond(fake, content):
                    if content == "":
                        probes.append(clock.time())
                        if clock.time() >= 6:
                            fake.chunks.append(prompt)

                fake = ScriptedTelnet(on_write=respond)
                session = make_session(fake, JUNOS_PROFILE)
                session.NUDGE_INTERVAL = 3
                with exchange_clock(session) as clock:
                    if operation == "exchange":
                        result = await session.exchange("show", timeout=20)
                        self.assertTrue(result.prompt_returned)
                    else:
                        await getattr(session, operation)(timeout=20)
                self.assertEqual(probes[:2], [3, 6])
                self.assertEqual(len(probes), 2)
                self.assertEqual(set(fake.line_endings), {JUNOS_PROFILE.line_ending})

    async def test_common_idle_probe_disconnect_keeps_each_flows_recovery_policy(self):
        for operation in ("enter_command_prompt", "exchange"):
            with self.subTest(operation=operation):
                probes = []
                prompt = "root@router# "

                def respond(fake, content):
                    if content == "":
                        probes.append(clock.time())
                        if len(probes) == 1:
                            fake.connected = False
                            raise ConnectionError("idle probe disconnected")
                        fake.chunks.append(prompt)

                async def reconnect():
                    fake.connected = True
                    fake.chunks.clear()
                    fake.chunks.append(prompt)

                fake = ScriptedTelnet(on_write=respond)
                fake.reconnect_async = AsyncMock(side_effect=reconnect)
                session = make_session(fake, JUNOS_PROFILE)
                session.NUDGE_INTERVAL = 3
                with exchange_clock(session) as clock:
                    if operation == "exchange":
                        result = await session.exchange("show", timeout=20)
                        self.assertTrue(result.disconnected)
                        self.assertTrue(result.restore_required)
                        self.assertFalse(result.timed_out)
                        fake.reconnect_async.assert_not_awaited()
                        self.assertEqual(fake.writes, ["show", ""])
                    else:
                        await getattr(session, operation)(timeout=20)
                        fake.reconnect_async.assert_awaited_once_with()
                self.assertEqual(probes[0], 3)

    async def test_drain_residual_never_sends_idle_probes(self):
        fake = ScriptedTelnet()
        session = make_session(fake, JUNOS_PROFILE)
        session.DRAIN_MAX = 10
        session.DRAIN_QUIET = 20
        with exchange_clock(session) as clock:
            output = await session.drain_residual()
        self.assertEqual(clock.time(), 10)
        self.assertEqual(output, "")
        self.assertEqual(fake.writes, [])

    async def test_enter_command_prompt_from_login(self):
        """验证 login→password→shell→exec→config 状态迁移。"""
        fake = ScriptedTelnet(on_write=junos_device)
        session = make_session(fake, JUNOS_PROFILE)

        await session.enter_command_prompt("router login: ", timeout=2)

        self.assertEqual(fake.writes, ["root", "admin123", "cli", "configure"])
        self.assertEqual(set(fake.line_endings), {b"\n"})

    async def test_login_handles_split_prompts_and_background_logs_with_all_line_endings(self):
        for newline in ("\r", "\n", "\r\n"):
            with self.subTest(newline=newline):
                fake = ScriptedTelnet(on_write=junos_device)
                fake.enqueue("router login:" + newline, size=2)
                fake.enqueue(("Auto Image Upgrade: checking image" + newline) * 100, size=200)
                session = make_session(fake, JUNOS_PROFILE)

                await session.enter_command_prompt(timeout=2)

                self.assertEqual(fake.writes, ["root", "admin123", "cli", "configure"])

    async def test_login_reconnect_discards_old_partial_prompt(self):
        fake = ScriptedTelnet()
        fake.read_available = AsyncMock(side_effect=[
            "root@route", ConnectionError("connection lost"), "r# ", "", "\nroot@router# "
        ])
        fake.reconnect_async = AsyncMock()
        session = make_session(fake, JUNOS_PROFILE)
        session.NUDGE_INTERVAL = .001

        await session.enter_command_prompt(timeout=1)

        fake.reconnect_async.assert_awaited_once_with()
        # 新连接的 r# 不能与旧连接的 root@route 拼成有效配置提示符。
        self.assertEqual(fake.writes, [""])

    async def test_login_recovers_when_idle_probe_write_disconnects(self):
        def respond(fake, content):
            if content == "":
                fake.connected = False
                raise ConnectionError("probe write disconnected")
            junos_device(fake, content)

        fake = ScriptedTelnet(on_write=respond)
        fake.reconnect_async = AsyncMock(wraps=fake.reconnect_async)
        session = make_session(fake, JUNOS_PROFILE)
        session.NUDGE_INTERVAL = .001

        await session.enter_command_prompt(timeout=1)

        fake.reconnect_async.assert_awaited_once_with()
        self.assertEqual(fake.writes, ["", "root", "admin123", "cli", "configure"])

    async def test_login_limits_reconnections(self):
        fake = ScriptedTelnet()
        fake.read_available = AsyncMock(side_effect=ConnectionError("still disconnected"))
        fake.reconnect_async = AsyncMock()
        session = make_session(fake, JUNOS_PROFILE)

        with self.assertRaises(ConnectionError):
            await session.enter_command_prompt(timeout=1)

        self.assertEqual(fake.reconnect_async.await_count, session.MAX_RECOVERIES)

    async def test_command_read_disconnect_preserves_output_without_reconnect_or_resend(self):
        fake = ScriptedTelnet()
        fake.read_available = AsyncMock(side_effect=["partial output", ConnectionError("connection lost")])
        fake.reconnect_async = AsyncMock()
        session = make_session(fake, JUNOS_PROFILE)

        result = await session.exchange("commit")

        self.assertEqual(result.output, "partial output")
        self.assertTrue(result.sent)
        self.assertTrue(result.disconnected)
        self.assertTrue(result.restore_required)
        self.assertFalse(result.timed_out)
        self.assertEqual(fake.writes, ["commit"])
        fake.reconnect_async.assert_not_called()

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

    async def test_unlimited_exchange_keeps_probing_until_prompt_arrives(self):
        for explicit_none in (False, True):
            with self.subTest(explicit_none=explicit_none):
                probes = []

                def respond_after_long_silence(fake, content):
                    if not content:
                        probes.append(clock.time())
                        if clock.time() >= 600:
                            fake.enqueue("root@router# ", size=100)

                fake = ScriptedTelnet(on_write=respond_after_long_silence)
                session = make_session(fake, JUNOS_PROFILE)
                with exchange_clock(session) as clock:
                    kwargs = {"timeout": None} if explicit_none else {}
                    result = await session.exchange("show", **kwargs)
                self.assertGreaterEqual(clock.time(), 600)
                self.assertEqual(probes, list(range(3, 601, 3)))
                self.assertTrue(result.prompt_returned)
                self.assertFalse(result.timed_out)
                self.assertTrue(result.sent)
                self.assertEqual(fake.writes.count("show"), 1)

    async def test_unlimited_exchange_propagates_external_cancellation(self):
        fake = ScriptedTelnet()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def hang():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        fake.read_available = AsyncMock(side_effect=hang)
        task = asyncio.create_task(make_session(fake, JUNOS_PROFILE).exchange("commit"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(fake.writes, ["commit"])

    async def test_unlimited_exchange_does_not_create_a_timeout_wrapper(self):
        fake = ScriptedTelnet(on_write=junos_device)
        session = make_session(fake, JUNOS_PROFILE)
        with patch("console_session.asyncio.wait_for", side_effect=AssertionError("unexpected timeout")):
            result = await session.exchange("top")
        self.assertTrue(result.prompt_returned)

    async def test_readiness_and_recovery_cancel_nested_login_with_one_timeout(self):
        for operation in ("ensure_ready", "restore"):
            with self.subTest(operation=operation):
                fake = ScriptedTelnet(chunks=["router login: "])
                session = make_session(fake, JUNOS_PROFILE)
                cancelled = asyncio.Event()

                async def hang(initial_output):
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

                session._enter_command_prompt = AsyncMock(side_effect=hang)
                with patch("console_session.asyncio.wait_for", wraps=asyncio.wait_for) as limited:
                    with self.assertRaises(TimeoutError):
                        await getattr(session, operation)(timeout=.02)
                self.assertTrue(cancelled.is_set())
                self.assertEqual(limited.await_count, 1)
                self.assertEqual(session.recovery_generation, 1)
                self.assertEqual(fake.writes, [])

    async def test_login_timeout_cancels_hung_read(self):
        fake = ScriptedTelnet()
        cancelled = asyncio.Event()

        async def hang():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        fake.read_available = AsyncMock(side_effect=hang)
        session = make_session(fake, JUNOS_PROFILE)
        with self.assertRaises(TimeoutError):
            await session.enter_command_prompt(timeout=.01)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(fake.writes, [])

    async def test_drain_limits_read_count_using_seconds_and_poll_interval(self):
        fake = ScriptedTelnet()
        fake.read_available = AsyncMock(return_value="background output\n")
        session = make_session(fake, JUNOS_PROFILE)
        session.DRAIN_MAX = 2.5
        with exchange_clock(session) as clock:
            output = await session.drain_residual()
        self.assertEqual(fake.read_available.await_count, 3)
        self.assertEqual(clock.time(), 3)
        self.assertEqual(output, "background output\n" * 3)
        self.assertEqual(fake.writes, [])

    async def test_exchange_expired_budget_never_writes(self):
        fake = ScriptedTelnet()
        session = make_session(fake, JUNOS_PROFILE)
        for timeout in (0, -1):
            result = await session.exchange("commit", timeout=timeout)
            self.assertTrue(result.timed_out)
            self.assertFalse(result.sent)
        self.assertEqual(fake.writes, [])

    async def test_exchange_restarts_settle_after_prompt_disappears(self):
        fake = ScriptedTelnet(chunks=[
            "commit complete\nroot@router# ", "\nwork in progress\n", "", "", "root@router# "
        ])
        session = make_session(fake, JUNOS_PROFILE)
        session.PROMPT_SETTLE = 2
        with exchange_clock(session) as clock:
            result = await session.exchange("commit", 10, success_pattern=re.compile("commit complete"))
        self.assertEqual(clock.time(), 6)
        self.assertTrue(result.success_seen)
        self.assertTrue(result.prompt_returned)
        self.assertIn("work in progress", result.output)

    async def test_exchange_stale_prompt_times_out_and_resumes_nudging(self):
        fake = ScriptedTelnet(chunks=["root@router# ", "\nstill running\n"])
        session = make_session(fake, JUNOS_PROFILE)
        session.PROMPT_SETTLE = 2
        with exchange_clock(session):
            result = await session.exchange("show", 5)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.prompt_returned)
        self.assertIsNone(result.prompt_name)
        self.assertEqual(fake.writes, ["show", ""])

    async def test_exchange_stable_window_cannot_exceed_deadline(self):
        fake = ScriptedTelnet(chunks=["root@router# "])
        session = make_session(fake, JUNOS_PROFILE)
        session.PROMPT_SETTLE = 5
        with exchange_clock(session) as clock:
            result = await session.exchange("show", 3)
        self.assertEqual(clock.time(), 3)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.prompt_returned)

    async def test_background_output_does_not_restart_valid_prompt_settle(self):
        fake = ScriptedTelnet(chunks=[
            "root@router# \n", "Auto Image Upgrade: busy\n", "Auto Image Upgrade: busy\n"
        ])
        session = make_session(fake, JUNOS_PROFILE)
        session.PROMPT_SETTLE = 2
        with exchange_clock(session) as clock:
            result = await session.exchange("show", 4)
        self.assertEqual(clock.time(), 2)
        self.assertTrue(result.prompt_returned)

    async def test_exchange_cancels_hung_read(self):
        fake = ScriptedTelnet()
        fake.read_available = AsyncMock(side_effect=asyncio.Event().wait)
        result = await make_session(fake, JUNOS_PROFILE).exchange("show", .02)
        self.assertTrue(result.timed_out)
        self.assertTrue(result.sent)

    async def test_exchange_cancels_hung_write_without_reading(self):
        fake = ScriptedTelnet()
        fake.read_available = AsyncMock()

        async def hang(*args):
            await asyncio.Event().wait()

        fake.write_line = AsyncMock(side_effect=hang)
        result = await make_session(fake, JUNOS_PROFILE).exchange("commit", .02)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.sent)
        fake.read_available.assert_not_called()

    async def test_ready_uses_one_timeout_and_calls_private_restore(self):
        fake = ScriptedTelnet(chunks=["router login: "])
        session = make_session(fake, JUNOS_PROFILE)
        session._restore = AsyncMock()
        session.restore = AsyncMock(side_effect=AssertionError("nested public timeout"))
        with patch("console_session.asyncio.wait_for", wraps=asyncio.wait_for) as limited:
            await session.ensure_ready(timeout=.1)
        session._restore.assert_awaited_once_with("router login: ")
        session.restore.assert_not_awaited()
        self.assertEqual(limited.await_count, 1)
        self.assertEqual(limited.call_args.kwargs["timeout"], .1)

    async def test_restore_bounds_reconnect_and_increments_generation(self):
        fake = ScriptedTelnet()
        fake.connected = False
        fake.reconnect_async = AsyncMock(side_effect=asyncio.Event().wait)
        session = make_session(fake, JUNOS_PROFILE)
        before = session.recovery_generation
        with self.assertRaises(TimeoutError):
            await session.restore(timeout=.02)
        self.assertGreater(session.recovery_generation, before)
        self.assertEqual(fake.writes, [])

    async def test_restore_calls_private_login_without_another_timeout(self):
        fake = ScriptedTelnet()
        fake.connected = False
        session = make_session(fake, JUNOS_PROFILE)
        session._enter_command_prompt = AsyncMock()
        session.enter_command_prompt = AsyncMock(side_effect=AssertionError("nested public timeout"))
        with exchange_clock(session) as clock:
            async def reconnect():
                await clock.sleep(2)
                fake.connected = True
            fake.reconnect_async = reconnect
            await session.restore(timeout=4)
        self.assertEqual(clock.time(), 3)
        session._enter_command_prompt.assert_awaited_once_with("")
        session.enter_command_prompt.assert_not_awaited()

    async def test_write_and_read_share_one_exchange_timeout(self):
        fake = ScriptedTelnet()
        session = make_session(fake, JUNOS_PROFILE)
        cancelled = asyncio.Event()

        async def slow_write(content, line_ending):
            fake.writes.append(content)
            await asyncio.sleep(.02)

        async def slow_read():
            try:
                await asyncio.sleep(.02)
                return "root@router# "
            finally:
                cancelled.set()

        fake.write_line = slow_write
        fake.read_available = AsyncMock(side_effect=slow_read)
        result = await session.exchange("show", timeout=.03)
        self.assertTrue(result.sent)
        self.assertTrue(result.timed_out)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(fake.writes, ["show"])

    async def test_expired_ready_and_restore_do_not_touch_transport(self):
        fake = ScriptedTelnet()
        fake.read_available = AsyncMock()
        session = make_session(fake, JUNOS_PROFILE)
        for action in (session.ensure_ready, session.restore, session.enter_command_prompt):
            with self.assertRaises(TimeoutError):
                await action(timeout=0)
        fake.read_available.assert_not_called()
        self.assertEqual(fake.writes, [])

    async def test_exchange_nudges_silent_device_with_profile_line_ending(self):
        """验证无回显设备收到空行后返回提示符，且使用厂商换行符。"""
        for profile, prompt in (
            (JUNOS_PROFILE, "root@router# "),
            (CISCO_PROFILE, "RP/0/RP0/CPU0:router(config)# "),
        ):
            with self.subTest(profile=profile.name):
                def respond(fake, content):
                    if content == "":
                        fake.chunks.append(prompt)

                fake = ScriptedTelnet(on_write=respond)
                session = make_session(fake, profile)
                session.NUDGE_INTERVAL = 3.0
                with exchange_clock(session):
                    result = await session.exchange("show", timeout=10)

                self.assertEqual(fake.writes, ["show", ""])
                self.assertEqual(fake.line_endings, [profile.line_ending] * 2)
                self.assertTrue(result.sent)
                self.assertTrue(result.prompt_returned)
                self.assertFalse(result.timed_out)

    async def test_exchange_repeated_nudges_do_not_extend_timeout(self):
        """验证持续空读每三秒探测一次，仍在原始截止时间结束。"""
        writes_at = []
        fake = ScriptedTelnet(on_write=lambda fake, content: writes_at.append(clock.time()))
        session = make_session(fake, JUNOS_PROFILE)
        session.NUDGE_INTERVAL = 3.0
        with exchange_clock(session) as clock:
            result = await session.exchange("show", timeout=10)

        self.assertEqual(writes_at, [0, 3, 6, 9])
        self.assertEqual(fake.writes, ["show", "", "", ""])
        self.assertEqual(clock.time(), 10)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.restore_required)

    async def test_exchange_partial_output_resets_idle_timer(self):
        """验证部分输出重置静默计时，之后再次静默仍会探测。"""
        writes_at = []
        fake = ScriptedTelnet(
            chunks=["", "", "partial output"],
            on_write=lambda fake, content: writes_at.append(clock.time()),
        )
        session = make_session(fake, JUNOS_PROFILE)
        session.NUDGE_INTERVAL = 3.0
        with exchange_clock(session) as clock:
            result = await session.exchange("show", timeout=9)

        self.assertEqual(writes_at, [0, 5, 8])
        self.assertEqual(result.output, "partial output")
        self.assertTrue(result.timed_out)

    async def test_exchange_continuous_output_does_not_nudge(self):
        """验证持续回显期间不会发送额外空行。"""
        fake = ScriptedTelnet(chunks=["output\r\n"] * 10)
        session = make_session(fake, JUNOS_PROFILE)
        session.NUDGE_INTERVAL = 3.0
        with exchange_clock(session):
            result = await session.exchange("show", timeout=10)

        self.assertEqual(fake.writes, ["show"])
        self.assertTrue(result.timed_out)

    async def test_exchange_prompt_settle_does_not_nudge(self):
        """验证等待提示符稳定及成功标记时停止空行探测。"""
        fake = ScriptedTelnet(chunks=["root@router# "])
        session = make_session(fake, JUNOS_PROFILE)
        session.NUDGE_INTERVAL = 3.0
        session.PROMPT_SETTLE = 3.0
        with exchange_clock(session):
            result = await session.exchange(
                "commit", timeout=10, success_pattern=re.compile("commit complete")
            )

        self.assertEqual(fake.writes, ["commit"])
        self.assertTrue(result.prompt_returned)
        self.assertFalse(result.success_seen)
        self.assertFalse(result.timed_out)

    async def test_exchange_nudge_write_disconnect_requires_restore(self):
        """验证探测写入断线立即返回恢复状态，保留已有回显。"""
        def disconnect(fake, content):
            if content == "":
                raise ConnectionError("nudge write disconnected")

        fake = ScriptedTelnet(chunks=["partial output"], on_write=disconnect)
        session = make_session(fake, JUNOS_PROFILE)
        session.NUDGE_INTERVAL = 3.0
        with exchange_clock(session) as clock:
            result = await session.exchange("show", timeout=10)

        self.assertEqual(fake.writes, ["show", ""])
        self.assertEqual(clock.time(), 3)
        self.assertEqual(result.output, "partial output")
        self.assertTrue(result.sent)
        self.assertTrue(result.disconnected)
        self.assertTrue(result.restore_required)
        self.assertFalse(result.timed_out)

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
