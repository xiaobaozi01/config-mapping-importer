#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2024. All rights reserved.
"""把自适应后的配置导入 GNS3 中的 Juniper vJunos 路由器。

做什么：完成 FTP 临时节点创建、准备命令、配置加载、提交以及提交历史核实。
为什么：console 交互已经下沉到通用 ConsoleSession，本模块只保留 Juniper
导入业务和厂商 profile。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from src.application.config.manager.ne_config_manager import NeConfigManager
from src.common.config import AppSettings
from src.common.constant import EMULATION_CLASS
from src.emulation.emulation_platform import EmulationPlatform
from src.schemas.emulation.nodes import NodeCreateInfoFactory

try:
    # 做什么：优先使用项目正式目录中的通用 console 会话。
    # 为什么：生产代码位于 src 包内，不能依赖仓库根目录的临时布局。
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
    # 做什么：导入 TelnetClient 用于公开接口和清理参数的类型标注。
    # 为什么：生产代码位于 src 包内，独立测试时需要回退到仓库根目录。
    from src.utils.telnet_utils import TelnetClient
except ImportError:  # pragma: no cover - 独立测试时的回退路径
    from telnet_utils import TelnetClient

log = logging.getLogger(__name__)

# 做什么：保存全局仿真平台实例，供清理 FTP 节点时使用。
# 为什么：父类没有暴露删除节点的统一入口，旧的清理逻辑也使用该实例。
emulation_instance: EmulationPlatform = EMULATION_CLASS.instance()
app_settings = AppSettings.load_from_yaml()


class LoadStatus(str, Enum):
    """load 输出分类。

    做什么：统一加载成功、失败、缺标记和需恢复四种状态。
    为什么：字符串状态容易拼错，且不利于类型检查。
    """

    FAILED = "failed"
    COMPLETE = "complete"
    NO_COMPLETE = "no_complete"
    RECOVER = "recover"

    def __str__(self) -> str:
        """返回枚举值。

        做什么：让日志显示字符串值而不是枚举名。
        为什么：保持现有日志和比较输出兼容。
        """
        return self.value


class CommitDecision(str, Enum):
    """一次 commit 尝试后的决策。

    做什么：统一重试、恢复、失败、完成、丢失和歧义。
    为什么：主循环只按枚举分支，不比较散落字符串。
    """

    RETRY = "retry"
    RECOVER = "recover"
    RAISE = "raise"
    COMPLETE = "complete"
    LOST = "lost"
    AMBIGUOUS = "ambiguous"

    def __str__(self) -> str:
        """返回枚举值。

        做什么：让日志显示字符串值而不是枚举名。
        为什么：保持现有日志和比较输出兼容。
        """
        return self.value


class HistoryDecision(str, Enum):
    """提交历史查询后的决策。

    做什么：统一匹配、不匹配、等待、重试、停止和未确认。
    为什么：历史核实循环需要明确的状态机。
    """

    MATCH = "match"
    MISMATCH = "mismatch"
    WAIT = "wait"
    RETRY = "retry"
    STOP = "stop"
    UNCONFIRMED = "unconfirmed"

    def __str__(self) -> str:
        """返回枚举值。

        做什么：让日志显示字符串值而不是枚举名。
        为什么：保持现有日志和比较输出兼容。
        """
        return self.value


class HistoryMatch(str, Enum):
    """提交历史记录匹配结果。

    做什么：统一匹配、不匹配和继续等待。
    为什么：记录判断和查询决策职责不同，需要独立枚举。
    """

    MATCH = "match"
    MISMATCH = "mismatch"
    WAIT = "wait"

    def __str__(self) -> str:
        """返回枚举值。

        做什么：让日志显示字符串值而不是枚举名。
        为什么：保持现有日志和比较输出兼容。
        """
        return self.value


class AdapterInfo(Protocol):
    """节点适配器的最小结构。

    做什么：描述导入流程使用的 adapter_number 和 port_name。
    为什么：避免在业务方法中使用 Any，同时兼容节点工厂返回的对象。
    """

    adapter_number: int
    port_name: str


class NodeInfo(Protocol):
    """节点信息的最小结构。

    做什么：只暴露 custom_adapters 列表。
    为什么：Juniper 导入只依赖适配器布局，不需要绑定具体节点模型。
    """

    custom_adapters: Sequence[AdapterInfo]


class FtpServerInfo(Protocol):
    """临时 FTP 节点的最小结构。

    做什么：只暴露 node_id。
    为什么：清理逻辑只需要节点 ID，避免依赖仿真平台的具体类型。
    """

    node_id: str


# 做什么：集中定义 Juniper 的提示符和文本识别规则。
# 为什么：ConsoleSession 保持厂商无关，Juniper 差异只放在 profile 中。
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
    ModeStep(PromptName.SHELL, PromptName.EXEC, "cli"),
    ModeStep(PromptName.EXEC, PromptName.CONFIG, "configure"),
)


def build_junos_console_profile(username: str, password: str) -> ConsoleProfile:
    """构建 Juniper console profile。

    做什么：把 Juniper 的提示符、登录动作、模式切换和文本规则组装成 profile。
    为什么：通用 ConsoleSession 不硬编码 Juniper 差异，便于未来 Cisco 复用。
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
        boot_prompts=(PromptName.LOGIN,),
        error_patterns=(JUNOS_ERROR_PATTERN,),
        background_patterns=(JUNOS_BACKGROUND_PATTERN,),
        noise_patterns=(JUNOS_NOISE_PATTERN,),
    )


