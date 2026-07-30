# Syntax Tree Structure for Supported Declaration Types

This document covers the Lean declaration types currently supported in `LeanLspExtension/Register.lean` and the primary node layout of these declarations in the syntax tree. "Syntax tree structure" here refers to the Lean parser node kinds and key child nodes that the extension code actually depends on, not the complete parser definitions.

## Top-Level Entry

Declaration extraction starts from the `snap.stx` of the command snapshot. The code first uses `getDeclarationKindFromSyntax` to read the first keyword in the command and identifies the following declaration types:

| kind | Keyword |
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

Then `getActualDeclaration` normalizes outer wrappers into the actual declaration node:

| Input Node | Processing |
| --- | --- |
| `Lean.Parser.Command.declaration` | Takes `args[1]` as the actual declaration |
| Mathlib `lemma` macro node, `kind.toString == "lemma"` | Takes `args[1]` as a theorem-shaped declaration |
| `definition` / `structure` / `inductive` / `classInductive` / `theorem` / `instance` / `abbrev` / `example` | Used directly as the actual declaration |

All subsequent `paramsText`, `params`, `typeText`, `bodyText` extractions are based on this actual declaration node.

### Dual-Track Parameter Extraction

Every declaration provides parameter information in two forms simultaneously:

| Field | Type | Description |
| --- | --- | --- |
| `paramsText` | `Option String` | Raw parameter text, e.g. `"(x : Nat) (y : Nat)"` (for backward compatibility) |
| `params` | `Option (Array ParamInfo)` | Structured parameter array; each element contains `name`, `type`, `binderKind` |

The `ParamInfo` structure:

| Field | Type | Description |
| --- | --- | --- |
| `name` | `Option String` | Parameter name; `none` for anonymous parameters |
| `type` | `Option String` | Type annotation text |
| `binderKind` | `String` | Binding style: `"explicit"` / `"implicit"` / `"strictImplicit"` / `"instance"` |

## Shared Body Nodes

The body of `def`, `abbrev`, `theorem`, `lemma`, and `example` is handled uniformly by `extractBodyText`. It finds the first declaration-level body node in the actual declaration node:

| body node kind | Corresponding source form | Returned text |
| --- | --- | --- |
| `Lean.Parser.Command.declValSimple` | `:= body` | The body after stripping the leading `:=` |
| `Lean.Parser.Command.declValEqns` | Equation-style alternatives, e.g. `| 0 => ...` | The full equation-style branch text |

The code intentionally looks for declaration-level `declValSimple` / `declValEqns` rather than recursively finding any `:=` atom. This avoids misidentifying `let x := ...` inside the body or structure field assignments as the start of the declaration body.

`DeclarationInfo.bodyRange` is the LSP range of the same body syntax. For a
`declValSimple`, it points to the body term and excludes `:=` and leading
whitespace. Comments between `:=` and the first expression are part of both
`bodyText` and `bodyRange`; slicing the original source with `bodyRange`
therefore reproduces `bodyText` exactly. For equation-style declarations it covers the complete
`declValEqns` node. It is `none` when no dedicated body syntax node exists.

## def

Actual declaration node:

```text
Lean.Parser.Command.definition
```

Structure relied on by current code:

```text
definition
├─ "def"
├─ declId
├─ optDeclSig
├─ declVal
└─ optDefDeriving / optional tail
```

Extraction rules:

| Field | Source |
| --- | --- |
| `name` | First identifier |
| `paramsText` | `optDeclSig.args[0]` |
| `params` | `extractStructuredParams` traverses binders from the actual declaration node |
| `typeText` | `optDeclSig.args[1]`, stripping the leading `:` |
| `bodyText` | `declValSimple` or `declValEqns` |

Two body forms are supported:

```lean
def f (n : Nat) : Nat := n + 1
```

Corresponds to `declValSimple`, returning `n + 1`.

```lean
def factorial : Nat -> Nat
  | 0 => 1
  | n + 1 => (n + 1) * factorial n
```

Corresponds to `declValEqns`, returning all `| pattern => body` branches.

## abbrev

Actual declaration node:

```text
Lean.Parser.Command.abbrev
```

Processing is essentially the same as `def`:

```text
abbrev
├─ "abbrev"
├─ declId
├─ optDeclSig
└─ declVal
```

Extraction rules:

| Field | Source |
| --- | --- |
| `name` | First identifier |
| `paramsText` | `optDeclSig.args[0]` |
| `params` | `extractStructuredParams` traverses binders from the actual declaration node |
| `typeText` | `optDeclSig.args[1]`, stripping the leading `:` |
| `bodyText` | `declValSimple` or `declValEqns` |

## theorem

Actual declaration node:

```text
Lean.Parser.Command.theorem
```

Structure relied on by current code:

```text
theorem
├─ "theorem"
├─ declId
├─ declSig
└─ declVal
```

Extraction rules:

