"""Record a real two-agent memdex session and render it as a README GIF.

The commands are executed for real against a throwaway project and MEMDEX_HOME;
the frames only lay out the captured stdout. No fake output.

    python make_demo.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CACHE = Path(os.environ.get("BRAND_FONT_CACHE", r"C:\Users\cho46\.cache\brand-identity\fonts\google\ofl"))
FONT_MONO = Path(os.environ.get("DEMO_FONT_MONO", CACHE / "ibmplexmono" / "IBMPlexMono-Regular.ttf"))
FONT_DISPLAY = Path(os.environ.get("DEMO_FONT_DISPLAY", CACHE / "hahmlet" / "Hahmlet[wght].ttf"))

INK = (14, 26, 28)
PANE = (16, 32, 36)
PANE_EDGE = (42, 58, 62)
BONE = (236, 229, 214)
AMBER = (240, 176, 63)
MUTED = (150, 170, 172)
DIM = (104, 126, 128)

W, H = 1000, 620
TEXT_LEFT = 32          # text starts this far from the pane's left edge
TEXT_RIGHT = 14         # and stops this far from its right edge
LINE_H = 23


def run_cli(env, project, *args):
    cmd = [sys.executable, "-m", "memdex", *args]
    r = subprocess.run(cmd, cwd=project, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    return [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()]


def capture():
    tmp = tempfile.mkdtemp(prefix="memdex-demo-")
    project = os.path.join(tmp, "demo-app")
    os.makedirs(os.path.join(project, "src"))
    Path(project, "src", "store.py").write_text(
        "class Store:\n    def get(self, key):\n        return self._rows.get(key)\n", encoding="utf-8")
    Path(project, "src", "api.py").write_text("def handler(req):\n    return {}\n", encoding="utf-8")
    env = dict(os.environ, MEMDEX_HOME=os.path.join(tmp, "home"), PYTHONIOENCODING="utf-8")
    env.pop("MEMDEX_AGENT", None)

    steps = []

    def step(agent, command, *args, keep=4):
        steps.append({"agent": agent, "command": command, "output": run_cli(env, project, *args)[:keep]})

    step("claude", "memdex brief --agent claude", "brief", "--agent", "claude", keep=2)
    step("claude", 'memdex claim src/store.py --intent "add retry"',
         "claim", "src/store.py", "--intent", "add retry", "--agent", "claude", keep=1)
    step("codex", 'memdex claim src/store.py --intent "fix timeout"',
         "claim", "src/store.py", "--intent", "fix timeout", "--agent", "codex", keep=3)
    step("codex", 'memdex post "store.py is yours; I take src/api.py" --to claude',
         "post", "store.py is yours; I take src/api.py", "--to", "claude", "--agent", "codex", keep=1)
    step("claude", "memdex inbox", "inbox", "--agent", "claude", keep=1)
    step("claude", 'memdex handoff "retry + backoff in store.py" --next "wire timeout to config"',
         "handoff", "retry + backoff in store.py", "--next", "wire timeout to config",
         "--pitfalls", "jitter required", "--agent", "claude", keep=2)
    step("codex", "memdex brief --agent codex", "brief", "--agent", "codex", keep=5)

    shutil.rmtree(tmp, ignore_errors=True)
    return steps


class Render:
    def __init__(self, steps):
        self.steps = steps
        self.mono = ImageFont.truetype(str(FONT_MONO), 16)
        self.small = ImageFont.truetype(str(FONT_MONO), 13)
        self.display = ImageFont.truetype(str(FONT_DISPLAY), 30)
        self.big = ImageFont.truetype(str(FONT_DISPLAY), 44)
        self.panes = {
            "claude": {"x": 28, "w": 456, "y": 112, "h": 436, "lines": []},
            "codex": {"x": 516, "w": 456, "y": 112, "h": 436, "lines": []},
        }

    def max_px(self, pane):
        return pane["w"] - TEXT_LEFT - TEXT_RIGHT

    def wrap(self, text, pane):
        d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        limit = self.max_px(pane)
        out, cur = [], ""
        for word in text.split(" "):
            trial = word if not cur else f"{cur} {word}"
            if d.textlength(trial, font=self.mono) <= limit or not cur:
                cur = trial
            else:
                out.append(cur)
                cur = word
        if cur:
            out.append(cur)
        return out

    def header(self, d):
        d.text((28, 22), "memdex", font=self.display, fill=BONE)
        d.text((150, 34), "· one memory, every agent", font=self.small, fill=AMBER)
        label = "github.com/ChoYugyeong/memdex"
        d.text((W - 28 - d.textlength(label, font=self.small), 34), label, font=self.small, fill=DIM)

    def footer(self, d):
        d.text((28, H - 34), "same checkout · two agents · zero file conflicts", font=self.small, fill=MUTED)
        inst = "pipx install memdex-mcp"
        d.text((W - 28 - d.textlength(inst, font=self.small), H - 34), inst, font=self.small, fill=AMBER)

    def title_card(self):
        img = Image.new("RGB", (W, H), INK)
        d = ImageDraw.Draw(img)
        self.header(d)
        y = 190
        for ln in ("Two agents.", "One checkout.", "One memory."):
            d.text((W // 2 - d.textlength(ln, font=self.big) // 2, y), ln, font=self.big, fill=BONE)
            y += 60
        sub = "claude  +  codex  —  no file conflicts"
        d.text((W // 2 - d.textlength(sub, font=self.small) // 2, y + 30), sub, font=self.small, fill=AMBER)
        self.footer(d)
        return img

    def frame(self, active):
        img = Image.new("RGB", (W, H), INK)
        d = ImageDraw.Draw(img)
        self.header(d)
        for key, p in self.panes.items():
            x, y, w, h = p["x"], p["y"], p["w"], p["h"]
            d.rounded_rectangle([x, y, x + w, y + h], radius=12, fill=PANE, outline=PANE_EDGE, width=2)
            d.rounded_rectangle([x, y, x + w, y + 38], radius=12, fill=(22, 40, 44))
            d.line([x, y + 38, x + w, y + 38], fill=PANE_EDGE, width=2)
            d.text((x + 14, y + 10), f"{key}  >  ~/demo-app", font=self.small,
                   fill=(AMBER if key == active else (200, 210, 210)))
            cy = y + 54
            for kind, text in p["lines"]:
                if kind == "cmd":
                    d.text((x + 14, cy), "$", font=self.mono, fill=AMBER)
                    d.text((x + TEXT_LEFT, cy), text, font=self.mono, fill=BONE)
                elif kind == "cmd2":
                    d.text((x + TEXT_LEFT, cy), text, font=self.mono, fill=BONE)
                else:
                    d.text((x + TEXT_LEFT, cy), text, font=self.mono, fill=MUTED)
                cy += LINE_H
            if key == active:
                d.rectangle([x + 14, cy + 2, x + 22, cy + 16], fill=AMBER)
        self.footer(d)
        return img

    def clean(self, s):
        return (s.replace("\u26a0\ufe0f", "!").replace("\u26a0", "!").replace("\u2192", ">")
                 .replace("\u2713", "ok"))

    def add(self, agent, command, output):
        p = self.panes[agent]
        wrapped = self.wrap(self.clean(command), p)
        p["lines"].append(("cmd", wrapped[0]))
        for cont in wrapped[1:]:
            p["lines"].append(("cmd2", cont))
        for ln in output:
            for w in self.wrap(self.clean(ln), p):
                p["lines"].append(("out", w))
        max_lines = (p["h"] - 66) // LINE_H
        if len(p["lines"]) > max_lines:
            p["lines"] = [("out", "…")] + p["lines"][-(max_lines - 1):]

    def run(self):
        frames, durations = [], []
        frames.append(self.title_card())
        durations.append(2200)
        for s in self.steps:
            self.add(s["agent"], s["command"], s["output"])
            frames.append(self.frame(s["agent"]))
            durations.append(1700 if "brief" in s["command"] else 1250)
        frames.append(self.frame("codex"))
        durations.append(3000)
        return frames, durations


def main():
    steps = capture()
    r = Render(steps)
    frames, durations = r.run()
    out = Path(r"C:\Dev\brand-work\demo.gif")
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   optimize=True, disposal=2)
    for i in (0, 4, len(frames) - 1):
        frames[i].save(Path(rf"C:\Dev\brand-work\frame-{i:02d}.png"))
    print("gif:", out, out.stat().st_size, "bytes,", len(frames), "frames,",
          round(sum(durations) / 1000, 1), "s")


if __name__ == "__main__":
    main()


