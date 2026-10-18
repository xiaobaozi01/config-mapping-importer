#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2024. All rights reserved.
"""把自适应后的配置导入 GNS3 中的 Cisco IOS XRv9k 路由器。

做什么：完成 FTP 临时节点、接口/IP 准备、配置加载、commit replace 和提交记录核实。
为什么：console 交互已经下沉到通用 ConsoleSession，本模块只保留 Cisco 业务和 profile。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Sequence
from typing import Any, Protocol

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

# 做什么：保存全局仿真平台和配置，供 FTP 清理和凭据读取使用。
# 为什么：父类没有暴露删除节点入口，现有 Cisco 逻辑也使用全局实例。
emulation_instance: EmulationPlatform = EMULATION_CLASS.instance()
app_settings = AppSettings.load_from_yaml()


class AdapterInfo(Protocol):
    """节点适配器的最小结构。

    做什么：描述 FTP 使用的 adapter_number 和 port_name。
    为什么：避免在业务方法中使用 Any，同时兼容节点工厂返回对象。
    """

    adapter_number: int
    port_name: str


class NodeInfo(Protocol):
    """节点信息的最小结构。

    做什么：只暴露 custom_adapters。
    为什么：Cisco 导入只依赖适配器布局，不绑定具体节点模型。
    """

    custom_adapters: Sequence[AdapterInfo]


class FtpServerInfo(Protocol):
    """临时 FTP 节点的最小结构。

    做什么：只暴露 node_id。
    为什么：清理逻辑只需要节点 ID。
    """

    node_id: str


CISCO_PROMPTS: tuple[PromptRule, ...] = (
    PromptRule(
        PromptName.CONFIG,
        re.compile(r"(?m)^RP/\S+\(config[^)]*\)#[ \t]*$"),
    ),
    PromptRule(PromptName.EXEC, re.compile(r"(?m)^RP/\S+#[ \t]*$")),
    PromptRule(
        PromptName.ROOT_USERNAME,
        re.compile(r"(?im)^Enter root-system username:[ \t]*$"),
    ),
    PromptRule(
        PromptName.SECRET,
        re.compile(r"(?im)^Enter secret(?: again)?:[ \t]*$"),
    ),
    PromptRule(PromptName.USERNAME, re.compile(r"(?im)^Username:[ \t]*$")),
    PromptRule(PromptName.PASSWORD, re.compile(r"(?im)^Password:[ \t]*$")),
    # 做什么：只匹配已有 XR 回显中确认过的 commit 警告提示。
    # 为什么：不要用通用 [yes/no] 猜测未知交互，避免把普通输出当确认。
    PromptRule(
        PromptName.CONFIRM,
        re.compile(r"(?im)^Do you wish to proceed.*\?[ \t]*$"),
    ),
)

# 做什么：固定命令自身异常的最小兜底。
# 为什么：命令都是确定的，正常情况下不应出现；保留用于防御。
CISCO_CLI_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)^% (?:Invalid input|Incomplete command|Ambiguous command)"),
    re.compile(r"(?im)^% Authorization failed"),
)

# 做什么：load 阶段可能出现的文件、资源和语法错误。
# 为什么：语法错误以 show configuration failed load 为准，文件错误发生在它之前。
CISCO_LOAD_ERROR_PATTERNS = CISCO_CLI_ERROR_PATTERNS + (
    re.compile(r"(?im)^.*!! CONFIGURATION FAILED"),
    re.compile(r"(?im)^.*config: failed to open input file"),
    re.compile(r"(?im)^.*Resource temporarily unavailable"),
    re.compile(r"(?im)^.*Syntax/Authorization errors"),
)

# 做什么：commit/apply 阶段的错误。
# 为什么：% Failed to commit 是官方 commit 失败提示，conflicts 属于 apply 阶段。
CISCO_COMMIT_ERROR_PATTERNS = CISCO_CLI_ERROR_PATTERNS + (
    re.compile(r"(?im)^% Failed to commit one or more configuration items"),
    re.compile(
        r"(?im)^.*Failed to commit one or more configuration items "
        r"during a pseudo-atomic operation"
    ),
    re.compile(r"(?im)^.*conflicts with other IPv4 addresses"),
)

# 做什么：匹配 XR syslog 的 %FACILITY-SEVERITY-MNEMONIC 结构。
# 为什么：实际日志可能带时间戳和进程前缀，不能锚定行首的 %。
CISCO_BACKGROUND_PATTERN = re.compile(
    r"(?i)%[A-Z0-9_]+(?:-[A-Z0-9_]+)*-\d+-[A-Z0-9_]+"
)
# 做什么：解析官方 show configuration commit list 的 SNo. Label/ID 首行。
# 为什么：commit label 会替代自动 ID 显示在最新一行。
CISCO_COMMIT_ROW_PATTERN = re.compile(r"(?m)^\s*1\s+(\S+)\s+")


def build_cisco_console_profile(username: str, password: str) -> ConsoleProfile:
    """构建 Cisco IOS XR console profile。

    做什么：组装 XRv9k 登录、模式切换、提示符和错误规则。
    为什么：ConsoleSession 保持厂商无关，Cisco 差异只放在 profile 中。
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
            ModeStep(PromptName.EXEC, PromptName.CONFIG, "configure"),
        ),
        command_prompt=PromptName.CONFIG,
        line_ending=b"\r\n",
        error_patterns=CISCO_CLI_ERROR_PATTERNS,
        background_patterns=(CISCO_BACKGROUND_PATTERN,),
    )


