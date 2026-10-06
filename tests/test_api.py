"""端到端测试：通过 TestClient 调用 HTTP API，覆盖成功路径与整单 422 场景。"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.service import app

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "examples" / "dist"
SRC = ROOT / "examples" / "src"
BUILD = ROOT / "examples" / "scripts" / "build_example.sh"


@pytest.fixture(scope="session", autouse=True)
def examples_built():
    if not (DIST / "libgeom.so.1").exists() or not (DIST / "client").exists():
        subprocess.run(["bash", str(BUILD)], check=True, capture_output=True)
    return DIST


@pytest.fixture
def payload_factory(examples_built):
    def _f(**overrides):
        files = {
            "old_lib": ("libgeom.so.1", (DIST / "libgeom.so.1").read_bytes(), "application/octet-stream"),
            "candidate_lib": ("libgeom_v2.so", (DIST / "libgeom_v2.so").read_bytes(), "application/octet-stream"),
            "client": ("client", (DIST / "client").read_bytes(), "application/octet-stream"),
        }
        data = [
            ("functions", "box_area"),
            ("functions", "add_i32"),
            ("functions", "scale_f64"),
            ("functions", "log_event"),
            ("defaults", '{"box.depth": 10, "box.weight": 0.0}'),
        ]
        for k, v in overrides.items():
            if k in files:
                if v is None:
                    del files[k]
                else:
                    files[k] = v
            else:
                # data overrides: 传 None 表示删除，传 list 表示替换全部
                if v is None:
                    data = [d for d in data if d[0] != k]
                elif isinstance(v, list):
                    data = [d for d in data if d[0] != k] + v
                else:
                    data = [(kk, vv) if kk != k else (kk, v) for kk, vv in data]
        return files, data
    return _f


def _post(client, files, data):
    # 文件字段： (filename, bytes, ctype)；普通表单字段： (None, value)
    fields = list(files.items()) + [(k, (None, v)) for k, v in data]
    return client.post("/bridge", files=fields)


def test_healthz():
    with TestClient(app) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_bridge_success_bundle_and_mapping(payload_factory):
    files, data = payload_factory()
    with TestClient(app) as c:
        r = _post(c, files, data)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/gzip"
    tf = tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz")
    names = tf.getnames()
    assert names == ["libgeom.so.1", "libgeom.so.1.candidate.so", "mapping.json"]
    mapping = __import__("json").loads(tf.extractfile("mapping.json").read())
    assert mapping["soname"] == "libgeom.so.1"
    assert mapping["candidate_loaded_as"] == "$ORIGIN/libgeom.so.1.candidate.so"
    box = mapping["functions"][0]
    assert box["name"] == "box_area"
    assert box["client_symbol_version"] == "LIBGEOM_1.0"
    assert box["candidate_symbol_version"] == "LIBGEOM_2.0"
    field_status = {f["name"]: f["status"] for f in box["parameters"][0]["fields"]}
    assert field_status == {"width": "kept", "height": "kept", "scale": "kept",
                            "depth": "added", "weight": "added"}
    # 候选库原文逐字节打包
    assert tf.extractfile("libgeom.so.1.candidate.so").read() == (DIST / "libgeom_v2.so").read_bytes()


def test_bridge_runs_with_original_client(payload_factory, tmp_path):
    files, data = payload_factory()
    with TestClient(app) as c:
        r = _post(c, files, data)
    assert r.status_code == 200, r.text
    tf = tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz")
    tf.extractall(tmp_path)
    shutil.copy(DIST / "client", tmp_path / "client")
    out = subprocess.run(["./client"], cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "box_area = 34" in out.stdout       # 24 旧几何 + depth 默认 10
    assert "add_i32  = 7" in out.stdout
    assert "scale_f64 = 20.000000" in out.stdout


def test_missing_default_for_new_field_is_422(payload_factory):
    files, data = payload_factory(defaults='{"box.depth": 10}')
    with TestClient(app) as c:
        r = _post(c, files, data)
    assert r.status_code == 422
    assert "weight" in r.text


def test_extra_default_key_is_422(payload_factory):
    files, data = payload_factory(defaults='{"box.depth": 10, "box.weight": 0.0, "box.bogus": 1}')
    with TestClient(app) as c:
        r = _post(c, files, data)
    assert r.status_code == 422
    assert "bogus" in r.text


def test_client_not_needing_symbol_is_422(payload_factory):
    files, data = payload_factory(functions=[
        ("functions", "box_area"), ("functions", "ghost_func")])
    with TestClient(app) as c:
        r = _post(c, files, data)
    assert r.status_code == 422
    assert "ghost_func" in r.text


def test_illegal_elf_is_422(payload_factory):
    files, data = payload_factory(old_lib=("x.so", b"not an elf at all", "application/octet-stream"))
    with TestClient(app) as c:
        r = _post(c, files, data)
    assert r.status_code == 422
    assert "ELF" in r.text


def test_wrong_arch_is_422(payload_factory, tmp_path):
    # 交叉不可用时跳过；用 aarch64 交叉编译器造一个错误架构 .so
    if shutil.which("aarch64-linux-gnu-gcc") is None:
        pytest.skip("no aarch64 cross compiler")
    bad = tmp_path / "arm64.so"
    src = tmp_path / "t.c"
    src.write_text("int f(void){return 1;}\n")
    subprocess.run(["aarch64-linux-gnu-gcc", "-shared", "-fPIC", "-g", str(src), "-o", str(bad)], check=True)
    files, data = payload_factory(old_lib=("arm64.so", bad.read_bytes(), "application/octet-stream"))
    with TestClient(app) as c:
        r = _post(c, files, data)
    assert r.status_code == 422
    assert "x86-64" in r.text


def test_no_dwarf_is_422(payload_factory, tmp_path):
    stripped = tmp_path / "stripped.so"
    shutil.copy(DIST / "libgeom.so.1", stripped)
    subprocess.run(["strip", "--strip-debug", str(stripped)], check=True)
    files, data = payload_factory(old_lib=("s.so", stripped.read_bytes(), "application/octet-stream"))
    with TestClient(app) as c:
        r = _post(c, files, data)
    assert r.status_code == 422
    assert "DWARF" in r.text


def test_oversize_is_422(payload_factory):
    big = b"\x7fELF" + b"\0" * (4 * 1024 * 1024 + 10)
    files, data = payload_factory(candidate_lib=("big.so", big, "application/octet-stream"))
    with TestClient(app) as c:
        r = _post(c, files, data)
    assert r.status_code == 422
    assert "4 MiB" in r.text


def test_bad_default_json_type_is_422(payload_factory, tmp_path):
    # 客户端只弱引用 box_area 时应 422
    weak = tmp_path / "weakcli.c"
    weak.write_text(
        '#include <stdint.h>\n'
        'struct box { int32_t a,b; double c; };\n'
        '__attribute__((weak)) extern int32_t box_area(struct box);\n'
        'int main(void){ struct box b={0}; return box_area ? box_area(b) : 0; }\n')
    wout = tmp_path / "weakcli"
    import subprocess
    subprocess.run(["gcc", "-g", str(weak), "-o", str(wout)], check=True)
    files, data = payload_factory(client=("weakcli", wout.read_bytes(), "application/octet-stream"))
    with TestClient(app) as c:
        r = _post(c, files, data)
    assert r.status_code == 422
    assert "强未定义" in r.text or "弱引用" in r.text


def test_default_value_wrong_type_is_422(payload_factory):
    files, data = payload_factory(defaults='{"box.depth": "ten", "box.weight": 0.0}')
    with TestClient(app) as c:
        r = _post(c, files, data)
    assert r.status_code == 422
    assert "数字" in r.text


def test_duplicate_function_name_is_422(payload_factory):
    files, data = payload_factory(functions=[("functions", "box_area"),
                                             ("functions", "box_area")])
    with TestClient(app) as c:
        r = _post(c, files, data)
    assert r.status_code == 422
    assert "重复" in r.text
