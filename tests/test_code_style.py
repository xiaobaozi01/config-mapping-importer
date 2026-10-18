#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检查生产代码的方法长度和圈复杂度。

做什么：静态解析 Juniper 与 Telnet 两个文件，统计每个方法的有效代码行数和
近似圈复杂度。
为什么：把“方法不超过 50 行、复杂度不要过高”固化成可重复执行的检查。
"""

import ast
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_FILES = (
    "juniper_vjunos_vmx.py",
    "cisco_xrv9k.py",
    "telnet_utils.py",
    "console_session.py",
)
MAX_CODE_LINES = 50
MAX_COMPLEXITY = 10


def _count_code_lines(source_lines, node):
    """统计方法体中的有效代码行。

    做什么：从方法体第一行到结束行，跳过空行和纯注释行。
    为什么：需求只约束代码行数，注释和空行不应计入。
    """
    count = 0
    for index in range(node.body[0].lineno - 1, node.end_lineno):
        line = source_lines[index].strip()
        if not line or line.startswith("#"):
            continue
        count += 1
    return count


def _count_complexity(node):
    """估算方法的圈复杂度。

    做什么：统计条件、循环、异常、布尔运算和推导式等分支点。
    为什么：这些结构决定了方法的路径数量，是复杂度控制的主要指标。
    """
    complexity = 1
    for child in ast.walk(node):
        if child is node:
            continue
        if isinstance(
            child,
            (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.IfExp, ast.Match),
        ):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            complexity += len(child.values) - 1
        elif isinstance(child, ast.comprehension):
            complexity += 1
        elif isinstance(child, ast.ExceptHandler):
            complexity += 1
    return complexity


def _iter_functions(tree):
    """遍历模块中的全部同步和异步方法。

    做什么：使用 ast.walk 找到 FunctionDef 和 AsyncFunctionDef。
    为什么：类方法、模块函数都需要遵守同一组约束。
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


class CodeStyleTest(unittest.TestCase):
    def test_source_methods_are_small_and_simple(self):
        """检查方法长度和复杂度。

        做什么：逐个方法比较有效代码行数和近似圈复杂度。
        为什么：避免单个方法过长或分支过多，保持单一职责。
        """
        violations = []
        for filename in SOURCE_FILES:
            path = os.path.join(PROJECT_ROOT, filename)
            with open(path, encoding="utf-8") as source_file:
                source = source_file.read()
            tree = ast.parse(source)
            source_lines = source.splitlines()

            for node in _iter_functions(tree):
                code_lines = _count_code_lines(source_lines, node)
                complexity = _count_complexity(node)
                if code_lines > MAX_CODE_LINES or complexity > MAX_COMPLEXITY:
                    violations.append(
                        f"{filename}:{node.lineno} {node.name} "
                        f"code_lines={code_lines} complexity={complexity}"
                    )

        self.assertEqual(violations, [], "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
