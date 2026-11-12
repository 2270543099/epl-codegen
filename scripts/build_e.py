#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直接生成 .e 扩展模块文件（不需要打开易语言 IDE）。

把 head / body / tail 模板与子程序文本拼成完整 .e，等价于原易语言工程里
`生成子程序数据` + `生成局部变量数据` + `生成扩展模块数据` + `GetCheckValue` 的流程。

用法:
    # 用内置示例（两个子程序）直接产出一个 .e
    python build_e.py --demo --out wnxd_macro.e

    # 用自己的 spec
    python build_e.py --spec spec.json --out wnxd_macro.e --data "C:\\Users\\我的电脑\\Desktop\\数据"

    # 生成后回读校验
    python build_e.py --demo --out demo.e --verify

spec.json:
    {
      "subprograms": [
        {"name": "_启动子程序", "code": ".局部变量 结果, 整数型\\n结果 ＝ 1 ＋ 2\\n返回 (结果)"},
        {"name": "_临时子程序", "code": ".局部变量 文本, 文本型\\n返回 (“x”)", "return_type": 2147483652}
      ]
    }

可选字段:
    name         子程序名（必填）
    code         子程序正文文本，含 `.局部变量` 行与语句（必填）
    return_type  返回类型常量；省略则按 `返回 (...)` 推断，推断不出按整数型
    var_index    从 1 起：把第 n 个局部变量的类型改成 return_type（等价于修正后的 变量序号）
