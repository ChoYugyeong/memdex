"""outline(): per-language symbol extraction and header detection.

Memory is shared across agents through cheap outlines, so a wrong pattern is
worse than no pattern: these tests pin the languages that used to be lumped
together ("jvm" for Java/Kotlin/C#/Swift/Dart) and the ones that used to
produce false positives (C/C++ control flow, `<template>` markup in Vue).
"""

import os
import shutil
import tempfile
import unittest

from memdex.project import Project
from memdex.scan import (_compiled, LANG_BY_EXT, MAX_BYTES, MAX_SYMBOLS, PATTERNS,
                         list_files, outline, read_text)


def names(ol):
    return [n for n, _k, _ln in ol.symbols]


def kinds(ol):
    return {n: k for n, k, _ln in ol.symbols}


def lines(ol):
    return {n: ln for n, _k, ln in ol.symbols}


class PythonTests(unittest.TestCase):
    def test_docstring_classes_and_methods(self):
        src = '"""Entry point."""\n\nMAX = 3\n\nclass App:\n    def run(self):\n        pass\n\ndef helper():\n    pass\n'
        ol = outline("app/main.py", src)
        self.assertEqual(ol.lang, "python")
        self.assertEqual(ol.head, "Entry point.")
        self.assertEqual(names(ol), ["MAX", "App", "App.run", "helper"])
        self.assertEqual(kinds(ol)["App.run"], "method")
        self.assertEqual(lines(ol)["App"], 5)
        self.assertEqual(lines(ol)["helper"], 9)

    def test_broken_syntax_falls_back_to_text(self):
        ol = outline("broken.py", "def nope(:\n")
        self.assertEqual(ol.lang, "python")
        self.assertEqual(ol.symbols, [])
        self.assertEqual(ol.lines, 2)


class JavaScriptTests(unittest.TestCase):
    SRC = (
        "// ui helpers\n"
        "export function render() {}\n"
        "export const handler = async (e) => {}\n"
        "interface Props {}\n"
        "export default class Widget {}\n"
    )

    def test_functions_types_and_classes(self):
        ol = outline("web/ui.ts", self.SRC)
        self.assertEqual(ol.lang, "js")
        self.assertEqual(ol.head, "ui helpers")
        self.assertEqual(kinds(ol), {
            "render": "fn", "handler": "fn", "Props": "type", "Widget": "class",
        })
        self.assertEqual(lines(ol)["handler"], 3)

    def test_tsx_and_mjs_share_the_scanner(self):
        self.assertEqual(LANG_BY_EXT[".tsx"], "js")
        self.assertEqual(LANG_BY_EXT[".mjs"], "js")


class ComponentTests(unittest.TestCase):
    def test_vue_scans_script_only(self):
        src = (
            "<!-- Vue component -->\n"
            "<script setup lang=\"ts\">\n"
            "const count = ref(0)\n"
            "function increment() { count.value++ }\n"
            "const decrement = () => { count.value-- }\n"
            "</script>\n"
            "\n"
            "<template>\n"
            "  <button @click=\"increment\">+</button>\n"
            "</template>\n"
        )
        ol = outline("src/App.vue", src)
        self.assertEqual(ol.lang, "vue")
        self.assertEqual(ol.head, "Vue component")
        self.assertEqual(names(ol), ["increment", "decrement"])
        # line numbers stay file-relative, not script-relative
        self.assertEqual(lines(ol)["increment"], 4)
        self.assertEqual(lines(ol)["decrement"], 5)

    def test_svelte_reports_functions(self):
        src = (
            "<script>\n"
            "  let name = 'world';\n"
            "  function greet() { return 'hi ' + name; }\n"
            "  const reset = () => { name = ''; };\n"
            "</script>\n"
            "\n"
            "<h1>Hello {name}</h1>\n"
        )
        ol = outline("src/App.svelte", src)
        self.assertEqual(ol.lang, "svelte")
        self.assertEqual(names(ol), ["greet", "reset"])

    def test_template_markup_is_not_a_symbol(self):
        src = "<template>\n  <div class=\"Main\">text</div>\n</template>\n"
        self.assertEqual(outline("Empty.vue", src).symbols, [])