class JunosError(RuntimeError):
    """Juniper 导入流程的异常基类。

    做什么：统一标识本模块主动抛出的业务异常。
    为什么：调用方可以只捕获 JunosError，而不必依赖具体实现细节。
    """


class JunosCommandError(JunosError):
    """设备明确拒绝了命令。

    做什么：表示已经拿到提示符，但输出中存在明确的语法或执行错误。
    为什么：这类失败不能继续按成功处理，也不能被准备阶段的告警逻辑忽略。
    """


class JunosCommitVerificationError(JunosError):
    """提交历史与本次 UUID 不一致。

    做什么：表示提交历史可以读取，但最新已完成记录不是本次提交。
    为什么：提示符或 commit complete 都不能替代历史核实。
    """


class JunosResultUnconfirmed(JunosError):
    """命令已经发出，但结果无法确认。

    做什么：在会话无法恢复或历史无法读取时报告“结果未确认”。
    为什么：此时盲目重发可能造成重复提交，必须让调用方明确感知风险。
    """


@dataclass
class CommitRecord:
    """show system commit 中的一条提交记录。

    做什么：保存序号、comment、pending 标记和原始行。
    为什么：只有最新“已完成”记录的 comment 才能作为成功依据。
    """

    index: int
    comment: str
    pending: bool
    raw: str


@dataclass
class CommitAnalysis:
    """单次 commit 回显的分类结果。

    做什么：把回显拆成完成、锁拒绝、失败和明确错误四类信号。
    为什么：不同信号对应重试、失败或继续核实等不同动作。
    """

    complete: bool
    locked: bool
    failure: bool
    error: str | None


@dataclass
class CommitAttempt:
    """一次 commit 尝试的分类结果。

    做什么：保存决策类型、命令交互结果和提交分析。
    为什么：把“重试、恢复、提交失败、继续核实”等分支从主循环中分离。
    """

    decision: CommitDecision
    result: ConsoleResult
    analysis: CommitAnalysis | None = None
    error: str | None = None


@dataclass
class HistoryQuery:
    """一次提交历史查询的原始结果。

    做什么：保存命令交互结果和分析文本。
    为什么：查询恢复与历史记录判断需要分开处理。
    """

    result: ConsoleResult
    text: str


@dataclass
class HistoryState:
    """提交历史核实期间的状态。

    做什么：保存恢复次数、comment 不一致次数和最后一次错误。
    为什么：恢复次数和不一致次数都需要跨查询累计。
    """

    recoveries: int = 0
    mismatches: int = 0
    last_error: str | None = None


# 做什么：检查并补齐 system root-authentication。
# 为什么：新启动设备可能没有该必填项，准备阶段的 commit 会直接失败。
ROOT_AUTH_PROBE = "show system root-authentication"
BOOTSTRAP_ROOT_HASH = (
    "$6$admin123$qkQUo1OlslMMLHPyx8kLQnBJKIZaPQsFkb7XxVevTdwU3L40gq0yjvrD2XbU1uKkZN38GonwTiTZnZ1euaZYB."
)
ROOT_AUTH_SET_COMMAND = (
    f'set system root-authentication encrypted-password "{BOOTSTRAP_ROOT_HASH}"'
)


