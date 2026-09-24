/**
 * The record of a driven run: every step, every check, every screenshot.
 *
 * Written as Markdown (for a terminal and for a pull request) and as one HTML
 * page (for looking at). This is what stands in for watching the run: a reader
 * who was not there can see what the editor showed at each step and which claims
 * were checked.
 */

"use strict";

const fs = require("node:fs");
const path = require("node:path");

const esc = (text) =>
  String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

class Report {
  constructor(outDir, name) {
    this.dir = path.join(outDir, name);
    fs.rmSync(this.dir, { recursive: true, force: true });
    fs.mkdirSync(this.dir, { recursive: true });
    this.name = name;
    this.started = new Date();
    this.steps = [];
    this.current = null;
    this.shots = 0;
    this.notes = [];
  }

  begin(title) {
    this.current = { title, status: "running", checks: [], shots: [], notes: [], started: Date.now() };
    this.steps.push(this.current);
    console.log(`\n> ${title}`);
  }

  pass() {
    if (this.current) {
      this.current.status = this.current.checks.some((c) => !c.ok) ? "failed" : "passed";
      this.current.ms = Date.now() - this.current.started;
    }
  }

  fail(error) {
    if (this.current) {
      this.current.status = "failed";
      this.current.error = String(error?.stack ?? error);
      this.current.ms = Date.now() - this.current.started;
    }
    console.log(`  FAILED: ${error?.message ?? error}`);
  }

  note(text) {
    (this.current ? this.current.notes : this.notes).push(text);
    console.log(`  ${text}`);
  }

  check(label, ok, detail) {
    const entry = { label, ok, detail };
    (this.current ? this.current.checks : (this.current = { checks: [] }).checks).push(entry);
    console.log(`  ${ok ? "ok  " : "FAIL"} ${label}${detail ? `  ${detail}` : ""}`);
  }

  nextShot(label) {
    this.shots += 1;
    const slug = label.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60);
    return path.join(this.dir, `${String(this.shots).padStart(2, "0")}-${slug}.png`);
  }

  attach(file, label) {
    if (this.current?.shots) {
      this.current.shots.push({ file: path.basename(file), label });
    }
  }

  get failed() {
    return this.steps.some((s) => s.status === "failed" || s.checks.some((c) => !c.ok));
  }

  write() {
    const lines = [`# ${this.name}`, "", `Run ${this.started.toISOString()}`, ""];
    for (const note of this.notes) {
      lines.push(`- ${note}`);
    }
    for (const step of this.steps) {
      lines.push("", `## ${step.title}  \`${step.status}\``, "");
      for (const note of step.notes) {
        lines.push(`- ${note}`);
      }
      for (const c of step.checks) {
        lines.push(`- [${c.ok ? "x" : " "}] ${c.label}${c.detail ? ` — ${c.detail}` : ""}`);
      }
      if (step.error) {
        lines.push("", "```", step.error, "```");
      }
      for (const shot of step.shots) {
        lines.push("", `![${shot.label}](${shot.file})`);
      }
    }
    fs.writeFileSync(path.join(this.dir, "report.md"), lines.join("\n") + "\n", "utf8");

    const body = this.steps
      .map(
        (step) => `<section class="${step.status}">
  <h2>${esc(step.title)} <small>${step.status}</small></h2>
  ${step.notes.map((n) => `<p>${esc(n)}</p>`).join("")}
  <ul>${step.checks
    .map((c) => `<li class="${c.ok ? "ok" : "bad"}">${c.ok ? "✓" : "✗"} ${esc(c.label)}${c.detail ? ` <em>${esc(c.detail)}</em>` : ""}</li>`)
    .join("")}</ul>
  ${step.error ? `<pre>${esc(step.error)}</pre>` : ""}
  ${step.shots.map((s) => `<figure><img src="${esc(s.file)}" alt=""><figcaption>${esc(s.label)}</figcaption></figure>`).join("")}
</section>`,
      )
      .join("\n");
    fs.writeFileSync(
      path.join(this.dir, "index.html"),
      `<!doctype html><meta charset="utf-8"><title>${esc(this.name)}</title>
<style>
  body{font:14px system-ui;margin:2rem auto;max-width:1100px;color:#222}
  section{border-left:4px solid #999;padding:0 1rem;margin:1.5rem 0}
  section.passed{border-color:#2a9d5c} section.failed{border-color:#d33}
  small{font-weight:400;color:#666} .ok{color:#1d7a46} .bad{color:#c22}
  img{max-width:100%;border:1px solid #ccc} figure{margin:.8rem 0} figcaption{color:#666;font-size:12px}
  pre{background:#f6f6f6;padding:.6rem;overflow:auto}
</style>
<h1>${esc(this.name)}</h1><p>${this.started.toISOString()}</p>
${this.notes.map((n) => `<p>${esc(n)}</p>`).join("")}
${body}`,
      "utf8",
    );
    console.log(`\nreport: ${path.join(this.dir, "index.html")}`);
  }
}

module.exports = { Report };
