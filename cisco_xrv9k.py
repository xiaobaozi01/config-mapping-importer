#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2024. All rights reserved.
"""把自适应后的配置导入 GNS3 中的 Cisco IOS XRv9k 路由器。

console 交互已经下沉到通用 ConsoleSession，本模块只保留 Cisco 业务和 profile。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any

from src.application.config.manager.ne_config_manager import NeConfigManager
from src.common.config import AppSettings
from src.common.constant import EMULATION_CLASS
from src.emulation.emulation_platform import EmulationPlatform
from src.schemas.emulation.nodes import NodeCreateInfoFactory

try:
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
    from src.utils.telnet_utils import TelnetClient
except ImportError:  # pragma: no cover - 独立测试时的回退路径
    from telnet_utils import TelnetClient

log = logging.getLogger(__name__)

# 父类没有暴露删除节点入口，现有 Cisco 逻辑也使用全局实例。
emulation_instance: EmulationPlatform = EMULATION_CLASS.instance()
app_settings = AppSettings.load_from_yaml()


CISCO_PROMPTS: tuple[PromptRule, ...] = (
    PromptRule(
        PromptName.CONFIG,
        re.compile(r"(?m)^RP/\S+\(config[^)]*\)#[ \t]*$"),
    ),
    PromptRule(PromptName.EXEC, re.compile(r"(?m)^RP/\S+#[ \t]*$")),
    PromptRule(PromptName.ROOT_USERNAME, re.compile(r"(?im)^Enter root-system username:[ \t]*$")),
    PromptRule(PromptName.SECRET, re.compile(r"(?im)^Enter secret(?: again)?:[ \t]*$")),
    PromptRule(PromptName.USERNAME, re.compile(r"(?im)^Username:[ \t]*$")),
    PromptRule(PromptName.PASSWORD, re.compile(r"(?im)^Password:[ \t]*$")),
    # 不要用通用 [yes/no] 猜测未知交互，避免把普通输出当确认。
    PromptRule(PromptName.CONFIRM, re.compile(r"(?im)^Do you wish to proceed[^\r\n]*\?(?:[ \t]*\[no\]:)?[ \t]*$")),
)

# 命令都是确定的，正常情况下不应出现；保留用于防御。
CISCO_CLI_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)^% (?:Invalid input|Incomplete command|Ambiguous command)"),
    re.compile(r"(?im)^% Authorization failed"),
)

# 语法错误以 show configuration failed load 为准，文件错误发生在它之前。
CISCO_LOAD_ERROR_PATTERNS = CISCO_CLI_ERROR_PATTERNS + (
    re.compile(r"(?im)^.*!! CONFIGURATION FAILED"),
    re.compile(r"(?im)^.*config: failed to open input file"),
    re.compile(r"(?im)^.*Resource temporarily unavailable"),
    re.compile(r"(?im)^.*Syntax/Authorization errors"),
)

# % Failed to commit 是官方 commit 失败提示，conflicts 属于 apply 阶段。
CISCO_COMMIT_ERROR_PATTERNS = CISCO_CLI_ERROR_PATTERNS + (
    re.compile(r"(?im)^% Failed to commit one or more configuration items"),
    re.compile(r"(?im)^.*Failed to commit one or more configuration items " r"during a pseudo-atomic operation"),
    re.compile(r"(?im)^.*conflicts with other IPv4 addresses"),
)

# 实际日志可能带时间戳和进程前缀，不能锚定行首的 %。
CISCO_BACKGROUND_PATTERN = re.compile(r"(?i)%[A-Z0-9_]+(?:-[A-Z0-9_]+)*-\d+-[A-Z0-9_]+")
# commit label 会替代自动 ID 显示在最新一行。
CISCO_COMMIT_ROW_PATTERN = re.compile(r"(?m)^\s*1\s+(\S+)\s+")


def build_cisco_console_profile(username: str, password: str) -> ConsoleProfile:
    """构建 Cisco IOS XR console profile。

    ConsoleSession 保持厂商无关，Cisco 差异只放在 profile 中。
    """
    return ConsoleProfile(
        name="cisco-xrv9k",
        prompts=CISCO_PROMPTS,
        login_actions={
            PromptName.ROOT_USERNAME: username,
            PromptName.SECRET: password,
            PromptName.USERNAME: username,
            PromptName.PASSWORD: password,
        },
        mode_steps=(
            ModeStep(PromptName.EXEC, "configure"),
        ),
        command_prompt=PromptName.CONFIG,
        line_ending=b"\r\n",
        error_patterns=CISCO_CLI_ERROR_PATTERNS,
        background_patterns=(CISCO_BACKGROUND_PATTERN,),
    )


class CiscoError(RuntimeError):
    """Cisco 导入流程的异常基类。

    调用方可以只捕获 CiscoError。
    """


class CiscoCommandError(CiscoError):
    """设备明确拒绝了命令。

    这类失败不能继续按成功处理。
    """


class CiscoCommitVerificationError(CiscoError):
    """提交记录与本次 label 不一致。

    提示符不能替代提交历史核实。
    """


class CiscoResultUnconfirmed(CiscoError):
    """命令已经发出，但结果无法确认。

    盲目重发可能造成重复提交。
    """


class CiscoCandidateLost(CiscoResultUnconfirmed):
    """配置会话已恢复；最终提交前必须重新加载目标配置。"""


class CiscoIOSXRV9kd7d11d1ConfigManager(NeConfigManager):
    """Cisco IOS XRv9k 配置导入管理器。

    console 状态机由 ConsoleSession 承担，本类只保留 Cisco 业务。
    """

    DEFAULT_USER = "root"
    DEFAULT_PASSWD = app_settings.second_cer
    FTP_USER = app_settings.ftp_user
    FTP_PWD = app_settings.ftp_pwd

    # 提交历史按查询次数限制，每次查询独立限时。
    COMMIT_HISTORY_MAX_ATTEMPTS = 60
    COMMIT_HISTORY_QUERY_TIMEOUT = 10
    MAX_RECOVERIES = 3

    # commit 历史可能短暂滞后，不能只查询一次就判失败。
    COMMIT_HISTORY_RETRY_INTERVAL = 5
    COMMIT_HISTORY_MISMATCH_LIMIT = 2

    # XR 的 commit label 最长 20 个字符，无法直接使用完整 UUID。
    COMMIT_LABEL_PREFIX = "ci-"
    COMMIT_LABEL_UUID_CHARS = 16

    # 测试和现场可能需要调整等待节奏。
    READ_INTERVAL = 0.2
    PROMPT_SETTLE = 0.5
    DRAIN_QUIET = 0.3
    DRAIN_MAX = 2.0
    NUDGE_INTERVAL = 3.0

    def __init__(
        self,
        emulation: Any,
        project_id: str,
        node_id: str,
        host_located: str,
        node_compute_id: str | None = None,
    ) -> None:
        """保存节点信息。

        后续 FTP 适配器选择依赖 node_info。
        """
        super().__init__(emulation, project_id, node_id, host_located, node_compute_id)
        self.node_info = NodeCreateInfoFactory().get_create_info("cisco xrv9k")

    async def init_telnet_client(self) -> TelnetClient:
        """获取 Cisco console 客户端。

        不调用 test_telnet_connect，避免它消费登录提示符。
        """
        telnet_client: TelnetClient | None = await self.get_telnet_client()
        if telnet_client is None:
            raise ConnectionError(f"{self._node_id} Telnet console is not available")
        return telnet_client

    def _console_session(self, telnet_client: TelnetClient) -> ConsoleSession:
        """创建 Cisco console 会话。

        业务层只依赖 ConsoleSession 接口。
        """
        profile = build_cisco_console_profile(self.DEFAULT_USER, self.DEFAULT_PASSWD)
        session = ConsoleSession(telnet_client, profile)
        session.MAX_RECOVERIES = self.MAX_RECOVERIES
        session.READ_INTERVAL = self.READ_INTERVAL
        session.PROMPT_SETTLE = self.PROMPT_SETTLE
        session.DRAIN_QUIET = self.DRAIN_QUIET
        session.DRAIN_MAX = self.DRAIN_MAX
        session.NUDGE_INTERVAL = self.NUDGE_INTERVAL
        return session


    def _import_adapter(self):
        """取得 FTP 使用的适配器。

        保持当前 Cisco 导入使用第一个数据口的业务行为。
        """
        adapters = getattr(self.node_info, "custom_adapters", None) or []
        if not adapters:
            raise CiscoError(f"{self._node_id} needs at least one custom adapter")
        return adapters[0]

    async def import_cfg(self, local_cfg_path: str) -> None:
        """把本地配置导入 XRv9k。

        FTP 创建和上传是 load 的前提；清理放在 finally 中。
        """
        telnet_client: TelnetClient | None = None
        ftp_server = None
        try:
            telnet_client = await self.init_telnet_client()
            session = self._console_session(telnet_client)
            await session.enter_command_prompt()
            await self._disable_paging(session)

            ftp_server = await self.init_ftp_server()
            await self.copy_file_to_ftp_server(ftp_server, local_cfg_path)
            adapter = self._import_adapter()
            await self.link_ftp_server(ftp_server, adapter.adapter_number)

            await self._prepare_ftp(session, adapter)
            label = await self._load_and_commit(session)
            log.info("Cisco XRv9k configuration imported: node=%s label=%s", self._node_id, label)
        finally:
            await self._cleanup(ftp_server, telnet_client)

    async def init_device(self, telnet_client: TelnetClient, sys_name: str) -> None:
        """执行设备初始化。

        这是原有 Cisco 初始化业务，必须确认成功。
        """
        if not sys_name:
            return
        session = self._console_session(telnet_client)
        await session.enter_command_prompt()
        await self._disable_paging(session)
        await self._run_command(session, f"hostname {sys_name}")
        await self._commit_and_verify(session, "init-device", replace=False)

    async def export_cfg(self, local_cfg_path: str) -> None:
        """保留导出占位行为。

        保持公开接口不变。
        """
        pass

    async def _prepare_ftp(self, session: ConsoleSession, adapter) -> None:
        """准备 FTP 源接口和地址。

        load 需要路由器能访问 FTP 服务器。
        """
        commands = (
            "logging console disable",
            f"ftp client source-interface {adapter.port_name}",
            f"interface {adapter.port_name}",
            f"ipv4 address {self.FTP_CLIENT_IP} 255.255.255.0",
            "no shutdown",
        )
        for command in commands:
            await self._run_command(session, command)
        await self._commit_and_verify(session, "prepare-ftp", replace=False)
        # load 只能在全局配置模式执行，不能停在 config-if 子模式。
        await self._run_command(session, "end", prompt_names=(PromptName.EXEC,))
        await self._run_command(session, "configure")

    async def _disable_paging(self, session: ConsoleSession) -> None:
        """尽力关闭终端分页。

        提交历史可能分页，分页提示会阻止 prompt 识别。
        """
        try:
            result = await session.exchange("do terminal length 0", prompt_names={PromptName.CONFIG})
            if not result.prompt_returned:
                log.warning("Cisco terminal length command did not return a prompt")
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            log.warning("Cisco terminal length command failed: %s", exc)

    async def _load_and_commit(self, session: ConsoleSession) -> str:
        """提交前会话丢失时有限重载；提交已发出则只核实历史。"""
        for attempt in range(self.MAX_RECOVERIES + 1):
            try:
                generation = await self._load_config(session)
                return await self._commit_and_verify(session, "final", loaded_generation=generation)
            except CiscoCandidateLost:
                if attempt >= self.MAX_RECOVERIES:
                    raise
                log.warning("Cisco candidate lost before commit; reloading: node=%s", self._node_id)
        raise CiscoResultUnconfirmed("Cisco reload retry limit reached")

    async def _load_config(self, session: ConsoleSession) -> int:
        """加载 FTP 上的配置文件。

        load 的语法错误以 Cisco 失败视图为准，文件错误则在 load 回显中。
        """
        command = (
            f"load ftp://{self.FTP_USER}:{self.FTP_PWD}@"
            f"{self.FTP_SERVER_IP}/{self._node_id}.cfg"
        )
        try:
            await self._run_command(session, command, patterns=CISCO_LOAD_ERROR_PATTERNS)
            generation = session.recovery_generation
            await self._run_command(
                session, "show configuration failed load",
                patterns=CISCO_LOAD_ERROR_PATTERNS, expected_generation=generation,
            )
            return generation
        except CiscoCandidateLost:
            raise
        except CiscoResultUnconfirmed as exc:
            await session.restore()
            raise CiscoCandidateLost(f"{self._node_id} Cisco load requires retry") from exc

    # ------------------------------------------------------------------
    # 提交与历史核实
    # ------------------------------------------------------------------
    async def _commit_and_verify(
        self,
        session: ConsoleSession,
        stage: str,
        replace: bool = True,
        loaded_generation: int | None = None,
    ) -> str:
        """用带 label 的 commit 提交配置并核实。

        只有最新 label 与本次 UUID 一致，才能证明本次提交成功。
        """
        label = f"{self.COMMIT_LABEL_PREFIX}{uuid.uuid4().hex[:self.COMMIT_LABEL_UUID_CHARS]}"
        command = f"commit label {label}" + (" replace" if replace else "")
        log.info("Cisco commit start: node=%s stage=%s label=%s", self._node_id, stage, label)
        commit_error: CiscoResultUnconfirmed | None = None
        try:
            await self._run_command(
                session,
                command,
                patterns=CISCO_COMMIT_ERROR_PATTERNS,
                allow_confirm=True,
                expected_generation=loaded_generation,
            )
        except CiscoCandidateLost:
            raise
        except CiscoResultUnconfirmed as exc:
            commit_error = exc
            log.warning("Cisco commit result lost; checking history: node=%s label=%s", self._node_id, label)

        verified = await self._verify_commit_history(session, label)
        if verified is True:
            log.info("Cisco XRv9k commit verified: node=%s label=%s", self._node_id, label)
            return label
        if verified is False:
            if commit_error is not None:
                raise commit_error
            raise CiscoCommitVerificationError(
                f"{self._node_id} latest Cisco commit label does not match "
                f"{label!r} ({stage})"
            )
        if commit_error is not None:
            raise commit_error
        raise CiscoResultUnconfirmed(
            f"{self._node_id} Cisco commit result could not be verified ({stage}); "
            "result unconfirmed (结果未确认)"
        )

    async def _verify_commit_history(self, session: ConsoleSession, label: str) -> bool | None:
        """循环核实最新提交记录。

        提交历史可能短暂滞后，单次查询会把成功误判为未确认。
        """
        mismatches = 0
        for attempt in range(self.COMMIT_HISTORY_MAX_ATTEMPTS):
            try:
                latest = await asyncio.wait_for(
                    self._run_history_query(session), timeout=self.COMMIT_HISTORY_QUERY_TIMEOUT,
                )
            except TimeoutError:
                latest = None
                log.warning("Cisco commit history query timed out: node=%s attempt=%s", self._node_id, attempt + 1)
            if latest == label:
                return True
            if latest is not None:
                mismatches += 1
                log.warning(
                    "Latest Cisco commit label mismatch: node=%s expected=%r actual=%r",
                    self._node_id,
                    label,
                    latest,
                )
                if mismatches >= self.COMMIT_HISTORY_MISMATCH_LIMIT:
                    return False
            if attempt + 1 < self.COMMIT_HISTORY_MAX_ATTEMPTS:
                await asyncio.sleep(self.COMMIT_HISTORY_RETRY_INTERVAL)
        log.warning("Cisco commit history verification unconfirmed: node=%s label=%s", self._node_id, label)
        return None

    async def _run_history_query(self, session: ConsoleSession) -> str | None:
        """执行一次提交历史查询。

        查询失败或暂无记录应返回 None，由外层决定是否重试。
        """
        command = "do show configuration commit list"
        try:
            result = await self._run_command(session, command)
        except CiscoCommandError as exc:
            raise CiscoResultUnconfirmed(f"{self._node_id} Cisco commit history query failed: {exc}") from exc
        except CiscoResultUnconfirmed:
            return None
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            log.warning("Cisco commit history query failed: node=%s detail=%s", self._node_id, exc)
            return None
        text = session.analysis_text(result.output, command)
        match = CISCO_COMMIT_ROW_PATTERN.search(text)
        if match is None:
            return None
        return match.group(1)

    async def _run_command(
        self,
        session: ConsoleSession,
        command: str,
        patterns: tuple[re.Pattern[str], ...] = CISCO_CLI_ERROR_PATTERNS,
        prompt_names: tuple[PromptName, ...] = (PromptName.CONFIG,),
        allow_confirm: bool = False,
        expected_generation: int | None = None,
    ) -> ConsoleResult:
        """执行一条命令并检查阶段结果。

        配置命令、EXEC 命令和历史查询共享同一套交互逻辑。
        """
        await session.ensure_ready()
        if expected_generation is not None and expected_generation != session.recovery_generation:
            raise CiscoCandidateLost(f"{self._node_id} Cisco configuration session changed")
        expected_prompts = set(prompt_names)
        if allow_confirm:
            expected_prompts.add(PromptName.CONFIRM)
        result = await session.exchange(command, prompt_names=expected_prompts)
        if allow_confirm and result.prompt_name == PromptName.CONFIRM:
            before_confirmation = result
            result = await session.exchange("yes", prompt_names={PromptName.CONFIG})
            result.output = before_confirmation.output + "\n" + result.output
            result.sent = before_confirmation.sent or result.sent
        self._check_result(session, result, command, patterns)
        return result

    def _check_result(
        self,
        session: ConsoleSession,
        result: ConsoleResult,
        command: str,
        patterns: tuple[re.Pattern[str], ...],
    ) -> None:
        """检查命令交换结果。

        不同阶段只应检查自己的错误，不能混用 load/commit 模式。
        """
        if result.restore_required or result.disconnected:
            raise CiscoResultUnconfirmed(
                f"{self._node_id} Cisco session lost during {command!r}; "
                "result unconfirmed (结果未确认)"
            )
        if not result.prompt_returned:
            raise CiscoResultUnconfirmed(
                f"{self._node_id} Cisco command did not return a prompt: {command!r}\n"
                f"{self._tail(result.output)}"
            )
        text = session.analysis_text(result.output, command)
        error = session.find_explicit_error(text, patterns=patterns)
        if error:
            raise CiscoCommandError(
                f"{self._node_id} Cisco command failed ({command}): {error}\n"
                f"{self._tail(result.output)}"
            )

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
