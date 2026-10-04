import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  ALLOWED_ENDPOINTS,
  buildSafeCapture,
  flattenJsonPaths,
  isAllowedEndpoint,
  safeCaptureJson,
  safeStringify,
  settlePendingSummaries,
  summarizeSafeRecord,
  summarizeRequest,
  summarizeResponse,
  trackSafeSummary,
  validateTempProfileForRemoval,
} from "./capture-kepco-login-schema.mjs";

const USERNAME_CANARY = "USERNAME_CANARY_DO_NOT_WRITE";
const PASSWORD_CANARY = "PASSWORD_CANARY_DO_NOT_WRITE";

test("flattenJsonPaths records nested object and array types deterministically", () => {
  const paths = flattenJsonPaths({
    zed: [{ token: "abc" }],
    alpha: { beta: null, count: 3, ok: true },
  });

  assert.deepEqual(paths, [
    { path: "$.alpha", type: "object" },
    { path: "$.alpha.beta", type: "null", isNull: true },
    { path: "$.alpha.count", type: "number" },
    { path: "$.alpha.ok", type: "boolean" },
    { path: "$.zed", type: "array", length: 1 },
    { path: "$.zed[0]", type: "object" },
    {
      path: "$.zed[0].token",
      type: "string",
      length: 3,
      matchesBase64: false,
      matchesHex: false,
      matchesJwt: false,
      matchesPassword: false,
      matchesUsername: false,
    },
  ]);
});

test("request summary keeps only endpoint, method, submissionid, and safe body metadata", () => {
  const summary = summarizeRequest(
    {
      url: "https://online.kepco.co.kr/cyb/me/login/indi/api",
      method: "POST",
      headers: { submissionid: "mf_login_popup_wframe_sbm_submission4" },
      postDataJSON: {
        userId: USERNAME_CANARY,
        pwdVal: PASSWORD_CANARY,
        autoFlag: "N",
        nested: ["value"],
      },
    },
    { username: USERNAME_CANARY, password: PASSWORD_CANARY },
  );

  assert.equal(summary.endpoint, "/cyb/me/login/indi/api");
  assert.equal(summary.method, "POST");
  assert.equal(summary.submissionid, "mf_login_popup_wframe_sbm_submission4");
  assert.equal(summary.allowed, true);
  assert.deepEqual(
    summary.bodyPaths.filter((item) => item.type === "string"),
    [
      {
        path: "$.autoFlag",
        type: "string",
        length: 1,
        matchesBase64: false,
        matchesHex: false,
        matchesJwt: false,
        matchesPassword: false,
        matchesUsername: false,
      },
      {
        path: "$.nested[0]",
        type: "string",
        length: 5,
        matchesBase64: false,
        matchesHex: false,
        matchesJwt: false,
        matchesPassword: false,
        matchesUsername: false,
      },
      {
        path: "$.pwdVal",
        type: "string",
        length: PASSWORD_CANARY.length,
        matchesBase64: false,
        matchesHex: false,
        matchesJwt: false,
        matchesPassword: true,
        matchesUsername: false,
      },
      {
        path: "$.userId",
        type: "string",
        length: USERNAME_CANARY.length,
        matchesBase64: false,
        matchesHex: false,
        matchesJwt: false,
        matchesPassword: false,
        matchesUsername: true,
      },
    ],
  );
  assert.equal(JSON.stringify(summary).includes(PASSWORD_CANARY), false);
  assert.equal(JSON.stringify(summary).includes(USERNAME_CANARY), false);
});

test("response summary records status, content type, key paths, and secret metadata only", () => {
  const summary = summarizeResponse({
    status: 200,
    headers: { "content-type": "application/json; charset=UTF-8" },
    json: {
      result: "YES",
      errorCode: "0",
      refreshToken: "abc.def.ghi",
      nested: { access_token: "0123456789abcdef0123456789abcdef" },
    },
  });

  assert.equal(summary.status, 200);
  assert.equal(summary.contentType, "application/json; charset=UTF-8");
  assert.deepEqual(summary.successFailureCodeFields, [
    "$.errorCode",
    "$.result",
  ]);
  assert.deepEqual(summary.secretFields, [
    { path: "$.nested.access_token", name: "access_token", length: 32 },
    { path: "$.refreshToken", name: "refreshToken", length: 11 },
  ]);
  assert.equal(JSON.stringify(summary).includes("abc.def.ghi"), false);
  assert.equal(JSON.stringify(summary).includes("0123456789abcdef"), false);
});

