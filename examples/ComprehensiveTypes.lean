import LeanLspExtension
import Mathlib

-- ============================================
-- 基础声明类型
-- ============================================

-- def 声明
def add (x : Nat) (y : Nat) : Nat := x + y

-- theorem 声明
theorem add_comm (a b : Nat) : a + b = b + a := by
  simp [add_comm]

-- lemma 声明
lemma lemma1 (n : Nat) : n + 0 = n := by
  rfl

-- example 声明
example : 1 + 1 = 2 := by
  rfl

-- ============================================
-- 结构体 (structure)
-- ============================================

structure Point where
  x : Nat
  y : Nat
  deriving Repr

structure Pair (α β : Type*) where
  fst : α
  snd : β

-- ============================================
-- 类型类 (class)
-- ============================================

class MyClass where
  myMethod : Nat → Nat

class MyMonad (m : Type → Type) where
  pure : α → m α
  bind : m α → (α → m β) → m β

-- ============================================
-- 归纳类型 (inductive)
-- ============================================

inductive Color where
  | red
  | green
  | blue
  deriving Repr

inductive Tree (α : Type) where
  | leaf : Tree α
  | node : α → Tree α → Tree α → Tree α

inductive NatList where
  | nil : NatList
  | cons : Nat → NatList → NatList

  --归纳类型不能识别函数体

-- ============================================
-- 类型类实例 (instance)
-- ============================================

instance : MyClass Nat where
  myMethod := fun x => x + 1

instance [MyClass α] : MyClass (Array α) where
  myMethod := fun xs => xs.length

instance : Monad Id where
  pure x := x
  bind x f := f x

-- ============================================
-- abbrev 声明
-- ============================================

abbrev NatId := Nat → Nat

abbrev addOne (n : Nat) : Nat := n + 1

-- ============================================
-- 混合参数类型
-- ============================================

def mixed {α : Type} [Add α] (x y : α) : α :=
  x + y

def implicitAndExplicit {α : Type} (x : α) (y : α) : α :=
  x

-- ============================================
-- 复杂类型签名
-- ============================================

def complexType (f : Nat → Nat) (g : Nat → Nat) : Nat → Nat :=
  fun x => f (g x)

def dependentType (n : Nat) : {m : Nat // m > n} :=
  ⟨n + 1, by simp⟩
  -- dependentType params=(n : Nat) type=Nat // m > n} body=⟨n + 1, by simp⟩

-- ============================================
-- 递归定义
-- ============================================

def factorial : Nat → Nat
  | 0 => 1
  | n + 1 => (n + 1) * factorial n

def sumList : List Nat → Nat
  | [] => 0
  | x :: xs => x + sumList xs

-- ============================================
-- 多行函数体
-- ============================================

def multilineExample (n : Nat) : Nat :=
  let x := n + 1
  let y := x * 2
  y - 1

def withMatch (xs : List Nat) : Nat :=
  match xs with
  | [] => 0
  | x :: xs =>
    let rest := withMatch xs
    x + rest
