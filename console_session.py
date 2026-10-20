#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2024. All rights reserved.
"""厂商无关的 console 会话层。

Juniper 和 Cisco 共用 console 状态机，但提示符、
登录动作和错误格式不同；把这些差异收敛到 ConsoleProfile 数据中。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from math import ceil

try:
    from src.utils.telnet_utils import TelnetClient, normalize_terminal_text
except ImportError:  # pragma: no cover - 独立测试时的回退路径
    from telnet_utils import TelnetClient, normalize_terminal_text

log = logging.getLogger(__name__)


class PromptName(str, Enum):
    """内置 profile 使用的提示符名称。

    避免在 ConsoleSession、Juniper 和 Cisco 中重复魔法字符串。
    """

    CONFIG = "config"
    EXEC = "exec"
    SHELL = "shell"
    LOGIN = "login"
    PASSWORD = "password"
    USERNAME = "username"
    ROOT_USERNAME = "root_username"
    SECRET = "secret"
    CONFIRM = "confirm"

    def __str__(self) -> str:
        """返回枚举值。"""
        return self.value


class ConsoleSessionError(RuntimeError):
    """Console 会话无法继续。

    调用方需要把这类错误与普通命令错误和超时区分开。
    """


@dataclass(frozen=True)
class PromptRule:
    """一条提示符规则。

    ConsoleSession 只按名称处理提示符，不关心厂商语法。
    """

    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class ModeStep:
    """一次模式切换动作。

    Juniper 的 shell→exec→config 和 Cisco 的 exec→config 可以共用同一逻辑。
    """

    from_prompt: str
    command: str


@dataclass(frozen=True)
class ConsoleProfile:
    """ConsoleSession 的厂商差异配置。

    ConsoleSession 保持通用，Juniper/Cisco 只提供 profile 数据。
    """

    name: str
    prompts: tuple[PromptRule, ...]
    login_actions: Mapping[str, str]
    mode_steps: tuple[ModeStep, ...]
    command_prompt: str
    line_ending: bytes = b"\n"
    error_patterns: tuple[re.Pattern[str], ...] = ()
    background_patterns: tuple[re.Pattern[str], ...] = ()
    noise_patterns: tuple[re.Pattern[str], ...] = ()
    warning_prefixes: tuple[str, ...] = ("warning:",)


@dataclass
class ConsoleResult:
    """一次命令交互的结果。

    提示符、成功标记和登录状态需要分别判断，不能混为一个布尔值。
    """

    output: str = ""
    sent: bool = False
    prompt_name: str | None = None
    prompt_returned: bool = False
    restore_required: bool = False
    success_seen: bool = False
    disconnected: bool = False
    timed_out: bool = False


@dataclass(frozen=True)
class _PromptMatch:
    """通用匹配结果：提示符名称、原始数据边界和稳定起点。"""

    name: str
    end: int
    since: float


class ConsoleSession:
    """厂商无关的 console 会话。

    未消费数据由会话持有，操作结果保存已消费内容的快照；同一会话按顺序执行操作。
    """

    MAX_RECOVERIES: int = 3
    READ_INTERVAL: float = 0.2
    PROMPT_SETTLE: float = 0.5
    DRAIN_QUIET: float = 0.3
    DRAIN_MAX: float = 2.0
    NUDGE_INTERVAL: float = 3.0

    def __init__(self, telnet_client: TelnetClient, profile: ConsoleProfile) -> None:
        """保存原始连接和厂商 profile。

        连接只负责收发，厂商差异全部由 profile 提供。
        """
        self._telnet: TelnetClient = telnet_client
        self._profile: ConsoleProfile = profile
        self._recovery_generation: int = 0
        self._buffer = ""
        self._last_data_at = 0.0
        self._last_probe_at = 0.0
        self._last_prompt: str | None = None
        self._prompt_since = 0.0

    @property
    def recovery_generation(self) -> int:
        """返回恢复代次；业务层据此使旧配置加载状态失效。"""
        return self._recovery_generation

    async def enter_command_prompt(self, initial_output: str='', timeout: float=300) -> None:
        """识别当前终端状态，按需登录并切换到目标命令提示符。

        不同厂商可能经过 login、shell、exec、config 多个阶段。
        """
        if timeout <= 0:
            raise TimeoutError("console login budget exhausted")
        await asyncio.wait_for(self._enter_command_prompt(initial_output), timeout=timeout)

    async def _enter_command_prompt(self, initial_output: str) -> None:
        """逐个消费登录和模式提示符，剩余数据继续用于下一步。"""
        prompts = {rule.name for rule in self._profile.prompts}
        if initial_output and self._find_prompt(prompts, first=True)[0] is None:
            self._buffer = self._last_meaningful_line(initial_output) + "\n" + self._buffer
        attempts: dict[str, int] = {}
        reconnects = 0
        while True:
            try:
                response = await self._expect(prompts, first_prompt=True)
                self._consume(response.end)
                if response.name == self._profile.command_prompt:
                    return
                await self._handle_prompt(response.name, attempts)
            except ConnectionError as exc:
                reconnects += 1
                if reconnects > self.MAX_RECOVERIES:
                    raise ConnectionError(f"console disconnected during login: {exc}") from exc
                log.warning("Console disconnected during login; reconnecting attempt=%s", reconnects)
                await self._reconnect()

    async def _handle_prompt(self, prompt: str, attempts: dict[str, int]) -> None:
        """发送 profile 定义的登录或模式动作，并限制同一提示符的尝试次数。"""
        action = self._profile.login_actions.get(prompt)
        if action is None:
            step = next((step for step in self._profile.mode_steps if step.from_prompt == prompt), None)
            if step is None:
                raise ConsoleSessionError(f"unexpected console prompt: {prompt}")
            action = step.command
        count = attempts.get(prompt, 0) + 1
        if count > self.MAX_RECOVERIES:
            raise ConsoleSessionError(f"console prompt retry limit reached: {prompt}")
        attempts[prompt] = count
        log.debug("Console prompt action: prompt=%s count=%s", prompt, count)
        await self._telnet.write_line(action, self._profile.line_ending)

    async def ensure_ready(self, timeout: float = 300) -> None:
        """限制一次就绪检查的耗时，包括残留读取和必要的恢复。"""
        if timeout <= 0:
            raise TimeoutError("console readiness budget exhausted")
        await asyncio.wait_for(self._ensure_ready(), timeout=timeout)

    async def _ensure_ready(self) -> None:
        """确保下一条命令可以在命令提示符执行。

        写操作不能偷读回显，残留必须由业务层显式处理。
        """
        residual = await self.drain_residual()
        if residual:
            log.debug("Drained residual output: %r", residual[-2000:])

        prompt = self._prompt_kind(residual)
        if prompt == self._profile.command_prompt:
            return
        if prompt is not None or not self._telnet.is_connected:
            log.info("Console needs refresh: prompt=%s", prompt)
            await self._restore(residual)

    async def restore(self, initial_output: str='', timeout: float=300) -> None:
        """恢复 console 到命令提示符。

        命令发送前必须确认终端处于可交互的命令模式。
        """
        if timeout <= 0:
            raise TimeoutError("console recovery budget exhausted")
        await asyncio.wait_for(self._restore(initial_output), timeout=timeout)

    async def _restore(self, initial_output: str) -> None:
        """重建连接并切换模式，耗时由调用入口统一限制。"""
        self._recovery_generation += 1
        if not self._telnet.is_connected:
            await self._reconnect()
            initial_output = ""
        await asyncio.sleep(self.READ_INTERVAL)
        await self._enter_command_prompt(initial_output)

    async def exchange(
        self,
        command: str,
        timeout: float | None = None,
        prompt_names: Iterable[str] | None = None,
        success_pattern: re.Pattern[str] | None = None,
    ) -> ConsoleResult:
        """发送一次命令并消费本次结果；None 表示发送和读取均不限时。"""
        result = ConsoleResult()
        if timeout is not None and timeout <= 0:
            result.timed_out = True
            return result
        expected_prompts = set(prompt_names) if prompt_names else {self._profile.command_prompt}
        log.info("Console execute: %s", command)
        try:
            operation = self._exchange_once(command, result, expected_prompts, success_pattern)
            if timeout is None:
                await operation
            else:
                await asyncio.wait_for(operation, timeout=timeout)
        except TimeoutError:
            if result.sent:
                result.output = self._consume()
                result.prompt_name = self._prompt_kind(result.output)
                result.success_seen = self._success_seen(result.output, command, success_pattern)
            result.timed_out = True
            result.prompt_returned = False
        log.info("Console execute done: prompt=%s returned=%s restore=%s success=%s",
                 result.prompt_name, result.prompt_returned, result.restore_required, result.success_seen)
        return result

    async def _exchange_once(
        self,
        command: str,
        result: ConsoleResult,
        expected_prompts: set[str],
        success_pattern: re.Pattern[str] | None,
    ) -> None:
        """发送耗时计入交换预算；断线只返回结果，不恢复或重发命令。"""
        if not await self._send_exchange(command, result):
            return
        try:
            response = await self._wait_command_result(expected_prompts, command, success_pattern)
            result.output = response.output
            result.prompt_name = response.prompt_name
            result.prompt_returned = response.prompt_returned
            result.restore_required = response.restore_required
            result.success_seen = response.success_seen
        except ConnectionError as exc:
            result.output = self._consume()
            result.prompt_name = self._prompt_kind(result.output)
            result.success_seen = self._success_seen(result.output, command, success_pattern)
            result.restore_required = result.disconnected = True
            log.warning("Console exchange failed: profile=%s command=%s detail=%s", self._profile.name, command, exc)

    async def _send_exchange(self, command: str, result: ConsoleResult) -> bool:
        """发送命令并记录发送状态。

        未发送成功的命令可以安全重试，不能与已发送但结果丢失混淆。
        """
        try:
            await self._telnet.write_line(command, self._profile.line_ending)
            result.sent = True
            return True
        except ConnectionError as exc:
            result.restore_required = True
            result.disconnected = True
            result.output = str(exc)
            log.warning("Console write failed: %s", exc)
            return False

    async def _wait_command_result(
        self,
        prompts: set[str],
        command: str,
        success_pattern: re.Pattern[str] | None,
    ) -> ConsoleResult:
        """命令交换独有的回显分析、成功标记宽限期和会话丢失判断。"""
        success_seen = False
        while True:
            match = await self._expect(prompts, settle=self.PROMPT_SETTLE)
            success_seen = success_seen or self._success_seen(self._buffer, command, success_pattern)
            elapsed = asyncio.get_running_loop().time() - match.since
            ready = success_pattern is None or success_seen or elapsed >= self.PROMPT_SETTLE * 2
            if match.name not in prompts or ready:
                return ConsoleResult(
                    output=self._consume(match.end), prompt_name=match.name,
                    prompt_returned=match.name in prompts, restore_required=match.name not in prompts,
                    success_seen=success_seen,
                )
            await asyncio.sleep(self.READ_INTERVAL)
            await self._read()

    async def drain_residual(self, timeout: float | None = None) -> str:
        """按最大轮询次数读取残留，满足安静窗口时提前结束，不发送回车。"""
        duration = min(self.DRAIN_MAX, timeout if timeout is not None else self.DRAIN_MAX)
        loop = asyncio.get_running_loop()
        quiet_since = loop.time()
        try:
            for _ in range(max(0, ceil(duration / self.READ_INTERVAL))):
                if await self._read():
                    quiet_since = loop.time()
                elif loop.time() - quiet_since >= self.DRAIN_QUIET:
                    break
                await asyncio.sleep(self.READ_INTERVAL)
        except ConnectionError:
            pass
        return self._consume()

    def analysis_text(self, output: str, command: str) -> str:
        """提取用于判断命令结果的有效文本。

        错误和成功标记只能来自命令自身输出。
        """
        text = normalize_terminal_text(output)
        text = self._strip_command_echo(text, command)

        lines = []
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if self._is_background_line(stripped) or self._is_noise_line(stripped):
                continue
            if self._is_prompt_line(stripped):
                continue
            lines.append(line.rstrip())
        return "\n".join(lines)

    def _strip_command_echo(self, text: str, command: str) -> str:
        """删除第一条命令 echo。

        整段 find 截断可能误删命令前的有效输出，行级删除更精确。
        """
        normalized_command = normalize_terminal_text(command).strip()
        if not normalized_command:
            return text

        lines = text.split("\n")
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped == normalized_command:
                lines[index] = ""
                break
            if stripped.endswith(normalized_command):
                prefix = stripped[:-len(normalized_command)].rstrip()
                if prefix and not self._is_prompt_line(prefix):
                    continue
                lines[index] = prefix
                break
        return "\n".join(lines)

    def find_explicit_error(
        self,
        text: str,
        ignore_pattern: re.Pattern[str] | None = None,
        patterns: tuple[re.Pattern[str], ...] | None = None,
    ) -> str | None:
        """查找明确错误。

        不同阶段需要不同错误模式，不能把所有错误混在一起。
        """
        warning_prefixes = tuple(prefix.lower() for prefix in self._profile.warning_prefixes)
        error_patterns = self._profile.error_patterns if patterns is None else patterns
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped or self._is_ignored_error_line(
                stripped, warning_prefixes, ignore_pattern
            ):
                continue
            for pattern in error_patterns:
                if pattern.search(stripped):
                    return stripped
        return None

    def _is_ignored_error_line(
        self,
        line: str,
        warning_prefixes: tuple[str, ...],
        ignore_pattern: re.Pattern[str] | None,
    ) -> bool:
        """判断错误扫描时需要跳过的行。

        把过滤条件集中在一处，避免错误识别主循环过于复杂。
        """
        if self._is_background_line(line):
            return True
        if warning_prefixes and line.lower().startswith(warning_prefixes):
            return True
        return ignore_pattern is not None and bool(ignore_pattern.search(line))

    def _prompt_kind(self, text: str) -> str | None:
        """识别最后一行对应的提示符名称。

        只匹配最后一行可以避免旧提示符干扰当前状态。
        """
        last_line = self._last_meaningful_line(text)
        if not last_line:
            return None
        for rule in self._profile.prompts:
            if rule.pattern.search(last_line):
                return rule.name
        return None

    def _last_meaningful_line(self, text: str) -> str:
        """取得最后一条有效输出。

        提示符可能被后台日志包围，需要找到真正的最后一行。
        """
        normalized = normalize_terminal_text(text)
        for line in reversed(normalized.split("\n")):
            stripped = line.strip()
            if not stripped:
                continue
            if self._is_background_line(stripped) or self._is_noise_line(stripped):
                continue
            return stripped
        return ""

    def _is_prompt_line(self, line: str) -> bool:
        """判断单行是否匹配任意 profile 提示符。

        分析命令输出和提交历史时需要排除提示符噪声。
        """
        stripped = line.strip()
        if not stripped or len(stripped) > 300:
            return False
        return any(rule.pattern.search(stripped) for rule in self._profile.prompts)

    def _is_background_line(self, line: str) -> bool:
        """判断是否为 profile 定义的后台日志。

        后台日志可能包含 error 单词，不能作为失败依据。
        """
        return any(pattern.search(line) for pattern in self._profile.background_patterns)

    def _is_noise_line(self, line: str) -> bool:
        """判断是否为 profile 定义的噪声行。

        层级 banner 等非命令输出不应参与结果判断。
        """
        return any(pattern.search(line) for pattern in self._profile.noise_patterns)

    async def _read(self) -> str:
        """唯一的传输读取入口；新数据始终追加到会话拥有的未消费缓冲区。"""
        chunk = await self._telnet.read_available()
        if chunk:
            self._buffer += chunk
            self._last_data_at = asyncio.get_running_loop().time()
        return chunk or ""

    def _consume(self, end: int | None = None) -> str:
        """取走已处理的前缀，保留匹配位置之后的原始数据。"""
        end = len(self._buffer) if end is None else end
        output = self._buffer[:end]
        self._buffer = self._buffer[end:]
        self._last_prompt = None
        self._prompt_since = 0.0
        return output

    def _find_prompt(self, prompts: set[str], first: bool = False) -> tuple[str | None, int]:
        """返回提示符及原始边界；first 控制匹配首个目标或最后有效行。"""
        found: tuple[str | None, int] = (None, 0)
        end = 0
        for line in self._buffer.splitlines(keepends=True):
            end += len(line)
            text = self._last_meaningful_line(line)
            if not text:
                continue
            prompt = self._prompt_kind(text)
            if first and prompt in prompts:
                return prompt, end
            if not first:
                found = (prompt, end)
        return found

    def _success_seen(self, output: str, command: str, pattern: re.Pattern[str] | None) -> bool:
        """只在本次输出的有效文本中查找成功标记。"""
        return pattern is not None and bool(pattern.search(self.analysis_text(output, command)))

    async def _expect(self, prompts: set[str], *, settle: float = 0, first_prompt: bool = False) -> _PromptMatch:
        """等待提示符并返回匹配边界，不消费数据；由调用方决定结果含义和消费时机。"""
        loop = asyncio.get_running_loop()
        self._last_data_at = self._last_probe_at = loop.time()
        if not self._buffer:
            await self._read()
        while True:
            prompt, end = self._find_prompt(prompts, first=first_prompt)
            now = loop.time()
            if prompt != self._last_prompt:
                self._last_prompt = prompt
                self._prompt_since = now
            if prompt is not None:
                if prompt not in prompts or now - self._prompt_since >= settle:
                    return _PromptMatch(prompt, end, self._prompt_since)
            if prompt is None:
                await self._probe_if_idle()
            await asyncio.sleep(self.READ_INTERVAL)
            await self._read()

    async def _probe_if_idle(self) -> None:
        """静默达到统一间隔时发送回车，登录和命令交换共用。"""
        now = asyncio.get_running_loop().time()
        if now - max(self._last_data_at, self._last_probe_at) < self.NUDGE_INTERVAL:
            return
        self._last_probe_at = now
        await self._telnet.write_line("", self._profile.line_ending)

    async def _reconnect(self) -> None:
        """重连使旧连接的未消费数据失效，恢复代次先递增。"""
        self._recovery_generation += 1
        self._consume()
        await self._telnet.reconnect_async()
