import LeanLspExtension

-- 正常声明
def good1 : Nat := 42

-- 语法错误1：未闭合的括号
def bad1 (x : Nat : Nat := x

-- 语法错误2：缺少函数体
def bad2 : Nat :=

-- 语法错误3：未闭合的字符串
def bad3 : String := "

-- 语法错误4：错误的类型表达式
def bad4 : NotAType := 5

-- 语法错误5：缺少冒号
def bad5 Nat := 10

-- 语法错误6：多个类型签名
def bad6 : Nat : String := 5

-- 语法错误7：未闭合的 match
def bad7 (xs : List Nat) : Nat :=
  match xs with
  | [] => 0
  | x :: xs =>

-- 语法错误8：缺少参数类型
def bad8 (x) : Nat := x

-- 语法错误9：错误的 tactic
theorem bad9 : False := by

-- 语法错误10：不存在的关键字
notakeyword foo : Nat := 1

-- 正常声明（在错误之后）
def good2 (n : Nat) : Nat := n + 1

theorem good3 : True := by trivial
