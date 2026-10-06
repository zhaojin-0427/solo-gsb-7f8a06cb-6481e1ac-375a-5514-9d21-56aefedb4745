"""根据旧库/候选库/客户端的 ELF 证据规划兼容桥。

规则（任何不满足都整单 422）：

* 客户端必须对每个请求的函数名持有强未定义引用；
* 旧库必须以对应 GNU 符号版本导出该函数，且 DWARF 中签名完整；
* 候选库必须有对应「可见」函数（默认版本/未版本化、非 hidden 符号版本）；
* 返回值只允许 void / int32 / double；参数只允许这些标量，或至多一个按值结构体；
* 结构体字段只允许名称唯一的 int32/double；旧字段必须在新版同名同型；
  允许重排，新增字段必须在请求中给出默认值；
* 签名未变化的函数也必须转发。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .errors import BridgeError
from .models import ELFInfo, ScalarType, StructModel, DynamicSymbol

_C_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_C_KEYWORDS = {
    "auto", "break", "case", "char", "const", "continue", "default", "do",
    "double", "else", "enum", "extern", "float", "for", "goto", "if",
    "inline", "int", "long", "register", "restrict", "return", "short",
    "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
    "unsigned", "void", "volatile", "while", "_Alignas", "_Alignof",
    "_Atomic", "_Bool", "_Complex", "_Generic", "_Imaginary", "_Noreturn",
    "_Static_assert", "_Thread_local",
}


@dataclass
class FieldDelta:
    name: str
    scalar: str
    old_offset: int | None
    new_offset: int
    status: str  # "kept" | "added"
    default: float | int | None = None


@dataclass
class StructBridge:
    old_name: str
    new_name: str
    byte_size: int
    fields: list[FieldDelta] = field(default_factory=list)

    @property
    def added(self) -> list[FieldDelta]:
        return [f for f in self.fields if f.status == "added"]


@dataclass
class FunctionBridge:
    name: str
    client_version: str | None
    old_versions_seen: list[str | None]
    candidate_version: str | None  # None 表示候选符号未版本化 -> dlsym
    return_kind: str  # "void" | "i32" | "f64"
    parameters: list[dict]  # [{name, kind:"i32"/"f64"/"struct", struct: StructBridge}]


def _check_ident(name: str) -> None:
    if not _C_IDENT.match(name):
        raise BridgeError(f"函数名 {name!r} 不是合法 C 标识符", field="functions")
    if name in _C_KEYWORDS:
        raise BridgeError(f"函数名 {name!r} 是 C 关键字，无法作为入口符号", field="functions")


def _type_kind(t) -> str:
    if isinstance(t, str):
        return t  # "void"
    if isinstance(t, ScalarType):
        return t.kind
    return "struct"


def _candidate_visible_symbol(cand: ELFInfo, fn_name: str) -> tuple[DynamicSymbol, list[DynamicSymbol]]:
    """选出候选库中实际可见的默认符号（默认版本或未版本化，非 hidden）。"""

    syms = cand.dyn_defined.get(fn_name)
    if not syms:
        raise BridgeError(f"候选库没有可见函数 {fn_name}（无导出符号）")

    visible = [s for s in syms if not s.hidden and s.binding in ("STB_GLOBAL", "STB_WEAK")]
    if not visible:
        raise BridgeError(f"候选库函数 {fn_name} 的符号版本全部为 hidden（无可见函数）")

    unversioned = [s for s in visible if s.version is None]
    if len(unversioned) > 1:
        raise BridgeError(f"候选库函数 {fn_name} 存在多个未版本化定义（证据矛盾）")
    if unversioned:
        return unversioned[0], visible

    # 版本化：默认版本是 verdef 中 BASE 节点之外、被 versym 引用的版本。
    # pyelftools 没有直接给「是否默认版本」标志，但未被隐藏的普通定义即默认版本；
    # 同一函数名出现多个可见版本定义时，旧版本通常以 @hidden 标记，已被上面滤掉。
    if len(visible) > 1:
        vers = sorted({s.version for s in visible})
        raise BridgeError(
            f"候选库函数 {fn_name} 同时有多个可见默认版本 {vers}（无法确定对应版本）"
        )
    return visible[0], visible


def _default_value(name_path: str, kind: str, raw) -> int | float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise BridgeError(
            f"新字段 {name_path} 类型为 {kind}，默认值必须是数字（收到 {raw!r}）",
            field="defaults",
        )
    if kind == "f64":
        value: int | float = float(raw)
    else:
        fv = float(raw)
        if not fv.is_integer():
            raise BridgeError(
                f"新字段 {name_path} 是 int32，默认值必须为整数（收到 {raw!r}）",
                field="defaults",
            )
        value = int(fv)
        if not (-2**31 <= value <= 2**31 - 1):
            raise BridgeError(
                f"新字段 {name_path} 的默认值 {value} 超出 int32 范围", field="defaults"
            )
    if not math.isfinite(float(value)):
        raise BridgeError(f"新字段 {name_path} 的默认值不是有限数值（收到 {raw!r}）", field="defaults")
    return value


def _bridge_struct(old_fn: str, pos: int, old_s: StructModel, new_s: StructModel,
                   defaults: dict) -> StructBridge:
    path_prefix = f"{old_fn} 第 {pos} 个参数结构体 {old_s.code_name}"
    if new_s.anon:
        raise BridgeError(f"候选库中{path_prefix}对应的结构体匿名（无法关联默认值）")

    new_index = new_s.index()
    sb = StructBridge(old_name=old_s.code_name, new_name=new_s.code_name,
                      byte_size=new_s.byte_size)

    # 旧字段：必须同名同型
    for fm in old_s.fields:
        nf = new_index.get(fm.name)
        if nf is None:
            raise BridgeError(
                f"{path_prefix} 的旧字段 {fm.name} 在候选库结构体中缺失（不可桥接的改动）"
            )
        if nf.scalar.kind != fm.scalar.kind:
            raise BridgeError(
                f"{path_prefix} 的字段 {fm.name} 类型由 {fm.scalar.kind} 变为 "
                f"{nf.scalar.kind}（不可桥接的改动）"
            )
        sb.fields.append(FieldDelta(
            name=fm.name, scalar=fm.scalar.kind,
            old_offset=fm.offset, new_offset=nf.offset, status="kept"))

    # 新字段：必须由请求给默认值
    old_names = {f.name for f in old_s.fields}
    for nf in new_s.fields:
        if nf.name in old_names:
            continue
        key = f"{new_s.code_name}.{nf.name}"
        if key not in defaults:
            raise BridgeError(
                f"{path_prefix} 在候选库中新增字段 {nf.name}（{nf.scalar.kind}），"
                f"但请求未给出 {key} 的默认值"
            )
        value = _default_value(key, nf.scalar.kind, defaults[key])
        sb.fields.append(FieldDelta(
            name=nf.name, scalar=nf.scalar.kind,
            old_offset=None, new_offset=nf.offset, status="added", default=value))

    return sb


def plan_bridge(old: ELFInfo, candidate: ELFInfo, client: ELFInfo,
                fn_names: list[str], defaults: dict) -> tuple[list[FunctionBridge], dict]:
    if not fn_names:
        raise BridgeError("functions 不能为空")
    if len(set(fn_names)) != len(fn_names):
        raise BridgeError("functions 中存在重复的函数名")

    if old.soname is None:
        raise BridgeError("旧库没有 SONAME（桥接必须保留旧库 SONAME）")

    fbs: list[FunctionBridge] = []
    # 已消费默认值的新字段（同一新结构体被多个参数/函数使用时默认值只给一次）
    claimed_defaults: set[str] = set()
    # (旧结构体名, 新结构体名) -> 首个已规划的结构体桥，用于核查布局一致
    struct_cache: dict[tuple[str, str], StructBridge] = {}

    def bridge_struct_cached(old_fn: str, pos: int, old_s: StructModel,
                             new_s: StructModel) -> StructBridge:
        # 每次都做完整的字段同名同型/默认值校验
        sb = _bridge_struct(old_fn, pos, old_s, new_s, defaults)
        key = (old_s.code_name, new_s.code_name)
        cached = struct_cache.get(key)
        if cached is not None:
            same = [(f.name, f.scalar, f.old_offset, f.new_offset, f.default)
                    for f in cached.fields]
            cur = [(f.name, f.scalar, f.old_offset, f.new_offset, f.default)
                   for f in sb.fields]
            if same != cur:
                raise BridgeError(
                    f"候选库中同名结构体 {new_s.code_name} 在不同位置出现互相矛盾的布局（证据矛盾）"
                )
            return cached
        struct_cache[key] = sb
        for fld in sb.added:
            claimed_defaults.add(f"{sb.new_name}.{fld.name}")
        return sb

    for name in fn_names:
        _check_ident(name)

        # 1) 客户端必须强未定义
        if name not in client.needed_versions:
            if name in client.dyn_undefined:
                raise BridgeError(f"客户端对 {name} 只有弱引用（要求强未定义引用）")
            raise BridgeError(f"客户端没有未定义引用 {name}（必须先按旧版链接客户端）")
        client_ver = client.needed_versions[name]

        # 2) 旧库必须满足所选导入（符号 + 版本）
        old_syms = old.dyn_defined.get(name)
        if not old_syms:
            raise BridgeError(f"旧库没有导出函数 {name}（不满足客户端导入）")
        old_visible = [s for s in old_syms if not s.hidden]
        old_vers = sorted({s.version for s in old_visible})
        if client_ver is None:
            if not any(s.version is None for s in old_visible):
                raise BridgeError(
                    f"旧库函数 {name} 只有版本化导出 {old_vers}，无法满足客户端的未版本化引用"
                )
        else:
            if not any(s.version == client_ver for s in old_visible):
                raise BridgeError(
                    f"旧库函数 {name} 没有客户端所需的符号版本 {client_ver}（现有：{old_vers}）"
                )

        # 3) DWARF 证据（旧库）
        old_fn = old.functions.get(name)
        if old_fn is None:
            raise BridgeError(f"旧库 DWARF 中缺少函数 {name} 的签名证据")

        # 4) 候选库必须有对应可见函数（符号层面先查，DWARF 随后）
        cand_sym, _all = _candidate_visible_symbol(candidate, name)

        cand_fn = candidate.functions.get(name)
        if cand_fn is None:
            raise BridgeError(f"候选库 DWARF 中缺少函数 {name} 的签名证据")

        # 5) 返回值类型
        old_ret = _type_kind(old_fn.return_type)
        new_ret = _type_kind(cand_fn.return_type)
        if old_ret not in ("void", "i32", "f64"):
            raise BridgeError(f"函数 {name} 返回值类型不受支持（{old_ret}）")
        if old_ret != new_ret:
            raise BridgeError(
                f"函数 {name} 返回值类型由 {old_ret} 变为 {new_ret}（不可桥接的改动）"
            )

        # 6) 参数：数量一致；标量同名型；至多一个按值结构体
        if len(old_fn.parameters) != len(cand_fn.parameters):
            raise BridgeError(f"函数 {name} 参数个数发生变化（不可桥接的改动）")

        param_out: list[dict] = []
        n_struct = 0
        for pos, (op, np) in enumerate(zip(old_fn.parameters, cand_fn.parameters), start=1):
            ok, nk = _type_kind(op.type), _type_kind(np.type)
            if ok != nk:
                raise BridgeError(
                    f"函数 {name} 第 {pos} 个参数类别变化（{ok} -> {nk}，不可桥接的改动）"
                )
            if ok in ("i32", "f64"):
                param_out.append({"name": op.name, "kind": ok, "struct": None})
            elif ok == "struct":
                n_struct += 1
                if n_struct > 1:
                    raise BridgeError(f"函数 {name} 有超过一个按值结构体参数（至多一个）")
                sb = bridge_struct_cached(name, pos, op.type, np.type)
                param_out.append({"name": op.name, "kind": "struct", "struct": sb})
            else:
                raise BridgeError(f"函数 {name} 第 {pos} 个参数类型 {ok} 不受支持")

        fbs.append(FunctionBridge(
            name=name,
            client_version=client_ver,
            old_versions_seen=[s.version for s in old_visible],
            candidate_version=cand_sym.version,
            return_kind=old_ret,
            parameters=param_out,
        ))

    # 默认值中不允许出现无关键
    unknown = sorted(set(defaults) - claimed_defaults)
    if unknown:
        raise BridgeError(
            f"defaults 给出了并非任何候选库新字段的键：{unknown}", field="defaults"
        )

    summary = {
        "soname": old.soname,
        "functions": [
            {
                "name": fb.name,
                "client_symbol_version": fb.client_version,
                "candidate_symbol_version": fb.candidate_version,
                "return": fb.return_kind,
                "parameters": [
                    {
                        "name": p["name"],
                        "kind": p["kind"],
                        **({
                            "old_struct": p["struct"].old_name,
                            "new_struct": p["struct"].new_name,
                            "new_size_bytes": p["struct"].byte_size,
                            "fields": [
                                {
                                    "name": f.name,
                                    "type": f.scalar,
                                    "status": f.status,
                                    "old_offset": f.old_offset,
                                    "new_offset": f.new_offset,
                                    **({"default": f.default} if f.default is not None else {}),
                                }
                                for f in p["struct"].fields
                            ],
                        } if p["struct"] else {}),
                    }
                    for p in fb.parameters
                ],
            }
            for fb in fbs
        ],
    }
    return fbs, summary
