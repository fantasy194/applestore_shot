import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";

const source = stripTypeScriptTypes(
  readFileSync(new URL("../src/uuid.ts", import.meta.url), "utf8"),
).replace(/^export /m, "");
const uuidV4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function load(crypto) {
  return runInNewContext(`${source}\nrandomUUID;`, {
    crypto,
    Math: {
      random() {
        throw new Error("insecure randomness is forbidden");
      },
    },
  });
}

test("uses native UUID with the correct Crypto receiver when available", () => {
  const crypto = {
    randomUUID() {
      assert.equal(this, crypto);
      return "11111111-1111-4111-8111-111111111111";
    },
    getRandomValues() {
      throw new Error("fallback should not run");
    },
  };
  assert.equal(load(crypto)(), "11111111-1111-4111-8111-111111111111");
});

test("HTTP fallback forces RFC 4122 version and variant bits", () => {
  let calls = 0;
  const generate = load({
    getRandomValues(bytes) {
      calls++;
      assert.equal(bytes.length, 16);
      return bytes.fill(255);
    },
  });
  assert.equal(generate(), "ffffffff-ffff-4fff-bfff-ffffffffffff");
  assert.equal(calls, 1);
});

test("HTTP fallback works with actual Web Crypto and produces distinct valid IDs", () => {
  const generate = load({
    getRandomValues: webcrypto.getRandomValues.bind(webcrypto),
  });
  const values = Array.from({ length: 64 }, () => generate());
  for (const value of values) assert.match(value, uuidV4);
  assert.equal(new Set(values).size, values.length);
});
