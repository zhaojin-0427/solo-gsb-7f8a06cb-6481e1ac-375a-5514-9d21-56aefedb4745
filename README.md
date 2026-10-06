# 兼容桥生成 HTTP API（compat-bridge）

为 **Linux C 库发布人员**提供的 ABI 兼容桥自动生成服务。

上传：

1. **旧版共享库** ELF64 小端 x86-64（带 DWARF，≤ 4 MiB）；
2. **候选版共享库** 同架构、带 DWARF、≤ 4 MiB；
3. **按旧版链接的客户端** ELF（≤ 4 MiB）；
4. 要桥接的**客户端强未定义函数名**（可多个）；
5. 候选库**结构体新增字段的默认值**（JSON，键形如 `结构体名.字段名`）。

服务在容器内用 **GCC** 生成并编译一个桥接 `.so`：

* 保留旧库 **SONAME** 与旧函数名；
* 提供客户端所需的 **GNU 符号版本**（如 `LIBGEOM_1.0`）；
* 每个入口按**旧结构体布局**接参，重建**新版结构体**（支持字段重排、以请求默认值补全新字段）后调用候选库；
* 签名未变化的函数同样转发；
* 候选库以**独立文件名** `$ORIGIN/<SONAME>.candidate.so` 经独立
  `dlmopen(LM_ID_NEWLM, …)` 命名空间加载，版本化符号用 `dlvsym`、未版本化用
  `dlsym` 精确解析，**桥接调用绝不回到自身**（即使候选库与桥接 SONAME 相同）。

任何证据缺失/矛盾、不可桥接改动、非法 ELF、架构不符均整单 **422**，
不留下可下载半成品；服务**从不执行**上传文件，只用 GCC 编译服务生成的 C 代码。

## 支持/拒绝的类型规则

| 维度 | 支持 | 拒绝（422） |
|---|---|---|
| 返回值 | `void` / `int32_t` / `double` | 其它（含按值返回结构体、64 位整型等） |
| 标量参数 | `int32_t` / `double`（含经 typedef/const 的同型） | `uint32_t`、`int64`、`char*`、枚举等 |
| 聚合参数 | **至多一个**按值传递的结构体 | 两个结构体、指针/数组/联合体/位域等 |
| 结构体字段 | 名称唯一的 int32/double，自然对齐 | 重名、嵌套、位域、非声明填充导致的尺寸矛盾 |
| 结构体演变 | 旧字段**同名同型**、允许重排、允许新增字段（须给默认值） | 改类型、删字段、字段重命名 |
| 函数 | 固定参数 | 变参（`...`）、C++ 改编名 |

> int32 以 DWARF `DW_ATE_signed` + 4 字节判定；double 以 `DW_ATE_float` + 8 字节判定。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 **8080** 端口，API：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `http://localhost:8080/healthz` | 健康检查 |
| POST | `http://localhost:8080/bridge` | multipart 表单，返回 gzip tar 包 |
| GET | `http://localhost:8080/docs` | OpenAPI 交互文档 |

## 实际调用样例

### 0) 准备示例输入（本机有 gcc 时）

```bash
bash examples/scripts/build_example.sh
# 产物在 examples/dist/：
#   libgeom.so.1     旧库（SONAME=libgeom.so.1，符号版本 LIBGEOM_1.0，带 DWARF）
#   libgeom_v2.so    候选库（独立文件名；结构体字段重排 + 新增 depth/weight）
#   client           按旧版链接、强引用 LIBGEOM_1.0 的客户端
```

示例新旧结构体：

```c
/* 旧 */  struct box { int32_t width; int32_t height; double scale; };
/* 新 */  struct box { double scale; int32_t width; int32_t height;
                       int32_t depth; double weight; };   // 后两者为新增
```

### 1) curl 调用

```bash
curl -sS -OJ --output bridge.tgz http://localhost:8080/bridge \
  -F old_lib=@examples/dist/libgeom.so.1 \
  -F candidate_lib=@examples/dist/libgeom_v2.so \
  -F client=@examples/dist/client \
  -F functions=box_area \
  -F functions=add_i32 \
  -F functions=scale_f64 \
  -F functions=log_event \
  -F defaults='{"box.depth": 10, "box.weight": 0.0}'
```

成功返回 `application/gzip` tar 包（HTTP 200），内含：

```
libgeom.so.1                     # 桥接：旧 SONAME、入口带 LIBGEOM_1.0 版本
libgeom.so.1.candidate.so        # 候选库独立文件名副本（逐字节）
mapping.json                     # 逐函数映射
```

### 2) 解包并用原客户端验证

```bash
mkdir out && tar xzf bridge.tgz -C out
cp examples/dist/client out/
cd out && ./client
```

