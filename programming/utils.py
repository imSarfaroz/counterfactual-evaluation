import ast
from contextlib import redirect_stdout
import hashlib
from io import StringIO
import re


class TypeHintRemover(ast.NodeTransformer):
    # src: https://stackoverflow.com/a/61308385

    def visit_FunctionDef(self, node):
        # remove the return type definition
        node.returns = None
        # remove all argument annotations
        if node.args.args:
            for arg in node.args.args:
                arg.annotation = None
        self.generic_visit(node)
        return node

    def visit_AnnAssign(self, node):
        if node.value is None:
            return None
        return ast.Assign([node.target], node.value)

    def visit_Import(self, node):
        node.names = [n for n in node.names if n.name != "typing"]
        return node if node.names else None

    def visit_ImportFrom(self, node):
        return node if node.module != "typing" else None


def remove_type_hints(program: str):
    tree = ast.parse(program)
    # we may not need to fix missing locations, but anyway
    new_tree = ast.fix_missing_locations(TypeHintRemover().visit(tree))
    return ast.unparse(new_tree)


class OneBasedIndexingTransformer(ast.NodeTransformer):
    # Just to be safe
    types_we_know_how_to_handle = (
        ast.Constant,
        ast.UnaryOp,
        ast.BinOp,
        ast.Name,
        ast.Subscript,
        ast.IfExp,
        ast.Slice,
    )

    def visit(self, node, is_root=False):
        # Needed for visit_Attribute
        if is_root:
            # https://stackoverflow.com/a/43311383
            for curr in ast.walk(node):
                for child in ast.iter_child_nodes(curr):
                    child.parent = curr
        return super().visit(node)

    def shift_idx(self, text: str) -> str:
        # 1/0 is an assert that the index shouldn't be 0 in 1-based indexing
        return f"({text})-(1 if ({text}) > 0 else (0 if ({text}) < 0 else (1/0)))"

    def expand_idx(self, slice_node) -> str:
        def expand_single_idx(idx_node):
            return self.shift_idx(ast.unparse(idx_node))

        if isinstance(slice_node, ast.Slice):
            lower_text = (
                f"({expand_single_idx(slice_node.lower)})" if slice_node.lower is not None else ""
            )
            upper_text = (
                f"({expand_single_idx(slice_node.upper)})" if slice_node.upper is not None else ""
            )
            step_text = f"({ast.unparse(slice_node.step)})" if slice_node.step is not None else ""
            return f"{lower_text}:{upper_text}:{step_text}"
        else:
            return expand_single_idx(slice_node)

    def visit_Subscript(self, node):
        assert any(isinstance(node.slice, t) for t in self.types_we_know_how_to_handle)
        node = self.generic_visit(node)
        lhs_text = ast.unparse(node.value)

        new_stmt = f"({lhs_text})[{self.expand_idx(node.slice)}] \
if isinstance(({lhs_text}), (list, tuple, str)) \
else ({ast.unparse(node)})"
        return ast.parse(new_stmt).body[0].value

    # The stupid statement/expression distinction in python means that our ternary solution above
    # won't work for assignments, so we have to special case these.

    def visit_Assign(self, node):
        if not any(isinstance(target, ast.Subscript) for target in node.targets):
            return self.generic_visit(node)
        if len(node.targets) > 1:
            # though this shouldn't be hard to support
            raise NotImplementedError("Can't handle multiple targets for now")

        target = node.targets[0]
        assert any(isinstance(target.slice, t) for t in self.types_we_know_how_to_handle)
        target = self.generic_visit(target)
        lhs_text = ast.unparse(target.value)
        node.value = self.visit(node.value)
        value_text = ast.unparse(node.value)

        new_stmt = f"""\
if isinstance(({lhs_text}), (list, tuple, str)):
    ({lhs_text})[{self.expand_idx(target.slice)}] = {value_text}
else:
    {ast.unparse(node)}"""
        return ast.parse(new_stmt).body[0]

    def visit_AugAssign(self, node):
        if not isinstance(node.target, ast.Subscript):
            return self.generic_visit(node)

        target = node.target
        assert any(isinstance(target.slice, t) for t in self.types_we_know_how_to_handle)
        target = self.generic_visit(target)
        lhs_text = ast.unparse(target.value)
        node.value = self.visit(node.value)
        value_text = ast.unparse(node.value)
        match node.op:
            # Wow this is long, but it's all generated by copilot. I myself have no idea what ops
            # are possible.
            case ast.Add():
                op_char = "+"
            case ast.Sub():
                op_char = "-"
            case ast.Mult():
                op_char = "*"
            case ast.Div():
                op_char = "/"
            case ast.FloorDiv():
                op_char = "//"
            case ast.Mod():
                op_char = "%"
            case ast.Pow():
                op_char = "**"
            case ast.LShift():
                op_char = "<<"
            case ast.RShift():
                op_char = ">>"
            case ast.BitOr():
                op_char = "|"
            case ast.BitXor():
                op_char = "^"
            case ast.BitAnd():
                op_char = "&"
            case ast.MatMult():
                op_char = "@"
            case _:
                raise NotImplementedError(f"Can't handle {node.op}")

        assert ast.unparse(
            ast.parse(f"({lhs_text})[({ast.unparse(target.slice)})] {op_char}= ({value_text})")
        ) == ast.unparse(node)
        new_stmt = f"""\
if isinstance(({lhs_text}), (list, tuple, str)):
    ({lhs_text})[{self.expand_idx(target.slice)}] {op_char}= {value_text}
else:
    {ast.unparse(node)}"""
        return ast.parse(new_stmt).body[0]

    def visit_Attribute(self, node):
        node = self.generic_visit(node)

        # Due to the complexity of fully supporting all possible attributes of str/list/tuple, this
        # current implementation is incredibly fragile, in the sense that it will throw
        # unimplemented/assertion errors for many cases, but shouldn't handle anything incorrectly.
        # 1. We take a whitelisting approach to be safe, so supported attribtues need to be added
        #    one by one.
        # 2. The parent-argument approach doesn't work if e.g. the function is saved to a variable
        #    and then called later.

        if node.attr in {"append", "extend", "join", "clear", "split", "lower", "upper", "islower", "isupper", "isdigit", "isalpha", "swapcase", "sort", "remove", "count", "replace", "reverse", "encode", "strip"}:
            # these don't have indexing
            return node
        if node.attr in {"startswith", "endswith"}:
            assert isinstance(node.parent, ast.Call)
            assert len(node.parent.args) == 1, "Indices not supported for startswith/endswith"
            return node
        unparsed_value = ast.unparse(node.value)
        if node.attr == "pop":
            assert isinstance(node.parent, ast.Call)
            if len(node.parent.args) == 0:
                return node
            assert len(node.parent.args) == 1
            return ast.parse(  # antwnc = "a name that would never collide"
                f"(lambda __antwnc: ({ast.unparse(node)})({self.shift_idx('__antwnc')})) if isinstance(({unparsed_value}), (list, tuple, str)) else ({ast.unparse(node)})"
            ).body[0].value
        if node.attr == "index":
            assert isinstance(node.parent, ast.Call)
            if len(node.parent.args) == 1:
                return ast.parse(  # antwnc = "a name that would never collide"
                    f"(lambda __antwnc: ({ast.unparse(node)})(__antwnc) + 1) if isinstance(({unparsed_value}), (list, tuple, str)) else ({ast.unparse(node)})"
                ).body[0].value
            assert False, "Indices not supported for index"

        # If it's a new method on list/tuple/str, we raise an error to manually inspect it.
        # But we need a different type of error than 0 division which we use for something else above
        return ast.parse(
            f"([][1] if isinstance(({unparsed_value}), (list, tuple, str)) else ({unparsed_value})).{node.attr}"
        ).body[0].value


