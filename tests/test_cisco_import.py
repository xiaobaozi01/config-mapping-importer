#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Cisco XRv9k 导入状态机的场景测试。

做什么：用脚本化 XR console 覆盖登录、FTP 准备、load、commit replace、
确认提示和提交记录核实。
为什么：Cisco 的提示符和确认流程与 Juniper 不同，需要独立回归测试。
"""

import asyncio
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT_USERNAME = "Enter root-system username: "
SECRET = "Enter secret: "
SECRET_AGAIN = "Enter secret again: "
EXEC = "RP/0/RP0/CPU0:ios# "
CONFIG = "RP/0/RP0/CPU0:ios(config)# "
CONFIG_IF = "RP/0/RP0/CPU0:ios(config-if)# "
CONFIRM = "Do you wish to proceed with this commit anyway? "


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
        return SimpleNamespace(
            second_cer="secretpw",
            ftp_user="ftpuser",
            ftp_pwd="ftppass",
        )


class _FakeEmulationClass:
    @staticmethod
    def instance():
        return STATE["emulation"]


class FakeNodeCreateInfoFactory:
    def get_create_info(self, _name):
        return SimpleNamespace(
            custom_adapters=[SimpleNamespace(adapter_number=0, port_name="ge-0/0/0")]
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


def _load_cisco_isolated():
    stubs = _build_src_stubs()
    name = "cisco_xrv9k"
    names = list(stubs) + [name]
    originals = {item: sys.modules[item] for item in names if item in sys.modules}
    sys.modules.update(stubs)
    try:
        path = Path(__file__).resolve().parent.parent / "cisco_xrv9k.py"
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


cisco = _load_cisco_isolated()


class FakeCiscoTelnet:
    """脚本化 Cisco IOS XR console。"""

    def __init__(self):
        self.chunks = []
        self.writes = []
        self.connected = True
        self.current_prompt = ROOT_USERNAME
        self.secret_count = 0
        self.history_label = ""
        self.history_override = None
        self.history_override_queries = 0
        self.confirm_commit = False
        self.load_output = None
        self.show_failed_output = ""
        self.commit_after_yes_error = None
        self.command_errors = {}
        self.closed = False
        self.reconnect_count = 0
        self.enqueue(ROOT_USERNAME)

    def enqueue(self, text):
        if text:
            self.chunks.append(text)

    async def read_available(self):
        await asyncio.sleep(0)
        if not self.connected:
            raise ConnectionError("fake console disconnected")
        return self.chunks.pop(0) if self.chunks else ""

    async def write_line(self, content, line_ending=None):
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        self.writes.append(content)
        self._handle(content.strip())

    async def telnet_write(self, content, sleep_time=0):
        await self.write_line(content, b"\r\n")

    def close(self):
        self.closed = True
        self.connected = False

    @property
    def is_connected(self):
        return self.connected

    async def reconnect_async(self):
        self.reconnect_count += 1
        self.connected = True
        self.current_prompt = ROOT_USERNAME
        self.enqueue(ROOT_USERNAME)

    def _handle(self, text):
        if text == "":
            self.enqueue(self.current_prompt)
            return
        if text == "root":
            self.current_prompt = SECRET
            self.enqueue(SECRET)
            return
        if text == "secretpw":
            self.secret_count += 1
            if self.secret_count == 1:
                self.current_prompt = SECRET_AGAIN
                self.enqueue(SECRET_AGAIN)
            else:
                self.current_prompt = EXEC
                self.enqueue(EXEC)
            return
        if text == "configure":
            self.current_prompt = CONFIG
            self.enqueue(CONFIG)
            return
        if text == "end":
            self.current_prompt = EXEC
            self.enqueue(EXEC)
            return
        if text == "yes":
            if self.commit_after_yes_error:
                self.enqueue(self.commit_after_yes_error + "\r\n")
            self.current_prompt = CONFIG
            self.enqueue(CONFIG)
            return
        if text in self.command_errors:
            self.enqueue(text + "\r\n")
            self.enqueue(self.command_errors[text] + "\r\n")
            self.current_prompt = CONFIG
            self.enqueue(CONFIG)
            return
        if text == "show configuration failed load":
            self.enqueue(text + "\r\n")
            if self.show_failed_output:
                self.enqueue(self.show_failed_output)
            self.current_prompt = CONFIG
            self.enqueue(CONFIG)
            return
        if text.startswith("load "):
            self.enqueue(text + "\r\n")
            if self.load_output:
                self.enqueue(self.load_output)
            self.current_prompt = CONFIG
            self.enqueue(CONFIG)
            return
        if text.startswith("commit label "):
            self.enqueue(text + "\r\n")
            label = text.split("commit label ", 1)[1].split()[0]
            self.history_label = label
            if self.confirm_commit:
                self.current_prompt = CONFIRM
                self.enqueue(CONFIRM)
            else:
                self.current_prompt = CONFIG
                self.enqueue(CONFIG)
            return
        if text == "do show configuration commit list":
            self.enqueue(text + "\r\n")
            label = self.history_label
            if (
                self.history_override is not None
                and self.history_override_queries != 0
            ):
                label = self.history_override
                self.history_override_queries -= 1
            self.enqueue("SNo. Label/ID User Line Client Time Stamp\r\n")
            self.enqueue(f"1 {label} root con0_RPs0_C CLI 00:00:00 UTC Mon Jan 01 2024\r\n")
            self.current_prompt = CONFIG
            self.enqueue(CONFIG)
            return
        if text.startswith("interface "):
            self.enqueue(text + "\r\n")
            self.current_prompt = CONFIG_IF
            self.enqueue(CONFIG_IF)
            return
        self.enqueue(text + "\r\n")
        self.current_prompt = CONFIG
        self.enqueue(CONFIG)


def make_manager(fake):
    emulation = STATE["emulation"]
    emulation.deleted = []
    manager = cisco.CiscoIOSXRV9kd7d11d1ConfigManager(
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
    manager.COMMIT_HISTORY_RETRY_INTERVAL = 0.01
    return manager


class CiscoImportScenarioTest(unittest.IsolatedAsyncioTestCase):
    async def test_import_cfg_verifies_commit_label(self):
        fake = FakeCiscoTelnet()
        manager = make_manager(fake)

        await manager.import_cfg("/tmp/cisco.cfg")

        commands = fake.writes
        self.assertIn("configure", commands)
        self.assertIn("logging console disable", commands)
        self.assertIn("ftp client source-interface ge-0/0/0", commands)
        self.assertIn("interface ge-0/0/0", commands)
        self.assertIn("ipv4 address 10.0.0.2 255.255.255.0", commands)
        self.assertIn("no shutdown", commands)
        self.assertIn("load ftp://ftpuser:ftppass@10.0.0.1/node-1.cfg", commands)
        self.assertIn("do show configuration commit list", commands)
        commit_commands = [item for item in commands if item.startswith("commit label ")]
        self.assertEqual(len(commit_commands), 1)
        self.assertIn(fake.history_label, commit_commands[0])
        self.assertLessEqual(len(fake.history_label), 20)
        self.assertTrue(fake.history_label.startswith("ci-"))
        self.assertEqual(STATE["emulation"].deleted, [("project-1", "ftp-node")])
        self.assertTrue(fake.closed)
        self.assertIn(("loopback_last_adapter_for_uni",), manager.parent_calls)

    async def test_commit_replace_handles_confirmation(self):
        fake = FakeCiscoTelnet()
        fake.confirm_commit = True
        manager = make_manager(fake)

        await manager.import_cfg("/tmp/cisco.cfg")

        self.assertIn("yes", fake.writes)

    async def test_commit_history_retries_until_label_matches(self):
        fake = FakeCiscoTelnet()
        fake.history_override = "old-label"
        fake.history_override_queries = 1
        manager = make_manager(fake)

        await manager.import_cfg("/tmp/cisco.cfg")

        self.assertEqual(
            fake.writes.count("do show configuration commit list"),
            2,
        )

    async def test_commit_label_mismatch_fails(self):
        fake = FakeCiscoTelnet()
        fake.history_override = "other-label"
        fake.history_override_queries = 2
        manager = make_manager(fake)

        with self.assertRaises(cisco.CiscoCommitVerificationError):
            await manager.import_cfg("/tmp/cisco.cfg")

        self.assertTrue(fake.closed)

    async def test_command_error_stops_import(self):
        fake = FakeCiscoTelnet()
        fake.command_errors["load ftp://ftpuser:ftppass@10.0.0.1/node-1.cfg"] = "% Invalid input"
        manager = make_manager(fake)

        with self.assertRaises(cisco.CiscoCommandError):
            await manager.import_cfg("/tmp/cisco.cfg")

        self.assertTrue(fake.closed)

    async def test_load_syntax_failure_from_show_failed_load(self):
        fake = FakeCiscoTelnet()
        fake.show_failed_output = "!! CONFIGURATION FAILED DUE TO SYNTAX ERRORS\r\n"
        manager = make_manager(fake)

        with self.assertRaises(cisco.CiscoCommandError):
            await manager.import_cfg("/tmp/cisco.cfg")

        self.assertNotIn("commit label", " ".join(fake.writes))

    async def test_load_file_error_fails(self):
        fake = FakeCiscoTelnet()
        fake.load_output = "config: failed to open input file\r\n"
        manager = make_manager(fake)

        with self.assertRaises(cisco.CiscoCommandError):
            await manager.import_cfg("/tmp/cisco.cfg")

    async def test_commit_error_after_confirm_yes_fails(self):
        fake = FakeCiscoTelnet()
        fake.confirm_commit = True
        fake.commit_after_yes_error = (
            "% Failed to commit one or more configuration items. "
            "Please use 'show configuration failed' to view the errors"
        )
        manager = make_manager(fake)

        with self.assertRaises(cisco.CiscoCommandError):
            await manager.import_cfg("/tmp/cisco.cfg")

        self.assertIn("yes", fake.writes)

    async def test_load_stage_does_not_use_commit_errors(self):
        fake = FakeCiscoTelnet()
        fake.load_output = "random text: % Failed to commit one or more configuration items\r\n"
        manager = make_manager(fake)

        await manager.import_cfg("/tmp/cisco.cfg")

        self.assertTrue(fake.closed)

    async def test_official_xr_log_matches_background_pattern(self):
        line = (
            "RP/0/RP0/CPU0:Feb 21 04:42:57.017 : config[65689]: "
            "%MGBL-LIBTARCFG-6-COMMIT : Configuration committed"
        )
        self.assertIsNotNone(cisco.CISCO_BACKGROUND_PATTERN.search(line))
        self.assertIsNone(cisco.CISCO_BACKGROUND_PATTERN.search("% Invalid input detected"))
        self.assertIsNone(cisco.CISCO_BACKGROUND_PATTERN.search("% Authorization failed"))

    async def test_init_device_sets_hostname(self):
        fake = FakeCiscoTelnet()
        manager = make_manager(fake)

        await manager.init_device(fake, "router1")

        self.assertIn("hostname router1", fake.writes)
        self.assertIn("commit", fake.writes)


if __name__ == "__main__":
    unittest.main()
