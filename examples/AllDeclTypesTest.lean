import LeanLspExtension

-- def
def add (x : Nat) : Nat := x

-- abbrev
abbrev NatId := Nat → Nat

-- structure
structure Point where
  x : Nat
  y : Nat

-- inductive
inductive Color where
  | red

-- class
class MyClass where
  myMethod : Nat → Nat

-- class with parameters
class MyMonad (m : Type → Type) where
  pure : α → m α

-- instance
instance : MyClass Nat where
  myMethod := fun x => x

-- instance with parameters
instance [Add α] : OfNat α where
  ofNat n := n + n

-- theorem
theorem add_zero (n : Nat) : n + 0 = n := by rfl

-- lemma
lemma simp_lem : 1 + 1 = 2 := by rfl

-- example
example : 2 + 2 = 4 := by rfl
