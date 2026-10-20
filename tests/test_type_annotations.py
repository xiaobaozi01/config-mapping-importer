#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检查生产代码的方法类型注解。

做什么：用 AST 遍历 Juniper、ConsoleSession、Telnet 三个文件，确认每个方法
除 self/cls 外的入参和返回值都有注解。
为什么：类型注解容易在新增方法时遗漏，用测试把要求固化成可重复检查。
"""

import ast
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_FILES = (
    "juniper_vjunos_vmx.py",
    "cisco_xrv9k.py",
    "console_session.py",
    "telnet_utils.py",
)
# 节点、适配器和 FTP 节点直接沿用运行时对象，不额外定义结构类型。
OPTIONAL_ANNOTATIONS = {
    ("cisco_xrv9k.py", "_import_adapter"): {"return"},
    ("juniper_vjunos_vmx.py", "_cleanup"): {"ftp_server"},
    ("cisco_xrv9k.py", "_cleanup"): {"ftp_server"},
    ("cisco_xrv9k.py", "_prepare_ftp"): {"adapter"},
}


def _method_arguments(node):
    """收集方法参数。

    做什么：合并普通参数、仅位置参数、关键字参数、*args 和 **kwargs。
    为什么：类型检查要覆盖所有参数形式。
    """
    args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
    if node.args.vararg is not None:
        args.append(node.args.vararg)
    if node.args.kwarg is not None:
        args.append(node.args.kwarg)
    return args


def _missing_annotations(node):
    """返回方法缺失的注解名称。

    做什么：跳过 self/cls，检查每个参数和返回值注解。
    为什么：测试失败信息需要明确指出缺的是哪个参数。
    """
    missing = []
    for argument in _method_arguments(node):
        if argument.arg in ("self", "cls"):
            continue
        if argument.annotation is None:
            missing.append(argument.arg)
    if node.returns is None:
        missing.append("return")
    return missing


class TypeAnnotationTest(unittest.TestCase):
    def test_all_methods_are_annotated(self):
        """确认生产方法都有入参和返回值类型。

        做什么：逐个文件、逐个方法检查注解完整性。
        为什么：防止新增或修改方法时遗漏类型。
        """
        violations = []
        for filename in SOURCE_FILES:
            path = os.path.join(PROJECT_ROOT, filename)
            with open(path, encoding="utf-8") as source_file:
                tree = ast.parse(source_file.read())

            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                optional = OPTIONAL_ANNOTATIONS.get((filename, node.name), set())
                missing = [name for name in _missing_annotations(node) if name not in optional]
                if missing:
                    violations.append(
                        f"{filename}:{node.lineno} {node.name} missing={missing}"
                    )

        self.assertEqual(violations, [], "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
