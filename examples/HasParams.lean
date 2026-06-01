import LeanLspExtension


-- Test cases for parameter detection
-- No parameters
def foo : Nat := 1

-- With parameters
def bar (x : Nat) : Nat := x

-- Theorem without parameters
theorem t1 : True := by trivial

-- Theorem with parameters
theorem t2 (n : Nat) : n = n := by rfl

-- Multiple parameters
def add (x : Nat) (y : Nat) : Nat := x + y

-- Implicit parameters
def implicit {α : Type} (x : α) : α := x

-- Instance parameters
def instanceParam [Add α] (x y : α) : α := x + y

-- Lemma without parameters
lemma simple : True := by trivial

-- Lemma with parameters
lemma withParam (n : Nat) : n + 0 = n := by rfl

-- Example without parameters
example : True := by trivial

-- Example with parameters
example (n : Nat) : n + 1 > n := by sorry
