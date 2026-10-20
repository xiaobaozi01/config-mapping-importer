#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2024. All rights reserved.
"""把自适应后的配置导入 GNS3 中的 Juniper vJunos 路由器。

console 交互已经下沉到通用 ConsoleSession，本模块只保留 Juniper
导入业务和厂商 profile。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.application.config.manager.ne_config_manager import NeConfigManager
from src.common.config import AppSettings
from src.common.constant import EMULATION_CLASS
from src.emulation.emulation_platform import EmulationPlatform
from src.schemas.emulation.nodes import NodeCreateInfoFactory

try:
    # 生产代码位于 src 包内，不能依赖仓库根目录的临时布局。
    from src.utils.console_session import (
        ConsoleProfile,
        ConsoleResult,
        ConsoleSession,
        ModeStep,
        PromptName,
        PromptRule,
    )
except ImportError:  # pragma: no cover - 独立测试时的回退路径
    from console_session import (
        ConsoleProfile,
        ConsoleResult,
        ConsoleSession,
        ModeStep,
        PromptName,
        PromptRule,
    )

try:
    # 生产代码位于 src 包内，独立测试时需要回退到仓库根目录。
    from src.utils.telnet_utils import TelnetClient
except ImportError:  # pragma: no cover - 独立测试时的回退路径
    from telnet_utils import TelnetClient

log = logging.getLogger(__name__)

# 父类没有暴露删除节点的统一入口，旧的清理逻辑也使用该实例。
emulation_instance: EmulationPlatform = EMULATION_CLASS.instance()
app_settings = AppSettings.load_from_yaml()


class LoadStatus(str, Enum):
    """load 输出分类。

    字符串状态容易拼错，且不利于类型检查。
    """

    FAILED = "failed"
    COMPLETE = "complete"
    NO_COMPLETE = "no_complete"
    RECOVER = "recover"

    def __str__(self) -> str:
        """返回枚举值。"""
        return self.value


# ConsoleSession 保持厂商无关，Juniper 差异只放在 profile 中。
JUNOS_LOGIN_PATTERN = re.compile(r"(?im)^(?!Last[ \t]+login:)[^\r\n]*login:[ \t]*$")
JUNOS_PASSWORD_PATTERN = re.compile(r"(?im)^[^\r\n]*password:[ \t]*$")
JUNOS_SHELL_PATTERN = re.compile(r"(?m)^[^\s@:#]+@[^\s:#]+:[^\s#]*[ \t]*#[ \t]*$")
JUNOS_CLI_PATTERN = re.compile(r"(?m)^[^\s@>]+@[^\s>]*>[ \t]*$")
JUNOS_EDIT_PATTERN = re.compile(r"(?m)^[^\s@:#]+(?:@[^\s:#]*)?#[ \t]*$")
JUNOS_ERROR_PATTERN = re.compile(r"(?i)^error:")
JUNOS_BACKGROUND_PATTERN = re.compile(r"(?i)Auto Image Upgrade")
JUNOS_NOISE_PATTERN = re.compile(r"(?m)^\[edit[^\]]*\]\s*$")

JUNOS_PROMPTS = (
    PromptRule(PromptName.CONFIG, JUNOS_EDIT_PATTERN),
    PromptRule(PromptName.SHELL, JUNOS_SHELL_PATTERN),
    PromptRule(PromptName.EXEC, JUNOS_CLI_PATTERN),
    PromptRule(PromptName.LOGIN, JUNOS_LOGIN_PATTERN),
    PromptRule(PromptName.PASSWORD, JUNOS_PASSWORD_PATTERN),
)
JUNOS_MODE_STEPS = (
    ModeStep(PromptName.SHELL, "cli"),
    ModeStep(PromptName.EXEC, "configure"),
)


def build_junos_console_profile(username: str, password: str) -> ConsoleProfile:
    """构建 Juniper console profile。

    登录动作和模式命令由厂商定义，交互流程由 ConsoleSession 负责。
    """
    return ConsoleProfile(
        name="juniper-vjunos",
        prompts=JUNOS_PROMPTS,
        login_actions={
            PromptName.LOGIN: username,
            PromptName.PASSWORD: password,
        },
        mode_steps=JUNOS_MODE_STEPS,
        command_prompt=PromptName.CONFIG,
        line_ending=b"\n",
        error_patterns=(JUNOS_ERROR_PATTERN,),
        background_patterns=(JUNOS_BACKGROUND_PATTERN,),
        noise_patterns=(JUNOS_NOISE_PATTERN,),
    )


class JunosError(RuntimeError):
    """Juniper 导入流程的异常基类。

    调用方可以只捕获 JunosError，而不必依赖具体实现细节。
    """


class JunosCommandError(JunosError):
    """设备明确拒绝了命令。

    这类失败不能继续按成功处理，也不能被准备阶段的告警逻辑忽略。
    """


class JunosCommitVerificationError(JunosError):
    """提交历史与本次 UUID 不一致。

    提示符或 commit complete 都不能替代历史核实。
    """


class JunosResultUnconfirmed(JunosError):
    """命令已经发出，但结果无法确认。

    此时盲目重发可能造成重复提交，必须让调用方明确感知风险。
    """


@dataclass
class CommitRecord:
    """show system commit 中的一条提交记录。

    只有最新“已完成”记录的 comment 才能作为成功依据。
    """

    index: int
    comment: str
    pending: bool
    raw: str


# 新启动设备可能没有该必填项，准备阶段的 commit 会直接失败。
ROOT_AUTH_PROBE = "show system root-authentication | display set | no-more"
BOOTSTRAP_ROOT_HASH = (
    "$6$admin123$qkQUo1OlslMMLHPyx8kLQnBJKIZaPQsFkb7XxVevTdwU3L40gq0yjvrD2XbU1uKkZN38GonwTiTZnZ1euaZYB."
)
ROOT_AUTH_SET_COMMAND = (
    f'set system root-authentication encrypted-password "{BOOTSTRAP_ROOT_HASH}"'
)


class JuniperVjunosRouter23d2R1d15dConfigManager(NeConfigManager):
    """Juniper vJunos 配置导入管理器。

    console 交互已经由通用 ConsoleSession 承担，本类只保留业务编排。
    """

    DEFAULT_USER = "root"
    DEFAULT_PASSWD = "admin123"

    # 提交历史按查询次数限制，每次查询独立限时。
    COMMIT_HISTORY_MAX_ATTEMPTS = 60
    COMMIT_HISTORY_QUERY_TIMEOUT = 10
    MAX_RECOVERIES = 3

    # 测试和现场可能需要调整等待节奏。
    READ_INTERVAL = 0.2
    PROMPT_SETTLE = 0.5
    DRAIN_QUIET = 0.3
    DRAIN_MAX = 2.0
    NUDGE_INTERVAL = 3.0

    # 首次提交之外最多重试 60 次，已发出的提交仍不限时等待。
    COMMIT_LOCK_MAX_RETRIES = 60
    COMMIT_LOCK_RETRY_INTERVAL = 5
    COMMIT_HISTORY_RETRY_INTERVAL = 5
    COMMIT_HISTORY_MISMATCH_LIMIT = 2

    # 这些是 Juniper 导入业务规则，不属于通用 console 层。
    LOAD_COMPLETE_PATTERN = re.compile(r"(?im)^[ \t]*load complete(?:[ \t]*\((?P<errors>\d+)[ \t]+errors?\))?[ \t]*$")
    COMMIT_COMPLETE_PATTERN = re.compile(r"(?im)^[ \t]*commit complete[ \t]*$")
    COMMIT_FAILURE_PATTERN = re.compile(r"(?im)^[ \t]*configuration check-out failed\b")
    COMMIT_LOCK_PATTERN = re.compile(
        r"(?i)(?:(?:configuration database|config(?:uration)? db)[^\n]{0,120}\block(?:ed|ing)?\b"
        r"|\block(?:ed|ing)?\b[^\n]{0,120}(?:configuration database|config(?:uration)? db)"
        r"|could not (?:open|lock)[^\n]{0,80}(?:configuration database|config(?:uration)? db)"
        r"|lock-configuration failed)"
    )
    COMMIT_RECORD_PATTERN = re.compile(
        r"^\s*(?P<index>\d+)\s+"
        r"(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?"
        r"(?:\s+[A-Za-z]{2,5})?\s+by\s+\S+\s+via\s+\S+)"
        r"(?P<rest>.*)$",
        re.IGNORECASE,
    )
    COMMIT_COMMENT_LABEL_PATTERN = re.compile(r"(?i)^commit comment:?\s*(.*)$")

    def __init__(
        self,
        emulation: Any,
        project_id: str,
        node_id: str,
        host_located: str,
        node_compute_id: str | None = None,
    ) -> None:
        """初始化 Juniper 配置管理器。

        后续 FTP 连线、端口数和临时 IP 都依赖同一份节点信息。
        """
        super().__init__(emulation, project_id, node_id, host_located, node_compute_id)
        self.node_info = NodeCreateInfoFactory().get_create_info("juniper vjunos")

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    async def init_telnet_client(self) -> TelnetClient:
        """获取控制台客户端。

        不在这里调用 test_telnet_connect，因为它在启动阶段可能反复
        重连并消费当前提示符；状态识别和模式切换由 ConsoleSession 完成。
        """
        telnet_client: TelnetClient | None = await self.get_telnet_client()
        if telnet_client is None:
            raise ConnectionError(f"{self._node_id} Telnet console is not available")
        return telnet_client

    def _console_session(self, telnet_client: TelnetClient) -> ConsoleSession:
        """创建 Juniper console 会话。

        业务层只依赖 ConsoleSession 接口，同时保留测试可调的超时参数。
        """
        profile = build_junos_console_profile(self.DEFAULT_USER, self.DEFAULT_PASSWD)
        session = ConsoleSession(telnet_client, profile)
        session.MAX_RECOVERIES = self.MAX_RECOVERIES
        session.READ_INTERVAL = self.READ_INTERVAL
        session.PROMPT_SETTLE = self.PROMPT_SETTLE
        session.DRAIN_QUIET = self.DRAIN_QUIET
        session.DRAIN_MAX = self.DRAIN_MAX
        session.NUDGE_INTERVAL = self.NUDGE_INTERVAL
        return session


    async def _enter_config(self, session: ConsoleSession) -> None:
        """从当前终端状态进入配置顶层，检查 root 认证并关闭自动升级。

        import_cfg 和 init_device 的入口流程完全相同。
        """
        await session.enter_command_prompt()
        await self._ensure_root_authentication(session)
        await self._disable_auto_image_upgrade(session)

    async def import_cfg(self, local_cfg_path: str) -> None:
        """把本地配置导入路由器。

        FTP 创建、上传和连线是后续 load 的前提，任何一步失败都必须
        向上抛出；清理则放在 finally 中分别尝试。
        """
        telnet_client = None
        ftp_server = None
        try:
            telnet_client = await self.init_telnet_client()
            session = self._console_session(telnet_client)
            await self._enter_config(session)

            adapters = self.node_info.custom_adapters
            # FTP 节点、上传和连线属于通用能力，Juniper 侧不应重复实现。
            ftp_server = await self.init_ftp_server()
            await self.copy_file_to_ftp_server(ftp_server, local_cfg_path)
            # 最后一个口用于自环，倒数第二个口用于 FTP 导入。
            await self.link_ftp_server(ftp_server, adapters[-2].adapter_number)

            await self._prepare_config(session)
            await self._load_config(session)
            comment = await self._commit_and_verify(session, "final")
            log.info("Junos configuration imported: node=%s commit=%s", self._node_id, comment)
        finally:
            await self._cleanup(ftp_server, telnet_client)

    async def init_device(self, telnet_client: TelnetClient, sys_name: str | None) -> None:
        """执行设备初始化。

        这些是原有初始化业务，必须在正式导入前完成并提交。
        """
        if sys_name is None:
            return

        session = self._console_session(telnet_client)
        await self._enter_config(session)

        init_commands = (
            "top",
            f"set system host-name {sys_name}",
            "set chassis fpc 0 pic 0 number-of-ports 40",
        )
        await self._run_config_commands(session, init_commands)
        await self._commit_and_verify(session, "init-device")

    async def export_cfg(self, local_cfg_path: str) -> None:
        """创建空的导出文件。

        本次需求没有扩展导出业务，保留占位行为即可。
        """
        with open(local_cfg_path, "w", encoding="utf-8") as file_writer:
            file_writer.write("")

    # ------------------------------------------------------------------
    # 配置准备
    # ------------------------------------------------------------------
    async def _ensure_root_authentication(self, session: ConsoleSession) -> None:
        """确保 system root-authentication 存在。

        新启动设备可能没有该必填项，后续准备提交会失败。
        """
        await self._run_config_commands(session, ("top",))
        await session.ensure_ready()
        result = await session.exchange(ROOT_AUTH_PROBE)
        if result.restore_required or not result.prompt_returned:
            raise JunosResultUnconfirmed(
                f"{self._node_id} root-authentication probe did not return a prompt; "
                "result unconfirmed (结果未确认)"
            )
        text = session.analysis_text(result.output, ROOT_AUTH_PROBE)
        error = session.find_explicit_error(text)
        if error:
            raise JunosCommandError(f"{self._node_id} root-authentication probe failed: {error}")
        if re.search(r"(?m)^set system root-authentication (?:encrypted-password|ssh-[\w-]+)\s+\S", text):
            return
        if text.strip():
            raise JunosResultUnconfirmed(f"{self._node_id} unexpected root authentication output")
        await session.ensure_ready()
        result = await session.exchange(ROOT_AUTH_SET_COMMAND)
        self._handle_command_result(session, result, ROOT_AUTH_SET_COMMAND)
        await self._commit_and_verify(session, "bootstrap-root-auth")

    async def _disable_auto_image_upgrade(self, session: ConsoleSession) -> None:
        """先单独关闭自动升级并核实提交，再进行后续配置。"""
        await self._run_config_commands(session, ("top", "delete chassis auto-image-upgrade"))
        await self._commit_and_verify(session, "disable-auto-image-upgrade")

    async def _prepare_config(self, session: ConsoleSession) -> None:
        """执行导入前的准备命令。

        准备命令作用于同一个 candidate，没必要每条都产生 commit。
        """
        port_count = len(self.node_info.custom_adapters) - 1
        interface = self.node_info.custom_adapters[-2].port_name
        prepare_commands = (
            "top",
            f"set chassis fpc 0 pic 0 number-of-ports {port_count}",
            f"set interfaces {interface} unit 0 family inet address "
            f"{self.FTP_CLIENT_IP}/24",
        )
        await self._run_config_commands(session, prepare_commands)
        await self._commit_and_verify(session, "prepare-config")

    async def _run_config_commands(self, session: ConsoleSession, commands: tuple[str, ...]) -> None:
        """执行一组必须成功的配置命令。

        准备和初始化共享相同的发送与结果检查流程。
        """
        for command in commands:
            await session.ensure_ready()
            result = await session.exchange(command)
            self._handle_command_result(session, result, command)

    def _handle_command_result(self, session: ConsoleSession, result: ConsoleResult, command: str) -> None:
        """检查必须成功的命令结果。

        准备和初始化命令都是固定命令，失败应立即中止。
        """
        if result.restore_required:
            raise JunosResultUnconfirmed(
                f"{self._node_id} Junos session lost during {command!r}; "
                "result unconfirmed (结果未确认)"
            )
        text = session.analysis_text(result.output, command)
        error = session.find_explicit_error(text)
        if error:
            raise JunosCommandError(
                f"{self._node_id} Junos command failed ({command}): {error}\n"
                f"{self._tail(result.output)}"
            )
        if not result.prompt_returned:
            raise JunosResultUnconfirmed(
                f"{self._node_id} Junos command did not return a prompt "
                f"({command}); result unconfirmed (结果未确认)"
            )

    # ------------------------------------------------------------------
    # 配置加载
    # ------------------------------------------------------------------
    async def _load_config(self, session: ConsoleSession) -> None:
        """加载配置文件并确认加载结果。

        只有明确的 load complete 才能进入提交阶段，提示符本身不算成功。
        """
        command = (
            f"load override ftp://ftpuser:ftpuser@{self.FTP_SERVER_IP}/{self._node_id}.cfg"
        )
        recoveries = 0

        for _ in range(self.MAX_RECOVERIES + 1):
            await session.ensure_ready()
            result = await session.exchange(command)
            text = session.analysis_text(result.output, command)
            explicit_error = session.find_explicit_error(text)
            status, detail = self._classify_load(result, text, explicit_error)

            if status is LoadStatus.FAILED:
                raise JunosCommandError(f"{self._node_id} Junos load failed: {detail}")
            if status is LoadStatus.COMPLETE:
                # 已经核实成功的阶段不应因会话刷新而重复执行。
                if result.prompt_returned:
                    return
                await self._restore_or_unconfirmed(session, result.output, "load recovery")
                return
            if status is LoadStatus.NO_COMPLETE:
                raise JunosCommandError(
                    f"{self._node_id} Junos load returned to the prompt without "
                    f"'load complete':\n{self._tail(result.output)}"
                )

            recoveries += 1
            if recoveries > self.MAX_RECOVERIES:
                raise JunosResultUnconfirmed(
                    f"{self._node_id} Junos load result could not be confirmed; "
                    "result unconfirmed (结果未确认)"
                )
            # 提交尚未发送，重新 load 不会产生重复提交风险。
            await self._restore_or_unconfirmed(session, result.output, "load recovery")

        raise JunosResultUnconfirmed(
            f"{self._node_id} Junos load result could not be confirmed after "
            f"{self.MAX_RECOVERIES} recoveries; result unconfirmed (结果未确认)"
        )

    def _classify_load(
        self,
        result: ConsoleResult,
        text: str,
        explicit_error: str | None,
    ) -> tuple[LoadStatus, str | None]:
        """分类 load 的输出。

        load complete 与非零错误、提示符之间不能互相替代。
        """
        counts = [
            int(match.group("errors") or 0)
            for match in self.LOAD_COMPLETE_PATTERN.finditer(text)
        ]
        complete = bool(counts)
        error_count = max(counts, default=0)

        if error_count > 0:
            return LoadStatus.FAILED, f"{error_count} error(s):\n{self._tail(result.output)}"
        if complete:
            if explicit_error:
                log.warning(
                    "Junos load complete with extra error text; trusting load count: "
                    "node=%s detail=%s",
                    self._node_id,
                    explicit_error,
                )
            return LoadStatus.COMPLETE, None
        if explicit_error:
            return LoadStatus.FAILED, f"{explicit_error}\n{self._tail(result.output)}"
        if result.prompt_returned:
            return LoadStatus.NO_COMPLETE, None
        return LoadStatus.RECOVER, None

    # ------------------------------------------------------------------
    # 提交与历史核实
    # ------------------------------------------------------------------
    async def _commit_and_verify(self, session: ConsoleSession, stage: str) -> str:
        """提交配置并核实最新提交记录。

        只有最新已完成 comment 与本次 UUID 一致，才能证明提交成功。
        """
        comment = f"cfg-import-{uuid.uuid4()}"
        command = f'commit comment "{comment}"'
        lock_retries = 0
        recoveries = 0
        log.info("Junos commit start: node=%s stage=%s comment=%s", self._node_id, stage, comment)

        while True:
            result, complete, locked, failure, error = await self._run_commit_command(session, command)
            if not result.sent:
                recoveries += 1
                if recoveries > self.MAX_RECOVERIES:
                    raise JunosResultUnconfirmed(
                        f"{self._node_id} Junos commit command could not be sent; "
                        "result unconfirmed (结果未确认)"
                    )
                await self._restore_or_unconfirmed(session, result.output, f"commit {comment}")
                continue
            if await self._retry_locked_commit(result, complete, locked, lock_retries):
                lock_retries += 1
                continue
            if not complete and (failure or error):
                raise JunosCommandError(
                    f"{self._node_id} Junos commit failed ({stage}): "
                    f"{error or 'configuration check-out failed'}\n"
                    f"{self._tail(result.output)}"
                )
            return await self._finish_commit_verification(session, result, comment, stage, complete)

    async def _run_commit_command(
        self,
        session: ConsoleSession,
        command: str,
    ) -> tuple[ConsoleResult, bool, bool, bool, str | None]:
        """发送前检查会话，发送后不限时等候提交结果。"""
        await session.ensure_ready()
        result = await session.exchange(command, success_pattern=self.COMMIT_COMPLETE_PATTERN)
        text = session.analysis_text(result.output, command)
        return (
            result,
            bool(self.COMMIT_COMPLETE_PATTERN.search(text)),
            bool(self.COMMIT_LOCK_PATTERN.search(text)),
            bool(self.COMMIT_FAILURE_PATTERN.search(text)),
            session.find_explicit_error(text, ignore_pattern=self.COMMIT_LOCK_PATTERN),
        )

    async def _retry_locked_commit(self, result: ConsoleResult, complete: bool, locked: bool, retries: int) -> bool:
        """只对明确锁拒绝且提示符已返回的提交进行有限重试。"""
        if complete or not locked or not result.prompt_returned:
            return False
        if retries >= self.COMMIT_LOCK_MAX_RETRIES:
            raise JunosCommandError(
                f"{self._node_id} Junos commit failed: configuration database locked; "
                f"lock retry limit reached ({self.COMMIT_LOCK_MAX_RETRIES} retries)\n"
                f"{self._tail(result.output)}"
            )
        log.warning(
            "Junos configuration database is locked; retrying commit: node=%s retry=%s/%s interval=%ss",
            self._node_id, retries + 1, self.COMMIT_LOCK_MAX_RETRIES, self.COMMIT_LOCK_RETRY_INTERVAL,
        )
        await asyncio.sleep(self.COMMIT_LOCK_RETRY_INTERVAL)
        return True

    async def _finish_commit_verification(
        self,
        session: ConsoleSession,
        result: ConsoleResult,
        comment: str,
        stage: str,
        complete: bool,
    ) -> str:
        """必要时恢复会话，核实提交历史并返回 comment。

        只有最新已完成 comment 与本次一致才算成功。
        """
        if result.restore_required or not result.prompt_returned:
            await self._restore_or_unconfirmed(session, result.output, f"commit {comment}")
        verified = await self._verify_commit_history(session, comment)
        if verified is True:
            log.info("Junos commit verified: node=%s stage=%s comment=%s", self._node_id, stage, comment)
            return comment
        if verified is False and complete:
            raise JunosCommitVerificationError(
                f"{self._node_id} latest completed commit comment does not match "
                f"{comment!r} ({stage})"
            )
        raise JunosResultUnconfirmed(
            f"{self._node_id} Junos commit result could not be verified ({stage}); "
            "result unconfirmed (结果未确认)"
        )

    async def _verify_commit_history(self, session: ConsoleSession, comment: str) -> bool | None:
        """循环核实最新已完成提交记录。

        命令回显、旧记录和 pending 记录都不能证明提交成功。
        """
        mismatches = 0
        for attempt in range(self.COMMIT_HISTORY_MAX_ATTEMPTS):
            try:
                latest = await asyncio.wait_for(
                    self._run_history_query(session, comment), timeout=self.COMMIT_HISTORY_QUERY_TIMEOUT,
                )
            except TimeoutError:
                latest = None
                log.warning("Junos commit history query timed out: node=%s attempt=%s", self._node_id, attempt + 1)
            if latest == comment:
                return True
            if latest is not None:
                mismatches += 1
                log.warning(
                    "Latest completed Junos commit comment mismatch: node=%s expected=%r actual=%r",
                    self._node_id,
                    comment,
                    latest,
                )
                if mismatches >= self.COMMIT_HISTORY_MISMATCH_LIMIT:
                    return False
            if attempt + 1 < self.COMMIT_HISTORY_MAX_ATTEMPTS:
                await asyncio.sleep(self.COMMIT_HISTORY_RETRY_INTERVAL)
        log.warning("Junos commit history verification unconfirmed: node=%s comment=%s", self._node_id, comment)
        return None

    async def _run_history_query(self, session: ConsoleSession, comment: str) -> str | None:
        """执行一次提交历史查询并返回最新已完成 comment。

        查询失败、pending 或无记录时返回 None，由外层继续等待。
        """
        query = "run show system commit | no-more"
        try:
            await session.ensure_ready()
            result = await session.exchange(query)
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            log.warning("Junos commit history query failed: node=%s detail=%s", self._node_id, exc)
            return None
        text = session.analysis_text(result.output, query)
        records = self._parse_commit_history(text)
        if not records:
            error = session.find_explicit_error(text)
            if error:
                raise JunosResultUnconfirmed(f"{self._node_id} Junos commit history query failed: {error}")
            return None
        if any(record.pending and record.comment == comment for record in records):
            return None
        latest_completed = next((record for record in records if not record.pending), None)
        return latest_completed.comment if latest_completed is not None else None

    def _parse_commit_history(self, text: str) -> list[CommitRecord]:
        """解析 show system commit 输出。

        提交历史需要结构化比较，不能只在整个输出中搜索 UUID。
        """
        records = []
        current = None
        for line in text.splitlines():
            record = self._match_commit_record(line)
            if record is not None:
                records.append(record)
                current = record
                continue
            if current is not None:
                self._append_commit_line(current, line)
        return records

    def _match_commit_record(self, line: str) -> CommitRecord | None:
        """尝试把一行解析成提交记录。

        记录行是提交历史的结构边界，后续行都属于当前记录。
        """
        match = self.COMMIT_RECORD_PATTERN.match(line)
        if match is None:
            return None

        record = CommitRecord(
            index=int(match.group("index")),
            comment="",
            pending=bool(re.search(r"(?i)\bpending\b", match.group(0))),
            raw=line.rstrip(),
        )
        inline_text = match.group("rest").strip()
        if inline_text:
            inline_text = re.sub(r"(?i)\b(?:rollback\s+)?pending\b", "", inline_text).strip()
        if inline_text:
            label = self.COMMIT_COMMENT_LABEL_PATTERN.match(inline_text)
            if label is not None:
                inline_text = label.group(1).strip()
            record.comment = inline_text.strip().strip('"')
        return record

    def _append_commit_line(self, record: CommitRecord, line: str) -> None:
        """把记录后的续行归入当前记录。

        show system commit 的 comment 通常位于记录下一行，需要归属到正确记录。
        """
        stripped = line.strip()
        if self._is_commit_noise_line(stripped):
            return
        if not record.comment:
            label = self.COMMIT_COMMENT_LABEL_PATTERN.match(stripped)
            comment = label.group(1).strip() if label is not None else stripped
            record.comment = comment.strip().strip('"')
        if re.search(r"(?i)\bpending\b", stripped):
            record.pending = True

    def _is_commit_noise_line(self, stripped: str) -> bool:
        """判断提交历史续行是否为噪声。

        这些行不能作为 comment，否则会污染最新提交记录的比较。
        """
        if not stripped:
            return True
        if any(rule.pattern.search(stripped) for rule in JUNOS_PROMPTS):
            return True
        if stripped.startswith("---") or JUNOS_NOISE_PATTERN.search(stripped):
            return True
        if JUNOS_BACKGROUND_PATTERN.search(stripped):
            return True
        return stripped.lower().startswith(("warning:", "error:"))

    # ------------------------------------------------------------------
    # 会话恢复与清理
    # ------------------------------------------------------------------
    async def _restore_or_unconfirmed(self, session: ConsoleSession, initial_output: str, context: str) -> None:
        """恢复会话，失败时报告结果未确认。

        命令已经发出时，恢复失败意味着结果无法核实，不能静默重发。
        """
        try:
            await session.restore(initial_output)
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            raise JunosResultUnconfirmed(
                f"{self._node_id} Junos {context} could not restore the console; "
                f"result unconfirmed (结果未确认): {exc}"
            ) from exc

    async def _cleanup(self, ftp_server, telnet_client: TelnetClient | None) -> None:
        """回收本次导入创建的资源，保持各项清理相互独立。"""
        if ftp_server is not None:
            try:
                emulation = getattr(self, "_emulation", None) or emulation_instance
                await emulation.delete_node(self._project_id, ftp_server.node_id)
            except Exception:  # noqa: BLE001 - 清理不能覆盖导入异常
                log.exception(
                    "Failed to delete temporary FTP node: node=%s ftp_node=%s",
                    self._node_id, getattr(ftp_server, "node_id", None),
                )
        if telnet_client is not None:
            try:
                telnet_client.close()
            except Exception:  # noqa: BLE001 - 独立执行各项清理
                log.exception("Failed to close Telnet client: node=%s", self._node_id)
        try:
            await self.loopback_last_adapter_for_uni()
        except Exception:  # noqa: BLE001 - 保留原始导入结果
            log.exception("Failed to configure the last-adapter loopback: node=%s", self._node_id)

    @staticmethod
    def _tail(text: str, length: int = 4000) -> str:
        """截取异常信息需要的末尾输出。"""
        return text[-length:]
