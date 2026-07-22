import Lean
import Lean.Data.Lsp.Basic
import Lean.Server.FileSource

namespace LeanLspExtension

open Lean.Lsp

/-- Response for lean/ping request -/
structure PingResponse where
  message : String
  deriving Lean.ToJson, Lean.FromJson

/-- Request parameters for lean/ping -/
structure PingParams where
  deriving Lean.ToJson, Lean.FromJson

/-- Request parameters for lean/echo -/
structure EchoParams where
  textDocument : TextDocumentIdentifier
  message : String
  deriving Lean.ToJson, Lean.FromJson

/-- Response for lean/echo request -/
structure EchoResponse where
  success : Bool
  uri : String
  message : String
  deriving Lean.ToJson, Lean.FromJson

/-- Request parameters for lean/debugDocument -/
structure DebugDocumentParams where
  deriving Lean.ToJson, Lean.FromJson

/-- Response for lean/debugDocument - returns document information -/
structure DebugDocumentResult where
  textLength : Nat
  textPrefix : String
  deriving Lean.ToJson, Lean.FromJson

/-- Request parameters for lean/parseDocument -/
structure ParseParams where
  deriving Lean.ToJson, Lean.FromJson

/-- Information about a single command in the document -/
structure CommandInfo where
  syntaxKind : String
  fullText : String
  range : Lean.Lsp.Range
  hasError : Bool := false
  errorMessages : Array String := #[]
  deriving Lean.ToJson, Lean.FromJson

/-- Response for lean/parseDocument -/
structure ParseResult where
  success : Bool
  commands : Array CommandInfo
  deriving Lean.ToJson, Lean.FromJson

/-- Request parameters for lean/testDeclarationKind -/
structure TestDeclarationKindParams where
  deriving Lean.ToJson, Lean.FromJson

/-- Response for lean/testDeclarationKind - returns identified declaration kinds -/
structure TestDeclarationKindResult where
  success : Bool
  kinds : Array String
  deriving Lean.ToJson, Lean.FromJson

/-- Request parameters for lean/testDeclarationName -/
structure TestDeclarationNameParams where
  deriving Lean.ToJson, Lean.FromJson

/-- Information about a declaration name -/
structure DeclarationNameInfo where
  kind : String
  name : Option String
  deriving Lean.ToJson, Lean.FromJson

/-- Response for lean/testDeclarationName - returns declaration names -/
structure TestDeclarationNameResult where
  success : Bool
  names : Array DeclarationNameInfo
  deriving Lean.ToJson, Lean.FromJson

/-- Structured information about a single parameter -/
structure ParamInfo where
  name : Option String        -- Parameter name, none for anonymous
  type : Option String        -- Type annotation text
  binderKind : String         -- "explicit" | "implicit" | "strictImplicit" | "instance"
  deriving Lean.ToJson, Lean.FromJson

/-- Structured source and elaboration information for a directly declared
    structure/class field.  Semantic fields stay `none` when the declaration
    did not elaborate far enough to create a projection. -/
structure StructureFieldInfo where
  name : String
  typeText : String
  binderKind : String         -- "explicit" | "implicit" | "instance"
  range : Lean.Lsp.Range
  projectionName : Option String := none
  isClass : Option Bool := none
  isProp : Option Bool := none
  isPropType : Option Bool := none
  className : Option String := none
  deriving Lean.ToJson, Lean.FromJson

/-- Complete information about a declaration -/
structure DeclarationInfo where
  kind : String  -- "def", "theorem", "axiom", "opaque", "structure", etc.
  name : Option String  -- example has no name
  paramsText : Option String  -- "(x : Nat) (y : Nat)" (backward compat)
  params : Option (Array ParamInfo)  -- Structured parameter information
  typeText : Option String  -- "Nat", "List α", etc.
  bodyText : Option String  -- "x + y", may contain multiple lines
  bodyRange : Option Lean.Lsp.Range := none  -- Exact source range of bodyText when available
  fields : Option (Array StructureFieldInfo) := none  -- Only structure/class declarations
  fullText : String  -- Complete declaration text
  range : Lean.Lsp.Range  -- Declaration range in document
  hasError : Bool := false  -- Whether this declaration has syntax errors
  errorMessage : Option String := none  -- Error message if hasError is true
  deriving Lean.ToJson, Lean.FromJson

