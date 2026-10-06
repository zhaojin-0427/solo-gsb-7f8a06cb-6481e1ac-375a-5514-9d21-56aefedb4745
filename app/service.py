"""兼容桥生成 HTTP API（FastAPI）。

POST /bridge:
    multipart/form-data:
        old_lib, candidate_lib, client: 各 ≤ 4 MiB 的 ELF
        functions: 可重复，客户端强未定义函数名
        defaults: 可选 JSON，形如 {"StructName.field": 值}

成功 -> application/gzip tar 包：
    <旧库 SONAME>.so                 桥接（保留旧 SONAME/符号版本）
    <SONAME>.candidate.so            候选库独立文件名副本
    mapping.json                     逐函数映射

任何证据缺失/矛盾/不可桥接/非法 ELF/架构不符 -> 422，且不落盘任何可下载产物。
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from .builder import compile_bridge, verify_artifact
from .codegen import generate_source, generate_version_script
from .elfparse import parse_elf
from .errors import BridgeError
from .planner import plan_bridge

MAX_SIZE = 4 * 1024 * 1024
DEFAULT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")

app = FastAPI(
    title="Linux C 兼容桥生成服务",
    version="1.0.0",
    description="上传旧版/候选 ELF64 x86-64 共享库与客户端，生成 ABI 兼容桥接 .so。",
)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


def _read_limited(upload: UploadFile, label: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = upload.file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_SIZE:
            raise BridgeError(f"{label} 超过 4 MiB 限制")
        chunks.append(chunk)
    if total == 0:
        raise BridgeError(f"{label} 为空")
    return b"".join(chunks)


@app.exception_handler(BridgeError)
async def bridge_error_handler(request, exc: BridgeError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": exc.detail()})


def _safe_soname_filename(soname: str) -> str:
    base = Path(soname).name
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$", base):
        raise BridgeError(f"旧库 SONAME {soname!r} 不适合作为文件名")
    if base in (".", "..") or "/" in base:
        raise BridgeError(f"旧库 SONAME {soname!r} 非法")
    return base


@app.post("/bridge")
async def create_bridge(
    old_lib: UploadFile = File(..., description="旧版共享库 ELF（≤4MiB，带 DWARF）"),
    candidate_lib: UploadFile = File(..., description="候选版共享库 ELF（≤4MiB，带 DWARF）"),
    client: UploadFile = File(..., description="按旧版链接的客户端 ELF（≤4MiB）"),
    functions: list[str] = Form(..., description="要桥接的客户端强未定义函数名（可重复）"),
    defaults: str | None = Form(None, description='可选 JSON：{"Struct.field": 值}'),
):
    # 整个请求在一次性临时目录中完成；任何失败都随目录删除，不留下半成品。
    workdir = Path(tempfile.mkdtemp(prefix="bridge-"))
    try:
        old_data = _read_limited(old_lib, "旧库")
        cand_data = _read_limited(candidate_lib, "候选库")
        client_data = _read_limited(client, "客户端")

        fn_names = [f.strip() for f in functions if f and f.strip()]
        if not fn_names:
            raise BridgeError("functions 不能为空", field="functions")
        if len(set(fn_names)) != len(fn_names):
            raise BridgeError("functions 中存在重复的函数名", field="functions")

        default_map: dict = {}
        if defaults is not None and defaults.strip():
            try:
                parsed = json.loads(defaults)
            except json.JSONDecodeError as exc:
                raise BridgeError(f"defaults 不是合法 JSON：{exc}", field="defaults")
            if not isinstance(parsed, dict):
                raise BridgeError("defaults 必须是 JSON 对象", field="defaults")
            for k, v in parsed.items():
                if not isinstance(k, str) or not DEFAULT_KEY_RE.match(k):
                    raise BridgeError(
                        f"defaults 键 {k!r} 必须形如 StructName.field_name", field="defaults"
                    )
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise BridgeError(f"defaults 键 {k} 的值必须是数字", field="defaults")
            default_map = parsed

        old_info = parse_elf(str(workdir / "old"), old_data, "old")
        cand_info = parse_elf(str(workdir / "cand"), cand_data, "candidate")
        client_info = parse_elf(str(workdir / "client"), client_data, "client")

        fbs, summary = plan_bridge(old_info, cand_info, client_info, fn_names, default_map)

        soname = _safe_soname_filename(old_info.soname)  # type: ignore[arg-type]
        candidate_file_name = f"{soname}.candidate.so"
        if candidate_file_name == soname:  # 理论不可达，防御
            raise BridgeError("候选库文件名与桥接文件名冲突")

        # 写候选库独立文件名副本（仅落盘用于打包/编译期不链接，绝不执行）
        cand_path = workdir / candidate_file_name
        cand_path.write_bytes(cand_data)

        source = generate_source(fbs, candidate_file_name)
        script = generate_version_script(fbs)
        (workdir / "bridge.c").write_text(source, encoding="utf-8")
        (workdir / "bridge.map").write_text(script, encoding="utf-8")

        bridge_path = compile_bridge(workdir, soname=soname)
        verify_artifact(bridge_path, soname, [fb.name for fb in fbs])
        bridge_bytes = bridge_path.read_bytes()

        mapping = {
            "soname": soname,
            "bridge_file": soname,
            "candidate_file": candidate_file_name,
            "candidate_loaded_as": f"$ORIGIN/{candidate_file_name}",
            "load_strategy": "dlmopen(LM_ID_NEWLM, ..., RTLD_NOW|RTLD_LOCAL|RTLD_DEEPBIND)",
            "symbol_resolution": "versioned=dlvsym, unversioned=dlsym",
            "functions": summary["functions"],
        }
        mapping_bytes = json.dumps(mapping, ensure_ascii=False, indent=2).encode("utf-8")

        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
            for arcname, payload in (
                (soname, bridge_bytes),
                (candidate_file_name, cand_data),
                ("mapping.json", mapping_bytes),
            ):
                info = tarfile.TarInfo(name=arcname)
                info.size = len(payload)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(payload))
        tar_buf.seek(0)

        headers = {"Content-Disposition": f'attachment; filename="{soname}.bridge.tgz"'}
        return StreamingResponse(
            tar_buf,
            media_type="application/gzip",
            headers=headers,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
