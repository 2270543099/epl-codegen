#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析并校验「易语言子程序数据块」（本项目 生成子程序数据 的布局）。

用法:
    python parse_subprogram.py <文件> [--offset N] [--vars] [--header-only]

布局（0 基，单位字节）:

    0   4   版本/标志            00 00 01 09
    4   4   属性                 生成器写 48；实测 .e 里该字段为 0
    8   4   返回类型             类型常量，如 0x80000301 = 整数型
    12  4   名称长度（字节数）
    16  n   名称（GBK 字节）
    ..  4   保留 0
    ..  ..  局部变量块
                [数量 4][总长 4][标识符 4*数量][偏移 4*数量][变量数据 ...]
                总长 = 数量*8 + 变量数据长度
    ..  8   保留 0（生成器占位；完整 .e 的子程序表区此处不同）
    ..  4   偏移表长度（字节数）
    ..  m   偏移表（每项 4 字节，语句代码偏移）
    ..  16  保留 0（生成器占位）
    ..  4   代码长度（字节数）
    ..  k   代码字节

变量数据（每项）:

    [长度 4][类型 4][是否静态 2][数组维数 1][各维尺寸 4*维数]
    [名称][0][备注][0]
    长度 = 该变量 payload 自身长度（不含长度字段）

校验点:
    * 变量块总长 == 数量*8 + 各变量数据长度之和
    * 第 i 个变量的偏移 == 前 i-1 个变量数据长度之和（偏移相对「变量数据」起点）
    * 每个变量声明的长度 == 实际解析出的 payload 长度
    * （完整块）偏移表长度是 4 的倍数，代码长度不越界

