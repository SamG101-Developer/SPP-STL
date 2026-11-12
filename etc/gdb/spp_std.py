# gdb pretty-printers for the std's types, as a debugger should show them: a string's text, a vector's elements, the
# member a variant holds. Matched by the type's name in the debug info, which carries its module path
# ("std::string::Str[std::allocator::GlobalAlloc]"), so a project's own type of the same short name is left alone.
# They read the layouts in "src/std", and change with them. The compiler marks its debug info as Rust's, so gdb
# already shows numbers, arrays and tuples Rust's way; these cover what is the std's own.
#
#   (gdb) source vcs/std/etc/gdb/spp_std.py

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Protocol

import gdb
import gdb.printing

_STR = re.compile(r"^std::string::Str\[")
_STR_VIEW = re.compile(r"^std::string_view::StrView$")
_VEC = re.compile(r"^std::vector::Vec\[")
_VIEW = re.compile(r"^std::view::View\[")
_CHAR = re.compile(r"^std::char::Char$")


class Printer(Protocol):
    """
    What gdb asks of a pretty-printer. Only "to_string" is required; "children" and "display_hint" are optional, and
    checked for with "hasattr" where a printer forwards to another.
    """

    def to_string(self) -> str: ...


class StrPrinter:
    """
    For strings and string views, both pointer/length pairs once their fields are read ("Str" through its "bytes"
    vector), read the UTF-8 text from the pointer, as long as the length, and nothing after it.
    """

    def __init__(self, pointer: gdb.Value, length: gdb.Value) -> None:
        self.pointer = pointer
        self.length = int(length)

    def to_string(self) -> str:
        # Read and decode, then handle escape characters.
        text = _bytes(self.pointer, self.length).decode("utf-8", errors="replace")
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class SequencePrinter:
    """
    For a vector or view, provide the length (and a vector's capacity) to the format, and add the elements at their
    index underneath, instead of just the raw fields.
    """

    def __init__(self, label: str, pointer: gdb.Value, length: gdb.Value, capacity: gdb.Value | None = None) -> None:
        self.label = label
        self.pointer = pointer
        self.length = int(length)
        self.capacity = None if capacity is None else int(capacity)

    def to_string(self) -> str:
        if self.capacity is None: return f"{self.label}(len={self.length})"
        return f"{self.label}(len={self.length}, cap={self.capacity})"

    def children(self) -> Iterator[tuple[str, gdb.Value]]:
        for i in range(self.length):
            yield f"[{i}]", (self.pointer + i).dereference()

    def display_hint(self) -> str:
        return "array"


class VariantPrinter:
    """
    For a variant, get the currently held type member of the variant, and the tag that determined it. For example with
    "Opt[Str]", check the "{ tag, payload }" structure, if the tag is 0, then "Some[Str]" is present.
    """

    def __init__(self, value: gdb.Value) -> None:
        self.value = value

    def _held(self) -> tuple[str, gdb.Value | None]:
        """
        Get the name of the held member, and its value from the payload. The member is the tag's enumerator, found by
        its value, as how the tag prints depends on the language mode.
        """

        # Get the tag as a number, and find the enumerator with that value.
        tag = self.value["tag"]
        name: str | None = None
        for enumerator in tag.type.strip_typedefs().fields():
            if enumerator.enumval == int(tag):
                # Rust's rules qualify an enumerator by its enumeration's path, ending "::tag::"; the member's own name
                # is what follows.
                name = enumerator.name.rsplit("::tag::", 1)[-1]
        if name is None: return "?", None

        # Find the payload's field of the same name.
        payload = self.value["payload"]
        for field in payload.type.strip_typedefs().fields():
            if field.name == name: return name, payload[field.name]
        return name, None

    def to_string(self) -> str:
        # A member with no value shows its name alone; a single value shows in parentheses.
        name, held = self._held()
        if held is None: return _short(name)
        held_type = held.type.strip_typedefs()
        if held_type.code not in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
            return f"{_short(name)}({held.format_string()})"

        # A class shows its one field in parentheses, or all of them as gdb formats them.
        fields = held_type.fields()
        if not fields: return _short(name)
        if len(fields) == 1: return f"{_short(name)}({held[fields[0].name].format_string()})"
        return f"{_short(name)} {held.format_string()}"


