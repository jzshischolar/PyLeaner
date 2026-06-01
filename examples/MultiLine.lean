import LeanLspExtension
import Mathlib

-- Test cases for multi-line body extraction

-- Multi-line let bindings
def foo : Nat :=
  let x := 1
  let y := 2
  x + y

-- Multi-line match expression
def bar (xs : List Nat) : Nat :=
  match xs with
  | [] => 0
  | x :: xs => x + bar xs

-- Multi-line theorem with tactics
theorem complex : True := by
  intro h
  trivial

-- Nested structure
def baz (n : Nat) : Nat :=
  let x :=
    let y := n + 1
    y * 2
  x + 10

-- Multi-line if-then-else
def testCond (b : Bool) (x y : Nat) : Nat :=
  if b then
    let z := x + y
    z * 2
  else
    let z := x * y
    z + 1

-- Simple case (for comparison)
def simple : Nat := 42

-- Single line with let
def singleLet : Nat := let x := 1; x + 1
