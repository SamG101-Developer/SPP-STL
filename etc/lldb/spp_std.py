# lldb formatters for the std's types, as a debugger should show them: a string's text, a vector's elements, the
# member a variant holds. Matched by the type's name in the debug info, which carries its module path
# ("std::string::Str[std::allocator::GlobalAlloc]"), so a project's own type of the same short name is left alone.
# They read the layouts in "src/std", and change with them. The same as "etc/gdb/spp_std.py", for lldb.
#
#   (lldb) command script import vcs/std/etc/lldb/spp_std.py

from __future__ import annotations

from typing import Any

import lldb

_CATEGORY = "spp"


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
    view and char types, which need special handling, and make U8/S8 include the actual number too.
    """

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
    run(f'type summary add -w {_CATEGORY} -F {module}.variant_summary --recognizer-function {module}.is_variant')
    # One-byte integers are numbers, not characters (lldb shows them as C's "unsigned char").
    run(f'type format add -w {_CATEGORY} -f decimal "unsigned char" "signed char" U8 S8')
    # Enabled last. A Rust-aware lldb (CLion's) applies only categories enabled for Rust to its Rust values; a stock
    # one has no Rust and refuses the language, so that is asked quietly.
    run(f'type category enable {_CATEGORY}')
    debugger.GetCommandInterpreter().HandleCommand(
        f'type category enable -l rust {_CATEGORY}', lldb.SBCommandReturnObject())
