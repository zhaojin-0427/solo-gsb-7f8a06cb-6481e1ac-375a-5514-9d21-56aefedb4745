"""容器内调用 GCC 编译桥接 .so，并对产物做静态核查。

绝不加载/执行任何上传的 ELF；只用 GCC 编译服务生成的 C 源码，并用
pyelftools 静态检查产物。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from .errors import BridgeError


def compile_bridge(workdir: Path, source_name: str = "bridge.c",
                   script_name: str = "bridge.map",
                   output_name: str = "bridge.so",
                   soname: str = "",
                   gcc: str = os.environ.get("GCC", "gcc")) -> Path:
    src = workdir / source_name
    script = workdir / script_name
    out = workdir / output_name
    cmd = [
        gcc,
        "-std=gnu11",
        "-O2",
        "-fPIC",
        "-fno-builtin",
        "-fstack-protector-strong",
        "-Wall",
        "-Wextra",
        "-Wcast-function-type",
        "-shared",
        "-o", str(out),
        str(src),
        f"-Wl,--version-script={script}",
        f"-Wl,-soname,{soname}",
        "-ldl",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=workdir, capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        raise BridgeError(f"容器内未找到 C 编译器（{gcc}）")
    except subprocess.TimeoutExpired:
        raise BridgeError("GCC 编译超时")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "未知错误").strip()
        raise BridgeError(f"桥接编译失败（GCC 返回 {proc.returncode}）：{detail[:2000]}")
    if not out.exists() or out.stat().st_size == 0:
        raise BridgeError("GCC 未产出桥接文件（拒绝半成品）")
    return out


def verify_artifact(path: Path, expected_soname: str, entry_names: list[str]) -> None:
    """静态核查：ELF64 x86-64、SONAME、入口为已定义导出、无自引用 DT_NEEDED。"""

    try:
        with path.open("rb") as fh:
            elf = ELFFile(fh)
            if elf.elfclass != 64 or elf.little_endian is False:
                raise BridgeError("桥接产物 ELF 类别错误")
            if elf.get_machine_arch() != "x64":
                raise BridgeError("桥接产物架构不是 x86-64")
            if elf["e_type"] != "ET_DYN":
                raise BridgeError("桥接产物不是共享对象")

            soname: str | None = None
            needed: list[str] = []
            for seg in elf.iter_segments():
                if seg["p_type"] == "PT_DYNAMIC":
                    for tag in seg.iter_tags():
                        if tag.entry.d_tag == "DT_SONAME":
                            soname = tag.soname
                        elif tag.entry.d_tag == "DT_NEEDED":
                            needed.append(tag.needed)
                    break
            if soname != expected_soname:
                raise BridgeError(
                    f"桥接产物 SONAME {soname!r} 与旧库 {expected_soname!r} 不一致"
                )
            if expected_soname in needed:
                raise BridgeError("桥接产物把旧库 SONAME 列为 DT_NEEDED（存在回到自身的风险）")

            dyn = elf.get_section_by_name(".dynsym")
            if dyn is None or not isinstance(dyn, SymbolTableSection):
                raise BridgeError("桥接产物缺少动态符号表")
            defined: dict[str, int] = {}
            for i in range(1, dyn.num_symbols()):
                s = dyn.get_symbol(i)
                if s.name in entry_names and s["st_shndx"] != "SHN_UNDEF":
                    defined[s.name] = i
            missing = [n for n in entry_names if n not in defined]
            if missing:
                raise BridgeError(f"桥接产物缺少入口导出：{missing}")
    except BridgeError:
        raise
    except Exception as exc:  # pragma: no cover
        raise BridgeError(f"桥接产物静态核查失败：{exc}")