def rewrite_for_one_based_indexing(program: str):
    tree = ast.parse(program)
    # we may not need to fix missing locations, but anyway
    new_tree = ast.fix_missing_locations(OneBasedIndexingTransformer().visit(tree, is_root=True))
    program = ast.unparse(new_tree)

    program = """\
old_enumerate = enumerate
# implicitly asserting that the user can't use this start= kwargs
enumerate = lambda x: old_enumerate(x, start=1)
old_range = range
range = lambda *args: old_range(1, args[0]) if len(args) == 1 else old_range(*args)
""" + program

    if "try:" in program and ("1 / 0" in program or "[][1]" in program):
        program_hash = hashlib.md5(program.encode("utf-8")).hexdigest()
        if program_hash not in {"722ca5ea4e134b7ab618a613c536e1b7", "fd9719a9fb2203258a6bbac23c111121", "635db0f57066ae026398a1aa262db3e2", "a8e61d8e24f0d37c2a072ed8023903b5", "9f2f4e58247fb4def3ab5fcc1f05411b", "e36747052b4230e728980cf270418a20", "6f05ff0fdc2168d0be5b1ceb442985f4", "66006bb80f1e8ea2608737f64a1432a7", "7e86358defcb3d3718d4a2fb1e70a963", "3a0d7f6551b5f52ea1f3860d42c32572", "30c5bc1328bd9aa8bc51acd87cd7bf72", "f4469d3192dc56cddb9e2bc23f7c1f92", "5aa7c6427b3d26ccb5fb0494fa1fa4ca", "2639430b68f6173d81687fa4a0281d77"}:  # variants of a program in humaneval (by_length) that we checked
            # We don't want exceptions that we throw to be handled by the program
            print(program)
            print(
                "!!!We rely on exceptions for some checks; manually check this program!!!"
                " We should make sure our manually raised exceptions will not happen in this program."
            )
            breakpoint()

    return program


