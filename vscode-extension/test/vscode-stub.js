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
  /**
   * Both real overloads, because the extension uses both.
   *
   * `vscode.Range` accepts either four numbers or two Positions.
   * Supporting only the numeric form made `new Range(pos, pos)` produce a
   * Position whose `.line` was itself a Position — which reads as a plain
   * assertion failure and sends you looking in the wrong file.
   */
  constructor(startLine, startChar, endLine, endChar) {
    if (startLine instanceof Position) {
      this.start = startLine;
      this.end = startChar;
      return;
    }
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

/**
 * A recording stand-in for `TestRun`.
 *
 * Behavioural like the rest of this stub: it stores which item was told what,
 * because "the right test went green" is the only assertion worth making about
 * a test run, and it is invisible if the calls are merely counted.
 */
class FakeTestRun {
  constructor() {
    this.states = new Map();
    this.messages = new Map();
    this.output = "";
    this.ended = false;
  }

  _record(item, state, message) {
    this.states.set(item.id, state);
    if (message !== undefined) {
      this.messages.set(item.id, message);
    }
  }

  enqueued(item) { this._record(item, "enqueued"); }
  started(item) { this._record(item, "started"); }
  passed(item) { this._record(item, "passed"); }
  skipped(item) { this._record(item, "skipped"); }
  failed(item, message) { this._record(item, "failed", message); }
  errored(item, message) { this._record(item, "errored", message); }
  appendOutput(text) { this.output += text; }
  end() { this.ended = true; }
}

/** A `TestItemCollection` that really stores and really iterates. */
function itemCollection() {
  const store = new Map();
  return {
    add(item) { store.set(item.id, item); },
    replace(items) {
      store.clear();
      for (const item of items) { store.set(item.id, item); }
    },
    forEach(fn) { for (const item of store.values()) { fn(item); } },
    get(id) { return store.get(id); },
    get size() { return store.size; },
  };
}

/** The controller `vscode.tests.createTestController` hands back. */
function fakeController(id, label) {
  return {
    id,
    label,
    items: itemCollection(),
    profiles: [],
    runs: [],
    resolveHandler: undefined,
    refreshHandler: undefined,
    createTestItem(itemId, itemLabel, uri) {
      return {
        id: itemId,
        label: itemLabel,
        uri,
        children: itemCollection(),
        range: undefined,
        description: undefined,
      };
    },
    createRunProfile(profileLabel, kind, handler, isDefault) {
      const profile = { label: profileLabel, kind, handler, isDefault };
      this.profiles.push(profile);
      return profile;
    },
    createTestRun(request) {
      const run = new FakeTestRun();
      run.request = request;
      this.runs.push(run);
      return run;
    },
    dispose() {},
  };
}

/** The most recently created controller, for tests to reach into. */
const controllers = [];

class EventEmitter {
  constructor() { this.listeners = []; }
  get event() {
    return (listener) => {
      this.listeners.push(listener);
      return { dispose: () => {} };
    };
  }
  fire(value) { for (const listener of this.listeners) { listener(value); } }
  dispose() { this.listeners = []; }
}

const vscodeStub = {
  workspace: {
    workspaceFolders: [],
    getConfiguration() {
      return {
        get(key, fallback) {
          return key in state.settings ? state.settings[key] : fallback;
        },
      };
    },
    getWorkspaceFolder(uri) {
      return (vscodeStub.workspace.workspaceFolders || []).find(
        (f) => uri.fsPath.startsWith(f.uri.fsPath));
    },
  },
  tests: {
    createTestController(id, label) {
      const controller = fakeController(id, label);
      controllers.push(controller);
      return controller;
    },
  },
  TestRunProfileKind: { Run: 1, Debug: 2, Coverage: 3 },
  TestMessage: class TestMessage {
    constructor(message) { this.message = message; }
  },
  EventEmitter,
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

/** A `CancellationToken` a test can trip by hand. */
function cancellation() {
  const listeners = [];
  const token = {
    isCancellationRequested: false,
    onCancellationRequested(listener) {
      listeners.push(listener);
      return { dispose: () => {} };
    },
  };
  return {
    token,
    cancel() {
      token.isCancellationRequested = true;
      for (const listener of listeners) { listener(); }
    },
  };
}

/** Set the folders `workspace.workspaceFolders` will report. */
function setFolders(folders) {
  vscodeStub.workspace.workspaceFolders = folders;
}

module.exports = {
  vscodeStub, setSettings, fakeCollection, folder, controllers, cancellation,
  setFolders, FakeTestRun,
};