退出码: 0 = 头部与变量块自洽（尾部不匹配只作提示）；1 = 发现结构问题。
"""

import argparse
import struct
import sys

# 统一按 UTF-8 输出：路径和变量名里都有中文，管道/重定向时默认 locale 编码会乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

TYPE_NAMES = {
    0x80000000: "通用型",
    0x80000101: "字节型",       # #字节型值   = 2147483905
    0x80000201: "短整数型",     # #短整型值   = 2147484161
    0x80000301: "整数型",       # #整数型值   = 2147484417
    0x80000401: "长整数型",     # #长整型值   = 2147484673
    0x80000601: "双精度小数型",  # #双精度值   = 2147485185
    0x80000002: "逻辑型",       # #逻辑型值   = 2147483650
    0x80000003: "日期时间型",    # #日期型值   = 2147483651
    0x80000004: "文本型",       # #文本型值   = 2147483652
    0x80000005: "字节集",       # #字节集值   = 2147483653
    0x10030: "对象",            # #对象型值   = 65584
}


def u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def u16(buf, off):
    return struct.unpack_from("<H", buf, off)[0]


def type_name(value):
    return TYPE_NAMES.get(value, "未知(0x%08X)" % value)


def parse_var(buf, off):
    """解析一个变量数据项，返回 (dict, 结束偏移)。"""
    declared = u32(buf, off)
    payload = buf[off + 4:off + 4 + declared]
    if len(payload) != declared:
        raise ValueError("变量 @0x%X: 声明长度 %d 超出缓冲区" % (off, declared))
    vtype = u32(payload, 0)
    static = u16(payload, 4)
    dims = payload[6]
    pos = 7
    if pos + 4 * dims > len(payload):
        raise ValueError("变量 @0x%X: 数组维数 %d 超出 payload" % (off, dims))
    dim_sizes = [u32(payload, pos + 4 * i) for i in range(dims)]
    pos += 4 * dims
    name, _, rest = payload[pos:].partition(b"\x00")
    note = rest.split(b"\x00")[0] if rest else b""
    return {
        "start": off,
        "declared_len": declared,
        "type": vtype,
        "type_name": type_name(vtype),
        "static": static,
        "dims": dims,
        "dim_sizes": dim_sizes,
        "name": name.decode("gbk", "replace"),
        "note": note.decode("gbk", "replace"),
    }, off + 4 + declared


def parse_subprogram(buf, base=0, verbose=False, header_only=False):
    problems = []
    if buf[base:base + 4] != b"\x00\x00\x01\x09":
        problems.append("版本标志不是 00 00 01 09，而是 %s" % buf[base:base + 4].hex(" "))
    if base + 16 > len(buf):
        raise ValueError("offset 之后不足 16 字节")

    attr = u32(buf, base + 4)
    ret_type = u32(buf, base + 8)
    name_len = u32(buf, base + 12)
    name = buf[base + 16:base + 16 + name_len].decode("gbk", "replace")
    pos = base + 16 + name_len
    if buf[pos:pos + 4] != b"\x00\x00\x00\x00":
        problems.append("名称后的 4 字节保留位不为 0")
    pos += 4

    count = u32(buf, pos)
    total = u32(buf, pos + 4)
    pos += 8
    tags = [u32(buf, pos + 4 * i) for i in range(count)]
    pos += 4 * count
    var_offsets = [u32(buf, pos + 4 * i) for i in range(count)]
    pos += 4 * count
    data_start = pos

    variables = []
    for index in range(count):
        var, pos = parse_var(buf, pos)
        variables.append(var)
        if var["start"] - data_start != var_offsets[index]:
            problems.append(
                "变量 %d: 偏移表写 %d，实际 %d"
                % (index + 1, var_offsets[index], var["start"] - data_start)
            )
        if var.get("note_len_mismatch"):
            problems.append("变量 %d: 备注长度与声明不符" % (index + 1))

    data_len = pos - data_start
    if total != count * 8 + data_len:
        problems.append("变量块总长 %d != %d*8 + %d" % (total, count, data_len))

    # 参数表：紧跟在局部变量表之后，结构完全一样（空表就是 8 个 0，即老生成器的 8 字节 0）
    params = []
    param_total = 0
    if pos + 8 <= len(buf):
        pcount = u32(buf, pos)
        param_total = u32(buf, pos + 4)
        cursor = pos + 8 + pcount * 8
        try:
            for _ in range(pcount):
                var, cursor = parse_var(buf, cursor)
                params.append(var)
        except (ValueError, struct.error):
            problems.append("参数表解析失败（数量 %d）" % pcount)
            cursor = pos + 8
    else:
        cursor = pos + 8

    result = {
        "base": base,
        "attr": attr,
        "return_type": ret_type,
        "return_type_name": type_name(ret_type),
        "name": name,
        "var_count": count,
        "var_total": total,
        "param_count": len(params),
        "params": params,
        "param_total": param_total,
        "data_start": data_start,
        "tags": ["0x%08X" % tag for tag in tags],
        "variables": variables,
        "var_end": pos,
        "tail": None,
        "tail_note": "",
        "end": pos,
        "size": pos - base,
        "problems": problems,
    }

    if not header_only and cursor + 12 <= len(buf):
        q = cursor  # 参数表之后就是偏移表
        table_len = u32(buf, q)
        if q + 4 + table_len + 20 <= len(buf) and table_len % 4 == 0:
            p = q + 4
            table = [u32(buf, p + 4 * i) for i in range(table_len // 4)]
            p += table_len
            jump_at = p
            jump_len = u32(buf, p)          # 跳转表字节数（每个块 9 字节）
            jumps = []
            for i in range(jump_len // 9):
                off = jump_at + 4 + 9 * i
                jumps.append((buf[off], u32(buf, off + 1), u32(buf, off + 5)))
            p += 4 + jump_len
            p += 12
            code_start = p + 4
            code_len = u32(buf, p)
            p += 4
            code = buf[p:p + code_len]
            if len(code) == code_len:
                p += code_len
                result["tail"] = {
                    "offset_table_len": table_len,
                    "offset_table": table,
                    "jump_len": jump_len,
                    "jumps": jumps,
                    "code_start": code_start,
                    "code_len": code_len,
                    "code": code,
                }
                result["end"] = p
                result["size"] = p - base
            else:
                problems.append("代码长度 %d 超出缓冲区" % code_len)
        else:
            result["tail_note"] = (
                "尾部不符合生成器布局（8 字节 0 + 偏移表长 + 偏移表 + 16 字节 0 + 代码长 + 代码）；"
                "这里很可能是完整 .e 的子程序表区，不是本生成器产出的块"
            )
    elif not header_only:
        result["tail_note"] = "变量块之后数据不足，未解析尾部"

    if verbose:
        print_result(result)
    return result


def print_result(r):
    print("== 子程序 @0x%X  长度 %d 字节 ==" % (r["base"], r["size"]))
    print("  名称      : %r" % r["name"])
    print("  属性      : %d" % r["attr"])
    print("  返回类型  : 0x%08X (%s)" % (r["return_type"], r["return_type_name"]))
    print("  变量数    : %d   变量块总长 %d   参数数 %d" % (r["var_count"], r["var_total"], r.get("param_count", 0)))
    print("  标识符    : %s" % ", ".join(r["tags"]))
    for index, var in enumerate(r["variables"], 1):
        dims = ""
        if var["dims"]:
            dims = " 数组 %d 维 %s" % (var["dims"], var["dim_sizes"])
        print(
            "    %d. %-8r %-10s 静态=%d%s 长度=%d 偏移=%d"
            % (
                index,
                var["name"],
                var["type_name"],
                var["static"],
                dims,
                var["declared_len"],
                var["start"] - r["data_start"],
            )
        )
    for index, var in enumerate(r.get("params", []), 1):
        dims = ""
        if var["dims"]:
            dims = " 数组 %d 维 %s" % (var["dims"], var["dim_sizes"])
        print(
            "    参数 %d. %-8r %-10s 静态=%d%s 长度=%d"
            % (index, var["name"], var["type_name"], var["static"], dims, var["declared_len"])
        )
    if r["tail"]:
        tail = r["tail"]
        print("  偏移表    : %d 字节 -> %s" % (tail["offset_table_len"], tail["offset_table"]))
        print("  代码长度  : %d 字节" % tail["code_len"])
        print("  结束于    : 0x%X" % r["end"])
    if r["tail_note"]:
        print("  尾部      : %s" % r["tail_note"])
    if r["problems"]:
        print("  !! 问题:")
        for problem in r["problems"]:
            print("     - %s" % problem)
    else:
        print("  头部/变量块: OK")
    print()


def main():
    parser = argparse.ArgumentParser(description="解析/校验易语言子程序数据块")
    parser.add_argument("path")
    parser.add_argument("--offset", type=lambda text: int(text, 0), default=0)
    parser.add_argument("--vars", action="store_true", help="只输出变量表")
    parser.add_argument("--header-only", action="store_true", help="只解析头部与变量块")
    args = parser.parse_args()

    with open(args.path, "rb") as handle:
        buf = handle.read()
    if args.offset >= len(buf):
        print("offset 超出文件长度", file=sys.stderr)
        return 2

    result = parse_subprogram(
        buf, args.offset, verbose=not args.vars, header_only=args.header_only
    )
    if args.vars:
        for index, var in enumerate(result["variables"], 1):
            print("%d\t%s\t%s" % (index, var["name"], var["type_name"]))
    return 0 if not result["problems"] else 1


if __name__ == "__main__":
    sys.exit(main())
