"""规划器/DWARF 拒绝路径测试：动态编译签名不同的旧/候选库。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app.elfparse import parse_elf
from app.planner import plan_bridge
from app.errors import BridgeError


def _build(tmp_path: Path, name: str, src: str, version: str, soname: str = "libt.so.1") -> bytes:
    c = tmp_path / f"{name}.c"
    m = tmp_path / f"{name}.map"
    out = tmp_path / f"{name}.so"
    c.write_text(textwrap.dedent(src))
    m.write_text(f"{version} {{ global: fn; local: *; }};\n")
    import subprocess
    subprocess.run(
        ["gcc", "-g", "-O0", "-fPIC", "-shared", f"-Wl,-soname,{soname}",
         f"-Wl,--version-script={m}", str(c), "-o", str(out)],
        check=True, capture_output=True,
    )
    return out.read_bytes()


def _build_client(tmp_path: Path, version: str, old_so: Path) -> bytes:
    # 链接旧库以产生对 fn@version 的强未定义引用（与真实“按旧版链接”一致）
    asm = tmp_path / "c.s"
    asm.write_text(f'.symver fn,fn@{version}\n.section .note.GNU-stack,"",@progbits\n')
    c = tmp_path / "c.c"
    c.write_text("extern int fn(void); int main(void){ return fn(); }\n")
    out = tmp_path / "client"
    import subprocess
    subprocess.run(
        ["gcc", "-g", "-O0", str(c), str(asm),
         f"-L{tmp_path}", f"-l:{old_so.name}", "-o", str(out)],
        check=True, capture_output=True,
    )
    return out.read_bytes()


def _plan(tmp_path, old_src, new_src, defaults=None, ver="V_1.0", ver2="V_2.0"):
    old_path = tmp_path / "old.so"
    old_path.write_bytes(_build(tmp_path, "old", old_src, ver))
    new_b = _build(tmp_path, "new", new_src, ver2)
    cli_b = _build_client(tmp_path, ver, old_path)
    old = parse_elf("old", old_path.read_bytes(), "old")
    new = parse_elf("new", new_b, "candidate")
    cli = parse_elf("cli", cli_b, "client")
    return plan_bridge(old, new, cli, ["fn"], defaults or {})


def test_unchanged_signature_still_forwards(tmp_path):
    old = "int fn(int a){return a;}\n"
    new = "int fn(int a){return a+1;}\n"
    fbs, summary = _plan(tmp_path, old, new)
    assert fbs[0].parameters[0]["kind"] == "i32"
    assert fbs[0].candidate_version == "V_2.0"


def test_struct_reorder_and_added_fields(tmp_path):
    old_src = """
        #include <stdint.h>
        struct s { int32_t a; double b; };
        int fn(struct s x){ return x.a + (int)x.b; }
    """
    new_src = """
        #include <stdint.h>
        struct s { double b; int32_t a; int32_t c; };
        int fn(struct s x){ return x.a + (int)x.b + x.c; }
    """
    fbs, summary = _plan(tmp_path, old_src, new_src, {"s.c": 7})
    fields = summary["functions"][0]["parameters"][0]["fields"]
    assert [(f["name"], f["status"]) for f in fields] == [
        ("a", "kept"), ("b", "kept"), ("c", "added")]


def test_field_type_change_rejected(tmp_path):
    old_src = """
        #include <stdint.h>
        struct s { int32_t a; };
        int fn(struct s x){ return x.a; }
    """
    new_src = """
        #include <stdint.h>
        struct s { double a; };
        int fn(struct s x){ return (int)x.a; }
    """
    with pytest.raises(BridgeError, match="同名同型|类型"):
        _plan(tmp_path, old_src, new_src)


def test_removed_field_rejected(tmp_path):
    old_src = """
        #include <stdint.h>
        struct s { int32_t a; int32_t b; };
        int fn(struct s x){ return x.a + x.b; }
    """
    new_src = """
        #include <stdint.h>
        struct s { int32_t a; };
        int fn(struct s x){ return x.a; }
    """
    with pytest.raises(BridgeError, match="缺失"):
        _plan(tmp_path, old_src, new_src)


def test_return_type_change_rejected(tmp_path):
    with pytest.raises(BridgeError, match="返回值"):
        _plan(tmp_path, "int fn(void){return 1;}\n", "double fn(void){return 1.0;}\n")


def test_param_count_change_rejected(tmp_path):
    with pytest.raises(BridgeError, match="参数个数"):
        _plan(tmp_path, "int fn(int a){return a;}\n", "int fn(int a,int b){return a+b;}\n")


def test_pointer_param_rejected(tmp_path):
    with pytest.raises(BridgeError, match="指针"):
        _plan(tmp_path, "int fn(int *p){return *p;}\n", "int fn(int *p){return *p;}\n")


def test_varargs_rejected(tmp_path):
    with pytest.raises(BridgeError, match="变参"):
        _plan(tmp_path, "int fn(int a, ...){return a;}\n",
                     "int fn(int a, ...){return a;}\n")


def test_two_struct_params_rejected(tmp_path):
    src = """
        #include <stdint.h>
        struct a { int32_t x; }; struct b { int32_t y; };
        int fn(struct a p, struct b q){ return p.x + q.y; }
    """
    with pytest.raises(BridgeError, match="结构体参数"):
        _plan(tmp_path, src, src)


def test_fractional_default_for_i32_rejected(tmp_path):
    old_src = """
        #include <stdint.h>
        struct s { int32_t a; };
        int fn(struct s x){ return x.a; }
    """
    new_src = """
        #include <stdint.h>
        struct s { int32_t a; int32_t b; };
        int fn(struct s x){ return x.a + x.b; }
    """
    with pytest.raises(BridgeError, match="整数"):
        _plan(tmp_path, old_src, new_src, {"s.b": 3.5})


def test_integer_valued_float_default_accepted(tmp_path):
    old_src = """
        #include <stdint.h>
        struct s { int32_t a; };
        int fn(struct s x){ return x.a; }
    """
    new_src = """
        #include <stdint.h>
        struct s { int32_t a; int32_t b; };
        int fn(struct s x){ return x.a + x.b; }
    """
    fbs, _ = _plan(tmp_path, old_src, new_src, {"s.b": 7.0})
    assert fbs[0].parameters[0]["struct"].fields[1].default == 7


def test_candidate_hidden_old_version_picks_default(tmp_path):
    """候选同时提供 hidden 旧版本别名 + 默认新版本时，应选择默认新版本（dlvsym 2.0）。"""
    import subprocess
    old_path = tmp_path / "old.so"
    old_path.write_bytes(_build(tmp_path, "old", "int fn(void){return 1;}\n", "V_1.0"))

    c = tmp_path / "nc.c"
    c.write_text("int fn_v1(void){return 1;}\n"
                 '__asm__(".symver fn_v1,fn@V_1.0");\n'
                 "int fn(void){return 2;}\n")
    m = tmp_path / "nc.map"
    m.write_text("V_2.0 { global: fn; local: *; };\nV_1.0 { };\n")
    new_path = tmp_path / "new.so"
    subprocess.run(["gcc", "-g", "-O0", "-fPIC", "-shared", "-Wl,-soname,libt.so.1",
                    f"-Wl,--version-script={m}", str(c), "-o", str(new_path)],
                   check=True, capture_output=True)
    cli_b = _build_client(tmp_path, "V_1.0", old_path)
    old = parse_elf("old", old_path.read_bytes(), "old")
    new = parse_elf("new", new_path.read_bytes(), "candidate")
    cli = parse_elf("cli", cli_b, "client")
    fbs, _ = plan_bridge(old, new, cli, ["fn"], {})
    assert fbs[0].candidate_version == "V_2.0"


def test_unsigned_field_rejected(tmp_path):
    src = """
        #include <stdint.h>
        struct s { uint32_t a; };
        int fn(struct s x){ return (int)x.a; }
    """
    with pytest.raises(BridgeError, match="int32 或 double"):
        _plan(tmp_path, src, src)


def test_candidate_missing_function_rejected(tmp_path):
    old_path = tmp_path / "old.so"
    old_path.write_bytes(_build(tmp_path, "old", "int fn(void){return 1;}\n", "V_1.0"))
    # 候选库只有别的函数
    new_b = _build(tmp_path, "new", "int other(void){return 2;}\n", "V_2.0")
    cli_b = _build_client(tmp_path, "V_1.0", old_path)
    old = parse_elf("old", old_path.read_bytes(), "old")
    new = parse_elf("new", new_b, "candidate")
    cli = parse_elf("cli", cli_b, "client")
    with pytest.raises(BridgeError, match="候选库没有可见函数"):
        plan_bridge(old, new, cli, ["fn"], {})


def test_old_missing_client_version_rejected(tmp_path):
    # 旧库只提供 V_9.0，客户端要 V_1.0
    old_path = tmp_path / "old.so"
    old_path.write_bytes(_build(tmp_path, "old", "int fn(void){return 1;}\n", "V_9.0"))
    new_b = _build(tmp_path, "new", "int fn(void){return 1;}\n", "V_2.0")
    # 客户端需要一个真正提供 V_1.0 的旧库来完成链接
    real_old_path = tmp_path / "realold.so"
    real_old_path.write_bytes(_build(tmp_path, "realold", "int fn(void){return 1;}\n", "V_1.0"))
    cli_b = _build_client(tmp_path, "V_1.0", real_old_path)
    old = parse_elf("old", old_path.read_bytes(), "old")
    new = parse_elf("new", new_b, "candidate")
    cli = parse_elf("cli", cli_b, "client")
    with pytest.raises(BridgeError, match="符号版本"):
        plan_bridge(old, new, cli, ["fn"], {})


def test_unversioned_client_uses_unversioned_candidate(tmp_path):
    # 无版本脚本：两边都未版本化
    import subprocess
    c = tmp_path / "o.c"
    c.write_text("int fn(void){return 1;}\n")
    old_path = tmp_path / "oldu.so"
    subprocess.run(["gcc", "-g", "-O0", "-fPIC", "-shared",
                    "-Wl,-soname,libt.so.1", str(c), "-o", str(old_path)], check=True)
    new_path = tmp_path / "newu.so"
    subprocess.run(["gcc", "-g", "-O0", "-fPIC", "-shared",
                    "-Wl,-soname,libt.so.1", str(c), "-o", str(new_path)], check=True)
    cc = tmp_path / "cu.c"
    cc.write_text("extern int fn(void); int main(void){return fn();}\n")
    cli_path = tmp_path / "clientu"
    subprocess.run(["gcc", str(cc), f"-L{tmp_path}", "-l:oldu.so", "-o", str(cli_path)], check=True)
    old = parse_elf("old", old_path.read_bytes(), "old")
    new = parse_elf("new", new_path.read_bytes(), "candidate")
    cli = parse_elf("cli", cli_path.read_bytes(), "client")
    fbs, _ = plan_bridge(old, new, cli, ["fn"], {})
    assert fbs[0].client_version is None
    assert fbs[0].candidate_version is None
