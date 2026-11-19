const { spawn } = require("node:child_process");
const readline = require("node:readline");

class Backend {
  constructor(python, args, cwd) {
    this.process = spawn(python, args, { cwd, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    this.next = 0;
    this.pending = new Map();
    let diagnostic = "";
    this.process.stderr.setEncoding("utf8").on("data", text => { diagnostic = (diagnostic + text).slice(-2000); });
    const fail = error => {
      this.error = error;
      for (const job of this.pending.values()) job.reject(error);
      this.pending.clear();
    };
    this.process.on("error", fail);
    this.process.stdin.on("error", fail);
    this.process.on("close", () => fail(new Error(diagnostic || "Memdex backend closed.")));
    readline.createInterface({ input: this.process.stdout }).on("line", line => {
      let result;
      try { result = JSON.parse(line); } catch { return fail(new Error("Invalid Memdex backend response.")); }
      const job = this.pending.get(result.id);
      if (!job) return;
      this.pending.delete(result.id);
      if (result.error) job.reject(new Error(result.error));
      else job.resolve(result.value);
    });
  }
  request(method, args = {}) {
    if (this.error) return Promise.reject(this.error);
    const id = ++this.next;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.process.stdin.write(JSON.stringify({ id, method, args }) + "\n");
    });
  }
  close() { this.process.stdin.end(); }
}
module.exports = Backend;