class GoTests(unittest.TestCase):
    SRC = (
        "// Go entry\n"
        "package main\n"
        "\n"
        "type Server struct{ addr string }\n"
        "\n"
        "func NewServer(addr string) *Server { return &Server{addr: addr} }\n"
        "\n"
        "func (s *Server) Start() error { return nil }\n"
        "\n"
        "func (s Server) Stop() {}\n"
        "\n"
        "func (h *handler) private() {}\n"
    )

    def test_receiver_methods_keep_their_type(self):
        ol = outline("main.go", self.SRC)
        self.assertEqual(ol.lang, "go")
        self.assertEqual(ol.head, "Go entry")
        self.assertEqual(names(ol), ["Server", "NewServer", "Server.Start", "Server.Stop", "handler.private"])
        self.assertEqual(kinds(ol)["Server.Start"], "method")
        self.assertEqual(kinds(ol)["NewServer"], "fn")
        self.assertEqual(kinds(ol)["Server"], "type")


class JvmFamilyTests(unittest.TestCase):
    def test_java(self):
        src = (
            "// Java entry\n"
            "package demo;\n"
            "public final class App {\n"
            "    public static void main(String[] args) {\n"
            "        if (args.length > 0) { System.out.println(args[0]); }\n"
            "    }\n"
            "    private List<String> build(Map<String, Integer> in) throws Exception { return null; }\n"
            "    void hidden() { new Thread(() -> {}).start(); }\n"
            "}\n"
            "interface Runner { void run(); }\n"
        )
        ol = outline("App.java", src)
        self.assertEqual(ol.lang, "java")
        self.assertEqual(kinds(ol), {
            "App": "class", "main": "method", "build": "method", "hidden": "method", "Runner": "class",
        })
        self.assertNotIn("if", names(ol))
        self.assertNotIn("Thread", names(ol))

    def test_java_statements_are_not_abstract_methods(self):
        src = "class A {\n    int f() {\n        return g(1);\n    }\n    void h() {\n        throw bad(2);\n    }\n}\n"
        self.assertEqual(names(outline("A.java", src)), ["A", "f", "h"])

    def test_kotlin(self):
        src = (
            "// Kotlin entry\n"
            "data class User(val id: Int)\n"
            "sealed interface Result\n"
            "class Service(private val repo: Repo) {\n"
            "    suspend fun load(id: Int): User { return repo.get(id) }\n"
            "    override fun toString(): String = \"s\"\n"
            "    companion object {\n        const val VERSION = 1\n    }\n"
            "}\n"
            "fun String.shout(): String = uppercase()\n"
            "typealias Handler = (String) -> Unit\n"
        )
        ol = outline("Main.kt", src)
        self.assertEqual(ol.lang, "kotlin")
        self.assertEqual(kinds(ol), {
            "User": "class", "Result": "class", "Service": "class",
            "load": "fn", "toString": "fn", "VERSION": "prop",
            "shout": "fn", "Handler": "type",
        })
        self.assertNotIn("if", names(ol))

    def test_csharp(self):
        src = (
            "// C# entry\n"
            "namespace Demo;\n"
            "public sealed class App {\n"
            "    public static void Main(string[] args) {\n"
            "        if (args.Length > 0) { Console.WriteLine(args[0]); }\n"
            "    }\n"
            "    public async Task<int> LoadAsync() { return await Task.FromResult(1); }\n"
            "    private void Hidden() { }\n"
            "    public int Count { get; set; }\n"
            "}\n"
            "public record Person(string Name);\n"
            "public record struct Pair(int A, int B);\n"
        )
        ol = outline("App.cs", src)
        self.assertEqual(ol.lang, "csharp")
        self.assertEqual(kinds(ol), {
            "App": "class", "Main": "method", "LoadAsync": "method",
            "Hidden": "method", "Count": "prop", "Person": "class", "Pair": "class",
        })
        self.assertNotIn("if", names(ol))

    def test_swift(self):
        src = (
            "// Swift entry\n"
            "final class App {\n"
            "    private let max = 3\n"
            "    func run(_ args: [String]) -> Int { return max }\n"
            "    init(max: Int) { self.max = max }\n"
            "}\n"
            "protocol Runnable {\n"
            "    func run()\n"
            "}\n"
            "extension String {\n"
            "    func shout() -> String { uppercased() }\n"
            "}\n"
        )
        ol = outline("App.swift", src)
        self.assertEqual(ol.lang, "swift")
        self.assertEqual(kinds(ol), {
            "App": "class", "max": "prop", "run": "fn",
            "init": "fn", "Runnable": "class", "String": "class", "shout": "fn",
        })

    def test_dart(self):
        src = (
            "// Dart entry\n"
            "abstract class Repo {\n"
            "  Future<String> load();\n"
            "}\n"
            "class Impl extends Repo {\n"
            "  Future<String> load() async {\n"
            "    if (count > 0) { return 'x'; }\n"
            "    return 'y';\n"
            "  }\n"
            "}\n"
            "mixin Logging {}\n"
            "void main() {\n"
            "  for (var i = 0; i < 3; i++) { print(i); }\n"
            "}\n"
            "String greet(String name) => 'hi $name';\n"
        )
        ol = outline("main.dart", src)
        self.assertEqual(ol.lang, "dart")
        self.assertEqual(kinds(ol), {
            "Repo": "class", "load": "fn", "Impl": "class", "Logging": "class",
            "main": "fn", "greet": "fn",
        })
        self.assertNotIn("if", names(ol))
        self.assertNotIn("for", names(ol))

    def test_extensions_are_split_from_the_old_jvm_bucket(self):
        for ext, lang in {".java": "java", ".kt": "kotlin", ".cs": "csharp",
                          ".swift": "swift", ".dart": "dart", ".scala": "scala"}.items():
            self.assertEqual(LANG_BY_EXT[ext], lang)


