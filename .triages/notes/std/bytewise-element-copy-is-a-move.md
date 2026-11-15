---
name: bytewise-element-copy-is-a-move
description: "mem_copy of elements moves them: the source must not drop them after (free its buffer only), and copying out of a borrow must clone each element"
updated: 2026-11-15
---

`std::mem::ops::mem_copy` over a container's elements duplicates their bytes, so for an element that owns heap memory
(a `Vec`, `Str`, `Single`...) both copies own the same allocation. That is only sound as a **move**: the source gives up
its elements without running their destructors. `Vec` frees a moved-from vector with `free_moved_from` (destructure,
drop only the `RawBuf`), not `drop(that)`, because `Vec::drop` destroys `[0, length)`. A **copy** out of something that
stays alive (`clone`, `from`/`from_in`/`append_from_view` from a `&View[T]`) clones each element. A `T: Clone` bound
does not make a function clone: until 2026-11-15, `Vec::clone` and `append_from_view` had the bound but `mem_copy`d,
and `append_many`/`insert_many_at` dropped `that` after moving its elements, so every `Vec[Vec[T]]` clone, `from` or
`append_many` double-freed.

**Why:** the compiler's linear-type and borrow checks see no ownership in `mem_copy`; it is a trusted raw-memory
intrinsic, so a second owner made through it goes unchecked. The `Vec` tests only used `S32`, which is `Copy`, so a
bytewise copy equals a clone and no test failed.

**How to apply:** a `mem_copy` of `T`s is either a move (the source then frees only its storage) or needs `T: Copy`;
anything else clones per element. Test containers with an element that owns heap memory (`Vec[Vec[S32]]`, `Str`), not
only integers, and run owning-type groups under valgrind as [[destroy-heap-values-by-read]] describes.