class JuniperVjunosRouter23d2R1d15dConfigManager(NeConfigManager):
    """Juniper vJunos 配置导入管理器。

    做什么：负责 FTP 准备、配置准备、加载、提交核实和资源清理。
    为什么：console 交互已经由通用 ConsoleSession 承担，本类只保留业务编排。
    """

    DEFAULT_USER = "root"
    DEFAULT_PASSWD = "admin123"

    # 做什么：限制一次完整导入/初始化的总时长。
    # 为什么：单条命令成功即可，不关心耗时；但失联时不能无限挂起。
    OPERATION_TIMEOUT = 1800
    MAX_RECOVERIES = 3

    # 做什么：统一异步轮询节奏，并传给 ConsoleSession。
    # 为什么：测试和现场可能需要调整等待节奏。
    READ_INTERVAL = 0.2
    PROMPT_SETTLE = 0.5
    DRAIN_QUIET = 0.3
    DRAIN_MAX = 2.0
    BOOT_READY_SETTLE = 1.0
    BOOT_NUDGE_INTERVAL = 3.0

    # 做什么：限制数据库锁冲突的短重试次数。
    # 为什么：锁通常由其他用户或残留 exclusive 会话持有，长时间重试没有收益。
    COMMIT_LOCK_RETRY_ATTEMPTS = 3
    COMMIT_LOCK_RETRY_INTERVAL = 5
    COMMIT_HISTORY_RETRY_INTERVAL = 2.0
    COMMIT_HISTORY_MISMATCH_LIMIT = 2

    # 做什么：识别加载和提交的业务成功标记与错误。
    # 为什么：这些是 Juniper 导入业务规则，不属于通用 console 层。
    LOAD_COMPLETE_PATTERN = re.compile(
        r"(?im)^[ \t]*load complete(?:[ \t]*\((?P<errors>\d+)[ \t]+errors?\))?[ \t]*$"
    )
    COMMIT_COMPLETE_PATTERN = re.compile(r"(?im)^[ \t]*commit complete[ \t]*$")
    COMMIT_FAILURE_PATTERN = re.compile(
        r"(?im)^[ \t]*configuration check-out failed\b"
    )
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

        做什么：调用父类保存仿真平台和节点信息，并缓存适配器布局。
        为什么：后续 FTP 连线、端口数和临时 IP 都依赖同一份节点信息。
        """
        super().__init__(emulation, project_id, node_id, host_located, node_compute_id)
        self.node_info: NodeInfo = NodeCreateInfoFactory().get_create_info("juniper vjunos")

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    async def init_telnet_client(self) -> TelnetClient:
        """获取控制台客户端。

        做什么：通过父类取得或创建 Telnet 客户端并返回。
        为什么：不在这里调用 test_telnet_connect，因为它在启动阶段可能反复
        重连并消费第一条登录提示符；启动探测由 ConsoleSession 完成。
        """
        telnet_client: TelnetClient | None = await self.get_telnet_client()
        if telnet_client is None:
            raise ConnectionError(f"{self._node_id} Telnet console is not available")
        return telnet_client

    def _console_session(self, telnet_client: TelnetClient) -> ConsoleSession:
        """创建 Juniper console 会话。

        做什么：组装 Juniper profile，并把管理器的等待节奏传给会话。
        为什么：业务层只依赖 ConsoleSession 接口，同时保留测试可调的超时参数。
        """
        profile = build_junos_console_profile(self.DEFAULT_USER, self.DEFAULT_PASSWD)
        session = ConsoleSession(telnet_client, profile)
        session.MAX_RECOVERIES = self.MAX_RECOVERIES
        session.READ_INTERVAL = self.READ_INTERVAL
        session.PROMPT_SETTLE = self.PROMPT_SETTLE
        session.DRAIN_QUIET = self.DRAIN_QUIET
        session.DRAIN_MAX = self.DRAIN_MAX
        session.BOOT_READY_SETTLE = self.BOOT_READY_SETTLE
        session.BOOT_NUDGE_INTERVAL = self.BOOT_NUDGE_INTERVAL
        return session

    @staticmethod
    def _remaining(deadline: float) -> float:
        """计算总预算剩余时间。

        做什么：返回 deadline 与当前事件循环时间的差值，最小为 0。
        为什么：所有命令共享同一个总预算，不再使用独立命令超时。
        """
        return max(0.0, deadline - asyncio.get_running_loop().time())

    async def _enter_config(
        self,
        session: ConsoleSession,
        deadline: float,
    ) -> None:
        """完成启动、登录和 root-authentication 检查。

        做什么：等待启动、进入 config，并确保 root-authentication 存在。
        为什么：import_cfg 和 init_device 的入口流程完全相同。
        """
        boot_output = await session.wait_for_boot(
            timeout=self._remaining(deadline)
        )
        await session.enter_command_prompt(
            boot_output, timeout=self._remaining(deadline)
        )
        await self._ensure_root_authentication(session, deadline)

    async def import_cfg(self, local_cfg_path: str) -> None:
        """把本地配置导入路由器。

        做什么：串起启动登录、FTP 准备、配置准备、load 和最终提交。
        为什么：FTP 创建、上传和连线是后续 load 的前提，任何一步失败都必须
        向上抛出；清理则放在 finally 中分别尝试。
        """
        telnet_client = None
        ftp_server = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.OPERATION_TIMEOUT
        try:
            telnet_client = await asyncio.wait_for(
                self.init_telnet_client(), timeout=self._remaining(deadline)
            )
            session = self._console_session(telnet_client)
            await self._enter_config(session, deadline)

            # 做什么：直接调用父类的 FTP 能力。
            # 为什么：FTP 节点、上传和连线属于通用能力，Juniper 侧不应重复实现。
            ftp_server = await self.init_ftp_server()
            await self.copy_file_to_ftp_server(ftp_server, local_cfg_path)
            import_adapter = self._import_adapter()
            await self.link_ftp_server(ftp_server, import_adapter.adapter_number)

            await self._prepare_config(session, deadline)
            await self._load_config(session, deadline)
            comment = await self._commit_and_verify(
                session, "final", timeout=self._remaining(deadline)
            )
            log.info("Junos configuration imported: node=%s commit=%s", self._node_id, comment)
        finally:
            await self._cleanup(ftp_server, telnet_client)

    async def init_device(self, telnet_client: TelnetClient, sys_name: str | None) -> None:
        """执行设备初始化。

        做什么：关闭自动升级、设置主机名和 40 个端口。
        为什么：这些是原有初始化业务，必须在正式导入前完成并提交。
        """
        if sys_name is None:
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.OPERATION_TIMEOUT
        session = self._console_session(telnet_client)
        await self._enter_config(session, deadline)

        init_commands = (
            "top",
            "delete chassis auto-image-upgrade",
            f"set system host-name {sys_name}",
            "set chassis fpc 0 pic 0 number-of-ports 40",
        )
        await self._run_config_commands(
            session,
            init_commands,
            deadline,
        )
        await self._commit_and_verify(
            session, "init-device", timeout=self._remaining(deadline)
        )

    async def export_cfg(self, local_cfg_path: str) -> None:
        """创建空的导出文件。

        做什么：按原接口生成一个空文件。
        为什么：本次需求没有扩展导出业务，保留占位行为即可。
        """
        with open(local_cfg_path, "w", encoding="utf-8") as file_writer:
            file_writer.write("")

    # ------------------------------------------------------------------
    # 配置准备
    # ------------------------------------------------------------------
    def _import_adapter(self) -> AdapterInfo:
        """取得用于连接 FTP 的网口。

        做什么：返回倒数第二个适配器。
        为什么：最后一个适配器用于自环，倒数第二个才是导入用的数据口。
        """
        adapters = getattr(self.node_info, "custom_adapters", None) or []
        if len(adapters) < 2:
            raise JunosError(
                f"{self._node_id} needs at least two custom adapters for the FTP import"
            )
        return adapters[-2]

    async def _ensure_root_authentication(
        self,
        session: ConsoleSession,
        deadline: float,
    ) -> None:
        """确保 system root-authentication 存在。

        做什么：查询认证配置，缺失时设置兜底密码并提交。
        为什么：新启动设备可能没有该必填项，后续准备提交会失败。
        """
        await session.ensure_ready()
        result = await session.exchange(
            ROOT_AUTH_PROBE,
            timeout=self._remaining(deadline),
        )
        if result.restore_required or not result.prompt_returned:
            raise JunosResultUnconfirmed(
                f"{self._node_id} root-authentication probe did not return a prompt; "
                "result unconfirmed (结果未确认)"
            )
        text = session.analysis_text(result.output, ROOT_AUTH_PROBE)
        error = session.find_explicit_error(text)
        if error:
            raise JunosCommandError(
                f"{self._node_id} root-authentication probe failed: {error}"
            )
        if "root-authentication" in text:
            return
        await session.ensure_ready()
        result = await session.exchange(
            ROOT_AUTH_SET_COMMAND,
            timeout=self._remaining(deadline),
        )
        self._handle_command_result(session, result, ROOT_AUTH_SET_COMMAND)
        await self._commit_and_verify(
            session,
            "bootstrap-root-auth",
            timeout=self._remaining(deadline),
        )

    async def _prepare_config(self, session: ConsoleSession, deadline: float) -> None:
        """执行导入前的准备命令。

        做什么：逐条发送准备命令，最后统一提交一次。
        为什么：准备命令作用于同一个 candidate，没必要每条都产生 commit。
        """
        port_count = len(self.node_info.custom_adapters) - 1
        interface = self._import_adapter().port_name
        prepare_commands = (
            "top",
            "delete chassis auto-image-upgrade",
            f"set chassis fpc 0 pic 0 number-of-ports {port_count}",
            f"set interfaces {interface} unit 0 family inet address "
            f"{self.FTP_CLIENT_IP}/24",
        )
        await self._run_config_commands(
            session,
            prepare_commands,
            deadline,
        )
        await self._commit_and_verify(
            session,
            "prepare-config",
            timeout=self._remaining(deadline),
        )

    async def _run_config_commands(
        self,
        session: ConsoleSession,
        commands: tuple[str, ...],
        deadline: float,
    ) -> None:
        """执行一组必须成功的配置命令。

        做什么：逐条发送命令，任一失败立即抛异常。
        为什么：准备和初始化共享相同的发送与结果检查流程。
        """
        for command in commands:
            await session.ensure_ready()
            result = await session.exchange(
                command, timeout=self._remaining(deadline)
            )
            self._handle_command_result(session, result, command)

    def _handle_command_result(
        self,
        session: ConsoleSession,
        result: ConsoleResult,
        command: str,
    ) -> None:
        """检查必须成功的命令结果。

        做什么：遇到会话丢失、明确错误或未回 prompt 时抛出异常。
        为什么：准备和初始化命令都是固定命令，失败应立即中止。
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
    async def _load_config(self, session: ConsoleSession, deadline: float) -> None:
        """加载配置文件并确认加载结果。

        做什么：发送 load override，必要时恢复会话后重试。
        为什么：只有明确的 load complete 才能进入提交阶段，提示符本身不算成功。
        """
        command = (
            f"load override ftp://ftpuser:ftpuser@{self.FTP_SERVER_IP}/{self._node_id}.cfg"
        )
        recoveries = 0

        for _ in range(self.MAX_RECOVERIES + 1):
            if self._remaining(deadline) <= 0:
                raise JunosResultUnconfirmed(
                    f"{self._node_id} operation timeout before load; "
                    "result unconfirmed (结果未确认)"
                )
            await session.ensure_ready()
            result = await session.exchange(command, timeout=self._remaining(deadline))
            text = session.analysis_text(result.output, command)
            explicit_error = session.find_explicit_error(text)
            status, detail = self._classify_load(result, text, explicit_error)

            if status is LoadStatus.FAILED:
                raise JunosCommandError(f"{self._node_id} Junos load failed: {detail}")
            if status is LoadStatus.COMPLETE:
                # 做什么：加载已确认时只恢复终端，不重新执行 load。
                # 为什么：已经核实成功的阶段不应因会话刷新而重复执行。
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
            # 做什么：加载尚未确认时先恢复会话，再重新 load。
            # 为什么：提交尚未发送，重新 load 不会产生重复提交风险。
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

        做什么：区分加载成功、明确失败、只返回提示符和需要恢复四种情况。
        为什么：load complete 与非零错误、提示符之间不能互相替代。
        """
        complete, error_count = self._load_result(text)

        if complete and error_count is not None and error_count > 0:
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

    def _load_result(self, text: str) -> tuple[bool, int | None]:
        """提取 load complete 和错误数量。

        做什么：收集所有 load complete 标记并取最大错误数。
        为什么：同一缓冲区里只要出现过非零错误，就不能当作成功。
        """
        counts = []
        for match in self.LOAD_COMPLETE_PATTERN.finditer(text):
            count_text = match.group("errors")
            counts.append(0 if count_text is None else int(count_text))
        if not counts:
            return False, None
        return True, max(counts)

    # ------------------------------------------------------------------
    # 提交与历史核实
    # ------------------------------------------------------------------
    async def _commit_and_verify(
        self,
        session: ConsoleSession,
        stage: str,
        timeout: float,
    ) -> str:
        """提交配置并核实最新提交记录。

        做什么：生成独立 UUID，循环执行 commit 尝试，并处理重试、恢复和核实。
        为什么：只有最新已完成提交记录的 comment 与 UUID 完全一致，才能证明
        本次配置真正提交成功。
        """
        comment = f"cfg-import-{uuid.uuid4()}"
        command = f'commit comment "{comment}"'
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        recoveries = 0
        lock_retries = 0
        log.info("Junos commit start: node=%s stage=%s comment=%s", self._node_id, stage, comment)

        while loop.time() < deadline:
            attempt = await self._commit_attempt(
                session, command, stage, deadline, lock_retries
            )
            if attempt.decision is CommitDecision.RETRY:
                lock_retries += 1
                continue
            if attempt.decision is CommitDecision.RECOVER:
                recoveries += 1
                if recoveries > self.MAX_RECOVERIES:
                    raise JunosResultUnconfirmed(
                        f"{self._node_id} Junos commit command could not be sent; "
                        "result unconfirmed (结果未确认)"
                    )
                await self._restore_or_unconfirmed(
                    session, attempt.result.output, f"commit {comment}"
                )
                continue
            if attempt.decision is CommitDecision.RAISE:
                raise JunosCommandError(attempt.error or "commit failed")

            if attempt.decision is CommitDecision.COMPLETE:
                return await self._finish_commit_complete(
                    session, attempt.result, attempt.analysis, comment, stage, deadline
                )
            if attempt.decision is CommitDecision.LOST:
                return await self._finish_commit_lost(
                    session, attempt.result, comment, stage, deadline
                )
            return await self._finish_commit_ambiguous(session, comment, stage, deadline)

        raise JunosResultUnconfirmed(
            f"{self._node_id} operation timeout during commit ({stage}); "
            "result unconfirmed (结果未确认)"
        )

    async def _commit_attempt(
        self,
        session: ConsoleSession,
        command: str,
        stage: str,
        deadline: float,
        lock_retries: int,
    ) -> CommitAttempt:
        """执行一次 commit 尝试并分类结果。

        做什么：发送 commit comment，识别锁拒绝、完成、失败和结果丢失。
        为什么：主循环只负责按分类结果决策，避免把大量分支堆在一个方法里。
        """
        await session.ensure_ready()
        result = await session.exchange(
            command,
            timeout=self._remaining(deadline),
            success_pattern=self.COMMIT_COMPLETE_PATTERN,
        )
        text = session.analysis_text(result.output, command)
        analysis = CommitAnalysis(
            complete=bool(self.COMMIT_COMPLETE_PATTERN.search(text)),
            locked=bool(self.COMMIT_LOCK_PATTERN.search(text)),
            failure=bool(self.COMMIT_FAILURE_PATTERN.search(text)),
            error=session.find_explicit_error(text, ignore_pattern=self.COMMIT_LOCK_PATTERN),
        )

        if not result.sent:
            return CommitAttempt(CommitDecision.RECOVER, result)
        if await self._retry_locked_commit(
            result, analysis, deadline, lock_retries
        ):
            return CommitAttempt(CommitDecision.RETRY, result, analysis)
        if analysis.complete:
            return CommitAttempt(CommitDecision.COMPLETE, result, analysis)
        detail = self._commit_failure_detail(result, analysis)
        if detail:
            return CommitAttempt(
                CommitDecision.RAISE,
                result,
                analysis,
                f"{self._node_id} Junos commit failed ({stage}): {detail}\n"
                f"{self._tail(result.output)}",
            )
        if result.restore_required or not result.prompt_returned:
            return CommitAttempt(CommitDecision.LOST, result, analysis)
        return CommitAttempt(CommitDecision.AMBIGUOUS, result, analysis)

    def _commit_failure_detail(
        self,
        result: ConsoleResult,
        analysis: CommitAnalysis,
    ) -> str | None:
        """提取 commit 的明确失败原因。

        做什么：区分重试耗尽的锁冲突、明确失败和普通错误。
        为什么：把失败分类从主流程中移出，避免主方法分支过多。
        """
        if analysis.locked and result.prompt_returned:
            return "configuration database locked; retry limit reached"
        if analysis.failure:
            return analysis.error or "commit failed"
        return analysis.error

    async def _retry_locked_commit(
        self,
        result: ConsoleResult,
        analysis: CommitAnalysis,
        deadline: float,
        lock_retries: int,
    ) -> bool:
        """处理被数据库锁拒绝的提交。

        做什么：确认是明确的锁拒绝且提示符已返回后，等待固定间隔返回 True。
        为什么：只有明确未提交的锁拒绝才可以沿用同一 UUID 安全重试。
        """
        if analysis.complete or not analysis.locked or not result.prompt_returned:
            return False
        if lock_retries >= self.COMMIT_LOCK_RETRY_ATTEMPTS:
            return False
        remaining = self._remaining(deadline)
        if remaining <= 0:
            return False
        log.warning(
            "Junos configuration database is locked; retrying commit in %ss: node=%s",
            self.COMMIT_LOCK_RETRY_INTERVAL,
            self._node_id,
        )
        await asyncio.sleep(min(self.COMMIT_LOCK_RETRY_INTERVAL, remaining))
        return True

    async def _finish_commit_complete(
        self,
        session: ConsoleSession,
        result: ConsoleResult,
        analysis: CommitAnalysis | None,
        comment: str,
        stage: str,
        deadline: float,
    ) -> str:
        """处理 commit complete 的结果。

        做什么：记录同时出现的错误文本，恢复会话并查询提交历史。
        为什么：commit complete 只是成功标记，最新提交记录才最终依据。
        """
        if analysis is None:
            raise JunosResultUnconfirmed(
                f"{self._node_id} Junos commit analysis missing; "
                "result unconfirmed (结果未确认)"
            )
        if analysis.failure or analysis.error:
            log.warning(
                "Junos commit complete with extra error text; verifying history: "
                "node=%s stage=%s detail=%s",
                self._node_id,
                stage,
                analysis.error or "configuration check-out failed",
            )

        if not result.prompt_returned or result.restore_required:
            await self._restore_or_unconfirmed(session, result.output, f"commit {comment}")

        verified = await self._verify_commit_history(session, comment, deadline)
        if verified is True:
            log.info(
                "Junos commit verified: node=%s stage=%s comment=%s",
                self._node_id,
                stage,
                comment,
            )
            return comment
        if verified is False:
            raise JunosCommitVerificationError(
                f"{self._node_id} latest completed commit comment does not match "
                f"{comment!r} ({stage})"
            )
        raise JunosResultUnconfirmed(
            f"{self._node_id} Junos commit result could not be verified ({stage}); "
            "result unconfirmed (结果未确认)"
        )

    async def _finish_commit_lost(
        self,
        session: ConsoleSession,
        result: ConsoleResult,
        comment: str,
        stage: str,
        deadline: float,
    ) -> str:
        """处理提交已发送但结果丢失的情况。

        做什么：恢复会话后先查询提交历史，成功则返回，否则报告未确认。
        为什么：结果不明时直接重发可能造成重复提交，必须先核实历史。
        """
        await self._restore_or_unconfirmed(session, result.output, f"commit {comment}")
        verified = await self._verify_commit_history(session, comment, deadline)
        if verified is True:
            log.info(
                "Junos commit recovered from history: node=%s stage=%s comment=%s",
                self._node_id,
                stage,
                comment,
            )
            return comment
        if verified is False:
            raise JunosResultUnconfirmed(
                f"{self._node_id} commit was sent but is not the latest completed "
                f"commit ({stage}); result unconfirmed (结果未确认)"
            )
        raise JunosResultUnconfirmed(
            f"{self._node_id} Junos commit result could not be verified ({stage}); "
            "result unconfirmed (结果未确认)"
        )

    async def _finish_commit_ambiguous(
        self,
        session: ConsoleSession,
        comment: str,
        stage: str,
        deadline: float,
    ) -> str:
        """处理既无成功标记也无明确错误的提交。

        做什么：只查询提交历史，根据历史结果决定成功或未确认。
        为什么：提示符已经返回但提交结果不明，重发不安全。
        """
        verified = await self._verify_commit_history(session, comment, deadline)
        if verified is True:
            return comment
        if verified is False:
            raise JunosResultUnconfirmed(
                f"{self._node_id} Junos commit was not confirmed by the commit history "
                f"({stage}); result unconfirmed (结果未确认)"
            )
        raise JunosResultUnconfirmed(
            f"{self._node_id} Junos commit result could not be verified ({stage}); "
            "result unconfirmed (结果未确认)"
        )

    async def _verify_commit_history(
        self,
        session: ConsoleSession,
        comment: str,
        deadline: float,
    ) -> bool | None:
        """核实最新已完成提交记录。

        做什么：循环查询 show system commit，解析并比较最新已完成记录的 comment。
        为什么：命令回显、旧记录和 pending 记录都不能证明本次提交成功。
        """
        loop = asyncio.get_running_loop()
        state = HistoryState()

        while loop.time() < deadline:
            query = await self._run_history_query(session, deadline)
            decision = await self._evaluate_history_query(session, query, state, comment)
            if decision is HistoryDecision.MATCH:
                return True
            if decision is HistoryDecision.MISMATCH:
                return False
            if decision in (HistoryDecision.STOP, HistoryDecision.UNCONFIRMED):
                break
            await asyncio.sleep(
                min(self.COMMIT_HISTORY_RETRY_INTERVAL, max(0.0, deadline - loop.time()))
            )

        log.warning(
            "Junos commit history verification unconfirmed: node=%s comment=%s detail=%s",
            self._node_id,
            comment,
            state.last_error,
        )
        return None

    async def _run_history_query(
        self,
        session: ConsoleSession,
        deadline: float,
    ) -> HistoryQuery:
        """执行一次提交历史查询。

        做什么：准备会话、发送 run show system commit 并分析输出。
        为什么：把查询本身的异常收敛成可恢复的结果，主循环只负责决策。
        """
        query = "run show system commit | no-more"
        try:
            await session.ensure_ready()
            result = await session.exchange(
                query,
                timeout=self._remaining(deadline),
            )
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            return HistoryQuery(
                result=ConsoleResult(output=str(exc), restore_required=True),
                text="",
            )
        text = session.analysis_text(result.output, query)
        return HistoryQuery(result=result, text=text)

    async def _evaluate_history_query(
        self,
        session: ConsoleSession,
        query: HistoryQuery,
        state: HistoryState,
        comment: str,
    ) -> HistoryDecision:
        """评估一次历史查询结果。

        做什么：处理会话恢复、无记录时的查询错误、pending、匹配和不匹配。
        为什么：历史 comment 是用户文本，不能把其中的 error 当成查询失败。
        """
        if query.result.restore_required or not query.result.prompt_returned:
            if not await self._recover_history_query(
                session, state, query.result.output
            ):
                return HistoryDecision.STOP
            return HistoryDecision.RETRY

        records = self._parse_commit_history(query.text)
        if not records:
            error = session.find_explicit_error(query.text)
            if error:
                log.warning(
                    "Junos commit history query failed: node=%s detail=%s",
                    self._node_id,
                    error,
                )
                return HistoryDecision.UNCONFIRMED
            return HistoryDecision.WAIT
        decision, detail = self._history_decision(records, comment)
        if decision is HistoryMatch.MATCH:
            return HistoryDecision.MATCH
        if decision is HistoryMatch.MISMATCH:
            state.mismatches += 1
            log.warning(
                "Latest completed Junos commit comment mismatch: node=%s expected=%r actual=%r",
                self._node_id,
                comment,
                detail,
            )
            if state.mismatches >= self.COMMIT_HISTORY_MISMATCH_LIMIT:
                return HistoryDecision.MISMATCH
        return HistoryDecision.WAIT

    async def _recover_history_query(
        self,
        session: ConsoleSession,
        state: HistoryState,
        initial_output: str,
    ) -> bool:
        """恢复历史查询前的会话。

        做什么：限制恢复次数后执行登录并回到命令提示符。
        为什么：历史查询必须在配置模式下通过 run 命令执行。
        """
        state.recoveries += 1
        if state.recoveries > self.MAX_RECOVERIES:
            state.last_error = "session recovery limit reached"
            return False
        try:
            await session.restore(initial_output)
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            state.last_error = str(exc)
            return False
        return True

    def _history_decision(
        self,
        records: list[CommitRecord],
        comment: str,
    ) -> tuple[HistoryMatch, str | None]:
        """根据记录决定历史核实结果。

        做什么：找出最新已完成记录，并检查本次提交是否仍处于 pending。
        为什么：pending 记录尚未完成，即使旧记录可见也不能提前下结论。
        """
        latest_completed = next((record for record in records if not record.pending), None)
        own_pending = any(record.pending and record.comment == comment for record in records)

        if own_pending:
            log.info("Own Junos commit is still pending; waiting: node=%s", self._node_id)
            return HistoryMatch.WAIT, None
        if latest_completed is None:
            log.info("No completed Junos commit record is available yet; waiting: node=%s", self._node_id)
            return HistoryMatch.WAIT, None
        if latest_completed.comment == comment:
            return HistoryMatch.MATCH, None
        return HistoryMatch.MISMATCH, latest_completed.comment

    def _parse_commit_history(self, text: str) -> list[CommitRecord]:
        """解析 show system commit 输出。

        做什么：逐行提取提交记录和 comment。
        为什么：提交历史需要结构化比较，不能只在整个输出中搜索 UUID。
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

        做什么：匹配序号、时间、用户和方式，并解析行内 comment。
        为什么：记录行是提交历史的结构边界，后续行都属于当前记录。
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

        做什么：跳过噪声行，并提取第一条 comment 和 pending 标记。
        为什么：show system commit 的 comment 通常位于记录下一行，需要归属到正确记录。
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

        做什么：排除空行、提示符、分页符、层级行、后台日志和错误行。
        为什么：这些行不能作为 comment，否则会污染最新提交记录的比较。
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
    async def _restore_or_unconfirmed(
        self,
        session: ConsoleSession,
        initial_output: str,
        context: str,
    ) -> None:
        """恢复会话，失败时报告结果未确认。

        做什么：调用 ConsoleSession.restore 并把恢复失败转换为业务异常。
        为什么：命令已经发出时，恢复失败意味着结果无法核实，不能静默重发。
        """
        try:
            await session.restore(initial_output)
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            raise JunosResultUnconfirmed(
                f"{self._node_id} Junos {context} could not restore the console; "
                f"result unconfirmed (结果未确认): {exc}"
            ) from exc

    async def _cleanup(
        self,
        ftp_server: FtpServerInfo | None,
        telnet_client: TelnetClient | None,
    ) -> None:
        """分别尝试清理临时资源。

        做什么：依次删除 FTP 节点、关闭 Telnet、处理自环。
        为什么：清理异常不能覆盖原始导入失败，因此每一步都独立捕获。
        """
        await self._cleanup_ftp_server(ftp_server)
        self._cleanup_telnet(telnet_client)
        await self._cleanup_loopback()

    async def _cleanup_ftp_server(self, ftp_server: FtpServerInfo | None) -> None:
        """删除临时 FTP 节点。

        做什么：有 FTP 节点时调用仿真平台删除。
        为什么：临时节点不删除会占用项目资源，但删除失败不应影响原始异常。
        """
        if ftp_server is None:
            return
        try:
            emulation = getattr(self, "_emulation", None) or emulation_instance
            await emulation.delete_node(self._project_id, ftp_server.node_id)
        except Exception:  # noqa: BLE001 - 清理失败不能覆盖原始异常
            log.exception(
                "Failed to delete temporary FTP node: node=%s ftp_node=%s",
                self._node_id,
                getattr(ftp_server, "node_id", None),
            )

    def _cleanup_telnet(self, telnet_client: TelnetClient | None) -> None:
        """关闭 Telnet 客户端。

        做什么：调用 close 并把异常记录为日志。
        为什么：连接关闭失败不能影响已经得到的导入结果或原始异常。
        """
        if telnet_client is None:
            return
        try:
            telnet_client.close()
        except Exception:  # noqa: BLE001 - 清理失败不能覆盖原始异常
            log.exception("Failed to close Telnet client: node=%s", self._node_id)

    async def _cleanup_loopback(self) -> None:
        """处理最后一个网口的自环。

        做什么：复用父类的自环能力并捕获异常。
        为什么：自环是通用资源处理逻辑，清理失败不应覆盖原始异常。
        """
        try:
            await self.loopback_last_adapter_for_uni()
        except Exception:  # noqa: BLE001 - 清理失败不能覆盖原始异常
            log.exception("Failed to configure the last-adapter loopback: node=%s", self._node_id)

    # ------------------------------------------------------------------
    # 文本辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _tail(text: str, length: int = 4000) -> str:
        """截取文本尾部用于日志和异常。

        做什么：返回最后 length 个字符。
        为什么：完整回显可能很长，异常信息只需要最近的上下文。
        """
        if not text:
            return ""
        return text[-length:]