class CTests(unittest.TestCase):
    SRC = (
        "// C entry\n"
        "#include <stdio.h>\n"
        "#define MAX 10\n"
        "typedef struct Node { int v; } Node;\n"
        "enum Color { RED };\n"
        "static void helper(int x) {\n"
        "    if (x > 0) {\n"
        "        printf(\"%d\", x);\n"
        "    }\n"
        "    while (x > 0) { x--; }\n"
        "    for (int i = 0; i < 3; i++) { x += i; }\n"
        "}\n"
        "int main(void) {\n"
        "    helper(MAX);\n"
        "    return 0;\n"
        "}\n"
        "unsigned long long compute(unsigned long long a) { return a; }\n"
    )

    def test_control_flow_is_not_a_definition(self):
        ol = outline("main.c", self.SRC)
        self.assertEqual(ol.lang, "c")
        self.assertEqual(ol.head, "C entry")
        got = names(ol)
        for bad in ("if", "while", "for", "printf", "return"):
            self.assertNotIn(bad, got)
        self.assertIn("helper", got)
        self.assertIn("main", got)
        self.assertIn("compute", got)
        self.assertIn("Node", got)
        self.assertEqual(kinds(ol)["MAX"], "macro")

    def test_names_starting_with_a_keyword_are_still_found(self):
        src = "void format(char *s) {\n}\nint newline_count(const char *s) {\n}\nstatic void do_work(void) {\n}\n"
        self.assertEqual(names(outline("a.c", src)), ["format", "newline_count", "do_work"])

    def test_cpp_classes_and_inline_methods(self):
        src = (
            "// C++ entry\n"
            "namespace app {\n"
            "template <typename T>\n"
            "class Box {\n"
            "public:\n"
            "    void put(const T& v) { item = v; }\n"
            "    T get() const { return item; }\n"
            "};\n"
            "struct Point { int x; };\n"
            "void freeFn(int a) {}\n"
            "}\n"
            "int main() { return 0; }\n"
        )
        ol = outline("main.cpp", src)
        self.assertEqual(ol.lang, "cpp")
        self.assertIn(("app", "mod"), [(n, k) for n, k, _ in ol.symbols])
        self.assertIn(("Box", "type"), [(n, k) for n, k, _ in ol.symbols])
        self.assertIn("put", names(ol))
        self.assertIn("get", names(ol))
        self.assertIn("freeFn", names(ol))
        self.assertIn("main", names(ol))
        self.assertNotIn("public", names(ol))

    def test_headers_use_the_c_family(self):
        self.assertEqual(LANG_BY_EXT[".h"], "c")
        self.assertEqual(LANG_BY_EXT[".hpp"], "cpp")


