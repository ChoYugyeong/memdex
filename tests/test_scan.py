"""outline(): per-language symbol extraction and header detection.

Memory is shared across agents through cheap outlines, so a wrong pattern is
worse than no pattern: these tests pin the languages that used to be lumped
together ("jvm" for Java/Kotlin/C#/Swift/Dart) and the ones that used to
produce false positives (C/C++ control flow, `<template>` markup in Vue).
"""

import unittest

from memdex.scan import LANG_BY_EXT, MAX_SYMBOLS, outline


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


if __name__ == "__main__":
    unittest.main()
