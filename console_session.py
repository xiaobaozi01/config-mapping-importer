#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2024. All rights reserved.
"""厂商无关的 console 会话层。

做什么：在原始 Telnet 之上提供累计回显、提示符识别、登录/模式切换、
命令交换、残留清理和通用输出分析。
为什么：Juniper 和未来的 Cisco 都需要相同的 console 状态机，但提示符、
登录动作和错误格式不同；把这些差异收敛到 ConsoleProfile 数据中。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

try:
    from src.utils.telnet_utils import normalize_terminal_text
except ImportError:  # pragma: no cover - 独立测试时的回退路径
    from telnet_utils import normalize_terminal_text

log = logging.getLogger(__name__)


def _split_terminal_lines(buffer: str) -> tuple[list[str], str]:
    """拆分完整终端行。

    做什么：识别 CRLF、LF、CR，并保留末尾未完成的行。
    为什么：console 可能用不同换行形式，且 CR/LF 可能跨 chunk 到达。
    """
    lines = []
    start = 0
    index = 0
    while index < len(buffer):
        char = buffer[index]
        if char == "\n":
            lines.append(buffer[start:index])
            index += 1
            start = index
            continue
        if char == "\r":
            if index + 1 >= len(buffer):
                break
            lines.append(buffer[start:index])
            index += 2 if buffer[index + 1] == "\n" else 1
            start = index
            continue
        index += 1
    return lines, buffer[start:]


class ConsoleSessionError(RuntimeError):
    """Console 会话无法继续。

    做什么：表示提示符、登录或模式切换进入无法处理的状态。
    为什么：调用方需要把这类错误与普通命令错误和超时区分开。
    """


@dataclass(frozen=True)
class PromptRule:
    """一条提示符规则。

    做什么：为一种提示符绑定名称和正则。
    为什么：ConsoleSession 只按名称处理提示符，不关心厂商语法。
    """

    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class ModeStep:
    """一次模式切换动作。

    做什么：描述从某个提示符发送什么命令进入另一个提示符。
    为什么：Juniper 的 shell→exec→config 和 Cisco 的 exec→config 可以共用同一逻辑。
    """

    from_prompt: str
    to_prompt: str
    command: str


@dataclass(frozen=True)
class ConsoleProfile:
    """ConsoleSession 的厂商差异配置。

    做什么：集中保存提示符、登录动作、模式切换和文本识别规则。
    为什么：ConsoleSession 保持通用，Juniper/Cisco 只提供 profile 数据。
    """

    name: str
    prompts: tuple[PromptRule, ...]
    login_actions: Mapping[str, str]
    mode_steps: tuple[ModeStep, ...]
    command_prompt: str
    line_ending: bytes = b"\n"
    # 做什么：限定可视为启动完成的提示符；空元组表示任意提示符。
    # 为什么：避免启动日志中的非登录提示符或残留提示符提前结束等待。
    boot_prompts: tuple[str, ...] = ()
    error_patterns: tuple[re.Pattern[str], ...] = ()
    background_patterns: tuple[re.Pattern[str], ...] = ()
    noise_patterns: tuple[re.Pattern[str], ...] = ()
    warning_prefixes: tuple[str, ...] = ("warning:",)


@dataclass
class ConsoleResult:
    """一次命令交互的结果。

    做什么：保存累计输出、提示符名称和会话恢复信号。
    为什么：提示符、成功标记和登录状态需要分别判断，不能混为一个布尔值。
    """

    output: str = ""
    sent: bool = False
    prompt_name: str | None = None
    prompt_returned: bool = False
    restore_required: bool = False
    success_seen: bool = False
    disconnected: bool = False
    timed_out: bool = False


@dataclass
class _BootState:
    """启动等待期间的内部状态。

    做什么：保存累计输出、未完成行、候选提示符时间、探测状态和重连次数。
    为什么：启动循环需要跨多次读取保持状态；pending_line 处理不同换行形式。
    """

    last_data_at: float
    last_nudge_at: float
    output: str = ""
    pending_line: str = ""
    candidate_at: float | None = None
    probe_active: bool = False
    reconnects: int = 0


@dataclass
class _PromptState:
    """登录/模式切换期间的内部状态。

    做什么：保存缓冲区、截止时间、尝试次数和重连次数。
    为什么：登录和模式切换需要跨多次读取保持状态。
    """

    buffer: str
    deadline: float
    last_data_at: float
    last_nudge_at: float
    attempts: dict[str, int] = field(default_factory=dict)
    reconnects: int = 0


class TelnetTransport(Protocol):
    """ConsoleSession 需要的 Telnet 能力。

    做什么：描述连接状态、增量读取、显式行写入和异步重连。
    为什么：测试替身和 TelnetClient 都能按结构匹配，避免把具体实现耦合进会话层。
    """

    @property
    def is_connected(self) -> bool:
        """做什么：返回连接状态；为什么：会话层据此决定是否重连。"""
        ...

    async def read_available(self) -> str:
        """做什么：读取增量文本；为什么：命令交换需要累计回显。"""
        ...

    async def write_line(
        self,
        content: str | bytes,
        line_ending: bytes | None = None,
    ) -> None:
        """做什么：写入一行并指定结束符；为什么：厂商行结束符不同。"""
        ...

    async def reconnect_async(self) -> None:
        """做什么：异步重连；为什么：避免阻塞事件循环。"""
        ...


class ConsoleSession:
    """厂商无关的 console 会话。

    做什么：在 TelnetClient 之上完成提示符驱动的登录、模式切换和命令交换。
    为什么：业务层只关心命令和结果，不需要重复实现 console 状态机。
    """

    MAX_RECOVERIES: int = 3
    READ_INTERVAL: float = 0.2
    PROMPT_SETTLE: float = 0.5
    DRAIN_QUIET: float = 0.3
    DRAIN_MAX: float = 2.0
    BOOT_READY_SETTLE: float = 1.0
    BOOT_NUDGE_INTERVAL: float = 3.0

    def __init__(
        self,
        telnet_client: TelnetTransport,
        profile: ConsoleProfile,
    ) -> None:
        """保存原始连接和厂商 profile。

        做什么：注入 TelnetClient 与 ConsoleProfile。
        为什么：连接只负责收发，厂商差异全部由 profile 提供。
        """
        self._telnet: TelnetTransport = telnet_client
        self._profile: ConsoleProfile = profile

    async def wait_for_boot(self, timeout: float = 300) -> str:
        """等待设备启动到可交互的登录提示符。

        做什么：持续读取，处理断线、启动提示符和空行探测。
        为什么：启动日志里的提示符片段不代表 console 已经响应，必须探测。
        """
        loop = asyncio.get_running_loop()
        now = loop.time()
        deadline = now + timeout
        state = _BootState(
            last_data_at=now,
            last_nudge_at=now,
        )

        while loop.time() < deadline:
            try:
                chunk = await self._read_text()
            except ConnectionError as exc:
                await self._recover_boot_connection(state, exc)
                continue

            now = loop.time()
            if chunk:
                self._process_boot_chunk(state, chunk, now)
            if self._boot_ready(state, now):
                if not state.probe_active:
                    state.probe_active = True
                    state.candidate_at = None
                    state.pending_line = ""
                    state.last_nudge_at = now
                    log.info("Console boot prompt detected; sending newline probe")
                    await self._nudge()
                    continue
                return state.output
            if self._boot_should_nudge(state, now):
                await self._nudge()
                state.last_nudge_at = now
            await asyncio.sleep(self.READ_INTERVAL)

        raise TimeoutError(
            f"console boot prompt timeout; output={state.output[-4000:]!r}"
        )

    def _process_boot_chunk(
        self,
        state: _BootState,
        chunk: str,
        now: float,
    ) -> None:
        """处理一段启动输出。

        做什么：按完整行累计并识别允许的启动提示符。
        为什么：行缓冲能处理 CRLF/LF/CR 以及跨 chunk 的换行。
        """
        state.output += chunk
        state.last_data_at = now
        state.pending_line += chunk

        lines, state.pending_line = _split_terminal_lines(state.pending_line)
        for line in lines:
            self._process_boot_line(state, line, now)

        # 没有换行的提示符也要能识别。
        if state.pending_line:
            self._process_boot_line(state, state.pending_line, now)

    def _process_boot_line(
        self,
        state: _BootState,
        line: str,
        now: float,
    ) -> None:
        """处理一条启动输出行。

        做什么：规范化、跳过噪声，并记录允许的候选提示符。
        为什么：把行级匹配集中在一处，避免换行形式影响状态机。
        """
        normalized = normalize_terminal_text(line).strip()
        if not normalized:
            return
        if self._is_background_line(normalized) or self._is_noise_line(normalized):
            return

        prompt = self._prompt_kind(normalized)
        allowed = self._profile.boot_prompts
        if (
            state.candidate_at is None
            and prompt is not None
            and (not allowed or prompt in allowed)
        ):
            state.candidate_at = now
            log.info("Console boot prompt candidate: %s", prompt)

    def _boot_ready(self, state: _BootState, now: float) -> bool:
        """判断启动等待能否进入探测或返回。

        做什么：确认已出现允许的提示符并满足稳定窗口。
        为什么：探测前和探测后都依赖同一个稳定窗口。
        """
        return (
            state.candidate_at is not None
            and now - state.candidate_at >= self.BOOT_READY_SETTLE
        )

    def _boot_should_nudge(self, state: _BootState, now: float) -> bool:
        """判断启动阶段是否需要换行探测。

        做什么：长时间无输出且尚未执行过就绪探测时返回 True。
        为什么：提示符可能在连接建立前已经输出，需要主动触发显示；
        探测后不再重复发送空行。
        """
        return (
            not state.probe_active
            and now - state.last_data_at >= self.BOOT_NUDGE_INTERVAL
            and now - state.last_nudge_at >= self.BOOT_NUDGE_INTERVAL
        )

    async def _recover_boot_connection(
        self,
        state: _BootState,
        exc: ConnectionError,
    ) -> None:
        """处理启动阶段断线。

        做什么：限制重连次数后重连并重置提示符状态。
        为什么：启动阶段 console 可能短暂不可用，但不能无限重连。
        """
        state.reconnects += 1
        if state.reconnects > self.MAX_RECOVERIES:
            raise ConnectionError(f"console disconnected while waiting for boot: {exc}") from exc
        log.warning("Console disconnected during boot; reconnecting attempt=%s", state.reconnects)
        await self._reconnect()
        state.candidate_at = None
        state.pending_line = ""
        state.last_data_at = asyncio.get_running_loop().time()

    async def enter_command_prompt(
        self,
        initial_output: str = "",
        timeout: float = 300,
    ) -> None:
        """登录并切换到命令提示符。

        做什么：按 profile 的登录动作和模式步骤持续处理提示符。
        为什么：不同厂商可能经过 login、shell、exec、config 多个阶段。
        """
        loop = asyncio.get_running_loop()
        now = loop.time()
        state = _PromptState(
            buffer=initial_output or "",
            deadline=now + timeout,
            last_data_at=now,
            last_nudge_at=now,
        )

        while loop.time() < state.deadline:
            prompt = self._prompt_kind(state.buffer)
            if prompt == self._profile.command_prompt:
                return
            if await self._handle_prompt(prompt, state):
                state.buffer = ""
                state.last_data_at = loop.time()
                continue

            try:
                chunk = await self._read_text()
            except ConnectionError as exc:
                await self._recover_prompt_connection(state, exc)
                continue

            now = loop.time()
            if chunk:
                state.buffer += chunk
                state.last_data_at = now
            elif self._prompt_should_nudge(state, now):
                await self._nudge()
                state.last_nudge_at = now
            await asyncio.sleep(self.READ_INTERVAL)

        raise TimeoutError(
            f"console enter command prompt timeout; output={state.buffer[-4000:]!r}"
        )

    async def _handle_prompt(
        self,
        prompt: str | None,
        state: _PromptState,
    ) -> bool:
        """处理一个登录或模式切换提示符。

        做什么：查找登录动作或模式步骤，发送对应输入并累计尝试次数。
        为什么：ConsoleSession 不硬编码厂商命令，全部由 profile 决定。
        """
        if prompt is None:
            return False

        action = self._profile.login_actions.get(prompt)
        if action is not None:
            await self._send_action(state, prompt, action)
            return True

        step = next(
            (step for step in self._profile.mode_steps if step.from_prompt == prompt),
            None,
        )
        if step is not None:
            await self._send_action(state, prompt, step.command)
            return True

        raise ConsoleSessionError(f"unexpected console prompt: {prompt}")

    async def _send_action(
        self,
        state: _PromptState,
        prompt: str,
        content: str,
    ) -> None:
        """发送登录或模式切换输入。

        做什么：限制同一提示符的尝试次数后写入一行。
        为什么：错误凭据或模式切换失败时不能无限重试。
        """
        count = state.attempts.get(prompt, 0) + 1
        if count > self.MAX_RECOVERIES:
            raise ConsoleSessionError(f"console prompt retry limit reached: {prompt}")
        state.attempts[prompt] = count
        log.debug("Console prompt action: prompt=%s count=%s", prompt, count)
        await self._telnet.write_line(content, self._profile.line_ending)

    async def _recover_prompt_connection(
        self,
        state: _PromptState,
        exc: ConnectionError,
    ) -> None:
        """处理登录/模式切换阶段断线。

        做什么：限制重连次数后重连并清空旧缓冲区。
        为什么：旧会话输出不能用于新连接的状态判断。
        """
        state.reconnects += 1
        if state.reconnects > self.MAX_RECOVERIES:
            raise ConnectionError(f"console disconnected during login: {exc}") from exc
        log.warning("Console disconnected during login; reconnecting attempt=%s", state.reconnects)
        await self._reconnect()
        state.buffer = ""
        state.last_data_at = asyncio.get_running_loop().time()

    def _prompt_should_nudge(self, state: _PromptState, now: float) -> bool:
        """判断登录阶段是否需要换行探测。

        做什么：长时间无输出时返回 True。
        为什么：登录提示符可能在连接建立前已经输出。
        """
        return (
            now - state.last_data_at >= self.BOOT_NUDGE_INTERVAL
            and now - state.last_nudge_at >= self.BOOT_NUDGE_INTERVAL
        )

    async def ensure_ready(self) -> None:
        """确保下一条命令可以在命令提示符执行。

        做什么：先 drain 残留；如果停在其他提示符或断线，则恢复会话。
        为什么：写操作不能偷读回显，残留必须由业务层显式处理。
        """
        residual = await self.drain_residual()
        if residual:
            log.debug("Drained residual output: %r", residual[-2000:])

        prompt = self._prompt_kind(residual)
        if prompt == self._profile.command_prompt:
            return
        if prompt is not None or not self._telnet.is_connected:
            log.info("Console needs refresh: prompt=%s", prompt)
            await self.restore(residual)

    async def restore(
        self,
        initial_output: str = "",
        timeout: float = 300,
    ) -> None:
        """恢复 console 到命令提示符。

        做什么：必要时重连，然后复用登录/模式状态机。
        为什么：命令发送前必须确认终端处于可交互的命令模式。
        """
        if not self._telnet.is_connected:
            await self._reconnect()
            initial_output = ""
        await asyncio.sleep(self.READ_INTERVAL)
        await self.enter_command_prompt(initial_output, timeout)

    async def exchange(
        self,
        command: str,
        timeout: float,
        prompt_names: Iterable[str] | None = None,
        success_pattern: re.Pattern[str] | None = None,
    ) -> ConsoleResult:
        """发送一条命令并等待提示符或成功标记。

        做什么：只发送一次命令，累计读取并在期望提示符处结束。
        为什么：命令回显、提示符和成功标记都可能跨包，必须累计判断。

        结束条件只有三种：
        1. restore_required：出现非期望提示符或断线；
        2. 回到期望提示符并满足稳定窗口；
        3. 超时。
        """
        result = ConsoleResult()
        expected_prompts = (
            set(prompt_names) if prompt_names else {self._profile.command_prompt}
        )
        log.info("Console execute: %s", command)
        if not await self._send_exchange(command, result):
            return result

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        prompt_stable_since = None

        while loop.time() < deadline:
            chunk = await self._read_exchange_chunk(command, result)
            if chunk is None:
                break
            now = loop.time()
            if chunk:
                result.output += chunk
                stable_since = self._consume_exchange_output(
                    result, command, expected_prompts, success_pattern, now
                )
                if stable_since is not None and prompt_stable_since is None:
                    prompt_stable_since = stable_since
            if self._exchange_can_finish(
                result, prompt_stable_since, now, success_pattern
            ):
                break
            await asyncio.sleep(self.READ_INTERVAL)

        result.timed_out = not result.prompt_returned and not result.restore_required
        log.info(
            "Console execute done: prompt=%s returned=%s restore=%s success=%s",
            result.prompt_name,
            result.prompt_returned,
            result.restore_required,
            result.success_seen,
        )
        return result

    async def _send_exchange(
        self,
        command: str,
        result: ConsoleResult,
    ) -> bool:
        """发送命令并记录发送状态。

        做什么：写入完整命令行，失败时标记需要恢复会话。
        为什么：未发送成功的命令可以安全重试，不能与已发送但结果丢失混淆。
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

    async def _read_exchange_chunk(
        self,
        command: str,
        result: ConsoleResult,
    ) -> str | None:
        """读取一次命令增量。

        做什么：正常时返回文本；断线时标记状态并返回 None。
        为什么：断线要交给上层恢复会话，不能在这里吞掉。
        """
        try:
            return await self._read_text()
        except ConnectionError as exc:
            result.restore_required = True
            result.disconnected = True
            log.warning("Console read failed: node=%s command=%s detail=%s", self._profile.name, command, exc)
            return None

    def _consume_exchange_output(
        self,
        result: ConsoleResult,
        command: str,
        expected_prompts: set[str],
        success_pattern: re.Pattern[str] | None,
        now: float,
    ) -> float | None:
        """消费一段命令输出。

        做什么：识别期望提示符、非期望提示符和成功标记。
        为什么：提示符、会话恢复和成功标记必须分别记录。
        """
        prompt = self._prompt_kind(result.output)
        analysis = self.analysis_text(result.output, command)
        stable_since = None

        if prompt in expected_prompts:
            result.prompt_name = prompt
            result.prompt_returned = True
            stable_since = now
        elif prompt is not None:
            result.prompt_name = prompt
            result.restore_required = True
            return None

        if success_pattern is not None and success_pattern.search(analysis):
            result.success_seen = True
        return stable_since

    def _exchange_can_finish(
        self,
        result: ConsoleResult,
        prompt_stable_since: float | None,
        now: float,
        success_pattern: re.Pattern[str] | None,
    ) -> bool:
        """判断命令读取能否结束。

        做什么：处理会话恢复、提示符稳定窗口和成功标记宽限期。
        为什么：成功标记可能晚于提示符出现，需要很短的稳定窗口。
        """
        if result.restore_required:
            return True
        if prompt_stable_since is None:
            return False

        elapsed = now - prompt_stable_since
        if elapsed < self.PROMPT_SETTLE:
            return False

        required_settle = (
            self.PROMPT_SETTLE
            if success_pattern is None or result.success_seen
            else self.PROMPT_SETTLE * 2
        )
        return elapsed >= required_settle

    async def drain_residual(self) -> str:
        """读取并返回当前残留输出。

        做什么：在安静窗口内持续读取，直到没有新数据或达到上限。
        为什么：残留数据只能由业务层显式消费，传输层不能偷偷丢弃。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.DRAIN_MAX
        output = ""
        last_data_at = loop.time()

        while loop.time() < deadline:
            try:
                chunk = await self._read_text()
            except ConnectionError:
                break
            if chunk:
                output += chunk
                last_data_at = loop.time()
            elif loop.time() - last_data_at >= self.DRAIN_QUIET:
                break
            await asyncio.sleep(self.READ_INTERVAL)
        return output

    def analysis_text(self, output: str, command: str) -> str:
        """提取用于判断命令结果的有效文本。

        做什么：去掉命令 echo、提示符、后台日志和 profile 噪声行。
        为什么：错误和成功标记只能来自命令自身输出。
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

        做什么：只处理第一条等于命令或“提示符 + 命令”的行，保留其余内容。
        为什么：整段 find 截断可能误删命令前的有效输出，行级删除更精确。
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

        做什么：逐行跳过后台日志、warning 前缀和可选忽略模式。
        为什么：不同阶段需要不同错误模式，不能把所有错误混在一起。
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

        做什么：识别后台日志、warning 前缀和调用方指定的忽略模式。
        为什么：把过滤条件集中在一处，避免错误识别主循环过于复杂。
        """
        if self._is_background_line(line):
            return True
        if warning_prefixes and line.lower().startswith(warning_prefixes):
            return True
        return ignore_pattern is not None and bool(ignore_pattern.search(line))

    def _prompt_kind(self, text: str) -> str | None:
        """识别最后一行对应的提示符名称。

        做什么：按 profile.prompts 顺序匹配最后一条有效行。
        为什么：只匹配最后一行可以避免旧提示符干扰当前状态。
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

        做什么：跳过空行、后台日志和 profile 噪声行。
        为什么：提示符可能被后台日志包围，需要找到真正的最后一行。
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

        做什么：逐条应用 profile.prompts 的正则。
        为什么：分析命令输出和提交历史时需要排除提示符噪声。
        """
        stripped = line.strip()
        if not stripped or len(stripped) > 300:
            return False
        return any(rule.pattern.search(stripped) for rule in self._profile.prompts)

    def _is_background_line(self, line: str) -> bool:
        """判断是否为 profile 定义的后台日志。

        做什么：逐条应用 background_patterns。
        为什么：后台日志可能包含 error 单词，不能作为失败依据。
        """
        return any(pattern.search(line) for pattern in self._profile.background_patterns)

    def _is_noise_line(self, line: str) -> bool:
        """判断是否为 profile 定义的噪声行。

        做什么：逐条应用 noise_patterns。
        为什么：层级 banner 等非命令输出不应参与结果判断。
        """
        return any(pattern.search(line) for pattern in self._profile.noise_patterns)

    async def _read_text(self) -> str:
        """读取一次原始 Telnet 增量并转成文本。

        做什么：调用 TelnetClient.read_available，兼容返回字节的替身。
        为什么：ConsoleSession 只依赖这一个读取入口。
        """
        chunk = await self._telnet.read_available()
        if isinstance(chunk, (bytes, bytearray)):
            chunk = bytes(chunk).decode("utf-8", errors="replace")
        return chunk or ""

    async def _nudge(self) -> None:
        """发送一次显式换行。

        做什么：用 profile 的行结束符写入一个空行。
        为什么：提示符可能在连接建立前已经输出，行结束符由厂商 profile 决定。
        """
        try:
            await self._telnet.write_line("", self._profile.line_ending)
        except ConnectionError as exc:
            log.debug("Console nudge failed: %s", exc)

    async def _reconnect(self) -> None:
        """异步重连底层 Telnet。

        做什么：调用 TelnetClient.reconnect_async。
        为什么：避免同步 TCP 连接阻塞事件循环。
        """
        await self._telnet.reconnect_async()
