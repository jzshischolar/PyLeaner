import Mathlib
import LeanLspExtension

lemma l_ok (n : Nat) : n = n := by
  rfl

theorem t_ok (n : Nat) : n = n := by
  exact l_ok n
