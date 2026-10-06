"""使用 pyelftools 抽取：

* ELF 头/机器标识、SONAME；
* 动态符号（强/弱、已定义/未定义）与 GNU 符号版本；
* DWARF 中的函数签名（返回值、参数、按值结构体布局）。

所有不满足需求规格的证据问题统一抛 :class:`BridgeError`（最终 -> 422）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
from elftools.common.exceptions import ELFError

from .errors import BridgeError
from .models import (
    DynamicSymbol,
    ELFInfo,
    FieldModel,
    FunctionModel,
    I32,
    F64,
    ParameterModel,
    ScalarType,
    StructModel,
)

# DWARF 常量（避免在各处 import 庞大的枚举）
DW_TAG_FORMAL_PARAMETER = 0x05
DW_TAG_STRUCTURE_TYPE = 0x13
DW_TAG_BASE_TYPE = 0x24
DW_TAG_POINTER_TYPE = 0x0F
DW_TAG_REFERENCE_TYPE = 0x10
DW_TAG_ARRAY_TYPE = 0x01
DW_TAG_ENUMERATION_TYPE = 0x04
DW_TAG_SUBPROGRAM = 0x2E
DW_TAG_UNION_TYPE = 0x07
DW_TAG_TYPEDF = 0x17
DW_TAG_CONST_TYPE = 0x26
DW_TAG_VOLATILE_TYPE = 0x35
DW_TAG_RESTRICT_TYPE = 0x3C
DW_TAG_UNSPEC_PARAMETERS = 0x4B

DW_ATE_SIGNED = 0x05
DW_ATE_FLOAT = 0x04

VER_NDX_LOCAL = 0
VER_NDX_GLOBAL = 1
VER_DEF_FLG_BASE = 0x1
VER_NDX_HIDDEN = 0x8000


@dataclass
class _StructCache:
    # (cu_offset, die_offset) -> StructModel，保证同一结构体只解析一次
    by_die: dict[tuple[int, int], StructModel] = field(default_factory=dict)


def _uattr(die, name: str):
    attr = die.attributes.get(name)
    return None if attr is None else attr.value


def _follow(die, attr_name: str):
    """跟随 DWARF 引用（支持全局/局部 offset、ref_addr/ref4 等所有形式）。"""
    ref_die = die.get_DIE_from_attribute(attr_name)
    if ref_die is None:  # pragma: no cover - pyelftools 对坏引用返回 None
        raise BridgeError("DWARF 类型引用无法解析（证据不完整）")
    return ref_die


def _normalize_scalar(die) -> ScalarType | None:
    """剥掉 typedef/const/volatile/restrict 后，识别 int32 或 double。"""

    seen = 0
    while True:
        tag = die.tag
        if tag in ("DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
                   "DW_TAG_restrict_type"):
            if "DW_AT_type" not in die.attributes:
                return None  # typedef void* 之类 -> 不支持
            die = _follow(die, "DW_AT_type")
            seen += 1
            if seen > 64:
                raise BridgeError("DWARF 类型链过长，疑似环（证据矛盾）")
            continue
        if tag != "DW_TAG_base_type":
            return None
        encoding = _uattr(die, "DW_AT_encoding")
        size = _uattr(die, "DW_AT_byte_size")
        if encoding == DW_ATE_SIGNED and size == 4:
            return I32
        if encoding == DW_ATE_FLOAT and size == 8:
            return F64
        return None


def _parse_struct(die, cache: _StructCache, anon_seq: list[int]) -> StructModel:
    key = (die.cu.cu_offset, die.offset)
    cached = cache.by_die.get(key)
    if cached is not None:
        return cached

    tag_name = _uattr(die, "DW_AT_name")
    byte_size = _uattr(die, "DW_AT_byte_size")
    if byte_size is None:
        raise BridgeError("结构体缺少 DW_AT_byte_size（DWARF 证据不完整）")

    anon = tag_name is None
    if anon:
        anon_seq[0] += 1
        name = f"__anon_struct_{die.cu.cu_offset:x}_{die.offset:x}_{anon_seq[0]}"
    else:
        name = tag_name.decode("utf-8", "strict") if isinstance(tag_name, bytes) else str(tag_name)
        if not name:
            raise BridgeError("存在空名称结构体（类型证据不足）")

    struct = StructModel(name=name, fields=[], byte_size=byte_size, anon=anon)
    cache.by_die[key] = struct  # 先入缓存避免自引用环导致失控

    fields: list[FieldModel] = []
    for child in die.iter_children():
        if child.tag != "DW_TAG_member":
            raise BridgeError(f"结构体 {name} 含有非字段成员（不支持的类型构造）")
        if "DW_AT_external" in child.attributes and _uattr(child, "DW_AT_external"):
            raise BridgeError(f"结构体 {name} 含外部成员（不支持）")
        if "DW_AT_bit_size" in child.attributes or "DW_AT_bit_offset" in child.attributes:
            raise BridgeError(f"结构体 {name} 含位域（不支持的类型构造）")
        loc = child.attributes.get("DW_AT_data_member_location")
        if loc is None:
            raise BridgeError(f"结构体 {name} 的字段缺少偏移（DWARF 证据不完整）")
        if not isinstance(loc.value, int):
            raise BridgeError(f"结构体 {name} 的字段偏移为位置表达式（不支持的布局）")
        fname_raw = _uattr(child, "DW_AT_name")
        if fname_raw is None:
            raise BridgeError(f"结构体 {name} 含无名字段（不支持的类型构造）")
        fname = fname_raw.decode("utf-8", "strict") if isinstance(fname_raw, bytes) else str(fname_raw)
        if "DW_AT_type" not in child.attributes:
            raise BridgeError(f"结构体 {name} 的字段 {fname} 缺少类型（DWARF 证据不完整）")
        scalar = _normalize_scalar(_follow(child, "DW_AT_type"))
        if scalar is None:
            raise BridgeError(
                f"结构体 {name} 的字段 {fname} 不是 int32 或 double（不支持的字段类型）"
            )
        fields.append(FieldModel(name=fname, scalar=scalar, offset=loc.value))

    if not fields:
        raise BridgeError(f"结构体 {name} 为空（不支持的类型构造）")

    # 核查布局：偏移递增、无重叠、无矛盾、大小与字段一致
    seen_names: set[str] = set()
    cursor = 0
    for fm in fields:
        if fm.name in seen_names:
            raise BridgeError(f"结构体 {name} 中字段名 {fm.name} 不唯一")
        seen_names.add(fm.name)
        width = 4 if fm.scalar.kind == "i32" else 8
        if fm.offset < cursor:
            raise BridgeError(f"结构体 {name} 字段 {fm.name} 偏移重叠/回退（证据矛盾）")
        if fm.offset % width != 0:
            raise BridgeError(f"结构体 {name} 字段 {fm.name} 未按 {width} 字节对齐（非支持布局）")
        cursor = fm.offset + width
    if cursor != byte_size:
        raise BridgeError(
            f"结构体 {name} 大小 {byte_size} 与字段布局 {cursor} 不一致（含非声明填充/证据矛盾）"
        )

    struct.fields = fields
    return struct


def _resolve_param_type(die, cache: _StructCache, anon_seq: list[int], where: str):
    """参数/返回值的类型只能是 int32/double 或按值结构体。"""

    if "DW_AT_type" not in die.attributes:
        # 无类型参数（K&R 风格）证据不足
        raise BridgeError(f"{where} 缺少类型（DWARF 证据不完整）")
    target = _follow(die, "DW_AT_type")
    scalar = _normalize_scalar(target)
    if scalar is not None:
        return scalar
    # 剥 typedef 看是不是结构体：_normalize_scalar 遇到 struct 会返回 None，
    # 这里单独剥一层以支持 typedef struct {...} Name 形式。
    seen = 0
    cur = target
    while cur.tag in ("DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
                      "DW_TAG_restrict_type"):
        if "DW_AT_type" not in cur.attributes:
            break
        cur = _follow(cur, "DW_AT_type")
        seen += 1
        if seen > 64:
            raise BridgeError("DWARF 类型链过长，疑似环（证据矛盾）")
    if cur.tag == "DW_TAG_structure_type":
        return _parse_struct(cur, cache, anon_seq)
    if cur.tag in ("DW_TAG_pointer_type", "DW_TAG_reference_type", "DW_TAG_rvalue_reference_type"):
        raise BridgeError(f"{where} 为指针/引用类型（仅支持标量或单个按值结构体）")
    if cur.tag == "DW_TAG_array_type":
        raise BridgeError(f"{where} 为数组（不支持的参数类型）")
    if cur.tag == "DW_TAG_union_type":
        raise BridgeError(f"{where} 为联合体（不支持的参数类型）")
    if cur.tag == "DW_TAG_enumeration_type":
        raise BridgeError(f"{where} 为枚举（不支持的参数类型）")
    raise BridgeError(f"{where} 为不支持的类型（tag={cur.tag.replace('DW_TAG_', '')}）")


def _parse_function(die, cache: _StructCache, anon_seq: list[int]) -> FunctionModel:
    name_raw = _uattr(die, "DW_AT_name")
    if name_raw is None:
        linkage = _uattr(die, "DW_AT_linkage_name")
        raise BridgeError(
            "函数缺少 DW_AT_name" + (f"（仅发现 linkage_name={linkage!r}，疑似 C++ 改编名）" if linkage else "")
        )
    name = name_raw.decode("utf-8", "strict") if isinstance(name_raw, bytes) else str(name_raw)
    linkage = die.attributes.get("DW_AT_linkage_name")
    if linkage is not None:
        lname = linkage.value.decode("utf-8", "strict") if isinstance(linkage.value, bytes) else str(linkage.value)
        if lname != name:
            raise BridgeError(f"函数 {name} 带有 C++ 改编名 {lname}（仅支持 C 未改编符号）")

    if "DW_AT_type" in die.attributes:
        ret = _resolve_param_type(die, cache, anon_seq, f"函数 {name} 的返回值")
        if isinstance(ret, StructModel):
            raise BridgeError(f"函数 {name} 按值返回结构体（不支持的返回类型）")
    else:
        ret = "void"

    params: list[ParameterModel] = []
    variadic = False
    n_param = 0
    for child in die.iter_children():
        if child.tag == "DW_TAG_formal_parameter":
            n_param += 1
            where = f"函数 {name} 的第 {n_param} 个参数"
            if "DW_AT_artificial" in child.attributes:
                raise BridgeError(f"{where} 是编译器隐式参数（疑似 C++ 成员函数，不支持）")
            ptype = _resolve_param_type(child, cache, anon_seq, where)
            pname_raw = _uattr(child, "DW_AT_name")
            if pname_raw is None:
                pname = f"arg{n_param}"
            else:
                pname = pname_raw.decode("utf-8", "strict") if isinstance(pname_raw, bytes) else str(pname_raw)
            if isinstance(ptype, StructModel):
                n_structs = sum(1 for p in params if isinstance(p.type, StructModel))
                if n_structs >= 1:
                    raise BridgeError(f"函数 {name} 有超过一个按值结构体参数（至多一个）")
            params.append(ParameterModel(name=pname, type=ptype))
        elif child.tag == "DW_TAG_unspecified_parameters":
            variadic = True
        # 其它子标签（变量、词法块等）忽略

    if variadic:
        raise BridgeError(f"函数 {name} 是变参函数（不支持）")

    return FunctionModel(name=name, return_type=ret, parameters=params, variadic=variadic)


def _merge_signature(existing: FunctionModel, new: FunctionModel) -> FunctionModel:
    """同一函数在不同 CU 中的证据必须一致（参数名允许不同，按位置比较类型）。"""

    def sig_key(f: FunctionModel):
        ret = f.return_type
        ret_k = ret if isinstance(ret, str) else ret.kind
        parts = [ret_k]
        for p in f.parameters:
            t = p.type
            if isinstance(t, ScalarType):
                parts.append(("S", t.kind))
            else:
                parts.append(("T", t.code_name, t.byte_size,
                              tuple((x.name, x.scalar.kind, x.offset) for x in t.fields)))
        return tuple(parts)

    if sig_key(existing) != sig_key(new):
        raise BridgeError(f"函数 {new.name} 在不同编译单元中的签名互相矛盾")
    return existing


# ---------------------------------------------------------------------------
# GNU 符号版本解析（手写，直接读 .gnu.version / verneed / verdef，稳定不依赖
# pyelftools 的版本段封装 API）。
# ---------------------------------------------------------------------------

def _parse_verneed(elf: ELFFile) -> dict[int, tuple[str, str]]:
    """verneed 版本索引 -> (版本名, 来源文件名)。"""

    sec = elf.get_section_by_name(".gnu.version_r")
    out: dict[int, tuple[str, str]] = {}
    if sec is None:
        return out
    data = sec.data()
    link_data = elf.get_section(sec["sh_link"]).data()

    def link_cstr(off: int) -> str:
        end = link_data.find(b"\0", off)
        if end < 0 or off >= len(link_data):
            raise BridgeError(".gnu.version_r 字符串偏移越界（非法 ELF）")
        return link_data[off:end].decode("utf-8", "strict")

    pos = 0
    while pos < len(data):
        vn_version, vn_cnt, vn_file, vn_aux, vn_next = struct.unpack_from("<HHIII", data, pos)  # H H I I I: 见下
        if vn_version != 1:
            raise BridgeError(f".gnu.version_r 版本号 {vn_version} 不支持（非法版本证据）")
        file_name = link_cstr(vn_file)
        apos = pos + vn_aux
        for _ in range(vn_cnt):
            vna_hash, vna_flags, vna_other, vna_name, vna_next = struct.unpack_from(
                "<IHHII", data, apos)
            out[vna_other] = (link_cstr(vna_name), file_name)
            if vna_next == 0:
                break
            apos += vna_next
        if vn_next == 0:
            break
        pos += vn_next
    return out


def _parse_verdef(elf: ELFFile) -> dict[int, str]:
    """verdef 版本索引 -> 版本名。"""

    sec = elf.get_section_by_name(".gnu.version_d")
    out: dict[int, str] = {}
    if sec is None:
        return out
    data = sec.data()
    link_data = elf.get_section(sec["sh_link"]).data()

    def link_cstr_vd(off: int) -> str:
        end = link_data.find(b"\0", off)
        if end < 0 or off >= len(link_data):
            raise BridgeError(".gnu.version_d 字符串偏移越界（非法 ELF）")
        return link_data[off:end].decode("utf-8", "strict")

    pos = 0
    while pos < len(data):
        vd_version, vd_flags, vd_ndx, vd_cnt, vd_hash, vd_aux, vd_next = struct.unpack_from(
            "<HHHHIII", data, pos)
        if vd_version != 1:
            raise BridgeError(f".gnu.version_d 版本号 {vd_version} 不支持（非法版本证据）")
        apos = pos + vd_aux
        vda_name, vda_next = struct.unpack_from("<II", data, apos)
        out[vd_ndx] = link_cstr_vd(vda_name)
        if vd_next == 0:
            break
        pos += vd_next
    return out


def _parse_versym(elf: ELFFile, nsym: int) -> list[int] | None:
    sec = elf.get_section_by_name(".gnu.version")
    if sec is None:
        return None
    data = sec.data()
    if len(data) < nsym * 2:
        raise BridgeError(".gnu.version 长度与动态符号表不符（非法 ELF）")
    return list(struct.unpack_from(f"<{nsym}H", data, 0))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_elf(path: str, data: bytes, kind: str) -> ELFInfo:
    """解析并按规格核查一个上传的 ELF。

    kind: "old" / "candidate" / "client"。
    """

    from io import BytesIO

    label = {"old": "旧库", "candidate": "候选库", "client": "客户端"}[kind]

    try:
        fh = BytesIO(data)
        elf = ELFFile(fh)
    except ELFError as exc:
        raise BridgeError(f"{label} 不是合法 ELF：{exc}")
    except Exception as exc:  # pragma: no cover - 防御解析器自身异常
        raise BridgeError(f"{label} ELF 解析失败：{exc}")

    if elf.little_endian is False:
        raise BridgeError(f"{label} 不是小端 ELF（仅支持 ELF64 小端 x86-64）")
    if elf.elfclass != 64:
        raise BridgeError(f"{label} 不是 ELF64（仅支持 ELF64 小端 x86-64）")
    if elf.get_machine_arch() != "x64":
        raise BridgeError(f"{label} 架构为 {elf.get_machine_arch()}（仅支持 x86-64）")

    etype = elf["e_type"]
    if kind in ("old", "candidate"):
        if etype != "ET_DYN":
            raise BridgeError(f"{label} 不是共享对象（e_type={etype}）")

    # SONAME
    soname: str | None = None
    for seg in elf.iter_segments():
        if seg["p_type"] == "PT_DYNAMIC":
            for tag in seg.iter_tags():
                if tag.entry.d_tag == "DT_SONAME":
                    soname = tag.soname
            break

    # 动态符号与版本
    dynsec = elf.get_section_by_name(".dynsym")
    dyn_defined: dict[str, list[DynamicSymbol]] = {}
    dyn_undefined: dict[str, list[DynamicSymbol]] = {}
    needed_versions: dict[str, str | None] = {}

    if dynsec is not None and isinstance(dynsec, SymbolTableSection):
        nsym = dynsec.num_symbols()
        versym = _parse_versym(elf, nsym)
        verneed = _parse_verneed(elf)
        verdef = _parse_verdef(elf)

        for idx in range(1, nsym):
            sym = dynsec.get_symbol(idx)
            sname = sym.name
            if not sname:
                continue
            stype = sym["st_info"]["type"]
            if str(stype) not in ("STT_FUNC", "STT_NOTYPE"):
                continue
            bind = str(sym["st_info"]["bind"])
            defined = str(sym["st_shndx"]) != "SHN_UNDEF"
            vi = versym[idx] if versym is not None else None
            version: str | None
            hidden = bool(vi is not None and (vi & VER_NDX_HIDDEN))
            vi_plain = (vi & ~VER_NDX_HIDDEN) if vi is not None else None

            if not defined:
                if vi_plain in (None, VER_NDX_LOCAL, VER_NDX_GLOBAL):
                    version = None
                else:
                    info = verneed.get(vi_plain)
                    if info is None:
                        raise BridgeError(
                            f"{label} 未定义符号 {sname} 引用了缺失的版本索引 {vi_plain}（非法 ELF）"
                        )
                    version, _src = info
                dyn_undefined.setdefault(sname, []).append(
                    DynamicSymbol(sname, version, hidden, False, bind,
                                  str(stype), vi_plain if vi_plain is not None else 0)
                )
            else:
                if vi_plain in (None, VER_NDX_GLOBAL):
                    version = None
                elif vi_plain == VER_NDX_LOCAL:
                    continue  # 本地符号，不可见
                else:
                    version = verdef.get(vi_plain)
                    if version is None:
                        raise BridgeError(
                            f"{label} 已定义符号 {sname} 引用了缺失的 verdef 索引 {vi_plain}（非法 ELF）"
                        )
                dyn_defined.setdefault(sname, []).append(
                    DynamicSymbol(sname, version, hidden, True, bind,
                                  str(stype), vi_plain if vi_plain is not None else 1)
                )

    # 客户端：强未定义函数 -> 需要的 GNU 符号版本
    if kind == "client":
        if etype not in ("ET_DYN", "ET_EXEC"):
            raise BridgeError(f"客户端不是可执行/动态 ELF（e_type={etype}）")
        for sname, syms in dyn_undefined.items():
            strong_versions: set[str | None] = set()
            for s in syms:
                if s.binding == "STB_WEAK":
                    continue
                strong_versions.add(s.version)
            if not strong_versions:
                continue  # 只有弱引用，不作为强需求
            if len(strong_versions) > 1:
                raise BridgeError(f"客户端对 {sname} 同时强引用多个符号版本（证据矛盾）")
            needed_versions[sname] = next(iter(strong_versions))

    # DWARF 签名（库必须带 DWARF；客户端不使用其 DWARF）
    functions: dict[str, FunctionModel] = {}
    if kind in ("old", "candidate"):
        dwarf = elf.get_dwarf_info()
        if dwarf is None:
            raise BridgeError(f"{label} 不含 DWARF 调试信息（库须带 DWARF）")
        cache = _StructCache()
        anon_seq = [0]
        found_debug_info = False
        for cu in dwarf.iter_CUs():
            top = cu.get_top_DIE()
            if top.tag != "DW_TAG_compile_unit" and top.tag != "DW_TAG_skeleton_unit":
                continue
            found_debug_info = True
            for die in cu.iter_DIEs():
                if die.tag != "DW_TAG_subprogram":
                    continue
                if "DW_AT_declaration" in die.attributes:
                    continue  # 原型声明；定义处给出证据
                if "DW_AT_name" not in die.attributes:
                    continue
                try:
                    fn = _parse_function(die, cache, anon_seq)
                except BridgeError:
                    raise
                prev = functions.get(fn.name)
                if prev is None:
                    functions[fn.name] = fn
                else:
                    _merge_signature(prev, fn)
        if not found_debug_info:
            raise BridgeError(f"{label} 的 DWARF 中没有任何编译单元（证据不足）")

    return ELFInfo(
        kind=kind,
        soname=soname,
        dyn_defined=dyn_defined,
        dyn_undefined=dyn_undefined,
        needed_versions=needed_versions,
        functions=functions,
    )