def remove_docstrings(s):
    # remove docstrings and some other misc cleaning
    s = s.replace('FIX = """', '"""')
    parts = s.split('"""')
    parts = [p for i, p in enumerate(parts) if i % 2 == 0]
    s = "".join(parts)

    s = s.replace("FIX = '''", "'''")
    parts = s.split("'''")
    parts = [p for i, p in enumerate(parts) if i % 2 == 0]
    s = "".join(parts)

    s = s.replace(":\n    \n", ":\n")
    s = s.lstrip("\n")
    return s


def extract_calls(s):
    assert "candidate(" in s
    span_indices = []
    calls = []
    idx = s.find("candidate(")
    while idx != -1:
        started = False
        in_string = False
        depth = 0
        for i, c in enumerate(s[idx:]):
            # This parsing logic is in general fragile! E.g. it doesn't handle "'" or escapes.
            # But it works for humaneval.
            if c in ('"', "'"):
                in_string = not in_string
            if not in_string:
                if c == "(":
                    started = True
                    depth += 1
                elif c == ")":
                    depth -= 1
                if started and depth == 0:
                    end = i
                    break
        assert depth == 0 and started and not in_string
        span_indices.append((idx, idx + end + 1))
        calls.append(s[idx : idx + end + 1])
        idx = s.find("candidate(", idx + 1)
    return span_indices, calls


def sub_calls(program: str, call_span_indices: list[tuple[int, int]], called_values: list[str]):
    delta = 0
    for (start, end), value in zip(call_span_indices, called_values, strict=True):
        assert value[0] == "[" and value[-1] == "]"  # artifact, see comment in query_exe.py
        value = value[1:-1]
        program = program[: start + delta] + value + program[end + delta :]
        delta += len(value) - (end - start)
    return program


def check_asserts_pass(obj: dict, called_values: list[str]):
    program = f"{obj['prompt']}{obj['canonical_solution']}{obj['test']}"
    call_span_indices, _ = extract_calls(program)
    subbed_program = sub_calls(program, call_span_indices, called_values)
    globals_and_locals = {k: v for k, v in globals().items() if k.startswith("__")}
    exec(subbed_program, globals_and_locals, globals_and_locals)


