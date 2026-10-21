#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Telnet 传输层的单元测试。

做什么：验证写入、增量读取、断线状态、重连和文本清理。
为什么：Juniper 导入依赖这些基础语义，必须先保证传输层行为稳定。
"""

import unittest
from unittest import mock

import telnet_utils
from telnet_utils import TelnetClient, TelnetDisconnected, normalize_terminal_text


class FakeSocket:
    """最小化的底层 socket 替身。

    做什么：提供 write、read_very_eager 和 close。
    为什么：测试不依赖真实网络，也能精确控制每次读取的字节。
    """

    def __init__(self, chunks=None):
        self.chunks = list(chunks or [])
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    def read_very_eager(self):
        if self.closed:
            raise EOFError("closed")
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


class TelnetClientTest(unittest.IsolatedAsyncioTestCase):
    def make_client(self, chunks=None):
        """创建使用 fake socket 的客户端。

        做什么：注入替身并关闭节拍等待。
        为什么：让测试快速且可控。
        """
        socket = FakeSocket(chunks)
        client = TelnetClient("127.0.0.1", 23, telnet_client=socket)
        client.TIME_INTERVAL = 0
        return client, socket

    async def test_write_does_not_read_or_truncate_input(self):
        """验证写入不读取回显且不截断输入。"""
        client, socket = self.make_client([b"device echo"])
        socket.read_very_eager = mock.Mock(side_effect=AssertionError("write must not read"))

        await client.telnet_write("set system host-name router-1", sleep_time=0)

        self.assertEqual(socket.writes, [b"set system host-name router-1\n"])

    async def test_write_preserves_embedded_newlines(self):
        """验证多行输入不会被截断。"""
        client, socket = self.make_client()

        await client.telnet_write("line-one\nline-two", sleep_time=0)

        self.assertEqual(socket.writes, [b"line-one\nline-two\n"])

    async def test_write_does_not_add_a_second_terminator(self):
        """验证已有换行时不会重复追加。"""
        client, socket = self.make_client()

        await client.telnet_write("already terminated\n", sleep_time=0)

        self.assertEqual(socket.writes, [b"already terminated\n"])

    async def test_write_line_uses_explicit_line_ending(self):
        """验证显式行结束符和空行写入。"""
        client, socket = self.make_client()

        await client.write_line("cmd", b"\r\n")
        await client.write_line("cmd\n", b"\r\n")
        await client.write_line("", b"\r")

        self.assertEqual(socket.writes, [b"cmd\r\n", b"cmd\n", b"\r"])

    async def test_cisco_command_and_confirmation_send_one_enter_each(self):
        client, socket = self.make_client()
        await client.write_line("commit label cabcdef234 replace", b"\r")
        await client.write_line("yes", b"\r")
        self.assertEqual(socket.writes, [b"commit label cabcdef234 replace\r", b"yes\r"])

    async def test_write_line_does_not_sleep(self):
        """验证原始 write_line 不包含等待。"""
        client, socket = self.make_client()

        with mock.patch.object(
            telnet_utils.asyncio, "sleep", side_effect=AssertionError("must not sleep")
        ):
            await client.write_line("top", b"\n")

        self.assertEqual(socket.writes, [b"top\n"])

    async def test_line_break_and_enter_do_not_sleep(self):
        """验证换行和回车发送不再内置等待。"""
        client, socket = self.make_client()

        with mock.patch.object(
            telnet_utils.asyncio, "sleep", side_effect=AssertionError("must not sleep")
        ):
            await client.telnet_line_break()
            await client.telnet_enter_key()

        self.assertEqual(socket.writes, [b"\n", b"\r"])

    async def test_write_with_zero_settle_does_not_sleep(self):
        """验证 Juniper 使用的 sleep_time=0 路径不等待。"""
        client, socket = self.make_client()

        with mock.patch.object(
            telnet_utils.asyncio, "sleep", side_effect=AssertionError("must not sleep")
        ):
            await client.telnet_write("top", sleep_time=0)

        self.assertEqual(socket.writes, [b"top\n"])

    async def test_incremental_utf8_decoding_across_packets(self):
        """验证 UTF-8 字符跨包时仍能正确解码。"""
        payload = "中文".encode("utf-8")
        client, _ = self.make_client([payload[:1], payload[1:2], payload[2:]])

        decoded = ""
        for _ in range(3):
            decoded += await client.read_available()

        self.assertEqual(decoded, "中文")

    async def test_read_available_is_non_destructive(self):
        """验证读取不会写回或清空后续数据。"""
        client, socket = self.make_client([b"first", b"second"])

        self.assertEqual(await client.read_available(), "first")
        self.assertEqual(await client.read_available(), "second")
        self.assertEqual(socket.writes, [])

    async def test_disconnect_is_reported_and_connection_state_changes(self):
        """验证断线时抛出异常并更新连接状态。"""
        client, socket = self.make_client([b""])
        socket.closed = True

        with self.assertRaises(TelnetDisconnected):
            await client.read_available()

        self.assertFalse(client.is_connected)

    async def test_connection_state_is_boolean_property(self):
        """验证 is_connected 是普通布尔属性。"""
        client, _ = self.make_client()

        self.assertIs(client.is_connected, True)
        client.close()
        self.assertIs(client.is_connected, False)

    async def test_reconnect_resets_decoder_and_state(self):
        """验证重连会重置解码器和连接状态。"""
        old_socket = FakeSocket([b"\xe4"])
        new_socket = FakeSocket(["中".encode("utf-8")])
        client = TelnetClient("127.0.0.1", 23, telnet_client=old_socket)

        with mock.patch.object(telnet_utils.telnetlib, "Telnet", return_value=new_socket):
            client.reconnect()

        self.assertTrue(client.is_connected)
        self.assertEqual(await client.read_available(), "中")

    async def test_close_is_idempotent(self):
        """验证重复关闭不会抛异常。"""
        client, _ = self.make_client()
        client.close()
        client.close()
        self.assertFalse(client.is_connected)

    async def test_normalize_removes_ansi_and_empty_lines(self):
        """验证 ANSI 清理和空行过滤。"""
        data = b"\x1b[1mbanner\x1b[0m\r\n\r\nprompt# "
        normalized = normalize_terminal_text(data)
        lines = [line for line in normalized.split("\n") if line.strip()]
        self.assertEqual("\n".join(lines), "banner\nprompt# ")

    async def test_normalize_applies_backspace(self):
        """验证退格修正。"""
        self.assertEqual(normalize_terminal_text(b"ab\x08c"), "ac")

    async def test_telnet_write_failure_marks_disconnected(self):
        """验证写失败后连接状态变为断开。"""
        client, socket = self.make_client()

        def fail(_data):
            raise OSError("pipe broken")

        socket.write = fail

        with self.assertRaises(TelnetDisconnected):
            await client.telnet_write("x", sleep_time=0)
        self.assertFalse(client.is_connected)


if __name__ == "__main__":
    unittest.main()
