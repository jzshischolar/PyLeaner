import Lean
import Lean.Data.Lsp.Basic
import Lean.Data.Lsp.Utf16
import Lean.Server.Rpc
import Lean.Server.Requests
import Lean.Server.FileWorker
import Lean.Server.Snapshots
import Lean.Parser.Command
import Lean.Syntax
import LeanLspExtension.Protocol

namespace LeanLspExtension

open Lean.Lsp
open Lean.Server
open Lean.Server.RequestM

/-- Get the source range used to attribute diagnostics to a command. -/
def getCommandRange? (stx : Lean.Syntax) : Option String.Range :=
  match stx.getRangeWithTrailing? (canonicalOnly := true) with
  | some range => some range
  | none => stx.getRange?

/-- Convert an official LSP diagnostic range back to the document UTF-8 range. -/
def diagnosticUtf8Range (text : Lean.FileMap) (diag : Lean.Widget.InteractiveDiagnostic) : String.Range := {
  start := text.lspPosToUtf8Pos diag.range.start,
  stop := text.lspPosToUtf8Pos diag.range.«end»
}

/-- Collect official Lean LSP error diagnostics belonging to a command. -/
partial def collectCommandErrorMessages
    (stx : Lean.Syntax) (text : Lean.FileMap)
    (interactiveDiagnostics : Array Lean.Widget.InteractiveDiagnostic) :
    RequestM (Array String) := do
  let commandRange? := getCommandRange? stx
  let mut errorMessages : Array String := #[]
  for diag in interactiveDiagnostics do
    if diag.severity? == some Lean.Lsp.DiagnosticSeverity.error then
      let includeDiag := match commandRange? with
      | some commandRange =>
        commandRange.overlaps (diagnosticUtf8Range text diag)
          (includeFirstStop := true) (includeSecondStop := true)
      | none => true
      if includeDiag then
        errorMessages := errorMessages.push (Lean.Widget.InteractiveDiagnostic.toDiagnostic diag).message
  return errorMessages

/-- Read diagnostics reported by the official Lean LSP reporter. -/
def collectDocumentDiagnostics (doc : Lean.Server.FileWorker.EditableDocument) : RequestM (Array Lean.Widget.InteractiveDiagnostic) := do
  let _ ← doc.reporter.wait
  doc.diagnosticsRef.get

/-- Helper: Recursively find the first atom -/
partial def findFirstAtom (stx : Lean.Syntax) : String :=
  if stx.isAtom then
    stx.getAtomVal
  else
    -- Use ifNode to check if it is a node
    Lean.Syntax.ifNode stx
      (fun node =>
        let args := node.getArgs
        if args.size > 0 then
          let firstArg := args[0]!
          let result := findFirstAtom firstArg
          if result == "" && args.size > 1 then
            -- If first child did not contain an atom, try the second
            findFirstAtom (args[1]!)
          else
            result
        else "")
      (fun _ => "")

/-- Helper: Find the first ident (identifier) node and return its name -/
partial def findFirstIdent (stx : Lean.Syntax) : Option String :=
  if stx.isIdent then
    some (toString stx)
  else if stx.isAtom then
    -- Atom nodes are not idents, skip
    none
  else
    -- Recursively search child nodes
    Lean.Syntax.ifNode stx
      (fun node =>
        let args := node.getArgs
        -- Use foldl to find the first ident
        args.foldl (fun acc arg =>
          match acc with
          | some _ => acc  -- Already found, return
          | none => findFirstIdent arg
        ) none)
      (fun _ => none)

/-- Get the actual declaration node from a declaration wrapper. Standard
    top-level declarations are wrapped in `Lean.Parser.Command.declaration`.
    Mathlib `lemma` is a macro command whose theorem-shaped declaration is arg 1. -/
partial def getActualDeclaration (stx : Lean.Syntax) : Option Lean.Syntax :=
  stx.ifNode
    (fun n =>
      let kind := n.getKind
      if kind == ``Lean.Parser.Command.declaration then
        let args := n.getArgs
        if args.size > 1 then some (args[1]!) else none
      else if kind.toString == "lemma" then
        let args := n.getArgs
        if args.size > 1 then some (args[1]!) else none
      else if kind == ``Lean.Parser.Command.definition ||
              kind == ``Lean.Parser.Command.structure ||
              kind == ``Lean.Parser.Command.inductive ||
              kind == ``Lean.Parser.Command.classInductive ||
              kind == ``Lean.Parser.Command.theorem ||
              kind == ``Lean.Parser.Command.instance ||
              kind == ``Lean.Parser.Command.abbrev ||
              kind == ``Lean.Parser.Command.example then
        some stx
      else
        none)
    (fun _ => none)

/-- Helper function to get the leading keyword from a syntax node, specifically for declaration types.
    Skips declaration modifiers by inspecting the actual declaration parser node.
    `instance` is special: its first atom may be `local` or `scoped`. -/
partial def getDeclarationKeyword (stx : Lean.Syntax) : RequestM String := do
  match getActualDeclaration stx with
  | none => pure ""
  | some targetStx =>
    let kind := targetStx.getKind
    if kind == ``Lean.Parser.Command.instance then pure "instance"
    else pure (findFirstAtom targetStx)

/-- Helper function to identify declaration kind from syntax structure -/
partial def getDeclarationKindFromSyntax (stx : Lean.Syntax) : RequestM (Option String) := do
  let keyword ← getDeclarationKeyword stx
  if keyword == "def" then pure (some "def")
  else if keyword == "theorem" then pure (some "theorem")
  else if keyword == "lemma" then pure (some "lemma")
  else if keyword == "example" then pure (some "example")
  else if keyword == "structure" then pure (some "structure")
  else if keyword == "class" then pure (some "class")
  else if keyword == "inductive" then pure (some "inductive")
  else if keyword == "abbrev" then pure (some "abbrev")
  else if keyword == "instance" then pure (some "instance")
  else if keyword == "axiom" then pure (some "axiom")
  else if keyword == "opaque" then pure (some "opaque")
  else pure none

/-- Normalized declaration modifiers in source order.  This is deliberately
    syntax-generic: callers can reason about Lean modifiers without PyLeaner
    learning any downstream template policy.  Attributes are represented by
    the stable tag `attribute`; their contents remain available in `fullText`. -/
