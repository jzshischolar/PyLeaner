-- Import the LSP extension module (this loads the custom handlers)
import LeanLspExtension

-- Simple test file with Mathlib
def f (x : Nat) : Nat := x + 1

theorem foo : True := by
  trivial


example : True := by
  trivial
