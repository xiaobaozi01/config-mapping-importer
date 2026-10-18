#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2024. All rights reserved.
import asyncio
import logging

from src.application.config.manager.ne_config_manager import NeConfigManager
from src.common.config import AppSettings
from src.common.constant import EMULATION_CLASS
from src.emulation.emulation_platform import EmulationPlatform
from src.schemas.emulation.nodes import NodeCreateInfoFactory

log = logging.getLogger(__name__)

emulation_instance: EmulationPlatform = EMULATION_CLASS.instance()
app_settings = AppSettings.load_from_yaml()

class CiscoIOSXRV9kd7d11d1ConfigManager(NeConfigManager):
    DEFAULT_PASSWD = app_settings.second_cer
    FTP_USER = app_settings.ftp_user
    FTP_PWD = app_settings.ftp_pwd

    def __init__(self, emulation, project_id, node_id, host_located, node_compute_id=None):
        super().__init__(emulation, project_id, node_id, host_located, node_compute_id)
        self.node_info = NodeCreateInfoFactory().get_create_info("cisco xrv9k")

    async def init_telnet_client(self):
        telnet_client = await self.get_telnet_client()
        await self.test_telnet_connect(telnet_client)

        while True:
            await asyncio.sleep(1)
            await telnet_client.telnet_line_break()
            output = await telnet_client.telnet_read()
            log.debug(f"Cmd exec Output:{output}")
            if "Enter root-system username:" in output:
                await telnet_client.telnet_write("root")
                await telnet_client.telnet_write(self.DEFAULT_PASSWD)
                await telnet_client.telnet_write(self.DEFAULT_PASSWD)
                break
            if output.startswith("RP/0/RP0/CPU0:") and "#" in output:
                break
            elif "Username" in output:
                await telnet_client.telnet_write(f"root\r{self.DEFAULT_PASSWD}")
                await telnet_client.telnet_write("\r")
                break
        return telnet_client

    async def __import_command(self, telnet_client):
        while True:
            await telnet_client.telnet_line_break()
            output = await telnet_client.telnet_read()
            if "Enter root-system username:" in output:
                await telnet_client.telnet_write("root")
                await telnet_client.telnet_write(self.DEFAULT_PASSWD)
                await telnet_client.telnet_write(self.DEFAULT_PASSWD)
                continue
            elif "Username" in output:
                await telnet_client.telnet_write(f"root\r{self.DEFAULT_PASSWD}")
                await telnet_client.telnet_write("\r")
            elif output.startswith("RP/0/RP0/CPU0:") and "#" in output:
                # 退出config状态
                if "config" in output:
                    await telnet_client.telnet_write("commit")
                    await telnet_client.telnet_write("end")
                else:
                    await self._import_config_from_ftpserver(telnet_client)
                    break

    async def _import_config_from_ftpserver(self, telnet_client):
        await telnet_client.telnet_write("config")
        # 关闭日志打印
        await telnet_client.telnet_write("no logging console\r")
        await telnet_client.telnet_write("commit")

        await telnet_client.telnet_write(
            f"ftp client source-interface {self.node_info.custom_adapters[0].port_name}")
        await telnet_client.telnet_write(f"int {self.node_info.custom_adapters[0].port_name}")
        await telnet_client.telnet_write(
            f"ip address {self.FTP_CLIENT_IP} 255.255.255.0")
        await telnet_client.telnet_write("shutdown")
        await telnet_client.telnet_write("commit")
        await self._handle_commit(telnet_client)
        await telnet_client.telnet_write("no shutdown")
        await telnet_client.telnet_write("commit")
        await self._handle_commit(telnet_client)
        await telnet_client.telnet_write("end")
        count = 0
        while True:
            count += 1
            await asyncio.sleep(1)
            output = await telnet_client.telnet_read()
            if "Uncommitted changes found, commit them before exiting" in output:
                # 这里为了能够重复导入调试，输入no，以免上一次不可提交的配置在这里造成死循环
                await telnet_client.telnet_write("no")
                break
            if count > 10:
                break

        await telnet_client.telnet_write("config")
        log.info(
            f"load ftp://{self.FTP_USER}:{self.FTP_PWD}@{self.FTP_SERVER_IP}/{self._node_id}.cfg")
        await telnet_client.telnet_write(
            f"load ftp://{self.FTP_USER}:{self.FTP_PWD}@{self.FTP_SERVER_IP}/{self._node_id}.cfg")
        await self._handle_load_config(telnet_client)
        await telnet_client.telnet_write("commit replace")
        # 这里不基于回显做判断 而是等待2秒敲入yes
        await asyncio.sleep(2)
        await telnet_client.telnet_write("yes")
        await self._handle_import_config(telnet_client)

    async def _handle_import_config(self, telnet_client):
        count = 0
        while True:
            count += 1
            await asyncio.sleep(2)
            await telnet_client.telnet_line_break()
            output = await telnet_client.telnet_read()
            if "Failed to commit one or more configuration items during a pseudo-atomic operation. All changes made have been reverted" in output:
                raise Exception(f"{self._node_id} config: failed to commit")
            # 当跳出commit日志，且hostname已经改变，则认为已经成功
            if "RP/0/RP0/CPU0:" in output and "ios" not in output and output.endswith("#"):
                log.info(f"{self._node_id} configuration import command has been executed.")
                await telnet_client.telnet_line_break()
                await telnet_client.telnet_write("end")
                break
            if count > 30:
                raise Exception(f"{self._node_id} import config timeout !")

    async def _handle_load_config(self, telnet_client):
        count = 0
        while True:
            count += 1
            await asyncio.sleep(1)
            output = await telnet_client.telnet_read()
            if "conflicts with other IPv4 addresses and has been forced down" in output:
                raise Exception(
                    f"{self._node_id} conflicts with other IPv4 addresses and has been forced down!")
            if "Resource temporarily unavailable" in output or "config: failed to open input file" in output:
                raise Exception(f"{self._node_id} config: failed to open input file!")
            if "Syntax/Authorization errors in one or more commands." in output:
                logging.info(f"{self._node_id} load config failed ,check Syntax/Authorization errors !")
            if "RP/0/RP0/CPU0:" in output and "#" in output:
                break
            if count > 30:
                raise Exception(f"{self._node_id} load config timeout !")

    async def _handle_commit(self, telnet_client):
        count = 0
        while count <= 30:
            await asyncio.sleep(1)
            output = await telnet_client.telnet_read()
            if "Do you wish to proceed with this commit anyway?" in output:
                await telnet_client.telnet_write("yes")
                break
            if output.startswith("RP/0/RP0/CPU0:") and "#" in output:
                break
            count += 1

    async def import_cfg(self, local_cfg_path):
        telnet_client = await self.init_telnet_client()
        if not telnet_client:
            raise Exception(f"{self._node_id} init telnet client failed!")
        ftpserver = await self.init_ftp_server()
        try:
            await self.copy_file_to_ftp_server(ftpserver, local_cfg_path)
            await self.link_ftp_server(ftpserver, self.node_info.custom_adapters[0].adapter_number)
            await self.__import_command(telnet_client)
        finally:
            # 销毁临时节点
            await emulation_instance.delete_node(
                self._project_id,
                ftpserver.node_id
            )
            telnet_client.close()
            await self.loopback_last_adapter_for_uni()
        pass

    async def init_device(self, telnet_client, sys_name:str):
        await telnet_client.telnet_line_break()
        output = await telnet_client.telnet_read()
        if "Username" in output:
            await telnet_client.telnet_write(f"root\r{self.DEFAULT_PASSWD}")
            await telnet_client.telnet_write("\r")
        output = await telnet_client.telnet_read()
        if output.startswith("RP/0/RP0/CPU0:") and "#" in output:
            await telnet_client.telnet_write("config")
            await telnet_client.telnet_write(f"hostname {sys_name}")
            await telnet_client.telnet_write("commit")
            await telnet_client.telnet_write("end")

    def export_cfg(self, local_cfg_path):
        pass