class CiscoError(RuntimeError):
    """Cisco 导入流程的异常基类。

    做什么：统一标识本模块主动抛出的业务异常。
    为什么：调用方可以只捕获 CiscoError。
    """


class CiscoCommandError(CiscoError):
    """设备明确拒绝了命令。

    做什么：表示已经回到提示符，但输出中存在明确错误。
    为什么：这类失败不能继续按成功处理。
    """


class CiscoCommitVerificationError(CiscoError):
    """提交记录与本次 label 不一致。

    做什么：表示提交历史可读，但最新记录不是本次提交。
    为什么：提示符不能替代提交历史核实。
    """


class CiscoResultUnconfirmed(CiscoError):
    """命令已经发出，但结果无法确认。

    做什么：在会话无法恢复或历史无法读取时报告未确认。
    为什么：盲目重发可能造成重复提交。
    """


class CiscoIOSXRV9kd7d11d1ConfigManager(NeConfigManager):
    """Cisco IOS XRv9k 配置导入管理器。

    做什么：负责 FTP 准备、配置加载、commit replace 和资源清理。
    为什么：console 状态机由 ConsoleSession 承担，本类只保留 Cisco 业务。
    """

    DEFAULT_USER = "root"
    DEFAULT_PASSWD = app_settings.second_cer
    FTP_USER = app_settings.ftp_user
    FTP_PWD = app_settings.ftp_pwd

    # 做什么：限制一次完整导入/初始化的总时长。
    # 为什么：单条命令成功即可，但失联时不能无限挂起。
    OPERATION_TIMEOUT = 1800
    MAX_RECOVERIES = 3

    # 做什么：统一 ConsoleSession 的轮询与稳定窗口节奏。
    # 为什么：测试和现场可能需要调整等待节奏。
    READ_INTERVAL = 0.2
    PROMPT_SETTLE = 0.5
    DRAIN_QUIET = 0.3
    DRAIN_MAX = 2.0
    BOOT_READY_SETTLE = 1.0
    BOOT_NUDGE_INTERVAL = 3.0

    def __init__(
        self,
        emulation: Any,
        project_id: str,
        node_id: str,
        host_located: str,
        node_compute_id: str | None = None,
    ) -> None:
        """保存节点信息。

        做什么：调用父类构造并加载 Cisco 节点布局。
        为什么：后续 FTP 适配器选择依赖 node_info。
        """
        super().__init__(emulation, project_id, node_id, host_located, node_compute_id)
        self.node_info: NodeInfo = NodeCreateInfoFactory().get_create_info("cisco xrv9k")

    async def init_telnet_client(self) -> TelnetClient:
        """获取 Cisco console 客户端。

        做什么：通过父类取得 TelnetClient。
        为什么：不调用 test_telnet_connect，避免它消费登录提示符。
        """
        telnet_client: TelnetClient | None = await self.get_telnet_client()
        if telnet_client is None:
            raise ConnectionError(f"{self._node_id} Telnet console is not available")
        return telnet_client

    def _console_session(self, telnet_client: TelnetClient) -> ConsoleSession:
        """创建 Cisco console 会话。

        做什么：组装 Cisco profile，并复制管理器的等待节奏。
        为什么：业务层只依赖 ConsoleSession 接口。
        """
        profile = build_cisco_console_profile(self.DEFAULT_USER, self.DEFAULT_PASSWD)
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

        做什么：返回 deadline 与当前事件循环时间的差值。
        为什么：所有命令共享同一个总预算。
        """
        return max(0.0, deadline - asyncio.get_running_loop().time())

    def _import_adapter(self) -> AdapterInfo:
        """取得 FTP 使用的适配器。

        做什么：返回第一个 custom adapter。
        为什么：保持当前 Cisco 导入使用第一个数据口的业务行为。
        """
        adapters = getattr(self.node_info, "custom_adapters", None) or []
        if not adapters:
            raise CiscoError(f"{self._node_id} needs at least one custom adapter")
        return adapters[0]

    async def import_cfg(self, local_cfg_path: str) -> None:
        """把本地配置导入 XRv9k。

        做什么：串起登录、FTP 准备、load、commit replace 和核实。
        为什么：FTP 创建和上传是 load 的前提；清理放在 finally 中。
        """
        telnet_client: TelnetClient | None = None
        ftp_server: FtpServerInfo | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.OPERATION_TIMEOUT
        try:
            telnet_client = await asyncio.wait_for(
                self.init_telnet_client(), timeout=self._remaining(deadline)
            )
            session = self._console_session(telnet_client)
            await session.enter_command_prompt("", timeout=self._remaining(deadline))
            await self._disable_paging(session, deadline)

            ftp_server = await self.init_ftp_server()
            await self.copy_file_to_ftp_server(ftp_server, local_cfg_path)
            adapter = self._import_adapter()
            await self.link_ftp_server(ftp_server, adapter.adapter_number)

            await self._prepare_ftp(session, adapter, deadline)
            await self._load_config(session, deadline)
            label = await self._commit_replace(session, deadline)
            log.info("Cisco XRv9k configuration imported: node=%s label=%s", self._node_id, label)
        finally:
            await self._cleanup(ftp_server, telnet_client)

    async def init_device(self, telnet_client: TelnetClient, sys_name: str) -> None:
        """执行设备初始化。

        做什么：登录后设置 hostname 并提交。
        为什么：这是原有 Cisco 初始化业务，必须确认成功。
        """
        if not sys_name:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.OPERATION_TIMEOUT
        session = self._console_session(telnet_client)
        await session.enter_command_prompt("", timeout=self._remaining(deadline))
        await self._run_config_command(
            session, f"hostname {sys_name}", deadline
        )
        await self._run_config_command(
            session,
            "commit",
            deadline,
            patterns=CISCO_COMMIT_ERROR_PATTERNS,
        )

    async def export_cfg(self, local_cfg_path: str) -> None:
        """保留导出占位行为。

        做什么：当前 Cisco 导出业务不扩展。
        为什么：保持公开接口不变。
        """
        pass

    async def _prepare_ftp(
        self,
        session: ConsoleSession,
        adapter: AdapterInfo,
        deadline: float,
    ) -> None:
        """准备 FTP 源接口和地址。

        做什么：设置 ftp source-interface、接口 IPv4 地址并提交。
        为什么：load 需要路由器能访问 FTP 服务器。
        """
        await self._run_config_command(
            session, f"ftp client source-interface {adapter.port_name}", deadline
        )
        await self._run_config_command(
            session, f"interface {adapter.port_name}", deadline
        )
        await self._run_config_command(
            session, f"ipv4 address {self.FTP_CLIENT_IP} 255.255.255.0", deadline
        )
        await self._run_config_command(session, "no shutdown", deadline)
        await self._run_config_command(
            session,
            "commit",
            deadline,
            patterns=CISCO_COMMIT_ERROR_PATTERNS,
        )
        # 做什么：结束配置会话并重新进入全局配置模式。
        # 为什么：load 只能在全局配置模式执行，不能停在 config-if 子模式。
        await self._run_exec_command(session, "end", deadline)
        await self._run_config_command(session, "configure", deadline)

    async def _disable_paging(self, session: ConsoleSession, deadline: float) -> None:
        """尽力关闭终端分页。

        做什么：在配置模式执行 do terminal length 0；失败只记录告警。
        为什么：提交历史可能分页，分页提示会阻止 prompt 识别。
        """
        try:
            result = await session.exchange(
                "do terminal length 0",
                timeout=self._remaining(deadline),
                prompt_names={PromptName.CONFIG},
            )
            if not result.prompt_returned:
                log.warning("Cisco terminal length command did not return a prompt")
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            log.warning("Cisco terminal length command failed: %s", exc)

    async def _load_config(self, session: ConsoleSession, deadline: float) -> None:
        """加载 FTP 上的配置文件。

        做什么：发送 load，再执行 show configuration failed load。
        为什么：load 的语法错误以 Cisco 失败视图为准，文件错误则在 load 回显中。
        """
        command = (
            f"load ftp://{self.FTP_USER}:{self.FTP_PWD}@"
            f"{self.FTP_SERVER_IP}/{self._node_id}.cfg"
        )
        await self._run_config_command(
            session,
            command,
            deadline,
            patterns=CISCO_LOAD_ERROR_PATTERNS,
        )
        await self._run_config_command(
            session,
            "show configuration failed load",
            deadline,
            patterns=CISCO_LOAD_ERROR_PATTERNS,
        )

    async def _commit_replace(self, session: ConsoleSession, deadline: float) -> str:
        """用带 label 的 commit replace 提交配置并核实。

        做什么：生成 UUID label，执行 commit replace，查询提交历史。
        为什么：label 是 Cisco 提交历史的稳定标识，可替代脆弱的 hostname 判断。
        """
        label = f"cfg-import-{uuid.uuid4()}"
        command = f"commit label {label} replace"
        await self._run_config_command(
            session,
            command,
            deadline,
            patterns=CISCO_COMMIT_ERROR_PATTERNS,
        )

        verified = await self._verify_commit_label(session, label, deadline)
        if verified is True:
            log.info("Cisco XRv9k commit verified: node=%s label=%s", self._node_id, label)
            return label
        if verified is False:
            raise CiscoCommitVerificationError(
                f"{self._node_id} latest Cisco commit label does not match {label!r}"
            )
        raise CiscoResultUnconfirmed(
            f"{self._node_id} Cisco commit result could not be verified; "
            "result unconfirmed (结果未确认)"
        )

    async def _verify_commit_label(
        self,
        session: ConsoleSession,
        label: str,
        deadline: float,
    ) -> bool | None:
        """核实最新提交记录。

        做什么：执行 show configuration commit list，比较第一行 label。
        为什么：提示符和 commit 回显不能替代提交历史。
        """
        command = "do show configuration commit list"
        try:
            result = await self._run_config_command(session, command, deadline)
        except (CiscoError, ConnectionError, TimeoutError, RuntimeError) as exc:
            log.warning(
                "Cisco commit history query failed: node=%s detail=%s",
                self._node_id,
                exc,
            )
            return None

        text = session.analysis_text(result.output, command)
        match = CISCO_COMMIT_ROW_PATTERN.search(text)
        if match is None:
            return None
        return match.group(1) == label

    async def _run_exec_command(
        self,
        session: ConsoleSession,
        command: str,
        deadline: float,
    ) -> ConsoleResult:
        """执行一条 EXEC 模式命令。

        做什么：确保会话就绪、发送命令并检查 exec 提示符。
        为什么：end 等命令必须回到 exec，而不是 config。
        """
        await session.ensure_ready()
        result = await session.exchange(
            command,
            timeout=self._remaining(deadline),
            prompt_names={PromptName.EXEC},
        )
        self._check_result(
            session, result, command, CISCO_CLI_ERROR_PATTERNS
        )
        return result

    async def _run_config_command(
        self,
        session: ConsoleSession,
        command: str,
        deadline: float,
        patterns: tuple[re.Pattern[str], ...] = CISCO_CLI_ERROR_PATTERNS,
    ) -> ConsoleResult:
        """执行一条配置模式命令。

        做什么：确保会话就绪、处理确认提示、检查阶段错误和提示符。
        为什么：所有配置命令共享同一套校验逻辑，但错误模式按阶段传入。
        """
        await session.ensure_ready()
        result = await self._exchange_with_confirm(session, command, deadline)
        self._check_result(session, result, command, patterns)
        return result

    async def _exchange_with_confirm(
        self,
        session: ConsoleSession,
        command: str,
        deadline: float,
    ) -> ConsoleResult:
        """发送命令并处理一次确认提示。

        做什么：如果返回 confirm 提示，发送 yes 后再等 config。
        为什么：commit/commit replace 可能要求二次确认。
        """
        result = await session.exchange(
            command,
            timeout=self._remaining(deadline),
            prompt_names={PromptName.CONFIG, PromptName.CONFIRM},
        )
        if result.prompt_name == PromptName.CONFIRM:
            result = await session.exchange(
                "yes",
                timeout=self._remaining(deadline),
                prompt_names={PromptName.CONFIG},
            )
        return result

    def _check_result(
        self,
        session: ConsoleSession,
        result: ConsoleResult,
        command: str,
        patterns: tuple[re.Pattern[str], ...],
    ) -> None:
        """检查命令交换结果。

        做什么：拒绝需要恢复、未回提示符或存在阶段明确错误的命令。
        为什么：不同阶段只应检查自己的错误，不能混用 load/commit 模式。
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

    async def _cleanup(
        self,
        ftp_server: FtpServerInfo | None,
        telnet_client: TelnetClient | None,
    ) -> None:
        """分别尝试清理临时资源。

        做什么：删除 FTP 节点、关闭 Telnet、处理自环。
        为什么：清理异常不能覆盖原始导入失败。
        """
        await self._cleanup_ftp_server(ftp_server)
        self._cleanup_telnet(telnet_client)
        await self._cleanup_loopback()

    async def _cleanup_ftp_server(self, ftp_server: FtpServerInfo | None) -> None:
        """删除临时 FTP 节点。

        做什么：有节点时调用仿真平台删除。
        为什么：临时节点不删除会占用项目资源。
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

        做什么：调用 close 并记录异常。
        为什么：关闭失败不能影响导入结果。
        """
        if telnet_client is None:
            return
        try:
            telnet_client.close()
        except Exception:  # noqa: BLE001 - 清理失败不能覆盖原始异常
            log.exception("Failed to close Telnet client: node=%s", self._node_id)

    async def _cleanup_loopback(self) -> None:
        """处理最后一个适配器自环。

        做什么：复用父类自环能力并捕获异常。
        为什么：自环清理失败不能覆盖原始异常。
        """
        try:
            await self.loopback_last_adapter_for_uni()
        except Exception:  # noqa: BLE001 - 清理失败不能覆盖原始异常
            log.exception("Failed to configure the last-adapter loopback: node=%s", self._node_id)

    @staticmethod
    def _tail(text: str, length: int = 4000) -> str:
        """截取文本尾部用于日志和异常。

        做什么：返回最后 length 个字符。
        为什么：完整回显可能很长，异常信息只需要最近上下文。
        """
        if not text:
            return ""
        return text[-length:]
