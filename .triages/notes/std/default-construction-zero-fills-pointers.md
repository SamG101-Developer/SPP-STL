---
name: default-construction-zero-fills-pointers
description: "T() zero-fills any class, NonNull included, so take(), and swap built on it, leave a placeholder whose drop dereferences null for Rc/Single/Arc"
updated: 2026-11-15
---

`T()` builds any class with every field defaulted, and a `NonNull` field defaults to null, although `NonNull`'s whole
invariant is non-null. User code can write `NonNull[S32]()`, `Rc[Str]()`, `Single[Str]()` or `Arc[Str]()`, and each
segfaults on use or drop. `std::mem::ops::take` was `replace(src, T())`, so `take(&mut rc)` returned the `Rc` and left
a null one to be dropped, and `Vec::swap` / `Arr::swap` swapped through it: swapping or reversing a `Vec[Rc[T]]`
crashed in `Rc::drop` (unsafe-surface audit U1/U2, 2026-11-15). `take` is removed; swapping is one `View::swap` that
moves both elements through `NonNull` read/write. `std::mem::ops::swap`, the intrinsic, also exchanges without a
placeholder.

**Why:** a placeholder value is only safe if dropping it is a no-op. That holds for `Str` and `Vec` (null with length
and capacity 0), but not for anything that dereferences a pointer in `Drop`.

**How to apply:** never manufacture a `T()` in generic std code to stand in for a moved-out value. Exchange in place
(`std::mem::ops::swap`, or `RawBuf`-level moves) or move out with `NonNull.read` and write back. Test any generic
container operation with an `Rc`/`Single` element, not only `S32` and `Str`. See [[bytewise-element-copy-is-a-move]]
and `.triages/unsafe-audit/README.md`.