/-- Request parameters for lean/extractDeclarations -/
structure ExtractDeclarationsParams where
  deriving Lean.ToJson, Lean.FromJson

/-- Response for lean/extractDeclarations - returns complete declaration information -/
structure ExtractDeclarationsResult where
  success : Bool
  decls : Array DeclarationInfo
  deriving Lean.ToJson, Lean.FromJson

/-- Request parameters for lean/testHasParams -/
structure TestHasParamsParams where
  deriving Lean.ToJson, Lean.FromJson

/-- Information about whether a declaration has parameters -/
structure HasParamsInfo where
  kind : String
  name : Option String
  hasParams : Bool
  deriving Lean.ToJson, Lean.FromJson

/-- Response for lean/testHasParams - returns parameter presence information -/
structure TestHasParamsResult where
  success : Bool
  declarations : Array HasParamsInfo
  deriving Lean.ToJson, Lean.FromJson

/-- Request parameters for lean/testParamsText -/
structure TestParamsTextParams where
  deriving Lean.ToJson, Lean.FromJson

/-- Information about declaration parameters text -/
structure ParamsTextInfo where
  kind : String
  name : Option String
  paramsText : Option String  -- "(x : Nat) (y : Nat)" or "{α : Type} (xs : List α)"
  deriving Lean.ToJson, Lean.FromJson

/-- Response for lean/testParamsText - returns parameters text -/
structure TestParamsTextResult where
  success : Bool
  declarations : Array ParamsTextInfo
  deriving Lean.ToJson, Lean.FromJson

/-- Request parameters for lean/testTypeText -/
structure TestTypeTextParams where
  deriving Lean.ToJson, Lean.FromJson

/-- Information about declaration return type text -/
structure TypeTextInfo where
  kind : String
  name : Option String
  typeText : Option String  -- "Nat", "List α", "IO Unit", etc.
  deriving Lean.ToJson, Lean.FromJson

/-- Response for lean/testTypeText - returns return type text -/
structure TestTypeTextResult where
  success : Bool
  declarations : Array TypeTextInfo
  deriving Lean.ToJson, Lean.FromJson

/-- Request parameters for lean/testBodyText -/
structure TestBodyTextParams where
  deriving Lean.ToJson, Lean.FromJson

/-- Information about declaration body text -/
structure BodyTextInfo where
  kind : String
  name : Option String
  bodyText : Option String  -- "42", "x + 1", "by trivial", etc.
  deriving Lean.ToJson, Lean.FromJson

/-- Response for lean/testBodyText - returns body text -/
structure TestBodyTextResult where
  success : Bool
  declarations : Array BodyTextInfo
  deriving Lean.ToJson, Lean.FromJson

/-- Request parameters for lean/debugSyntaxTree -/
structure DebugSyntaxTreeParams where
  deriving Lean.ToJson, Lean.FromJson

/-- Response for lean/debugSyntaxTree - returns syntax tree information -/
structure DebugSyntaxTreeResult where
  success : Bool
  syntaxInfo : String
  deriving Lean.ToJson, Lean.FromJson

/-- Request parameters for lean/debugBinderStructure -/
structure DebugBinderStructureParams where
  deriving Lean.ToJson, Lean.FromJson

/-- Response for lean/debugBinderStructure -/
structure DebugBinderStructureResult where
  success : Bool
  binderInfo : String
  deriving Lean.ToJson, Lean.FromJson

end LeanLspExtension