test("safe capture rejects unallowed endpoints and sorts captured records", () => {
  const capture = buildSafeCapture(
    [
      {
        request: {
          url: "https://online.kepco.co.kr/ssoCheck",
          method: "POST",
          headers: {},
          postDataJSON: { userId: "u" },
        },
        response: { status: 200, headers: {}, json: { loginChk: "Y" } },
      },
      {
        request: {
          url: "https://online.kepco.co.kr/not-allowed",
          method: "POST",
          headers: {},
          postDataJSON: { raw: "ignored" },
        },
        response: { status: 200, headers: {}, json: {} },
      },
    ],
    { username: "u", password: "p" },
  );

  assert.deepEqual(
    capture.records.map((record) => record.endpoint),
    ["/ssoCheck"],
  );
  assert.deepEqual(capture.allowedEndpoints, [...ALLOWED_ENDPOINTS]);
  assert.equal(isAllowedEndpoint("https://online.kepco.co.kr/not-allowed"), false);
});

test("summarizeSafeRecord stores only safe metadata without raw headers or bodies", () => {
  const record = summarizeSafeRecord(
    {
      request: {
        url: "https://online.kepco.co.kr/cyb/me/login/indi/api",
        method: "POST",
        headers: {
          submissionid: "mf_login_popup_wframe_sbm_submission4",
          cookie: "RAW_COOKIE_SHOULD_NOT_WRITE",
        },
        postDataJSON: {
          userId: USERNAME_CANARY,
          pwdVal: PASSWORD_CANARY,
        },
      },
      response: {
        status: 200,
        headers: {
          "content-type": "application/json",
          "set-cookie": "RAW_RESPONSE_COOKIE_SHOULD_NOT_WRITE",
        },
        json: {
          refreshToken: "abc.def.ghi",
          mbrsNm: USERNAME_CANARY,
        },
      },
    },
    { username: USERNAME_CANARY, password: PASSWORD_CANARY },
  );

  assert.equal(record.endpoint, "/cyb/me/login/indi/api");
  assert.equal(record.method, "POST");
  assert.equal("headers" in record, false);
  assert.equal("body" in record, false);
  assert.equal("postData" in record, false);
  assert.equal("postDataJSON" in record, false);
  assert.equal("responseJson" in record, false);
  assert.equal(JSON.stringify(record).includes(USERNAME_CANARY), false);
  assert.equal(JSON.stringify(record).includes(PASSWORD_CANARY), false);
  assert.equal(JSON.stringify(record).includes("RAW_COOKIE_SHOULD_NOT_WRITE"), false);
});

test("settlePendingSummaries waits for delayed records and reports safe failures", async () => {
  const pending = new Set();
  const records = [];
  const failures = [];

  trackSafeSummary(
    pending,
    records,
    failures,
    new Promise((resolve) =>
      setTimeout(
        () =>
          resolve({
            sequence: 2,
            endpoint: "/ssoCheck",
            method: "POST",
            request: { bodyPaths: [] },
            response: { status: 200 },
          }),
        20,
      ),
    ),
    2,
  );
  trackSafeSummary(
    pending,
    records,
    failures,
    Promise.reject(new Error("RAW_TOKEN_SHOULD_NOT_WRITE")),
    1,
  );

  const settled = await settlePendingSummaries(pending, records, failures);

  assert.equal(settled.total, 2);
  assert.equal(settled.failures.length, 1);
  assert.deepEqual(records.map((record) => record.endpoint), ["/ssoCheck"]);
  assert.equal(JSON.stringify(settled).includes("RAW_TOKEN_SHOULD_NOT_WRITE"), false);
});

test("safe capture JSON and hash are stable across different completion orders", () => {
  const first = safeCaptureJson([
    {
      sequence: 2,
      endpoint: "/ssoCheck",
      method: "POST",
      request: { endpoint: "/ssoCheck", method: "POST", bodyPaths: [] },
      response: { status: 200, contentType: "application/json", keyPaths: [] },
    },
    {
      sequence: 1,
      endpoint: "/ssoCheck",
      method: "POST",
      request: {
        endpoint: "/ssoCheck",
        method: "POST",
        bodyPaths: [{ path: "$.a", type: "number" }],
      },
      response: { status: 200, contentType: "application/json", keyPaths: [] },
    },
  ]);
  const second = safeCaptureJson([
    {
      sequence: 99,
      endpoint: "/ssoCheck",
      method: "POST",
      request: {
        endpoint: "/ssoCheck",
        method: "POST",
        bodyPaths: [{ path: "$.a", type: "number" }],
      },
      response: { status: 200, contentType: "application/json", keyPaths: [] },
    },
    {
      sequence: 7,
      endpoint: "/ssoCheck",
      method: "POST",
      request: { endpoint: "/ssoCheck", method: "POST", bodyPaths: [] },
      response: { status: 200, contentType: "application/json", keyPaths: [] },
    },
  ]);

  assert.equal(first.json, second.json);
  assert.equal(first.hash, second.hash);
  assert.equal(first.json.includes("generatedAt"), false);
  const parsed = JSON.parse(first.json);
  assert.equal(parsed.records.length, 2);
  assert.deepEqual(
    parsed.records.map((record) => record.request.bodyPaths.length),
    [0, 1],
  );
});

