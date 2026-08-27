/**
 * test/vscode-stub.js — enough of the editor API to run this extension's
 * decision logic headless.
 *
 * The alternative is `@vscode/test-electron`, which downloads a VS Code build
 * and runs a real extension host. That is the right tool for testing the tree
 * and the command wiring, and badly overpriced for the parts that are just
 * functions: which runner to resolve, what an exit code means, whether a
 * schema is one we understand, and how a finding becomes a squiggle.
 *
 * The stub is **behavioural, not a mock**. `Range` keeps its numbers,
 * `Uri.joinPath` really joins, and the collection really stores what it is
 * handed — because the properties worth asserting (a 1-based coordinate
 * becoming 0-based, a repo-relative path resolving into the right workspace
 * root) are only meaningful if those parts behave like the real thing.
 *
 * Requiring this module installs the `Module._load` hook, so it must be
 * required BEFORE anything that imports `vscode`.
 */
"use strict";

const Module = require("node:module");
const path = require("node:path");

/** Mutable per-test settings backing `workspace.getConfiguration`. */
const state = { settings: {} };

class Position {
  constructor(line, character) {
    this.line = line;
    this.character = character;
  }
}

class Range {
  constructor(startLine, startChar, endLine, endChar) {
    this.start = new Position(startLine, startChar);
    this.end = new Position(endLine, endChar);
  }
}

class Diagnostic {
  constructor(range, message, severity) {
    this.range = range;
    this.message = message;
    this.severity = severity;
  }
}

const vscodeStub = {
  workspace: {
    getConfiguration() {
      return {
        get(key, fallback) {
          return key in state.settings ? state.settings[key] : fallback;
        },
      };
    },
  },
  Position,
  Range,
  Diagnostic,
  DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 },
  Uri: {
    file(fsPath) {
      return { fsPath, scheme: "file", toString: () => `file://${fsPath}` };
    },
    joinPath(uri, ...parts) {
      return vscodeStub.Uri.file(path.join(uri.fsPath, ...parts));
    },
  },
};

const originalLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === "vscode") {
    return vscodeStub;
  }
  return originalLoad.call(this, request, ...rest);
};

/** Replace the settings `getConfiguration` will report. */
function setSettings(settings) {
  state.settings = settings || {};
}

/**
 * A stand-in for a `DiagnosticCollection` that records what it was told.
 *
 * Reading it back is how a test sees what the Problems panel would show.
 */
function fakeCollection() {
  const store = new Map();
  return {
    set(uri, diags) { store.set(uri.fsPath, diags); },
    delete(uri) { store.delete(uri.fsPath); },
    clear() { store.clear(); },
    get(fsPath) { return store.get(fsPath); },
    get size() { return store.size; },
    paths() { return [...store.keys()]; },
  };
}

/** A stand-in for a `WorkspaceFolder` rooted at *fsPath*. */
function folder(fsPath) {
  return { name: path.basename(fsPath), uri: vscodeStub.Uri.file(fsPath) };
}

module.exports = { vscodeStub, setSettings, fakeCollection, folder };
