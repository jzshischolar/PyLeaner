import LeanLspExtension

-- Simple structure
structure Point where
  x : Nat
  y : Nat
  deriving Repr

-- Structure with parameters
structure Pair (α β : Type*) where
  fst : α
  snd : β

-- Simple inductive
inductive Color where
  | red
  | green
  | blue

-- Inductive with parameters
inductive Tree (α : Type) where
  | leaf : Tree α
  | node : α → Tree α → Tree α → Tree α

-- Inductive with mixed syntax
inductive NatList where
  | nil : NatList
  | cons : Nat → NatList → NatList
