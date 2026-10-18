#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2024. All rights reserved.

import asyncio
import inspect
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from src.application.config.manager.ne_config_manager import NeConfigManager
from src.common.config import AppSettings
from src.common.constant import EMULATION_CLASS
from src.emulation.emulation_platform import EmulationPlatform
from src.schemas.emulation.nodes import NodeCreateInfoFactory

log = logging.getLogger(__name__)

emulation_instance: EmulationPlatform = EMULATION_CLASS.instance()
app_settings = AppSettings.load_from_yaml()


@dataclass
class ResponseHandler:
    """正则匹配设备回显：action 返回 True 时结束等待，返回 False 时继续等待。"""
    pattern: re.Pattern
    action: Callable[[str], bool | None | Awaitable[bool | None]]


class JuniperVjunosRouter23d2R1d15dConfigManager(NeConfigManager):
    DEFAULT_USER = "root"
    DEFAULT_PASSWD = "admin123"

    LOGIN_TIMEOUT = 300

    # 登录、Shell、操作模式、配置模式提示符
    LOGIN_PATTERN = re.compile(r"(?im)^(?!Last[ \t]+login:)(?:[^\r\n]*[ \t]+)?login:[ \t]*\Z")
    PASSWORD_PATTERN = re.compile(r"(?im)^(?:root[ \t]+)?password:[ \t]*\Z")
    SHELL_PATTERN = re.compile( r"(?m)^[^@\s:#]+(?:@[^@\s:#]*)?:\S+[ \t]*#[ \t]*\Z")
    CLI_PATTERN = re.compile(r"(?m)^(?:\S+@\S*|[^@\s>]+)>[ \t]*\Z")
    EDIT_PATTERN = re.compile(r"(?m)^[^@\s:#]+(?:@[^@\s:#]*)?#[ \t]*\Z")

    # 命令执行结果
    COMMIT_SUCCESS_PATTERN = re.compile( r"(?im)^[ \t]*commit complete[ \t]*\r?$")
    LOAD_SUCCESS_PATTERN = re.compile(r"(?im)^[ \t]*load complete(?:[ \t]*\(0 errors?\))?[ \t]*\r?$")
    LOAD_FAILED_PATTERN = re.compile(r"(?im)^[ \t]*load complete[ \t]*\([1-9]\d*[ \t]+errors?\)[ \t]*\r?$")
    ERROR_PATTERN = re.compile( r"(?im)^[ \t]*(?:error:|syntax error\b|commit failed\b)")

    def __init__(self, emulation, project_id, node_id, host_located, node_compute_id=None):
        super().__init__(emulation, project_id, node_id, host_located, node_compute_id)
        self.node_info = NodeCreateInfoFactory().get_create_info("juniper vjunos")

    async def init_telnet_client(self):
        telnet_client = await self.get_telnet_client()
        if telnet_client is None:
            raise ConnectionError(f"{self._node_id} Telnet console is not available")
        await self.test_telnet_connect(telnet_client)
        return telnet_client

    async def import_cfg(self, local_cfg_path):
        telnet_client = None
        ftp_server = None
        try:
            telnet_client = await self.init_telnet_client()
            await self.__wait_for_boot_prompt(telnet_client)
            await self.__enter_junos_edit(telnet_client)

            ftp_server = await self.init_ftp_server()
            await self.copy_file_to_ftp_server(ftp_server, local_cfg_path)
            await self.link_ftp_server(ftp_server, self.node_info.custom_adapters[-2].adapter_number)
            await self.__import_command(telnet_client)
        finally:
            if ftp_server is not None:
                await emulation_instance.delete_node(self._project_id, ftp_server.node_id)
            if telnet_client is not None:
                telnet_client.close()
            await self.loopback_last_adapter_for_uni()

    async def init_device(self, telnet_client, sys_name: str):
        await self.__wait_for_boot_prompt(telnet_client)
        await self.__enter_junos_edit(telnet_client)
        output = await self.__execute_config_command(telnet_client, "top", 30)
        self.__check_command_error("top", output)
        await self.__execute_and_commit(telnet_client, "delete chassis auto-image-upgrade", 30)
        await self.__execute_and_commit(telnet_client, f"set system host-name {sys_name}", 30)
        await self.__execute_and_commit(telnet_client, "set chassis fpc 0 pic 0 number-of-ports 40", 30)

    async def export_cfg(self, local_cfg_path):
        with open(local_cfg_path, "w", encoding="utf-8") as fw:
            fw.write("")

    async def __import_command(self, telnet_client):
        log.info("Start import Junos configuration: node=%s", self._node_id)
        # import_cfg() 已在导入前确认设备进入了 [edit]。
        output = await self.__execute_config_command(telnet_client, "top", 30)
        self.__check_command_error("top", output)

        await self.__execute_and_commit(telnet_client, "delete chassis auto-image-upgrade", 30)

        port_count = len(self.node_info.custom_adapters) - 1
        await self.__execute_and_commit(telnet_client, f"set chassis fpc 0 pic 0 number-of-ports {port_count}", 30)

        interface = self.node_info.custom_adapters[-2].port_name
        await self.__execute_and_commit(telnet_client, f"set interfaces {interface} unit 0 family inet address {self.FTP_CLIENT_IP}/24", 30)

        await self.__load_config(telnet_client)
        log.info("Junos configuration imported: node=%s", self._node_id)

    async def __wait_for_boot_prompt(self, telnet_client):
        """等待设备输出登录或命令提示符；不负责输入账号、密码或切换模式。"""
        ready_patterns = (
            self.LOGIN_PATTERN,
            self.PASSWORD_PATTERN,
            self.SHELL_PATTERN,
            self.CLI_PATTERN,
            self.EDIT_PATTERN
        )
        fips_detected = False

        try:
            async for _, output in self.__iter_telnet_output(telnet_client, self.LOGIN_TIMEOUT):
                if any(pattern.search(output) for pattern in ready_patterns):
                    return

                if not fips_detected and "FIPS Self-tests for RSA" in output:
                    fips_detected = True
                    log.info("Junos FIPS self-test detected: node=%s", self._node_id)
                    await asyncio.sleep(60)
        except TimeoutError as exc:
            raise TimeoutError( f"{self._node_id} Junos boot prompt timeout; {exc}") from exc

    async def __enter_junos_edit(self, telnet_client):
        """从登录、Shell 或操作模式进入 [edit]；不负责启动检测或配置提交。"""
        login_attempts = 0
        last_output = ""

        try:
            async for buffer, _ in self.__iter_telnet_output(telnet_client, self.LOGIN_TIMEOUT):
                last_output = buffer
                if self.EDIT_PATTERN.search(buffer):
                    return

                if self.LOGIN_PATTERN.search(buffer):
                    login_attempts += 1
                    if login_attempts > 5:
                        raise RuntimeError(f"{self._node_id} Junos login failed too many times")
                    await telnet_client.telnet_write(self.DEFAULT_USER)
                elif self.PASSWORD_PATTERN.search(buffer):
                    await telnet_client.telnet_write(self.DEFAULT_PASSWD)
                elif self.SHELL_PATTERN.search(buffer):
                    await telnet_client.telnet_write("cli")
                elif self.CLI_PATTERN.search(buffer):
                    await telnet_client.telnet_write("edit")
        except TimeoutError as exc:
            raise TimeoutError( f"{self._node_id} Junos enter [edit] timeout; buffer={last_output[-3000:]!r}") from exc

    async def __execute_config_command(self, telnet_client, command, timeout):
        """发送一条配置命令，并等待配置模式提示符重新出现。"""
        log.info("Junos start execute: %s", command)
        await telnet_client.telnet_write(command)
        handlers = [
            ResponseHandler(self.EDIT_PATTERN, lambda _: True),
            ResponseHandler(self.ERROR_PATTERN, lambda output: self.__check_command_error(command, output)),
        ]
        result = await self.__wait_for_output(command, telnet_client, handlers, timeout)
        log.info("Junos end execute: %s", command)
        return result

    async def __execute_and_commit(self, telnet_client, command, timeout=120):
        """执行一条配置变更命令，检查结果并提交。"""
        output = await self.__execute_config_command(telnet_client, command, timeout)
        self.__check_command_error(command, output)
        await self.__commit(telnet_client)

    async def __commit(self, telnet_client):
        # commit 只发送一次
        command = "commit"

        log.info("Junos start execute: %s", command)
        await telnet_client.telnet_write(command)
        # 循环读取 commit 回显
        await self.__wait_for_output(
            command,
            telnet_client,
            [
                ResponseHandler(self.ERROR_PATTERN, lambda output: self.__check_command_error(command, output)),
                ResponseHandler(self.COMMIT_SUCCESS_PATTERN, lambda _: True),
                ResponseHandler(self.LOGIN_PATTERN, lambda _: True),
            ],
            120
        )
        log.info("Junos end execute: %s", command)

    def __check_command_error(self, command, output):
        if self.ERROR_PATTERN.search(output):
            raise RuntimeError(f"{self._node_id} Junos command failed ({command}):\n{output[-4000:]}")

    async def __load_config(self, telnet_client):
        command = f"load override ftp://ftpuser:ftpuser@{self.FTP_SERVER_IP}/{self._node_id}.cfg"
        output = await self.__execute_config_command(telnet_client, command, timeout=300)
        if self.LOAD_FAILED_PATTERN.search(output) or self.ERROR_PATTERN.search(output):
            raise RuntimeError(f"{self._node_id} Junos load failed:\n{output[-4000:]}")

        await self.__commit(telnet_client)

    async def __wait_for_output(self, command, telnet_client, handlers: list[ResponseHandler], timeout):
        try:
            async for output, _ in self.__iter_telnet_output(telnet_client, timeout):
                for handler in handlers:
                    if not handler.pattern.search(output):
                        continue

                    result = handler.action(output)
                    if inspect.isawaitable(result):
                        result = await result
                    if result:
                        return output
        except TimeoutError as exc:
            raise TimeoutError(f"Wait {command} response timeout; {exc}") from exc

    async def __iter_telnet_output(self, telnet_client, timeout, line_break_interval=5.0):
        """持续读取并累计 Telnet 回显，空闲时定期发送回车。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_receive_time = loop.time()
        last_break_time = loop.time()
        buffer = ""

        while loop.time() < deadline:
            output = await telnet_client.fixed_telnet_read()

            if output:
                buffer += output
                last_receive_time = loop.time()
                yield self.__normalize_telnet_output(buffer), self.__normalize_telnet_output(output)

            now = loop.time()
            if now - last_receive_time >= line_break_interval and now - last_break_time >= line_break_interval:
                await telnet_client.telnet_line_break()
                last_break_time = loop.time()

            await asyncio.sleep(1)
        raise TimeoutError(f"Telnet response timeout; output={buffer[-3000:]!r}")

    @staticmethod
    def __normalize_telnet_output(buffer: str) -> str:
        """规范化 Telnet 回显，过滤 Auto Image Upgrade 日志。"""
        normalized = buffer.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.rstrip("\n")