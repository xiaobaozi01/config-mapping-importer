#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Juniper 导入状态机的场景测试。

做什么：用脚本化 console 覆盖启动、加载、提交、会话恢复和清理路径。
为什么：这些流程依赖真实设备时序，单元测试需要稳定地模拟拆包和异常。
"""

import asyncio
import importlib.util
import os
import re
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch
from pathlib import Path
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# src.* 桩模块
# ---------------------------------------------------------------------------
def _new_module(name, registry):
    module = types.ModuleType(name)
    module.__path__ = []
    registry[name] = module
    return module


class FakeEmulation:
    def __init__(self):
        self.deleted = []

    async def delete_node(self, project_id, node_id):
        self.deleted.append((project_id, node_id))


STATE = {"emulation": FakeEmulation()}


class FakeNeConfigManager:
    FTP_SERVER_IP = "10.0.0.1"
    FTP_CLIENT_IP = "10.0.0.2"

    def __init__(self, emulation, project_id, node_id, host_located, node_compute_id=None):
        self._emulation = emulation
        self._project_id = project_id
        self._node_id = node_id
        self._host_located = host_located
        self._node_compute_id = node_compute_id
        self.fake_telnet = None
        self.parent_calls = []
        self.fail_loopback = False

    async def get_telnet_client(self):
        return self.fake_telnet

    async def init_ftp_server(self):
        self.parent_calls.append(("init_ftp_server",))
        return SimpleNamespace(node_id="ftp-node")

    async def copy_file_to_ftp_server(self, ftp_server, local_cfg_path):
        self.parent_calls.append(("copy_file_to_ftp_server", ftp_server.node_id, local_cfg_path))

    async def link_ftp_server(self, ftp_server, adapter_number):
        self.parent_calls.append(("link_ftp_server", ftp_server.node_id, adapter_number))

    async def loopback_last_adapter_for_uni(self):
        self.parent_calls.append(("loopback_last_adapter_for_uni",))
        if self.fail_loopback:
            raise RuntimeError("loopback failed")


class FakeSettings:
    @classmethod
    def load_from_yaml(cls):
        return SimpleNamespace()


class _FakeEmulationClass:
    @staticmethod
    def instance():
        return STATE["emulation"]


class FakeNodeCreateInfoFactory:
    def get_create_info(self, _name):
        return SimpleNamespace(
            custom_adapters=[
                SimpleNamespace(adapter_number=index, port_name=f"ge-0/0/{index}")
                for index in range(4)
            ]
        )


def _build_src_stubs():
    registry = {}

    _new_module("src", registry)
    _new_module("src.application", registry)
    _new_module("src.application.config", registry)
    _new_module("src.application.config.manager", registry)
    base_module = _new_module(
        "src.application.config.manager.ne_config_manager", registry
    )
    base_module.NeConfigManager = FakeNeConfigManager

    _new_module("src.common", registry)
    config_module = _new_module("src.common.config", registry)
    config_module.AppSettings = FakeSettings
    constant = _new_module("src.common.constant", registry)
    constant.EMULATION_CLASS = _FakeEmulationClass

    _new_module("src.emulation", registry)
    emulation_platform = _new_module("src.emulation.emulation_platform", registry)
    emulation_platform.EmulationPlatform = object

    _new_module("src.schemas", registry)
    _new_module("src.schemas.emulation", registry)
    nodes = _new_module("src.schemas.emulation.nodes", registry)
    nodes.NodeCreateInfoFactory = FakeNodeCreateInfoFactory
    return registry


def _load_juniper_isolated():
    """加载被测管理器并恢复真实模块表。

    做什么：临时安装 src 桩模块，导入管理器后恢复 sys.modules。
    为什么：避免本测试的桩影响同进程中的其他测试。
    """

    stubs = _build_src_stubs()
    name = "juniper_vjunos_vmx"
    names = list(stubs) + [name]
    originals = {item: sys.modules[item] for item in names if item in sys.modules}

    sys.modules.update(stubs)
    try:
        path = Path(__file__).resolve().parent.parent / "juniper_vjunos_vmx.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for item in names:
            sys.modules.pop(item, None)
        for item, original in originals.items():
            sys.modules[item] = original


juniper = _load_juniper_isolated()


# ---------------------------------------------------------------------------
# 脚本化 Junos console
# ---------------------------------------------------------------------------
LOGIN_PROMPT = "router login: "
PASSWORD_PROMPT = "Password: "
SHELL_PROMPT = "root@router:~ # "
CLI_PROMPT = "root@router> "
EDIT_PROMPT = "[edit]\r\nroot@router# "


class FakeJunosTelnet:
    """最小化的脚本化 Junos 设备。

    做什么：按写入命令返回预置输出，并默认拆成小包。
    为什么：用于验证提示符、成功标记和 UTF-8 字符跨包处理。
    """

    def __init__(self, fragment_size=3):
        self.chunks = []
        self.writes = []
        self.connected = True
        self.fragment_size = fragment_size
        self.history = []
        self.load_behaviors = []
        self.command_errors = {}
        self.lock_commits = 0
        self.commit_login = False
        self.commit_login_adds_history = True
        self.commit_complete_then_login = False
        self.commit_no_history = False
        self.commit_error = None
        self.commit_complete_trailing = ""
        self.history_override = None
        self.background_log = ""
        self.load_count = 0
        self.commit_count = 0
        self.root_auth_present = True
        self.history_query_count = 0
        self.reconnect_count = 0
        self.closed = False
        self.current_prompt = LOGIN_PROMPT

        self.enqueue(LOGIN_PROMPT)

    # -- 传输层 -----------------------------------------------------------
    def enqueue(self, text):
        if not text:
            return
        size = max(1, int(self.fragment_size or len(text)))
        for index in range(0, len(text), size):
            self.chunks.append(text[index:index + size])

    async def read_available(self):
        await asyncio.sleep(0)
        if not self.connected:
            raise ConnectionError("fake console disconnected")
        if not self.chunks:
            return ""
        return self.chunks.pop(0)

    async def write_line(self, content, line_ending=None):
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        self.writes.append(content)
        self._handle(content.strip())

    async def telnet_write(self, content, sleep_time=0):
        await self.write_line(content, b"\n")

    async def telnet_line_break(self):
        self.writes.append("\n")

    def close(self):
        self.closed = True
        self.connected = False

    @property
    def is_connected(self):
        return self.connected

    async def reconnect_async(self):
        self.reconnect_count += 1
        self.connected = True
        self.current_prompt = LOGIN_PROMPT
        self.enqueue(LOGIN_PROMPT)

    # -- 设备行为 ---------------------------------------------------------
    def _handle(self, text):
        if text == "":
            self.enqueue(self.current_prompt)
            return
        if text == "root":
            self.current_prompt = PASSWORD_PROMPT
            self.enqueue(PASSWORD_PROMPT)
            return
        if text == "admin123":
            self.current_prompt = SHELL_PROMPT
            self.enqueue("Last login: from 10.0.0.1\r\n")
            self.enqueue(SHELL_PROMPT)
            return
        if text == "cli":
            self.current_prompt = CLI_PROMPT
            self.enqueue(CLI_PROMPT)
            return
        if text == "configure":
            self.current_prompt = EDIT_PROMPT
            self.enqueue("Entering configuration mode\r\n\r\n")
            self.enqueue(EDIT_PROMPT)
            return

        if text in self.command_errors:
            self.enqueue(text + "\r\n")
            self.enqueue(self.command_errors[text] + "\r\n")
            self.current_prompt = EDIT_PROMPT
            self.enqueue(EDIT_PROMPT)
            return

        if text == juniper.ROOT_AUTH_PROBE:
            self.enqueue(text + "\r\n")
            if self.root_auth_present:
                self.enqueue(
                    'set system root-authentication encrypted-password "hash"\r\n'
                )
            self.current_prompt = EDIT_PROMPT
            self.enqueue(EDIT_PROMPT)
            return

        if text.startswith("set system root-authentication "):
            self.root_auth_present = True
            self.enqueue(text + "\r\n")
            self.current_prompt = EDIT_PROMPT
            self.enqueue(EDIT_PROMPT)
            return

        if text.startswith("load override "):
            self._handle_load(text)
            return

        if text.startswith("commit comment "):
            self._handle_commit(text)
            return

        if text.startswith("run show system commit"):
            self._handle_history(text)
            return

        self.enqueue(text + "\r\n")
        if self.background_log:
            self.enqueue(self.background_log)
        self.current_prompt = EDIT_PROMPT
        self.enqueue(EDIT_PROMPT)

    def _handle_load(self, text):
        behavior = self.load_behaviors.pop(0) if self.load_behaviors else "ok"
        self.load_count += 1
        self.enqueue(text + "\r\n")
        if behavior == "login":
            self.current_prompt = LOGIN_PROMPT
            self.enqueue(LOGIN_PROMPT)
            return
        if behavior == "login_after_complete":
            self.current_prompt = LOGIN_PROMPT
            self.enqueue("load complete (0 errors)\r\n")
            self.enqueue(LOGIN_PROMPT)
            return
        if behavior == "errors":
            self.current_prompt = EDIT_PROMPT
            self.enqueue("load complete (2 errors)\r\n")
            self.enqueue(EDIT_PROMPT)
            return
        if behavior == "no_complete":
            self.current_prompt = EDIT_PROMPT
            self.enqueue(EDIT_PROMPT)
            return
        if self.background_log:
            self.enqueue(self.background_log)
        self.current_prompt = EDIT_PROMPT
        self.enqueue("load complete (0 errors)\r\n")
        self.enqueue(EDIT_PROMPT)

    def _handle_commit(self, text):
        match = re.search(r'commit comment "([^"]+)"', text)
        comment = match.group(1) if match else ""
        self.commit_count += 1
        self.enqueue(text + "\r\n")

        if self.lock_commits > 0:
            self.lock_commits -= 1
            self.enqueue("error: configuration database locked by: root\r\n")
            self.current_prompt = EDIT_PROMPT
            self.enqueue(EDIT_PROMPT)
            return

        if self.commit_error:
            self.enqueue(self.commit_error + "\r\n")
            self.current_prompt = EDIT_PROMPT
            self.enqueue(EDIT_PROMPT)
            return

        if self.commit_login:
            self.commit_login = False
            if self.commit_login_adds_history:
                self._record_commit(comment)
            self.current_prompt = LOGIN_PROMPT
            self.enqueue(LOGIN_PROMPT)
            return

        if self.commit_complete_then_login:
            self.commit_complete_then_login = False
            self._record_commit(comment)
            self.enqueue("commit complete\r\n")
            self.current_prompt = LOGIN_PROMPT
            self.enqueue(LOGIN_PROMPT)
            return

        if not self.commit_no_history:
            self._record_commit(comment)
        self.enqueue("commit complete\r\n")
        if self.commit_complete_trailing:
            self.enqueue(self.commit_complete_trailing + "\r\n")
        self.current_prompt = EDIT_PROMPT
        self.enqueue(EDIT_PROMPT)

    def _record_commit(self, comment):
        self.history.insert(0, (comment, False))

    def _handle_history(self, text):
        self.history_query_count += 1
        self.enqueue(text + "\r\n")
        override = self.history_override
        if callable(override):
            override = override(self)
        if override is not None:
            self.enqueue(override)
        else:
            self.enqueue(self._build_history())
        self.current_prompt = EDIT_PROMPT
        self.enqueue(EDIT_PROMPT)

    def _build_history(self):
        if not self.history:
            return "0 2024-01-01 00:00:00 UTC by root via cli\r\nInitial configuration.\r\n"
        lines = []
        for index, (comment, pending) in enumerate(self.history):
            suffix = " pending" if pending else ""
            lines.append(
                f"{index} 2024-01-01 00:00:{index:02d} UTC by root via cli{suffix}\r\n"
            )
            if comment:
                lines.append(comment + "\r\n")
        return "".join(lines)

    def written_commands(self):
        return [write for write in self.writes if write and write != "\n"]


def make_manager(fake):
    emulation = STATE["emulation"]
    emulation.deleted = []
    manager = juniper.JuniperVjunosRouter23d2R1d15dConfigManager(
        emulation, "project-1", "node-1", "host"
    )
    manager.fake_telnet = fake
    manager._telnet_client = fake
    manager.parent_calls = []
    manager.fail_loopback = False

    manager.COMMIT_HISTORY_MAX_ATTEMPTS = 3
    manager.COMMIT_HISTORY_QUERY_TIMEOUT = .5
    manager.READ_INTERVAL = 0.001
    manager.PROMPT_SETTLE = 0.01
    manager.DRAIN_QUIET = 0.005
    manager.DRAIN_MAX = 0.02
    manager.NUDGE_INTERVAL = 0.05
    manager.COMMIT_LOCK_RETRY_INTERVAL = 0.01
    manager.COMMIT_HISTORY_RETRY_INTERVAL = 0.01
    manager.COMMIT_HISTORY_MISMATCH_LIMIT = 2
    return manager


@contextmanager
def commit_lock_clock(manager):
    """只推进业务重试时间，ConsoleSession 仍使用真实异步读写和提示符判断。"""
    clock = SimpleNamespace(now=0.0, sleeps=[])
    clock.time = lambda: clock.now

    async def advance(delay):
        clock.sleeps.append(delay)
        clock.now += delay
        await asyncio.sleep(0)

    manager.COMMIT_LOCK_RETRY_INTERVAL = 5
    runtime = SimpleNamespace(get_running_loop=lambda: clock, sleep=advance, wait_for=asyncio.wait_for)
    with patch.object(juniper, "asyncio", runtime):
        yield clock


class JuniperImportScenarioTest(unittest.IsolatedAsyncioTestCase):
    async def test_full_import_with_boot_fragments_and_background_logs(self):
        fake = FakeJunosTelnet(fragment_size=3)
        fake.background_log = (
            "Auto Image Upgrade: error while checking image, continuing\r\n"
            "warning: harmless preparation warning\r\n"
        )
        manager = make_manager(fake)

        await manager.import_cfg("/tmp/adapted.cfg")

        calls = manager.parent_calls
        self.assertIn(("init_ftp_server",), calls)
        self.assertIn(("copy_file_to_ftp_server", "ftp-node", "/tmp/adapted.cfg"), calls)
        self.assertIn(("link_ftp_server", "ftp-node", 2), calls)

        commands = fake.written_commands()
        self.assertIn("set chassis fpc 0 pic 0 number-of-ports 3", commands)
        self.assertIn(
            "set interfaces ge-0/0/2 unit 0 family inet address 10.0.0.2/24", commands
        )
        self.assertIn(
            "load override ftp://ftpuser:ftpuser@10.0.0.1/node-1.cfg", commands
        )
        self.assertIn("run show system commit | no-more", commands)

        commit_commands = [item for item in commands if item.startswith("commit comment ")]
        self.assertEqual(len(commit_commands), 3)
        comments = [re.search(r'"(cfg-import-[^"]+)"', item).group(1) for item in commit_commands]
        self.assertEqual(len(set(comments)), 3)
        self.assertEqual(fake.history[0][0], comments[-1])

        self.assertEqual(STATE["emulation"].deleted, [("project-1", "ftp-node")])
        self.assertTrue(fake.closed)
        self.assertIn(("loopback_last_adapter_for_uni",), calls)

    def test_load_status_enum_is_stable_string(self):
        """验证 load 状态枚举仍与旧字符串完全兼容。"""
        self.assertEqual(juniper.LoadStatus.FAILED, "failed")
        self.assertEqual(str(juniper.LoadStatus.COMPLETE), "complete")

    async def test_missing_root_auth_is_bootstrapped(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.root_auth_present = False
        manager = make_manager(fake)

        await manager.import_cfg("/tmp/adapted.cfg")

        commands = fake.written_commands()
        self.assertIn(juniper.ROOT_AUTH_SET_COMMAND, commands)
        self.assertIn("encrypted-password", juniper.ROOT_AUTH_SET_COMMAND)
        self.assertIn("$6$admin123$", juniper.ROOT_AUTH_SET_COMMAND)
        self.assertTrue(fake.root_auth_present)
        self.assertEqual(fake.commit_count, 4)

    async def test_existing_root_auth_is_not_overwritten(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)

        await manager.import_cfg("/tmp/adapted.cfg")

        commands = fake.written_commands()
        self.assertIn(juniper.ROOT_AUTH_PROBE, commands)
        self.assertNotIn(
            "show configuration system root-authentication | display set",
            commands,
        )
        self.assertNotIn(juniper.ROOT_AUTH_SET_COMMAND, commands)
        self.assertEqual(fake.commit_count, 3)

    async def test_root_auth_probe_error_stops_import(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.command_errors[juniper.ROOT_AUTH_PROBE] = "error: permission denied"
        manager = make_manager(fake)

        with self.assertRaises(juniper.JunosCommandError):
            await manager.import_cfg("/tmp/adapted.cfg")

    async def test_existing_root_ssh_key_is_preserved(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.command_errors[juniper.ROOT_AUTH_PROBE] = 'set system root-authentication ssh-ed25519 "ssh-ed25519 AAAA"'
        manager = make_manager(fake)
        await manager.import_cfg("/tmp/adapted.cfg")
        self.assertNotIn(juniper.ROOT_AUTH_SET_COMMAND, fake.written_commands())

    async def test_unrecognized_auth_output_does_not_overwrite_password(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.command_errors[juniper.ROOT_AUTH_PROBE] = "syntax error."
        manager = make_manager(fake)
        with self.assertRaises(juniper.JunosResultUnconfirmed):
            await manager.import_cfg("/tmp/adapted.cfg")
        self.assertNotIn(juniper.ROOT_AUTH_SET_COMMAND, fake.written_commands())

    async def test_import_and_init_accept_current_terminal_mode(self):
        modes = (
            (LOGIN_PROMPT, ["root", "admin123", "cli", "configure"]),
            (PASSWORD_PROMPT, ["admin123", "cli", "configure"]),
            (SHELL_PROMPT, ["cli", "configure"]),
            (CLI_PROMPT, ["configure"]),
            (EDIT_PROMPT, []),
            ("[edit interfaces ge-0/0/0 unit 0]\r\nroot@router# ", []),
        )
        for operation in ("import", "init"):
            for prompt, transitions in modes:
                for silent in (False, True):
                    with self.subTest(operation=operation, prompt=prompt, silent=silent):
                        fake = FakeJunosTelnet(fragment_size=3)
                        fake.chunks.clear()
                        fake.current_prompt = prompt
                        if not silent:
                            fake.enqueue(prompt)
                        manager = make_manager(fake)
                        if operation == "import":
                            await manager.import_cfg("/tmp/adapted.cfg")
                        else:
                            await manager.init_device(fake, "router1")
                        prefix = transitions + ["top", juniper.ROOT_AUTH_PROBE]
                        self.assertEqual(fake.written_commands()[:len(prefix)], prefix)
                        self.assertEqual(fake.writes[0] == "", silent)
                        self.assertEqual(fake.load_count, int(operation == "import"))

    async def test_external_cancellation_cancels_hung_connection_and_runs_cleanup(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)

        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.Event().wait()

        manager.init_telnet_client = AsyncMock(side_effect=hang)
        task = asyncio.create_task(manager.import_cfg("/tmp/config.cfg"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(fake.writes, [])
        self.assertIn(("loopback_last_adapter_for_uni",), manager.parent_calls)

    async def test_external_cancellation_cancels_init_hung_login(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)
        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.Event().wait()

        fake.read_available = AsyncMock(side_effect=hang)
        task = asyncio.create_task(manager.init_device(fake, "router1"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(fake.writes, [])

    async def test_external_cancellation_cancels_hung_preparation_and_cleans_up(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)
        manager._enter_config = AsyncMock()
        started = asyncio.Event()

        async def hang(*args):
            started.set()
            await asyncio.Event().wait()
        manager.copy_file_to_ftp_server = AsyncMock(side_effect=hang)
        task = asyncio.create_task(manager.import_cfg("/tmp/adapted.cfg"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(fake.closed)
        self.assertIn(("project-1", "ftp-node"), STATE["emulation"].deleted)
        self.assertEqual(fake.load_count, 0)

    async def test_load_requires_load_complete(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.load_behaviors = ["no_complete"]
        manager = make_manager(fake)

        with self.assertRaises(juniper.JunosCommandError):
            await manager.import_cfg("/tmp/adapted.cfg")

        self.assertEqual(fake.load_count, 1)
        # 做什么：确认异常路径仍然执行了清理。
        self.assertTrue(fake.closed)
        self.assertIn(("loopback_last_adapter_for_uni",), manager.parent_calls)

    async def test_load_non_zero_error_count_fails(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.load_behaviors = ["errors"]
        manager = make_manager(fake)

        with self.assertRaises(juniper.JunosCommandError):
            await manager.import_cfg("/tmp/adapted.cfg")

    def test_load_complete_with_trailing_error_is_success(self):
        manager = make_manager(FakeJunosTelnet(fragment_size=0))
        result = juniper.ConsoleResult(
            output="load complete (0 errors)\nerror: unrelated background\n",
            prompt_returned=True,
        )

        status, detail = manager._classify_load(
            result, result.output, "error: unrelated background"
        )

        self.assertEqual(status, "complete")
        self.assertIsNone(detail)

    def test_load_without_complete_and_bare_syntax_error_is_not_success(self):
        manager = make_manager(FakeJunosTelnet(fragment_size=0))
        result = juniper.ConsoleResult(
            output="syntax error, expecting <name>\n",
            prompt_returned=True,
        )

        status, detail = manager._classify_load(result, result.output, None)

        # 做什么：裸 syntax error 没有 load complete 时也不能成功。
        # 为什么：只回提示符但缺少成功标记，必须按未完成处理。
        self.assertEqual(status, "no_complete")
        self.assertIsNone(detail)

    async def test_session_refresh_before_commit_reloads(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.load_behaviors = ["login", "ok"]
        manager = make_manager(fake)

        await manager.import_cfg("/tmp/adapted.cfg")

        self.assertEqual(fake.load_count, 2)
        self.assertGreaterEqual(fake.reconnect_count + 1, 1)

    async def test_confirmed_load_is_not_reloaded_after_login(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.load_behaviors = ["login_after_complete"]
        manager = make_manager(fake)

        await manager.import_cfg("/tmp/adapted.cfg")

        self.assertEqual(fake.load_count, 1)
        commit_commands = [
            item for item in fake.written_commands() if item.startswith("commit comment ")
        ]
        self.assertEqual(len(commit_commands), 3)

    async def test_preparation_error_aborts_before_load(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.command_errors["delete chassis auto-image-upgrade"] = "error: statement not found"
        manager = make_manager(fake)

        with self.assertRaises(juniper.JunosCommandError):
            await manager.import_cfg("/tmp/adapted.cfg")

        self.assertEqual(fake.load_count, 0)
        self.assertEqual(fake.commit_count, 0)
        self.assertTrue(fake.closed)

    async def test_auto_image_upgrade_is_committed_before_other_configuration(self):
        for operation in ("import", "init"):
            with self.subTest(operation=operation):
                fake = FakeJunosTelnet(fragment_size=0)
                manager = make_manager(fake)
                if operation == "import":
                    await manager.import_cfg("/tmp/adapted.cfg")
                    next_command = "set chassis fpc 0 pic 0 number-of-ports 3"
                else:
                    await manager.init_device(fake, "router-1")
                    next_command = "set system host-name router-1"
                commands = fake.written_commands()
                delete_index = commands.index("delete chassis auto-image-upgrade")
                self.assertEqual(commands.count("delete chassis auto-image-upgrade"), 1)
                self.assertTrue(commands[delete_index + 1].startswith("commit comment "))
                history_index = commands.index("run show system commit | no-more", delete_index + 1)
                self.assertLess(history_index, commands.index(next_command))

    async def test_auto_image_upgrade_commit_error_aborts_before_preparation(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.commit_error = "error: commit failed"
        manager = make_manager(fake)

        with self.assertRaises(juniper.JunosCommandError):
            await manager.import_cfg("/tmp/adapted.cfg")

        self.assertEqual(fake.load_count, 0)
        self.assertEqual(fake.commit_count, 1)
        self.assertNotIn(("init_ftp_server",), manager.parent_calls)
        self.assertNotIn("set chassis fpc 0 pic 0 number-of-ports 3", fake.written_commands())
        self.assertTrue(fake.closed)

    async def test_auto_image_upgrade_history_mismatch_aborts_before_preparation(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.history_override = "0 2024-01-01 00:00:00 UTC by root via cli\r\nold commit\r\n"
        manager = make_manager(fake)
        with self.assertRaises(juniper.JunosCommitVerificationError):
            await manager.import_cfg("/tmp/adapted.cfg")
        self.assertEqual(fake.commit_count, 1)
        self.assertEqual(fake.load_count, 0)
        self.assertNotIn(("init_ftp_server",), manager.parent_calls)
        self.assertNotIn("set chassis fpc 0 pic 0 number-of-ports 3", fake.written_commands())
        self.assertTrue(fake.closed)

    async def test_preparation_commit_error_aborts_before_load(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)
        commit = fake._handle_commit

        def fail_after_auto_image_upgrade(text):
            commit(text)
            if fake.commit_count == 1:
                fake.commit_error = "error: preparation commit failed"

        fake._handle_commit = fail_after_auto_image_upgrade
        with self.assertRaises(juniper.JunosCommandError):
            await manager.import_cfg("/tmp/adapted.cfg")
        self.assertEqual(fake.commit_count, 2)
        self.assertEqual(fake.load_count, 0)
        self.assertTrue(fake.closed)
        self.assertIn(("project-1", "ftp-node"), STATE["emulation"].deleted)

    async def test_cleanup_error_does_not_override_success(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)
        manager.fail_loopback = True

        await manager.import_cfg("/tmp/adapted.cfg")

        self.assertTrue(fake.closed)

    async def test_cleanup_error_does_not_override_import_failure(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.load_behaviors = ["errors"]
        manager = make_manager(fake)
        manager.fail_loopback = True

        with self.assertRaises(juniper.JunosCommandError):
            await manager.import_cfg("/tmp/adapted.cfg")

        self.assertTrue(fake.closed)

    async def test_init_device_still_sets_hostname_and_40_ports(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)

        await manager.init_device(fake, "router-1")

        commands = fake.written_commands()
        self.assertIn("delete chassis auto-image-upgrade", commands)
        self.assertIn("set system host-name router-1", commands)
        self.assertIn("set chassis fpc 0 pic 0 number-of-ports 40", commands)
        self.assertNotIn(("init_ftp_server",), manager.parent_calls)
        self.assertEqual(fake.commit_count, 2)

    async def test_init_device_command_error_stops_before_commit(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.command_errors["delete chassis auto-image-upgrade"] = "error: denied"
        manager = make_manager(fake)

        with self.assertRaises(juniper.JunosCommandError):
            await manager.init_device(fake, "router-1")

        self.assertEqual(fake.commit_count, 0)

    async def test_init_device_bootstraps_missing_root_auth(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.root_auth_present = False
        manager = make_manager(fake)

        await manager.init_device(fake, "router-1")

        commands = fake.written_commands()
        self.assertIn(juniper.ROOT_AUTH_SET_COMMAND, commands)
        self.assertLess(
            commands.index(juniper.ROOT_AUTH_SET_COMMAND),
            commands.index("set system host-name router-1"),
        )
        self.assertEqual(fake.commit_count, 3)

    async def test_export_cfg_creates_empty_file(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "out.cfg")
            await manager.export_cfg(path)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(os.path.getsize(path), 0)

    async def test_commit_lock_retry_uses_same_uuid_through_last_allowed_retry(self):
        for locks in (4, 60):
            with self.subTest(locks=locks):
                fake = FakeJunosTelnet(fragment_size=0)
                fake.lock_commits = locks
                manager = make_manager(fake)

                session = manager._console_session(fake)
                with commit_lock_clock(manager) as clock:
                    comment = await manager._commit_and_verify(session, "lock-test")

                commit_commands = [
                    item for item in fake.written_commands() if item.startswith("commit comment ")
                ]
                self.assertEqual(clock.now, locks * 5)
                self.assertEqual(len(commit_commands), locks + 1)
                self.assertEqual(len(set(commit_commands)), 1)
                self.assertIn(comment, commit_commands[0])
                self.assertEqual(fake.history[0][0], comment)

    async def test_persistent_commit_lock_stops_after_sixty_retries(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.lock_commits = 1000
        enqueue = fake.enqueue

        def number_lock_error(text):
            if text.startswith("error: configuration database locked"):
                text = text.rstrip("\r\n") + f" (attempt {fake.commit_count})\r\n"
            enqueue(text)

        fake.enqueue = number_lock_error
        manager = make_manager(fake)
        session = manager._console_session(fake)
        handle = fake._handle
        sent_at = []

        def track_commit(command):
            if command.startswith("commit comment "):
                sent_at.append(clock.now)
            handle(command)

        fake._handle = track_commit
        with commit_lock_clock(manager) as clock:
            with self.assertRaises(juniper.JunosCommandError) as raised:
                await manager._commit_and_verify(session, "persistent-lock")
        self.assertEqual(fake.commit_count, 61)
        self.assertEqual(sent_at, list(range(0, 301, 5)))
        self.assertEqual(clock.sleeps, [5] * 60)
        self.assertIn("lock retry limit reached (60 retries)", str(raised.exception))
        self.assertIn("error: configuration database locked by: root (attempt 61)", str(raised.exception))
        commits = [command for command in fake.written_commands() if command.startswith("commit comment ")]
        self.assertEqual(len(set(commits)), 1)
        self.assertEqual(fake.history_query_count, 0)

    async def test_slow_readiness_does_not_reduce_lock_retry_count(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.chunks.clear()
        fake.current_prompt = EDIT_PROMPT
        fake.lock_commits = 2
        manager = make_manager(fake)
        session = manager._console_session(fake)

        async def ready():
            clock.now += 400

        session.ensure_ready = AsyncMock(side_effect=ready)
        with commit_lock_clock(manager) as clock:
            comment = await manager._commit_and_verify(session, "slow-readiness")
        self.assertEqual(fake.commit_count, 3)
        self.assertEqual(clock.sleeps, [5, 5])
        self.assertGreater(clock.now, 300)
        self.assertEqual(fake.history[0][0], comment)
        self.assertTrue(all(not call.args and not call.kwargs for call in session.ensure_ready.await_args_list))

    async def test_zero_lock_retries_fails_after_first_lock_without_sleep(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.lock_commits = 1000
        manager = make_manager(fake)
        manager.COMMIT_LOCK_MAX_RETRIES = 0
        session = manager._console_session(fake)
        with commit_lock_clock(manager) as clock:
            with self.assertRaises(juniper.JunosCommandError):
                await manager._commit_and_verify(session, "no-retries")
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(fake.commit_count, 1)

    async def test_lock_retry_readiness_keeps_single_call_timeout(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.chunks.clear()
        fake.current_prompt = EDIT_PROMPT
        fake.lock_commits = 1
        manager = make_manager(fake)
        manager.COMMIT_LOCK_RETRY_INTERVAL = .001
        session = manager._console_session(fake)
        cancelled = asyncio.Event()
        wait_for = asyncio.wait_for
        limits = []

        async def ready():
            if fake.commit_count:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        async def limited(operation, timeout):
            limits.append(timeout)
            return await wait_for(operation, timeout=.02)

        session._ensure_ready = AsyncMock(side_effect=ready)
        with patch("console_session.asyncio.wait_for", side_effect=limited):
            with self.assertRaises(TimeoutError):
                await manager._commit_and_verify(session, "hung-readiness")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(fake.commit_count, 1)
        self.assertEqual(limits, [300, 300])

    async def test_unsent_commit_recovery_does_not_reset_lock_retries(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.chunks.clear()
        fake.current_prompt = EDIT_PROMPT
        fake.lock_commits = 1000
        manager = make_manager(fake)
        manager.COMMIT_LOCK_MAX_RETRIES = 2
        session = manager._console_session(fake)
        session.ensure_ready = AsyncMock()
        write = fake.write_line
        attempts = []

        async def fail_second_write(content, line_ending=None):
            if content.startswith("commit comment "):
                attempts.append(clock.now)
                if len(attempts) == 2:
                    raise ConnectionError("commit not sent")
            await write(content, line_ending)

        async def restore(initial_output):
            clock.now += 400

        fake.write_line = fail_second_write
        session.restore = AsyncMock(side_effect=restore)
        with commit_lock_clock(manager) as clock:
            with self.assertRaises(juniper.JunosCommandError) as raised:
                await manager._commit_and_verify(session, "recovery-lock")
        self.assertEqual(clock.now, 410)
        self.assertEqual(clock.sleeps, [5, 5])
        self.assertEqual(attempts, [0, 5, 405, 410])
        self.assertEqual(fake.commit_count, 3)
        self.assertEqual(session.restore.await_count, 1)
        self.assertIn("lock retry limit reached (2 retries)", str(raised.exception))

    async def test_unsent_commit_still_has_finite_recoveries_after_lock(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.chunks.clear()
        fake.current_prompt = EDIT_PROMPT
        fake.lock_commits = 1
        manager = make_manager(fake)
        session = manager._console_session(fake)
        session.ensure_ready = AsyncMock()
        session.restore = AsyncMock()
        write = fake.write_line

        async def fail_retries(content, line_ending=None):
            if fake.commit_count:
                raise ConnectionError("commit not sent")
            await write(content, line_ending)

        fake.write_line = fail_retries
        with commit_lock_clock(manager):
            with self.assertRaises(juniper.JunosResultUnconfirmed):
                await manager._commit_and_verify(session, "failed-writes")
        self.assertEqual(fake.commit_count, 1)
        self.assertEqual(session.restore.await_count, manager.MAX_RECOVERIES)

    async def test_retry_already_sent_can_finish_after_long_wait(self):
        for lose_response in (False, True):
            with self.subTest(lose_response=lose_response):
                fake = FakeJunosTelnet(fragment_size=0)
                fake.chunks.clear()
                fake.current_prompt = EDIT_PROMPT
                fake.lock_commits = 1
                fake.commit_login = lose_response
                manager = make_manager(fake)
                session = manager._console_session(fake)
                session.ensure_ready = AsyncMock()
                session.restore = AsyncMock()
                handle = fake._handle

                def finish_after_long_wait(command):
                    if command.startswith("commit comment ") and fake.commit_count == 1:
                        clock.now += 400
                    handle(command)

                fake._handle = finish_after_long_wait
                with commit_lock_clock(manager) as clock:
                    comment = await manager._commit_and_verify(session, "long-retry")
                self.assertEqual(clock.now, 405)
                self.assertEqual(fake.commit_count, 2)
                self.assertEqual(fake.history_query_count, 1)
                self.assertEqual(fake.history[0][0], comment)
                if lose_response:
                    self.assertEqual(session.restore.await_count, 1)
                    self.assertEqual(session.restore.call_args.kwargs, {})

    async def test_history_zero_attempts_does_not_query_or_sleep(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)
        manager.COMMIT_HISTORY_MAX_ATTEMPTS = 0
        session = manager._console_session(fake)
        with patch.object(juniper.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            verified = await manager._verify_commit_history(session, "no-attempts")
        self.assertIsNone(verified)
        self.assertEqual(fake.history_query_count, 0)
        self.assertEqual(fake.commit_count, 0)
        sleep.assert_not_awaited()

    async def test_commit_sent_result_lost_queries_history_before_resend(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.commit_login = True
        manager = make_manager(fake)

        session = manager._console_session(fake)
        comment = await manager._commit_and_verify(session, "lost-result")

        commit_commands = [
            item for item in fake.written_commands() if item.startswith("commit comment ")
        ]
        self.assertEqual(len(commit_commands), 1)
        self.assertEqual(fake.history_query_count, 1)
        self.assertEqual(fake.history[0][0], comment)

    async def test_commit_sent_result_lost_without_history_is_unconfirmed(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.commit_login = True
        fake.commit_login_adds_history = False
        manager = make_manager(fake)

        with self.assertRaises(juniper.JunosResultUnconfirmed):
            session = manager._console_session(fake)
            await manager._commit_and_verify(session, "lost-result")

        commit_commands = [
            item for item in fake.written_commands() if item.startswith("commit comment ")
        ]
        self.assertEqual(len(commit_commands), 1)

    async def test_commit_complete_but_latest_comment_mismatch(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.history_override = (
            "0 2024-01-01 00:00:00 UTC by root via cli\r\n"
            "some old commit\r\n"
            "1 2023-01-01 00:00:00 UTC by root via cli\r\n"
            "older commit\r\n"
        )
        manager = make_manager(fake)

        with self.assertRaises(juniper.JunosCommitVerificationError):
            session = manager._console_session(fake)
            await manager._commit_and_verify(session, "mismatch")

    async def test_explicit_commit_failure_is_not_retried(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.commit_error = "error: commit failed"
        manager = make_manager(fake)

        with self.assertRaises(juniper.JunosCommandError):
            session = manager._console_session(fake)
            await manager._commit_and_verify(session, "commit-error")

        commit_commands = [
            item for item in fake.written_commands() if item.startswith("commit comment ")
        ]
        self.assertEqual(len(commit_commands), 1)

    async def test_commit_complete_with_trailing_error_verifies_history(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.commit_complete_trailing = "error: unrelated background"
        manager = make_manager(fake)

        session = manager._console_session(fake)
        comment = await manager._commit_and_verify(session, "complete-with-error")

        self.assertEqual(fake.history[0][0], comment)

    def test_error_patterns_are_line_anchored(self):
        manager = make_manager(FakeJunosTelnet(fragment_size=0))

        self.assertIsNotNone(juniper.JUNOS_ERROR_PATTERN.search("error: real failure"))
        self.assertIsNotNone(
            juniper.JUNOS_ERROR_PATTERN.search("error: configuration check-out failed")
        )
        self.assertIsNotNone(
            manager.COMMIT_FAILURE_PATTERN.search("configuration check-out failed")
        )
        for text in (
            "syntax error, expecting <name>",
            "unknown command",
            "invalid command",
            "invalid value",
            "missing argument",
            "some error: text",
            "warning: statement not found",
            "commit check-out failed",
            "commit failed",
        ):
            self.assertIsNone(juniper.JUNOS_ERROR_PATTERN.search(text))
        self.assertIsNone(
            manager.COMMIT_FAILURE_PATTERN.search("commit check-out failed")
        )
        self.assertIsNone(manager.COMMIT_FAILURE_PATTERN.search("commit failed"))

    async def test_history_uses_sixty_attempts_and_fixed_five_second_intervals(self):
        manager = make_manager(FakeJunosTelnet(fragment_size=0))
        manager.COMMIT_HISTORY_MAX_ATTEMPTS = 60
        manager.COMMIT_HISTORY_QUERY_TIMEOUT = 10
        manager.COMMIT_HISTORY_RETRY_INTERVAL = 5
        clock = SimpleNamespace(now=0.0, sleeps=[])

        async def advance(delay):
            clock.sleeps.append(delay)
            clock.now += delay

        async def unavailable(*args):
            clock.now += 8
            return None

        query = AsyncMock(side_effect=unavailable)
        limited = AsyncMock(wraps=asyncio.wait_for)
        runtime = SimpleNamespace(sleep=advance, wait_for=limited)
        with patch.object(juniper, "asyncio", runtime), patch.object(manager, "_run_history_query", query):
            verified = await manager._verify_commit_history(None, "expected")
        self.assertIsNone(verified)
        self.assertEqual(query.await_count, 60)
        self.assertEqual(clock.sleeps, [5] * 59)
        self.assertEqual(clock.now, 60 * 8 + 59 * 5)
        self.assertTrue(all(call.kwargs == {"timeout": 10} for call in limited.await_args_list))

    async def test_each_history_query_cancels_hung_call_without_resending_commit(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)
        manager.COMMIT_HISTORY_QUERY_TIMEOUT = .03
        session = manager._console_session(fake)
        handle = fake._handle
        cancelled = asyncio.Event()
        cancellations = []

        async def hang():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
                cancellations.append(True)

        def hang_on_history(command):
            if command == "run show system commit | no-more":
                fake.read_available = AsyncMock(side_effect=hang)
            else:
                handle(command)

        fake._handle = hang_on_history
        with self.assertRaises(juniper.JunosResultUnconfirmed):
            await manager._commit_and_verify(session, "hung-history")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(cancellations), manager.COMMIT_HISTORY_MAX_ATTEMPTS)
        self.assertEqual(sum(command.startswith("commit comment ") for command in fake.written_commands()), 1)

    async def test_history_query_timeout_covers_hung_readiness(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)
        manager.COMMIT_HISTORY_MAX_ATTEMPTS = 2
        manager.COMMIT_HISTORY_QUERY_TIMEOUT = .01
        manager.COMMIT_HISTORY_RETRY_INTERVAL = 0
        session = manager._console_session(fake)
        cancellations = []

        async def hang():
            try:
                await asyncio.Event().wait()
            finally:
                cancellations.append(True)

        session._ensure_ready = AsyncMock(side_effect=hang)
        session.exchange = AsyncMock(side_effect=AssertionError("query sent before ready"))
        verified = await manager._verify_commit_history(session, "expected")
        self.assertIsNone(verified)
        self.assertEqual(len(cancellations), 2)
        session.exchange.assert_not_awaited()

    async def test_history_timeout_consumes_attempt_and_next_query_can_succeed(self):
        manager = make_manager(FakeJunosTelnet(fragment_size=0))
        manager.COMMIT_HISTORY_MAX_ATTEMPTS = 2
        manager.COMMIT_HISTORY_QUERY_TIMEOUT = .01
        manager.COMMIT_HISTORY_RETRY_INTERVAL = 0
        cancelled = asyncio.Event()
        attempts = []

        async def query(*args):
            attempts.append(True)
            if len(attempts) == 1:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return "expected"

        with patch.object(manager, "_run_history_query", new=AsyncMock(side_effect=query)) as queried:
            verified = await manager._verify_commit_history(None, "expected")
        self.assertTrue(verified)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(queried.await_count, 2)

    async def test_slow_commit_does_not_consume_history_query_timeout(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)
        manager.COMMIT_HISTORY_QUERY_TIMEOUT = .04
        manager.PROMPT_SETTLE = .001
        manager.DRAIN_QUIET = .001
        write = fake.write_line

        async def slow_commit(content, line_ending=None):
            if content.startswith("commit comment "):
                await asyncio.sleep(.06)
            await write(content, line_ending)

        fake.write_line = slow_commit
        session = manager._console_session(fake)
        result = await manager._commit_and_verify(session, "slow-commit")
        self.assertTrue(result)
        self.assertEqual(sum(command.startswith("commit comment ") for command in fake.written_commands()), 1)

    async def test_uuid_in_non_record_text_is_not_success(self):
        fake = FakeJunosTelnet(fragment_size=0)
        expected = "cfg-import-deadbeef"
        fake.history_override = (
            "0 2024-01-01 00:00:00 UTC by root via cli\r\n"
            "another commit\r\n"
            f"echoed text: {expected}\r\n"
            "1 2023-01-01 00:00:00 UTC by root via cli\r\n"
            "old commit\r\n"
        )
        manager = make_manager(fake)

        session = manager._console_session(fake)
        verified = await manager._verify_commit_history(session, expected)

        self.assertFalse(verified)

    async def test_uuid_in_echo_or_old_record_is_not_success(self):
        fake = FakeJunosTelnet(fragment_size=0)
        expected = "cfg-import-deadbeef"
        fake.history_override = (
            "0 2024-01-01 00:00:00 UTC by root via cli\r\n"
            "another commit\r\n"
            "1 2023-01-01 00:00:00 UTC by root via cli\r\n"
            f"{expected}\r\n"
        )
        manager = make_manager(fake)

        session = manager._console_session(fake)
        verified = await manager._verify_commit_history(session, expected)

        self.assertFalse(verified)
        self.assertGreaterEqual(fake.history_query_count, 2)

    async def test_history_comment_with_error_does_not_fail_query(self):
        fake = FakeJunosTelnet(fragment_size=0)
        expected = "cfg-import-good"
        fake.history_override = (
            "0 2024-01-01 00:00:00 UTC by root via cli\r\n"
            f"{expected}\r\n"
            "1 2023-01-01 00:00:00 UTC by root via cli\r\n"
            "old error: previous failure\r\n"
        )
        manager = make_manager(fake)

        session = manager._console_session(fake)
        verified = await manager._verify_commit_history(session, expected)

        self.assertTrue(verified)

    async def test_history_without_records_and_error_is_unconfirmed(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.history_override = "error: permission denied\r\n"
        manager = make_manager(fake)

        session = manager._console_session(fake)
        with self.assertRaises(juniper.JunosResultUnconfirmed):
            await manager._verify_commit_history(
                session, "cfg-import-good"
            )

    async def test_pending_record_is_not_a_completed_commit(self):
        fake = FakeJunosTelnet(fragment_size=0)
        expected = "cfg-import-pending"
        fake.history_override = (
            "0 2024-01-01 00:00:00 UTC by root via cli pending\r\n"
            f"{expected}\r\n"
            "1 2023-01-01 00:00:00 UTC by root via cli\r\n"
            "old commit\r\n"
        )
        manager = make_manager(fake)

        session = manager._console_session(fake)
        verified = await manager._verify_commit_history(session, expected)

        self.assertIsNone(verified)

    async def test_parse_commit_history_uses_first_completed_record(self):
        manager = make_manager(FakeJunosTelnet(fragment_size=0))
        text = (
            "0 2024-01-01 00:00:00 UTC by root via cli pending\r\n"
            "pending comment\r\n"
            "1 2023-01-01 00:00:00 UTC by root via cli\r\n"
            "commit comment: cfg-import-1234\r\n"
        )
        records = manager._parse_commit_history(text)
        self.assertEqual(records[0].comment, "pending comment")
        self.assertTrue(records[0].pending)
        self.assertEqual(records[1].comment, "cfg-import-1234")

    async def test_history_comment_starting_with_bracket_is_preserved(self):
        manager = make_manager(FakeJunosTelnet(fragment_size=0))
        text = (
            "0 2024-01-01 00:00:00 UTC by root via cli\r\n"
            "[legacy] note\r\n"
            "1 2023-01-01 00:00:00 UTC by root via cli\r\n"
            "old commit\r\n"
        )

        records = manager._parse_commit_history(text)

        self.assertEqual(records[0].comment, "[legacy] note")

    async def test_exchange_does_not_send_automatic_enter(self):
        fake = FakeJunosTelnet(fragment_size=0)
        # 做什么：登录提示已经作为初始输入传入。
        # 为什么：需要移除 fake 中重复的那一份，避免重复登录。
        fake.chunks.clear()
        manager = make_manager(fake)
        session = manager._console_session(fake)
        await session.enter_command_prompt("router login: ", timeout=2)
        await session.exchange("top", timeout=0.2)

        self.assertEqual(fake.written_commands(), ["root", "admin123", "cli", "configure", "top"])
        self.assertNotIn("\n", fake.writes)


if __name__ == "__main__":
    unittest.main()