partial def extractDeclarationModifiers (stx : Lean.Syntax) : Array String :=
  let accepted := #[
    "private", "public", "protected", "meta", "noncomputable", "unsafe",
    "partial", "nonrec", "local", "scoped"
  ]
  let rec collect (node : Lean.Syntax) (acc : Array String) : Array String :=
    if node.getKind == ``Lean.Parser.Term.attributes then
      acc.push "attribute"
    else if node.isAtom then
      let atom := node.getAtomVal.trim
      if accepted.contains atom then acc.push atom else acc
    else if node.isIdent then
      acc
    else
      node.ifNode
        (fun n => n.getArgs.foldl (fun result child => collect child result) acc)
        (fun _ => acc)
  let raw := stx.ifNode
    (fun n =>
      if n.getKind == ``Lean.Parser.Command.declaration then
        let args := n.getArgs
        let fromOuter := if args.size > 0 then collect args[0]! #[] else #[]
        let fromActual := if args.size > 1 then collect args[1]! #[] else #[]
        fromOuter ++ fromActual
      else
        collect stx #[])
    (fun _ => #[])
  raw.foldl (fun result modifier =>
    if result.contains modifier then result else result.push modifier) #[]

/-- Helper: Check if a syntax node represents a binder (parameter) -/
partial def isBinderNode (stx : Lean.Syntax) : Bool :=
  -- Check if this node is a binder by examining its kind
  let kind := stx.getKind
  -- Known binder kinds in Lean 4
  kind == ``Lean.Parser.Term.explicitBinder ||
  kind == ``Lean.Parser.Term.implicitBinder ||
  kind == ``Lean.Parser.Term.strictImplicitBinder ||
  kind == ``Lean.Parser.Term.instBinder

/-- Helper: Find all binder nodes in syntax tree -/
partial def findBinderNodes (stx : Lean.Syntax) : Array Lean.Syntax :=
  let rec collect (node : Lean.Syntax) (acc : Array Lean.Syntax) : Array Lean.Syntax :=
    if node.isAtom || node.isIdent then
      acc
    else
      node.ifNode
        (fun n =>
          let args := n.getArgs
          let newAcc := args.foldl (fun innerAcc arg =>
            if isBinderNode arg then
              innerAcc.push arg
            else
              collect arg innerAcc
          ) acc
          newAcc)
        (fun _ => acc)
  collect stx #[]

/-- Helper: Convert binder node kind to string tag -/
def binderKindToString (stx : Lean.Syntax) : String :=
  let kind := stx.getKind
  if kind == ``Lean.Parser.Term.explicitBinder then "explicit"
  else if kind == ``Lean.Parser.Term.implicitBinder then "implicit"
  else if kind == ``Lean.Parser.Term.strictImplicitBinder then "strictImplicit"
  else if kind == ``Lean.Parser.Term.instBinder then "instance"
  else "explicit"

/-- Helper: Strip leading backtick from identifier names produced by toString.
    toString on a Syntax.ident returns "`x", we want just "x".
-/
def stripIdentPrefix (name : String) : String :=
  if name.startsWith "`" then name.drop 1 else name

/-- Helper: Extract type text from the binderType child node of a binder.
    Actual binder structure (from debug dump):
      explicitBinder: args = [atom "(", namesNode, typeNode, defaultNode?, atom ")"]
      implicitBinder: args = [atom "{", namesNode, typeNode, atom "}"]
    where typeNode is a null node containing [atom ":", ...type subtree...]
    The type node is at args[2].
-/
def extractBinderTypeText (binderArgs : Array Lean.Syntax) (text : Lean.FileMap) : Option String :=
  -- args[2] is the type annotation node (null node with ": type")
  if binderArgs.size < 3 then none
  else
    let typeNode := binderArgs[2]!
    typeNode.ifNode
      (fun n =>
        let innerArgs := n.getArgs
        -- innerArgs = [atom ":", ...type expression...]
        -- Skip the ":" atom, extract range of remaining children
        if innerArgs.size < 2 then none
        else
          let typeExpr := innerArgs.drop 1
          let firstRange := typeExpr[0]!.getRange?
          let lastRange := typeExpr[typeExpr.size - 1]!.getRange?
          match firstRange, lastRange with
          | some r1, some r2 =>
            let rawText := String.Pos.Raw.extract text.source r1.start r2.stop |>.trim
            if rawText.isEmpty then none else some rawText
          | _, _ => none)
      (fun _ => none)

/-- Helper: Collect identifier names from the names node of a binder.
    The names node (args[1]) is a null node containing ident/hole children.
-/
def collectBinderNames (namesNode : Lean.Syntax) : Array (Option String) :=
  namesNode.ifNode
    (fun n =>
      n.getArgs.foldl (fun acc arg =>
        if arg.isIdent then
          acc.push (some (stripIdentPrefix (toString arg)))
        else if arg.isAtom && arg.getAtomVal == "_" then
          acc.push none
        else acc
      ) #[])
    (fun _ => #[])

/-- Helper: Extract ParamInfo from explicit/implicit binder.
    Binder structure: [bracket, namesNode, typeNode, ...]
    namesNode = null node with ident children
    typeNode = null node with [":", typeExpr...]
-/
partial def extractExplicitOrImplicitBinderInfo (binderNode : Lean.Syntax) (text : Lean.FileMap) (binderKindStr : String) : Array ParamInfo :=
  binderNode.ifNode
    (fun node =>
      let args := node.getArgs
      -- args[1] = names node, args[2] = type node
      if args.size < 2 then #[]
      else
        let names := collectBinderNames args[1]!
        let typeText := extractBinderTypeText args text
        if names.isEmpty then #[]
        else names.map fun nameOpt => {
          name := nameOpt
          type := typeText
          binderKind := binderKindStr
        })
    (fun _ => #[])

/-- Helper: Extract ParamInfo from instance binder.
    Instance binder structure (from debug dump):
      args = [atom "[", optIdentNode, classExprNode, atom "]"]
    optIdentNode: empty null node when no name, or null node with ident + ":" when named
    classExprNode: the class expression (e.g. Lean.Parser.Term.app for "Add α")
-/
partial def extractInstBinderInfo (binderNode : Lean.Syntax) (text : Lean.FileMap) (binderKindStr : String) : Array ParamInfo :=
  binderNode.ifNode
    (fun node =>
      let args := node.getArgs
      if args.size < 3 then #[]
      else
        -- args[1] = optIdent result (null node, may be empty or contain ident + ":")
        let optIdentNode := args[1]!
        -- args[2] = class expression
        let classExpr := args[2]!
        -- Check if optIdentNode contains a named instance
        let paramName : Option String :=
          optIdentNode.ifNode
            (fun n =>
              -- Look for an ident child (if named, e.g. [inst : Add α])
              n.getArgs.findSome? fun arg =>
                if arg.isIdent then some (stripIdentPrefix (toString arg)) else none)
            (fun _ => none)
        -- Type text from the class expression node
        let typeText : Option String := match classExpr.getRange? with
          | some range =>
            let rawText := String.Pos.Raw.extract text.source range.start range.stop |>.trim
            if rawText.isEmpty then none else some rawText
          | none => none

        #[{ name := paramName, type := typeText, binderKind := binderKindStr }])
    (fun _ => #[])

/-- Helper: Extract ParamInfo array from a single binder node -/
partial def extractBinderParamInfo (binder : Lean.Syntax) (text : Lean.FileMap) : Array ParamInfo :=
  let kind := binder.getKind
  let binderKindStr := binderKindToString binder

  if kind == ``Lean.Parser.Term.instBinder then
    extractInstBinderInfo binder text binderKindStr
  else
    -- explicitBinder, implicitBinder, strictImplicitBinder all share the same structure
    extractExplicitOrImplicitBinderInfo binder text binderKindStr


/-- Helper: Simplified parameter text extraction -
    Uses syntax tree ranges to extract parameter text -/
partial def extractParametersTextSimple (stx : Lean.Syntax) (text : Lean.FileMap) : Option String :=
  -- Find binder nodes using pure syntax tree traversal
  let binders := findBinderNodes stx

  if binders.isEmpty then
    none
  else
    -- Extract and concatenate binder texts
    let texts := binders.filterMap fun binder =>
      match binder.getRange? with
      | some range => some (String.Pos.Raw.extract text.source range.start range.stop)
      | none => none

    if texts.isEmpty then none
    else some (String.intercalate " " texts.toList)

/-- Helper: Extract structured parameter info from a declaration's signature.
    Works uniformly across all declaration kinds by finding declSig/optDeclSig.
-/
partial def extractStructuredParams (stx : Lean.Syntax) (text : Lean.FileMap) : Option (Array ParamInfo) :=
  match getActualDeclaration stx with
  | none => none
  | some declStx =>
    -- Find the signature node (either declSig or optDeclSig)
    let rec findSigNode (node : Lean.Syntax) : Option Lean.Syntax :=
      node.ifNode
        (fun n =>
          let kind := n.getKind
          if kind == ``Lean.Parser.Command.declSig ||
             kind == ``Lean.Parser.Command.optDeclSig then
            some node
          else
            n.getArgs.findSome? fun arg => findSigNode arg)
        (fun _ => none)

    match findSigNode declStx with
    | none => none
    | some sigNode =>
      sigNode.ifNode
        (fun n =>
          let args := n.getArgs
          if args.size < 1 then none
          else
            -- args[0] is the null node containing the binder sequence
            let bindersContainer := args[0]!
            let binders := findBinderNodes bindersContainer
            if binders.isEmpty then none
            else
              let paramInfos := binders.foldl (fun acc binder =>
                acc ++ extractBinderParamInfo binder text
              ) #[]
              if paramInfos.isEmpty then none
              else some paramInfos)
        (fun _ => none)

/-- Helper: Extract return type text using optDeclSig structure. -/
partial def extractTypeText (stx : Lean.Syntax) (text : Lean.FileMap) : Option String :=
  match getActualDeclaration stx with
  | none => none
  | some declStx =>
    declStx.ifNode
      (fun node =>
        let kind := node.getKind
        if kind == ``Lean.Parser.Command.definition ||
           kind == ``Lean.Parser.Command.abbrev ||
           kind == ``Lean.Parser.Command.example then
          let args := node.getArgs
          let sigIdx := if kind == ``Lean.Parser.Command.example then 1 else 2
          if args.size > sigIdx then
            let optDeclSig := args[sigIdx]!
            optDeclSig.ifNode
              (fun sigNode =>
                let sigArgs := sigNode.getArgs
                if sigArgs.size > 1 then
                  let returnTypePart := sigArgs[1]!
                  match returnTypePart.getRange? with
                  | some range =>
                    let rawText := String.Pos.Raw.extract text.source range.start range.stop
                    let stripped := rawText.trimLeft |>.drop 1 |>.trimLeft
                    if stripped.isEmpty then none else some stripped
                  | none => none
                else none)
              (fun _ => none)
          else none
        else none)
      (fun _ => none)

/-- Helper: Find the declaration value node for simple or equation-style bodies. -/
partial def findDeclValNode? (stx : Lean.Syntax) : Option Lean.Syntax :=
  stx.ifNode
    (fun n =>
      let kind := n.getKind
      if kind == ``Lean.Parser.Command.declValSimple ||
         kind == ``Lean.Parser.Command.declValEqns then
        some stx
      else
        n.getArgs.findSome? fun arg => findDeclValNode? arg)
    (fun _ => none)

/-- Helper: Strip the leading `:=` marker from a `declValSimple` node text. -/
def stripDeclValSimplePrefix (bodyText : String) : String :=
  let trimmed := bodyText.trimLeft
  if trimmed.startsWith ":=" then
    trimmed.drop 2 |>.trimLeft
  else
    trimmed

/-- Helper: Extract body text using declaration-level body nodes. -/
partial def extractBodyText (stx : Lean.Syntax) (text : Lean.FileMap) : Option String :=
  match getActualDeclaration stx with
  | none => none
  | some declStx =>
    match findDeclValNode? declStx with
    | none => none
    | some bodyNode =>
      match bodyNode.getRange? with
      | none => none
      | some range =>
        let rawText := String.Pos.Raw.extract text.source range.start range.stop
        let bodyText := if bodyNode.getKind == ``Lean.Parser.Command.declValSimple then
          stripDeclValSimplePrefix rawText
        else
          rawText.trim
        if bodyText.isEmpty then none else some bodyText

/-- Helper: Extract parameters from optDeclSig (for structure/inductive/def) -/
partial def extractParamsFromOptDeclSig (stx : Lean.Syntax) (text : Lean.FileMap) : Option String :=
  -- Get the actual declaration node (unwrap declaration wrapper)
  match getActualDeclaration stx with
  | none => none
  | some declStx =>
    -- Find the optDeclSig node in the actual declaration
    let rec findOptDeclSig (node : Lean.Syntax) : Option Lean.Syntax :=
      node.ifNode
        (fun n =>
          let kind := n.getKind
          if kind == ``Lean.Parser.Command.optDeclSig then
            some node
          else
            let args := n.getArgs
            args.findSome? fun arg => findOptDeclSig arg)
        (fun _ => none)

    match findOptDeclSig declStx with
    | none => none
    | some optDeclSig =>
      optDeclSig.ifNode
        (fun n =>
          let args := n.getArgs
          if args.size > 0 then
            let paramsPart := args[0]!
            match paramsPart.getRange? with
            | some range =>
              let rawText := String.Pos.Raw.extract text.source range.start range.stop
              let trimmed := rawText.trim
              if trimmed.isEmpty then none else some trimmed
            | none => none
          else none)
        (fun _ => none)

/-- Helper: Extract type from optDeclSig (for def/abbrev) -/
partial def extractTypeFromOptDeclSig (stx : Lean.Syntax) (text : Lean.FileMap) : Option String :=
  -- Get the actual declaration node (unwrap declaration wrapper)
  match getActualDeclaration stx with
  | none => none
  | some declStx =>
    -- Find the optDeclSig node and extract type from [1]
    let rec findOptDeclSig (node : Lean.Syntax) : Option Lean.Syntax :=
      node.ifNode
        (fun n =>
          let kind := n.getKind
          if kind == ``Lean.Parser.Command.optDeclSig then
            some node
          else
            let args := n.getArgs
            args.findSome? fun arg => findOptDeclSig arg)
        (fun _ => none)

    match findOptDeclSig declStx with
    | none => none
    | some optDeclSig =>
      optDeclSig.ifNode
        (fun n =>
          let args := n.getArgs
          if args.size > 1 then
            let typePart := args[1]!
            match typePart.getRange? with
            | some range =>
              let rawText := String.Pos.Raw.extract text.source range.start range.stop
              let trimmed := rawText.trim
              if trimmed.isEmpty || trimmed == ":" then none
              else
                let withoutColon := if trimmed.startsWith ":" then trimmed.drop 1 |>.trim else trimmed
                if withoutColon.isEmpty then none else some withoutColon
            | none => none
          else none)
        (fun _ => none)

/-- Helper: Extract parameters from declSig (for theorem/instance) -/
partial def extractParamsFromDeclSig (stx : Lean.Syntax) (text : Lean.FileMap) : Option String :=
  -- Get the actual declaration node (unwrap declaration wrapper)
  match getActualDeclaration stx with
  | none => none
  | some declStx =>
    -- Find the declSig node
    let rec findDeclSig (node : Lean.Syntax) : Option Lean.Syntax :=
      node.ifNode
        (fun n =>
          let kind := n.getKind
          if kind == ``Lean.Parser.Command.declSig then
            some node
          else
            let args := n.getArgs
            args.findSome? fun arg => findDeclSig arg)
        (fun _ => none)

    match findDeclSig declStx with
    | none => none
    | some declSig =>
      declSig.ifNode
        (fun n =>
          let args := n.getArgs
          if args.size > 0 then
            let paramsPart := args[0]!
            match paramsPart.getRange? with
            | some range =>
              let rawText := String.Pos.Raw.extract text.source range.start range.stop
              let trimmed := rawText.trim
              if trimmed.isEmpty then none else some trimmed
            | none => none
          else none)
        (fun _ => none)

/-- Helper: Extract type from declSig (for theorem/instance) -/
partial def extractTypeFromDeclSig (stx : Lean.Syntax) (text : Lean.FileMap) : Option String :=
  -- Get the actual declaration node (unwrap declaration wrapper)
  match getActualDeclaration stx with
  | none => none
  | some declStx =>
    -- Find the declSig node
    let rec findDeclSig (node : Lean.Syntax) : Option Lean.Syntax :=
      node.ifNode
        (fun n =>
          let kind := n.getKind
          if kind == ``Lean.Parser.Command.declSig then
            some node
          else
            let args := n.getArgs
            args.findSome? fun arg => findDeclSig arg)
        (fun _ => none)

    match findDeclSig declStx with
    | none => none
    | some declSig =>
      declSig.ifNode
        (fun n =>
          let args := n.getArgs
          if args.size > 1 then
            let typePart := args[1]!
            match typePart.getRange? with
            | some range =>
              let rawText := String.Pos.Raw.extract text.source range.start range.stop
              let trimmed := rawText.trim
              if trimmed.isEmpty || trimmed == ":" then none
              else
                let withoutColon := if trimmed.startsWith ":" then trimmed.drop 1 |>.trim else trimmed
                if withoutColon.isEmpty then none else some withoutColon
            | none => none
          else none)
        (fun _ => none)

/-- Helper: Extract body text from whereStructInst (for instance) -/
partial def extractBodyFromWhereStructInst (stx : Lean.Syntax) (text : Lean.FileMap) : Option String :=
  -- Get the actual declaration node (unwrap declaration wrapper)
  match getActualDeclaration stx with
  | none => none
  | some declStx =>
    -- Find the whereStructInst node
    let rec findWhereStructInst (node : Lean.Syntax) : Option Lean.Syntax :=
      node.ifNode
        (fun n =>
          let kind := n.getKind
          if kind == ``Lean.Parser.Command.whereStructInst then
            some node
          else
            let args := n.getArgs
            args.findSome? fun arg => findWhereStructInst arg)
        (fun _ => none)

    match findWhereStructInst declStx with
    | none => none
    | some whereStructInst =>
      match whereStructInst.getRange? with
      | some range =>
        let rawText := String.Pos.Raw.extract text.source range.start range.stop
        let trimmed := rawText.trim
        if trimmed.isEmpty then none else some trimmed
      | none => none

/-- Helper: Extract body text from structFields (for structure) -/
partial def extractBodyFromStructFields (stx : Lean.Syntax) (text : Lean.FileMap) : Option String :=
  -- Get the actual declaration node (unwrap declaration wrapper)
  match getActualDeclaration stx with
  | none => none
  | some declStx =>
    -- For structure/inductive, the "body" is the fields/constructors (arg [4])
    declStx.ifNode
      (fun n =>
        let kind := n.getKind
        if kind == ``Lean.Parser.Command.structure || kind == ``Lean.Parser.Command.inductive then
          let args := n.getArgs
          if args.size > 4 then
            let fieldsPart := args[4]!
            -- First try to get range directly from fieldsPart
            match fieldsPart.getRange? with
            | some range =>
              let rawText := String.Pos.Raw.extract text.source range.start range.stop
              let trimmed := rawText.trim
              -- Strip "where" keyword if present at the start
              let hasWhere := trimmed.take 5 == "where"
              let withoutWhere := if hasWhere then
                let after := trimmed.drop 5 |>.trimLeft
                if after.isEmpty then "" else after
              else
                trimmed
              if withoutWhere.isEmpty || withoutWhere == "where" then none else some withoutWhere
            | none =>
              -- If fieldsPart has no range, try to extract from its children
              fieldsPart.ifNode
                (fun fn =>
                  let fargs := fn.getArgs
                  if fargs.isEmpty then
                    none
                  else
                    let first := fargs[0]!
                    let last := fargs[fargs.size - 1]!
                    match first.getRange?, last.getRange? with
                    | some r1, some r2 =>
                      let rawText := String.Pos.Raw.extract text.source r1.start r2.stop
                      let trimmed := rawText.trim
                      if trimmed.isEmpty then none else some trimmed
                    | _, _ => none)
                (fun _ => none)
          else
            none
        else
          none)
      (fun _ => none)

/-- Find the first syntax node with the requested parser kind. -/
private partial def findSyntaxNodeByKind? (stx : Lean.Syntax) (kind : Lean.SyntaxNodeKind) : Option Lean.Syntax :=
  if stx.getKind == kind then
    some stx
  else
    stx.ifNode
      (fun n => n.getArgs.findSome? fun arg => findSyntaxNodeByKind? arg kind)
      (fun _ => none)

/-- Convert a syntax node's canonical UTF-8 range to an LSP range. -/
private def syntaxLspRange? (stx : Lean.Syntax) (text : Lean.FileMap) : Option Lean.Lsp.Range :=
  stx.getRange?.map text.utf8RangeToLspRange

/-- Reproduce `extractBodyFromStructFields`' whitespace/`where` trimming while
    retaining offsets into the original document. This also covers comments
    and an optional custom constructor before the first field. -/
private def extractStructureBodyRange (declStx : Lean.Syntax) (text : Lean.FileMap) : Option Lean.Lsp.Range :=
  declStx.ifNode
    (fun n =>
      if n.getKind != ``Lean.Parser.Command.structure || n.getArgs.size ≤ 4 then none
      else
        match n.getArgs[4]!.getRange? with
        | none => none
        | some range =>
          let raw : Substring := ⟨text.source, range.start, range.stop⟩
          let trimmed := raw.trim
          let body := if trimmed.toString.startsWith "where" then
            (trimmed.drop 5).trimLeft
          else
            trimmed
          if body.isEmpty then none
          else
            some <| text.utf8RangeToLspRange { start := body.startPos, stop := body.stopPos })
    (fun _ => none)

/-- Reproduce `stripDeclValSimplePrefix` while retaining offsets into the
    original document. In particular, comments between `:=` and the first term
    syntax are part of `bodyText` and therefore part of `bodyRange`. -/
private def extractDeclValSimpleBodyRange
    (bodyNode : Lean.Syntax) (text : Lean.FileMap) : Option Lean.Lsp.Range :=
  match bodyNode.getRange? with
  | none => none
  | some range =>
    let raw : Substring := ⟨text.source, range.start, range.stop⟩
    let trimmed := raw.trimLeft
    let body := if trimmed.toString.startsWith ":=" then
      (trimmed.drop 2).trimLeft
    else
      trimmed
    if body.isEmpty then none
    else
      some <| text.utf8RangeToLspRange { start := body.startPos, stop := body.stopPos }

/-- Extract the type portion from a `declSig` or `optDeclSig` node. -/
private def extractTypeFromSignatureNode (sig : Lean.Syntax) (text : Lean.FileMap) : Option String :=
  sig.ifNode
    (fun n =>
      let args := n.getArgs
      if args.size < 2 then none
      else
        match args[1]!.getRange? with
        | none => none
        | some range =>
          let rawText := String.Pos.Raw.extract text.source range.start range.stop |>.trim
          let withoutColon := if rawText.startsWith ":" then rawText.drop 1 |>.trim else rawText
          if withoutColon.isEmpty then none else some withoutColon)
    (fun _ => none)

/-- Direct structure field syntax before environment-based enrichment. -/
private structure ParsedStructureField where
  name : String
  typeText : String
  binderKind : String
  range : Lean.Lsp.Range

/-- Recursively collect direct `structure`/`class` field binder nodes in source order. -/
private partial def findStructureFieldNodes (stx : Lean.Syntax) : Array Lean.Syntax :=
  let kind := stx.getKind
  if kind == ``Lean.Parser.Command.structExplicitBinder ||
     kind == ``Lean.Parser.Command.structImplicitBinder ||
     kind == ``Lean.Parser.Command.structInstBinder ||
     kind == ``Lean.Parser.Command.structSimpleBinder then
    #[stx]
  else
    stx.ifNode
      (fun n => n.getArgs.foldl (fun acc arg => acc ++ findStructureFieldNodes arg) #[])
      (fun _ => #[])

/-- Extract one or more source fields from a structure binder. Grouped fields
    such as `(x y : α)` become two entries sharing the binder's source range. -/
private def extractParsedStructureFieldsFromBinder
    (binder : Lean.Syntax) (text : Lean.FileMap) : Array ParsedStructureField :=
  let kind := binder.getKind
  binder.ifNode
    (fun n =>
      let args := n.getArgs
      let binderKind :=
        if kind == ``Lean.Parser.Command.structImplicitBinder then "implicit"
        else if kind == ``Lean.Parser.Command.structInstBinder then "instance"
        else "explicit"
      let names : Array (Option String) :=
        if kind == ``Lean.Parser.Command.structSimpleBinder then
          if args.size > 1 && args[1]!.isIdent then
            #[some (stripIdentPrefix (toString args[1]!))]
          else #[]
        else if args.size > 2 then
          collectBinderNames args[2]!
        else #[]
      let sig? :=
        if kind == ``Lean.Parser.Command.structSimpleBinder then
          if args.size > 2 then some args[2]! else none
        else if args.size > 3 then some args[3]!
        else none
      let typeText := sig?.bind (extractTypeFromSignatureNode · text) |>.getD ""
      match syntaxLspRange? binder text with
      | none => #[]
      | some range => names.filterMap fun name? => name?.map fun name => {
          name := name
          typeText := typeText
          binderKind := binderKind
          range := range
        })
    (fun _ => #[])

/-- Extract direct source fields from `structure` and structure-style `class`
    declarations. This path is syntax-only and remains available when elaboration fails. -/
private def extractParsedStructureFields (stx : Lean.Syntax) (text : Lean.FileMap) : Array ParsedStructureField :=
  match getActualDeclaration stx with
  | none => #[]
  | some declStx =>
    declStx.ifNode
      (fun n =>
        if n.getKind != ``Lean.Parser.Command.structure then #[]
        else match findSyntaxNodeByKind? declStx ``Lean.Parser.Command.structFields with
          | none => #[]
          | some fieldsNode =>
            (findStructureFieldNodes fieldsNode).foldl (fun fields binder =>
              fields ++ extractParsedStructureFieldsFromBinder binder text) #[])
      (fun _ => #[])

/-- Whether this command is backed by Lean's structure parser (including the
    ordinary `class` syntax, but excluding `class inductive`). -/
private def isStructureLikeDeclaration (stx : Lean.Syntax) : Bool :=
  match getActualDeclaration stx with
  | some declStx => declStx.getKind == ``Lean.Parser.Command.structure
  | none => false

/-- Semantic metadata recovered from a generated structure projection. -/
private structure ElaboratedStructureField where
  name : String
  projectionName : String
  isClass : Bool
  isProp : Bool
  isPropType : Bool
  className : Option String

/-- Resolve the structure actually introduced by this command by comparing its
    post-command environment with the preceding command snapshot. This handles
    private/mangled and `_root_` names without ever falling back to an unrelated
    same-named declaration already present in the environment. -/
private def resolveIntroducedStructureName?
    (snap : Lean.Server.Snapshots.Snapshot) (previousEnv? : Option Lean.Environment)
    (sourceName : String) : Option Lean.Name := Id.run do
  let env := snap.env
  let currNamespace := snap.cmdState.scopes.head?.map (·.currNamespace) |>.getD Lean.Name.anonymous
  let userName := if sourceName.startsWith "_root_." then
    (sourceName.drop 7).toName
  else
    currNamespace ++ sourceName.toName
  let exactCandidates := #[userName, Lean.mkPrivateName env userName]
  let isNewStructure := fun candidate =>
    let existedBefore := previousEnv?.any fun previousEnv => previousEnv.contains candidate
    !existedBefore && (Lean.getStructureInfo? env candidate).isSome
  let exactMatches := exactCandidates.filter isNewStructure
  if exactMatches.size == 1 then
    return exactMatches[0]?
  else if exactMatches.size > 1 then
    return none

  -- Conservative fallback for elaborators that introduce a hygienic name not
  -- expressible from the source identifier. Only a unique environment delta is
  -- accepted; an existing same-named declaration is never considered.
  let mut candidates : Array Lean.Name := #[]
  for (candidate, _) in env.constants.map₂ do
    if isNewStructure candidate then
      candidates := candidates.push candidate
  if candidates.size == 1 then candidates[0]? else none

/-- Use projection types in Lean's environment to classify direct fields. Any
    field without an elaborated projection is simply omitted from this semantic map. -/
private def extractElaboratedStructureFields
    (snap : Lean.Server.Snapshots.Snapshot) (previousEnv? : Option Lean.Environment)
    (sourceName : String) : RequestM (Array ElaboratedStructureField) := do
  let some structName := resolveIntroducedStructureName? snap previousEnv? sourceName
    | return #[]
  runTermElabM snap do
    let env ← Lean.getEnv
    let some structInfo := Lean.getStructureInfo? env structName
      | return #[]
    let mut result := #[]
    for fieldName in structInfo.fieldNames do
      let some fieldInfo := Lean.getFieldInfo? env structName fieldName
        | continue
      let some constantInfo := env.find? fieldInfo.projFn
        | continue
      let some projectionInfo := env.getProjectionFnInfo? fieldInfo.projFn
        | continue
      let projectionArity := projectionInfo.numParams + 1
      let semantic? ← try
        Lean.Meta.forallBoundedTelescope constantInfo.type (some projectionArity) fun args resultType => do
          if args.size != projectionArity then
            pure none
          else
            let reducedType ← Lean.Meta.whnf resultType
            let className? := match reducedType.getAppFn with
              | .const className _ =>
                if Lean.isClass env className then some className else none
              | _ => none
            let isProp ← Lean.Meta.isProp resultType
            let isPropType := match reducedType with
              | .sort .zero => true
              | _ => false
            pure <| some (className?, isProp, isPropType)
      catch _ =>
        pure none
      if let some (className?, isProp, isPropType) := semantic? then
        result := result.push {
          name := toString fieldInfo.fieldName
          projectionName := toString fieldInfo.projFn
          isClass := className?.isSome
          isProp := isProp
          isPropType := isPropType
          className := className?.map toString
        }
    return result

/-- Combine syntax-derived fields with optional environment semantics. -/
private def extractStructureFields
    (snap : Lean.Server.Snapshots.Snapshot) (previousEnv? : Option Lean.Environment)
    (stx : Lean.Syntax) (text : Lean.FileMap) (semanticSourceName? : Option String) :
    RequestM (Array LeanLspExtension.StructureFieldInfo) := do
  let parsed := extractParsedStructureFields stx text
  let elaborated ← match semanticSourceName? with
    | some sourceName => extractElaboratedStructureFields snap previousEnv? sourceName
    | none => pure #[]
  return parsed.map fun field =>
    let semantic? := elaborated.find? (·.name == field.name)
    {
      name := field.name
      typeText := field.typeText
      binderKind := field.binderKind
      range := field.range
      projectionName := semantic?.map (·.projectionName)
      isClass := semantic?.map (·.isClass)
      isProp := semantic?.map (·.isProp)
      isPropType := semantic?.map (·.isPropType)
      className := semantic?.bind (·.className)
    }

/-- Exact source range corresponding to `bodyText`, when the parser exposes a
    dedicated body node. -/
private partial def extractBodyRange
    (kind : String) (stx : Lean.Syntax) (text : Lean.FileMap) : Option Lean.Lsp.Range :=
  match getActualDeclaration stx with
  | none => none
  | some declStx =>
    if kind == "structure" || kind == "class" then
      extractStructureBodyRange declStx text
    else if kind == "inductive" then
      declStx.ifNode
        (fun n => if n.getArgs.size > 4 then syntaxLspRange? n.getArgs[4]! text else none)
        (fun _ => none)
    else if kind == "instance" then
      match findSyntaxNodeByKind? declStx ``Lean.Parser.Command.whereStructInst with
      | some bodyNode => syntaxLspRange? bodyNode text
      | none =>
        match findDeclValNode? declStx with
        | some bodyNode =>
          if bodyNode.getKind == ``Lean.Parser.Command.declValSimple then
            extractDeclValSimpleBodyRange bodyNode text
          else
            syntaxLspRange? bodyNode text
        | none => none
    else
      match findDeclValNode? declStx with
      | none => none
      | some bodyNode =>
        if bodyNode.getKind == ``Lean.Parser.Command.declValSimple then
          extractDeclValSimpleBodyRange bodyNode text
        else
          syntaxLspRange? bodyNode text


/-- Helper: Simple name extraction (without parameters).
    Extracts declaration name from the syntax tree.
    Returns none for nameless declarations like example.
-/
partial def extractDeclarationNameSimple (stx : Lean.Syntax) : RequestM (Option String) := do
  -- First check if it is an example (nameless declaration)
  let keyword ← getDeclarationKeyword stx
  if keyword == "example" then
    pure none
  else
    let name? := match getActualDeclaration stx with
      | none => none
      | some declStx =>
        if declStx.getKind == ``Lean.Parser.Command.instance then
          -- instance args are: attrKind, `instance`, priority, optional declId,
          -- declSig, declVal. Do not mistake the first binder for an unnamed
          -- instance's declaration name.
          declStx.ifNode
            (fun n => if n.getArgs.size > 3 then findFirstIdent n.getArgs[3]! else none)
            (fun _ => none)
        else
          findFirstIdent declStx
    -- For named declarations, strip the rendering prefix from the source ident.
    match name? with
    | some name =>
      -- Strip the Lean identifier backtick prefix
      if name.startsWith "`" then
        pure (some (name.drop 1))
      else
        pure (some name)
    | none => pure none


/-- Helper: Check if a declaration has syntax errors by examining its structure -/
partial def checkDeclarationSyntaxError (kind : String) (stx : Lean.Syntax) (_text : Lean.FileMap) : Bool × Option String :=
  match getActualDeclaration stx with
  | none => (false, none)
  | some declStx =>
    declStx.ifNode
      (fun n =>
        let args := n.getArgs
        match kind with
        | "structure" => if args.size < 6 then
            -- structure should have 6 args, if less, likely has syntax error
            (true, some s!"Structure has incomplete syntax (only {args.size} arguments, expected 6)")
          else
            (false, none)
        | "inductive" => if args.size < 7 then
            -- inductive should have 7 args
            (true, some s!"Inductive has incomplete syntax (only {args.size} arguments, expected 7)")
          else
            (false, none)
        | _ => (false, none))
      (fun _ => (false, none))



/-================================================Methods================================================-/

/-- RPC handler for lean/ping -/
@[server_rpc_method]
def ping (_params : PingParams) : RequestM (RequestTask PingResponse) := do
  let response : PingResponse := { message := "pong"}
  return RequestTask.pure response

/-- RPC handler for lean/echo -/
@[server_rpc_method]
def echo (params : EchoParams) : RequestM (RequestTask EchoResponse) := do
  let response : EchoResponse := {
    success := true
    uri := params.textDocument.uri
    message := params.message
  }
  return RequestTask.pure response


/-- RPC handler for lean/extractDeclarations - extracts complete declaration information -/
@[server_rpc_method]
def extractDeclarations (_params : ExtractDeclarationsParams) : RequestM (RequestTask ExtractDeclarationsResult) := do
  let doc ← readDoc
  let interactiveDiagnostics ← collectDocumentDiagnostics doc

  -- Get all command snapshots
  let snapsTask := doc.cmdSnaps.waitAll

  -- Process snapshots using mapTaskCheap
  mapTaskCheap snapsTask fun (snaps,_) => do
    -- Extract complete declaration information from snapshots
    let mut decls := #[]
    let mut previousEnv? : Option Lean.Environment := none
    for snap in snaps do
      let stx := snap.stx

      -- First check if it is a declaration
      if let some kind ← getDeclarationKindFromSyntax stx then
        -- Get the syntax range
        let synRange := stx.getRange?.getD (⟨0,0⟩: String.Range)
        let lspRange := doc.meta.text.utf8RangeToLspRange synRange

        -- Extract declaration text
        let startPos := synRange.start
        let endPos := synRange.stop
        let fullText := String.Pos.Raw.extract doc.meta.text.source startPos endPos

        -- Extract declaration name
        let name ← extractDeclarationNameSimple stx

        -- Use different extraction methods based on declaration type
        let (paramsText, typeText, bodyText) :=
        if kind == "structure" || kind == "inductive" || kind == "class" then
          -- For structure/inductive/class: params from optDeclSig[0], body from [4]
          let params := extractParamsFromOptDeclSig stx doc.meta.text
          let body := extractBodyFromStructFields stx doc.meta.text
          (params, none, body)
        else if kind == "theorem" || kind == "lemma" || kind == "instance" then
          -- For theorem/lemma/instance: params and type from declSig
          let params := extractParamsFromDeclSig stx doc.meta.text
          let type := extractTypeFromDeclSig stx doc.meta.text
          -- Instances support both `where` and ordinary `:=` declaration bodies.
          let body := if kind == "instance" then
            match extractBodyFromWhereStructInst stx doc.meta.text with
            | some body => some body
            | none => extractBodyText stx doc.meta.text
          else
            extractBodyText stx doc.meta.text
          (params, type, body)
        else if kind == "def" || kind == "abbrev" then
          -- For def/abbrev: use optDeclSig methods
          let params := extractParamsFromOptDeclSig stx doc.meta.text
          let type := extractTypeFromOptDeclSig stx doc.meta.text
          let body := extractBodyText stx doc.meta.text
          (params, type, body)
        else
          -- For example and others: use existing methods
          let params := extractParametersTextSimple stx doc.meta.text
          let type := extractTypeText stx doc.meta.text
          let body := extractBodyText stx doc.meta.text
          (params, type, body)

        -- Check errors: 1) Official Lean LSP diagnostics 2) Syntax structure completeness
        let errorMessages ← collectCommandErrorMessages stx doc.meta.text interactiveDiagnostics

        let (hasError, errorMessage) := if errorMessages.isEmpty then
          -- No runtime errors, check syntax errors
          -- checkDeclarationSyntaxError kind stx doc.meta.text
          (false, some "")
        else
          -- Has runtime errors, build error message
          (true, some (String.intercalate "; " errorMessages.toList))

        -- Construct declaration info
        let structuredParams := extractStructuredParams stx doc.meta.text
        let bodyRange := extractBodyRange kind stx doc.meta.text
        let fields ← if isStructureLikeDeclaration stx then
          let semanticSourceName? := if hasError then none else name
          pure <| some (← extractStructureFields snap previousEnv? stx doc.meta.text semanticSourceName?)
        else
          pure none
        let declInfo : LeanLspExtension.DeclarationInfo := {
          kind := kind,
          name := name,
          modifiers := some (extractDeclarationModifiers stx),
          paramsText := paramsText,
          params := structuredParams,
          typeText := typeText,
          bodyText := bodyText,
          bodyRange := bodyRange,
          fields := fields,
          fullText := fullText,
          range := lspRange,
          hasError := hasError,
          errorMessage := errorMessage
        }
        decls := decls.push declInfo

      previousEnv? := some snap.env

    return { success := true, decls := decls }







/-================================================Debug methods=================================================================-/
/-- RPC handler for lean/parseDocument - parses document and returns command structure -/
@[server_rpc_method]
def parseDocument (_params : ParseParams) : RequestM (RequestTask ParseResult) := do
  let doc ← readDoc
  let interactiveDiagnostics ← collectDocumentDiagnostics doc

  -- Get all command snapshots
  let snapsTask := doc.cmdSnaps.waitAll

  -- Process snapshots using mapTaskCheap
  mapTaskCheap snapsTask fun (snaps,_) => do
    -- Extract command information from snapshots
    let mut commands : Array CommandInfo := #[]
    for snap in snaps do
      -- Get the command syntax from the snapshot
      let stx := snap.stx
      let synRange := stx.getRange?.getD (⟨0,0⟩: String.Range)
      let lspRange := doc.meta.text.utf8RangeToLspRange synRange

      -- Extract the text for this command using String.Pos.Raw.extract
      let startPos := synRange.start
      let endPos := synRange.stop
      let text := String.Pos.Raw.extract doc.meta.text.source startPos endPos

      -- Get syntax kind
      let kindName := toString stx.getKind

      -- Collect compile errors reported by official Lean LSP diagnostics
      let errorMessages ← collectCommandErrorMessages stx doc.meta.text interactiveDiagnostics

      commands := commands.push {
        syntaxKind := kindName,
        fullText := text,
        range := lspRange,
        hasError := not errorMessages.isEmpty,
        errorMessages := errorMessages
      }

    return { success := true, commands := commands }


/-- RPC handler for lean/testDeclarationKind - identifies declaration kinds in document -/
@[server_rpc_method]
def testDeclarationKind (_params : TestDeclarationKindParams) : RequestM (RequestTask TestDeclarationKindResult) := do
  let doc ← readDoc

  -- Get all command snapshots
  let snapsTask := doc.cmdSnaps.waitAll

  -- Process snapshots using mapTaskCheap
  mapTaskCheap snapsTask fun (snaps,_) => do
    -- Extract declaration kinds from snapshots
    let mut kinds := #[]
    for snap in snaps do
      let stx := snap.stx
      if let some kind ← getDeclarationKindFromSyntax stx then
        kinds := kinds.push kind

    return TestDeclarationKindResult.mk true kinds



/-- RPC handler for lean/testDeclarationName - extracts declaration names (simple cases) -/
@[server_rpc_method]
def testDeclarationName (_params : TestDeclarationNameParams) : RequestM (RequestTask TestDeclarationNameResult) := do
  let doc ← readDoc

  -- Get all command snapshots
  let snapsTask := doc.cmdSnaps.waitAll

  -- Process snapshots using mapTaskCheap
  mapTaskCheap snapsTask fun (snaps,_) => do
    -- Extract declaration names from snapshots
    let mut names := #[]
    for snap in snaps do
      let stx := snap.stx
      -- First check if it is a declaration
      if let some kind ← getDeclarationKindFromSyntax stx then
        -- Extract name
        let name ← extractDeclarationNameSimple stx
        let nameInfo : LeanLspExtension.DeclarationNameInfo := { kind := kind, name := name }
        names := names.push nameInfo

    return { success := true, names := names }

/-- Helper: Simplified version - check for binder-like patterns -/
partial def hasParametersSimple (stx : Lean.Syntax) : Bool :=
  -- Look for the presence of binder syntax in the declaration
  -- This is a pure syntax tree traversal
  let rec check (node : Lean.Syntax) : Bool :=
    if node.isAtom || node.isIdent then
      false
    else
      node.ifNode
        (fun n =>
          let args := n.getArgs
          -- Check if any arg looks like a binder
          args.any fun arg =>
            let k := arg.getKind
            -- Check for known binder kinds
            if k == ``Lean.Parser.Term.explicitBinder then true
            else if k == ``Lean.Parser.Term.implicitBinder then true
            else if k == ``Lean.Parser.Term.strictImplicitBinder then true
            else if k == ``Lean.Parser.Term.instBinder then true
            -- Recursively check
            else check arg)
        (fun _ => false)
  check stx

/-- Helper: Determine if a declaration has parameters -/
partial def hasParameters (stx : Lean.Syntax) : Bool :=
  -- Use the simplified check
  hasParametersSimple stx

/-- RPC handler for lean/debugDocument - reads current document content -/
@[server_rpc_method]
def debugDocument (_params : DebugDocumentParams) : RequestM (RequestTask DebugDocumentResult) := do
  let doc ← readDoc
  let text := doc.meta.text.source
  let prefixLen := min 100 text.length
  return RequestTask.pure {
    textLength := text.length,
    textPrefix := text.take prefixLen
  }


/-- RPC handler for lean/debugSnapshotInfo - debug snapshot structure -/
@[server_rpc_method]
def debugSnapshotInfo (_params : DebugSyntaxTreeParams) : RequestM (RequestTask DebugSyntaxTreeResult) := do
  let doc ← readDoc
  let interactiveDiagnostics ← collectDocumentDiagnostics doc

  let snapsTask := doc.cmdSnaps.waitAll

  mapTaskCheap snapsTask fun (snaps,_) => do
    let mut results := #[]
    for snap in snaps do
      let stx := snap.stx

      -- Get keyword
      let keyword ← getDeclarationKeyword stx

      -- Check official Lean LSP diagnostics for errors
      let errorMessages ← collectCommandErrorMessages stx doc.meta.text interactiveDiagnostics

      let errorInfo := if errorMessages.isEmpty then
        s!"keyword='{keyword}', no errors"
      else
        s!"keyword='{keyword}', errors: {String.intercalate "; " errorMessages.toList}"

      results := results.push errorInfo

    let combined := String.intercalate "\n" results.toList
    return { success := true, syntaxInfo := combined }

/-- RPC handler for lean/debugBodyFields - debug body extraction for structures -/
@[server_rpc_method]
def debugBodyFields (_params : DebugSyntaxTreeParams) : RequestM (RequestTask DebugSyntaxTreeResult) := do
  let doc ← readDoc

  let snapsTask := doc.cmdSnaps.waitAll

  mapTaskCheap snapsTask fun (snaps,_) => do
    let mut results := #[]
    for snap in snaps do
      let stx := snap.stx

      -- Show all declarations with their kind
      if let some kind ← getDeclarationKindFromSyntax stx then
        let name ← extractDeclarationNameSimple stx
        let nameStr := name.getD "(none)"

        -- Get the actual declaration node
        let debugInfo := match getActualDeclaration stx with
        | none => s!"{nameStr}: kind={kind}, getActualDeclaration returned none"
        | some declStx =>
          declStx.ifNode
            (fun n =>
              let args := n.getArgs
              let numArgs := args.size
              let kindName := toString n.getKind
              -- Show optDeclSig structure (arg [2])
              let optDeclSigInfo := if numArgs > 2 then
                let optDeclSig := args[2]!
                optDeclSig.ifNode
                  (fun sigNode =>
                    let sigArgs := sigNode.getArgs
                    let sigNum := sigArgs.size
                    -- Show args[0] (params) and args[1] (return type)
                    let paramsInfo := if sigNum > 0 then
                      let paramsPart := sigArgs[0]!
                      match paramsPart.getRange? with
                      | some range =>
                        let rawText := String.Pos.Raw.extract doc.meta.text.source range.start range.stop
                        s!"params[0] range: '{rawText}'"
                      | none => s!"params[0]: no range"
                    else "params[0]: doesn't exist"
                    let typeInfo := if sigNum > 1 then
                      let typePart := sigArgs[1]!
                      match typePart.getRange? with
                      | some range =>
                        let rawText := String.Pos.Raw.extract doc.meta.text.source range.start range.stop
                        s!"type[1] range: '{rawText}'"
                      | none => s!"type[1]: no range"
                    else "type[1]: doesn't exist"
                    s!"optDeclSig has {sigNum} args, {paramsInfo}, {typeInfo}")
                  (fun _ => s!"optDeclSig not a node")
              else "no optDeclSig"
              -- Show fields structure (arg [4])
              let fieldsInfo := if numArgs > 4 then
                let fieldsPart := args[4]!
                match fieldsPart.getRange? with
                | some range =>
                  let rawText := String.Pos.Raw.extract doc.meta.text.source range.start range.stop
                  s!"fields[4] range: '{rawText}'"
                | none => s!"fields[4]: no range"
              else "no fields[4]"
              s!"{nameStr}: {kindName} has {numArgs} args, {optDeclSigInfo}, {fieldsInfo}")
            (fun _ => s!"{nameStr}: actual decl not a node")

        results := results.push debugInfo

    let combined := String.intercalate "\n" results.toList
    return { success := true, syntaxInfo := combined }

/-- RPC handler for lean/testBodyFields - tests body extraction for structures -/
@[server_rpc_method]
def testBodyFields (_params : TestHasParamsParams) : RequestM (RequestTask TestBodyTextResult) := do
  let doc ← readDoc

  let snapsTask := doc.cmdSnaps.waitAll

  mapTaskCheap snapsTask fun (snaps,_) => do
    let mut results := #[]
    for snap in snaps do
      let stx := snap.stx

      if let some kind ← getDeclarationKindFromSyntax stx then
        let name ← extractDeclarationNameSimple stx

        -- Test extractBodyFromStructFields
        let bodyText := extractBodyFromStructFields stx doc.meta.text

        let info : LeanLspExtension.BodyTextInfo := {
          kind := kind,
          name := name,
          bodyText := bodyText
        }
        results := results.push info

    return { success := true, declarations := results }


/-- RPC handler for lean/debugAllSnapshots - debug all snapshots -/
@[server_rpc_method]
def debugAllSnapshots (_params : DebugSyntaxTreeParams) : RequestM (RequestTask DebugSyntaxTreeResult) := do
  let doc ← readDoc

  let snapsTask := doc.cmdSnaps.waitAll

  mapTaskCheap snapsTask fun (snaps,_) => do
    let mut results := #[]
    for snap in snaps do
      let stx := snap.stx

      -- Show all snapshots to diagnose
      let keyword ← getDeclarationKeyword stx
      let kind ← getDeclarationKindFromSyntax stx
      let kindStr := match kind with | some k => k | none => "(none)"

      let debugInfo := s!"keyword={keyword}, kind={kindStr}"
      results := results.push debugInfo

    let combined := String.intercalate "\n" results.toList
    return { success := true, syntaxInfo := combined }

/-- RPC handler for lean/testHasParams - checks if declarations have parameters -/
@[server_rpc_method]
def testHasParams (_params : TestHasParamsParams) : RequestM (RequestTask TestHasParamsResult) := do
  let doc ← readDoc

  -- Get all command snapshots
  let snapsTask := doc.cmdSnaps.waitAll

  -- Process snapshots using mapTaskCheap
  mapTaskCheap snapsTask fun (snaps,_) => do
    let mut results := #[]
    -- Extract parameter presence information from snapshots
    for snap in snaps do
      let stx := snap.stx

      -- First check if it is a declaration
      if let some kind ← getDeclarationKindFromSyntax stx then
        -- Extract declaration name
        let name ← extractDeclarationNameSimple stx

        -- Check if it has parameters
        let hasParams := hasParameters stx

        -- Construct result info
        let info : LeanLspExtension.HasParamsInfo := {
          kind := kind,
          name := name,
          hasParams := hasParams
        }
        results := results.push info

    return { success := true, declarations := results }

/-- RPC handler for lean/testParamsText - extracts parameters text -/
@[server_rpc_method]
def testParamsText (_params : TestParamsTextParams) : RequestM (RequestTask TestParamsTextResult) := do
  let doc ← readDoc

  -- Get all command snapshots
  let snapsTask := doc.cmdSnaps.waitAll

  -- Process snapshots using mapTaskCheap
  mapTaskCheap snapsTask fun (snaps,_) => do
    let mut results := #[]
    -- Extract parameters text from snapshots
    for snap in snaps do
      let stx := snap.stx

      -- First check if it is a declaration
      if let some kind ← getDeclarationKindFromSyntax stx then
        -- Extract declaration name
        let name ← extractDeclarationNameSimple stx

        -- Extract parameters text
        let paramsText := extractParametersTextSimple stx doc.meta.text

        -- Construct result info
        let info : LeanLspExtension.ParamsTextInfo := {
          kind := kind,
          name := name,
          paramsText := paramsText
        }
        results := results.push info

    return { success := true, declarations := results }

/-- RPC handler for lean/testTypeText - extracts return type text -/
@[server_rpc_method]
def testTypeText (_params : TestTypeTextParams) : RequestM (RequestTask TestTypeTextResult) := do
  let doc ← readDoc

  -- Get all command snapshots
  let snapsTask := doc.cmdSnaps.waitAll

  -- Process snapshots using mapTaskCheap
  mapTaskCheap snapsTask fun (snaps,_) => do
    let mut results := #[]
    -- Extract return type text from snapshots
    for snap in snaps do
      let stx := snap.stx

      -- First check if it is a declaration
      if let some kind ← getDeclarationKindFromSyntax stx then
        -- Extract declaration name
        let name ← extractDeclarationNameSimple stx

        -- Extract return type text
        let typeText := extractTypeText stx doc.meta.text

        -- Construct result info
        let info : LeanLspExtension.TypeTextInfo := {
          kind := kind,
          name := name,
          typeText := typeText
        }
        results := results.push info

    return { success := true, declarations := results }

/-- RPC handler for lean/testBodyText - extracts body text -/
@[server_rpc_method]
def testBodyText (_params : TestBodyTextParams) : RequestM (RequestTask TestBodyTextResult) := do
  let doc ← readDoc

  -- Get all command snapshots
  let snapsTask := doc.cmdSnaps.waitAll

  -- Process snapshots using mapTaskCheap
  mapTaskCheap snapsTask fun (snaps,_) => do
    -- Extract body text from snapshots
    let mut results := #[]
    for snap in snaps do
      let stx := snap.stx

      -- First check if it is a declaration
      if let some kind ← getDeclarationKindFromSyntax stx then
        -- Extract declaration name
        let name ← extractDeclarationNameSimple stx

        -- Extract body text
        let bodyText := extractBodyText stx doc.meta.text

        -- Construct result info
        let info : LeanLspExtension.BodyTextInfo := {
          kind := kind,
          name := name,
          bodyText := bodyText
        }
        results := results.push info

    return { success := true, declarations := results }

/-- Helper: Make indent string -/
partial def makeIndent (n : Nat) : String :=
  if n == 0 then ""
  else if n == 1 then "  "
  else "  " ++ makeIndent (n - 1)

/-- Helper: Simple syntax tree debug - only show top level structure -/
partial def debugSyntaxTreeInfo (stx : Lean.Syntax) (depth : Nat := 0) (maxDepth : Nat := 4) : String :=
  if depth >= maxDepth then
    let indent := makeIndent depth
    s!"{indent}..."
  else if stx.isAtom then
    let indent := makeIndent depth
    s!"{indent}Atom: '{stx.getAtomVal}'"
  else if stx.isIdent then
    let indent := makeIndent depth
    s!"{indent}Ident: '{toString stx}'"
  else
    let indent := makeIndent depth
    stx.ifNode
      (fun n =>
        let kind := toString n.getKind
        let numArgs := n.getNumArgs
        let header := s!"{indent}Node kind={kind} args={numArgs}"
        -- Only show first 2 args to limit output
        if depth >= 2 || numArgs == 0 then
          header
        else
          let numArgsToShow := min 2 numArgs
          let childList := Array.range numArgsToShow |>.toList.map fun i =>
            let arg := n.getArgs[i]!
            debugSyntaxTreeInfo arg (depth + 1) maxDepth
          let more := if numArgs > numArgsToShow then [s!"{indent}  ... ({numArgs - numArgsToShow} more)"] else []
          String.intercalate "\n" (header :: childList ++ more))
      (fun _ => s!"{indent}Unknown")

/-- Helper: Get syntax node info string -/
def getSyntaxNodeInfo (stx : Lean.Syntax) (text : Lean.FileMap) : String :=
  match stx.getRange? with
  | some range =>
    let snippet := String.Pos.Raw.extract text.source range.start range.stop
    let maxLength := min 30 snippet.length
    let preview := snippet.take maxLength
    if snippet.length > 30 then s!"{preview}..." else s!"{preview}"
  | none => "(no range)"

/-- Helper: Debug def/structure/inductive node structure -/
partial def debugDefStructure (stx : Lean.Syntax) (text : Lean.FileMap) : String :=
  let rec process (node : Lean.Syntax) (acc : Array String) : Array String :=
    if node.isAtom || node.isIdent then
      acc
    else
      node.ifNode
        (fun n =>
          let kind := n.getKind
          let isTarget := kind == ``Lean.Parser.Command.declaration ||
                          kind == ``Lean.Parser.Command.definition ||
                          kind == ``Lean.Parser.Command.abbrev ||
                          kind == ``Lean.Parser.Command.structure ||
                          kind == ``Lean.Parser.Command.inductive ||
                          kind == ``Lean.Parser.Command.classInductive ||
                          kind == ``Lean.Parser.Command.instance ||
                          kind == ``Lean.Parser.Command.theorem ||
                          kind == ``Lean.Parser.Command.opaque ||
                          kind == ``Lean.Parser.Command.axiom ||
                          kind == ``Lean.Parser.Command.example ||
                          kind == ``Lean.Parser.Command.coinductive
          if isTarget then
            -- If this is a declaration wrapper, process the actual declaration
            let args := if kind == ``Lean.Parser.Command.declaration then
              let declArgs := n.getArgs
              if declArgs.size > 1 then
                declArgs[1]!.ifNode (fun an => an.getArgs) (fun _ => n.getArgs)
              else
                n.getArgs
            else
              n.getArgs

            let numArgs := args.size
            let kindName := if kind == ``Lean.Parser.Command.declaration then
              let declArgs := n.getArgs
              if declArgs.size > 1 then
                declArgs[1]!.ifNode (fun an => toString an.getKind) (fun _ => toString kind)
              else
                toString kind
            else
              toString kind
            let header := s!"\n=== {kindName} node has {numArgs} arguments ==="
            let rec buildInfos (idx : Nat) : List String :=
              if idx >= numArgs then
                []
              else
                let arg := args[idx]!
                let info := getSyntaxNodeInfo arg text
                let argKind := toString arg.getKind
                -- For optDeclSig and declSig, show internal structure
                let extra := if (idx == 2 && argKind == "Lean.Parser.Command.optDeclSig") || argKind == "Lean.Parser.Command.declSig" then
                  let subArgs := arg.ifNode (fun sn => sn.getArgs) (fun _ => #[])
                  let subNum := subArgs.size
                  let nodeName := if argKind == "Lean.Parser.Command.optDeclSig" then "optDeclSig" else "declSig"
                  let subHeader := s!"\n    {nodeName} has {subNum} sub-arguments:"
                  let rec buildSubInfos (sidx : Nat) : List String :=
                    if sidx >= subNum then []
                    else
                      let sarg := subArgs[sidx]!
                      let sinfo := getSyntaxNodeInfo sarg text
                      let skind := toString sarg.getKind
                      s!"      [{sidx}] kind={skind} | {sinfo}" :: buildSubInfos (sidx + 1)
                  subHeader :: buildSubInfos 0
                else
                  []
                s!"  [{idx}] kind={argKind} | {info}" :: extra ++ buildInfos (idx + 1)
            acc ++ (header :: buildInfos 0)
          else
            let args := n.getArgs
            let rec processArgs (idx : Nat) : Array String :=
              if idx >= args.size then
                acc
              else
                process (args[idx]!) (processArgs (idx + 1))
            processArgs 0)
        (fun _ => acc)
  process stx #[] |>.toList |> String.intercalate "\n"

/-- RPC handler for lean/debugSyntaxTree - debug syntax tree structure -/
@[server_rpc_method]
def debugSyntaxTree (_params : DebugSyntaxTreeParams) : RequestM (RequestTask DebugSyntaxTreeResult) := do
  let doc ← readDoc

  -- Get all command snapshots
  let snapsTask := doc.cmdSnaps.waitAll

  -- Process snapshots using mapTaskCheap
  mapTaskCheap snapsTask fun (snaps,_) => do
    let mut results := #[]
    for snap in snaps do
      let stx := snap.stx
      -- Use debugDefStructure to show def node structure
      let info := debugDefStructure stx doc.meta.text
      results := results.push info

    let combined := String.intercalate "\n\n---\n\n" results.toList
    return { success := true, syntaxInfo := combined }


/-- Debug helper: describe a single syntax node as a string (pure function) -/
partial def describeSyntaxNode (stx : Lean.Syntax) : String :=
  if stx.isIdent then s!"ident:{toString stx}"
  else if stx.isAtom then s!"atom:\"{stx.getAtomVal}\""
  else stx.ifNode (fun n =>
    let innerDescs := n.getArgs.mapIdx fun _j c => describeSyntaxNode c
    s!"node(kind={n.getKind}, {n.getArgs.size} children)[{String.intercalate ", " innerDescs.toList}]"
  ) (fun _ => "?")

/-- Debug: Dump binder node structure for binders in each declaration -/
@[server_rpc_method]
def debugBinderStructure (_params : DebugBinderStructureParams) : RequestM (RequestTask DebugBinderStructureResult) := do
  let doc ← readDoc
  let snapsTask := doc.cmdSnaps.waitAll
  mapTaskCheap snapsTask fun (snaps, _) => do
    let mut results : Array String := #[]
    for snap in snaps do
      let stx := snap.stx
      if let some kind ← getDeclarationKindFromSyntax stx then
        let name ← extractDeclarationNameSimple stx
        let nameStr := name.getD "(none)"
        let binders := findBinderNodes stx
        if binders.isEmpty then
          results := results.push s!"{kind} {nameStr}: no binders"
        else
          let binderDumps := binders.map fun binder =>
            let bKind := binderKindToString binder
            binder.ifNode
              (fun n =>
                let childDescs := n.getArgs.mapIdx fun _i arg =>
                  s!"  [{_i}] {describeSyntaxNode arg}"
                s!"  binder({bKind}):\n{String.intercalate "\n" childDescs.toList}")
              (fun _ => "  binder: not a node?")
          results := results.push s!"{kind} {nameStr}: {binders.size} binder(s)\n{String.intercalate "\n" binderDumps.toList}"
    let combined := String.intercalate "\n\n" results.toList
    return { success := true, binderInfo := combined }

end LeanLspExtension
