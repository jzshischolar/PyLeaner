import LeanLspExtension

structure Point where
  x : Nat
  y : Nat

structure Pair (α : Type) (β : Type) where
  fst : α
  snd : β

inductive Tree (α : Type) where
  | leaf : Tree α
  | node : α → Tree α → Tree α
