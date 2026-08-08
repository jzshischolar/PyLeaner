import Lean.Server.FileWorker
import Lean.Server.Requests
import Lean.Syntax
import Lean.Data.PersistentArray

/-!
Compatibility wrappers for Lean server and core APIs used by the RPC
extension.  Keep version-sensitive implementation details here so that the
RPC handlers themselves only depend on stable, project-owned operations.
-/

namespace LeanLspExtension.Compat

open Lean.Server
open Lean.Server.RequestM

/-- Canonical source range used by Lean syntax in Lean 4.32. -/
abbrev SourceRange := Lean.Syntax.Range

/-- Test whether two canonical source ranges overlap. -/
def rangesOverlap
    (first second : SourceRange)
    (includeFirstStop := false)
    (includeSecondStop := false) : Bool :=
  Lean.Syntax.Range.overlaps first second includeFirstStop includeSecondStop

/-- Collect the current official diagnostics after the reporter has caught up. -/
def collectDocumentDiagnostics
    (doc : Lean.Server.FileWorker.EditableDocument) :
    RequestM (Array Lean.Widget.InteractiveDiagnostic) := do
  let _ ← doc.reporter.wait
  let diagnostics ← doc.collectCurrentDiagnostics
  return Lean.PersistentArray.toArray diagnostics

/-- Trim ASCII whitespace and materialize the resulting slice as a string. -/
@[inline] def trim (value : String) : String :=
  value.trimAscii.toString

/-- Trim leading ASCII whitespace and materialize the resulting slice. -/
@[inline] def trimStart (value : String) : String :=
  value.trimAsciiStart.toString

/-- Drop characters and materialize the resulting slice as a string. -/
@[inline] def drop (value : String) (count : Nat) : String :=
  (value.drop count).toString

/-- Take characters and materialize the resulting slice as a string. -/
@[inline] def take (value : String) (count : Nat) : String :=
  (value.take count).toString

end LeanLspExtension.Compat