输出（旧客户端二进制零改动，运行的是候选库实现）：

```
box_area = 34 (expect 24)      # 旧字段按重排后布局转发：几何部分 24 + 新字段默认 depth=10
add_i32  = 7 (expect 7)
scale_f64 = 20.000000 ...      # 签名未变也转发
```

`mapping.json` 摘要：

```json
{
  "soname": "libgeom.so.1",
  "bridge_file": "libgeom.so.1",
  "candidate_file": "libgeom.so.1.candidate.so",
  "candidate_loaded_as": "$ORIGIN/libgeom.so.1.candidate.so",
  "functions": [
    {
      "name": "box_area",
      "client_symbol_version": "LIBGEOM_1.0",
      "candidate_symbol_version": "LIBGEOM_2.0",
      "return": "i32",
      "parameters": [{
        "kind": "struct", "old_struct": "box", "new_struct": "box",
        "fields": [
          {"name": "width",  "type": "i32", "status": "kept",  "old_offset": 0, "new_offset": 8},
          {"name": "height", "type": "i32", "status": "kept",  "old_offset": 4, "new_offset": 12},
          {"name": "scale",  "type": "f64", "status": "kept",  "old_offset": 8, "new_offset": 0},
          {"name": "depth",  "type": "i32", "status": "added", "new_offset": 16, "default": 10},
          {"name": "weight", "type": "f64", "status": "added", "new_offset": 24, "default": 0.0}
        ]
      }]
    }
  ]
}
```

### 3) Python 调用样例

```python
import requests

with open("examples/dist/libgeom.so.1", "rb") as f_old, \
     open("examples/dist/libgeom_v2.so", "rb") as f_new, \
     open("examples/dist/client", "rb") as f_cli:
    resp = requests.post(
        "http://localhost:8080/bridge",
        files={
            "old_lib": ("libgeom.so.1", f_old, "application/octet-stream"),
            "candidate_lib": ("libgeom_v2.so", f_new, "application/octet-stream"),
            "client": ("client", f_cli, "application/octet-stream"),
        },
        data=[
            ("functions", "box_area"),
            ("functions", "add_i32"),
            ("functions", "scale_f64"),
            ("functions", "log_event"),
            ("defaults", '{"box.depth": 10, "box.weight": 0.0}'),
        ],
        timeout=60,
    )
resp.raise_for_status()
open("bridge.tgz", "wb").write(resp.content)
```

## 部署注意

* 解包后请保持三个文件在**同一目录**（桥接用 `$ORIGIN` 定位候选库），
  候选库文件名不可改名（`<SONAME>.candidate.so`）。
* 桥接与候选库**不在同一链接命名空间**：即使二者 SONAME 相同、
  甚至候选库被加载到同一进程，桥接入口的调用都严格落在候选库自身，不会递归回桥。
* 桥接 `.so` 在加载时（constructor）立即 `dlmopen(..., RTLD_NOW)` 并解析全部目标符号；
  候选库缺失或版本不匹配会在加载期 `abort` 并打印明确错误，而不是静默调用错误实现。
* 每个上传文件硬性限制 **4 MiB**；超限返回 422。
* 表单字段：`old_lib`、`candidate_lib`、`client` 为文件；
  `functions` 可重复；`defaults` 为可选 JSON 字符串。

## 失败响应（全部 422，无半成品）

```json
HTTP/1.1 422 Unprocessable Entity
{"detail": {"error": "候选库没有可见函数 ghost_func（无导出符号）"}}
```

典型拒绝原因：客户端对该函数没有强未定义引用 / 旧库缺该符号或所需版本 /
候选库无可见函数 / DWARF 缺签名证据 / 结构体字段改型或被删 / 新增字段缺默认值 /
`defaults` 给出无关键 / 变参或不支持的类型 / ELF 非法或架构不是 x86-64。

## 本地开发与测试（无需 Docker）

```bash
pip install -r requirements.txt
python -m uvicorn app.service:app --reload --port 8080
pytest -q          # 端到端：会现场用 gcc 编译各类旧/候选库与客户端
```

## 目录结构

```
app/
  errors.py     # 统一 422
  models.py     # 类型/函数/符号的中间表示
  elfparse.py   # pyelftools：ELF 标识、动态符号、GNU 版本、DWARF 签名
  planner.py    # 可桥接性规则检查 + 逐函数映射
  codegen.py    # 生成桥接 C 源码与 GNU 版本脚本
  builder.py    # 容器内 GCC 编译 + 产物静态核查
  service.py    # FastAPI /bridge、打包、临时目录即弃
examples/       # 旧库/候选库/客户端源码与一键构建脚本
tests/          # 成功路径 + 全部 422 路径 + 真实运行时 ABI 验证
```