| Field | Source |
| --- | --- |
| `name` | First identifier |
| `paramsText` | `declSig.args[0]` |
| `params` | `extractStructuredParams` traverses binders from the actual declaration node |
| `typeText` | `declSig.args[1]`, stripping the leading `:` |
| `bodyText` | `declValSimple` or `declValEqns` |

Common form:

```lean
theorem t_ok (n : Nat) : n = n := by
  rfl
```

`bodyText` returns:

```lean
by
  rfl
```

## lemma

`lemma` comes from a Mathlib macro rather than a regular command kind in the Lean core parser. The current code first identifies `kind.toString == "lemma"`, then takes `args[1]` as the theorem-shaped declaration node.

Abstract structure:

```text
lemma macro node
├─ modifiers / macro metadata
└─ theorem-shaped declaration
   ├─ "lemma"
   ├─ declId
   ├─ declSig
   └─ declVal
```

Extraction rules are the same as `theorem`:

| Field | Source |
| --- | --- |
| `name` | First identifier |
| `paramsText` | `declSig.args[0]` |
| `params` | `extractStructuredParams` traverses binders from the actual declaration node |
| `typeText` | `declSig.args[1]`, stripping the leading `:` |
| `bodyText` | `declValSimple` or `declValEqns` |

## example

Actual declaration node:

```text
Lean.Parser.Command.example
```

Structure relied on by current code:

```text
example
├─ "example"
├─ optDeclSig
└─ declVal
```

Extraction rules:

| Field | Source |
| --- | --- |
| `name` | Always `none` |
| `paramsText` | Found via binder traversal for `explicitBinder` / `implicitBinder` / `instBinder` |
| `params` | `extractStructuredParams` traverses binders from the actual declaration node |
| `typeText` | `optDeclSig.args[1]`, stripping the leading `:` |
| `bodyText` | `declValSimple` or `declValEqns` |

`example` has no declaration name, so name extraction returns `none` early.

## instance

Actual declaration node:

```text
Lean.Parser.Command.instance
```

Structure relied on by current code:

```text
instance
├─ optional priority / name
├─ "instance"
├─ optional declId
├─ declSig
└─ declVal
```

Extraction rules:

| Field | Source |
| --- | --- |
| `name` | First identifier; anonymous instances may be `none` or get the first identifier in the signature, depending on syntax tree content |
| `paramsText` | `declSig.args[0]` |
| `params` | `extractStructuredParams` traverses binders from the actual declaration node |
| `typeText` | `declSig.args[1]`, stripping the leading `:` |
| `bodyText` | `whereStructInst`, falling back to ordinary `declValSimple` / `declValEqns` |

The instance extractor first looks for:

```text
Lean.Parser.Command.whereStructInst
```

For example:

```lean
instance : MyClass Nat where
  value := 1
```

`bodyText` returns the structure instance field text starting from `where`.
Instances written with ordinary declaration syntax, such as
`instance marker : Marker := { value := 1 }`, fall back to the shared
`extractBodyText` path. Their `bodyRange` follows the same exact-slice rule as
other simple declaration bodies, including comments after `:=`.

## structure

Actual declaration node:

```text
Lean.Parser.Command.structure
```

Structure relied on by current code:

```text
structure
├─ "structure"
├─ declId
├─ optDeclSig
├─ parent / optional parts
└─ fields part
```

Extraction rules:

| Field | Source |
| --- | --- |
| `name` | First identifier |
| `paramsText` | `optDeclSig.args[0]` |
| `params` | `extractStructuredParams` traverses binders from the actual declaration node |
| `typeText` | Currently always `none` |
| `bodyText` | `extractBodyFromStructFields` extracts from the fields region |
| `bodyRange` | Exact source span matching `bodyText`, excluding `where` but including a custom constructor/comments |
| `fields` | Structured direct fields from the four `struct*Binder` node kinds |

Currently `extractBodyFromStructFields` reads `args[4]` of the actual declaration node as the fields region for `structure`. If the field text starts with `where`, it strips `where` and returns the remaining field content.

The `structFields` node contains four source binder forms:

| Syntax kind | `binderKind` |
| --- | --- |
| `structSimpleBinder` / `structExplicitBinder` | `explicit` |
| `structImplicitBinder` | `implicit` |
| `structInstBinder` | `instance` |

Each directly declared source field becomes a `StructureFieldInfo`. A grouped
binder such as `(x y : α)` becomes two records with a shared binder range and
type text. The command snapshot's elaborated environment is then used to match
each source field to its generated projection. Structure parameters and the
projection's structure argument are opened according to
`ProjectionFunctionInfo.numParams + 1`; binders inside the field type itself
are deliberately left intact. Consequently, a field of type
`Nat → BEq Nat` is not classified as a class field merely because its result is
a class. The residual field type is reduced to weak-head normal form, its head
constant is checked against Lean's class registry, and `isProp` is obtained
from Lean Meta. `isPropType` independently reports whether that reduced type is
`Sort 0` (`Prop`), including aliases and parenthesized syntax. This distinction
is necessary because `h : True` is proof-valued, while `goal : Prop` stores a
proposition. PyLeaner does not use a Python or hard-coded class list.
Inherited fields that occur only in an `extends` clause are intentionally not
reported as directly declared fields.

