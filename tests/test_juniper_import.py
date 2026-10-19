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
                    "system {\r\n"
                    '    root-authentication {\r\n'
                    '        encrypted-password "hash";\r\n'
                    "    }\r\n"
                    "}\r\n"
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

    manager.OPERATION_TIMEOUT = 5
    manager.READ_INTERVAL = 0.001
    manager.PROMPT_SETTLE = 0.01
    manager.DRAIN_QUIET = 0.005
    manager.DRAIN_MAX = 0.02
    manager.BOOT_READY_SETTLE = 0.01
    manager.BOOT_NUDGE_INTERVAL = 0.05
    manager.COMMIT_LOCK_RETRY_ATTEMPTS = 3
    manager.COMMIT_LOCK_RETRY_INTERVAL = 0.01
    manager.COMMIT_HISTORY_RETRY_INTERVAL = 0.01
    manager.COMMIT_HISTORY_MISMATCH_LIMIT = 2
    return manager


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
        self.assertEqual(len(commit_commands), 4)
        comments = [re.search(r'"(cfg-import-[^"]+)"', item).group(1) for item in commit_commands]
        self.assertEqual(len(set(comments)), 4)
        self.assertEqual(fake.history[0][0], comments[-1])

        self.assertEqual(STATE["emulation"].deleted, [("project-1", "ftp-node")])
        self.assertTrue(fake.closed)
        self.assertIn(("loopback_last_adapter_for_uni",), calls)

    def test_status_enums_are_stable_strings(self):
        """验证状态枚举仍与旧字符串完全兼容。"""
        self.assertEqual(juniper.LoadStatus.FAILED, "failed")
        self.assertEqual(juniper.CommitDecision.COMPLETE, "complete")
        self.assertEqual(str(juniper.HistoryDecision.MATCH), "match")
        self.assertEqual(str(juniper.HistoryMatch.WAIT), "wait")

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
        self.assertEqual(fake.commit_count, 5)

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
        self.assertEqual(fake.commit_count, 4)

    async def test_root_auth_probe_error_stops_import(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.command_errors[juniper.ROOT_AUTH_PROBE] = "error: permission denied"
        manager = make_manager(fake)

        with self.assertRaises(juniper.JunosCommandError):
            await manager.import_cfg("/tmp/adapted.cfg")

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
        self.assertEqual(len(commit_commands), 4)

    async def test_preparation_error_continues_when_prompt_returns(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.command_errors["delete chassis auto-image-upgrade"] = "error: statement not found"
        manager = make_manager(fake)

        with self.assertLogs("juniper_vjunos_vmx", level="WARNING") as logs:
            await manager.import_cfg("/tmp/adapted.cfg")

        self.assertTrue(any("explicitly failed" in line for line in logs.output))
        self.assertEqual(fake.load_count, 1)
        commit_commands = [
            item for item in fake.written_commands() if item.startswith("commit comment ")
        ]
        # 做什么：delete 失败不提交，后续端口、IP 和最终提交仍然执行。
        self.assertEqual(len(commit_commands), 3)

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

    async def test_export_cfg_creates_empty_file(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "out.cfg")
            await manager.export_cfg(path)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(os.path.getsize(path), 0)

    async def test_commit_lock_retry_uses_same_uuid(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.lock_commits = 1
        manager = make_manager(fake)

        session = manager._console_session(fake)
        comment = await manager._commit_and_verify(session, "lock-test", timeout=5)

        commit_commands = [
            item for item in fake.written_commands() if item.startswith("commit comment ")
        ]
        self.assertEqual(len(commit_commands), 2)
        self.assertEqual(commit_commands[0], commit_commands[1])
        self.assertIn(comment, commit_commands[0])
        self.assertEqual(fake.history[0][0], comment)

    async def test_persistent_commit_lock_fails_after_retry_limit(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.lock_commits = 10
        manager = make_manager(fake)
        session = manager._console_session(fake)

        with self.assertRaises(juniper.JunosCommandError) as raised:
            await manager._commit_and_verify(session, "persistent-lock", timeout=5)

        self.assertIn("configuration database locked", str(raised.exception))
        commit_commands = [
            item for item in fake.written_commands() if item.startswith("commit comment ")
        ]
        self.assertEqual(len(commit_commands), 4)
        self.assertEqual(fake.history_query_count, 0)

    async def test_commit_rejects_exhausted_budget(self):
        fake = FakeJunosTelnet(fragment_size=0)
        manager = make_manager(fake)
        session = manager._console_session(fake)

        with self.assertRaises(juniper.JunosResultUnconfirmed):
            await manager._commit_and_verify(session, "no-budget", timeout=0)

        self.assertEqual(fake.commit_count, 0)

    async def test_commit_sent_result_lost_queries_history_before_resend(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.commit_login = True
        manager = make_manager(fake)

        session = manager._console_session(fake)
        comment = await manager._commit_and_verify(session, "lost-result", timeout=5)

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
            await manager._commit_and_verify(session, "lost-result", timeout=5)

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
            await manager._commit_and_verify(session, "mismatch", timeout=5)

    async def test_explicit_commit_failure_is_not_retried(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.commit_error = "error: commit failed"
        manager = make_manager(fake)

        with self.assertRaises(juniper.JunosCommandError):
            session = manager._console_session(fake)
            await manager._commit_and_verify(session, "commit-error", timeout=5)

        commit_commands = [
            item for item in fake.written_commands() if item.startswith("commit comment ")
        ]
        self.assertEqual(len(commit_commands), 1)

    async def test_commit_complete_with_trailing_error_verifies_history(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.commit_complete_trailing = "error: unrelated background"
        manager = make_manager(fake)

        session = manager._console_session(fake)
        comment = await manager._commit_and_verify(session, "complete-with-error", timeout=5)

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
        deadline = asyncio.get_running_loop().time() + 0.5
        verified = await manager._verify_commit_history(session, expected, deadline)

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
        deadline = asyncio.get_running_loop().time() + 0.5
        verified = await manager._verify_commit_history(session, expected, deadline)

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
        deadline = asyncio.get_running_loop().time() + 0.5
        verified = await manager._verify_commit_history(session, expected, deadline)

        self.assertTrue(verified)

    async def test_history_without_records_and_error_is_unconfirmed(self):
        fake = FakeJunosTelnet(fragment_size=0)
        fake.history_override = "error: permission denied\r\n"
        manager = make_manager(fake)

        session = manager._console_session(fake)
        deadline = asyncio.get_running_loop().time() + 0.5
        verified = await manager._verify_commit_history(
            session, "cfg-import-good", deadline
        )

        self.assertIsNone(verified)

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
        deadline = asyncio.get_running_loop().time() + 0.5
        verified = await manager._verify_commit_history(session, expected, deadline)

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
