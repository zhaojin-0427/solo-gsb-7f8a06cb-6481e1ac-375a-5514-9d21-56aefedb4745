"""额外的 ELF 解析边界测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.elfparse import parse_elf
from app.errors import BridgeError


def _so(tmp_path: Path, src: str, name: str = "x", map_text: str | None = None) -> bytes:
    c = tmp_path / f"{name}.c"
    c.write_text(src)
    out = tmp_path / f"{name}.so"
    cmd = ["gcc", "-g", "-O0", "-fPIC", "-shared", "-Wl,-soname,libx.so.1", str(c), "-o", str(out)]
    if map_text:
        m = tmp_path / f"{name}.map"
        m.write_text(map_text)
        cmd.insert(-2, f"-Wl,--version-script={m}")
    subprocess.run(cmd, check=True, capture_output=True)
    return out.read_bytes()


def test_weak_undefined_not_strong(tmp_path):
    _so(tmp_path, "int fn(void){return 1;}\n")
    c = tmp_path / "w.c"
    c.write_text("__attribute__((weak)) extern int fn(void);\n"
                 "int main(void){ return fn ? fn() : 0; }\n")
    out = tmp_path / "w"
    subprocess.run(["gcc", "-g", str(c), "-o", str(out)], check=True)
    cli = parse_elf("cli", out.read_bytes(), "client")
    assert "fn" not in cli.needed_versions
    assert cli.dyn_undefined["fn"][0].binding == "STB_WEAK"


def test_big_endian_rejection_unavailable_arch(tmp_path):
    # 无交叉编译器时，手工篡改 ELF e_machine 字节来触发“架构不符”
    data = bytearray(_so(tmp_path, "int fn(void){return 1;}\n"))
    data[18] = 0xB7  # EM_AARCH64 = 183 (小端数据 0xB7,0x00)
    with pytest.raises(BridgeError, match="x86-64"):
        parse_elf("old", bytes(data), "old")


def test_elfclass_tamper(tmp_path):
    data = bytearray(_so(tmp_path, "int fn(void){return 1;}\n"))
    data[4] = ord("1")  # EI_CLASS = ELFCLASS32（但内容是 64 位 -> 非法）
    with pytest.raises(BridgeError):
        parse_elf("old", bytes(data), "old")


def test_executable_as_library_rejected(tmp_path):
    c = tmp_path / "p.c"
    c.write_text("int main(void){return 0;}\n")
    out = tmp_path / "p"
    subprocess.run(["gcc", "-g", "-no-pie", str(c), "-o", str(out)], check=True)
    with pytest.raises(BridgeError, match="共享对象"):
        parse_elf("old", out.read_bytes(), "old")


def test_garbage_rejected():
    with pytest.raises(BridgeError, match="合法 ELF"):
        parse_elf("old", b"\x7fELFgarbage garbage", "old")
    with pytest.raises(BridgeError, match="合法 ELF"):
        parse_elf("old", b"totally not elf", "old")
