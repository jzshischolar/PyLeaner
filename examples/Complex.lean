-- Import the LSP extension module (this loads the custom handlers)
import LeanLspExtension
import Mathlib

-- Complex test file with various declaration types

-- Simple def (no parameters)
def simpleDef : Nat := 42

-- Def with parameters
def add (x : Nat) (y : Nat) : Nat := x + y

-- Def with multiple parameters and explicit type
def complexDef (a : Nat) (b : String) (c : Bool) : Nat :=
  if c then a else a + b.length

-- Structure declaration
structure Point where
  x : Nat
  y : Nat
  deriving Repr

-- Structure with parameters
structure Rectangle (w : Nat) (h : Nat) where
  area : Nat
  perimeter : Nat

-- Inductive type
inductive Tree where
  | leaf : Tree
  | node : Tree -> Tree -> Tree

-- Class declaration
class Monad (m : Type -> Type) where
  pure : {α : Type} -> α -> m α
  bind : {α β : Type} -> m α -> (α -> m β) -> m β

-- Abbrev declaration
abbrev NatList := List Nat

-- Theorem with parameters
theorem add_zero (x : Nat) : x + 0 = x := by
  rw [Nat.add_zero]

-- Lemma with parameters
lemma mul_one (x : Nat) : x * 1 = x := by
  rw [Nat.mul_one, Nat.one_mul]

-- Example (no name)
example : True := by
  trivial

example : 2 + 2 = 4 := by
  rfl
