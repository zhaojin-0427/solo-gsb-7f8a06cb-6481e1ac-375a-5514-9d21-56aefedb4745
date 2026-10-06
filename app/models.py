"""类型与函数签名的中间表示（从 DWARF 抽取后的规范化形态）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScalarType:
    kind: str  # "i32" | "f64"

    @property
    def c_type(self) -> str:
        return "int32_t" if self.kind == "i32" else "double"


I32 = ScalarType("i32")
F64 = ScalarType("f64")
VOID = "void"  # 返回值专用哨兵


@dataclass(frozen=True)
class FieldModel:
    name: str
    scalar: ScalarType
    offset: int  # DWARF 字节偏移（用于一致性/填充核查）


@dataclass
class StructModel:
    """按值结构体：只允许名称唯一的 int32/double 字段。"""

    name: str
    fields: list[FieldModel]
    byte_size: int
    anon: bool = False
    # 抽取时见过的 typedef 名（匿名结构体取首个作为代码名）
    alias: str | None = None

    def index(self) -> dict[str, FieldModel]:
        return {f.name: f for f in self.fields}

    @property
    def code_name(self) -> str:
        if self.anon:
            return self.alias or self.name
        return self.name


@dataclass
class ParameterModel:
    name: str
    # ScalarType 或 StructModel
    type: object


@dataclass
class FunctionModel:
    """DWARF 证据中的函数签名（与符号版本无关）。"""

    name: str
    return_type: object  # "void" | ScalarType
    parameters: list[ParameterModel]
    variadic: bool = False

    def structs(self) -> list[StructModel]:
        out: list[StructModel] = []
        for p in self.parameters:
            if isinstance(p.type, StructModel):
                out.append(p.type)
        return out


@dataclass
class DynamicSymbol:
    name: str
    version: str | None  # None = 未版本化
    hidden: bool
    defined: bool
    binding: str
    type: str
    index: int  # .gnu.version 中的版本索引


@dataclass
class ELFInfo:
    kind: str  # "old" | "candidate" | "client"
    soname: str | None
    # 函数名 -> 动态符号（版本化时可能有多条）
    dyn_defined: dict[str, list[DynamicSymbol]]
    dyn_undefined: dict[str, list[DynamicSymbol]]
    # 客户端强未定义： 函数名 -> 需要的版本（None=未版本化）
    needed_versions: dict[str, str | None]
    # DWARF 证据：函数名 -> 签名（多 CU 合并后应为唯一一致形态）
    functions: dict[str, FunctionModel] = field(default_factory=dict)