"""

import argparse
import json
import os
import struct
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

# ---------------------------------------------------------------- 类型常量

TYPE_NAMES = {
    "文本型": 2147483652,
    "整数型": 2147484417,
    "长整数型": 2147484673,
    "短整数型": 2147484161,
    "双精度小数型": 2147485185,
    "日期时间型": 2147483651,
    "字节集": 2147483653,
    "字节型": 2147483905,
    "逻辑型": 2147483650,
    "对象": 65584,
    "通用型": 2147483648,
}

TEXT = TYPE_NAMES["文本型"]
INTS = TYPE_NAMES["整数型"]
BYTESET = TYPE_NAMES["字节集"]
DATE = TYPE_NAMES["日期时间型"]
DOUBLE = TYPE_NAMES["双精度小数型"]

LOCAL_PREFIX = ".局部变量"
PARAM_PREFIX = ".参数"

DEFAULT_DATA_DIR = r"C:\Users\我的电脑\Desktop\数据"

# 子程序记录风格：
#   a = skill 模板族（00 00 01 09 版本、属性 48、代码块尾 7 字节、变量标识符 0x2500xxxx）
#   b = 实测 IDE 模块（02 00 01 19 版本、属性 0、代码块尾 6 字节、变量标识符 0x2501xxxx）
STYLES = {
    "a": {
        "version": b"\x00\x00\x01\x09",
        "attr": 48,
        "trailer": b"\x00\x00\x00\x00\x00\x36\x01",
        "var_tag_base": 0x25000000,
    },
    "b": {
        "version": b"\x02\x00\x01\x19",
        "attr": 0,
        "trailer": b"\x00\x00\x00\x00\x36\x01",
        "var_tag_base": 0x25010000,
    },
    # c = a 的版本/尾部不变，只把子程序属性 48 改成 0（实测 IDE 存的模块是 0）
    "c": {
        "version": b"\x00\x00\x01\x09",
        "attr": 0,
        "trailer": b"\x00\x00\x00\x00\x00\x36\x01",
        "var_tag_base": 0x25000000,
    },
    # d = 完全照 IDE 存的模块：版本 02 00 01 19、属性 0、标识符 0x2501xxxx，尾部仍 7 字节
    "d": {
        "version": b"\x02\x00\x01\x19",
        "attr": 0,
        "trailer": b"\x00\x00\x00\x00\x00\x36\x01",
        "var_tag_base": 0x25010000,
    },
}

STYLE = STYLES["a"]

# 生成过程中收集的结构警告（块尾相邻之类），main() 里统一打印
LINT = []

DEMO = {
    "subprograms": [
        {
            "name": "_启动子程序",
            "code": "\n".join(
                [
                    ".局部变量 结果, 整数型",
                    "结果 ＝ 1 ＋ 2",
                    "返回 (结果)",
                ]
            ),
        },
        {
            "name": "_临时子程序",
            "code": "\n".join(
                [
                    ".局部变量 文本, 文本型",
                    "文本 ＝ “你好”",
                    "返回 (文本)",
                ]
            ),
        },
    ]
}


# ---------------------------------------------------------------- 基础工具


def gb(text):
    return text.encode("gbk", "replace")


def u32(value):
    return struct.pack("<I", value & 0xFFFFFFFF)


def u16(value):
    return struct.pack("<H", value & 0xFFFF)


def check_value(data):
    """GetCheckValue：4 字节滚动异或，按小端读回整数。"""
    key = bytearray(4)
    for index, byte in enumerate(data):
        key[index % 4] ^= byte
    return struct.unpack("<I", bytes(key))[0]


class ModuleCounter:
    """模块编号：子程序标识符和变量标识符共用一个递增计数器。"""

    def __init__(self, start=0):
        self.value = start

    def next(self):
        self.value += 1
        return self.value


# ---------------------------------------------------------------- 预处理代码

# 语法块标记：直接产出固定字节，并让 add = 1
MARKERS = {
    "否则": b"\x50",
    "如果结束": b"\x51\x72",
    "如果真结束": b"\x52\x73",
    "默认": b"\x53\x6f",
    "判断结束": b"\x54\x74",
}

# 带头部的块：head, 属性1, 属性2
BLOCKS = {
    "如果": (b"\x6b", 0, 0),
    "如果真": (b"\x6c", 1, 0),
    "判断开始": (b"\x6d\x6e", 2, 0),
    "判断": (b"\x53\x6e", 2, 0),
    "循环判断首": (b"\x70", 5, 0),
    "循环判断尾": (b"\x55\x71", 6, 0),
    "计次循环首": (b"\x70", 7, 0),
    "计次循环尾": (b"\x55\x71", 8, 0),
    "判断循环首": (b"\x70", 3, 0),
    "判断循环尾": (b"\x55\x71", 4, 0),
    "变量循环首": (b"\x70", 9, 0),
    "变量循环尾": (b"\x55\x71", 10, 0),
}


def chunk(head, attr1, attr2, code):
    payload = gb(code)
    return (
        head
        + u32(attr1)
        + u16(attr2)
        + b"\x40\x00"
        + u32(len(payload) + 1)
        + payload
        + STYLE["trailer"]
    )


def preprocess(line):
    """返回 (字节块, add)。add 用于偏移表：语法标记不记偏移但会推进长度。"""
    code = line
    if line.startswith("."):
        paren = line.find("(")
        name = (line[1:] if paren < 0 else line[1:paren]).strip()
        if name in ("版本 2", "局部变量"):
            return b"", 0
        if name in MARKERS:
            return MARKERS[name], 1
        if name == "判断开始":
            code = line.replace("开始", "", 1)
            head, attr1, attr2 = BLOCKS[name]
            return chunk(head, attr1, attr2, code[1:]), 1
        if name in BLOCKS:
            head, attr1, attr2 = BLOCKS[name]
            return chunk(head, attr1, attr2, code[1:]), 0
        return chunk(b"\x6a", 0, 0xFFFF, code[1:]), 0
    return chunk(b"\x6a", 0, 0xFFFF, code), 0


# ---------------------------------------------------------------- 返回类型推断


def split_fields(text):
    return [field.strip() for field in text.split(",")]


def var_return_type(name, locals_):
    for line in locals_:
        prefix = PARAM_PREFIX if line.startswith(PARAM_PREFIX) else LOCAL_PREFIX
        rest = line[len(prefix):].strip()
        fields = split_fields(rest)
        tokens = fields[0].split()
        if not tokens or tokens[-1] != name:
            continue
        if len(fields) == 1:
            return 0
        return TYPE_NAMES.get(fields[1], 0)
    return 0


def code_return_type(statements, locals_):
    """处理代码返回：找 `返回 (...)`，按第一个字节判类型；找不到补 `返回(0)`。

    注意按 GBK 字节定位：`返回` 占 4 字节，`(` 从第 5 字节之后找，和易语言
    `寻找文本 (tmp, “(”, 5, 假)` 的口径一致；按字符找会差 2 位，中文返回语句全部漏判。
    """
    result = 0
    for statement in statements:
        text = statement.strip()
        if not text.startswith("返回") or result:
            continue
        raw = gb(text)
        start = raw.find(b"(", 4)
        if start < 0:
            continue
        end = raw.rfind(b")")
        if end <= start:
            continue
        inner = raw[start + 1:end].decode("gbk", "replace").strip()
        first = gb(inner)[:1]
        if first in (b"\xa1", b"\x22"):
            result = TEXT
        elif first == b"{":
            result = BYTESET
        elif first == b"[":
            result = DATE
        elif first and 48 <= first[0] <= 57:
            result = DOUBLE
        else:
            result = var_return_type(inner, locals_)
    if result == 0:
        statements.append("返回(0)")
        result = INTS
    return result


# ---------------------------------------------------------------- 变量与子程序


def variable_blob(name, vtype, static, dims, note):
    payload = u32(vtype) + u16(static) + bytes([len(dims)])
    payload += b"".join(u32(size) for size in dims)
    payload += gb(name) + b"\x00" + gb(note) + b"\x00"
    return u32(len(payload)) + payload


def parse_decl(line, prefix=LOCAL_PREFIX):
    """解析 `.局部变量 名, 类型, 静态, "维数", 备注` 或 `.参数 名, 类型, 修饰`。"""
    rest = line[len(prefix):].strip()
    fields = split_fields(rest)
    name = fields[0] if fields else ""
    vtype = TYPE_NAMES.get(fields[1], 0) if len(fields) > 1 else 0
    if prefix == PARAM_PREFIX:
        return name, vtype, 0, [], ""
    static = 1 if len(fields) > 2 and fields[2] else 0
    dims = []
    note = ""
    for index, field in enumerate(fields[2:], start=2):
        if '"' in field:
            spec = field.replace('"', "")
            dims = [int(part) for part in spec.split(",") if part.strip().isdigit()]
        elif index >= 3 and not dims:
            note = field
    return name, vtype, static, dims, note


def var_block(locals_, counter, prefix=LOCAL_PREFIX):
    tags = b""
    offsets = b""
    blobs = b""
    position = 0
    count = 0
    for line in locals_:
        name, vtype, static, dims, note = parse_decl(line, prefix)
        if not name:
            continue
        tags += u32(STYLE["var_tag_base"] + counter.next())
        offsets += u32(position)
        blob = variable_blob(name, vtype, static, dims, note)
        blobs += blob
        position += len(blob)
        count += 1
    if count == 0:
        return b"\x00" * 8
    return u32(count) + u32(count * 8 + len(blobs)) + tags + offsets + blobs


def classify_block(line):
    """识别块首/块尾：返回 (role, kind)。kind 1=如果 2=如果真 3=循环 4=判断（判断未实测）。"""
    if not line.startswith("."):
        return None, 0
    paren = line.find("(")
    name = (line[1:] if paren < 0 else line[1:paren]).strip()
    if name == "如果":
        return "open", 1
    if name == "如果真":
        return "open", 2
    if name == "判断开始":
        return "open", 4
    if name in ("判断循环首", "循环判断首", "计次循环首", "变量循环首"):
        return "open", 3
    if name == "如果结束":
        return "close", 1
    if name == "如果真结束":
        return "close", 2
    if name == "判断结束":
        return "close", 4
    if name in ("判断循环尾", "循环判断尾", "计次循环尾", "变量循环尾"):
        return "close", 3
    return None, 0


def subprogram_blob(name, code, counter, var_index=0, return_type=0):
    lines = [line.strip() for line in code.splitlines()]
    lines = [line for line in lines if line]
    params_ = [line for line in lines if line.startswith(PARAM_PREFIX)]
    locals_ = [line for line in lines if line.startswith(LOCAL_PREFIX)]
    statements = [
        line
        for line in lines
        if not line.startswith(PARAM_PREFIX) and not line.startswith(LOCAL_PREFIX)
    ]

    result_type = return_type or code_return_type(statements, locals_ + params_)

    chunks = b""
    offsets = []
    jumps = []          # (kind, 块首偏移, 块尾偏移+1)
    stack = []
    marks = []          # 每条语句的块角色，用来查「块尾相邻」这种让 IDE 认错缩进的写法
    current = 0
    for statement in statements:
        blob, add = preprocess(statement)
        role, kind = classify_block(statement)
        marks.append("tail" if (role == "close" and kind == 3) else (role or "stmt"))
        # 块尾（如果结束/如果真结束/循环尾）的头部是 1 字节前缀 + 真正操作码，记录位置要 +1
        record_at = current + 1 if role == "close" else current + add
        if len(blob) > 9:  # 纯跳转标记不记偏移
            offsets.append(record_at)
        if role == "open":
            stack.append((kind, current))
        elif role == "close":
            start = stack.pop()[1] if stack else current
            jumps.append((kind, start, current + 1))
        chunks += blob
        current += len(blob)

    # 块尾相邻是实测会让易语言 IDE 认错缩进的写法，生成时就报出来
    for i in range(len(marks) - 1):
        if marks[i] == "close" and marks[i + 1] in ("close", "tail"):
            LINT.append(
                "%s: 第 %d 条语句（块尾）紧跟%s，IDE 会把块认错、缩进线穿到文件末尾"
                % (name, i + 1, "块尾" if marks[i + 1] == "close" else "循环尾")
            )

    name_bytes = gb(name)
    # 跳转表按块首偏移升序排（实测 IDE 的模块就是按源码顺序存的）
    jumps.sort(key=lambda item: item[1])
    # 参数先分配标识符（源码里 .参数 在 .局部变量 之前），但文件里局部变量表在前、参数表在后
    params_blob = var_block(params_, counter, PARAM_PREFIX)
    locals_blob = var_block(locals_, counter)

    data = bytearray()
    data += STYLE["version"]
    data += u32(STYLE["attr"])
    data += u32(result_type)
    data += u32(len(name_bytes))
    data += name_bytes
    data += b"\x00\x00\x00\x00"
    data += locals_blob
    data += params_blob
    data += u32(len(offsets) * 4)
    data += b"".join(u32(offset) for offset in offsets)
    data += u32(len(jumps) * 9)
    data += b"".join(bytes([kind]) + u32(start) + u32(end) for kind, start, end in jumps)
    data += b"\x00" * 12
    data += u32(len(chunks))
    data += chunks

    # 变量序号：改第 n 个局部变量的类型字段（拼接后按偏移回写）
    if var_index > 0:
        base = 20 + len(name_bytes)
        count = struct.unpack_from("<I", data, base)[0]
        if 1 <= var_index <= count:
            offset = struct.unpack_from(
                "<I", data, base + 8 + count * 4 + (var_index - 1) * 4
            )[0]
            position = base + 8 + count * 8 + offset + 4
            data[position:position + 4] = u32(result_type)

    return bytes(data)


def build_module(head, body, tail, blobs, counter):
    count = len(blobs)
    ids = b"".join(u32(0x04000000 + counter.next()) for _ in range(count))
    data = bytearray(
        body
        + u32(count * 4)
        + ids
        + b"\x00" * 8
        + u32(count * 8)
        + ids
        + b"\x00" * (count * 4)
        + b"".join(blobs)
        + b"\x00" * 56
    )
    # 三次回填（0 基 56 / 52 / 4），顺序不能换
    data[56:60] = u32(len(data) - 100)
    data[52:56] = u32(check_value(bytes(data[100:])))
    data[4:8] = u32(check_value(bytes(data[8:100])))
    return head + bytes(data) + tail


# ---------------------------------------------------------------- 入口


def load_parts(data_dir):
    parts = {}
    for name in ("head", "body", "tail"):
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            raise SystemExit("缺少模板片段：%s" % path)
        with open(path, "rb") as handle:
            parts[name] = handle.read()
    return parts["head"], parts["body"], parts["tail"]


def build(spec, data_dir):
    head, body, tail = load_parts(data_dir)
    counter = ModuleCounter()
    blobs = []
    for item in spec.get("subprograms", []):
        if "name" not in item or "code" not in item:
            raise SystemExit("subprogram 缺少 name/code：%r" % (item,))
        blobs.append(
            subprogram_blob(
                item["name"],
                item["code"],
                counter,
                var_index=int(item.get("var_index") or 0),
                return_type=int(item.get("return_type") or 0),
            )
        )
    return build_module(head, body, tail, blobs, counter), blobs


def read_u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def verify(path, blob_count, head_len, body_len, tail_len):
    """回读生成结果：模块头字段 + 校验和 + 每个子程序块。"""
    from parse_subprogram import parse_subprogram

    with open(path, "rb") as handle:
        buf = handle.read()

    data = buf[head_len:len(buf) - tail_len]
    count = blob_count
    problems = []

    if len(data) < 100:
        problems.append("data 区不足 100 字节")
    else:
        if read_u32(data, body_len) != count * 4:
            problems.append(
                "模块头子程序数字段 = %d，期望 %d" % (read_u32(data, body_len), count * 4)
            )
        second = body_len + 4 + 4 * count + 8
        if read_u32(data, second) != count * 8:
            problems.append(
                "模块头第二张表长度 = %d，期望 %d" % (read_u32(data, second), count * 8)
            )
        if read_u32(data, 56) != len(data) - 100:
            problems.append("长度字段 = %d，期望 %d" % (read_u32(data, 56), len(data) - 100))
        if read_u32(data, 52) != check_value(data[100:]):
            problems.append("尾部校验和不符")
        if read_u32(data, 4) != check_value(data[8:100]):
            problems.append("头部校验和不符")

    if problems:
        print("  模块头: " + "; ".join(problems))
    else:
        print("  模块头: OK（子程序数、两张表长度、长度字段、两处校验和都对）")

    offset = head_len + body_len + 16 + 12 * count  # 模块头之后第一个子程序块
    ok = not problems
    for index in range(count):
        result = parse_subprogram(buf, offset, verbose=False)
        tail = result["tail"]
        # 风格 b 的版本标记本来就不是 00 00 01 09，不算问题
        real_problems = [
            item
            for item in result["problems"]
            if not (STYLE["version"] != b"\x00\x00\x01\x09" and item.startswith("版本标志"))
        ]
        print(
            "  子程序 %d: %s  类型=%s  参数=%d  变量=%d  代码=%d 字节  问题=%d"
            % (
                index + 1,
                result["name"],
                result["return_type_name"],
                result.get("param_count", 0),
                result["var_count"],
                tail["code_len"] if tail else -1,
                len(real_problems),
            )
        )
        ok = ok and not real_problems and tail is not None
        offset = result["end"]

    expected = len(buf) - tail_len - 56      # 末尾 56 个 0 + #tail
    if offset != expected:
        print(
            "  !! 子程序块结束后偏移 0x%X，期望 0x%X（差 %d 字节）"
            % (offset, expected, expected - offset)
        )
        ok = False
    return ok

def main():
    parser = argparse.ArgumentParser(description="直接生成 .e 扩展模块文件")
    parser.add_argument("--spec", help="spec.json 路径")
    parser.add_argument("--demo", action="store_true", help="使用内置示例")
    parser.add_argument("--out", default="wnxd_macro.e")
    parser.add_argument("--data", default=DEFAULT_DATA_DIR, help="head/body/tail 所在目录")
    parser.add_argument(
        "--style",
        default="a",
        choices=sorted(STYLES),
        help="子程序记录风格：a=skill 模板族（默认）；b=实测 IDE 模块（02 00 01 19 / 6 字节尾）",
    )
    parser.add_argument("--verify", action="store_true", help="生成后回读校验")
    parser.add_argument("--write-spec", help="把所用 spec 另存为文件")
    args = parser.parse_args()

    global STYLE
    STYLE = STYLES[args.style]

    if args.demo or not args.spec:
        spec = DEMO
    else:
        with open(args.spec, "r", encoding="utf-8") as handle:
            spec = json.load(handle)

    blob, blobs = build(spec, args.data)
    with open(args.out, "wb") as handle:
        handle.write(blob)

    out_path = os.path.abspath(args.out)
    print("已生成: %s  (%d 字节)" % (out_path, len(blob)))
    print("  子程序数: %d" % len(blobs))
    if LINT:
        print("  结构警告（易语言 IDE 会把这类块的缩进显示错位，建议改写法）：")
        for item in LINT:
            print("    ! " + item)

    if args.write_spec:
        with open(args.write_spec, "w", encoding="utf-8") as handle:
            json.dump(spec, handle, ensure_ascii=False, indent=2)
        print("  spec 已保存: %s" % os.path.abspath(args.write_spec))

    if args.verify:
        print("回读校验:")
        head_len, body_len, tail_len = (len(part) for part in load_parts(args.data))
        ok = verify(out_path, len(blobs), head_len, body_len, tail_len)
        print("  结果: %s" % ("通过" if ok else "失败"))
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