def assemble_program_with_calls(obj: dict, perturbation_filter: str = None, fn_name: str = None):
    """Returns a program in the format of
    ```
    def fn(...):
        ...

    print([fn(...)])
    print([fn(...)])
    ...
    ```
    """
    context = f"""{remove_docstrings(obj["prompt"])}{obj["canonical_solution"]}"""
    context = remove_type_hints(context)

    entry_point = obj["entry_point"]
    if fn_name is not None:
        context = context.replace(f"{entry_point}(", f"{fn_name}(")
        entry_point = fn_name

    _, test_body = obj["test"].split("def check(candidate):\n")
    _, calls = extract_calls(test_body)
    assert all(c.startswith("candidate(") for c in calls)
    # If it were simply print(x), we can't distinguish str vs. other built-in types.
    # But this is possible when x is in an array.
    formatted_calls = [f"print([{entry_point}{c[len('candidate'):]}])" for c in calls]

    filtered = False
    if len(formatted_calls) != len(set(formatted_calls)):  # dedup
        new_formatted_calls = []  # technically better to use a set but these are short
        for call in formatted_calls:
            if call not in new_formatted_calls:
                new_formatted_calls.append(call)
        formatted_calls = new_formatted_calls
        assert len(formatted_calls) == len(set(formatted_calls))
        filtered = True

    if perturbation_filter is not None:
        filtered_call_indices = []
        filtered_formatted_calls = []
        for i, call in enumerate(formatted_calls):
            program = f"{context}\n\n{call}"
            try:
                output = eval_program_with_calls(program, perturbation=perturbation_filter)
            except ZeroDivisionError:
                # involves indexing with 0; remove this test case
                filtered = True
                continue
            assert len(output) == 1
            filtered_call_indices.append(i)
            filtered_formatted_calls.append(call)
        formatted_calls = filtered_formatted_calls
    else:
        filtered_call_indices = list(range(len(formatted_calls)))

    newline = "\n"  # https://stackoverflow.com/a/67680321
    program = f"""{context}\n\n{newline.join(formatted_calls)}"""
    if not filtered:
        check_asserts_pass(obj, eval_program_with_calls(program))
    elif len(formatted_calls) > 0:
        eval_program_with_calls(program)  # at least make sure it runs
    return program, filtered_call_indices


def eval_program_with_calls(program: str, perturbation: str = None, return_output: bool = True):
    if perturbation == "one_based_indexing":
        program = rewrite_for_one_based_indexing(program)
    else:
        assert perturbation is None

    f = StringIO()
    with redirect_stdout(f):
        globals_and_locals = {k: v for k, v in globals().items() if k.startswith("__")}
        exec(program, globals_and_locals, globals_and_locals)
    if return_output:
        output = f.getvalue()
        assert output[-1] == "\n"
        return output[:-1].split("\n")


def one_based_indexing_unit_tests(test=False):
    tests = """\
assert (7, 8, 9)[1] == 7
assert ["abc", "def", "ghi"][3] == "ghi"
assert "abcde"[4] == "d"
assert "abc"[:2] == "a"
assert [7, 8, 9][1:] == [7, 8, 9][1:5] == [7, 8, 9][1::1] == [7, 8, 9][:4] == [9, 8, 7][::-1] == [9, 8, 7, 6][3::-1] == [7, 8, 9]
assert list(enumerate([7, 8, 9])) == [(1, 7), (2, 8), (3, 9)]
assert list(range(2)) == [1]
assert list(range(2, 4)) == [2, 3]
assert {0: 7, 1: 8, 2: 9}[1] == 8
assert [7, 8, 9].index(8) == 2"""
    if test:
        program = rewrite_for_one_based_indexing(tests)
        globals_and_locals = {k: v for k, v in globals().items() if k.startswith("__")}
        exec(program, globals_and_locals, globals_and_locals)
    return tests


def one_based_indexing_checks(test=False):
    tests = """\
print([list(range(3))])
print([[4, 5, 6].pop(2)])
print(["qrs"[:2]])
print(["qrstu"[4]])
print([list(enumerate("qrstuv"))])"""
    if test:
        print(eval_program_with_calls(tests, perturbation="one_based_indexing"))
    return tests
