# lldb formatters for the std's types, as a debugger should show them: a string's text, a vector's elements, the
# member a variant holds. Matched by the type's name in the debug info, which carries its module path
# ("std::string::Str[std::allocator::GlobalAlloc]"), so a project's own type of the same short name is left alone.
# They read the layouts in "src/std", and change with them. The same as "etc/gdb/spp_std.py", for lldb.
#
#   (lldb) command script import vcs/std/etc/lldb/spp_std.py

from __future__ import annotations

import datetime
from typing import Any

import lldb

_CATEGORY = "spp"
_MAX_LIMBS = 256


class SequenceProvider:
    """
    Instead of just the raw fields, add the elements at their index under the vector or view.
    """

    def __init__(self, valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> None:
        self.valobj = valobj
        self.update()

    def _fields(self) -> tuple[lldb.SBValue, lldb.SBValue]:
        """
        Get the pointer and length of the sequence.
        """

        value = _pointee(self.valobj)
        buffer = value.GetChildMemberWithName("buffer")
        if buffer.IsValid():
            return buffer.GetChildMemberWithName("pointer"), value.GetChildMemberWithName("length")
        return value.GetChildMemberWithName("pointer"), value.GetChildMemberWithName("length")

    def update(self) -> bool:
        self.pointer, length = self._fields()
        self.length = length.GetValueAsUnsigned()
        self.element = self.pointer.GetType().GetPointeeType()
        self.size = self.element.GetByteSize()
        return False

    def num_children(self) -> int:
        return self.length

    def get_child_index(self, name: str) -> int:
        try:
            return int(name.lstrip("[").rstrip("]"))
        except ValueError:
            return -1

    def get_child_at_index(self, index: int) -> lldb.SBValue | None:
        if index < 0 or index >= self.length or self.size == 0:
            return None
        address = self.pointer.GetValueAsUnsigned() + index * self.size
        return self.valobj.CreateValueFromAddress(f"[{index}]", address, self.element)

    def has_children(self) -> bool:
        return True


class OwnerProvider:
    """
    Instead of just the raw fields (a pointer and an allocator), add the value an owner or a lock holds as its one child,
    "value". A "Weak" whose value is gone has none.
    """

    def __init__(self, valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> None:
        self.valobj = valobj
        self.update()

    def update(self) -> bool:
        self.held = _owned(self.valobj)[1]
        return False

    def num_children(self) -> int:
        return 0 if self.held is None else 1

    def get_child_index(self, name: str) -> int:
        return 0 if name == "value" else -1

    def get_child_at_index(self, index: int) -> lldb.SBValue | None:
        if index != 0 or self.held is None: return None
        return self.valobj.CreateValueFromAddress("value", self.held.GetLoadAddress(), self.held.GetType())

    def has_children(self) -> bool:
        return self.held is not None


def _pointee(valobj: lldb.SBValue) -> lldb.SBValue:
    """
    Read from a pointer, with a length, into a value (valobj). This creates the reference to the pointer that LLDB
    passes into the formatters, and what the _read_text function needs.
    """
    if valobj.GetType().IsPointerType(): valobj = valobj.Dereference()
    return valobj.GetNonSyntheticValue()


def _short(name: str) -> str:
    """
    Convert a type identifier into "short form". This strips back the generic argument group, and also any namespacing
    that exists on the type. "std::option::Some[std::boolean::Bool]" becomes "Some".
    """
    return name.split("[", 1)[0].rsplit("::", 1)[-1]


def _text(valobj: lldb.SBValue) -> str:
    """
    The text of a value is either its summary, should the summary exist, otherwise its value. Make it null safe with a
    final "" fallback.
    """

    # A child as a summary shows it: its own summary, else its value.
    return valobj.GetSummary() or valobj.GetValue() or ""


def _read_text(valobj: lldb.SBValue, pointer: int, length: int) -> str:
    """
    Unified pointer/length memory read function. 0 length is empty wherever the pointer is. Try to read the memory and
    save a potential error. Decode in UTF-8 and handle escape characters.
    """

    # Initial read and error saving, and 0-length guard.
    if length == 0: return '""'
    error = lldb.SBError()
    data = valobj.GetProcess().ReadMemory(pointer, length, error)

    # Fallback on error, otherwise decode and parse.
    if not error.Success(): return "<unreadable>"
    text = data.decode("utf-8", errors="replace")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def str_summary(valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> str:
    """
    For strings, the owned, heap-allocated UTF-8 string type, get the "bytes" field (vector of bytes), and then get the
    bytes' buffer's pointer. This is the raw location of the string. Combined with the length, we can read from the
    pointer.
    """

    value = _pointee(valobj)
    vector = value.GetChildMemberWithName("bytes")
    pointer = vector.GetChildMemberWithName("buffer").GetChildMemberWithName("pointer").GetValueAsUnsigned()
    length = vector.GetChildMemberWithName("length").GetValueAsUnsigned()
    return _read_text(value, pointer, length)


def str_view_summary(valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> str:
    """
    For string views, they are already pointer/length pairs, so route them into the unified pointer/length memory reader
    and return that value.
    """
    value = _pointee(valobj)
    pointer = value.GetChildMemberWithName("pointer").GetValueAsUnsigned()
    length = value.GetChildMemberWithName("length").GetValueAsUnsigned()
    return _read_text(value, pointer, length)


def vec_summary(valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> str:
    """
    For a vector, provide the length and capacity values to the format, and not the elements. These are attached via the
    SequenceProvider.
    """

    value = _pointee(valobj)
    length = value.GetChildMemberWithName("length").GetValueAsUnsigned()
    capacity = value.GetChildMemberWithName("buffer").GetChildMemberWithName("capacity").GetValueAsUnsigned()
    return f"Vec(len={length}, cap={capacity})"


def view_summary(valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> str:
    """
    For a view, provide the length to the format, and not the elements. These are attached via the SequenceProvider, and
    lots of collections can forward to a view.
    """

    value = _pointee(valobj)
    length = value.GetChildMemberWithName("length").GetValueAsUnsigned()
    return f"View(len={length})"


def char_summary(valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> str:
    """
    For a character (type "Char"), format with the character, and its code point. If there is no formattable
    representation, just the code point is returned.
    """
    code = _pointee(valobj).GetChildMemberWithName("inner").GetValueAsUnsigned()
    try:
        return f"'{chr(code)}' (U+{code:04X})"
    except ValueError:
        return f"U+{code:04X}"


def _inline(valobj: lldb.SBValue, depth: int = 0) -> str:
    """
    The text of a value on one line: its own text should it have one, otherwise its fields by name, two levels down at
    most. A class with no formatter of its own still reads as its contents inside an owner's summary.
    """

    text = _text(valobj)
    if text or valobj.GetNumChildren() == 0: return text
    if depth == 2: return "{...}"
    children = (valobj.GetChildAtIndex(i) for i in range(valobj.GetNumChildren()))
    fields = ", ".join(f"{child.GetName()}: {_inline(child, depth + 1)}" for child in children)
    return f"{{ {fields} }}"


def _counter(field: lldb.SBValue) -> int:
    """
    A control block's count: a plain number in an "Rc"'s block, an atomic ("Atom[USize]", read through its "val") in an
    "Arc"'s.
    """

    inner = field.GetChildMemberWithName("val")
    return (inner if inner.IsValid() else field).GetValueAsUnsigned()


def _owned(valobj: lldb.SBValue) -> tuple[str, lldb.SBValue | None, str]:
    """
    For a type that owns, shares or guards a value, get its short name, the value, and the counts to show after it.
    "Vol" and "Atom" hold the value in place; "Single" points to it; "Mutex" and "RwLock" hold a "Single" of it; "Rc",
    "Arc" and "Weak" point to a control block holding the strong and weak counts beside it. A block with no strong
    count left no longer holds a value, so there is none.
    """

    # Values held in place, and the locks' cells.
    value = _pointee(valobj)
    name = _short(value.GetTypeName())
    if name in ("Vol", "Atom"): return name, value.GetChildMemberWithName("val"), ""
    if name in ("Mutex", "RwLock"): value = value.GetChildMemberWithName("val")

    # Follow the pointer; a counted one reaches a control block.
    target = value.GetChildMemberWithName("pointer").Dereference()
    if name not in ("Rc", "Arc", "Weak"): return name, target, ""
    strong = _counter(target.GetChildMemberWithName("strong_counter"))
    weak = _counter(target.GetChildMemberWithName("weak_counter"))
    held = target.GetChildMemberWithName("val") if strong > 0 else None
    return name, held, f", strong={strong}, weak={weak}"


def owner_summary(valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> str:
    """
    For an owner, a shared owner or a lock, format its name around the value it holds, and a shared owner's counts:
    "Single(5)", "Rc(6, strong=2, weak=1)", "Mutex(10)". A "Weak" whose value is gone shows "dropped" in its place.
    """

    name, held, counts = _owned(valobj)
    text = "dropped" if held is None else _inline(held)
    return f"{name}({text}{counts})"


def _limbs(vector: lldb.SBValue) -> list[int] | None:
    """
    The 32-bit limbs of a big number's "Vec[U32]", least significant first, read in the program's byte order. None past
    _MAX_LIMBS, whose decimal text would be too long to show (and past what Python converts to text at all).
    """

    # The vector's pointer and length, and the bytes behind them.
    pointer = vector.GetChildMemberWithName("buffer").GetChildMemberWithName("pointer").GetValueAsUnsigned()
    length = vector.GetChildMemberWithName("length").GetValueAsUnsigned()
    if length > _MAX_LIMBS: return None
    if length == 0: return []
    error = lldb.SBError()
    data = vector.GetProcess().ReadMemory(pointer, length * 4, error)
    if not error.Success(): return None

    # Each limb in the program's byte order.
    order = "little" if vector.GetProcess().GetByteOrder() == lldb.eByteOrderLittle else "big"
    return [int.from_bytes(data[i:i + 4], order) for i in range(0, length * 4, 4)]


def _big_text(limbs: list[int] | None, negative: bool) -> str:
    """
    The decimal text of a big number from its limbs, with a "-" for a negative one; the number of limbs instead when
    there are too many to show.
    """

    if limbs is None: return f"<over {_MAX_LIMBS} limbs>"
    magnitude = sum(limb << (32 * i) for i, limb in enumerate(limbs))
    return ("-" if negative and magnitude != 0 else "") + str(magnitude)


def biguint_summary(valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> str:
    """
    For an unsigned big number ("BigUInt"), format its decimal value, worked out from the limbs of its "data" vector.
    """

    value = _pointee(valobj)
    return f"BigUInt({_big_text(_limbs(value.GetChildMemberWithName('data')), False)})"


def bigint_summary(valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> str:
    """
    For a signed big number ("BigInt"), format its decimal value: the magnitude in its "data" ("BigUInt"), negative when
    its "sign" is set.
    """

    value = _pointee(valobj)
    negative = value.GetChildMemberWithName("sign").GetValueAsUnsigned() != 0
    limbs = _limbs(value.GetChildMemberWithName("data").GetChildMemberWithName("data"))
    return f"BigInt({_big_text(limbs, negative)})"


def _duration_text(seconds: int, nanoseconds: int) -> str:
    """
    A duration as Rust shows one: in seconds with the fraction it has ("1.5s"), or under a second, in the largest unit
    it fills ("250ms", "250µs", "5ns").
    """

    def scaled(whole: int, part: int, digits: int, unit: str) -> str:
        fraction = f"{part:0{digits}d}".rstrip("0")
        return f"{whole}.{fraction}{unit}" if fraction else f"{whole}{unit}"

    if seconds > 0: return scaled(seconds, nanoseconds, 9, "s")
    if nanoseconds >= 1_000_000: return scaled(nanoseconds // 1_000_000, nanoseconds % 1_000_000, 6, "ms")
    if nanoseconds >= 1_000: return scaled(nanoseconds // 1_000, nanoseconds % 1_000, 3, "µs")
    return f"{nanoseconds}ns" if nanoseconds else "0s"


def _parts(duration: lldb.SBValue) -> tuple[int, int]:
    """
    The seconds and nanoseconds of a "Duration".
    """

    return (duration.GetChildMemberWithName("seconds").GetValueAsUnsigned(),
            duration.GetChildMemberWithName("nanoseconds").GetValueAsUnsigned())


def duration_summary(valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> str:
    """
    For a duration, format its length in the largest unit that fits it ("Duration(1.5s)").
    """

    return f"Duration({_duration_text(*_parts(_pointee(valobj)))})"


def time_point_summary(valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> str:
    """
    For a point in time, a duration since the Unix epoch, format its date and time in UTC to the nanosecond
    ("TimePoint(2026-11-12T10:09:58.050703768Z)"). One too far from the epoch for a date shows as its duration.
    """

    seconds, nanoseconds = _parts(_pointee(valobj).GetChildMemberWithName("duration"))
    try:
        date = datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return f"TimePoint({_duration_text(seconds, nanoseconds)} since the epoch)"
    return f"TimePoint({date:%Y-%m-%dT%H:%M:%S}.{nanoseconds:09d}Z)"


def path_summary(valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> str:
    """
    For a path, format its text, read from the string it wraps ("inner").
    """

    return f"Path({str_summary(_pointee(valobj).GetChildMemberWithName('inner'), internal_dict)})"


def is_variant(sbtype: lldb.SBType, internal_dict: dict[str, Any]) -> bool:
    """
    Custom "recogniser" function for variants as they have a bit of extra processing; remove a borrow around the
    variant, and check for the two fields; this is the "{ tag, payload }" structure. Validate the layout of the struct,
    and return True for a match.
    """

    if sbtype.IsPointerType():
        sbtype = sbtype.GetPointeeType()
    if sbtype.GetNumberOfFields() != 2:
        return False
    tag, payload = sbtype.GetFieldAtIndex(0), sbtype.GetFieldAtIndex(1)

    # A Rust-aware lldb (CLion's) calls the union a struct.
    return (tag.GetName() == "tag" and payload.GetName() == "payload"
            and tag.GetType().GetTypeClass() == lldb.eTypeClassEnumeration
            and payload.GetType().GetTypeClass() in (lldb.eTypeClassUnion, lldb.eTypeClassStruct))


def variant_summary(valobj: lldb.SBValue, internal_dict: dict[str, Any]) -> str:
    """
    For a variant, get the currently held type member of the variant, and the tag that determined it. For example with
    "Opt[Str]", check the "{ tag, payload }" structure, if the tag is 0, then "Some[Str]" is present.
    """

    # Get the tag as a number
    value = _pointee(valobj)
    tag = value.GetChildMemberWithName("tag")
    number = tag.GetValueAsUnsigned()
    name: str | None = None

    #
    members = tag.GetType().GetEnumMembers()
    for i in range(members.GetSize()):
        member = members.GetTypeEnumMemberAtIndex(i)
        if member.GetValueAsUnsigned() == number:
            name = member.GetName().rsplit("::tag::", 1)[-1]

    # A Rust-aware lldb (CLion's) lists no enumerators, but shows the tag as the one it holds.
    if name is None and members.GetSize() == 0 and tag.GetValue():
        name = tag.GetValue().rsplit("::tag::", 1)[-1]
    if name is None:
        return "?"
    held = value.GetChildMemberWithName("payload").GetChildMemberWithName(name)
    if not held.IsValid():
        return _short(name)
    if held.GetNumChildren() == 0 or not held.GetType().IsAggregateType():
        text = _text(held)
        small = text and held.GetType().GetTypeClass() != lldb.eTypeClassStruct
        return f"{_short(name)}({text})" if small else _short(name)
    if held.GetNumChildren() == 1:
        return f"{_short(name)}({_text(held.GetChildAtIndex(0))})"
    fields = ", ".join(f"{held.GetChildAtIndex(i).GetName()}: {_text(held.GetChildAtIndex(i))}"
                       for i in range(held.GetNumChildren()))
    return f"{_short(name)} {{ {fields} }}"


def __lldb_init_module(debugger: lldb.SBDebugger, internal_dict: dict[str, Any]) -> None:
    """
    Entry point for the LLDB debugger to use. Register the "spp" category, and add summaries for the string, vector,
    view and char types, the owners and locks, the big numbers, times and paths, and variants, which need special
    handling, and make U8/S8 include the actual number too.
    """

    owners = ("single::Single|rc::rc::Rc|rc::weak::Weak|threading::arc::Arc|threading::weak::Weak"
              "|threading::mutex::Mutex|threading::rwlock::RwLock")
    module = __name__
    run = debugger.HandleCommand
    run(f'type category define {_CATEGORY}')
    run(f'type summary add -w {_CATEGORY} -F {module}.str_summary -x "^std::string::Str\\["')
    run(f'type summary add -w {_CATEGORY} -F {module}.str_view_summary -x "^std::string_view::StrView$"')
    run(f'type summary add -w {_CATEGORY} -F {module}.vec_summary -x "^std::vector::Vec\\["')
    run(f'type synthetic add -w {_CATEGORY} -l {module}.SequenceProvider -x "^std::vector::Vec\\["')
    run(f'type summary add -w {_CATEGORY} -F {module}.view_summary -x "^std::view::View\\["')
    run(f'type synthetic add -w {_CATEGORY} -l {module}.SequenceProvider -x "^std::view::View\\["')
    run(f'type summary add -w {_CATEGORY} -F {module}.char_summary -x "^std::char::Char$"')
    run(f'type summary add -w {_CATEGORY} -F {module}.owner_summary -x "^std::({owners})\\["')
    run(f'type synthetic add -w {_CATEGORY} -l {module}.OwnerProvider -x "^std::({owners})\\["')
    run(f'type summary add -w {_CATEGORY} -F {module}.owner_summary'
        f' -x "^std::(mem::volatile::Vol|threading::atomic::Atom)\\["')
    run(f'type summary add -w {_CATEGORY} -F {module}.biguint_summary -x "^std::bignum::biguint::BigUInt$"')
    run(f'type summary add -w {_CATEGORY} -F {module}.bigint_summary -x "^std::bignum::bigint::BigInt$"')
    run(f'type summary add -w {_CATEGORY} -F {module}.duration_summary -x "^std::time::duration::Duration$"')
    run(f'type summary add -w {_CATEGORY} -F {module}.time_point_summary -x "^std::time::time_point::TimePoint$"')
    run(f'type summary add -w {_CATEGORY} -F {module}.path_summary -x "^std::fs::path::Path$"')
    run(f'type summary add -w {_CATEGORY} -F {module}.variant_summary --recognizer-function {module}.is_variant')
    # One-byte integers are numbers, not characters (lldb shows them as C's "unsigned char").
    run(f'type format add -w {_CATEGORY} -f decimal "unsigned char" "signed char" U8 S8')
    # Enabled last. A Rust-aware lldb (CLion's) applies only categories enabled for Rust to its Rust values; a stock
    # one has no Rust and refuses the language, so that is asked quietly.
    run(f'type category enable {_CATEGORY}')
    debugger.GetCommandInterpreter().HandleCommand(
        f'type category enable -l rust {_CATEGORY}', lldb.SBCommandReturnObject())
