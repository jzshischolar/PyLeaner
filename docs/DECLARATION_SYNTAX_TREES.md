# 已处理声明类型的语法树结构

本文档整理 `LeanLspExtension/Register.lean` 当前已经支持的 Lean 声明类型，以及这些声明在语法树中的主要节点布局。这里的“语法树结构”指扩展代码实际依赖的 Lean parser node kind 和关键子节点，不是完整 parser 定义。

## 总体入口

声明提取从 command snapshot 的 `snap.stx` 开始。代码首先用 `getDeclarationKindFromSyntax` 读取 command 中的第一个关键字，并识别以下声明类型：

| kind | 关键字 |
| --- | --- |
| `def` | `def` |
| `theorem` | `theorem` |
| `lemma` | `lemma` |
| `example` | `example` |
| `structure` | `structure` |
| `class` | `class` |
| `inductive` | `inductive` |
| `abbrev` | `abbrev` |
| `instance` | `instance` |

随后 `getActualDeclaration` 会把外层 wrapper 规范化为真正的声明节点：

| 输入节点 | 处理方式 |
| --- | --- |
| `Lean.Parser.Command.declaration` | 取 `args[1]` 作为实际声明 |
| Mathlib `lemma` macro node，`kind.toString == "lemma"` | 取 `args[1]` 作为 theorem-shaped 声明 |
| `definition` / `structure` / `inductive` / `classInductive` / `theorem` / `instance` / `abbrev` / `example` | 直接作为实际声明 |

后续所有 `paramsText`、`typeText`、`bodyText` 都基于这个实际声明节点提取。

## 共享 body 节点

`def`、`abbrev`、`theorem`、`lemma`、`example` 的正文统一由 `extractBodyText` 处理。它会在实际声明节点中查找第一个声明级 body 节点：

| body node kind | 对应源码形式 | 返回文本 |
| --- | --- | --- |
| `Lean.Parser.Command.declValSimple` | `:= body` | 去掉开头 `:=` 后的 body |
| `Lean.Parser.Command.declValEqns` | equation-style alternatives，例如 `| 0 => ...` | 保留整个 equation-style 分支文本 |

这里故意查找声明级 `declValSimple` / `declValEqns`，而不是递归找任意 `:=` atom。这样可以避免把 body 内部的 `let x := ...`、结构字段赋值等误判为声明 body 起点。

## def

实际声明节点：

```text
Lean.Parser.Command.definition
```

当前代码依赖的结构：

```text
definition
├─ "def"
├─ declId
├─ optDeclSig
├─ declVal
└─ optDefDeriving / optional tail
```

提取规则：

| 字段 | 来源 |
| --- | --- |
| `name` | 第一个 identifier |
| `paramsText` | `optDeclSig.args[0]` |
| `typeText` | `optDeclSig.args[1]`，去掉开头 `:` |
| `bodyText` | `declValSimple` 或 `declValEqns` |

支持两类 body：

```lean
def f (n : Nat) : Nat := n + 1
```

对应 `declValSimple`，返回 `n + 1`。

```lean
def factorial : Nat -> Nat
  | 0 => 1
  | n + 1 => (n + 1) * factorial n
```

对应 `declValEqns`，返回所有 `| pattern => body` 分支。

## abbrev

实际声明节点：

```text
Lean.Parser.Command.abbrev
```

当前处理方式与 `def` 基本一致：

```text
abbrev
├─ "abbrev"
├─ declId
├─ optDeclSig
└─ declVal
```

提取规则：

| 字段 | 来源 |
| --- | --- |
| `name` | 第一个 identifier |
| `paramsText` | `optDeclSig.args[0]` |
| `typeText` | `optDeclSig.args[1]`，去掉开头 `:` |
| `bodyText` | `declValSimple` 或 `declValEqns` |

## theorem

实际声明节点：

```text
Lean.Parser.Command.theorem
```

当前代码依赖的结构：

```text
theorem
├─ "theorem"
├─ declId
├─ declSig
└─ declVal
```

提取规则：

| 字段 | 来源 |
| --- | --- |
| `name` | 第一个 identifier |
| `paramsText` | `declSig.args[0]` |
| `typeText` | `declSig.args[1]`，去掉开头 `:` |
| `bodyText` | `declValSimple` 或 `declValEqns` |

常见形式：

```lean
theorem t_ok (n : Nat) : n = n := by
  rfl
```

`bodyText` 返回：

```lean
by
  rfl
```

## lemma

`lemma` 来自 Mathlib macro，而不是 Lean core parser 中的普通 command kind。当前代码先识别 `kind.toString == "lemma"`，再取 `args[1]` 作为 theorem-shaped 声明节点。

抽象结构：

```text
lemma macro node
├─ modifiers / macro metadata
└─ theorem-shaped declaration
   ├─ "lemma"
   ├─ declId
   ├─ declSig
   └─ declVal
```

提取规则与 `theorem` 相同：

| 字段 | 来源 |
| --- | --- |
| `name` | 第一个 identifier |
| `paramsText` | `declSig.args[0]` |
| `typeText` | `declSig.args[1]`，去掉开头 `:` |
| `bodyText` | `declValSimple` 或 `declValEqns` |

