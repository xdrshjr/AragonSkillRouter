#!/usr/bin/env node
// Thin launcher. All behaviour lives in ../src/cli.js so it can be imported by tests.
import { run } from "../src/cli.js";

run(process.argv.slice(2)).then(
  (code) => {
    process.exitCode = code;
  },
  (error) => {
    // Anything that escapes the CLI's own error handling is a bug; say so plainly
    // instead of printing a stack trace at an operator.
    process.stderr.write(`skill-router-connect: unexpected failure: ${error && error.message ? error.message : String(error)}\n`);
    process.exitCode = 1;
  },
);
