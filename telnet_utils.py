#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2024. All rights reserved.
import asyncio
import codecs
import logging
import re
import telnetlib
import time
from typing import Union

log = logging.getLogger(__name__)


def _is_read_continue(continue_check_mark: Union[str, re], output: bytes):
    if continue_check_mark is None:
        return False
    output_decode = output.decode()
    if isinstance(continue_check_mark, str):
        return continue_check_mark == output_decode
    else:
        return continue_check_mark.search(output_decode)

async def get_cleaned_echo_lines(data):
    decoded_data = data.replace(b"\r", b"").decode()
    output = decoded_data.replace("\x1b[16D                \x1b[16D", '').replace(
        "[42D                                          [42D", '').replace(' \x1b[1D', '')
    # 逐行处理，去除每行开头和结尾的换行符，但保留空格
    lines = output.split('\n')
    cleaned_lines = [line.rstrip('\n') for line in lines if line.strip() != '']
    return cleaned_lines


async def clean_telnet_data(data):
    cleaned_lines = await get_cleaned_echo_lines(data)
    cleaned_data = '\n'.join(cleaned_lines)
    return cleaned_data


class TelnetClient:
    TIME_INTERVAL = 0.5
    LINK_BREAK = b"\n"
    ENTER_KEY = b"\r"

    def __init__(self, host, port):
        self.__host = host
        self.__port = port
        self.__telnet_client = telnetlib.Telnet(host, port)
        time.sleep(self.TIME_INTERVAL)
        self.__telnet_client.read_very_eager()
        self.__is_connected = True
        self._telnet_decoder = codecs.getincrementaldecoder("utf-8")( errors="ignore")

    def telnet_address(self):
        return f"telnet {self.__host} {self.__port}"

    def reconnect(self):
        self.__telnet_client.close()
        self.__telnet_client = telnetlib.Telnet(self.__host, self.__port)
        time.sleep(self.TIME_INTERVAL)
        self.__telnet_client.read_very_eager()
        self.__is_connected = True
        self._telnet_decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")

    def close(self):
        self.__telnet_client.close()
        self.__is_connected = False

    def is_connected(self):
        return self.__is_connected

    async def telnet_line_break(self):
        await asyncio.sleep(self.TIME_INTERVAL)
        self.__telnet_client.write(self.LINK_BREAK)
        await asyncio.sleep(self.TIME_INTERVAL)

    async def telnet_enter_key(self):
        #wait asyncio.sleep(self.TIME_INTERVAL)
        self.__telnet_client.write(self.ENTER_KEY)
        await asyncio.sleep(self.TIME_INTERVAL)

    async def telnet_write(self, content, sleep_time=TIME_INTERVAL):
        await asyncio.sleep(sleep_time)
        if isinstance(content, bytes):
            content = content.decode()

        line_break = "\n"
        last_line_break_index = content.rfind(line_break)
        if last_line_break_index != -1:
            content = content[:last_line_break_index]

        content = content.encode()

        self.__telnet_client.write(content)

        await asyncio.sleep(sleep_time)
        # 将输入的字符先读掉，避免写后读取包含写入的内容
        self.__telnet_client.read_very_eager()

        self.__telnet_client.write(self.LINK_BREAK)
        # 方法以Unix为标准，每行结尾以"\n"的方式，如果设备以"\r\n"为结尾的话，使用改方法添加"\r"

        await asyncio.sleep(sleep_time)

    async def telnet_write_contain_input(self, content, sleep_time=TIME_INTERVAL, chunk_size=1024):
        if isinstance(content, bytes):
            content = content.decode()

        line_break = "\n"
        last_line_break_index = content.rfind(line_break)
        if last_line_break_index != -1:
            content = content[:last_line_break_index]

        content = content.encode()
        data_size = len(content)

        for i in range(0, data_size, chunk_size):
            chunk = content[i:i + chunk_size]
            self.__telnet_client.write(chunk)
            await asyncio.sleep(0.1)

        self.__telnet_client.write(self.LINK_BREAK)
        # 方法以Unix为标准，每行结尾以"\n"的方式，如果设备以"\r\n"为结尾的话，使用改方法添加"\r"

        await asyncio.sleep(sleep_time)

    async def read_until_output_stable(self, timeout: float = 30, check_interval: float = 1, end_marker: str = '@write_end_mark', continue_check_count: int = 30, continue_check_mark: Union[str, re] = None, return_result_timeout: bool = False):
        start_time = time.time()
        output = b''
        empty_output_count = 0
        while time.time() - start_time < timeout:
            time.sleep(check_interval)
            current_output = self.__telnet_client.read_very_eager()
            if _is_read_continue(continue_check_mark, current_output):
                if empty_output_count > continue_check_count:
                    log.info("The output is continue_check_mark for multiple consecutive times; the echo has been completed.")
                    return output
                empty_output_count+= 1
                continue
            output += current_output
            current_output_clean = await clean_telnet_data(current_output)
            if end_marker in current_output_clean:
                return output
        if return_result_timeout:
            return output
        raise TimeoutError("Waiting timeout: Output not stable")

    async def telnet_read(self):
        await asyncio.sleep(self.TIME_INTERVAL)
        data = self.__telnet_client.read_very_eager()
        data = data.replace(b"\r\n", b"\n")
        data = data.decode().strip()
        await asyncio.sleep(0.1)
        return data

    async def fixed_telnet_read(self):
        """读取当前可用的 Telnet 增量数据。"""
        await asyncio.sleep(self.TIME_INTERVAL)
        data = self.__telnet_client.read_very_eager()
        if not data:
            return ""

        output = self._telnet_decoder.decode(data, final=False)
        log.debug("Telnet read: %r", output[-2000:])
        return output

    async def telnet_send_sigstop(self):
        await self.telnet_write(b"\x26")

    async def telnet_send_sigint(self):
        await self.telnet_write(b"\x03")

    async def read_and_clean_telnet_data(self, sleep_time=TIME_INTERVAL):
        await asyncio.sleep(sleep_time)
        data = self.__telnet_client.read_very_eager()
        return await clean_telnet_data(data)