class CharPrinter:
    """
    For a character (type "Char"), format with the character, and its code point. If there is no formattable
    representation, just the code point is returned.
    """

    def __init__(self, value: gdb.Value) -> None:
        self.code = int(value["inner"])

    def to_string(self) -> str:
        try:
            return f"'{chr(self.code)}' (U+{self.code:04X})"
        except ValueError:
            return f"U+{self.code:04X}"


class BorrowPrinter:
    """
    For a borrow of a type with a printer of its own ("&StrView", "&std::vector::Vec[...]"), forward to that printer,
    with a leading "&", so a borrowed string or vector reads as its contents.
    """

    def __init__(self, value: gdb.Value, printer: Printer) -> None:
        self.value = value
        self.printer = printer

    def to_string(self) -> str:
        return "&" + str(self.printer.to_string())

    def children(self) -> Iterator[tuple[str, gdb.Value]]:
        return self.printer.children() if hasattr(self.printer, "children") else iter(())

    def display_hint(self) -> str | None:
        return self.printer.display_hint() if hasattr(self.printer, "display_hint") else None


def _strip(value: gdb.Value) -> gdb.Type:
    """
    The type of a value, with any typedefs removed, which is what the printers match against.
    """

    return value.type.strip_typedefs()


def _short(name: str) -> str:
    """
    Convert a type identifier into "short form". This strips back the generic argument group, and also any namespacing
    that exists on the type. "std::option::Some[std::boolean::Bool]" becomes "Some".
    """

    return name.split("[", 1)[0].rsplit("::", 1)[-1]


def _bytes(pointer: gdb.Value, length: int) -> bytes:
    """
    Unified pointer/length memory read function. 0 length is empty wherever the pointer is.
    """

    if length == 0: return b""
    return bytes(gdb.selected_inferior().read_memory(int(pointer), length))


def _is_variant(stripped: gdb.Type) -> bool:
    """
    Check for the structure the compiler gives every variant: the "{ tag, payload }" structure, where the tag is an
    enumeration naming each member, and the payload is a union of the members. Return True for a match.
    """

    if stripped.code != gdb.TYPE_CODE_STRUCT: return False
    names = [field.name for field in stripped.fields()]
    if names != ["tag", "payload"]: return False
    return (stripped.fields()[0].type.strip_typedefs().code == gdb.TYPE_CODE_ENUM
            and stripped.fields()[1].type.strip_typedefs().code == gdb.TYPE_CODE_UNION)


def lookup(value: gdb.Value) -> Printer | None:
    """
    The function gdb calls for every value it prints. A non-null borrow of a type with a printer gets that printer
    behind a "&"; anything else is matched directly.
    """

    stripped = _strip(value)
    if stripped.code == gdb.TYPE_CODE_PTR and int(value) != 0:
        target = _std_printer(value.dereference())
        return BorrowPrinter(value, target) if target is not None else None
    return _std_printer(value)


def _std_printer(value: gdb.Value) -> Printer | None:
    """
    Match a value's type name against the std's types, and build the printer for it from the fields it needs. Variants
    are matched by their structure instead, as every variant shares it.
    """

    stripped = _strip(value)
    name = stripped.name or ""
    if stripped.code != gdb.TYPE_CODE_STRUCT: return None

    # Strings and string views are both read as pointer/length pairs.
    if _STR.match(name):
        vector = value["bytes"]
        return StrPrinter(vector["buffer"]["pointer"], vector["length"])
    if _STR_VIEW.match(name): return StrPrinter(value["pointer"], value["length"])

    # Vectors and views list their elements.
    if _VEC.match(name):
        return SequencePrinter("Vec", value["buffer"]["pointer"], value["length"], value["buffer"]["capacity"])
    if _VIEW.match(name): return SequencePrinter("View", value["pointer"], value["length"])

    # Characters, then any variant.
    if _CHAR.match(name): return CharPrinter(value)
    if _is_variant(stripped): return VariantPrinter(value)
    return None


def register(objfile: gdb.Objfile | None = None) -> None:
    """
    Entry point for gdb to use. Add the printers to "objfile" (a program loading this through ".debug_gdb_scripts"), or
    to every program.
    """

    gdb.printing.register_pretty_printer(objfile, lookup, replace=True)


register(gdb.current_objfile())