test("safeStringify is deterministic and fails closed when canaries are present", () => {
  const safe = safeStringify({ b: 2, a: { d: 4, c: 3 } }, {
    username: USERNAME_CANARY,
    password: PASSWORD_CANARY,
  });
  assert.equal(safe, '{\n  "a": {\n    "c": 3,\n    "d": 4\n  },\n  "b": 2\n}\n');

  assert.throws(
    () => safeStringify({ value: PASSWORD_CANARY }, { username: "u", password: PASSWORD_CANARY }),
    /Refusing to write/,
  );
});

test("safeStringify rejects direct and nested sensitive property values", () => {
  const sensitivePayloads = [
    { cookie: "COOKIE_SECRET_SHOULD_NOT_WRITE" },
    { token: "abcdefghijklmnopqrstuvwxyz" },
    { pwdVal: "PASSWORD_SECRET_SHOULD_NOT_WRITE" },
    { userId: "USER_ID_SECRET_SHOULD_NOT_WRITE" },
    { mbrsNm: "MEMBER_NAME_SECRET_SHOULD_NOT_WRITE" },
    { userMngSeqno: "USER_SEQ_SECRET_SHOULD_NOT_WRITE" },
    { custNo: "CUSTOMER_SECRET_SHOULD_NOT_WRITE" },
    { housCntrNo: "HOUSE_CONTRACT_SECRET_SHOULD_NOT_WRITE" },
    { CUST_NO: "CUSTOMER_SECRET_SHOULD_NOT_WRITE" },
    { SI_CUST_NO: "HOUSE_CONTRACT_SECRET_SHOULD_NOT_WRITE" },
    { NAME: "NAME_SECRET_SHOULD_NOT_WRITE" },
    { ADDR: "ADDRESS_SECRET_SHOULD_NOT_WRITE" },
    { USER_MTEL: "PHONE_SECRET_SHOULD_NOT_WRITE" },
    { USER_EMAIL_ADDR: "EMAIL_SECRET_SHOULD_NOT_WRITE" },
    { name: "REAL_MEMBER_NAME" },
    { nested: { cookie: "COOKIE_SECRET_SHOULD_NOT_WRITE" } },
    { nested: { name: "REAL_MEMBER_NAME" } },
    { list: [{ token: "abcdefghijklmnopqrstuvwxyz" }] },
    { auth: { value: "AUTH_SECRET_SHOULD_NOT_WRITE" } },
    { session: { id: "SESSION_SECRET_SHOULD_NOT_WRITE" } },
  ];

  for (const payload of sensitivePayloads) {
    assert.throws(() => safeStringify(payload), /Refusing to write/);
  }
});

test("safeStringify permits schema metadata for sensitive field names", () => {
  const serialized = safeStringify({
    fields: [
      {
        path: "$.refreshToken",
        type: "string",
        key: "refreshToken",
        name: "mbrsNm",
        length: 26,
        secret: true,
      },
    ],
  });

  assert.match(serialized, /"\$\.refreshToken"/);
  assert.match(serialized, /"refreshToken"/);
  assert.match(serialized, /"mbrsNm"/);
  assert.match(serialized, /"secret": true/);
});

test("safeStringify treats lowercase name as schema metadata only in strict metadata objects", () => {
  const allowed = safeStringify({
    fields: [
      {
        path: "$.mbrsNm",
        type: "string",
        name: "mbrsNm",
        length: 9,
        secret: true,
      },
    ],
  });

  assert.match(allowed, /"\$\.mbrsNm"/);
  assert.match(allowed, /"name": "mbrsNm"/);
  assert.throws(() => safeStringify({ field: { path: "$.mbrsNm", name: "mbrsNm" } }), /Refusing to write/);
  assert.throws(
    () => safeStringify({ field: { path: "$.mbrsNm", type: "string", name: "mbrsNm", raw: "unexpected" } }),
    /Refusing to write/,
  );
});

test("temp profile cleanup guard allows only the tool mkdtemp prefix under OS temp", () => {
  const profile = mkdtempSync(join(tmpdir(), "kepco-login-schema-"));
  try {
    assert.equal(validateTempProfileForRemoval(profile), profile);
    assert.throws(() => validateTempProfileForRemoval(tmpdir()), /Refusing to remove/);
    assert.throws(() => validateTempProfileForRemoval(process.cwd()), /Refusing to remove/);
  } finally {
    rmSync(profile, { recursive: true, force: true });
  }
});