The elaborated structure name is resolved against the environment delta for
the current command. Exact namespace-qualified, `_root_`, and private/mangled
candidates are preferred; a hygienic fallback is accepted only when the delta
contains one unique new structure. Existing same-named declarations are never
silently associated with the source command.

If structure elaboration fails, syntax-derived `name`, `typeText`,
`binderKind`, and `range` are still returned. `projectionName`, `isClass`,
`isProp`, and `className` remain `null`.

## class

The declaration keyword is identified as:

```text
class
```

The actual declaration node usually resolves to:

```text
Lean.Parser.Command.classInductive
```

Extraction rules share the same branch as `structure` and `inductive` in `extractDeclarations`:

| Field | Source |
| --- | --- |
| `name` | First identifier |
| `paramsText` | `optDeclSig.args[0]` |
| `params` | `extractStructuredParams` traverses binders from the actual declaration node |
| `typeText` | Currently always `none` |
| `bodyText` | Structure-field-style body extraction path |
| `bodyRange` | Exact source span matching `bodyText`, excluding `where` but including comments |
| `fields` | Same structured direct-field extraction as `structure` |

Note: `getActualDeclaration` already accepts `classInductive`, but `extractBodyFromStructFields` currently only lists `structure` and `inductive` in its explicit kind check. Therefore class body extraction relies on the current syntax tree falling into a compatible structure, or `classInductive` needs to be explicitly added to the field extraction check in the future.

## inductive

Actual declaration node:

```text
Lean.Parser.Command.inductive
```

Structure relied on by current code:

```text
inductive
├─ "inductive"
├─ declId
├─ optDeclSig
├─ universe / index / optional parts
└─ constructors part
```

Extraction rules:

| Field | Source |
| --- | --- |
| `name` | First identifier |
| `paramsText` | `optDeclSig.args[0]` |
| `params` | `extractStructuredParams` traverses binders from the actual declaration node |
| `typeText` | Currently always `none` |
| `bodyText` | `extractBodyFromStructFields` extracts from the constructors region |

The current implementation also reads `args[4]` of the actual declaration node as the constructors region, and strips a leading `where`.

## Helper Extraction Functions

| Function | Purpose |
| --- | --- |
| `extractParamsFromOptDeclSig` | Extracts parameter text (`paramsText`) from `optDeclSig.args[0]`; used for `def` / `abbrev` / `structure` / `class` / `inductive` |
| `extractTypeFromOptDeclSig` | Extracts type from `optDeclSig.args[1]`; used for `def` / `abbrev` |
| `extractParamsFromDeclSig` | Extracts parameter text (`paramsText`) from `declSig.args[0]`; used for `theorem` / `lemma` / `instance` |
| `extractTypeFromDeclSig` | Extracts proposition or type from `declSig.args[1]`; used for `theorem` / `lemma` / `instance` |
| `extractStructuredParams` | Extracts structured parameters (`params : Array ParamInfo`) by traversing binders from the actual declaration node |
| `extractBinderParamInfo` | Extracts `ParamInfo` from a single binder node |
| `extractExplicitOrImplicitBinderInfo` | Handles `explicitBinder` / `implicitBinder` / `strictImplicitBinder` |
| `extractInstBinderInfo` | Handles `instBinder`, extracting instance parameters |
| `extractTypeText` | Handles optDeclSig type extraction specifically for `definition` / `abbrev` / `example` |
| `extractBodyText` | Extracts `declValSimple` / `declValEqns` |
| `extractBodyFromWhereStructInst` | Extracts `whereStructInst`; ordinary `:=` instances fall back to `extractBodyText` |
| `extractBodyFromStructFields` | Extracts field or constructor regions for structure / inductive |
| `extractBodyRange` | Returns the syntax range corresponding to `bodyText` when available |
| `extractParsedStructureFields` | Expands direct structure/class binders into source field records |
| `extractElaboratedStructureFields` | Adds projection, class, and proposition metadata from the command snapshot environment |

## Relationship with Error Detection

Declaration syntax tree extraction and error detection are two independent paths. Current command-level errors only use the official diagnostics from `doc.diagnosticsRef` and map them back to the command range via LSP range. This attribution is intentionally best-effort: `DeclarationInfo.hasError` and `errorMessage` are not guaranteed to contain every document error. The Python `extract_declarations` response therefore also exposes the complete top-level `publishDiagnostics` list from the same `didChange`; correctness-sensitive callers should use that list. Field source extraction is based on syntax ranges, while optional field semantics comes from projection declarations in the snapshot environment. A malformed structure therefore returns diagnostics and partial source fields instead of failing the RPC request.
