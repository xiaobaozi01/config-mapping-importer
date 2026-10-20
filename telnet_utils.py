#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2024. All rights reserved.
"""Juniper/Cisco console 使用的 Telnet 传输层。

做什么：提供原始增量读取、显式行写入、重连、连接状态，以及当前 Cisco
仍会调用的少量 legacy 轮询包装。
为什么：Juniper/Cisco 需要精确控制命令回显和行结束符，传输层不能偷读、
丢弃回显或硬编码厂商节奏；厂商差异由 ConsoleProfile 和调用方决定。
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import re
import telnetlib
from typing import Protocol

log = logging.getLogger(__name__)


# 做什么：识别 ANSI CSI/OSC 以及 Junos 常用的光标、擦除序列。
# 为什么：提示符和成功标记可能夹带控制字符，累计匹配前必须先清理。
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)


class TelnetDisconnected(ConnectionError):
    """底层 Telnet 连接已经不可用。

    做什么：统一表示读、写或连接状态上的断线。
    为什么：业务层需要区分“暂时没数据”和“连接已经断开”。
    """


class _RawTelnet(Protocol):
    """底层 telnetlib 对等物的最小结构。

    做什么：描述 TelnetClient 真正调用的 write/read/close。
    为什么：测试替身和 telnetlib.Telnet 都能按结构匹配，避免使用 Any。
    """

    def write(self, data: bytes) -> None:
        """做什么：写入字节；为什么：TelnetClient 只依赖这一种写入口。"""
        ...

    def read_very_eager(self) -> bytes:
        """做什么：非阻塞读取已有字节；为什么：业务层需要自己控制轮询节奏。"""
        ...

    def close(self) -> None:
        """做什么：关闭底层连接；为什么：清理阶段必须尽力释放资源。"""
        ...


def _as_bytes(data: str | bytes | bytearray) -> bytes:
    """把输入统一转换为字节。

    做什么：字符串按 UTF-8 编码，字节保持不变。
    为什么：写操作必须保留调用方的原始输入，不能按字符串规则截断。
    """
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    raise TypeError(f"Telnet payload must be str or bytes, got {type(data)!r}")


def _as_text(data: str | bytes | bytearray) -> str:
    """把输入统一转换为文本。

    做什么：字节按 UTF-8 解码，非法字节用替换字符处理。
    为什么：提示符和清理逻辑都以文本为单位，需要统一的入口。
    """
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", errors="replace")
    raise TypeError(f"Telnet payload must be str or bytes, got {type(data)!r}")


def normalize_terminal_text(text: str | bytes | bytearray) -> str:
    """规范化终端文本。

    做什么：去除 ANSI 控制序列、处理退格、统一 CR/LF 并移除 NUL。
    为什么：提示符和成功标记可能跨包、夹带控制字符，业务层需要在累计
    缓冲区上做稳定的文本匹配。
    """
    text = _as_text(text)
    text = _ANSI_ESCAPE_RE.sub("", text)

    # 做什么：按终端语义应用退格。
    # 为什么：设备回显中可能出现退格修正，直接保留会破坏提示符匹配。
    if "\x08" in text:
        cleaned = []
        for char in text:
            if char == "\x08":
                if cleaned:
                    cleaned.pop()
            else:
                cleaned.append(char)
        text = "".join(cleaned)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")
    return text


class TelnetClient:
    """Juniper/Cisco console 使用的 Telnet 客户端。

    做什么：提供原始 read_available/write_line/reconnect_async，以及当前
    Cisco 仍依赖的 telnet_write/telnet_read 等 legacy 包装。
    为什么：新代码只使用原始能力并自行控制节奏；legacy 包装用于兼容现有
    Cisco 和它调用的父类 FTP/test_telnet_connect 流程。
    """

    TIME_INTERVAL: float = 0.5
    LINK_BREAK: bytes = b"\n"
    ENTER_KEY: bytes = b"\r"

    def __init__(self, host: str, port: int, telnet_client: _RawTelnet | None=None) -> None:
        """建立 Telnet 连接。

        做什么：保存地址、初始化解码器并建立底层连接。
        为什么：构造阶段不读取数据，避免在调用方不知情时丢掉启动回显。
        """
        self.__host: str = host
        self.__port: int = port
        self.__is_connected: bool = False
        self._telnet_decoder: codecs.IncrementalDecoder = codecs.getincrementaldecoder(
            "utf-8"
        )(errors="replace")
        self.__telnet_client: _RawTelnet | None = telnet_client
        if self.__telnet_client is None:
            self.__telnet_client = telnetlib.Telnet(host, port)
        self.__is_connected = True

    def telnet_address(self) -> str:
        """返回可读的 Telnet 地址。

        做什么：拼接 host 和 port。
        为什么：日志和异常中需要快速定位具体连接。
        """
        return f"telnet {self.__host} {self.__port}"

    @property
    def is_connected(self) -> bool:
        """返回连接状态。

        做什么：直接读取内部连接标记。
        为什么：Juniper 导入和父类 FTP 流程都只需要布尔判断。
        """
        return self.__is_connected

    def reconnect(self) -> None:
        """同步重建连接。

        做什么：关闭旧连接、重新建立连接并重置解码器。
        为什么：父类 FTP 流程以同步方式调用重连。
        """
        old_client: _RawTelnet | None = self.__telnet_client
        self.__is_connected = False
        if old_client is not None:
            try:
                old_client.close()
            except Exception:  # noqa: BLE001 - 关闭旧连接必须尽力而为
                log.debug("Ignoring error while closing old Telnet connection", exc_info=True)

        self.__telnet_client = telnetlib.Telnet(self.__host, self.__port)
        self._telnet_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.__is_connected = True
        log.info("Reconnected to %s", self.telnet_address())

    async def reconnect_async(self) -> None:
        """异步重建连接。

        做什么：把可能阻塞的 TCP 连接放到线程中执行。
        为什么：避免同步连接过程阻塞 Juniper 的事件循环。
        """
        await asyncio.to_thread(self.reconnect)

    def close(self) -> None:
        """关闭连接。

        做什么：尽力关闭底层 socket 并标记为未连接。
        为什么：清理阶段不能因为关闭失败而抛出新异常。
        """
        self.__is_connected = False
        client = self.__telnet_client
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关闭连接必须尽力而为
                log.debug("Ignoring error while closing Telnet connection", exc_info=True)

    async def _write_bytes(self, data: bytes) -> None:
        """写入原始字节。

        做什么：检查连接状态并调用底层 write。
        为什么：写失败需要立即标记断线，让上层决定是否恢复会话。
        """
        if not self.__is_connected or self.__telnet_client is None:
            raise TelnetDisconnected(f"{self.telnet_address()} is not connected")
        try:
            self.__telnet_client.write(data)
        except (EOFError, OSError, AttributeError) as exc:
            self.__is_connected = False
            raise TelnetDisconnected(f"{self.telnet_address()} write failed: {exc}") from exc
        log.debug("Telnet write %s: %r", self.telnet_address(), data[:2000])

    async def _read_raw(self) -> bytes:
        """读取当前可用的原始字节。

        做什么：调用 read_very_eager 并处理断线异常。
        为什么：这是唯一真正读取 socket 的入口，便于统一连接状态。
        """
        if not self.__is_connected or self.__telnet_client is None:
            raise TelnetDisconnected(f"{self.telnet_address()} is not connected")
        try:
            data = self.__telnet_client.read_very_eager()
        except (EOFError, OSError, AttributeError) as exc:
            self.__is_connected = False
            raise TelnetDisconnected(f"{self.telnet_address()} read failed: {exc}") from exc
        return data or b""

    async def read_available(self) -> str:
        """读取当前可用的一个增量。

        做什么：只读取已有字节并用增量解码器转成文本。
        为什么：本方法不睡眠、不发送数据，残留回显由业务层显式处理。
        """
        data = await self._read_raw()
        if not data:
            return ""
        if isinstance(data, str):
            return data
        return self._telnet_decoder.decode(data, final=False)

    async def write_line(self, content: str | bytes, line_ending: bytes | None=None) -> None:
        """写入一行并显式指定行结束符。

        做什么：保留完整内容，只在内容没有以换行结尾时追加 line_ending。
        为什么：Juniper/Cisco 的行结束符不同，原始传输层不应硬编码，也不应 sleep。
        """
        data = _as_bytes(content)
        ending = self.LINK_BREAK if line_ending is None else _as_bytes(line_ending)
        if not data.endswith(b"\n"):
            data += ending
        await self._write_bytes(data)

    async def telnet_write(self, content: str | bytes, sleep_time: float=TIME_INTERVAL) -> None:
        """兼容写入一行。

        做什么：调用 write_line 追加 LF，并保留可选的写入前后沉降。
        为什么：现有 Cisco 和父类 FTP 流程仍依赖该接口及其沉降参数；
        Juniper/ConsoleSession 应使用 write_line，不经过这里的固定等待。
        """
        if sleep_time:
            await asyncio.sleep(sleep_time)
        await self.write_line(content, self.LINK_BREAK)
        if sleep_time:
            await asyncio.sleep(sleep_time)

    async def telnet_line_break(self) -> None:
        """立即发送一次换行。

        做什么：直接写入 LINK_BREAK。
        为什么：原始传输层不负责节奏，等待由 ConsoleSession 或调用方控制。
        """
        await self._write_bytes(self.LINK_BREAK)

    async def telnet_enter_key(self) -> None:
        """立即发送一次回车。

        做什么：直接写入 ENTER_KEY。
        为什么：原始传输层不负责节奏，回车后何时读取由调用方控制。
        """
        await self._write_bytes(self.ENTER_KEY)

    async def telnet_read(self) -> str:
        """读取一次并返回规范化文本。

        做什么：等待一个节拍后读取增量，再去掉首尾空白。
        为什么：这是父类 FTP/Cisco 的轮询兼容接口；Juniper 使用无等待的
        read_available()，节奏由 ConsoleSession 控制。
        注意：这里对单个 chunk 做 normalize，不保证跨 chunk 的 CRLF；
        新 ConsoleSession 在累计文本上做规范化。
        """
        await asyncio.sleep(self.TIME_INTERVAL)
        try:
            data = await self.read_available()
        except TelnetDisconnected as exc:
            log.warning("Telnet read failed: %s", exc)
            return ""
        return normalize_terminal_text(data).strip()