class OtherLanguageTests(unittest.TestCase):
    def test_rust(self):
        src = "// entry\npub struct Point { x: i32 }\nimpl Point {\n    pub fn new() -> Self { Self { x: 0 } }\n}\npub mod util {}\n"
        ol = outline("src/lib.rs", src)
        self.assertEqual(ol.lang, "rust")
        self.assertIn("Point", names(ol))
        self.assertIn("new", names(ol))
        self.assertIn("util", names(ol))

    def test_markdown_uses_headings(self):
        ol = outline("README.md", "# Title\n\ntext\n\n## Section\n\n### Deep\n#### Too deep\n")
        self.assertEqual(ol.lang, "markdown")
        self.assertEqual([k for _n, k, _ln in ol.symbols], ["h1", "h2", "h3"])
        self.assertEqual(names(ol), ["Title", "Section", "Deep"])
        self.assertEqual(ol.head, "Title")

    def test_unknown_extension_is_text_with_a_comment_head(self):
        ol = outline("notes.xyz", "# a comment\nbody\n")
        self.assertEqual(ol.lang, "text")
        self.assertEqual(ol.symbols, [])
        self.assertEqual(ol.head, "a comment")


class RenderTests(unittest.TestCase):
    def test_render_caps_symbols(self):
        src = "\n".join(f"export function fn{i}() {{}}" for i in range(MAX_SYMBOLS + 5))
        ol = outline("big.js", src)
        self.assertEqual(len(ol.symbols), MAX_SYMBOLS + 5)
        rendered = ol.render()
        self.assertIn("fn0:1", rendered)
        self.assertIn(f"(+5 more symbols)", rendered)
        self.assertNotIn(f"fn{MAX_SYMBOLS + 4}:{MAX_SYMBOLS + 5}", rendered)

    def test_render_includes_head_and_lines(self):
        ol = outline("a.py", '"""Doc."""\ndef f():\n    pass\n')
        self.assertEqual(ol.lines, 4)  # trailing newline still counts as a line
        self.assertTrue(ol.render().startswith("Doc."))
        self.assertIn("fn f:2", ol.render())


