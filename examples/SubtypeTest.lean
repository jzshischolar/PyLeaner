import LeanLspExtension

-- Simple def for comparison
def simpleDef (n : Nat) : Nat := n + 1

-- Subtype declaration
def dependentType (n : Nat) : {m : Nat // m > n} :=
  ⟨n + 1, by simp⟩

-- Another Subtype
def subtype2 (x : Nat) : {y : Nat // y > x} :=
  ⟨x + 1, by linarith⟩

-- For comparison: def with explicit parameter
def withExplicit (n : Nat) (m : Nat) : Nat :=
  n + m
