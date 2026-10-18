#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2024. All rights reserved.
import asyncio
import logging
import os
import re
import time
import urllib
import uuid
from abc import ABC, abstractmethod

from src.common.config import AppSettings
from src.emulation import get_compute_by_partitioner
from src.emulation.impl.gns3.gns3 import GNS3
from src.schemas.emulation.nodes import NodeStatus, NodeType
from src.schemas.emulation.projects import ProjectStatus
from src.utils.ssh_utils import SshUtil
from src.utils.telnet_utils import TelnetClient

log = logging.getLogger(__name__)

qemu_monitor_pattern = re.compile(r"-monitor tcp:\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:(\d+)")
app_settings = AppSettings.load_from_yaml()
is_op = app_settings.is_op_scenario

class NeConfigManager(ABC):
    FTP_SERVER_IP = "10.0.0.1"
    FTP_CLIENT_IP = "10.0.0.2"

    def __init__(self, emulation, project_id, node_id, host_located, node_compute_id=None):
        self._emulation: GNS3 = emulation
        self._project_id = project_id
        self._node_id = node_id
        self._ftpserver_link_id = None
        self._host_located = host_located
        self._telnet_client = None
        self._node_compute_id = node_compute_id

    def get_project_id(self):
        return self._project_id

    def get_node_id(self):
        return self._node_id

    @abstractmethod
    def import_cfg(self, local_cfg_path):
        pass

    @abstractmethod
    def export_cfg(self, local_cfg_path):
        pass

    async def init_ftp_server(self):
        if self._node_compute_id:
            compute_id = self._node_compute_id
        else:
            compute_id = await get_compute_by_partitioner(self._emulation, str(uuid.uuid4()))

        project = await self._emulation.get_project(self._project_id)
        if project.status != ProjectStatus.opened:
            await self._emulation.open_project(self._project_id)

        ftpserver = await self._emulation.create_node(self._project_id, compute_id, uuid.uuid4(),
                                                      "ftpserver", "ftpserver")
        try:
            host, port = ftpserver.console_host, ftpserver.console
            log.info(
                f"Created ftpserver for project {self._project_id} in {host} telnet port {port}.")

            await self._emulation.start_node(self._project_id, ftpserver.node_id)
            telnet_client = TelnetClient(host, port)
            try:
                await telnet_client.telnet_write(f"ifconfig eth0 {self.FTP_SERVER_IP} netmask 255.255.255.0")
            finally:
                telnet_client.close()
        except Exception as ex:
            log.error(f"Failed to init ftpserver, cleaning up: {ex}")
            await self._emulation.delete_node(self._project_id, ftpserver.node_id)
            raise
        return ftpserver

    async def copy_file_to_ftp_server(self, ftpserver, local_cfg_path):
        host, port = ftpserver.console_host, ftpserver.console

        config_file_name = f"{self._node_id}.cfg"

        await self.import_file_to_node(ftpserver.node_id, local_cfg_path, f"/data/ftpuser/{config_file_name}")

        telnet_client = TelnetClient(host, port)

        try:
            await telnet_client.telnet_write(f"chown ftpuser:ftpgroup /data/ftpuser/{config_file_name}")
            await telnet_client.telnet_write(f"chmod 775 /data/ftpuser/{config_file_name}")
            log.info(f"Copy config file {local_cfg_path} to ftp server successfully.")
        except Exception as ex:
            log.error(f"Copy file to node {self._node_id} error", ex)
        finally:
            telnet_client.close()

    async def import_file_to_node(self, node_id, local_file_path, target_path):
        if not os.path.exists(local_file_path):
            raise Exception(f"Local file not found {local_file_path}")
        encoded_path = urllib.parse.quote(target_path, safe='')
        with open(local_file_path, "rb") as file:
            cfg_file_content = file.read()
            await self._emulation.upload_file(self._project_id, node_id, encoded_path,
                                              cfg_file_content)

    async def link_ftp_server(self, ftpserver, adapter_number):
        log.info(f"Link ftp server {ftpserver.node_id} to node {self._node_id} adapter {adapter_number}.")
        link = await self._emulation.create_link(self._project_id, uuid.uuid4(),
                                                 ftpserver.node_id,
                                                 0, self._node_id, adapter_number)
        self._ftpserver_link_id = link.link_id

    async def relink_ftp_server(self, ftpserver, adapter_number):
        log.info(f"ReLink ftp server {ftpserver.node_id} to node {self._node_id} adapter {adapter_number}.")
        await self._emulation.delete_link(self._project_id, self._ftpserver_link_id)
        await asyncio.sleep(1)
        await self.link_ftp_server(ftpserver, adapter_number)

    async def get_telnet_client(self):
        if self._telnet_client and self._telnet_client.is_connected:
            return self._telnet_client

        node = await self._emulation.get_node(self._project_id, self._node_id)
        if node.status != NodeStatus.started:
            raise Exception(f"The node {node.node_id} is not started, and the configuration cannot be imported.")
        host = node.console_host
        port = node.console

        log.info(f"Get telnet client, Node id {self._node_id}, ip {host}, port {port}.")

        self._telnet_client = TelnetClient(host, port)
        return self._telnet_client

    async def test_telnet_connect(self, telnet_client):
        output_test_retry_times = 0
        await telnet_client.telnet_line_break()
        await telnet_client.telnet_enter_key()
        try_reset = False
        while True:
            output = await telnet_client.telnet_read()
            log.debug(
                f"{self._project_id} {self._node_id} {telnet_client.telnet_address()} config import Output: {output}")
            if output:
                log.info(f"The {self._node_id} terminal test success.")
                return
            else:
                output_test_retry_times += 1

                await asyncio.sleep(1)
                telnet_client.reconnect()
                await telnet_client.telnet_line_break()
                await telnet_client.telnet_enter_key()

                if output_test_retry_times > 120:
                    log.error(
                        f"The {self._node_id} terminal attempts to use the service for more than 120 times exceeds "
                        f"120s. The Telnet server may be faulty. Please check.")

                    if not try_reset:
                        # 两分钟之后还没法敲命令，尝试重置一下终端
                        try_reset = True
                        log.info(f"Try reset {self._node_id} terminal")
                        await self.reset_node_ws()
                        telnet_client.reconnect()
                        output_test_retry_times = 0
                        continue

                    telnet_client.close()
                    break

        raise Exception(f"The {self._node_id} terminal connects failed.")

    async def reset_node_ws(self):
        await self._emulation.refresh_node_console(self._project_id, self._node_id)

    async def loopback_last_adapter_for_uni(self):
        """
        为每个设备的最后一个端口执行自环操作用于支撑uni端口的压缩
        """
        node = await self._emulation.get_node(self._project_id, self._node_id)
        if node.node_type == NodeType.qemu:
            node_command_line = node.command_line
            host, port = (await self._emulation.get_computes())[node.compute_id]
            last_adapter_number = len(node.custom_adapters) - 1

            result = qemu_monitor_pattern.search(node_command_line)
            if result:
                monitor_port = result.group(1)
                cmd = f"""(echo "set_link gns3-{last_adapter_number} on" && sleep 0.1) | telnet 127.0.0.1 {monitor_port}"""
                if is_op:
                    # 使用物理机的qemu执行命令
                    ssh = SshUtil(host)
                    ssh.exec_cmd(cmd)
                    log.info(f"set {self._node_id} uni up successfully")
                else:
                    telnet_client = TelnetClient(host, port)
                    await telnet_client.telnet_write(cmd)
            else:
                log.warning("Qemu command line without monitor setting.")

    async def write_when(self, telnet_client, command, echo, interval, max_retries):
        await telnet_client.telnet_write(command)
        output = await telnet_client.telnet_read()
        retries = 0
        while retries < max_retries and echo in output:
            log.warning(f"retry command: {command}, retry times: {retries}")
            await asyncio.sleep(interval)
            await telnet_client.telnet_write(command)
            output = await telnet_client.telnet_read()
            retries += 1

        if retries == max_retries:
            log.warning(f"Command '{command}' failed after {max_retries} retries")
            raise Exception(f"Command '{command}' failed after {max_retries} retries")

    async def login_ftpserver(self, telnet_client, ftpserver, adapter_number, timeout=120):
        if ftpserver:
            log.info(f"Login ftp server {ftpserver.node_id} {self._node_id} adapter {adapter_number}.")
        # 避开管理口的vpn
        command = f"ftp {self.FTP_SERVER_IP} public-net"
        await telnet_client.telnet_write(command)
        start = time.time()
        while True:
            await asyncio.sleep(2)
            if time.time() - start > timeout:
                log.error(f"{self._node_id} execute command {command} timeout")
                break

            output = await telnet_client.read_and_clean_telnet_data()
            log.info(f"{self._node_id} execute command {command} output {output}")

            if f"User({self.FTP_SERVER_IP}:(none)):" in output:
                await telnet_client.telnet_write("ftpuser")
                await telnet_client.telnet_write("ftpuser")
                output = await telnet_client.read_and_clean_telnet_data()
                log.info(f"{self._node_id} execute login ftpserver output: {output}")
                if "230 Login successful." in output:
                    await telnet_client.telnet_write("passive")
                    await telnet_client.telnet_write("")
                    await asyncio.sleep(2)
                    break
                await telnet_client.telnet_write(command)

            if "[ftp]" in output:
                log.info(f"{self._node_id} execute command {command} fail")
                await telnet_client.telnet_write("quit")
                await telnet_client.telnet_write(command)
                continue

            if "Error" in output:
                log.info(f"{self._node_id} execute command {command} error")
                await self.relink_ftp_server(ftpserver, adapter_number)
                await telnet_client.telnet_write(command)
                continue

            if "331 Please specify the password" in output:
                await telnet_client.telnet_write("ftpuser")
                output = await telnet_client.read_and_clean_telnet_data()
                log.info(f"{self._node_id} execute login ftpserver output: {output}")
                if "230 Login successful." in output:
                    await telnet_client.telnet_write("passive")
                    await telnet_client.telnet_write("")
                    await asyncio.sleep(2)
                    break
                await telnet_client.telnet_write(command)

            await self.empty_output_process(command, telnet_client)

    async def empty_output_process(self, command, telnet_client):
        await telnet_client.telnet_line_break()
        output = await telnet_client.read_and_clean_telnet_data()
        if output.endswith("]"):
            await telnet_client.telnet_write("quit")
            output = await telnet_client.read_and_clean_telnet_data()
        if output.endswith(">"):
            # 兼容性处理，当无输出结果时，在系统视图重新进入
            log.info(f"{self._node_id} output {output} is empty, try to execute command {command}")
            await telnet_client.telnet_write(command)

    async def init_device(self, telnet_client:TelnetClient, sys_name:str):
        if sys_name is None:
            return
        await asyncio.sleep(2)
        await telnet_client.telnet_write("system-view")
        await telnet_client.telnet_write(f"sysname {sys_name}")

        while True:
            await telnet_client.telnet_write("commit")
            output = await telnet_client.telnet_read()
            log.info(f"Output: {output}")
            if "Error: The system is busy in building system configurations. Please wait a moment and then try again." not in output:
                break
            await asyncio.sleep(2)
        await telnet_client.telnet_write("quit")
        await telnet_client.telnet_write("y")

    async def init_telnet_client(self):
        telnet_client = await self.get_telnet_client()
        await self.test_telnet_connect(telnet_client)

        while True:
            await telnet_client.telnet_line_break()
            output = await telnet_client.telnet_read()
            if ((output.startswith("<") and output.endswith(">")) or (output.startswith("[") and output.endswith("]"))
                    or output.endswith("<HUAWEI>") or "<HUAWEI>" in output or output.endswith(">") or output.endswith(
                        "]")):
                await self._return_user_view(telnet_client)
                break

            if not output:
                await asyncio.sleep(5)

        return telnet_client

    @staticmethod
    async def _return_user_view(telnet_client):
        await telnet_client.telnet_line_break()
        output = await telnet_client.telnet_read()
        if output.endswith("[Y/N]:") or "Y/N" in output:
            await telnet_client.telnet_write("N")
            await telnet_client.telnet_line_break()
            output = await telnet_client.telnet_read()

        return_user_view_try_times = 0
        while not output.endswith(">"):
            await telnet_client.telnet_write("return")
            output = await telnet_client.telnet_read()
            if output.endswith("[Y/N]:") or "Y/N" in output:
                await telnet_client.telnet_write("N")
                await telnet_client.telnet_line_break()
                output = await telnet_client.telnet_read()
            return_user_view_try_times += 1
            if return_user_view_try_times > 5:
                log.error("Try to return to user view more than 5 times please check what is the problem")
                raise Exception("Try to return to user view more than 5 times please check what is the problem")

        if output.endswith("<HUAWEI>"):
            await telnet_client.telnet_write("system-view")
            await telnet_client.telnet_write("undo pnp enable")
            await telnet_client.telnet_write("return")

        await telnet_client.telnet_read()