class ExtendedLanguageTests(unittest.TestCase):
    """Languages beyond the first round: one realistic sample each."""

    def test_elixir(self):
        src = (
            "# Elixir app\n"
            "defmodule MyApp.Accounts do\n"
            "  def list_users() do\n"
            "    []\n"
            "  end\n"
            "  defp normalize(user) do\n"
            "    user\n"
            "  end\n"
            "end\n"
            "defprotocol Size do\n"
            "  def size(data)\n"
            "end\n"
        )
        ol = outline("lib/accounts.ex", src)
        self.assertEqual(ol.lang, "elixir")
        self.assertEqual(ol.head, "Elixir app")
        self.assertEqual(kinds(ol), {
            "MyApp.Accounts": "mod", "list_users": "fn", "normalize": "fn",
            "Size": "type", "size": "fn",
        })

    def test_clojure(self):
        src = (
            ";; Clojure core\n"
            "(ns my.app.core)\n"
            "(def default-size 3)\n"
            "(defn start [opts] opts)\n"
            "(defn- helper [x] x)\n"
            "(defmacro with-retry [& body] body)\n"
        )
        ol = outline("src/core.clj", src)
        self.assertEqual(ol.lang, "clojure")
        self.assertEqual(ol.head, "Clojure core")
        self.assertEqual(kinds(ol), {
            "my.app.core": "mod", "default-size": "const", "start": "fn",
            "helper": "fn", "with-retry": "const",
        })

    def test_zig_skips_imports(self):
        src = (
            "// Zig entry\n"
            "const std = @import(\"std\");\n"
            "const Point = struct { x: i32 };\n"
            "pub fn main() !void {}\n"
            "pub fn add(a: i32, b: i32) i32 { return a + b; }\n"
        )
        ol = outline("src/main.zig", src)
        self.assertEqual(ol.lang, "zig")
        self.assertNotIn("std", names(ol))
        self.assertEqual(kinds(ol), {
            "Point": "type", "main": "fn", "add": "fn",
        })

    def test_julia(self):
        src = (
            "# Julia physics\n"
            "module Physics\n"
            "mutable struct Body\n"
            "    mass::Float64\n"
            "end\n"
            "function kinetic(body, v)\n"
            "    body.mass * v^2 / 2\n"
            "end\n"
            "energy(m, v) = m * v^2\n"
            "end\n"
        )
        ol = outline("src/physics.jl", src)
        self.assertEqual(ol.lang, "julia")
        self.assertEqual(kinds(ol), {
            "Physics": "mod", "Body": "type", "kinetic": "fn", "energy": "fn",
        })

    def test_r(self):
        src = (
            "# R analysis\n"
            "normalize <- function(x) {\n"
            "  (x - mean(x)) / sd(x)\n"
            "}\n"
            "summarize_df = function(df) df\n"
        )
        ol = outline("analysis.r", src)
        self.assertEqual(ol.lang, "r")
        self.assertEqual(names(ol), ["normalize", "summarize_df"])

    def test_haskell_definitions_not_signatures(self):
        src = (
            "-- Haskell entry\n"
            "module Main where\n"
            "data Shape = Circle Double | Square Double\n"
            "area :: Shape -> Double\n"
            "area (Circle r) = pi * r * r\n"
            "main :: IO ()\n"
            "main = putStrLn \"hi\"\n"
        )
        ol = outline("Main.hs", src)
        self.assertEqual(ol.lang, "haskell")
        self.assertEqual([(n, k) for n, k, _ in ol.symbols], [
            ("Main", "mod"), ("Shape", "type"), ("area", "fn"), ("main", "fn"),
        ])
        self.assertEqual(lines(ol)["area"], 5)

    def test_groovy(self):
        src = (
            "// Groovy build\n"
            "class Builder {\n"
            "    private final String name\n"
            "    def build() {\n"
            "        return name\n"
            "    }\n"
            "}\n"
            "def helper(x) { x * 2 }\n"
        )
        ol = outline("build.groovy", src)
        self.assertEqual(ol.lang, "groovy")
        self.assertEqual(kinds(ol), {"Builder": "class", "build": "fn", "helper": "fn"})

    def test_proto(self):
        src = (
            "// proto\n"
            "message User {\n"
            "  string name = 1;\n"
            "}\n"
            "enum Role { ADMIN = 0; }\n"
            "service Users {\n"
            "  rpc GetUser(User) returns (User);\n"
            "}\n"
        )
        ol = outline("api/service.proto", src)
        self.assertEqual(ol.lang, "proto")
        self.assertEqual(kinds(ol), {"User": "type", "Role": "type", "Users": "type", "GetUser": "fn"})

    def test_graphql(self):
        src = (
            "# GraphQL schema\n"
            "type User { id: ID! }\n"
            "input UserInput { name: String }\n"
            "enum Role { ADMIN USER }\n"
            "query GetUser($id: ID!) { user(id: $id) { id } }\n"
            "mutation CreateUser($input: UserInput!) { createUser(input: $input) { id } }\n"
        )
        ol = outline("schema.graphql", src)
        self.assertEqual(ol.lang, "graphql")
        self.assertEqual(kinds(ol), {
            "User": "type", "UserInput": "type", "Role": "type",
            "GetUser": "fn", "CreateUser": "fn",
        })

    def test_sql_is_case_insensitive(self):
        src = (
            "-- SQL schema\n"
            "CREATE TABLE IF NOT EXISTS users (id INTEGER);\n"
            "create or replace view active_users as select 1;\n"
            "CREATE FUNCTION greet(name TEXT) RETURNS TEXT;\n"
        )
        ol = outline("db/schema.sql", src)
        self.assertEqual(ol.lang, "sql")
        self.assertEqual(kinds(ol), {"users": "type", "active_users": "type", "greet": "fn"})