## example

实际声明节点：

```text
Lean.Parser.Command.example
```

当前代码依赖的结构：

```text
example
├─ "example"
├─ optDeclSig
└─ declVal
```

提取规则：

| 字段 | 来源 |
| --- | --- |
| `name` | 固定为 `none` |
| `paramsText` | 通过 binder traversal 查找 `explicitBinder` / `implicitBinder` / `instBinder` |
| `typeText` | `optDeclSig.args[1]`，去掉开头 `:` |
| `bodyText` | `declValSimple` 或 `declValEqns` |

`example` 没有声明名，因此名称提取会提前返回 `none`。

## instance

实际声明节点：

```text
Lean.Parser.Command.instance
```

当前代码依赖的结构：

```text
instance
├─ optional priority / name
├─ "instance"
├─ optional declId
├─ declSig
└─ declVal
```

提取规则：

| 字段 | 来源 |
| --- | --- |
| `name` | 第一个 identifier；匿名 instance 可能为 `none` 或取到签名中的第一个 identifier，取决于语法树内容 |
| `paramsText` | `declSig.args[0]` |
| `typeText` | `declSig.args[1]`，去掉开头 `:` |
| `bodyText` | `whereStructInst` |

instance 的 body 不走普通 `extractBodyText`，而是专门查找：

```text
Lean.Parser.Command.whereStructInst
```

例如：

```lean
instance : MyClass Nat where
  value := 1
```

`bodyText` 返回从 `where` 开始的结构实例字段文本。

## structure

实际声明节点：

```text
Lean.Parser.Command.structure
```

当前代码依赖的结构：

```text
structure
├─ "structure"
├─ declId
├─ optDeclSig
├─ parent / optional parts
└─ fields part
```

提取规则：

| 字段 | 来源 |
| --- | --- |
| `name` | 第一个 identifier |
| `paramsText` | `optDeclSig.args[0]` |
| `typeText` | 当前固定为 `none` |
| `bodyText` | `extractBodyFromStructFields` 从字段区域提取 |

当前 `extractBodyFromStructFields` 对 `structure` 读取实际声明节点的 `args[4]` 作为字段区域。如果字段文本以 `where` 开头，会去掉 `where` 后返回剩余字段内容。

## class

声明关键字识别为：

```text
class
```

实际声明节点通常进入：

```text
Lean.Parser.Command.classInductive
```

提取规则在 `extractDeclarations` 中与 `structure`、`inductive` 共用同一个分支：

| 字段 | 来源 |
| --- | --- |
| `name` | 第一个 identifier |
| `paramsText` | `optDeclSig.args[0]` |
| `typeText` | 当前固定为 `none` |
| `bodyText` | 结构字段式 body 提取路径 |

注意：`getActualDeclaration` 已接受 `classInductive`，但 `extractBodyFromStructFields` 的显式 kind 判断目前只列出 `structure` 和 `inductive`。因此 class body 提取依赖当前语法树实际落到兼容结构，或者需要后续把 `classInductive` 显式加入字段提取判断。

## inductive

实际声明节点：

```text
Lean.Parser.Command.inductive
```

当前代码依赖的结构：

```text
inductive
├─ "inductive"
├─ declId
├─ optDeclSig
├─ universe / index / optional parts
└─ constructors part
```

提取规则：

| 字段 | 来源 |
| --- | --- |
| `name` | 第一个 identifier |
| `paramsText` | `optDeclSig.args[0]` |
| `typeText` | 当前固定为 `none` |
| `bodyText` | `extractBodyFromStructFields` 从 constructors 区域提取 |

当前实现同样读取实际声明节点的 `args[4]` 作为 constructors 区域，并去掉开头 `where`。

## 辅助提取函数对应关系

| 函数 | 作用 |
| --- | --- |
| `extractParamsFromOptDeclSig` | 从 `optDeclSig.args[0]` 提取参数，用于 `def` / `abbrev` / `structure` / `class` / `inductive` |
| `extractTypeFromOptDeclSig` | 从 `optDeclSig.args[1]` 提取类型，用于 `def` / `abbrev` |
| `extractParamsFromDeclSig` | 从 `declSig.args[0]` 提取参数，用于 `theorem` / `lemma` / `instance` |
| `extractTypeFromDeclSig` | 从 `declSig.args[1]` 提取命题或类型，用于 `theorem` / `lemma` / `instance` |
| `extractTypeText` | 专门处理 `definition` / `abbrev` / `example` 的 optDeclSig 类型提取 |
| `extractBodyText` | 提取 `declValSimple` / `declValEqns` |
| `extractBodyFromWhereStructInst` | 提取 instance 的 `whereStructInst` |
| `extractBodyFromStructFields` | 提取 structure / inductive 的字段或构造子区域 |

## 与错误发现的关系

声明语法树提取和错误发现是两条独立路径。当前 command 级错误只使用官方 `doc.diagnosticsRef` 中的 diagnostics，并通过 LSP range 映射回 command range；声明字段提取仍然基于 snapshot syntax tree 的节点 range。