class ScannerContractTests(unittest.TestCase):
    """Guards that keep new languages from silently breaking the outline."""

    def test_every_extension_maps_to_a_scanner(self):
        special = {"python", "vue", "svelte", "go", "markdown", "text"}
        for ext, lang in LANG_BY_EXT.items():
            if lang in special:
                continue
            self.assertIn(lang, PATTERNS, f"{ext} -> {lang} has no PATTERNS entry")

    def test_every_pattern_compiles_and_captures_a_name(self):
        for lang in PATTERNS:
            for rx, kind in _compiled(lang):
                self.assertGreaterEqual(rx.groups, 1, f"{lang}/{kind} must capture the symbol name")

    def test_compiled_patterns_are_reused(self):
        self.assertIs(_compiled("js"), _compiled("js"))
        self.assertIs(_compiled("unknown-language"), _compiled("unknown-language"))


class ReadTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_utf8_bom_is_stripped(self):
        p = self.write("Main.kt", "\ufefffun main() {}\n".encode("utf-8"))
        raw, text = read_text(p)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # the hash still sees the bytes
        self.assertFalse(text.startswith("\ufeff"))
        self.assertIn("fn main:1", outline("Main.kt", text).render())

    def test_outline_tolerates_a_bom_too(self):
        ol = outline("Main.kt", "\ufefffun main() {}")
        self.assertEqual(ol.symbols[0][0], "main")

    def test_binary_is_rejected(self):
        self.assertIsNone(read_text(self.write("blob.bin", b"\0\1\2")))

    def test_oversized_is_rejected(self):
        self.assertIsNone(read_text(self.write("huge.js", b"x" * (MAX_BYTES + 1))))

    def test_missing_file(self):
        self.assertIsNone(read_text(os.path.join(self.tmp, "nope.py")))


class ListFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        self.project = Project(root=os.path.realpath(self.root), key="k", name="proj", is_git=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def touch(self, rel):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(p) else None
        with open(p, "w", encoding="utf-8") as f:
            f.write("x\n")

    def test_skips_generated_binary_and_secret_files(self):
        for rel in ("src/app.py", "node_modules/lib/index.js", "dist/bundle.js",
                    ".env", "server.key", "logo.png", "package-lock.json", ".git/config"):
            self.touch(rel)
        self.assertEqual(list_files(self.project), ["src/app.py"])

    def test_paths_use_forward_slashes(self):
        self.touch("src/deep/mod.py")
        self.assertIn("src/deep/mod.py", list_files(self.project))


if __name__ == "__main__":
    unittest.main()
